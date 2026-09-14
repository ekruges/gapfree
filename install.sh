#!/bin/sh
# gapfree installer: drops gapfree.py in ~/.gapfree and keeps it running as a user service.
#   curl -fsSL https://raw.githubusercontent.com/ekruges/gapfree/main/install.sh | sh
set -e
HOME_DIR="${GAPFREE_HOME:-$HOME/.gapfree}"
PORT="${GAPFREE_PORT:-7331}"
RAW="https://raw.githubusercontent.com/ekruges/gapfree/main/gapfree.py"

command -v python3 >/dev/null || { echo "gapfree needs python3 (3.9 or newer)"; exit 1; }
command -v git >/dev/null || { echo "gapfree needs git"; exit 1; }
python3 -c 'import zoneinfo' 2>/dev/null || { echo "python3 is too old (need 3.9+)"; exit 1; }

mkdir -p "$HOME_DIR" && chmod 700 "$HOME_DIR"
if [ -f "$(dirname "$0")/gapfree.py" ]; then
  cp "$(dirname "$0")/gapfree.py" "$HOME_DIR/gapfree.py"
else
  curl -fsSL "$RAW" -o "$HOME_DIR/gapfree.py"
fi
PY="$(command -v python3)"
PATH_LINE="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

case "$(uname -s)" in
Darwin)
  PLIST="$HOME/Library/LaunchAgents/sh.gapfree.plist"
  mkdir -p "$HOME/Library/LaunchAgents"
  cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
 <key>Label</key><string>sh.gapfree</string>
 <key>ProgramArguments</key><array><string>$PY</string><string>$HOME_DIR/gapfree.py</string><string>serve</string></array>
 <key>EnvironmentVariables</key><dict><key>PATH</key><string>$PATH_LINE</string><key>GAPFREE_HOME</key><string>$HOME_DIR</string><key>GAPFREE_PORT</key><string>$PORT</string><key>GAPFREE_SERVICE</key><string>launchd</string></dict>
 <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
 <key>StandardOutPath</key><string>$HOME_DIR/service.log</string><key>StandardErrorPath</key><string>$HOME_DIR/service.log</string>
</dict></plist>
PL
  launchctl bootout "gui/$(id -u)/sh.gapfree" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  ;;
Linux)
  if [ "$(id -u)" = 0 ] && command -v systemctl >/dev/null; then
    # root on a server or container: a system unit, no user session needed
    cat > /etc/systemd/system/gapfree.service <<SV
[Unit]
Description=gapfree
After=network-online.target
Wants=network-online.target
[Service]
ExecStart=$PY $HOME_DIR/gapfree.py serve
Environment=PATH=$PATH_LINE GAPFREE_HOME=$HOME_DIR GAPFREE_PORT=$PORT GAPFREE_SERVICE=systemd GAPFREE_BIND=${GAPFREE_BIND:-127.0.0.1}
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
SV
    systemctl daemon-reload
    systemctl enable --now gapfree
    systemctl restart gapfree
  elif command -v systemctl >/dev/null && systemctl --user show-environment >/dev/null 2>&1; then
    mkdir -p "$HOME/.config/systemd/user"
    cat > "$HOME/.config/systemd/user/gapfree.service" <<SV
[Unit]
Description=gapfree
After=network-online.target
[Service]
ExecStart=$PY $HOME_DIR/gapfree.py serve
Environment=PATH=$PATH_LINE GAPFREE_HOME=$HOME_DIR GAPFREE_PORT=$PORT GAPFREE_SERVICE=systemd
Restart=always
[Install]
WantedBy=default.target
SV
    systemctl --user daemon-reload
    systemctl --user enable --now gapfree
    loginctl enable-linger "$(id -un)" 2>/dev/null || true
  else
    ( crontab -l 2>/dev/null | grep -v gapfree.py; echo "@reboot cd $HOME_DIR && GAPFREE_SERVICE=cron nohup $PY gapfree.py serve >> service.log 2>&1 &" ) | crontab -
    cd "$HOME_DIR" && GAPFREE_SERVICE=cron nohup "$PY" gapfree.py serve >> service.log 2>&1 &
  fi
  ;;
*)
  echo "Unknown OS; run it yourself: python3 $HOME_DIR/gapfree.py serve"
  ;;
esac

URL="http://localhost:$PORT"
echo "gapfree is running at $URL"
echo "Open it and press Publish to create the private activity repo (or pick one you already have)."
echo "If the gh CLI is not logged in on this machine, paste a GitHub token with repo scope under Settings first."
command -v open >/dev/null && open "$URL" 2>/dev/null || command -v xdg-open >/dev/null && xdg-open "$URL" 2>/dev/null || true
