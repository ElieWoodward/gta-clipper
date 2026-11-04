#!/bin/bash
# Mac setup for the clip bot's Mac side (Telegram + Content Rewards checks).
# Run from the repo folder:  bash mac/setup.sh
# Safe to run again: it only does what's missing, and restarts the bot at the end.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$(pwd)"
PLIST="$HOME/Library/LaunchAgents/com.wantedlevel.clipbot.plist"
LOGS="$HOME/Library/Logs/clipbot"

echo "== Clip bot Mac setup =="

# 1. Python 3.10+ (the python.org installer puts it in /usr/local/bin)
PY="$(command -v python3.13 || command -v python3.12 || command -v python3)"
"$PY" -c 'import sys; assert sys.version_info >= (3, 10), sys.version' || {
  echo "Python 3.10 or newer is needed. Install it from python.org (macOS 64-bit universal2 installer)."; exit 1; }

# 2. Its own Python environment, with the bot's packages and Playwright (it drives Chrome)
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt playwright

# 3. Google Chrome (Playwright uses the real Chrome app, not a separate browser)
[ -d "/Applications/Google Chrome.app" ] || { echo "Install Google Chrome into Applications first."; exit 1; }

# 4. Secrets file (never committed)
if [ ! -f .env ]; then
  cat > .env <<'EOF'
# Telegram: token from @BotFather, your id from @userinfobot
TELEGRAM_TOKEN=paste-token-here
TELEGRAM_CHAT_ID=paste-your-id-here
EOF
  chmod 600 .env
fi
if grep -q "paste-" .env; then
  echo
  echo "Next: put your Telegram token and id into the .env file. Opening it now..."
  open -e .env
  echo "Save it, close TextEdit, then run this script again:  bash mac/setup.sh"
  exit 0
fi

# 5. Telegram test
.venv/bin/python -m clipper tg_test

# 6. Content Rewards login for the bot's own Chrome window (once)
if [ ! -d "$HOME/.clipper/chrome-profile/Default" ]; then
  echo
  echo "A Chrome window will open. Log in to Content Rewards there (this is the bot's own window)."
  .venv/bin/python -m clipper cr_login
fi

# 7. Start at login and keep running
mkdir -p "$LOGS" "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.wantedlevel.clipbot</string>
  <key>ProgramArguments</key><array>
    <string>$REPO/.venv/bin/python</string><string>-m</string><string>clipper</string><string>mac</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/usr/local/bin:/opt/local/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>60</integer>
  <key>StandardOutPath</key><string>$LOGS/bot.log</string>
  <key>StandardErrorPath</key><string>$LOGS/bot.log</string>
</dict></plist>
EOF
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo
echo "Done. The bot is running and will message you on Telegram. Log: $LOGS/bot.log"
