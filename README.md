# earshot

Local-first pipeline for recorder audio: syncs Plaud recordings, transcribes and labels speakers
offline, triages each with a local LLM, and gives you a page to listen, title and file them.

**Status:** a personal project, early and opinionated. It runs every day on one machine
(Windows 11 + WSL2, an NVIDIA RTX 4080 and a Plaud Note), so expect rough edges and hard
requirements. The design notes are in [SPEC.md](SPEC.md).

## What it does

- **Syncs** new recordings from your Plaud account once a day, using Plaud's official CLI login.
  Each download is checked: the byte count matches, it decodes cleanly, and its length agrees
  with what Plaud reports.
- **Transcribes and labels speakers offline.** It uses faster-whisper large-v3 and pyannote
  `speaker-diarization-community-1`. Both run inside a network namespace with no network access.
- **Writes a triage brief with a local LLM** (Ollama, `gemma4:31b` by default):
  - a category (interview, 1:1 meeting, group meeting or family), which files the recording
  - a topic and a suggested title
  - a summary of up to 60 words and a few takeaways

  The brief tells you whether a recording deserves a full summary at all.
- **Review page** at http://localhost:8790:
  - listen with the transcript beside the player, and click a line to jump there
  - rename and recategorize recordings
  - name speakers; giving two labels the same name merges them
  - discard recordings
  - read summaries
- **Backs up** hourly to a NAS. Original audio is append-only and checksum-verified.
- **Alerts on Slack** when:
  - the Plaud login is about to lapse
  - a step fails
  - new recordings are ready

  Once a week it also lists recordings that are safe to delete from Plaud's cloud.

## Privacy model

- **Audio and transcripts stay on your machines.** Transcription and speaker labelling have no
  network access, and briefs go only to an Ollama server you run. Ollama's `-cloud` models are
  refused.
- **Slack messages carry dates and durations only**, never titles or transcript text.
- **The review page listens on 127.0.0.1 only.** It rejects non-localhost `Host` headers (which
  blocks DNS rebinding) and cross-site writes.
- **Plaud's cloud keeps its own copy** until you delete it in the Plaud app. earshot tells you
  when that's safe.

## How it works

```
Plaud cloud ──> sync.py ──> ~/recordings/plaud/audio/        (originals, never modified)
   (17:00)        │
                  ├─> transcribe (faster-whisper, no network)
                  ├─> diarize    (pyannote, no network)  ──> ~/recordings/plaud/transcripts/
                  ├─> brief.py   (local Ollama)          ──> <stem>.brief.json + category
                  └─> Slack: "N new recordings, triage them at localhost:8790"

review/server.py (systemd, :8790) ── listen, title, recategorize, name speakers, discard
backup.sh (hourly) ──> NAS: originals append-only + verified; metadata mirrored, never deleted
```

`folders.csv` in `~/recordings` is the filing record: id, start time, duration, category and
title. It's plain CSV, so you can edit it by hand.

## Requirements

- **Linux or WSL2**, with systemd user services and cron
- **Python 3.11+.** earshot itself uses only the standard library.
- **Tools:** `ffmpeg`, `rsync`, and `unshare` / `flock` (util-linux)
- **An NVIDIA GPU** with about 6 GB of free VRAM for transcription and speaker labelling
- **A Plaud account** and Plaud's official CLI (`@plaud-ai/cli`)
- **pyarize**, a companion repo that isn't published yet. earshot currently runs its download,
  transcription and speaker-labelling scripts, and uses its pyannote environment. Moving the
  download step into earshot is planned.
- **A Python environment with `faster-whisper`**
- **A Hugging Face token**, needed once to download the gated pyannote model
- **Optional:**
  - Ollama with a local model, for briefs. The sync carries on without it.
  - A Slack incoming webhook
  - A mounted NAS share, for backups

## Setup

1. **Clone** earshot and pyarize side by side, e.g. into `~/projects/`.
2. **Install the Plaud CLI** into its own prefix, pinned, without install scripts:
   ```bash
   npm --prefix ~/.local/plaud-cli install --ignore-scripts @plaud-ai/cli@0.3.14
   ```
   Then add a wrapper at `~/.local/bin/plaud`. It turns the CLI's telemetry off, and stops a
   `.env` file in the current folder from redirecting its login endpoints:
   ```bash
   #!/usr/bin/env bash
   export PLAUD_TELEMETRY_DISABLED=1 DO_NOT_TRACK=1 PLAUD_NO_UPDATE_NOTIFIER=1
   export DOTENV_CONFIG_PATH=/dev/null DOTENV_CONFIG_QUIET=true
   exec "$HOME/.local/plaud-cli/node_modules/.bin/plaud" "$@"
   ```
   Make it executable and run `plaud login` once. After that, refresh tokens rotate on each use,
   so a daily sync keeps the login alive.
