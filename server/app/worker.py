"""Delivery workers.

Two lanes: "fast" (screenshots, Steam clips, mp4 recordings — seconds each) and "slow" (recordings that need
transcoding — minutes each). Several workers per lane; devices are served round-robin inside a lane so a deck
with a big backlog never starves the others. Screenshots of one game taken within ALBUM_WAIT go as one album.
"""
import asyncio
import logging
import shutil
import time
from pathlib import Path

import httpx

from . import config as C
from . import media, store
from .tg import Bot, TelegramError

log = logging.getLogger("deckshots.worker")

bot = Bot(C.API, C.BOT_TOKEN, C.LOCAL_FILES)
SERVED_AT: dict[str, float] = {}
_lock = asyncio.Lock()


def inbox_path(shot_id: str, filename: str) -> Path:
    return C.INBOX / f"{shot_id}_{filename}"


# ----------------------------------------------------------------------------- game names
def resolve_name(appid: str, game: str) -> str:
    if game:
        return game
    if not appid:
        return "Unknown game"
    try:
        n = int(appid)
    except ValueError:
        return f"appid {appid}"
    if n >= 2 ** 31 or not C.STORE_LOOKUP:      # non-Steam shortcut: only the deck knows the name
        return f"appid {appid}"
    row = store.get_name(appid)
    if row and row["name"]:
        return row["name"]
    if row and time.time() - (row["checked"] or 0) < 86400:
        return f"appid {appid}"
    name = ""
    try:
        r = httpx.get("https://store.steampowered.com/api/appdetails",
                      params={"appids": appid, "filters": "basic"}, timeout=10)
        data = r.json().get(appid, {})
        if data.get("success"):
            name = data["data"].get("name", "")
    except Exception as e:  # noqa: BLE001
        log.warning("store lookup for %s failed: %s", appid, e)
    store.put_name(appid, name)
    return name or f"appid {appid}"


# ----------------------------------------------------------------------------- picking work
def pick(lane: str, now: float) -> list | None:
    """Return a batch of rows to deliver together (1 item, or up to ALBUM_MAX screenshots of one game)."""
    rows = store.pending_candidates(lane, now)
    if not rows:
        return None
    devices = sorted({r["device"] for r in rows}, key=lambda d: SERVED_AT.get(d, 0.0))
    for device in devices:
        for row in (r for r in rows if r["device"] == device):
            if row["kind"] != "screenshot" or C.ALBUM_MAX <= 1:
                return [row]
            group = store.group_pending(device, row["appid"] or "", "screenshot", now, C.ALBUM_MAX)
            newest = max(g["created"] for g in group)
            if len(group) >= C.ALBUM_MAX or now - newest >= C.ALBUM_WAIT or (row["attempts"] or 0) > 0:
                return list(group)
            # album still collecting: try the next game/device
    return None


# ----------------------------------------------------------------------------- sending
def caption_for(dev: dict, rows: list, icon: str, extra: str = "") -> str:
    game = resolve_name(rows[0]["appid"] or "", rows[0]["game"] or "")
    label = dev.get("label") or dev["name"]
    takens = [r["taken_at"] for r in rows if r["taken_at"]]
    when = ""
    if takens:
        when = f" · {takens[0]}" if len(takens) == 1 or takens[0] == takens[-1] else f" · {takens[0]} → {takens[-1]}"
    count = f" · {len(rows)} шт." if len(rows) > 1 else ""
    return f"🎮 {game}\n{icon} {label}{when}{count}{extra}"


