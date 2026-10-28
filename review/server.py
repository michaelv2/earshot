"""Local review page for Plaud recordings: listen, give them friendly titles, file them, name speakers.

Serves on 127.0.0.1 only, from the files under ~/recordings. Standard library only (SPEC G5).
  folders.csv          the filing source of truth; this page edits its `folder` and `title` columns
  plaud/audio/         originals, never modified. Ogg files are played through an audio-only stream
                       copy in plaud/cache/, because Plaud adds a metadata stream browsers may reject
  plaud/transcripts/   reads <stem>.diarized.json, or <stem>.json before diarization, plus
                       <stem>.brief.json (brief.py) and <stem>.summary.md; speaker names are saved
                       to <stem>.speakers.json

Requests must carry a localhost Host header (blocks DNS rebinding), and writes need the X-Earshot
header, which a cross-site form or fetch can't send without a CORS preflight this server never grants.

usage: python3 -I review/server.py [--port 8790]
"""

import argparse
import csv
import fcntl
import io
import json
import os
import re
import subprocess
import tempfile
import threading
from collections import defaultdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlsplit

ROOT = Path.home() / "recordings"
CSV_PATH = ROOT / "folders.csv"
AUDIO, TRANSCRIPTS, CACHE = (ROOT / "plaud" / d for d in ("audio", "transcripts", "cache"))
# Categories match brief.py's; "unfiled" means no brief yet, "discard" hides a recording.
FOLDERS = ["interview", "1:1 meeting", "group meeting", "family", "unfiled", "discard"]
PAGE = Path(__file__).with_name("index.html")
ID_RE = re.compile(r"[A-Za-z0-9_]{1,64}")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
csv_lock, cache_lock = threading.Lock(), threading.Lock()


# --- Storage ---------------------------------------------------------------------------------------

def read_csv() -> tuple[list[str], list[dict]]:
    with open(CSV_PATH, newline="") as fh:
        reader = csv.DictReader(fh)
        return list(reader.fieldnames or []), [{k: v for k, v in r.items() if k is not None} for r in reader]


