#!/bin/bash
# Install job-watcher as a macOS launchd agent that runs every 30 minutes while the Mac is awake
# (missed runs during sleep fire once on wake). Usage, from the job-watcher folder:
#   bash mac/install.sh            # install / update
#   bash mac/install.sh uninstall
set -euo pipefail
LABEL="com.shubh.jobwatcher"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
ENVFILE="$HOME/.config/job-watcher/env"
PY="$(command -v python3)"

if [[ "${1:-}" == "uninstall" ]]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"; echo "uninstalled"; exit 0
fi

"$PY" -c 'import sys, tomllib; assert sys.version_info >= (3, 11)' 2>/dev/null || {
  echo "Need Python 3.11+ (found: $("$PY" --version 2>&1)). Install with: brew install python"; exit 1; }

mkdir -p "$(dirname "$ENVFILE")" "$DIR/logs" "$HOME/Library/LaunchAgents"
if [[ ! -f "$ENVFILE" ]]; then
  read -r -p "Gmail address to send from: " U
  read -r -s -p "Gmail app password (16 chars, input hidden): " P; echo
  printf 'SMTP_USER=%q\nSMTP_PASSWORD=%q\nALERT_TO=%q\n' "$U" "$P" "mall.shubhkumar@gmail.com" > "$ENVFILE"
  chmod 600 "$ENVFILE"
fi

cat > "$DIR/mac/run.sh" <<EOF
#!/bin/bash
set -a; source "$ENVFILE"; set +a
cd "$DIR"
exec "$PY" watcher.py --mode run
EOF
chmod +x "$DIR/mac/run.sh"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$DIR/mac/run.sh</string></array>
  <key>StartInterval</key><integer>1800</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$DIR/logs/watcher.log</string>
  <key>StandardErrorPath</key><string>$DIR/logs/watcher.log</string>
  <key>ProcessType</key><string>Background</string>
</dict></plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Installed. Runs every 30 min while awake. Log: $DIR/logs/watcher.log"
echo "Run now:   launchctl kickstart gui/$(id -u)/$LABEL"
