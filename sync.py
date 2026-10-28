"""Daily Plaud sync: download new recordings, verify them, transcribe and diarize, and report to Slack.

An interim take on SPEC M1/M2: it drives the scripts in ~/projects/pyarize/scripts until they move
here. Run it from cron through sync_cron.sh, which loads ~/.bashrc_env (SLACK_WEBHOOK_URL), puts
node on PATH for the Plaud CLI, and holds a lock so runs never overlap.

- New recordings arrive `unfiled`, then brief.py files each under its suggested category (interview,
  1:1 meeting, group meeting, family) and adds a suggested title and short summary (gemma4 on
  llm-server; skipped if it's unreachable, and `brief.py --missing` catches up later). Fix the
  category and add a title on the review page.
- Drift policy (SPEC §3.1): up to 3 s is fine; 3-30 s is accepted if the file decodes cleanly
  and is flagged as a short cloud copy; more than 30 s is refused and alerted.
- Slack messages carry local start times and durations only, never titles or transcript text
  (SPEC §5). Nothing is posted when there's nothing new and nothing wrong.
- A recording that fails is alerted once, then remembered in the state file and retried quietly.

usage: python3 -I sync.py [--dry-run]
"""

import argparse
import base64
import csv
import hashlib
import json
import os
import re
import subprocess
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path.home()
# Machine-specific settings come from the environment; sync_cron.sh loads ~/.config/earshot/env.
PYARIZE = Path(os.environ.get("EARSHOT_PYARIZE_DIR", HOME / "projects" / "pyarize"))
WHISPER_PY = os.environ.get("EARSHOT_WHISPER_PYTHON", "python3")  # a Python with faster-whisper installed
ROOT = HOME / "recordings"
CSV_PATH = ROOT / "folders.csv"
AUDIO, TRANSCRIPTS = ROOT / "plaud" / "audio", ROOT / "plaud" / "transcripts"
STATE = HOME / ".local" / "share" / "earshot" / "state.json"
TOKENS = HOME / ".plaud" / "tokens.json"
BASE = "https://platform.plaud.ai/developer/api/open/third-party"
PAGE_SIZE = 50
MAX_DRIFT_OK, MAX_DRIFT = 3.0, 30.0
REFRESH_WARN_H = 48
REVIEW_URL = "http://localhost:8790/"
BACKUP_MOUNT = os.environ.get("EARSHOT_BACKUP_MOUNT", "/mnt/s")
NAS = Path(os.environ.get("EARSHOT_BACKUP_DIR", "/mnt/s/backup/recordings")) / "plaud"  # written by backup.sh
DELETE_REMINDER_DAYS = 7


def log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def slack(text: str, dry_run: bool) -> None:
    if dry_run:
        log(f"(dry run) would post to Slack: {text}")
        return
    url = os.environ.get("SLACK_WEBHOOK_URL")
    if not url:
        log("SLACK_WEBHOOK_URL is not set; alert not sent")
        return
    try:
        urllib.request.urlopen(urllib.request.Request(
            url, json.dumps({"text": text}).encode(), {"Content-Type": "application/json"}), timeout=15)
    except OSError as e:
        log(f"Slack post failed: {type(e).__name__}")


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {"failed": {}}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE)


def auth(dry_run: bool) -> str | None:
    """Refresh through the CLI's own path; return the access token, or None after alerting."""
    # `plaud me` prints the account email, which must not be kept, so its output is discarded.
    r = subprocess.run([str(HOME / ".local" / "bin" / "plaud"), "me"], capture_output=True, timeout=60)
    if r.returncode or not TOKENS.exists():
        log(f"plaud me failed (exit {r.returncode}, tokens.json {'present' if TOKENS.exists() else 'missing'})")
        slack(":warning: Plaud sync: the Plaud login has expired or was rejected. "
              "Run `plaud login` in a WSL terminal; recordings stay in the cloud until then.", dry_run)
        return None
    tokens = json.loads(TOKENS.read_text())
    payload = tokens["refresh_token"].split(".")[1]
    exp = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["exp"]
    hours = (exp - time.time()) / 3600
    log(f"logged in; refresh token valid for {hours:.0f} h")
    if hours < REFRESH_WARN_H:
        slack(f":hourglass: Plaud sync: the Plaud login expires in {hours:.0f} h and isn't refreshing. "
              "Run `plaud login` in a WSL terminal to renew it.", dry_run)
    return tokens["access_token"]


