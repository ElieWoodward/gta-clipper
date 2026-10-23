#!/usr/bin/env bash
# Install ffmpeg and the caption fonts. The .deb files are kept in GitHub's cache (~/apt-debs, restored by the
# workflow), so most runs never touch Ubuntu's mirror. When they do, a slow or broken mirror gets a timeout and
# the next try uses another mirror.
set -u
PKGS="ffmpeg fonts-dejavu-core fonts-noto-color-emoji"
DEBS="$HOME/apt-debs"
ready() { command -v ffmpeg >/dev/null && command -v ffprobe >/dev/null && fc-list | grep -qi NotoColorEmoji; }
mkdir -p "$DEBS"
if ls "$DEBS"/*.deb >/dev/null 2>&1; then
  sudo dpkg -i "$DEBS"/*.deb >/dev/null 2>&1 && sudo fc-cache -f >/dev/null 2>&1
  if ready; then echo "ffmpeg and fonts installed from the cache"; exit 0; fi
  echo "cached packages didn't install cleanly; downloading fresh"; rm -f "$DEBS"/*.deb
fi
for i in 1 2 3 4; do
  if [ "$i" -ge 2 ]; then   # the default (Azure) mirror is the one that hangs: try the main Ubuntu servers
    mirror=$([ "$i" -eq 2 ] && echo "http://archive.ubuntu.com" || echo "http://us.archive.ubuntu.com")
    sudo sed -i -E "s#https?://[a-z.]*archive\.ubuntu\.com#$mirror#g" /etc/apt/sources.list.d/*.sources /etc/apt/sources.list 2>/dev/null
  fi
  if timeout 180 sudo apt-get update -qq -o Acquire::Retries=2 -o Acquire::http::Timeout=30 \
     && timeout 300 sudo apt-get install -y -qq --no-install-recommends -o Acquire::http::Timeout=30 \
          -o Dir::Cache::archives="$DEBS" $PKGS; then
    sudo chown -R "$(id -u)" "$DEBS"; rm -rf "$DEBS/partial" "$DEBS/lock"
    if ready; then echo "ffmpeg and fonts installed (try $i)"; exit 0; fi
  fi
  echo "apt was slow or failed (try $i), retrying"; sleep 5
done
echo "::error::couldn't install ffmpeg (Ubuntu's package servers didn't answer)"
exit 1
