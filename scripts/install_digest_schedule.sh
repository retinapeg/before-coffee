#!/bin/bash
# Install (or remove) the 07:00 job digest as a user LaunchAgent.
#
#   scripts/install_digest_schedule.sh            install and start
#   scripts/install_digest_schedule.sh --remove   unload and delete
#
# Paths are resolved here rather than written into the repository, so nothing
# identifying the machine or its owner is ever committed.
#
# This schedules ONE command: python -m careerops.digest_cli, which composes one
# digest and emails it to the mailbox that owns the OAuth token. It does not start
# the legacy submission, email or browser workers, and it never loads
# com.jobs2026.runner or com.jobs2026.browser-worker.
set -euo pipefail

LABEL="com.careerops.digest"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOUR="${DIGEST_HOUR:-7}"
MINUTE="${DIGEST_MINUTE:-0}"

if [ "${1:-}" = "--remove" ]; then
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Removed $LABEL."
    exit 0
fi

# This repo is usually checked out as a worktree, whose .venv lives in the main
# checkout. git tells us where that is without hard-coding anyone's paths.
PYTHON="${DIGEST_PYTHON:-}"
if [ -z "$PYTHON" ] || [ ! -x "$PYTHON" ]; then
    for candidate in \
        "$REPO/.venv/bin/python" \
        "$(cd "$(git -C "$REPO" rev-parse --git-common-dir 2>/dev/null)/.." 2>/dev/null && pwd)/.venv/bin/python"
    do
        if [ -n "$candidate" ] && [ -x "$candidate" ]; then PYTHON="$candidate"; break; fi
    done
fi
[ -n "${PYTHON:-}" ] && [ -x "$PYTHON" ] || {
    echo "No interpreter found; set DIGEST_PYTHON=/path/to/python." >&2; exit 1; }

mkdir -p "$HOME/Library/LaunchAgents" "$REPO/local_data/logs" "$REPO/local_data/digests"

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>-m</string><string>careerops.digest_cli</string>
        <string>--send</string>
        <string>--data</string><string>$REPO/local_data/careerops.sqlite3</string>
        <string>--save</string><string>$REPO/local_data/digests</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict><key>PYTHONPATH</key><string>$REPO/src</string></dict>
    <key>WorkingDirectory</key><string>$REPO</string>
    <key>StartCalendarInterval</key>
    <dict><key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>$MINUTE</integer></dict>
    <!-- If the machine was asleep at the scheduled time, run once on wake rather
         than skipping the morning entirely. -->
    <key>RunAtLoad</key><false/>
    <key>StandardOutPath</key><string>$REPO/local_data/logs/digest.log</string>
    <key>StandardErrorPath</key><string>$REPO/local_data/logs/digest.err</string>
    <!-- Standard, not Background: launchd throttles CPU and I/O for Background jobs,
         which stretched this from about 12 seconds to nearly two minutes. It is a
         short task that should finish promptly at 07:00. -->
    <key>ProcessType</key><string>Standard</string>
    <key>LowPriorityIO</key><false/>
</dict>
</plist>
PLISTEOF

chmod 600 "$PLIST"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl load "$PLIST"
echo "Installed $LABEL for $(printf '%02d:%02d' "$HOUR" "$MINUTE") daily."
echo "Remove with: scripts/install_digest_schedule.sh --remove"
