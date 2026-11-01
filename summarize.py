"""Full summary of a recording, drafted by a local model from a per-category template.

On demand only; the daily sync never runs it. brief.py is the triage step (category, title, 60
words). This writes the notes you work from when deciding what to keep, e.g. what to copy into
memoize. Every point cites the transcript time it came from, so you can check it against the audio.

It uses brief.py's local-only model check (cloud models are refused) and the speaker names set on
the review page. Name the speakers first, or action items can't have owners.

Writes <stem>.summary.md (the review page's Summary tab) and <stem>.summary.meta.json (model,
template, timings, citation checks), both 0600. An existing summary is kept as
<stem>.summary.prev.md. --out DIR writes <stem>.<template>.<model>.md and its .meta.json to DIR
instead, to compare models without touching the real summary.

Templates are Markdown files in templates/, where {owner} stands for EARSHOT_OWNER (default
Michael). A file of the same name in ~/.config/earshot/templates/ overrides one, so a private
version (e.g. a role-specific interview rubric) stays out of the repo. The template defaults from
the recording's category.

usage: python3 -I summarize.py <id-or-stem> [--template NAME] [--model TAG] [--no-think] [--out DIR]
       python3 -I summarize.py --templates
"""

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # python3 -I leaves the script's folder off sys.path
from brief import CSV_PATH, OLLAMA, TRANSCRIPTS, check_model, find_stems  # noqa: E402

# Chosen in a 2026-11-01 bake-off on a 20-minute 1:1 (SPEC §4.2): with reasoning on, qwen3.8 covered
# about everything a hand-checked summary did; gemma4 covered about two thirds, without or with it.
MODEL = os.environ.get("EARSHOT_SUMMARY_MODEL", "qwen3.8:27b-128k")
OWNER = os.environ.get("EARSHOT_OWNER", "Michael")
TEMPLATE_DIRS = [Path.home() / ".config" / "earshot" / "templates", HERE / "templates"]  # first wins
DEFAULT_TEMPLATE = {"interview": "interview", "1:1 meeting": "one-on-one", "group meeting": "group-meeting"}
NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")
MAX_CTX = 65536

SYSTEM = (f"You write notes from automatic transcripts of recordings made by {OWNER}: candidate interviews "
          f"{OWNER} runs, and {OWNER}'s 1:1s and group meetings at work. Be accurate: never invent names, "
          "numbers, decisions or commitments, and say so when something is unclear. Output only the notes, "
          "in Markdown.")
RULES = """Rules:
- Use only what is in the transcript. If a section has nothing to report, write "None." under it.
- End every bullet with the transcript time(s) it comes from, copied exactly from the start of the
  transcript line, in square brackets: [12:34], or [12:34, 15:02] for more than one. At most three
  per bullet. The Summary section doesn't need them.
- Keep numbers, amounts and names exactly as said. Mark a word that is probably mis-transcribed
  with (?), including a word whose literal meaning doesn't fit the context, and list those words
  at the end under "## Possible mis-transcriptions" with your best guess, e.g. "Jason file"
  (likely "JSON file"): at most 10, the ones most likely to mislead a reader. Leave that section
  out if there are none.
- A decision is only something explicitly agreed, or stated as what will happen. Proposals,
  preferences and "I'm not against ..." are points, not decisions.
- An action item is only a task someone said they, or someone else, would do. Never turn a topic,
  problem or open question into an action item of your own; those go under follow-ups. Give an
  item an owner only when the transcript makes clear who will do it; otherwise put it under
  **Unassigned**.
- Paraphrase by default. Quote only a phrase whose exact wording matters, at most one per bullet
  and under 20 words.
- Use only this Markdown: "## " and "### " headings, "- " bullets (indent nested bullets by two
  spaces), "- [ ] " for action items, **bold** and *italic*. No tables, links, code blocks,
  horizontal rules, or a "# " title.
- Plain, concise English. Specifics over generalities."""

CITE_RE = re.compile(r"\[(\d[0-9:,;\s–-]*)\]")  # not "[ ]", the task checkbox
TIME_RE = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})")
NONE_RE = re.compile(r"\s*- (?:\[ \] )?\*?None\.?\*?\s*")


def log(msg: str) -> None:
    print(msg, flush=True)


def stamp(t: float) -> str:
    t = int(t)
    h, m, s = t // 3600, t % 3600 // 60, t % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def template_path(name: str) -> Path:
    if not NAME_RE.fullmatch(name):
        sys.exit(f"bad template name {name!r}")
    for d in TEMPLATE_DIRS:
        if (p := d / f"{name}.md").is_file():
            return p
    sys.exit(f"no template {name!r}; --templates lists them")


def list_templates() -> None:
    found = {}
    for d in reversed(TEMPLATE_DIRS):  # the override folder last, so it wins
        for p in sorted(d.glob("*.md")) if d.is_dir() else []:
            found[p.stem] = p
    for name, p in sorted(found.items()):
        cats = ", ".join(c for c, t in DEFAULT_TEMPLATE.items() if t == name)
        log(f"{name:16} {p}{f'   (default for {cats})' if cats else ''}")