def list_cloud(token: str) -> list[dict]:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "earshot/0.1"}
    items, page = [], 1
    while True:
        req = urllib.request.Request(f"{BASE}/files/?page={page}&page_size={PAGE_SIZE}", headers=headers)
        batch = json.load(urllib.request.urlopen(req, timeout=30))["data"]
        items += batch
        if len(batch) < PAGE_SIZE:
            return items
        page += 1
        time.sleep(2)  # detail and list calls hit 429 when spaced under ~1 s


def describe(item: dict) -> str:
    start = datetime.fromisoformat(item["start_at"]).replace(tzinfo=timezone.utc).astimezone()
    return f"{start:%a %d %b %H:%M} ({item['duration'] / 60000:.0f} min)"


def run(cmd: list[str], timeout: int, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)


def ingest(item: dict) -> tuple[Path | None, float | None, str]:
    """Download and verify one recording. Returns (audio path, drift, error)."""
    r = run(["python3", "-I", "scripts/plaud_ingest.py", item["id"], "unfiled", str(MAX_DRIFT)], 900, cwd=PYARIZE)
    out = (r.stdout + r.stderr).strip()
    m = re.search(r"^ok (\S+)\s.*?drift ([+-]?[\d.]+)s", out, re.M)
    if r.returncode or not m:
        return None, None, out.splitlines()[-1][:200] if out else f"exit {r.returncode}"
    path, drift = AUDIO / m.group(1), float(m.group(2))
    errors = run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"], 600).stderr.strip()
    if errors:
        return path, drift, f"decode errors: {len(errors.splitlines())} lines"
    return path, drift, ""


def transcribe(audio: Path) -> str:
    """Transcribe and diarize with no network access. Returns an error, or "" on success."""
    transcript = TRANSCRIPTS / f"{audio.stem}.json"
    if not transcript.exists():
        r = run(["unshare", "-Urn", "env", "HF_HUB_OFFLINE=1", "HF_HUB_DISABLE_TELEMETRY=1",
                 str(WHISPER_PY), "-I", "scripts/plaud_transcribe.py", str(audio)], 7200, cwd=PYARIZE)
        if r.returncode:
            return f"transcription failed (exit {r.returncode})"
        log("  " + " | ".join(l for l in r.stdout.splitlines() if l.startswith(("model load", "language"))))
    r = run(["unshare", "-Urn", ".venv/bin/python", "-I", "scripts/plaud_diarize.py", str(audio), str(transcript)],
            3600, cwd=PYARIZE)
    if r.returncode:
        return f"diarization failed (exit {r.returncode})"
    log("  " + " | ".join(l for l in r.stdout.splitlines() if re.match(r"\d+ speakers", l)))
    return ""


