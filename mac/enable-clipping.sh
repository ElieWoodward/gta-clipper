#!/bin/bash
# Switch the clipping itself onto the Mac (run once, the day the Work SSD is plugged in, with Claude in the chat).
#   bash mac/enable-clipping.sh
# What it does: checks the Work SSD, installs the video tools and fonts, asks for the Claude and Groq API keys,
# copies the live bot's latest state from GitHub, runs the bot's one-minute self-check, then turns clipping on.
# GitHub's runs are paused separately (Claude does that), so the two never clip at the same time.
set -euo pipefail
cd "$(dirname "$0")/.."
BIN="$HOME/.clipper/bin"
WORK="/Volumes/Work/clipper"

echo "== Moving the clipping onto this Mac =="

# 1. The Work SSD
if [ ! -d /Volumes/Work ]; then
  echo "The Work SSD isn't plugged in (or isn't named Work). Plug it in, wipe it in Disk Utility as 'Work' (APFS), then run this again."
  exit 1
fi
mkdir -p "$WORK"

# 2. ffmpeg + ffprobe (Intel builds), the emoji font and the caption font
mkdir -p "$BIN"
if [ ! -x "$BIN/ffmpeg" ]; then
  echo "Downloading ffmpeg..."
  curl -fsSL -o /tmp/ffmpeg.zip https://evermeet.cx/ffmpeg/getrelease/zip
  curl -fsSL -o /tmp/ffprobe.zip https://evermeet.cx/ffmpeg/getrelease/ffprobe/zip
  unzip -oq /tmp/ffmpeg.zip -d "$BIN" && unzip -oq /tmp/ffprobe.zip -d "$BIN"
  xattr -dr com.apple.quarantine "$BIN" 2>/dev/null || true
fi
"$BIN/ffmpeg" -hide_banner -version | head -1
[ -f fonts/NotoColorEmoji.ttf ] || curl -fsSL -o fonts/NotoColorEmoji.ttf \
  https://github.com/googlefonts/noto-emoji/raw/main/fonts/NotoColorEmoji.ttf
if [ ! -f fonts/DejaVuSans-Bold.ttf ]; then
  curl -fsSL -o /tmp/dejavu.zip https://github.com/dejavu-fonts/dejavu-fonts/releases/download/version_2_37/dejavu-fonts-ttf-2.37.zip
  unzip -ojq /tmp/dejavu.zip "dejavu-fonts-ttf-2.37/ttf/DejaVuSans.ttf" "dejavu-fonts-ttf-2.37/ttf/DejaVuSans-Bold.ttf" -d fonts
fi

# 3. API keys (typed here, saved only in .env on this Mac)
setkey() {   # setkey NAME VALUE: replace or add a line in .env
  grep -v "^$1=" .env > .env.tmp || true
  echo "$1=$2" >> .env.tmp && mv .env.tmp .env && chmod 600 .env
}
if ! grep -q "^ANTHROPIC_API_KEY=sk-" .env; then
  read -r -s -p "Paste the Claude API key (console.anthropic.com > API Keys), then Enter: " K; echo
  setkey ANTHROPIC_API_KEY "$K"
fi
if ! grep -q "^GROQ_API_KEY=gsk_" .env; then
  read -r -s -p "Paste the Groq API key (console.groq.com > API Keys), then Enter: " K; echo
  setkey GROQ_API_KEY "$K"
fi
setkey NOTIFY_VIA telegram
setkey CLIP_STORAGE local
setkey CLIPPER_WORK "$WORK"

# 4. The live bot's latest memory (clips, campaigns, posting times, spend) from GitHub
git fetch -q origin main
for f in $(git ls-tree --name-only origin/main data/); do
  git show "origin/main:$f" > "$f"
done
git update-index --assume-unchanged data/* 2>/dev/null || true
echo "Copied the bot's latest state from GitHub."

# 5. Self-check: Claude, Groq, rendering, Telegram
set -a; . ./.env; set +a
export PATH="$BIN:$PATH"
.venv/bin/python -m clipper preflight

# 6. Turn it on and restart the bot
setkey MAC_RUNS_CLIPPER 1
launchctl kickstart -k "gui/$(id -u)/com.wantedlevel.clipbot"
echo
echo "Clipping now runs on this Mac. Tell Claude so it can pause the GitHub runs."
