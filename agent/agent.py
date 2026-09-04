#!/usr/bin/env python3
"""DeckShots agent for Steam Deck (stdlib only, survives SteamOS updates).

Watches Steam screenshots, Game Recording clips and extra folders (Spectacle recordings), queues them in SQLite,
uploads to the DeckShots server and deletes the local copy only after the server confirms Telegram delivery.
Config: ~/.config/deckshots/config.json
"""
import hashlib
import http.client
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import tarfile
import time
import urllib.parse
import zlib
from pathlib import Path

AGENT_VERSION = "1.1.0"

HOME = Path.home()
CONFIG_PATH = HOME / ".config/deckshots/config.json"
STATE_DIR = HOME / ".local/share/deckshots"
DB_PATH = STATE_DIR / "queue.db"
TMP_DIR = STATE_DIR / "tmp"
STEAM = HOME / ".local/share/Steam"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
log = logging.getLogger("deckshots")

DEFAULTS = {
    "server": "https://example.com/shots",
    "token": "",
    "name": "deck",
    "poll_seconds": 10,
    "settle_seconds": 8,          # screenshot must be untouched this long before we take it
    "clip_settle_seconds": 20,    # clip dir must be untouched this long
    "ping_seconds": 300,          # heartbeat to the server (feeds /status in the bot)
    "delete_after_send": True,
    "watch_screenshots": True,
    "watch_clips": True,
    "ignore_before": 0,           # unix time; older files are never sent (installer sets it = now, 0 = send backlog)
    # non-Steam sources (Spectacle, OBS...). kind: video | screenshot. Plain files, no tar.
    "extra_dirs": [
        {"path": "~/Videos/Screencasts", "kind": "video", "recursive": True, "game": "Desktop"},
        {"path": "~/Videos", "kind": "video", "recursive": False, "game": "Desktop"},
        {"path": "~/Pictures/Screenshots", "kind": "screenshot", "recursive": False, "game": "Desktop"},
    ],
}
VIDEO_EXT = (".mp4", ".m4v", ".mov", ".webm", ".mkv")
IMAGE_EXT = (".jpg", ".jpeg", ".png")


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    except FileNotFoundError:
        log.error("no config at %s", CONFIG_PATH)
        sys.exit(1)
    if not cfg["token"]:
        log.error("config has no token")
        sys.exit(1)
    cfg["server"] = cfg["server"].rstrip("/")
    return cfg


