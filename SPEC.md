# earshot — SPEC

**Status:** v1.1 draft (2026-10-27). Renamed from capture to earshot on 2026-10-28. Plaud is the only source for now.
**Owner:** michaelv2
**Depends on:** `pyarize` (diarized transcription), the official Plaud CLI, and the DS418 mount.

---

## 1. Summary

`earshot` pulls recordings from their sources, archives the original audio, and sends it to
pyarize. It then delivers the diarized transcripts, unattended, raising Slack alerts when
something needs a person.

```
Plaud cloud ───┐                                   ┌──> ~/recordings/<source>/transcripts/
(later: memos) ┼──> earshot ──> pyarize (socket) ──┤
               │       │                           └──> later: memoize, a speaker-tagging UI
               │       └──> ~/recordings archive ──> DS418 (/mnt/s/backup/recordings)
               └── Slack: stale login, failures, new recordings to file
```

Each source's quirks stay here: Plaud's three formats, short cloud copies, token lifetimes, and
the missing folder API. Pyarize only ever sees standard audio.

---

## 2. Goals and non-goals

### Goals

- **G1:** Unattended Plaud sync. New recordings are archived, verified and transcribed with no
  manual step.
- **G2:** Slack alerts when a person is needed:
  - the login is about to expire or has expired
  - a sync, verification or backup has failed
  - pyarize is unreachable
  - new recordings are waiting to be filed

  Each condition alerts once, then not again until it's resolved.
- **G3:** The archive is the source of truth. Originals are kept bit-exact, forever, with a
  backup on the DS418. Cloud copies can then be deleted in the Plaud app.
- **G4:** Idempotent and resumable. Every step can be re-run safely, and a crash mid-download
  never leaves a file under its final name.
- **G5:** No new dependencies. earshot uses only the standard library, the already-audited Plaud
  CLI, and pyarize's stdlib-only client.

### Non-goals

- **Transcription and diarization.** pyarize does these.
- **Summaries and speaker roles.** Consumers add these (pyarize SPEC §2).
- **Deleting cloud copies.** The Plaud API can't. earshot reports which ones are safe to delete
  (§6).

---

## 3. Sources

### 3.1 Plaud

**API.** The base URL is `platform.plaud.ai/developer/api/open/third-party`.

- **Endpoints used:** `files/` (list) and `files/{id}` (detail). Detail includes `presigned_url`,
  a 24-hour S3 link that is **never logged**, plus `source_list` and `note_list`. A non-empty
  `source_list` or `note_list` means Plaud has processed the file itself.
- **Headers.** Raw HTTP calls need `Accept: application/json` and a non-default `User-Agent`;
  without them the API returns 403.
- **Rate limits.** Detail calls hit 429 after about 25 calls at 0.2 s spacing. 2 s spacing ran
  clean. Back off on `Retry-After`.
- **No folders, no delete.** List items carry only `id`, `name`, `created_at`, `start_at`,
  `duration` and `serial_number`. The app's folders (unfiled, candidates, work, family; `candidates` is `interview` here) aren't
  exposed, so filing is manual (§4).
- **The listing is only what's still in the cloud.** It went from 42 files to 1 once the
  backfilled originals were deleted in the app. One file once returned 404 on detail while
  still being listed.
- **IDs changed prefix** from `of_` to `f_` around 2026-10-26. Treat IDs as opaque strings.

**Auth.** The daemon never implements OAuth itself. Auth goes through the wrapper
`~/.local/bin/plaud`. The wrapper:
- disables telemetry
- sets `DOTENV_CONFIG_PATH=/dev/null`, so a `.env` in the working directory can't redirect the
  auth endpoints
- wraps `@plaud-ai/cli` 0.3.14, which passed the dependency audit on 2026-10-18 and was installed
  with `--ignore-scripts`

**Tokens.**
- **Lifetimes:** the access token lasts 1 day and the refresh token 7 days. Both are JWTs, kept
  in `~/.plaud/tokens.json`.
- **When it refreshes:** the CLI refreshes lazily, on any authenticated call made within 60 s of
  the access token expiring.
- **Rotation: confirmed 2026-10-27.** The first observed refresh returned a new refresh token
  with a fresh 7-day expiry, and the CLI saved it (`data.refresh_token ?? refreshToken`): the
  token issued 2026-10-25 23:39 (expiring 11-01) became one expiring 11-03 06:42. So the 7 days
  are a sliding window, and a login survives indefinitely as long as something refreshes at
  least once a week.
