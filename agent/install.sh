#!/bin/bash
# DeckShots agent installer for Steam Deck. Run in Desktop Mode (Konsole) as user deck:
#   curl -fsSL https://YOUR-SERVER/shots/install.sh | bash -s -- <DEVICE_TOKEN> [deck-name]
# Re-run without arguments to update the agent and keep the existing config.
# Files made before the install are skipped ("ignore_before" in config.json; set it to 0 to send the backlog).
set -euo pipefail

SERVER="${DECKSHOTS_SERVER:-https://plugins.geekcom.org/shots}"
TOKEN="${1:-}"
NAME="${2:-$(uname -n)}"
BIN="$HOME/.local/bin/deckshots-agent.py"
CFG_DIR="$HOME/.config/deckshots"
UNIT_DIR="$HOME/.config/systemd/user"

mkdir -p "$HOME/.local/bin" "$CFG_DIR" "$UNIT_DIR" "$HOME/.local/share/deckshots"

echo "[deckshots] fetching agent from $SERVER"
curl -fsSL "$SERVER/agent.py" -o "$BIN.new"
python3 -m py_compile "$BIN.new"
mv "$BIN.new" "$BIN"
chmod +x "$BIN"

if [ ! -f "$CFG_DIR/config.json" ]; then
  if [ -z "$TOKEN" ]; then
    echo "[deckshots] first install needs a device token: install.sh <DEVICE_TOKEN> [deck-name]" >&2
    exit 1
  fi
  cat > "$CFG_DIR/config.json" <<EOF
{
  "server": "$SERVER",
  "token": "$TOKEN",
  "name": "$NAME",
  "poll_seconds": 10,
  "delete_after_send": true,
  "watch_screenshots": true,
  "watch_clips": true,
  "ignore_before": $(date +%s),
  "extra_dirs": [
    {"path": "~/Videos/Screencasts", "kind": "video", "recursive": true, "game": "Desktop"},
    {"path": "~/Videos", "kind": "video", "recursive": false, "game": "Desktop"},
    {"path": "~/Pictures/Screenshots", "kind": "screenshot", "recursive": false, "game": "Desktop"}
  ]
}
EOF
  chmod 600 "$CFG_DIR/config.json"
  echo "[deckshots] wrote $CFG_DIR/config.json"
elif [ -n "$TOKEN" ]; then
  python3 - "$CFG_DIR/config.json" "$TOKEN" "$NAME" <<'EOF'
import json, sys
p, token, name = sys.argv[1:]
c = json.load(open(p)); c["token"] = token; c["name"] = name
json.dump(c, open(p, "w"), indent=2)
EOF
  echo "[deckshots] updated token/name in existing config"
fi

cat > "$UNIT_DIR/deckshots.service" <<EOF
[Unit]
Description=DeckShots agent (Steam screenshots/clips -> Telegram)
After=default.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 $BIN
Restart=always
RestartSec=15
Nice=10

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable deckshots.service >/dev/null
systemctl --user restart deckshots.service
loginctl enable-linger "$USER" 2>/dev/null || true

sleep 2
systemctl --user --no-pager --lines=5 status deckshots.service || true
echo "[deckshots] done. Logs: journalctl --user -u deckshots -f"
