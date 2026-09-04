"""Command bot (optional): /status, /stats, pause/resume with colored inline buttons.

Runs only when COMMAND_BOT_TOKEN is set. It must be a *separate* bot from the one used by another
service with webhooks/polling, otherwise getUpdates conflicts. Users are recognised by chat_id from
config.yml (devices) — nobody else gets anything.
"""
import asyncio
import html
import logging
import time

from . import config as C
from . import store
from .tg import Bot, TelegramError

log = logging.getLogger("deckshots.bot")

T = {
    "ru": {
        "hello": "👋 Это <b>DeckShots</b>: скриншоты и клипы со Steam Deck прилетают сюда автоматически.\n\nТвои деки: {decks}",
        "no_decks": "🙈 За этим чатом не закреплён ни один дек. Попроси админа добавить тебя в config.yml.",
        "status": "📊 Статус", "stats": "📈 Статистика", "pause": "⏸ Пауза", "resume": "▶️ Продолжить",
        "all": "👑 Все деки", "help": "❓ Помощь",
        "paused_ok": "⏸ Отправка с {deck} приостановлена. Файлы копятся на деке и сервере, ничего не теряется.",
        "resumed_ok": "▶️ Отправка с {deck} возобновлена.",
        "help_text": ("Команды:\n/status — очередь и связь с деками\n/stats — статистика\n/pause и /resume — пауза отправки\n\n"
                      "Скриншот: Steam + R1. Клип: Steam + R1 удерживать (при включённой записи). "
                      "Записи Spectacle в десктоп-режиме тоже уходят."),
        "online": "🟢 на связи", "stale": "🟡 молчит {ago}", "never": "⚪️ ещё не выходил на связь",
        "paused": "⏸ на паузе",
    },
    "en": {
        "hello": "👋 This is <b>DeckShots</b>: screenshots and clips from your Steam Deck land here automatically.\n\nYour decks: {decks}",
        "no_decks": "🙈 No deck is linked to this chat. Ask the admin to add you to config.yml.",
        "status": "📊 Status", "stats": "📈 Stats", "pause": "⏸ Pause", "resume": "▶️ Resume",
        "all": "👑 All decks", "help": "❓ Help",
        "paused_ok": "⏸ Delivery from {deck} paused. Files keep piling up on the deck and server, nothing is lost.",
        "resumed_ok": "▶️ Delivery from {deck} resumed.",
        "help_text": ("Commands:\n/status — queue and deck connectivity\n/stats — statistics\n/pause and /resume — pause delivery\n\n"
                      "Screenshot: Steam + R1. Clip: hold Steam + R1 (with recording enabled). "
                      "Spectacle recordings from Desktop Mode are sent too."),
        "online": "🟢 online", "stale": "🟡 silent for {ago}", "never": "⚪️ never checked in",
        "paused": "⏸ paused",
    },
}
L = T.get(C.LANG, T["en"])


def ago(ts: float | None) -> str:
    if not ts:
        return "—"
    s = int(time.time() - ts)
    if s < 90:
        return f"{s}s"
    if s < 5400:
        return f"{s // 60}m"
    if s < 172800:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def esc(s) -> str:
    return html.escape(str(s))


# ----------------------------------------------------------------------------- views
def keyboard(devs: list[dict], admin: bool) -> dict:
    any_paused = any((store.device_state(d["name"]) or {"paused": 0})["paused"] for d in devs)
    row1 = [{"text": L["status"], "callback_data": "status", "style": "primary"},
            {"text": L["stats"], "callback_data": "stats", "style": "success"}]
    row2 = [{"text": L["resume"], "callback_data": "resume", "style": "success"} if any_paused
            else {"text": L["pause"], "callback_data": "pause", "style": "danger"},
            {"text": L["help"], "callback_data": "help"}]
    rows = [row1, row2]
    if admin:
        rows.append([{"text": L["all"], "callback_data": "all", "style": "primary"}])
    return {"inline_keyboard": rows}


def device_line(d: dict) -> str:
    st = store.device_state(d["name"])
    label = esc(d.get("label") or d["name"])
    if not st or not st["last_seen"]:
        link = L["never"]
    elif time.time() - st["last_seen"] < 900:
        link = L["online"]
    else:
        link = L["stale"].format(ago=ago(st["last_seen"]))
    pending = store.pending_count(d["name"])
    q = f" · deck queue {st['queue_new'] + st['queue_uploaded']}" if st and (st["queue_new"] or st["queue_uploaded"]) else ""
    paused = f" · {L['paused']}" if st and st["paused"] else ""
    ver = f" · v{esc(st['agent_version'])}" if st and st["agent_version"] else ""
    return f"🎮 <b>{label}</b> — {link}{paused}\n    server queue {pending}{q}{ver}"


def status_text(devs: list[dict]) -> str:
    return "\n".join(device_line(d) for d in devs) or L["no_decks"]