- **Expiry:** if the refresh token has expired or is rejected, the CLI deletes `tokens.json`.
  Recovery then needs an interactive browser login (`plaud login`).

The daemon's auth step:
1. Run `plaud me` with all output discarded. It calls `users/current` through the CLI's normal
   refresh path, and prints the user's email, which must not be kept.
2. Read the access token from `tokens.json` for the raw HTTP calls. Never print it.
3. Decode the refresh token's `exp`. This only reads the JWT payload; there's no signature check.
4. If more than 48 h remain, carry on. If less, send a warning, at most daily. If the file is
   missing, or the refresh was rejected, alert immediately with the fix:
   "run `plaud login` in a terminal".
5. Track the refresh token's `exp`. Because tokens rotate, it should move forward about once a
   day. If it's under 48 h away, refreshing has stopped (for example, the WSL instance was down
   for days), and step 4's warning is what catches it.

A lock file around CLI calls stops the daemon and an interactive `plaud` from refreshing at the
same moment. Rotation makes this matter more: if the server rejects an already-used refresh
token, the losing process's CLI would delete `tokens.json`. Whether it does that is untested.

**Formats.** `presigned_url` serves three formats. The `Content-Type` header is unreliable, so
the format is detected from the bytes:

| Bytes | Format | Handling |
|---|---|---|
| `ID3` or an MPEG frame sync | MP3, 16 kHz mono, 32 kbps CBR | Stored as-is (`.mp3`) |
| `OggS` | Ogg Opus, plus an extra `PLAUD.AI` metadata stream | Stored as-is (`.ogg`). A missing end-of-stream flag is normal |
| `0xb8` at every 80-byte boundary | **Raw Opus**: bare 20 ms CELT-wideband packets, no container | Wrapped losslessly into Ogg (`plaud_raw2ogg.py`); the original is kept in `raw/` |

**Verification.** Each download is checked in order:
1. The byte count must match `Content-Length`.
2. A full ffmpeg decode must show zero errors.
3. The decoded duration is compared with the API's `duration`, and the drift decides the outcome:

| Drift | Outcome |
|---|---|
| ≤ 3 s | Accept |
| 3–30 s, with a clean decode | Accept, flagged `short_cloud_copy`. Re-check once after 24 h, and if the cloud copy has grown, archive the longer one alongside |
| > 30 s, or any decode error | Quarantine to `~/recordings/plaud/quarantine/` and alert |

The first post-backfill file (`f_01a1…`, 2026-10-26) was 9.4 s short and decoded cleanly.

### 3.2 iOS voice memos (deferred)

Deferred on 2026-10-27; Plaud comes first. When this is picked up, the transport needs choosing
(an iOS Shortcut saving to the NAS, iCloud Drive for Windows, or Signal voice notes to memoize's
bot), and two constraints apply to any choice:
- **WSL can't watch Windows folders.** `inotify` doesn't fire on `/mnt/c` or the `/mnt/s` 9p
  mount, so pickup must poll.
- **Memos are single-speaker.** They go to pyarize with `diarize.enabled: false`.

---

## 4. Pipeline and state

State lives in `~/.local/share/earshot/state.db` (SQLite). Each recording moves through:

```
discovered → downloaded → verified → archived → backed_up → submitted → transcribed → delivered
                               └──→ quarantined (alert)                       └──→ failed (retry with backoff; alert after 3)
```

- **Filing.** The API has no folders, so new recordings get `folder = unfiled`. The owner files
  them with `earshot file <id> <folder>`, or by editing `~/recordings/folders.csv`. The CSV stays
  user-editable and is the filing source of truth; the daemon only appends rows. If recordings
  are still unfiled after 24 h, a digest alert lists them, showing only date, time and duration.
- **Transcription doesn't wait for filing.** The folder only affects later routing, such as
  sending candidate interviews to an interview-notes sink.
- **Plaud-processed flag.** The flag is recorded, but Plaud's own transcripts and summaries are
  never fetched.

**Storage layout.** This is unchanged from the 2026-10 backfill. Directories are 0700 and files
0600.

