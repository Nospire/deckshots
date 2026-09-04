"""DeckShots server: HTTP API for the deck agents + delivery workers + optional command bot.

Flow: deck PUT /upload -> inbox (received) -> worker -> Telegram -> sent -> files deleted.
The deck polls GET /status/<id> and deletes its local copy only when status == sent.
"""
import asyncio
import hashlib
import logging
import os
import re
import time
import urllib.parse
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse

from . import bot as command_bot
from . import config as C
from . import media, store, worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("deckshots")

VERSION = "1.1.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    for d in (C.INBOX, C.WORK, C.DBDIR):
        d.mkdir(parents=True, exist_ok=True)
    store.init(C.DBDIR / "shots.db")
    log.info("deckshots %s: api=%s prefix=%s lanes fast=%d slow=%d devices=%s",
             VERSION, C.API, C.PREFIX, C.WORKERS_FAST, C.WORKERS_SLOW, list(C.BY_NAME))
    tasks = worker.start_all() + [asyncio.create_task(command_bot.run())]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="deckshots", docs_url=None, redoc_url=None, lifespan=lifespan)
router = APIRouter(prefix=C.PREFIX)


def auth(token: str) -> dict:
    dev = C.DEVICES.get(token)
    if not dev:
        raise HTTPException(401, "unknown device token")
    return dev


@router.get("/health")
async def health():
    return {"ok": True, "version": VERSION, "pending": store.pending_count()}


@router.put("/upload")
async def upload(
    request: Request,
    x_device_token: str = Header(...),
    x_sha256: str = Header(...),
    x_kind: str = Header(...),
    x_filename: str = Header(...),
    x_appid: str = Header(""),
    x_game: str = Header(""),
    x_taken_at: str = Header(""),
):
    dev = auth(x_device_token)
    if x_kind not in ("screenshot", "clip", "video"):
        raise HTTPException(400, "kind must be screenshot|clip|video")
    sha = x_sha256.lower()
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise HTTPException(400, "bad sha256")
    filename = Path(urllib.parse.unquote(x_filename)).name
    if not filename:
        raise HTTPException(400, "bad filename")
    game = urllib.parse.unquote(x_game).strip()
    appid = x_appid.strip()

    row = store.find_dup(dev["name"], sha)
    if row:
        if row["status"] == "sent" or worker.inbox_path(row["id"], row["filename"]).exists():
            return {"id": row["id"], "status": row["status"], "dup": True}
        store.delete_shot(row["id"])   # stale record without a file: receive again

    shot_id = uuid.uuid4().hex[:12]
    dest = worker.inbox_path(shot_id, filename)
    part = dest.with_suffix(dest.suffix + ".part")
    h, size = hashlib.sha256(), 0
    try:
        with open(part, "wb") as out:
            async for chunk in request.stream():
                out.write(chunk)
                h.update(chunk)
                size += len(chunk)
        if h.hexdigest() != sha:
            raise HTTPException(400, "sha256 mismatch")
        os.replace(part, dest)
    finally:
        part.unlink(missing_ok=True)

    lane = "slow" if x_kind == "video" and media.needs_transcode(filename) else "fast"
    store.insert_shot(id=shot_id, device=dev["name"], sha256=sha, kind=x_kind, lane=lane, appid=appid, game=game,
                      filename=filename, taken_at=x_taken_at, size=size, created=time.time())
    store.touch_device(dev["name"])
    log.info("received %s from %s: %s/%s %s (%s, %d bytes)", shot_id, dev["name"], x_kind, lane, filename, game or appid, size)
    return {"id": shot_id, "status": "received"}


@router.get("/status/{shot_id}")
async def status(shot_id: str, x_device_token: str = Header(...)):
    dev = auth(x_device_token)
    row = store.get_shot(shot_id, dev["name"])
    if not row:
        raise HTTPException(404, "unknown id")
    return {"id": row["id"], "status": row["status"], "attempts": row["attempts"], "error": row["error"]}


@router.post("/ping")
async def ping(x_device_token: str = Header(...), x_host: str = Header(""), x_agent_version: str = Header(""),
               x_queue_new: str = Header("0"), x_queue_uploaded: str = Header("0")):
    dev = auth(x_device_token)
    host = re.sub(r"[^\w.-]", "", x_host)[:40]
    store.touch_device(dev["name"], host, x_agent_version[:20], int(x_queue_new or 0), int(x_queue_uploaded or 0))
    st = store.device_state(dev["name"])
    return {"ok": True, "paused": bool(st and st["paused"]), "server_version": VERSION}


@router.post("/hello")
async def hello(x_device_token: str = Header(...), x_host: str = Header("")):
    """Called by the installer once the agent is up: tells the chat (and admin) that the deck is connected."""
    dev = auth(x_device_token)
    host = re.sub(r"[^\w.-]", "", x_host)[:40]
    store.touch_device(dev["name"], host)
    label = dev.get("label") or dev["name"]
    results = {}
    if dev.get("chat_id"):
        text = (f"🎮 Дек «{label}» подключён к DeckShots.\nСкриншоты и клипы Steam будут приходить сюда."
                if C.LANG == "ru" else f"🎮 Deck “{label}” is connected to DeckShots.\nSteam screenshots and clips will arrive here.")
        try:
            await asyncio.to_thread(worker.bot.send_message, dev["chat_id"], text, dev.get("thread_id"), None, False)
            results["chat"] = True
        except Exception as e:  # noqa: BLE001
            results["chat"] = str(e)
    if C.ADMIN_CHAT and C.ADMIN_CHAT != dev.get("chat_id"):
        try:
            await asyncio.to_thread(worker.bot.send_message, C.ADMIN_CHAT,
                                    f"🔔 Deck {dev['name']} ({label}) connected" + (f", host {host}" if host else ""), None, None, False)
            results["admin"] = True
        except Exception as e:  # noqa: BLE001
            results["admin"] = str(e)
    log.info("hello from %s host=%s -> %s", dev["name"], host, results)
    return {"ok": True, **results}


@router.get("/dl/{key}/{fname}")
async def dl(key: str, fname: str):
    if not C.DL_KEY or key != C.DL_KEY or "/" in fname or fname.startswith("."):
        raise HTTPException(404, "not found")
    p = C.PKG_DIR / fname
    if not p.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(p, filename=fname)


@router.get("/agent.py")
async def agent_py():
    return FileResponse(C.AGENT_DIR / "agent.py", media_type="text/x-python")


@router.get("/install.sh")
async def install_sh():
    return FileResponse(C.AGENT_DIR / "install.sh", media_type="text/x-shellscript")


app.include_router(router)
