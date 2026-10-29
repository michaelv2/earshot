"""Build a small fake recordings library for screenshots and trying out the review page.

Everything here is invented: people, conversations and audio (silence). Nothing is read from a
real library.

usage: python3 -I demo/make_demo.py [target=/tmp/earshot-demo]
       python3 -I review/server.py --root /tmp/earshot-demo/recordings --port 8791
"""

import csv
import json
import subprocess
import sys
from pathlib import Path

TARGET = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/earshot-demo") / "recordings"
AUDIO, TRANSCRIPTS = TARGET / "plaud" / "audio", TARGET / "plaud" / "transcripts"

# The featured recording: a 1:1 about a product launch, with named speakers, a brief and a summary.
FEATURED = [
    ("SPEAKER_01", "Okay, I think we're recording. The main thing I wanted to cover today is the launch date."),
    ("SPEAKER_00", "Sure. Where did we land after Tuesday's review?"),
    ("SPEAKER_01", "Engineering says the migration work is about a week behind. Payments is fine, it's the data backfill."),
    ("SPEAKER_00", "Is that a week we can absorb, or does it push the announcement?"),
    ("SPEAKER_01", "If we keep the announcement on the fourth, support only gets two days with the final build. That's the part I'm worried about."),
    ("SPEAKER_00", "Two days isn't enough for the help center articles. They wanted a full week last time."),
    ("SPEAKER_01", "Right. So my proposal is we move the public announcement to the eleventh and keep the beta invites on schedule."),
    ("SPEAKER_00", "That works for me, as long as sales hears it from us before Friday."),
    ("SPEAKER_01", "Agreed. I can draft the note to sales today."),
    ("SPEAKER_00", "Great. What about the pricing page? Is legal still reviewing the annual plan wording?"),
    ("SPEAKER_01", "They sent comments yesterday. Mostly small things, and one real question about refunds on annual plans."),
    ("SPEAKER_00", "Let's take the refund question to Thursday's meeting with finance. I don't want to guess on that one."),
    ("SPEAKER_01", "Makes sense. Last thing is the beta feedback. We've had forty-two responses so far."),
    ("SPEAKER_00", "And the headline?"),
    ("SPEAKER_01", "People like the new dashboard, but setup takes too long. Almost everyone mentioned the import step."),
    ("SPEAKER_00", "That matches what I heard from the two customers I talked to. Can we get a quick fix in before launch?"),
    ("SPEAKER_01", "Probably a progress indicator and better error messages. The real fix is a bigger piece of work."),
    ("SPEAKER_00", "Let's do the small version now and put the import rework on the Q1 roadmap."),
    ("SPEAKER_01", "Okay. I'll write that up and share it with the team."),
    ("SPEAKER_00", "Perfect. Anything else from your side?"),
    ("SPEAKER_01", "No, that's it. Thanks."),
]

FEATURED_SUMMARY = """# Jordan K sync: key points and action items

27 Oct, 09:30, 24 min. Sam and Jordan.

## Key points

**Launch date**
- The data backfill is about a week behind; payments is on track.
- Keeping the 4th would give support only two days with the final build, and help center articles
  need a week.
- **Decision:** move the public announcement to the 11th. Beta invites stay on schedule.

**Pricing page**
- Legal's comments are mostly minor. One open question: refunds on annual plans.

**Beta feedback**
- 42 responses. The new dashboard lands well; setup is slow, especially the import step.
- **Decision:** ship a small fix now (progress indicator, clearer errors). The import rework goes
  on the Q1 roadmap.

## Action items

**Jordan**
- [ ] Tell sales about the new announcement date before Friday.
- [ ] Write up the small import fix and share it with the team.

**Sam**
- [ ] Bring the annual-plan refund question to Thursday's finance meeting.
- [ ] Add the import rework to the Q1 roadmap.
"""