3. **Set up pyarize** (see its `scripts/README.md`). That covers its pyannote environment, the
   one-time model download with your Hugging Face token, and a Python environment with
   faster-whisper.
4. **Write your machine's settings** to `~/.config/earshot/env` (mode 600). See Configuration
   below.
5. **Optionally, set up Slack.** Export `SLACK_WEBHOOK_URL` in `~/.bashrc_env`, which the cron
   scripts source.
6. **Schedule the sync and backup** with `crontab -e`:
   ```
   0 17 * * * ~/projects/earshot/sync_cron.sh
   45 * * * * ~/projects/earshot/backup.sh
   ```
7. **Install the review page** as a systemd user service. Run
   `loginctl enable-linger $USER` once if you want it running without a login session.
   ```bash
   ln -s ~/projects/earshot/deploy/earshot-review.service ~/.config/systemd/user/
   systemctl --user daemon-reload && systemctl --user enable --now earshot-review
   ```
8. **Check it all:** `~/projects/earshot/sync_cron.sh --dry-run`, then read
   `~/.local/share/earshot/sync.log`.

## Configuration

`~/.config/earshot/env` holds plain `KEY=value` lines, loaded by `sync_cron.sh` and `backup.sh`:

| Variable | Default | What it's for |
|---|---|---|
| `EARSHOT_PYARIZE_DIR` | `~/projects/pyarize` | pyarize checkout (download, transcription and speaker-labelling scripts) |
| `EARSHOT_WHISPER_PYTHON` | `python3` | A Python with `faster-whisper` installed |
| `EARSHOT_OLLAMA_URL` | `$OLLAMA_HOST`, then `localhost:11434` | Ollama server for briefs |
| `EARSHOT_BRIEF_MODEL` | `gemma4:31b` | Brief model; must be a local model |
| `EARSHOT_BACKUP_MOUNT` | `/mnt/s` | Backups are skipped, with an alert, if this isn't mounted |
| `EARSHOT_BACKUP_DIR` | `$EARSHOT_BACKUP_MOUNT/backup/recordings` | Backup destination |

For example:
```
EARSHOT_OLLAMA_URL=http://192.168.1.50:11434
EARSHOT_WHISPER_PYTHON=/home/you/venvs/whisper/bin/python
```

## Using it

Open http://localhost:8790. On WSL2, the Windows browser reaches it at the same address.

- **Triage.** Each new recording shows its brief above the player. Fix the category if it's
  wrong, press **Use title** or type your own title and press Enter, and name the speakers.
  `▶ Sample` plays a speaker's longest stretch, to tell who's who.
- **Discard** (`D`) hides a recording and marks it as not worth a summary. The audio is kept,
  and **Restore** brings it back.
- **Summaries.** A recording with `<stem>.summary.md` next to its transcript shows it on the
  Summary tab. Writing these is manual for now; a button with summary templates per category is
  planned.
- **Keys:**

  | Key | Action |
  |---|---|
  | `Space` | Play / pause |
  | `←` `→` | Back / forward 5 s (`Shift` for 30 s) |
  | `N` | Next untitled recording |
  | `T` | Edit the title |
  | `D` | Discard / restore |
  | `/` | Search |

Command line:
```bash
./sync_cron.sh --dry-run                 # check the login and list new recordings, change nothing
./sync_cron.sh --safe-to-delete          # list Plaud cloud recordings with a verified NAS copy
python3 -I brief.py --missing            # brief everything transcribed that has no brief yet
python3 -I brief.py <id> --force         # redo one brief, e.g. after naming its speakers
systemctl --user restart earshot-review  # after editing review/
```

## Files

```
~/recordings/
├── folders.csv                  # id, start_local, duration_min, folder (category), plaud_transcribed, name, title
└── plaud/
    ├── audio/<YYYY-MM-DD_HHMM>_<id>.{mp3,ogg}   # originals
    ├── raw/                     # Plaud's bare-Opus originals (the .ogg is a lossless rewrap)
    ├── cache/                   # audio-only copies for the browser (rebuildable)
    └── transcripts/<stem>.{json,txt,rttm,diarized.json,diarized.txt,brief.json,speakers.json,summary.md}
~/.local/share/earshot/          # sync.log, backup.log, state.json
~/.config/earshot/env            # your settings
```

Everything earshot writes is mode 600 in 700 directories.

## Known limits and ideas

- **Plaud is the only source so far.** Phone voice memos and plain audio folders are the obvious
  next ones.
- **Suggested titles rarely include names**, because introductions happen before the recorder
  starts. Matching recordings to calendar events would fix that.
- **Linux or WSL with NVIDIA only.** Apple Silicon support would need a different speech stack.
- **earshot depends on Plaud's developer API** and on the formats it serves today: MP3, Ogg Opus,
  and bare Opus packets.
