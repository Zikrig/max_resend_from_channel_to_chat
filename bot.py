"""
MAX-бот: long polling по /updates, пересылка сообщений из чата комментариев канала в целевой чат.
Требуется: бот — администратор канала (см. документацию MAX).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any

import httpx

API_BASE = "https://platform-api.max.ru"
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()


def _parse_int_set(raw: str | None) -> set[int]:
    if not raw or not raw.strip():
        return set()
    out: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        out.add(int(part))
    return out


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def recipient_chat_id(message: dict[str, Any]) -> int | None:
    r = message.get("recipient")
    if not isinstance(r, dict):
        return None
    if "chat_id" in r:
        try:
            return int(r["chat_id"])
        except (TypeError, ValueError):
            return None
    chat = r.get("chat")
    if isinstance(chat, dict) and "chat_id" in chat:
        try:
            return int(chat["chat_id"])
        except (TypeError, ValueError):
            return None
    return None


def sender_user_id(message: dict[str, Any]) -> int | None:
    s = message.get("sender")
    if not isinstance(s, dict):
        return None
    uid = s.get("user_id")
    if uid is None:
        return None
    try:
        return int(uid)
    except (TypeError, ValueError):
        return None


def is_reply_or_thread(message: dict[str, Any]) -> bool:
    link = message.get("link")
    return isinstance(link, dict) and bool(link)


def sender_display_name(sender: dict[str, Any]) -> str:
    fn = sender.get("first_name")
    ln = sender.get("last_name")
    if fn and ln:
        return f"{fn} {ln}".strip()
    if fn:
        return str(fn)
    if ln:
        return str(ln)
    return (
        sender.get("name")
        or sender.get("username")
        or str(sender.get("user_id", "?"))
    )


def sender_profile_lines(sender: dict[str, Any]) -> list[str]:
    """Ссылки на пользователя: веб по username, deep link по user_id (см. документацию MAX)."""
    lines: list[str] = []
    uid = sender.get("user_id")
    uname = sender.get("username")
    if uname:
        u = str(uname).lstrip("@")
        lines.append(f"Профиль: https://max.ru/{u}")
    if uid is not None:
        lines.append(f"MAX (приложение): max://user/{uid}")
    return lines


def format_forward_text(message: dict[str, Any]) -> str:
    body = message.get("body") if isinstance(message.get("body"), dict) else {}
    text = (body or {}).get("text") or ""
    sender = message.get("sender") if isinstance(message.get("sender"), dict) else {}
    name = sender_display_name(sender)
    lines = [f"💬 {name}"]
    lines.extend(sender_profile_lines(sender))
    lines.append("")
    lines.append(text.strip() if text else "(без текста)")
    atts = (body or {}).get("attachments")
    if isinstance(atts, list) and atts:
        lines.append(f"[вложений: {len(atts)}]")
    return "\n".join(lines).strip()


class MaxBot:
    def __init__(self) -> None:
        token = os.environ.get("MAX_BOT_TOKEN", "").strip()
        if not token:
            print("MAX_BOT_TOKEN is required", file=sys.stderr)
            sys.exit(1)
        self._token = token
        self._headers = {"Authorization": self._token}
        target = os.environ.get("TARGET_CHAT_ID", "").strip()
        if not target:
            print("TARGET_CHAT_ID is required", file=sys.stderr)
            sys.exit(1)
        self._target_chat_id = int(target)
        self._source_chats = _parse_int_set(os.environ.get("SOURCE_CHANNEL_CHAT_IDS"))
        if not self._source_chats:
            print(
                "SOURCE_CHANNEL_CHAT_IDS is required (comma-separated chat ids of channel comments)",
                file=sys.stderr,
            )
            sys.exit(1)
        self._comments_only = _env_bool("COMMENTS_ONLY", False)
        self._bot_user_id: int | None = None
        timeout = httpx.Timeout(120.0, connect=30.0)
        self._client = httpx.AsyncClient(
            base_url=API_BASE,
            headers=self._headers,
            timeout=timeout,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def fetch_me(self) -> None:
        r = await self._client.get("/me")
        r.raise_for_status()
        data = r.json()
        uid = data.get("user_id")
        if uid is not None:
            self._bot_user_id = int(uid)
        logging.info("Bot user_id=%s name=%s", self._bot_user_id, data.get("first_name"))

    async def send_to_target(self, text: str) -> None:
        r = await self._client.post(
            "/messages",
            params={"chat_id": self._target_chat_id},
            json={"text": text},
        )
        if r.status_code == 429:
            await asyncio.sleep(2.0)
            r = await self._client.post(
                "/messages",
                params={"chat_id": self._target_chat_id},
                json={"text": text},
            )
        r.raise_for_status()

    def _should_forward(self, message: dict[str, Any]) -> bool:
        if self._bot_user_id is not None:
            su = sender_user_id(message)
            if su is not None and su == self._bot_user_id:
                return False
        cid = recipient_chat_id(message)
        if cid is None or cid not in self._source_chats:
            return False
        if self._comments_only and not is_reply_or_thread(message):
            return False
        return True

    async def handle_message_created(self, message: dict[str, Any]) -> None:
        if not self._should_forward(message):
            return
        text = format_forward_text(message)
        if not text:
            return
        try:
            await self.send_to_target(text)
            logging.info(
                "Forwarded message from chat_id=%s sender=%s",
                recipient_chat_id(message),
                sender_user_id(message),
            )
        except httpx.HTTPStatusError as e:
            logging.error("Send failed: %s %s", e.response.status_code, e.response.text[:500])

    async def process_update(self, update: dict[str, Any]) -> None:
        ut = update.get("update_type")
        if ut != "message_created":
            return
        msg = update.get("message")
        if not isinstance(msg, dict):
            return
        await self.handle_message_created(msg)

    async def poll_loop(self) -> None:
        marker: int | None = None
        while True:
            params: dict[str, Any] = {
                "limit": 100,
                "timeout": 30,
                "types": "message_created",
            }
            if marker is not None:
                params["marker"] = marker
            try:
                r = await self._client.get("/updates", params=params)
                r.raise_for_status()
            except httpx.HTTPError as e:
                logging.warning("GET /updates failed: %s", e)
                await asyncio.sleep(5.0)
                continue

            data = r.json()
            updates = data.get("updates") or []
            if not isinstance(updates, list):
                updates = []
            next_marker = data.get("marker")
            if isinstance(next_marker, int):
                marker = next_marker
            elif next_marker is not None:
                try:
                    marker = int(next_marker)
                except (TypeError, ValueError):
                    pass

            for u in updates:
                if isinstance(u, dict):
                    await self.process_update(u)


async def main() -> None:
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    bot = MaxBot()
    try:
        await bot.fetch_me()
        await bot.poll_loop()
    finally:
        await bot.close()


if __name__ == "__main__":
    asyncio.run(main())