```
~/recordings/
├── folders.csv                          # id, start_local, duration_min, folder, plaud_transcribed, name, title, archived
└── plaud/
    ├── cache/<stem>.ogg                # audio-only copies for the review page
    ├── audio/<YYYY-MM-DD_HHMM>_<id>.{mp3,ogg}
    ├── raw/<stem>.opus                  # raw-Opus originals
    ├── quarantine/
    └── transcripts/<stem>.{json,txt,rttm,diarized.json,diarized.txt,speakers.json,brief.json,summary.md,summary.meta.json}
```

---

### 4.1 Review page (built 2026-10-27)

`python3 -I review/server.py` serves http://localhost:8790/, reachable from the Windows browser.
It's standard library only, and listens on 127.0.0.1 unless remote access is configured.

- **Listen.** The player sits next to the diarized transcript. Clicking a line seeks to it, and
  the current line follows playback.
- **Name, file and tag.** Each recording gets a friendly title and a folder, written to
  `folders.csv` as its `title` and `folder` columns. Speaker labels can be mapped to names or
  roles in `<stem>.speakers.json`. Giving two labels the same name merges them in the display.
  Files keep their `<date>_<id>` names, because those names link the audio, raw and transcript
  files.
- **Inbox (2026-11-01).** The page opens on an Inbox tab listing every recording that is
  neither archived nor discarded. Archive means "dealt with", for example after its details
  were copied into memoize by hand. It's separate from the category, so filing is never lost.
  `folders.csv` gains an `archived` column holding the local date (blank means in the inbox).
  The review page is its only writer, and the other writers carry it through because they're
  header-aware. Archiving can be done from the recording (`A`) or from its row in the list, with
  an undo toast.
- **Download.** A transcript with speaker names is available for summaries.
- **Tabs.** Each recording has Transcript and Summary tabs. The Summary tab shows
  `<stem>.summary.md` through a small Markdown renderer that builds text nodes only, so a
  summary can never inject HTML. `summarize.py` writes it (§4.2), from the command line or the
  tab's Generate button. `[mm:ss]` citations render as buttons that move the player there.
- **Audio.** Plaud's Ogg files carry a second, unidentified metadata stream, so they play through
  an audio-only stream copy in `plaud/cache/` (0600). The originals are never touched.
- **Request checks.** A localhost (or configured) `Host` header is required, which blocks DNS
  rebinding. Writes also need the `X-Earshot` header, a JSON body, and a same-origin `Origin`.
  All transcript text is rendered as text, never as HTML.
- **Remote access (2026-11-01).** Opt-in, for a phone on WireGuard (router → Windows portproxy →
  WSL). Non-loopback clients must come from an allowed network and hold a session cookie: an
  HMAC of `EARSHOT_REVIEW_TOKEN`, set by opening `/?token=…` once (HttpOnly, SameSite=Strict, one
  year; the response redirects to `/`). The token is compared in constant time. Under WSL2 every
  forwarded connection arrives from the default gateway (`--allow-from gateway`), which keeps
  Docker containers out but can't tell LAN from VPN clients. The Windows firewall rule, scoped
  to the VPN subnet, does that; the token covers a mistake there. The server refuses to listen
  beyond loopback without allowed networks, hosts and a token of 20+ characters.

### 4.2 Full summaries (built 2026-11-01)

`summarize.py <id>` drafts the notes you work from before copying anything into memoize. It runs
only when asked. The daily sync never runs it, and the brief stays the triage step.

- **Templates.** There is one per category in `templates/`: `interview`, `one-on-one` and
  `group-meeting`. Family recordings have none, because the brief covers them (they're a median
  of 2 minutes). `{owner}` stands for `EARSHOT_OWNER`. A file of the same name in
  `~/.config/earshot/templates/` overrides one, so a private version, such as a role rubric,
  stays out of the repo. Rules that apply to every template live in `summarize.py`:
  - every bullet cites a transcript time
  - a decision must be explicit
  - an action item must be something someone said would be done
  - an owner is named only when it's clear
  - likely mis-transcriptions are flagged
- **Interviews record evidence, not verdicts.** The template forbids rating or ranking the
  candidate and recommending for or against hiring. It also leaves out personal characteristics
  that come up in small talk.
- **Input.** The diarized transcript with a `[mm:ss]` time on every line, plus:
  - the speaker names from the review page. Owners need them, so the script warns when none are set
  - talk times
  - the recording's date and title

  The instructions come after the transcript.