def build_prompt(stem: str, title: str, data: dict, names: dict, template: str) -> tuple[str, list[str]]:
    """Return the prompt and the participants, by name or label, in order of talk time."""
    utts = data["utterances"]
    talk: dict = {}
    for u in utts:
        talk[u["speaker"]] = talk.get(u["speaker"], 0) + u["end"] - u["start"]
    order = sorted(talk, key=lambda s: -talk[s])
    label = {s: names.get(s) or f"Speaker {chr(65 + i)}" for i, s in enumerate(order)}
    by_name: dict = {}  # two labels given the same name are one person
    for s in order:
        by_name[label[s]] = by_name.get(label[s], 0) + talk[s]
    started = datetime.strptime(stem[:15], "%Y-%m-%d_%H%M")
    context = [f"Recording{f' titled “{title}”' if title else ''}: {started:%A %d %B %Y, %H:%M}, "
               f"{data.get('duration', 0) / 60:.0f} minutes.",
               "Speakers, with talk time: " + ", ".join(f"{n} ({t / 60:.0f} min)" for n, t in by_name.items()) + "."]
    if not any(n.lower() == OWNER.lower() for n in by_name):
        context.append(f"{OWNER} is usually one of the speakers, but the labels don't say which. Don't assume "
                       f"the speaker who talks most is {OWNER}; in interviews the candidate usually talks most.")
    context.append("Speaker detection is automatic and sometimes splits one person into two. The transcript is "
                   "automatic too, so names and jargon are often mis-transcribed.")
    lines = "\n".join(f"[{stamp(u['start'])}] {label[u['speaker']]}: {u['text'].strip()}" for u in utts)
    prompt = ("\n".join(context) + "\n\nTranscript (each line starts with its time from the start of the "
              "recording):\n" + lines + "\n\n---\n\n" + template.replace("{owner}", OWNER).strip() + "\n\n" + RULES)
    return prompt, list(by_name)


