"""Geekcom Panel mode: devices come from the Panel instead of config.yml.

The deck sends its Geekcom device token (the same one the Geekcom plugin uses for /доступ). Unknown tokens are
resolved by the Panel (POST /api/shots/whois, service-to-service): whose deck it is, whether the person is a Geek
right now and whether they paused "Media from your Deck" in the app. Answers are cached for 10 minutes.

The deck -> chat mapping is kept in the local store, so workers can deliver after a restart without the token.
Delivery itself is unchanged (DeckShots' own pipeline); the Panel only gets a journal line per sent batch.
Devices listed in config.yml keep working exactly as before.
"""
import logging
import os
import time

import httpx
from fastapi import HTTPException

from . import config as C, store

log = logging.getLogger("deckshots.panel")

URL = (C.CFG.get("panel_url") or "").rstrip("/")
TOKEN = os.environ.get("PANEL_SERVICE_TOKEN", "")
TTL = int(C.CFG.get("panel_cache_seconds", 600))
_cache: dict[str, tuple[float, dict]] = {}


def enabled() -> bool:
    return bool(URL and TOKEN)


def _dev(w: dict) -> dict:
    return {"name": w["device_id"], "label": w.get("устройство") or w["device_id"], "chat_id": w["tg_id"],
            "paused": bool(w.get("пауза")), "panel": True}


def who(token: str) -> dict | None:
    """Device by Geekcom token, or None when the Panel mode is off. Raises HTTPException with the Panel's code."""
    if not enabled():
        return None
    hit = _cache.get(token)
    if hit and time.time() - hit[0] < TTL:
        return hit[1]
    try:
        r = httpx.post(f"{URL}/api/shots/whois", json={"device_token": token},
                       headers={"X-GCE-Service-Token": TOKEN}, timeout=15)
    except httpx.HTTPError as e:
        raise HTTPException(503, "panel unreachable, retry later") from e
    if r.status_code != 200:
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = r.text[:200]
        raise HTTPException(r.status_code if r.status_code in (401, 403, 404) else 503, detail or "panel refused")
    dev = _dev(r.json())
    _cache[token] = (time.time(), dev)
    store.remember_panel_device(dev["name"], dev["chat_id"], dev["label"])
    return dev


def by_name(name: str) -> dict | None:
    """Deck known from the Panel (for workers: they only have the device name)."""
    row = store.panel_device(name)
    if not row:
        return None
    return {"name": row["device"], "label": row["label"] or row["device"], "chat_id": row["chat_id"], "panel": True}


def journal(dev: dict, kind: str, files: int, size: int, ok: bool, error: str = "") -> None:
    """One line per delivered batch -> Panel (the app shows how many and from which deck). Best effort."""
    if not (enabled() and dev.get("panel")):
        return
    try:
        httpx.post(f"{URL}/api/shots/log", headers={"X-GCE-Service-Token": TOKEN}, timeout=10,
                   json={"tg_id": dev["chat_id"], "device_id": dev["name"], "вид": kind, "файлов": files,
                         "байт": size, "ok": ok, "ошибка": error[:300]})
    except httpx.HTTPError as e:
        log.warning("panel journal failed: %s", e)
