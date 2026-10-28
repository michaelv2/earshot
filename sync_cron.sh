#!/usr/bin/env bash
# Cron entry point for sync.py. Cron starts with a bare environment, so this loads the Slack
# webhook from ~/.bashrc_env (as the backup-*.sh jobs do), puts nvm's node on PATH for the
# Plaud CLI, and holds a lock so a slow run never overlaps the next one.
#
# crontab: 0 17 * * * ~/projects/earshot/sync_cron.sh
# log:     ~/.local/share/earshot/sync.log

if [ -f "$HOME/.bashrc_env" ]; then
    source "$HOME/.bashrc_env"
fi
# Machine-specific settings (EARSHOT_*; see README), kept out of the repo.
if [ -f "$HOME/.config/earshot/env" ]; then
    set -a; source "$HOME/.config/earshot/env"; set +a
fi
NODE_BIN=$(ls -d "$HOME"/.nvm/versions/node/*/bin 2>/dev/null | sort -V | tail -1)
export PATH="${NODE_BIN:+$NODE_BIN:}/usr/local/bin:/usr/bin:/bin"

umask 077
STATE_DIR="$HOME/.local/share/earshot"
mkdir -p -m 700 "$STATE_DIR"
exec flock -n "$STATE_DIR/sync.lock" python3 -I "$(dirname "$(readlink -f "$0")")/sync.py" "$@" >> "$STATE_DIR/sync.log" 2>&1
