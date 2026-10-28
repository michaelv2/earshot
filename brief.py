"""Triage brief for a recording: category, topic, suggested title, and a very short summary.

Runs a local model on llm-server (gemma4:31b via Ollama; cloud models are refused) over the diarized
transcript and writes <stem>.brief.json next to it (0600). It's for triage: choosing a full synthesis
template, or deciding a recording isn't worth one, without spending tokens on a full summary: a short
plain-English summary plus a few one-line takeaways.

A recording that's still `unfiled` is filed under the suggested category; change it on the review
page if it's wrong. Speaker names from the review page are used when set; at ingest there are none yet, so the model
works from content alone (--no-names reproduces that for testing).

usage: python3 -I brief.py <id-or-stem>... [--force] [--no-names]
       python3 -I brief.py --missing             # every transcribed recording without a brief
"""

import argparse
import csv
import fcntl
import io
import json
import os
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

_host = os.environ.get("EARSHOT_OLLAMA_URL") or os.environ.get("OLLAMA_HOST") or "localhost:11434"
OLLAMA = _host if "://" in _host else f"http://{_host}"
MODEL = os.environ.get("EARSHOT_BRIEF_MODEL", "gemma4:31b")
ROOT = Path.home() / "recordings"
CSV_PATH = ROOT / "folders.csv"
TRANSCRIPTS = ROOT / "plaud" / "transcripts"
CATEGORIES = ["family", "interview", "1:1 meeting", "group meeting"]
SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": CATEGORIES},
        "topic": {"type": "string"},
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "takeaways": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["category", "topic", "title", "summary", "takeaways"],
}
SYSTEM = ("You triage recordings made by Michael: his work meetings, the candidate interviews he runs, and family "
          "conversations. Be accurate and never invent names, numbers or facts. Output only the requested JSON.")
INSTRUCTIONS = """Classify and briefly summarize this recording. Return a JSON object with:
- "category": one of
    "interview" (a job interview with a candidate),
    "1:1 meeting" (a work conversation between two people),
    "group meeting" (a work meeting with three or more people),
    "family" (a personal or family conversation, not work).
- "topic": the main topic, at most 6 words.
- "title": a short title, at most 6 words, like "<Name> interview" or "<Name> sync" for interviews and
  1:1s, or "<subject> review" for group meetings. <Name> is the other person (the candidate, or the
  colleague Michael is meeting). Use a name when someone is addressed by it or introduces themselves
  ("Hi Dana", "I'm Sam", "thanks, Alex"); otherwise describe the subject.
- "summary": at most 60 words of plain English covering the main topic, key points and tone.
- "takeaways": 2 to 4 single-sentence key points, decisions or action items.

Michael is usually one of the speakers, but the speaker labels don't say which one. Don't assume the
speaker who talks most is Michael: in interviews the candidate usually talks most. When it's unclear
who said something, describe people by role ("the interviewer", "the candidate", "a colleague").
"""


def log(msg: str) -> None:
    print(msg, flush=True)


def check_model() -> None:
    """Refuse anything that isn't a local model: '-cloud' tags have no local weights and send data off-site."""
    tags = json.load(urllib.request.urlopen(f"{OLLAMA}/api/tags", timeout=10))["models"]
    m = next((t for t in tags if t["name"] == MODEL), None)
    if m is None:
        sys.exit(f"{MODEL} is not installed on {OLLAMA}")
    if "cloud" in MODEL or not m.get("size"):
        sys.exit(f"{MODEL} looks like a cloud model (no local weights); refusing to send transcripts to it")


def find_stems(keys: list[str]) -> list[str]:
    out = []
    for k in keys:
        hits = sorted(p.name[: -len(".diarized.json")] for p in TRANSCRIPTS.glob(f"*{k}*.diarized.json")
                      if ".unsmoothed." not in p.name)
        if len(hits) != 1:
            sys.exit(f"{k!r} matches {len(hits)} transcripts")
        out.append(hits[0])
    return out