# ----------------------------------------------------------------------------- queue
def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    with db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS queue(
                path TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                appid TEXT,
                taken_at TEXT,
                game TEXT,
                state TEXT NOT NULL,          -- new | uploaded | done
                upload_path TEXT,             -- file actually uploaded (tar for clips)
                sha256 TEXT,
                server_id TEXT,
                attempts INTEGER DEFAULT 0,
                next_try REAL DEFAULT 0,
                error TEXT,
                created REAL
            );
            CREATE TABLE IF NOT EXISTS names(appid TEXT PRIMARY KEY, name TEXT, seen REAL);
            """
        )
        cols = [r[1] for r in con.execute("PRAGMA table_info(queue)")]
        if "game" not in cols:
            con.execute("ALTER TABLE queue ADD COLUMN game TEXT")


# ----------------------------------------------------------------------------- steam metadata
def parse_binary_vdf(data: bytes) -> dict:
    pos = 0

    def read_str():
        nonlocal pos
        end = data.index(b"\x00", pos)
        s = data[pos:end].decode("utf-8", "replace")
        pos = end + 1
        return s

    def read_map():
        nonlocal pos
        m = {}
        while pos < len(data):
            t = data[pos]
            pos += 1
            if t == 0x08:
                return m
            key = read_str()
            if t == 0x00:
                m[key] = read_map()
            elif t == 0x01:
                m[key] = read_str()
            elif t == 0x02:
                m[key] = int.from_bytes(data[pos:pos + 4], "little", signed=True)
                pos += 4
            elif t == 0x07:
                m[key] = int.from_bytes(data[pos:pos + 8], "little", signed=False)
                pos += 8
            else:
                raise ValueError(f"unknown vdf type {t:#x} at {pos}")
        return m

    return read_map()


def _ci(d: dict, key: str):
    for k, v in d.items():
        if k.lower() == key.lower():
            return v
    return None


class GameNames:
    """appid -> name from shortcuts.vdf (non-Steam), appmanifest_*.acf (Steam) and a persistent cache
    (so a clip of a shortcut you deleted last month still gets its name)."""

    def __init__(self):
        self.shortcuts: dict[str, str] = {}
        self.shortcuts_mtime: dict[Path, float] = {}
        self.manifests: dict[str, str] = {}
        self.manifests_scanned = 0.0

    def refresh_shortcuts(self):
        for vdf in STEAM.glob("userdata/*/config/shortcuts.vdf"):
            try:
                mtime = vdf.stat().st_mtime
            except OSError:
                continue
            if self.shortcuts_mtime.get(vdf) == mtime:
                continue
            try:
                root = parse_binary_vdf(vdf.read_bytes())
                entries = _ci(root, "shortcuts") or {}
                learned = {}
                for entry in entries.values():
                    name = _ci(entry, "AppName") or ""
                    appid = _ci(entry, "appid")
                    if appid is None:
                        exe = _ci(entry, "Exe") or ""
                        appid = zlib.crc32((exe + name).encode("utf-8")) | 0x80000000
                    if name:
                        learned[str(appid & 0xFFFFFFFF)] = name
                self.shortcuts.update(learned)
                self.shortcuts_mtime[vdf] = mtime
                with db() as con:
                    con.executemany("INSERT OR REPLACE INTO names(appid,name,seen) VALUES(?,?,?)",
                                    [(a, n, time.time()) for a, n in learned.items()])
                log.info("loaded %d non-Steam shortcuts from %s", len(entries), vdf)
            except Exception as e:  # noqa: BLE001
                log.warning("cannot parse %s: %s", vdf, e)

    def refresh_manifests(self):
        if time.time() - self.manifests_scanned < 300:
            return
        dirs = [STEAM / "steamapps"]
        lf = STEAM / "steamapps/libraryfolders.vdf"
        if lf.exists():
            for p in re.findall(r'"path"\s+"([^"]+)"', lf.read_text(errors="replace")):
                dirs.append(Path(p.replace("\\\\", "\\")) / "steamapps")
        dirs += [Path(p) for p in Path("/run/media").glob("*/*/steamapps")] + [Path(p) for p in Path("/run/media").glob("*/steamapps")]
        learned = {}
        for d in dirs:
            for acf in d.glob("appmanifest_*.acf"):
                appid = acf.stem.split("_", 1)[1]
                if appid in self.manifests:
                    continue
                try:
                    m = re.search(r'"name"\s+"(.*)"', acf.read_text(errors="replace"))
                    if m:
                        self.manifests[appid] = learned[appid] = m.group(1)
                except OSError:
                    pass
        if learned:
            with db() as con:
                con.executemany("INSERT OR REPLACE INTO names(appid,name,seen) VALUES(?,?,?)",
                                [(a, n, time.time()) for a, n in learned.items()])
        self.manifests_scanned = time.time()

    def name(self, appid: str) -> str:
        self.refresh_shortcuts()
        self.refresh_manifests()
        found = self.shortcuts.get(appid) or self.manifests.get(appid)
        if found:
            return found
        with db() as con:
            row = con.execute("SELECT name FROM names WHERE appid=?", (appid,)).fetchone()
        return row["name"] if row else ""


# ----------------------------------------------------------------------------- scanning
def userdata_dirs():
    for d in STEAM.glob("userdata/*"):
        if d.name != "0" and d.is_dir() and ((d / "760").exists() or (d / "gamerecordings").exists()):
            yield d


def newest_mtime(path: Path) -> float:
    latest = path.stat().st_mtime
    for p in path.rglob("*"):
        try:
            latest = max(latest, p.stat().st_mtime)
        except OSError:
            pass
    return latest


def norm_appid(appid: str) -> str:
    """Steam uses 64-bit gameids for non-Steam shortcuts in some paths: appid = gameid >> 32."""
    try:
        n = int(appid)
    except ValueError:
        return appid
    if n >= 2 ** 32:
        n >>= 32
    return str(n)


def taken_from_name(name: str) -> str:
    m = re.search(r"(\d{4})(\d{2})(\d{2})_?(\d{2})(\d{2})(\d{2})", name)
    if m:
        y, mo, d, h, mi, s = m.groups()
        return f"{y}-{mo}-{d} {h}:{mi}:{s}"
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})[_ T](\d{2})[-:](\d{2})[-:](\d{2})", name)
    if m:
        y, mo, d, h, mi, s = m.groups()
        return f"{y}-{mo}-{d} {h}:{mi}:{s}"
    return ""


def scan(cfg: dict):
    now = time.time()
    found = []
    for ud in userdata_dirs():
        if cfg["watch_screenshots"]:
            for shot in (ud / "760/remote").glob("*/screenshots/*"):
                if shot.suffix.lower() not in IMAGE_EXT or not shot.is_file():
                    continue
                try:
                    st = shot.stat()
                except OSError:
                    continue
                if now - st.st_mtime < cfg["settle_seconds"] or st.st_size == 0 or st.st_mtime < cfg["ignore_before"]:
                    continue
                found.append((str(shot), "screenshot", norm_appid(shot.parent.parent.name), taken_from_name(shot.name), ""))
        if cfg["watch_clips"]:
            for clip in (ud / "gamerecordings/clips").glob("*"):
                if not clip.is_dir():
                    continue
                try:
                    latest = newest_mtime(clip)
                except OSError:
                    continue
                if now - latest < cfg["clip_settle_seconds"] or latest < cfg["ignore_before"]:
                    continue
                if not any(clip.rglob("*.m4s")) and not any(clip.rglob("*.mp4")):
                    continue
                m = re.match(r"clip_(\d+)_", clip.name)
                appid = norm_appid(m.group(1)) if m else ""
                found.append((str(clip), "clip", appid, taken_from_name(clip.name), ""))
    for extra in cfg.get("extra_dirs", []):
        base = Path(os.path.expanduser(extra["path"]))
        if not base.is_dir():
            continue
        kind = extra.get("kind", "video")
        exts = VIDEO_EXT if kind == "video" else IMAGE_EXT
        it = base.rglob("*") if extra.get("recursive") else base.glob("*")
        for f in it:
            if not f.is_file() or f.suffix.lower() not in exts or f.name.startswith("."):
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            if now - st.st_mtime < max(cfg["settle_seconds"], 15) or st.st_size == 0 or st.st_mtime < cfg["ignore_before"]:
                continue
            taken = taken_from_name(f.name) or time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))
            found.append((str(f), kind, "", taken, extra.get("game", "Desktop")))
    if not found:
        return
    with db() as con:
        for path, kind, appid, taken, game in found:
            cur = con.execute(
                "INSERT OR IGNORE INTO queue(path,kind,appid,taken_at,game,state,created) VALUES(?,?,?,?,?,'new',?)",
                (path, kind, appid, taken, game, now),
            )
            if cur.rowcount:
                log.info("queued %s %s (appid %s)", kind, path, appid)


# ----------------------------------------------------------------------------- upload
def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def api_call(cfg: dict, method: str, path: str, headers: dict | None = None, body=None, timeout=60):
    u = urllib.parse.urlsplit(cfg["server"])
    conn_cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(u.hostname, u.port, timeout=timeout)
    hdrs = {"X-Device-Token": cfg["token"], "User-Agent": f"deckshots-agent/{AGENT_VERSION}"}
    hdrs.update(headers or {})
    try:
        conn.request(method, u.path + path, body=body, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        try:
            payload = json.loads(data.decode("utf-8")) if data else {}
        except ValueError:
            payload = {"raw": data[:200].decode("utf-8", "replace")}
        return resp.status, payload
    finally:
        conn.close()


def prepare_upload(row: sqlite3.Row) -> Path:
    src = Path(row["path"])
    if row["kind"] != "clip":
        return src
    tar_path = TMP_DIR / (src.name + ".tar")
    if row["upload_path"] and Path(row["upload_path"]).exists():
        return Path(row["upload_path"])
    tmp = tar_path.with_suffix(".tar.part")
    with tarfile.open(tmp, "w") as tf:
        tf.add(src, arcname=src.name)
    os.replace(tmp, tar_path)
    return tar_path


def upload_one(cfg: dict, names: GameNames, row: sqlite3.Row):
    src = Path(row["path"])
    if not src.exists():
        log.warning("vanished before upload, dropping: %s", src)
        with db() as con:
            con.execute("DELETE FROM queue WHERE path=?", (row["path"],))
        return
    up = prepare_upload(row)
    sha = row["sha256"] or sha256_of(up)
    game = row["game"] or (names.name(row["appid"]) if row["appid"] else "")
    headers = {
        "Content-Type": "application/octet-stream",
        "Content-Length": str(up.stat().st_size),
        "X-Sha256": sha,
        "X-Kind": row["kind"],
        "X-Filename": urllib.parse.quote(up.name),
        "X-Appid": row["appid"] or "",
        "X-Game": urllib.parse.quote(game),
        "X-Taken-At": row["taken_at"] or "",
    }
    with open(up, "rb") as fh:
        status, payload = api_call(cfg, "PUT", "/upload", headers, fh, timeout=1800)
    if status == 200 and payload.get("id"):
        with db() as con:
            con.execute(
                "UPDATE queue SET state='uploaded', upload_path=?, sha256=?, server_id=?, error=NULL, attempts=0, next_try=0 WHERE path=?",
                (str(up), sha, payload["id"], row["path"]),
            )
        log.info("uploaded %s -> %s (%s)", src.name, payload["id"], game or row["appid"])
    else:
        raise RuntimeError(f"upload HTTP {status}: {payload}")


def check_one(cfg: dict, row: sqlite3.Row):
    status, payload = api_call(cfg, "GET", f"/status/{row['server_id']}")
    if status == 404:
        log.warning("server forgot %s, re-uploading", row["server_id"])
        with db() as con:
            con.execute("UPDATE queue SET state='new', server_id=NULL WHERE path=?", (row["path"],))
        return
    if status != 200:
        raise RuntimeError(f"status HTTP {status}: {payload}")
    st = payload.get("status")
    if st == "sent":
        finish(cfg, row)
    elif st == "failed":
        log.warning("server retrying %s: %s", row["server_id"], payload.get("error"))
        with db() as con:  # poll less often while the server retries
            con.execute("UPDATE queue SET next_try=? WHERE path=?", (time.time() + 60, row["path"]))


def finish(cfg: dict, row: sqlite3.Row):
    src = Path(row["path"])
    if cfg["delete_after_send"]:
        try:
            if src.is_dir():
                shutil.rmtree(src, ignore_errors=True)
            else:
                src.unlink(missing_ok=True)
                thumb = src.parent.parent / "thumbnails" / src.name
                thumb.unlink(missing_ok=True)
        except OSError as e:
            log.warning("cannot delete %s: %s", src, e)
    if row["upload_path"] and row["upload_path"] != row["path"]:
        Path(row["upload_path"]).unlink(missing_ok=True)
    with db() as con:
        con.execute("UPDATE queue SET state='done', error=NULL WHERE path=?", (row["path"],))
    log.info("delivered, local copy %s: %s", "removed" if cfg["delete_after_send"] else "kept", src.name)


def backoff(row: sqlite3.Row, err: Exception):
    attempts = (row["attempts"] or 0) + 1
    delay = min(300, 10 * 2 ** min(attempts, 6))
    with db() as con:
        con.execute("UPDATE queue SET attempts=?, next_try=?, error=? WHERE path=?",
                    (attempts, time.time() + delay, str(err)[:300], row["path"]))
    log.warning("%s (attempt %d, retry in %ds): %s", Path(row["path"]).name, attempts, delay, err)


def pump(cfg: dict, names: GameNames):
    now = time.time()
    with db() as con:
        rows = con.execute(
            "SELECT * FROM queue WHERE state IN ('new','uploaded') AND next_try <= ? ORDER BY created", (now,)
        ).fetchall()
    for row in rows:
        try:
            if row["state"] == "new":
                upload_one(cfg, names, row)
            else:
                check_one(cfg, row)
        except Exception as e:  # noqa: BLE001
            backoff(row, e)
            if isinstance(e, (OSError, http.client.HTTPException)):
                break  # network is probably down: stop hammering the rest of the queue


def ping(cfg: dict):
    with db() as con:
        q_new = con.execute("SELECT COUNT(*) FROM queue WHERE state='new'").fetchone()[0]
        q_up = con.execute("SELECT COUNT(*) FROM queue WHERE state='uploaded'").fetchone()[0]
    try:
        api_call(cfg, "POST", "/ping", {"X-Host": os.uname().nodename, "X-Agent-Version": AGENT_VERSION,
                                        "X-Queue-New": str(q_new), "X-Queue-Uploaded": str(q_up)}, timeout=15)
    except Exception as e:  # noqa: BLE001
        log.debug("ping failed: %s", e)


def main():
    cfg = load_config()
    init_db()
    names = GameNames()
    log.info("deckshots agent %s '%s' -> %s (steam: %s)", AGENT_VERSION, cfg["name"], cfg["server"], STEAM)
    last_ping = 0.0
    while True:
        try:
            scan(cfg)
            pump(cfg, names)
            if time.time() - last_ping >= cfg["ping_seconds"]:
                ping(cfg)
                last_ping = time.time()
        except Exception as e:  # noqa: BLE001
            log.exception("loop error: %s", e)
        time.sleep(cfg["poll_seconds"])


if __name__ == "__main__":
    main()
