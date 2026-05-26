#!/usr/bin/env bash
# Install the Hilton monitor as a launchd job (runs every 2 hours).
#
# Usage:
#   ./install_launchd.sh
#
# This writes a populated plist to ~/Library/LaunchAgents/ and loads it.
# To uninstall later, run: ./install_launchd.sh uninstall

set -euo pipefail

LABEL="com.cswilliams.hilton-monitor"
WORKDIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_SRC="$WORKDIR/$LABEL.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
VENV_PY="$WORKDIR/.venv/bin/python"
SCRIPT="$WORKDIR/monitor.py"

if [[ "${1:-}" == "uninstall" ]]; then
  echo "Unloading launchd job..."
  launchctl unload "$PLIST_DEST" 2>/dev/null || true
  rm -f "$PLIST_DEST"
  echo "Uninstalled. (Source plist in repo is left in place.)"
  exit 0
fi

# Sanity checks
[[ -f "$PLIST_SRC" ]] || { echo "Missing $PLIST_SRC"; exit 1; }
[[ -x "$VENV_PY"   ]] || { echo "Missing venv: run 'python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/patchright install chromium' first"; exit 1; }
[[ -f "$SCRIPT"    ]] || { echo "Missing $SCRIPT"; exit 1; }

NTFY_TOPIC="${NTFY_TOPIC:-}"
if [[ -z "$NTFY_TOPIC" ]]; then
  read -r -p "Enter NTFY topic (e.g. hilton-lxtswhx-x7n4q2k8m): " NTFY_TOPIC
fi
[[ -n "$NTFY_TOPIC" ]] || { echo "NTFY_TOPIC is required"; exit 1; }

mkdir -p "$WORKDIR/logs"
mkdir -p "$(dirname "$PLIST_DEST")"

# Substitute placeholders.  Using | as sed delimiter so paths with / don't break it.
sed \
  -e "s|__VENV_PY__|$VENV_PY|g" \
  -e "s|__SCRIPT__|$SCRIPT|g" \
  -e "s|__WORKDIR__|$WORKDIR|g" \
  -e "s|__NTFY_TOPIC__|$NTFY_TOPIC|g" \
  "$PLIST_SRC" > "$PLIST_DEST"

# Reload (unload first in case it was previously installed)
launchctl unload "$PLIST_DEST" 2>/dev/null || true
launchctl load "$PLIST_DEST"

echo
echo "Installed: $PLIST_DEST"
echo "Logs:      $WORKDIR/logs/monitor.{out,err}.log"
echo
echo "Useful commands:"
echo "  Run now:          launchctl kickstart -k gui/\$(id -u)/$LABEL"
echo "  Check status:     launchctl list | grep hilton"
echo "  Tail logs:        tail -f $WORKDIR/logs/monitor.out.log"
echo "  Uninstall:        $0 uninstall"