def deliver(rows: list) -> list[int]:
    dev = C.BY_NAME.get(rows[0]["device"])
    if not dev or not dev.get("chat_id"):
        raise RuntimeError(f"device {rows[0]['device']} has no chat_id configured")
    chat, thread = dev["chat_id"], dev.get("thread_id")
    base = {"chat_id": chat}
    if thread:
        base["message_thread_id"] = thread
    kind = rows[0]["kind"]

    if kind == "screenshot":
        paths = [inbox_path(r["id"], r["filename"]) for r in rows]
        for p in paths:
            if not p.exists():
                raise FileNotFoundError(f"inbox file missing: {p}")
        as_doc = C.SCREENSHOT_AS != "photo" or any(p.stat().st_size > C.PHOTO_LIMIT for p in paths)
        caption = caption_for(dev, rows, "📸")
        if len(rows) == 1:
            method, field = ("sendDocument", "document") if as_doc else ("sendPhoto", "photo")
            res = bot.send_file(method, field, paths[0], {**base, "caption": caption})
            return [res["message_id"]]
        items = [("document" if as_doc else "photo", p, caption if i == 0 else "") for i, p in enumerate(paths)]
        res = bot.send_media_group(chat, items, thread)
        return [m["message_id"] for m in res]

    row = rows[0]
    src = inbox_path(row["id"], row["filename"])
    if not src.exists():
        raise FileNotFoundError(f"inbox file missing: {src}")
    workdir = C.WORK / row["id"]
    workdir.mkdir(parents=True, exist_ok=True)
    thumb = None
    if kind == "clip":
        video = media.remux_clip(src, workdir)
        thumb = media.clip_thumbnail(workdir)
        icon = "🎬"
    else:
        video = media.normalize_video(src, workdir, C.TRANSCODE_PRESET)
        icon = "🎥"
    parts = media.split_if_needed(video, workdir, C.MAX_PART)
    ids = []
    for i, part in enumerate(parts, 1):
        extra = f" · {i}/{len(parts)}" if len(parts) > 1 else ""
        payload = {**base, "caption": caption_for(dev, [row], icon, extra), "supports_streaming": True}
        dur = media.probe_duration(part)
        if dur:
            payload["duration"] = int(dur)
        if thumb and i == 1 and C.LOCAL_FILES:
            payload["thumbnail"] = f"file://{thumb}"
        res = bot.send_file("sendVideo", "video", part, payload)
        ids.append(res["message_id"])
        if len(parts) > 1:
            time.sleep(1)
    return ids


def cleanup(rows: list):
    for r in rows:
        inbox_path(r["id"], r["filename"]).unlink(missing_ok=True)
        shutil.rmtree(C.WORK / r["id"], ignore_errors=True)


def process_one(lane: str) -> bool:
    now = time.time()
    rows = pick(lane, now)
    if not rows:
        return False
    ids = [r["id"] for r in rows]
    if not store.claim(ids):
        return True
    SERVED_AT[rows[0]["device"]] = now
    try:
        msgs = deliver(rows)
        store.mark_sent(ids, msgs)
        cleanup(rows)
        log.info("[%s] sent %s (%s x%d, %s) -> %s", lane, rows[0]["device"], rows[0]["kind"], len(rows), rows[0]["filename"], msgs)
    except TelegramError as e:
        delay = store.mark_failed(ids, str(e), C.RETRY_MAX if e.code != 429 else 120)
        for r in rows:
            shutil.rmtree(C.WORK / r["id"], ignore_errors=True)
        log.error("[%s] telegram refused %s: %s (retry in %ds)", lane, ids, e, delay)
    except Exception as e:  # noqa: BLE001
        delay = store.mark_failed(ids, str(e), C.RETRY_MAX)
        for r in rows:
            shutil.rmtree(C.WORK / r["id"], ignore_errors=True)
        log.error("[%s] failed %s: %s (retry in %ds)", lane, ids, e, delay)
    return True


async def worker(lane: str, n: int):
    log.info("worker %s#%d started", lane, n)
    while True:
        try:
            busy = await asyncio.to_thread(process_one, lane)
        except Exception as e:  # noqa: BLE001
            log.exception("worker %s#%d loop error: %s", lane, n, e)
            busy = False
        await asyncio.sleep(0.5 if busy else 5)


def janitor_once():
    """Remove inbox/work leftovers that no pending row references (crashes, dedupe edge cases)."""
    now = time.time()
    with store.connect() as con:
        live = {r[0] for r in con.execute("SELECT id FROM shots WHERE status != 'sent'")}
    removed = 0
    for p in C.INBOX.glob("*"):
        if p.is_file() and p.name.split("_", 1)[0] not in live and now - p.stat().st_mtime > 3600:
            p.unlink(missing_ok=True)
            removed += 1
    for d in C.WORK.glob("*"):
        if d.is_dir() and d.name not in live and now - d.stat().st_mtime > 3600:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    if removed:
        log.info("janitor removed %d orphaned files/dirs", removed)


async def janitor():
    while True:
        try:
            await asyncio.to_thread(janitor_once)
        except Exception as e:  # noqa: BLE001
            log.warning("janitor error: %s", e)
        await asyncio.sleep(1800)


def start_all():
    tasks = [asyncio.create_task(janitor())]
    for i in range(max(1, C.WORKERS_FAST)):
        tasks.append(asyncio.create_task(worker("fast", i + 1)))
    for i in range(max(1, C.WORKERS_SLOW)):
        tasks.append(asyncio.create_task(worker("slow", i + 1)))
    return tasks