- **Output.**
  - `<stem>.summary.md` is what the Summary tab shows. The script writes the title, date, length
    and participants itself, not the model, and adds the action-item checkboxes if the model
    leaves them out.
  - `<stem>.summary.meta.json` holds the model, the template and its hash, tokens and time. It
    also holds checks: citations that match a transcript line, citations past the end, bullets
    without a citation, and missing sections.
  - A previous summary is kept as `.summary.prev.md`.
  - `--out DIR` writes somewhere else, for comparing models.
- **Model.** Local only, using brief.py's check. The default is `qwen3.8:27b-128k` with reasoning
  on (`EARSHOT_SUMMARY_MODEL`, `--no-think`). It gets no tools, so the agentic risks in the
  2026-10-26 Qwen audit don't apply. The transcripts reach llm-server whichever model runs.
- **Context.** Up to 64k tokens, with a 32k reasoning budget. The longest recording so far
  (86 minutes, 18k prompt tokens) used 39k.

**Bake-off, 2026-11-01.** The benchmark was a 20-minute 1:1 with named speakers. Each draft was
scored against the points in a hand-checked summary, and checked against the transcript. Rounds
1–3 used an unwritten list of about 31 points; round 4 used a written 32-point checklist (kept
privately with the drafts), and two earlier drafts were rescored against it, shown in brackets.

| Model | Time | Points covered | Errors of fact | Overreach | Citations on a real line |
|---|---|---|---|---|---|
| gemma4:31b | 57 s | ~19 | 1 | 1 | 63/70 |
| qwen3.8:27b | 53 s | ~19 | 2 | 3 | 55/55 |
| gemma4:31b, reasoning | 124 s | ~21 (23.5/32) | 1 | 0–1 | 84/85 |
| qwen3.8:27b, reasoning | 142 s | ~31 | 1 | 3 | 137/137 |
| qwen3.8:27b, reasoning, tightened rules | 166 s | ~31 (32/32) | 0 | 1 | 115/115 |
| muse-glimmer:30b, no reasoning | — | — | — | — | — |
| *Round 4: bullet budget, mis-transcriptions capped at 10* | | | | | |
| qwen3.8:27b, reasoning | 148 s | 27.5/32 | 2 | 3 | 93/93 |
| gemma4:31b, reasoning | 131 s | 23.5/32 | 1 | 0 | 65/66 |
| muse-glimmer:30b, reasoning | 473 s | 23/32 | 1 | 0 | 61/63 |

- **Overreach** means an owner nobody stated, a proposal recorded as a decision, or an invented
  action item. The tightened rules fixed most of it: decisions must be explicit, open issues go
  to follow-ups, and paraphrase is the default.
- **Citations** sometimes point at the neighbouring line, a few seconds off.
- **muse-glimmer** ignores `think: false`: it spent 6,000 tokens on reasoning that Ollama
  discarded, and returned nothing. With reasoning on it works, but it's 3.5x slower than the
  others (62k characters of reasoning), missed a headline number, found one action item, and writes
  in fragments. Not worth pursuing.
- **Mis-transcriptions.** Without a hint, no model worked out the benchmark's main mis-heard
  term. The round 1–3 rules happened to use it as their example, which is why those drafts got
  it; the rules now use a generic one.

**At scale, with reasoning:**
- An 81-minute interview took 246 s (gemma4: 138 s).
- An 86-minute group meeting took 363 s.

**Length.** Uncapped, qwen3.8 wrote about 12 KB for the 20-minute 1:1 (the reference was 6.6
KB), 23 KB for the interview (gemma4: 7 KB) and 31 KB for the group meeting, including 67
mis-transcription bullets. Round 4 tried a bullet budget for each template's main section (about
1.5 a minute, 10 to 40) and capped the mis-transcription list at 10:

| qwen3.8, reasoning | Before | After | Main section | Mis-transcriptions |
|---|---|---|---|---|
| 20-min 1:1 | 12.5 KB | 10.7 KB | 8.4 → 6.5 KB (77 → 31 bullets, budget 29) | 0.9 → 0.8 KB |
| 81-min interview | 23.3 KB | 16.4 KB | 11.8 → 8.1 KB (56 → 43, budget 40) | 1.9 → 0.8 KB |
| 86-min group meeting | 30.7 KB | 21.1 KB | 20.3 → 14.7 KB (119 → 90, budget 40) | 3.9 → 0.8 KB |

