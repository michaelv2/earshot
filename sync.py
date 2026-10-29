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
- A preflight check runs first and reports setup problems in plain words: missing pyarize
  scripts or environments, the pyannote model not downloaded, or faster-whisper not importable.
  Download problems stop downloads; transcription problems only stop transcription.
- Each run transcribes every archived recording that has no transcript yet (except discarded
  ones), so a recording whose transcription failed is retried the next day.
- A failure is alerted once, then remembered in the state file and retried quietly.

usage: python3 -I sync.py [--dry-run | --safe-to-delete]
"""

import argparse
import base64
import csv
import hashlib
import json
import os
import re
import shutil
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


def has_module(python: str | Path, module: str) -> bool:
    try:
        r = run([str(python), "-I", "-c", f"import importlib.util, sys; sys.exit(importlib.util.find_spec({module!r}) is None)"], 60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def preflight() -> tuple[list[str], list[str]]:
    """Check the setup before touching anything. Returns (download problems, transcription problems)."""
    dl, tx = [], []
    if not (HOME / ".local" / "bin" / "plaud").exists():
        dl.append("the Plaud CLI wrapper ~/.local/bin/plaud is missing (README, Setup step 2)")
    if not shutil.which("ffmpeg"):
        dl.append("ffmpeg isn't on PATH")
    if not (PYARIZE / "scripts" / "plaud_ingest.py").exists():
        dl.append(f"pyarize isn't at {PYARIZE}; set EARSHOT_PYARIZE_DIR in ~/.config/earshot/env")
        return dl, ["pyarize is missing"]
    for script in ("plaud_transcribe.py", "plaud_diarize.py"):
        if not (PYARIZE / "scripts" / script).exists():
            tx.append(f"pyarize has no scripts/{script}; check the pyarize version in the README")
    if not shutil.which("unshare"):
        tx.append("unshare isn't on PATH (util-linux)")
    diarize_py = PYARIZE / ".venv" / "bin" / "python"
    if not diarize_py.exists():
        tx.append(f"pyarize's diarization environment is missing ({diarize_py}); see pyarize's README")
    elif not has_module(diarize_py, "pyannote.audio"):
        tx.append("pyarize's environment has no pyannote.audio; run `uv pip sync --no-build requirements-diarize.txt` there")
    if not (HOME / ".pyarize" / "community-1.manifest.json").exists():
        tx.append("the pyannote model isn't downloaded; run pyarize's scripts/fetch_community1.py once")
    if not has_module(WHISPER_PY, "faster_whisper"):
        tx.append(f"{WHISPER_PY} can't import faster_whisper; set EARSHOT_WHISPER_PYTHON in ~/.config/earshot/env")
    return dl, tx


def pending_audio(folders: dict[str, str]) -> list[Path]:
    """Archived recordings with no diarized transcript yet: new downloads and earlier failures."""
    out = []
    for p in sorted(AUDIO.iterdir()):
        if p.suffix not in (".ogg", ".mp3") or p.name.startswith("_") or len(p.stem) <= 16:
            continue
        if folders.get(p.stem[16:]) != "discard" and not (TRANSCRIPTS / f"{p.stem}.diarized.json").exists():
            out.append(p)
    return out


def describe_stem(stem: str, minutes: str | None) -> str:
    start = datetime.strptime(stem[:15], "%Y-%m-%d_%H%M")
    return f"{start:%a %d %b %H:%M}" + (f" ({float(minutes):.0f} min)" if minutes else "")


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
    dl_problems, tx_problems = preflight()
    for msg in dl_problems + tx_problems:
        log(f"preflight: {msg}")
    if not dl_problems and not tx_problems:
        log("preflight ok")

    token = auth(args.dry_run)
    if not token:
        return
    rows = {r["id"]: r for r in csv.DictReader(open(CSV_PATH))}
    cloud = list_cloud(token)
    new = sorted((i for i in cloud if i["id"] not in rows), key=lambda i: i["start_at"])
    log(f"{len(cloud)} recording(s) in the cloud, {len(new)} new")
    if args.dry_run or args.safe_to_delete:
        for item in new:
            log(f"  would ingest {item['id']}  {describe(item)}")
        for audio in pending_audio({k: r["folder"] for k, r in rows.items()}):
            log(f"  would transcribe {audio.name} (archived, no transcript yet)")
        for item in safe_to_delete(cloud):
            log(f"  safe to delete in the Plaud app: {item['id']}  {describe(item)}")
        return

    state = load_state()
    done, problems, failed = [], [], 0

    def fail(rid: str, label: str, err: str) -> None:
        nonlocal failed
        failed += 1
        log(f"  FAILED: {err}")
        if rid not in state["failed"]:
            problems.append(f"{label}: {err}")
        state["failed"][rid] = {"first": state["failed"].get(rid, {}).get("first", datetime.now().isoformat()),
                                "error": err}

    # Setup problems are alerted once per distinct set, then quietly until they change or clear.
    setup = dl_problems + tx_problems
    if setup and state.get("preflight") != setup:
        problems.append("setup: " + "; ".join(setup))
    state["preflight"] = setup

    drifts = {}
    if not dl_problems:
        for item in new:
            log(f"ingest {item['id']}  {describe(item)}")
            audio, drift, err = ingest(item)
            if err:
                fail(item["id"], describe(item), err)
                continue
            drifts[audio.stem] = drift
            time.sleep(2)

    if not tx_problems:
        rows = {r["id"]: r for r in csv.DictReader(open(CSV_PATH))}  # now including today's downloads
        for audio in pending_audio({k: r["folder"] for k, r in rows.items()}):
            rid = audio.stem[16:]
            label = describe_stem(audio.stem, rows.get(rid, {}).get("duration_min"))
            log(f"transcribe {audio.name}")
            if err := transcribe(audio):
                fail(rid, label, err)
                continue
            state["failed"].pop(rid, None)
            drift = drifts.get(audio.stem)
            note = f", Plaud's copy {abs(drift):.0f} s short" if drift is not None and abs(drift) > MAX_DRIFT_OK else ""
            if berr := make_brief(audio):
                log(f"  brief failed: {berr}")
                note += ", no brief yet (llm-server?)"
            done.append(label + note)
            log(f"  done{note}")

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
        lines.append(f":inbox_tray: Plaud sync: {len(done)} recording(s) transcribed, diarized and briefed, "
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
    log(f"sync end: {len(done)} done, {failed} failed")


if __name__ == "__main__":
    main()
