#!/usr/bin/env bash
# Back up ~/recordings to the DS418 (SPEC §6). Hourly from cron.
#
# - Originals (plaud/audio, plaud/raw) are append-only: new files are copied but never overwritten
#   or deleted on the NAS, and each new copy is sha256-checked against the source. A bad copy is
#   removed so the next run retries it.
# - Everything else (transcripts, briefs, summaries, speaker names, folders.csv) is mirrored with
#   updates but no deletions. plaud/cache holds derived audio and is skipped.
# - folders.csv also keeps one dated copy per day in folders-history/, for 90 days.
# The share is readable only by the owner (confirmed 2026-10-27), so nothing is encrypted.
# The share refuses to set file times (EPERM), so rsync never passes -t: originals don't need times
# (new files only), and the mirror uses --update. Each NAS copy carries the time it was written, so
# an unchanged file is older than its copy and skipped, while an edited one is newer and copied.
#
# cron: 45 * * * * ~/projects/earshot/backup.sh
# log:  ~/.local/share/earshot/backup.log

if [ -f "$HOME/.bashrc_env" ]; then
    source "$HOME/.bashrc_env"
fi
if [ -f "$HOME/.config/earshot/env" ]; then
    set -a; source "$HOME/.config/earshot/env"; set +a
fi
umask 077
SRC="$HOME/recordings"
MOUNT="${EARSHOT_BACKUP_MOUNT:-/mnt/s}"
DEST="${EARSHOT_BACKUP_DIR:-$MOUNT/backup/recordings}"
STATE_DIR="$HOME/.local/share/earshot"
mkdir -p -m 700 "$STATE_DIR"
exec >> "$STATE_DIR/backup.log" 2>&1
exec 9> "$STATE_DIR/backup.lock"
flock -n 9 || exit 0  # a previous run is still going

log() { echo "$(date '+%F %T') $*"; }

send_alert() {
    local message="$1"
    log "ALERT: $message"
    if [ -n "${SLACK_WEBHOOK_URL:-}" ]; then
        local escaped
        escaped=$(echo "$message" | sed 's/"/\\"/g')
        curl -s -X POST -H 'Content-type: application/json' \
            --data "{\"text\":\"🔴 *Recordings backup failed* ($(hostname))\n${escaped}\"}" \
            "$SLACK_WEBHOOK_URL" > /dev/null 2>&1
    fi
}

if ! mountpoint -q "$MOUNT" 2>/dev/null; then
    send_alert "NAS not mounted at $MOUNT — recordings backup skipped. Mount and re-run manually."
    exit 1
fi
mkdir -p "$DEST/plaud/audio" "$DEST/plaud/raw" "$DEST/folders-history"

FAILED=()
COPIED=0

# 1. Originals: copy new files only, then verify each one.
for sub in plaud/audio plaud/raw; do
    [ -d "$SRC/$sub" ] || continue
    if ! NEW=$(rsync -r --ignore-existing --exclude='_*' --exclude='.*' --out-format='%n' "$SRC/$sub/" "$DEST/$sub/"); then
        FAILED+=("rsync $sub")
    fi
    while IFS= read -r f; do
        if [ -z "$f" ] || [ -d "$SRC/$sub/$f" ]; then
            continue
        fi
        a=$(sha256sum < "$SRC/$sub/$f" | cut -d' ' -f1)
        b=$(sha256sum < "$DEST/$sub/$f" | cut -d' ' -f1)
        if [ "$a" = "$b" ]; then
            COPIED=$((COPIED + 1))
            log "copied and verified $sub/$f"
        else
            rm -f -- "${DEST:?}/$sub/$f"
            FAILED+=("checksum mismatch on $sub/$f (removed; will retry)")
        fi
    done <<< "$NEW"
done

# 2. Everything else: mirror updates, never delete.
if ! rsync -r --update --exclude='/plaud/audio/' --exclude='/plaud/raw/' --exclude='/plaud/cache/' \
        --exclude='.*' --exclude='_*' "$SRC/" "$DEST/"; then
    FAILED+=("rsync metadata")
fi

# 3. One folders.csv copy per day (the day's last run wins), kept 90 days.
if ! cp "$SRC/folders.csv" "$DEST/folders-history/folders-$(date +%Y%m%d).csv"; then
    FAILED+=("folders.csv snapshot")
fi
find "$DEST/folders-history" -name 'folders-*.csv' -mtime +90 -delete

if [ "${#FAILED[@]}" -gt 0 ]; then
    send_alert "Problems: ${FAILED[*]}"
    exit 1
fi
log "ok: $COPIED new original(s) copied and verified"
