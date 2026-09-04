"""Thin Telegram Bot API client. Works with api.telegram.org or a local telegram-bot-api server.

With a local server started with --local, files are sent by path (file://...) with zero copying;
if the server refuses the path we fall back to a regular multipart upload.
"""
import logging
from pathlib import Path

import httpx

log = logging.getLogger("deckshots.tg")
logging.getLogger("httpx").setLevel(logging.WARNING)   # never log bot-token URLs
logging.getLogger("httpcore").setLevel(logging.WARNING)


class TelegramError(RuntimeError):
    def __init__(self, code, description):
        super().__init__(f"telegram: {code} {description}")
        self.code, self.description = code, description


class Bot:
    def __init__(self, api_url: str, token: str, local_files: bool = True):
        self.base = f"{api_url.rstrip('/')}/bot{token}"
        self.local_files = local_files
        self.client = httpx.Client(timeout=httpx.Timeout(60, read=1800))

    def call(self, method: str, payload: dict | None = None, files: dict | None = None) -> dict:
        url = f"{self.base}/{method}"
        if files:
            r = self.client.post(url, data={k: _plain(v) for k, v in (payload or {}).items()}, files=files)
        else:
            r = self.client.post(url, json=payload or {})
        j = r.json()
        if not j.get("ok"):
            raise TelegramError(j.get("error_code"), j.get("description"))
        return j["result"]

    def send_file(self, method: str, field: str, path: Path, payload: dict) -> dict:
        if self.local_files:
            try:
                return self.call(method, {**payload, field: f"file://{path}"})
            except TelegramError as e:
                if e.code == 429:
                    raise
                log.warning("%s via file:// refused (%s), retrying as upload", method, e.description)
        with open(path, "rb") as fh:
            return self.call(method, payload, files={field: (path.name, fh)})

    def send_media_group(self, chat_id, items: list[tuple[str, Path, str]], thread_id=None) -> list[dict]:
        """items: [(type, path, caption)] — caption only on the first item is shown as the album caption."""
        base = {"chat_id": chat_id}
        if thread_id:
            base["message_thread_id"] = thread_id
        if self.local_files:
            media = [{"type": t, "media": f"file://{p}", **({"caption": c} if c else {})} for t, p, c in items]
            try:
                return self.call("sendMediaGroup", {**base, "media": media})
            except TelegramError as e:
                if e.code == 429:
                    raise
                log.warning("sendMediaGroup via file:// refused (%s), retrying as upload", e.description)
        handles = []
        try:
            files, media = {}, []
            for i, (t, p, c) in enumerate(items):
                fh = open(p, "rb")
                handles.append(fh)
                files[f"f{i}"] = (p.name, fh)
                media.append({"type": t, "media": f"attach://f{i}", **({"caption": c} if c else {})})
            import json
            return self.call("sendMediaGroup", {**base, "media": json.dumps(media)}, files=files)
        finally:
            for fh in handles:
                fh.close()

    def send_message(self, chat_id, text: str, thread_id=None, reply_markup: dict | None = None, html=True) -> dict:
        payload = {"chat_id": chat_id, "text": text}
        if html:
            payload["parse_mode"] = "HTML"
        if thread_id:
            payload["message_thread_id"] = thread_id
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self.call("sendMessage", payload)


def _plain(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (dict, list)):
        import json
        return json.dumps(v)
    return str(v)