# (id, start, minutes, category, title, brief or None, speakers, status)
#   status: "diarized", "transcribed" (no speaker labels yet)
RECORDINGS = [
    ("f_demo0001", "2026-10-27 09:30", 24.2, "1:1 meeting", "Jordan K sync",
     {"category": "1:1 meeting", "topic": "Launch date and beta feedback", "title": "Launch timing sync",
      "summary": "Jordan and Sam move the public announcement to the 11th so support gets a full week with the "
                 "final build; beta invites stay on schedule. Legal's refund question goes to Thursday's finance "
                 "meeting. Beta feedback is positive apart from slow setup, so a small import fix ships now and "
                 "the rework moves to Q1.",
      "takeaways": ["Public announcement moves from the 4th to the 11th.",
                    "Sales hears about the new date before Friday.",
                    "A small import fix ships before launch; the full rework goes to Q1."]}, 2, "diarized"),
    ("f_demo0002", "2026-10-27 13:00", 41.5, "interview", "Riley M interview",
     {"category": "interview", "topic": "Backend engineering role", "title": "Candidate interview",
      "summary": "A backend engineer walks through a payments migration they led, how they handled a data "
                 "backfill under deadline, and their approach to on-call. Thoughtful about trade-offs; less "
                 "experience with frontend work.", "takeaways": []}, 2, "diarized"),
    ("f_demo0003", "2026-10-26 16:10", 52.0, "group meeting", "Launch readiness review",
     {"category": "group meeting", "topic": "Launch readiness", "title": "Launch readiness review",
      "summary": "The team reviews open launch risks: the backfill delay, support training, and the pricing "
                 "page. Owners are assigned for each.", "takeaways": []}, 5, "diarized"),
    ("f_demo0004", "2026-10-26 11:05", 18.7, "1:1 meeting", "",
     {"category": "1:1 meeting", "topic": "Dashboard design handoff", "title": "Design handoff sync",
      "summary": "A designer hands off the dashboard specs and flags two empty states that still need copy.",
      "takeaways": []}, 2, "diarized"),
    ("f_demo0005", "2026-10-25 18:40", 33.1, "family", "Weekend plans",
     {"category": "family", "topic": "Weekend plans", "title": "Weekend plans",
      "summary": "Planning a Saturday hike and who's bringing what to Sunday dinner.", "takeaways": []}, 3, "diarized"),
    ("f_demo0006", "2026-10-24 10:00", 47.9, "interview", "",
     {"category": "interview", "topic": "Product manager role", "title": "Candidate interview",
      "summary": "A product manager candidate discusses prioritising a roadmap with limited engineering capacity.",
      "takeaways": []}, 3, "diarized"),
    ("f_demo0007", "2026-10-23 15:30", 0.3, "discard", "",
     {"category": None, "topic": "", "title": "", "summary": "No speech detected.", "takeaways": []}, 0, "diarized"),
    ("f_demo0008", "2026-10-23 09:15", 29.4, "group meeting", "Weekly ops standup",
     {"category": "group meeting", "topic": "Weekly operations", "title": "Ops standup",
      "summary": "Incident follow-ups, an upcoming on-call rotation change, and a vendor renewal.",
      "takeaways": []}, 4, "diarized"),
    ("f_demo0009", "2026-10-22 14:00", 12.0, "unfiled", "", None, 0, "transcribed"),
]


def silence(path: Path, seconds: float) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
                    "-t", f"{seconds:.0f}", "-c:a", "libmp3lame", "-b:a", "8k", str(path)], check=True)


def utterances(rid: str, n_speakers: int) -> list[dict]:
    if rid == "f_demo0001":
        out, t = [], 0.0
        for spk, text in FEATURED:
            dur = max(2.0, len(text.split()) * 0.38)
            out.append({"speaker": spk, "start": round(t, 2), "end": round(t + dur, 2), "text": text, "words": []})
            t += dur + 0.6
        return out
    lines = ["Thanks for making the time.", "Happy to. Where do you want to start?", "Let's start with the timeline.",
             "Sounds good.", "I'll follow up by email."]
    return [{"speaker": f"SPEAKER_{i % n_speakers:02d}", "start": i * 6.0, "end": i * 6.0 + 4.0, "text": t, "words": []}
            for i, t in enumerate(lines[:max(n_speakers, 2) + 1])] if n_speakers else []


def main() -> None:
    if TARGET.exists() and any(TARGET.iterdir()):
        sys.exit(f"{TARGET} already exists; remove it or pick another target")
    AUDIO.mkdir(parents=True)
    TRANSCRIPTS.mkdir(parents=True)
    with open(TARGET / "folders.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "start_local", "duration_min", "folder", "plaud_transcribed", "name", "title"])
        for rid, start, minutes, category, title, brief, n_spk, status in RECORDINGS:
            w.writerow([rid, start, minutes, category, "no", "", title])
            stem = f"{start[:10]}_{start[11:13]}{start[14:16]}_{rid}"
            # Only the featured recording needs its real length, for the player; the rest are short.
            silence(AUDIO / f"{stem}.mp3", minutes * 60 if rid == "f_demo0001" else 2)
            utts = utterances(rid, n_spk)
            (TRANSCRIPTS / f"{stem}.json").write_text(json.dumps(
                {"duration": minutes * 60, "segments": [{**u, "words": []} for u in utts]}))
            if status == "diarized":
                (TRANSCRIPTS / f"{stem}.diarized.json").write_text(json.dumps(
                    {"duration": minutes * 60, "utterances": utts}))
            if brief:
                (TRANSCRIPTS / f"{stem}.brief.json").write_text(json.dumps(
                    {"model": "gemma4:31b", "created": "2026-10-28T10:00:00", "names_used": False, **brief}))
            if rid == "f_demo0001":
                (TRANSCRIPTS / f"{stem}.speakers.json").write_text(json.dumps({"SPEAKER_00": "Sam", "SPEAKER_01": "Jordan"}))
                (TRANSCRIPTS / f"{stem}.summary.md").write_text(FEATURED_SUMMARY)
            if rid == "f_demo0003":
                (TRANSCRIPTS / f"{stem}.speakers.json").write_text(json.dumps({"SPEAKER_00": "Sam", "SPEAKER_01": "Jordan"}))
    print(f"demo library written to {TARGET}")


if __name__ == "__main__":
    main()