The mis-transcription cap is a clear win. The budget is a trade-off: on the benchmark it cost
4.5 points (32 → 27.5; four secondary points were merged away) for 1.9 KB, and qwen3.8 ignored it on the group meeting. One run each, so some of that is
run-to-run variation; overreach went from 1 to 3, which round 2 also saw without a budget.

**Decision:** keep the mis-transcription cap and drop the budget. A point a draft leaves out is
invisible unless you reread the transcript, while extra length only costs skimming, and drafts
are read to excerpt from. If long drafts turn out to be unusable, the budget can come back.

**Generate button (built 2026-11-01).** `POST /api/recordings/<id>/summary {"template"}`, with
the same write checks as edits, starts `summarize.py` in a background thread. One job runs at a
time across all recordings, because each takes minutes of llm-server time; a second start gets
409. `GET /api/summary-job` reports the current or last job (state, elapsed, last output line).
The page polls it every 3 s while a job runs and every 15 s otherwise, so a job started on
another device shows up. A finished draft replaces the panel in place, leaving playback alone.
Jobs live in memory: restarting the service stops a running one. The child process doesn't get
`EARSHOT_REVIEW_TOKEN`. Generation is off with `--root`, since `summarize.py` only uses
`~/recordings`.

**Next (P3).** A memoize-notes template, and editing a draft on the page.

## 5. Alerts

Alerts post to Slack through `SLACK_WEBHOOK_URL`, the same webhook, environment file
(`~/.bashrc_env`) and `send_alert` style as the existing `~/.local/bin/backup-*.sh` jobs.

- **Content.** Alerts never include recording titles or transcript text. Plaud's titles are
  generated from the content (e.g. "Interview: Senior Engineer Candidate …"), and Slack is a third party.
  Alerts carry the ID, local start time, duration and condition only.
- **Deduplication.** The state DB keeps which alerts are open. A resolved condition posts one
  ✅ message.

---

## 6. Backup and cloud deletion

**Built 2026-10-28** as `earshot/backup.sh`, hourly at :45 (see the README). It differs from
the plan below in three ways:
- It lives in this repo rather than `~/.local/bin/`.
- The share refuses to set file times, so the mirror uses `rsync --update` instead of `-t`.
- The weekly safe-to-delete list rides on the 5 pm sync.

Categories replaced folders the same day. `folder` in `folders.csv` now holds one of interview,
1:1 meeting, group meeting, family, unfiled or discard, and brief.py sets it at ingest.

**Backup (original plan).** A new `~/.local/bin/backup-recordings.sh` follows the existing jobs' pattern:
- an hourly cron entry
- a `mountpoint -q /mnt/s` check that alerts if the mount is missing
- the destination `/mnt/s/backup/recordings/`

It differs from the database backups, which make timestamped copies kept 7 days:
- **Append-only.** It copies new files only and never deletes on the NAS. A local deletion
  never propagates.
- **Verified by hash.** Each copy is sha256-checked against the archive, and only then is the
  recording marked `backed_up`.
- **Unencrypted.** The owner confirmed on 2026-10-27 that the DS418 is readable only by them,
  so private audio is copied as-is. If the share's permissions ever change, revisit this before
  the next run. Encryption would mean a tool like `age` or `restic`, and a dependency audit.

**Cloud deletion.** `earshot status --safe-to-delete` lists recordings that are archived,
verified and backed up. The owner deletes those in the Plaud app.

---

## 7. Scheduling

**Current (2026-10-27):** `0 17 * * * sync_cron.sh`, once a day at 5 pm. It runs `sync.py`, an
interim sync built on the pyarize scripts. The 30-minute schedule below is the target.


The sync runs from cron, like the other WSL jobs, every 30 minutes from 07:00 to 23:00. That's
frequent enough to catch an upload soon after a meeting, and it still refreshes well within the
refresh token's 7-day life.

It depends on the WSL instance being up, as the existing backup jobs already do. A daily
heartbeat can catch silent stalls: if there's been no successful sync in 24 h, alert from
whatever process runs next.

---

## 8. Security

- **Secrets:**
  - `~/.plaud/tokens.json` is managed by the CLI. earshot only reads the access token.
  - `SLACK_WEBHOOK_URL` comes from `~/.bashrc_env`.
  - `EARSHOT_REVIEW_TOKEN` (remote access to the review page) lives in `~/.config/earshot/env`
    (0600). It's never logged; `--link` prints it on request.
  - Neither, nor the presigned URLs, ever goes to logs or stdout.