def make_brief(audio: Path) -> str:
    """Triage brief on llm-server (brief.py). Returns an error, or "" on success; never fatal."""
    r = run(["python3", "-I", str(Path(__file__).with_name("brief.py")), audio.stem], 900)
    out = (r.stdout + r.stderr).strip().splitlines()
    return "" if r.returncode == 0 else (out[-1][:160] if out else f"exit {r.returncode}")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_to_delete(cloud: list[dict]) -> list[dict]:
    """Cloud recordings that are archived here and have a checksum-verified copy on the NAS."""
    if not os.path.ismount(BACKUP_MOUNT):
        return []
    out = []
    for item in cloud:
        local = [p for p in AUDIO.glob(f"*_{item['id']}.*") if p.suffix in (".ogg", ".mp3")]
        local += list((AUDIO.parent / "raw").glob(f"*_{item['id']}.opus"))  # raw-Opus originals
        if not local:
            continue
        copies = [NAS / p.parent.name / p.name for p in local]
        if all(c.exists() and c.stat().st_size == p.stat().st_size and sha256(c) == sha256(p)
               for p, c in zip(local, copies)):
            out.append(item)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="check login and list new recordings only")
    ap.add_argument("--safe-to-delete", action="store_true",
                    help="list cloud recordings that are archived and backed up, then exit")
    args = ap.parse_args()
    log(f"sync start{' (dry run)' if args.dry_run else ''}; Slack webhook "
        f"{'configured' if os.environ.get('SLACK_WEBHOOK_URL') else 'MISSING'}")

    token = auth(args.dry_run)
    if not token:
        return
    known = {r["id"] for r in csv.DictReader(open(CSV_PATH))}
    cloud = list_cloud(token)
    new = sorted((i for i in cloud if i["id"] not in known), key=lambda i: i["start_at"])
    log(f"{len(cloud)} recording(s) in the cloud, {len(new)} new")
    if args.dry_run or args.safe_to_delete:
        for item in new:
            log(f"  would ingest {item['id']}  {describe(item)}")
        for item in safe_to_delete(cloud):
            log(f"  safe to delete in the Plaud app: {item['id']}  {describe(item)}")
        return

    state = load_state()
    done, problems = [], []
    for item in new:
        log(f"ingest {item['id']}  {describe(item)}")
        audio, drift, err = ingest(item)
        if not err:
            err = transcribe(audio)
        if err:
            log(f"  FAILED: {err}")
            if item["id"] not in state["failed"]:
                problems.append(f"{describe(item)}: {err}")
            state["failed"][item["id"]] = {"first": state["failed"].get(item["id"], {}).get("first", datetime.now().isoformat()),
                                           "error": err}
            continue
        state["failed"].pop(item["id"], None)
        note = f", Plaud's copy {abs(drift):.0f} s short" if drift is not None and abs(drift) > MAX_DRIFT_OK else ""
        if berr := make_brief(audio):
            log(f"  brief failed: {berr}")
            note += ", no brief yet (llm-server?)"
        done.append(describe(item) + note)
        log(f"  done{note}")
        time.sleep(2)
    # Weekly nudge to clear the Plaud cloud of recordings that are safely archived and backed up.
    deletable, remind = [], False
    last = state.get("last_delete_reminder")
    if not last or datetime.now() - datetime.fromisoformat(last) >= timedelta(days=DELETE_REMINDER_DAYS):
        deletable = safe_to_delete(cloud)
        if deletable:
            remind = True
            state["last_delete_reminder"] = datetime.now().isoformat(timespec="seconds")
    state["last_run"] = datetime.now().isoformat(timespec="seconds")
    save_state(state)

    lines = []
    if done:
        lines.append(f":inbox_tray: Plaud sync: {len(done)} new recording(s) transcribed, diarized and briefed, "
                     f"each filed under its suggested category. Triage them at {REVIEW_URL}")
        lines += [f"• {d}" for d in done]
    if problems:
        lines.append(":warning: Plaud sync: these need attention (see ~/.local/share/earshot/sync.log):")
        lines += [f"• {p}" for p in problems]
    if remind:
        lines.append(f":wastebasket: {len(deletable)} recording(s) in the Plaud cloud are archived here and "
                     "backed up to the NAS, so they can be deleted in the Plaud app:")
        lines += [f"• {describe(i)}" for i in deletable]
    if lines:
        slack("\n".join(lines), False)
    log(f"sync end: {len(done)} done, {len(new) - len(done)} failed")


if __name__ == "__main__":
    main()