def stats_text(devs: list[dict]) -> str:
    out = []
    for d in devs:
        s = store.stats(d["name"])
        kinds = " · ".join(f"{k} {v}" for k, v in sorted(s["by_kind"].items())) or "—"
        games = "\n".join(f"    {i}. {esc(g)} — {c}" for i, (g, c) in enumerate(s["top_games"], 1)) or "    —"
        out.append(
            f"📈 <b>{esc(d.get('label') or d['name'])}</b>\n"
            f"<blockquote>sent: <b>{s['total']}</b> (today {s['today']}, 7d {s['week']}) · {human(s['bytes'])}\n"
            f"{kinds}\npending {s['pending']} · failed {s['failed']} · last {ago(s['last_sent'])} ago</blockquote>\n"
            f"top games:\n{games}"
        )
    return "\n\n".join(out) or L["no_decks"]


def admin_text() -> str:
    devs = list(C.DEVICES.values())
    total_pending = store.pending_count()
    inbox_bytes = sum(p.stat().st_size for p in C.INBOX.glob("*") if p.is_file())
    head = f"👑 <b>DeckShots</b> · server queue {total_pending} · inbox {human(inbox_bytes)}\n\n"
    return head + status_text(devs) + "\n\n" + stats_text(devs)


# ----------------------------------------------------------------------------- dispatch
def handle(bot: Bot, chat_id, text: str, cb_id: str | None = None):
    admin = C.is_admin(chat_id)
    devs = C.devices_for_chat(chat_id)
    if not devs and not admin:
        bot.send_message(chat_id, L["no_decks"])
        return
    cmd = text.strip().split()[0].lower().lstrip("/") if text.strip() else "start"
    cmd = cmd.split("@")[0]
    kb = keyboard(devs, admin)
    if cmd in ("start",):
        names = ", ".join(esc(d.get("label") or d["name"]) for d in devs) or "—"
        bot.send_message(chat_id, L["hello"].format(decks=names), reply_markup=kb)
    elif cmd == "help":
        bot.send_message(chat_id, L["help_text"], reply_markup=kb)
    elif cmd == "status":
        bot.send_message(chat_id, status_text(devs) if devs else admin_text(), reply_markup=kb)
    elif cmd == "stats":
        bot.send_message(chat_id, stats_text(devs) if devs else admin_text(), reply_markup=kb)
    elif cmd in ("pause", "resume"):
        for d in devs:
            store.set_paused(d["name"], cmd == "pause")
        kb = keyboard(devs, admin)
        key = "paused_ok" if cmd == "pause" else "resumed_ok"
        bot.send_message(chat_id, L[key].format(deck=", ".join(esc(d.get("label") or d["name"]) for d in devs)), reply_markup=kb)
    elif cmd == "all" and admin:
        bot.send_message(chat_id, admin_text(), reply_markup=kb)
    else:
        bot.send_message(chat_id, L["help_text"], reply_markup=kb)
    if cb_id:
        try:
            bot.call("answerCallbackQuery", {"callback_query_id": cb_id})
        except TelegramError:
            pass


def poll_once(bot: Bot, offset: int) -> int:
    updates = bot.call("getUpdates", {"offset": offset, "timeout": 25, "allowed_updates": ["message", "callback_query"]})
    for u in updates:
        offset = u["update_id"] + 1
        try:
            if "message" in u and u["message"].get("text"):
                m = u["message"]
                handle(bot, m["chat"]["id"], m["text"])
            elif "callback_query" in u:
                q = u["callback_query"]
                handle(bot, q["message"]["chat"]["id"], "/" + q.get("data", "help"), q["id"])
        except Exception as e:  # noqa: BLE001
            log.exception("update %s failed: %s", u.get("update_id"), e)
    return offset


async def run():
    if not C.COMMAND_BOT_TOKEN:
        log.info("command bot disabled (no COMMAND_BOT_TOKEN)")
        return
    bot = Bot(C.API, C.COMMAND_BOT_TOKEN, C.LOCAL_FILES)
    try:
        me = await asyncio.to_thread(bot.call, "getMe")
        await asyncio.to_thread(bot.call, "setMyCommands", {"commands": [
            {"command": "status", "description": L["status"]},
            {"command": "stats", "description": L["stats"]},
            {"command": "pause", "description": L["pause"]},
            {"command": "resume", "description": L["resume"]},
            {"command": "help", "description": L["help"]},
        ]})
        log.info("command bot @%s started", me.get("username"))
    except Exception as e:  # noqa: BLE001
        log.error("command bot cannot start: %s", e)
        return
    offset = 0
    while True:
        try:
            offset = await asyncio.to_thread(poll_once, bot, offset)
        except Exception as e:  # noqa: BLE001
            log.warning("poll error: %s", e)
            await asyncio.sleep(5)
