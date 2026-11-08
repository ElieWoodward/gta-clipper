"""Nightly copy of the small things that can't be rebuilt, onto the Work SSD (a different drive from the Mac's).

What's copied (well under 1 GB): the bot's memory (data/) and .env, the media apps' settings (Radarr, Sonarr,
Prowlarr, Seerr, qBittorrent, Gluetun... from the Docker folder, without any media or downloads), Jellyfin's
settings, users and watch history (without its artwork and caches), and AdGuard Home's settings.

Layout on the drive:
  <BACKUP_DIR>/latest/...           the newest full copy
  <BACKUP_DIR>/bot-daily/<date>/    the bot's memory for each of the last 7 days (small; for "undo yesterday")
  <BACKUP_DIR>/RESTORE.txt          how to put it back
"""
import os
import shutil
from datetime import datetime
from pathlib import Path

from .common import DATA, ROOT, log

KEEP_DAYS = 7
COMMON_SKIP = {"cache", "Cache", "logs", "log", "transcodes", "Crash Reports", "UpdateLogs", "Backups", "backups"}
SKIP_DIRS = {   # per source: what's big and rebuildable (media, downloads, artwork, caches)
    "servarr": COMMON_SKIP | {"data", "media", "movies", "tv", "shows", "music", "downloads", "torrents", "MediaCover"},
    "jellyfin": COMMON_SKIP | {"metadata", "keyframes", "attachments", "subtitles", "trickplay", "transcodes"},
}
SKIP_FILES = (".log", ".mkv", ".mp4", ".avi", ".m4v", ".iso", ".part", ".!qB", ".torrent.tmp")
MAX_FILE = 300 * 1024 * 1024   # anything bigger than this isn't a setting

RESTORE = """How to restore (on the Mac, in Terminal, with the bot stopped):

1. Stop the bot:   launchctl bootout gui/$(id -u)/com.wantedlevel.clipbot
2. Bot memory:     cp -R "{d}/latest/bot/data/" ~/projects/gta-clipper/data/   and   cp "{d}/latest/bot/.env" ~/projects/gta-clipper/.env
   (an older day:  cp -R "{d}/bot-daily/YYYY-MM-DD/data/" ~/projects/gta-clipper/data/)
3. Media apps:     cd ~/docker/servarr && docker compose down && cp -R "{d}/latest/servarr/" ~/docker/servarr/ && docker compose up -d
4. Jellyfin:       quit Jellyfin, then  cp -R "{d}/latest/jellyfin/" ~/Library/Application\\ Support/jellyfin/  and open it again
5. AdGuard Home:   sudo cp "{d}/latest/adguard/AdGuardHome.yaml" /Applications/AdGuardHome/ && sudo /Applications/AdGuardHome/AdGuardHome -s restart
6. Start the bot:  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.wantedlevel.clipbot.plist
"""


def dest():
    """Where backups go, or None when the Work SSD isn't plugged in."""
    d = os.environ.get("BACKUP_DIR")
    if d:
        return Path(d)
    work = os.environ.get("CLIPPER_WORK", "")
    if work.startswith("/Volumes/"):
        vol = "/Volumes/" + work.split("/")[2]
        return Path(vol) / "backup" if os.path.ismount(vol) else None
    return None


def sources():
    home = Path.home()
    return {
        "bot/data": DATA,
        "bot/.env": ROOT / ".env",
        "servarr": Path(os.environ.get("SERVARR_DIR", home / "docker" / "servarr")),
        "jellyfin": Path(os.environ.get("JELLYFIN_DIR", home / "Library" / "Application Support" / "jellyfin")),
        "adguard/AdGuardHome.yaml": Path(os.environ.get("ADGUARD_CONF", "/Applications/AdGuardHome/AdGuardHome.yaml")),
    }


def _ignore(skip):
    def ign(folder, names):
        out = []
        for n in names:
            p = Path(folder) / n
            try:
                if p.is_symlink():
                    out.append(n)
                elif p.is_dir():
                    if n in skip:
                        out.append(n)
                elif n.endswith(SKIP_FILES) or p.stat().st_size > MAX_FILE:
                    out.append(n)
            except OSError:
                out.append(n)
        return out
    return ign


def _copy(src, dst, problems, skip=frozenset()):
    try:
        if src.is_dir():
            shutil.copytree(src, dst, ignore=_ignore(skip), dirs_exist_ok=True,
                            copy_function=lambda a, b: _copy_file(a, b, problems))
        elif src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        else:
            return False
        return True
    except shutil.Error as e:   # some files couldn't be read (locked or no permission): keep the rest
        problems.append(f"{src.name}: {len(e.args[0])} file(s) skipped")
        return True
    except OSError as e:
        problems.append(f"{src}: {e.strerror or e}")
        return False


def _copy_file(a, b, problems):
    try:
        shutil.copy2(a, b)
    except OSError as e:
        problems.append(f"{Path(a).name}: {e.strerror or e}")


def _size(p):
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def run(now=None):
    """Make tonight's copy. Returns {"ok", "where", "size_mb", "copied", "missing", "problems"}; never raises."""
    now = now or datetime.now()
    root = dest()
    if root is None:
        return {"ok": False, "where": None, "problems": ["the Work SSD isn't plugged in"], "copied": [], "missing": []}
    try:
        root.mkdir(parents=True, exist_ok=True)
        new, latest, old = root / "latest.new", root / "latest", root / "latest.old"
        shutil.rmtree(new, ignore_errors=True)
        copied, missing, problems = [], [], []
        for name, src in sources().items():
            skip = SKIP_DIRS.get(name.split("/")[0], COMMON_SKIP if name != "bot/data" else set())
            (copied if _copy(Path(src), new / name, problems, skip) else missing).append(name)
        if "bot/data" not in copied:
            raise RuntimeError("couldn't copy the bot's memory")
        shutil.rmtree(old, ignore_errors=True)   # swap in the new copy only once it's complete
        if latest.exists():
            latest.rename(old)
        new.rename(latest)
        shutil.rmtree(old, ignore_errors=True)
        day = root / "bot-daily" / now.date().isoformat()
        shutil.rmtree(day, ignore_errors=True)
        shutil.copytree(latest / "bot", day)
        for d in sorted((root / "bot-daily").iterdir())[:-KEEP_DAYS]:
            shutil.rmtree(d, ignore_errors=True)
        (root / "RESTORE.txt").write_text(RESTORE.format(d=root))
        size = _size(latest) / 1e6
        log.info("backup done: %.0f MB to %s (%s)", size, root, ", ".join(copied))
        return {"ok": True, "where": str(root), "size_mb": round(size), "copied": copied, "missing": missing,
                "problems": problems[:5]}
    except Exception as e:
        log.warning("backup failed: %s", e)
        return {"ok": False, "where": str(root), "problems": [str(e)[:200]], "copied": [], "missing": []}