def write_private_atomic(path: Path, text: str, mode: int = 0o600) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", newline="") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def update_csv_row(rid: str, changes: dict) -> None:
    # The file lock is shared with pyarize's plaud_ingest.py, which appends rows (e.g. the daily sync).
    with csv_lock, open(ROOT / ".folders.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        fields, rows = read_csv()
        for k in changes:
            if k not in fields:
                fields.append(k)
        for r in rows:
            if r["id"] == rid:
                r.update(changes)
        out = io.StringIO()
        w = csv.DictWriter(out, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows({k: (r.get(k) or "") for k in fields} for r in rows)
        write_private_atomic(CSV_PATH, out.getvalue(), CSV_PATH.stat().st_mode & 0o777)


def stems() -> dict[str, tuple[str, Path]]:
    """Map recording id -> (stem, audio path). Stems are <YYYY-MM-DD_HHMM>_<id>."""
    out = {}
    for p in AUDIO.iterdir():
        if p.suffix in (".ogg", ".mp3") and not p.name.startswith("_") and len(p.stem) > 16:
            out[p.stem[16:]] = (p.stem, p)
    return out


def load_transcript(stem: str) -> tuple[str, list[dict]]:
    """Return (status, utterances). Partly written files (diarization in progress) count as absent."""
    try:
        data = json.loads((TRANSCRIPTS / f"{stem}.diarized.json").read_text())
        return "diarized", [{k: u[k] for k in ("start", "end", "speaker", "text")} for u in data["utterances"]]
    except (OSError, ValueError, KeyError):
        pass
    try:
        segs = json.loads((TRANSCRIPTS / f"{stem}.json").read_text())["segments"]
        return "transcribed", [{"start": s["start"], "end": s["end"], "speaker": None, "text": s["text"]} for s in segs]
    except (OSError, ValueError, KeyError):
        return "none", []


def load_names(stem: str) -> dict[str, str]:
    try:
        return json.loads((TRANSCRIPTS / f"{stem}.speakers.json").read_text())
    except (OSError, ValueError):
        return {}


def load_brief(stem: str) -> dict | None:
    try:
        return json.loads((TRANSCRIPTS / f"{stem}.brief.json").read_text())
    except (OSError, ValueError):
        return None


def load_summary(stem: str) -> str | None:
    try:
        with open(TRANSCRIPTS / f"{stem}.summary.md", encoding="utf-8") as fh:
            return fh.read(1_000_000)
    except OSError:
        return None


def speakers(utts: list[dict], names: dict[str, str]) -> list[dict]:
    talk, longest = defaultdict(float), {}
    for u in utts:
        if spk := u["speaker"]:
            d = u["end"] - u["start"]
            talk[spk] += d
            if d > longest.get(spk, (0,))[0]:
                longest[spk] = (d, u["start"], u["end"])
    return [{"label": s, "name": names.get(s, ""), "seconds": round(talk[s]), "sample": list(longest[s][1:])}
            for s in sorted(talk, key=lambda s: -talk[s])]


def summary(row: dict, index: dict) -> dict:
    stem = index.get(row["id"], (None,))[0]
    status, utts = load_transcript(stem) if stem else ("no audio", [])
    names = load_names(stem) if stem else {}
    labels = {u["speaker"] for u in utts if u["speaker"]}
    return {
        "id": row["id"], "stem": stem, "start": row.get("start_local", ""),
        "duration_min": float(row.get("duration_min") or 0), "folder": row.get("folder", ""),
        "title": row.get("title") or "", "plaud_name": row.get("name") or "", "status": status,
        "speakers": len(labels), "speakers_named": sum(1 for s in labels if names.get(s)),
        "has_summary": bool(stem) and (TRANSCRIPTS / f"{stem}.summary.md").exists(),
        "brief": {k: b.get(k) for k in ("category", "topic", "title")} if stem and (b := load_brief(stem)) else None,
    }


def playable(stem: str, src: Path) -> tuple[Path, str]:
    if src.suffix == ".mp3":
        return src, "audio/mpeg"
    out = CACHE / f"{stem}.ogg"
    with cache_lock:
        if not out.exists() or out.stat().st_mtime < src.stat().st_mtime:
            CACHE.mkdir(mode=0o700, exist_ok=True)
            tmp = CACHE / f"_{stem}.ogg"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-map", "0:a:0", "-c", "copy", str(tmp)],
                           check=True, capture_output=True)
            os.chmod(tmp, 0o600)
            os.replace(tmp, out)
    return out, "audio/ogg"


def hms(t: float) -> str:
    return f"{int(t // 3600):02d}:{int(t % 3600 // 60):02d}:{int(t % 60):02d}"


# --- HTTP ------------------------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "earshot-review"

    def log_message(self, fmt, *args):  # paths only, never bodies
        pass

    def _allowed(self) -> bool:
        if self.headers.get("Host", "") not in self.server.allowed_hosts:
            self.send_error(HTTPStatus.FORBIDDEN, "bad Host")
            return False
        return True

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, status: int = 200) -> None:
        self._send(status, json.dumps(obj).encode(), "application/json")

    def _recording(self, rid: str):
        if not ID_RE.fullmatch(rid):
            return None, None
        rows = {r["id"]: r for r in read_csv()[1]}
        return rows.get(rid), stems().get(rid)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._allowed():
            return
        parts = urlsplit(self.path).path.strip("/").split("/")
        if parts == [""]:
            csp = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                   "connect-src 'self'; media-src 'self'; img-src 'self' data:; form-action 'none'; "
                   "frame-ancestors 'none'; base-uri 'none'")
            return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8", {"Content-Security-Policy": csp})
        if parts == ["api", "recordings"]:
            index = stems()
            rows = read_csv()[1]
            return self._json({"folders": FOLDERS, "recordings": sorted(
                (summary(r, index) for r in rows), key=lambda s: s["start"], reverse=True)})
        if len(parts) >= 3 and parts[:2] == ["api", "recordings"]:
            row, found = self._recording(parts[2])
            if not row or not found:
                return self.send_error(404)
            stem = found[0]
            status, utts = load_transcript(stem)
            names = load_names(stem)
            if parts[3:] == ["transcript.txt"]:
                label = lambda s: names.get(s) or s or "?"
                text = "".join(f"[{hms(u['start'])}] {label(u['speaker'])}: {u['text']}\n" for u in utts)
                fname = re.sub(r"[^\w .-]+", "", row.get("title") or "")[:80].strip() or stem
                return self._send(200, text.encode(), "text/plain; charset=utf-8", {
                    "Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname + '.txt')}"})
            if parts[3:]:
                return self.send_error(404)
            return self._json({**summary(row, {row["id"]: found}), "speakers": speakers(utts, names),
                               "utterances": utts, "summary": load_summary(stem), "brief_full": load_brief(stem)})
        if len(parts) == 2 and parts[0] == "audio":
            row, found = self._recording(parts[1])
            if not row or not found:
                return self.send_error(404)
            try:
                path, ctype = playable(*found)
            except subprocess.CalledProcessError:
                return self.send_error(500, "could not prepare audio")
            return self._send_file(path, ctype)
        self.send_error(404)

    def _send_file(self, path: Path, ctype: str) -> None:
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        if rng := self.headers.get("Range"):
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip())
            if not m or m.groups() == ("", ""):
                return self._send(416, b"", ctype, {"Content-Range": f"bytes */{size}"})
            a, b = m.groups()
            if a == "":
                start = max(0, size - int(b))
            else:
                start, end = int(a), min(int(b), size - 1) if b else size - 1
            if start > end:
                return self._send(416, b"", ctype, {"Content-Range": f"bytes */{size}"})
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "no-store")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            with open(path, "rb") as fh:
                fh.seek(start)
                left = end - start + 1
                while left > 0 and (chunk := fh.read(min(65536, left))):
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the player seeks by dropping connections

    def do_PATCH(self):
        if not self._allowed():
            return
        origin = self.headers.get("Origin")
        if (self.headers.get("X-Earshot") != "1"
                or not self.headers.get("Content-Type", "").startswith("application/json")
                or (origin and urlsplit(origin).netloc not in self.server.allowed_hosts)):
            return self.send_error(HTTPStatus.FORBIDDEN)
        parts = urlsplit(self.path).path.strip("/").split("/")
        if len(parts) != 3 or parts[:2] != ["api", "recordings"]:
            return self.send_error(404)
        row, found = self._recording(parts[2])
        if not row or not found:
            return self.send_error(404)
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 65536)))
        except ValueError:
            return self.send_error(400, "bad JSON")

        changes = {}
        if "title" in body:
            if not isinstance(body["title"], str):
                return self.send_error(400, "title")
            changes["title"] = CONTROL_RE.sub(" ", body["title"]).strip()[:200]
        if "folder" in body:
            if body["folder"] not in FOLDERS:
                return self.send_error(400, "folder")
            changes["folder"] = body["folder"]
        if changes:
            update_csv_row(row["id"], changes)
        if "speakers" in body:
            stem = found[0]
            labels = {u["speaker"] for u in load_transcript(stem)[1] if u["speaker"]}
            if not isinstance(body["speakers"], dict) or not set(body["speakers"]) <= labels:
                return self.send_error(400, "speakers")
            names = load_names(stem)
            for label, name in body["speakers"].items():
                name = CONTROL_RE.sub(" ", str(name)).strip()[:80]
                if name:
                    names[label] = name
                else:
                    names.pop(label, None)
            write_private_atomic(TRANSCRIPTS / f"{stem}.speakers.json", json.dumps(names, indent=1))
        self._json({"ok": True})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8790)
    args = ap.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.allowed_hosts = {f"127.0.0.1:{args.port}", f"localhost:{args.port}"}
    print(f"Review page: http://localhost:{args.port}/  (Ctrl+C to stop)")
    server.serve_forever()


if __name__ == "__main__":
    main()