def make_brief(stem: str, use_names: bool) -> dict:
    data = json.loads((TRANSCRIPTS / f"{stem}.diarized.json").read_text())
    utts = data["utterances"]
    started = datetime.strptime(stem[:15], "%Y-%m-%d_%H%M")
    base = {"model": MODEL, "created": datetime.now().isoformat(timespec="seconds"), "names_used": False}
    if not utts:
        return {**base, "category": None, "topic": "", "title": "", "takeaways": [],
                "summary": "No speech detected."}

    names = {}
    if use_names:
        try:
            names = json.loads((TRANSCRIPTS / f"{stem}.speakers.json").read_text())
        except (OSError, ValueError):
            pass
    talk: dict[str, float] = {}
    for u in utts:
        talk[u["speaker"]] = talk.get(u["speaker"], 0) + u["end"] - u["start"]
    order = sorted(talk, key=lambda s: -talk[s])
    label = {s: names.get(s) or f"Speaker {chr(65 + i)}" for i, s in enumerate(order)}
    speakers = ", ".join(f"{label[s]} {talk[s] / 60:.0f} min" for s in order)
    transcript = "\n".join(f"{label[u['speaker']]}: {u['text']}" for u in utts)

    prompt = (INSTRUCTIONS + f"\nRecorded {started:%A %d %B %Y, %H:%M}, {data.get('duration', 0) / 60:.0f} minutes. "
              f"Automatic speaker detection found {len(order)} speaker(s): {speakers}. It sometimes splits one person "
              "into two. The transcript is automatic and may contain mis-transcribed names.\n\nTranscript:\n" + transcript)
    num_ctx = min(32768, ((len(prompt) // 3 + 2048) // 4096 + 1) * 4096)
    body = {"model": MODEL, "stream": False, "think": False, "format": SCHEMA,
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
            "options": {"num_ctx": num_ctx, "temperature": 0.2, "num_predict": 1500}}
    t0 = time.time()
    r = json.load(urllib.request.urlopen(urllib.request.Request(
        f"{OLLAMA}/api/chat", json.dumps(body).encode(), {"Content-Type": "application/json"}), timeout=600))
    brief = json.loads(r["message"]["content"])
    if brief.get("category") not in CATEGORIES:
        raise ValueError(f"unexpected category {brief.get('category')!r}")
    if r.get("prompt_eval_count", 0) >= num_ctx:
        raise ValueError(f"prompt filled the {num_ctx}-token context; transcript may be truncated")
    return {**base, "names_used": bool(names), **{k: brief[k] for k in SCHEMA["properties"]},
            "seconds": round(time.time() - t0), "prompt_tokens": r.get("prompt_eval_count")}


def write_brief(stem: str, brief: dict) -> None:
    path = TRANSCRIPTS / f"{stem}.brief.json"
    tmp = path.with_name(f".{path.name}.tmp")
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:
        json.dump(brief, fh, indent=1)
    os.replace(tmp, path)


def file_if_unfiled(stem: str, category: str | None) -> bool:
    """Apply the suggested category to a recording that's still unfiled. Uses the folders.csv lock
    shared with the review page and plaud_ingest.py."""
    if not category:
        return False
    rid = stem[16:]  # stems are <YYYY-MM-DD_HHMM>_<id>
    with open(ROOT / ".folders.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with open(CSV_PATH, newline="") as fh:
            reader = csv.DictReader(fh)
            fields = list(reader.fieldnames or [])
            rows = [{k: v for k, v in r.items() if k is not None} for r in reader]
        row = next((r for r in rows if r["id"] == rid and r["folder"] == "unfiled"), None)
        if not row:
            return False
        row["folder"] = category
        out = io.StringIO()
        w = csv.DictWriter(out, fieldnames=fields, restval="")
        w.writeheader()
        w.writerows(rows)
        tmp = ROOT / ".folders.csv.brief.tmp"
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", newline="") as fh:
            fh.write(out.getvalue())
        os.replace(tmp, CSV_PATH)
    return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("keys", nargs="*", help="recording ids or stems")
    ap.add_argument("--missing", action="store_true", help="brief every transcribed recording that has none")
    ap.add_argument("--force", action="store_true", help="replace existing briefs")
    ap.add_argument("--no-names", action="store_true", help="ignore speaker names, as at ingest")
    args = ap.parse_args()

    if args.missing:
        stems = sorted((p.name[: -len(".diarized.json")] for p in TRANSCRIPTS.glob("*.diarized.json")
                        if ".unsmoothed." not in p.name), reverse=True)
    else:
        stems = find_stems(args.keys)
    if not args.force:
        stems = [s for s in stems if not (TRANSCRIPTS / f"{s}.brief.json").exists()]
    check_model()
    failed = 0
    for stem in stems:
        try:
            brief = make_brief(stem, use_names=not args.no_names)
        except (OSError, ValueError, KeyError) as e:
            failed += 1
            log(f"{stem}: FAILED {type(e).__name__}: {str(e)[:160]}")
            continue
        write_brief(stem, brief)
        filed = file_if_unfiled(stem, brief["category"])
        log(f"{stem}: {brief['category']}{', filed' if filed else ''} ({brief.get('seconds', 0)}s)")
    log(f"briefs: {len(stems) - failed} written, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