def generate(prompt: str, model: str, think: bool) -> tuple[str, dict]:
    num_predict = 32000 if think else 6000  # reasoning took 8k tokens on a 20-minute 1:1
    num_ctx = min(MAX_CTX, ((len(prompt) // 3 + num_predict) // 4096 + 1) * 4096)
    body = {"model": model, "stream": False, "think": think,
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
            "options": {"num_ctx": num_ctx, "temperature": 0.2, "num_predict": num_predict}}
    t0 = time.time()
    r = json.load(urllib.request.urlopen(urllib.request.Request(
        f"{OLLAMA}/api/chat", json.dumps(body).encode(), {"Content-Type": "application/json"}), timeout=1800))
    used = r.get("prompt_eval_count", 0) + r.get("eval_count", 0)
    if used >= num_ctx:
        raise ValueError(f"filled the {num_ctx}-token context; the transcript may have been cut")
    if r.get("done_reason") == "length":
        raise ValueError(f"hit the {num_predict}-token output limit; the summary is cut off")
    return r["message"]["content"], {
        "seconds": round(time.time() - t0), "num_ctx": num_ctx, "prompt_tokens": r.get("prompt_eval_count"),
        "output_tokens": r.get("eval_count"), "thinking_chars": len(r["message"].get("thinking") or "")}


def clean(md: str) -> str:
    md = md.strip()
    if m := re.fullmatch(r"```(?:markdown|md)?\s*\n(.*?)\n```", md, re.S):
        md = m.group(1).strip()
    lines = md.splitlines()
    while lines and (lines[0].startswith("# ") or not lines[0].strip()):  # the title is written here, not by the model
        lines.pop(0)
    section = ""
    for i, line in enumerate(lines):  # models sometimes drop the checkboxes the review page renders
        if line.startswith("## "):
            section = line[3:].strip().lower()
        elif section == "action items" and (m := re.match(r"(\s*)- (?!\[[ xX]\] )", line)) and not NONE_RE.fullmatch(line):
            lines[i] = f"{m.group(1)}- [ ] {line[m.end():]}"
    return "\n".join(lines).strip() + "\n"


def check(md: str, utts: list[dict], duration: float, template: str) -> dict:
    """Citations that match a transcript line, citations past the end, uncited bullets, missing sections."""
    starts = {stamp(u["start"]) for u in utts}
    total = matched = past_end = 0
    for group in CITE_RE.findall(md):
        for m in TIME_RE.finditer(group):
            h, mi, s = m.groups()
            t = int(h or 0) * 3600 + int(mi) * 60 + int(s)
            total += 1
            past_end += t > duration + 1
            matched += stamp(t) in starts
    uncited_sections = {"summary", "attendees", "possible mis-transcriptions"}
    section, bullets, cited, current = "", 0, 0, None

    def finish():
        nonlocal bullets, cited
        if current is not None and section not in uncited_sections and not NONE_RE.fullmatch(current):
            bullets += 1
            cited += bool(CITE_RE.search(current))

    for line in md.splitlines() + [""]:
        if re.match(r"\s*- ", line) or line.startswith("#") or not line.strip():
            finish()
            current = line if re.match(r"\s*- ", line) else None
            if line.startswith("## "):
                section = line[3:].strip().lower()
        elif current is not None:
            current += " " + line.strip()  # a bullet wrapped onto the next line
    expected = [line[3:].strip() for line in template.splitlines() if line.startswith("## ")]
    have = {line[3:].strip().lower() for line in md.splitlines() if line.startswith("## ")}
    return {"citations": total, "citations_matching_a_line": matched, "citations_past_end": past_end,
            "bullets": bullets, "bullets_cited": cited,
            "missing_sections": [h for h in expected if h.lower() not in have]}


def write_private(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:
        fh.write(text)
    os.replace(tmp, path)


def main() -> None:
    ap = argparse.ArgumentParser(description="Draft a full summary of one recording with a local model.")
    ap.add_argument("key", nargs="?", help="recording id or stem")
    ap.add_argument("--template", help="template name (default: from the recording's category)")
    ap.add_argument("--model", default=MODEL, help=f"local Ollama model (default {MODEL})")
    ap.add_argument("--think", action=argparse.BooleanOptionalAction, default=True,
                    help="let the model reason before writing (default on; about 2.5x slower, far more complete)")
    ap.add_argument("--out", type=Path, help="write to this folder instead of next to the transcript")
    ap.add_argument("--templates", action="store_true", help="list templates and exit")
    args = ap.parse_args()
    if args.templates:
        return list_templates()
    if not args.key:
        ap.error("give a recording id or stem")

    stem = find_stems([args.key])[0]
    with open(CSV_PATH, newline="") as fh:
        row = next((r for r in csv.DictReader(fh) if r["id"] == stem[16:]), {})
    name = args.template or DEFAULT_TEMPLATE.get(row.get("folder", ""))
    if not name:
        sys.exit(f"no default template for category {row.get('folder')!r}; pass --template")
    tpath = template_path(name)
    template = tpath.read_text()
    data = json.loads((TRANSCRIPTS / f"{stem}.diarized.json").read_text())
    utts = data["utterances"]
    if not utts:
        sys.exit("no speech in this recording")
    try:
        names = json.loads((TRANSCRIPTS / f"{stem}.speakers.json").read_text())
    except (OSError, ValueError):
        names = {}
    if not any(names.values()):
        log("note: no speakers are named, so action items can't have owners. Name them on the review page first.")
    title = row.get("title") or ""
    if not title:
        try:
            title = json.loads((TRANSCRIPTS / f"{stem}.brief.json").read_text()).get("title") or ""
        except (OSError, ValueError):
            pass

    check_model(args.model)
    prompt, people = build_prompt(stem, title, data, names, template)
    try:
        raw, stats = generate(prompt, args.model, args.think)
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as e:
        sys.exit(f"{stem}: FAILED {type(e).__name__}: {str(e)[:200]}")
    body = clean(raw)
    started = datetime.strptime(stem[:15], "%Y-%m-%d_%H%M")
    head = (f"# {title or f'Recording {started:%d %b %Y, %H:%M}'}\n\n"
            f"{started:%a %d %b %Y, %H:%M} · {data.get('duration', 0) / 60:.0f} min · {', '.join(people)}\n\n"
            f"*Drafted by {args.model} from the {name} template on {datetime.now():%d %b %Y}. "
            f"Check anything you keep against its [time] in the transcript.*\n\n")
    meta = {"model": args.model, "think": args.think, "template": name, "template_file": str(tpath),
            "template_sha256": hashlib.sha256(template.encode()).hexdigest()[:16],
            "created": datetime.now().isoformat(timespec="seconds"), "names_used": any(names.values()),
            **stats, "checks": check(body, utts, data.get("duration", 0), template)}

    if args.out:
        args.out.mkdir(mode=0o700, parents=True, exist_ok=True)
        tag = re.sub(r"[^A-Za-z0-9._-]", "-", args.model) + ("-think" if args.think else "")
        md_path, meta_path = args.out / f"{stem}.{name}.{tag}.md", args.out / f"{stem}.{name}.{tag}.meta.json"
    else:
        md_path, meta_path = TRANSCRIPTS / f"{stem}.summary.md", TRANSCRIPTS / f"{stem}.summary.meta.json"
        if md_path.exists():
            os.replace(md_path, TRANSCRIPTS / f"{stem}.summary.prev.md")
    write_private(md_path, head + body)
    write_private(meta_path, json.dumps(meta, indent=1))
    c = meta["checks"]
    log(f"{md_path.name}: {stats['seconds']}s, {stats['prompt_tokens']} prompt + {stats['output_tokens']} output tokens; "
        f"citations {c['citations_matching_a_line']}/{c['citations']} match a line ({c['citations_past_end']} past the end); "
        f"{c['bullets_cited']}/{c['bullets']} bullets cited"
        + (f"; MISSING sections: {', '.join(c['missing_sections'])}" if c["missing_sections"] else ""))


if __name__ == "__main__":
    main()