- **Downloads are untrusted data.** They're sniffed by bytes, decoded by ffmpeg only, kept in
  their own directories and never executed. Any Python that reads them runs with `-I`.
- **Dependencies.** None beyond the standard library (G5). Adding one needs a security-auditor
  pass first. The Plaud CLI is pinned at 0.3.14, and upgrading it means a new audit.
- **pyarize access.** Through its Unix socket only. earshot never needs a GPU, a model or an HF
  token.

---

## 9. Milestones

| # | Deliverable | Exit criterion |
|---|---|---|
| **M1** | One-shot sync | `earshot sync --once` covers listing, download, verification, archive and the `folders.csv` row. `plaud_ingest.py` and `plaud_raw2ogg.py` move here from `pyarize/scripts/`. Transcription calls `pyarize transcribe` until the service exists |
| **M2** | Unattended | Cron schedule, auth through `plaud me`, refresh-token expiry tracked, Slack alerts deduped, heartbeat |
| **M3** | Backup | `backup-recordings.sh` live, unencrypted. The `--safe-to-delete` report |
| **M4** | pyarize service | Jobs submitted and polled over the socket. Results delivered. Recordings queue, rather than fail, while pyarize is down (pyarize M2/M3) |
| **M5** | memoize sink | If chosen: distilled notes, not raw transcripts (Appendix A) |
| Later | Voice memos | Transport chosen. Polling pickup. Transcription only (§3.2) |

---

## 10. Open questions

1. **Filing.** Is the 24-hour unfiled digest the right nudge, or should filing be possible from
   Slack? Replying from Slack would mean an inbound path, which earshot doesn't have.
2. **memoize delivery.** Which recordings, if any, should become memoize entries, and who
   distils them? An LLM summarizer would be a consumer of earshot's output.

---

## Appendix A — Findings carried over from pyarize SPEC v3 (2026-08-09)

These were recorded when the Plaud pipeline was first dropped. They still hold unless noted.

**Plaud hardware (Plaud Note, 2026-08-08):**

- **USB file access was removed in firmware V2.1**, per Plaud support, "for data compliance."
  Pre-2.1 devices have an "Access via USB" toggle that exposes `NOTES/` and `CALLS/` as mass
  storage. On 2.1 and later the toggle is gone. Observed here as
  `Unknown USB Device (Device Descriptor Request Failed)` (`VID_0000&PID_0002`), and then no
  enumeration at all.
- **Consequence:** on current firmware, audio has to pass through Plaud's cloud. Declining their
  transcription keeps the derived text local, but the audio itself can't be kept off their
  servers.
- **Web export is MP3 only, one file at a time.** The app can export WAV. The API is the only
  low-friction route.
- **Several third-party clients** log in with the account email and password instead of scoped
  tokens. Avoid them.

**Host (WSL2):**

- WSL2 doesn't see host USB storage; `lsblk` shows only virtual disks. A device with a Windows
  drive letter is reachable at `/mnt/<letter>`, and raw block access needs `usbipd-win`.
- **`inotify` doesn't fire on `/mnt/c` or other drvfs paths.** A `watchdog` observer pointed at a
  Windows folder never triggers. Watched folders must be on ext4, or use `PollingObserver`.

**memoize integration, for a file-based funnel:**

- `memoize sync` (`memoize.py:1674`):
  - globs `*.md` from `MEMOIZE_SYNC_DIR`
  - **skips names that start with `_`**, so write-then-rename is atomic
  - takes a leading `YYYY-MM-DD` from the filename as the entry's date
  - dedupes through a `sync_manifest` table

  `--force` deletes an entry and its entities before re-ingesting.
- `memoize add --file` (`memoize.py:1644`) splits the content on `[YYYY-MM-DDTHH:MM` timestamps
  and creates **one entry per block**, so any header before the first timestamp becomes a
  phantom entry. It's the only path with per-file `--private` control; `sync` applies one
  sensitivity to the whole run.
- YAML frontmatter is a liability in ingested files. `raw_text` is stored verbatim and embedded,
  so frontmatter pollutes retrieval snippets.
- Segmentation thresholds (`limits.toml`) never go below 200 characters and force a split above
  20,000. A 60-minute transcript is about 55,000 characters, which is why raw transcripts make a
  poor memoize input: they're mostly filler that an LLM then has to compress lossily.

(Line numbers are from 2026-08 and may have moved.)
