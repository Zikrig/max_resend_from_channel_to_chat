"""
MAX-бот:
1. Добавляет две кнопки к постам в канале.
2. Пересылает пост в чат комментариев.
3. Управляет рекламой, ссылкой на чат, списком админов и тихими часами через /admin.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import struct
import sys
from datetime import datetime, time
from enum import Enum
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import httpx

API_BASE = "https://platform-api.max.ru"
MOSCOW_TZ = ZoneInfo("Europe/Moscow")


class MoscowFormatter(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        dt = datetime.fromtimestamp(record.created, MOSCOW_TZ)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime("%Y-%m-%d %H:%M:%S")


handler = logging.StreamHandler()
handler.setFormatter(MoscowFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
root_logger = logging.getLogger()
root_logger.handlers.clear()
root_logger.addHandler(handler)
root_logger.setLevel(logging.INFO)
logger = logging.getLogger("MaxBot")


class AdminState(Enum):
    NONE = "none"
    AWAITING_AD_TEXT = "awaiting_ad_text"
    AWAITING_AD_LINK = "awaiting_ad_link"
    AWAITING_CHAT_TEXT = "awaiting_chat_text"
    AWAITING_CHAT_LINK = "awaiting_chat_link"
    AWAITING_NEW_ADMIN = "awaiting_new_admin"
    AWAITING_QUIET_HOURS = "awaiting_quiet_hours"


def parse_admin_ids(raw: Any) -> List[int]:
    if raw is None:
        return []
    if isinstance(raw, list):
        values = raw
    else:
        values = str(raw).split(",")
    result: List[int] = []
    for item in values:
        part = str(item).strip()
        if not part:
            continue
        try:
            result.append(int(part))
        except ValueError:
            logger.warning("Skipping invalid admin id: %s", part)
    return sorted(set(result))


def get_short_id(seq: Any) -> str:
    try:
        if not seq:
            return ""
        packed = struct.pack(">Q", int(seq))
        return base64.urlsafe_b64encode(packed).decode().rstrip("=")
    except Exception:
        return ""


def parse_hhmm(value: str) -> time:
    return datetime.strptime(value, "%H:%M").time()


def normalize_quiet_hours(value: str) -> str:
    raw = value.strip()
    if "-" not in raw:
        raise ValueError("Формат должен быть HH:MM-HH:MM")
    start_raw, end_raw = [part.strip() for part in raw.split("-", 1)]
    start_time = parse_hhmm(start_raw)
    end_time = parse_hhmm(end_raw)
    return f"{start_time.strftime('%H:%M')}-{end_time.strftime('%H:%M')}"


def is_time_in_range(now_value: time, range_value: str) -> bool:
    if not range_value:
        return False
    start_raw, end_raw = range_value.split("-", 1)
    start_time = parse_hhmm(start_raw)
    end_time = parse_hhmm(end_raw)
    if start_time <= end_time:
        return start_time <= now_value <= end_time
    return now_value >= start_time or now_value <= end_time


class Config:
    def __init__(self, filename: str = "config.json"):
        self.filename = filename
        self.ad_text = os.environ.get("AD_TEXT", "Реклама")
        self.ad_url = os.environ.get("AD_URL", "https://max.ru")
        self.channel_id = int(os.environ.get("CHANNEL_CHAT_ID", "0"))
        self.comments_chat_id = int(os.environ.get("COMMENTS_CHAT_ID", "0"))
        self.comments_chat_text = os.environ.get("COMMENTS_CHAT_TEXT", "Чат комментариев")
        self.comments_chat_link = os.environ.get("COMMENTS_CHAT_LINK", "")
        self.quiet_hours = os.environ.get("QUIET_HOURS", "").strip()
        self._env_admin_ids = parse_admin_ids(os.environ.get("ADMIN_USER_IDS", ""))
        self.admin_ids = list(self._env_admin_ids)

        self.load()
        self.admin_ids = sorted(set(self.admin_ids) | set(self._env_admin_ids))
        logger.info(
            "Config initialized: channel=%s comments_chat=%s admins=%s quiet_hours=%s",
            self.channel_id,
            self.comments_chat_id,
            self.admin_ids,
            self.quiet_hours or "-",
        )

    def load(self) -> None:
        if not os.path.exists(self.filename):
            return
        try:
            with open(self.filename, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.ad_text = data.get("ad_text", self.ad_text)
            self.ad_url = data.get("ad_url", self.ad_url)
            self.comments_chat_text = data.get("comments_chat_text", self.comments_chat_text)
            self.comments_chat_link = data.get("comments_chat_link", self.comments_chat_link)
            self.quiet_hours = data.get("quiet_hours", self.quiet_hours)
            self.admin_ids = parse_admin_ids(data.get("admin_ids", self.admin_ids))
            logger.info("Config loaded from file.")
        except Exception as e:
            logger.error("Failed to load config file: %s", e)

    def save(self) -> None:
        try:
            with open(self.filename, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "ad_text": self.ad_text,
                        "ad_url": self.ad_url,
                        "comments_chat_text": self.comments_chat_text,
                        "comments_chat_link": self.comments_chat_link,
                        "admin_ids": self.admin_ids,
                        "quiet_hours": self.quiet_hours,
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
            logger.info("Config saved to file.")
        except Exception as e:
            logger.error("Failed to save config file: %s", e)


class MaxBot:
    def __init__(self, token: str, config: Config):
        self.token = token
        self.config = config
        self.headers = {"Authorization": self.token}
        self.client = httpx.AsyncClient(base_url=API_BASE, headers=self.headers, timeout=60.0)
        self.bot_id: int | None = None
        self.admin_states: Dict[int, AdminState] = {}

    def is_admin(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.config.admin_ids

    def in_quiet_hours(self) -> bool:
        return is_time_in_range(datetime.now(MOSCOW_TZ).time(), self.config.quiet_hours)

    async def get_me(self) -> None:
        try:
            r = await self.client.get("/me")
            r.raise_for_status()
            data = r.json()
            self.bot_id = data.get("user_id")
            logger.info("Logged in as bot ID %s (@%s)", self.bot_id, data.get("username"))
        except Exception as e:
            logger.critical("Failed to get bot info: %s", e)
            sys.exit(1)

    async def send_message(
        self,
        chat_id: int,
        text: str,
        attachments: Optional[List[Dict]] = None,
    ) -> Optional[Dict]:
        try:
            payload = {"text": text}
            if attachments:
                payload["attachments"] = attachments
            params = {"user_id": chat_id} if chat_id > 0 else {"chat_id": chat_id}
            r = await self.client.post("/messages", params=params, json=payload)
            r.raise_for_status()
            return r.json().get("message")
        except Exception as e:
            logger.error("Failed to send message to %s: %s", chat_id, e)
            return None

    async def edit_message(
        self,
        message_id: str,
        text: str,
        attachments: Optional[List[Dict]] = None,
    ) -> bool:
        try:
            payload = {"text": text}
            if attachments is not None:
                payload["attachments"] = attachments
            r = await self.client.put("/messages", params={"message_id": message_id}, json=payload)
            if r.status_code != 200:
                logger.error("Edit failed: %s %s", r.status_code, r.text)
            r.raise_for_status()
            return True
        except Exception as e:
            logger.error("Failed to edit message %s: %s", message_id, e)
            return False

    async def delete_message(self, message_id: str) -> bool:
        try:
            r = await self.client.delete("/messages", params={"message_id": message_id})
            if r.status_code != 200:
                logger.error("Delete failed: %s %s", r.status_code, r.text)
            r.raise_for_status()
            return True
        except Exception as e:
            logger.error("Failed to delete message %s: %s", message_id, e)
            return False

    def get_standard_buttons(self, include_ad: bool = True) -> List[List[Dict]]:
        buttons: List[List[Dict]] = []
        if include_ad and self.config.ad_text and self.config.ad_url:
            buttons.append([{"type": "link", "text": self.config.ad_text, "url": self.config.ad_url}])
        return buttons

    async def handle_update(self, update: Dict[str, Any]) -> None:
        update_type = update.get("update_type")
        if update_type == "message_created":
            await self.on_message_created(update.get("message", {}))
        elif update_type == "message_callback":
            await self.on_callback(update)

    async def on_message_created(self, msg: Dict[str, Any]) -> None:
        sender = msg.get("sender", {})
        sender_id = int(sender.get("user_id")) if sender.get("user_id") else None
        recipient = msg.get("recipient", {})
        raw_chat_id = recipient.get("chat_id") or recipient.get("chat", {}).get("chat_id") or recipient.get("user_id")
        chat_id = int(raw_chat_id) if raw_chat_id is not None else None
        message_id = msg.get("body", {}).get("mid")

        if sender_id and self.bot_id and sender_id == self.bot_id:
            return

        if chat_id is not None and chat_id == self.config.channel_id:
            await self.process_channel_post(msg)
            return

        if chat_id is not None and chat_id == self.config.comments_chat_id and message_id and self.in_quiet_hours():
            logger.info("Deleting message %s due to quiet hours %s", message_id, self.config.quiet_hours)
            await self.delete_message(message_id)
            return

        if self.is_admin(sender_id):
            is_not_public = chat_id not in (self.config.channel_id, self.config.comments_chat_id)
            if is_not_public:
                await self.process_admin_message(msg)

    async def process_channel_post(self, msg: Dict[str, Any]) -> None:
        message_id = msg.get("body", {}).get("mid")
        text = msg.get("body", {}).get("text") or ""
        attachments = msg.get("body", {}).get("attachments") or []

        clean_attachments = []
        for item in attachments:
            if item.get("type") == "inline_keyboard":
                continue
            payload = item.get("payload", {})
            safe_payload = {
                key: value
                for key, value in payload.items()
                if key not in ("callback_id", "url", "size", "width", "height", "duration")
            }
            clean_attachments.append({"type": item.get("type"), "payload": safe_payload})

        short_message_id = ""
        if self.config.comments_chat_id:
            copy_attachments = list(clean_attachments)
            ad_buttons = self.get_standard_buttons(include_ad=True)
            if ad_buttons:
                copy_attachments.append({"type": "inline_keyboard", "payload": {"buttons": ad_buttons}})

            forwarded = await self.send_message(self.config.comments_chat_id, text, copy_attachments)
            if forwarded:
                body = forwarded.get("body", {})
                short_message_id = get_short_id(body.get("seq")) or str(body.get("mid")).split(".")[-1]

        message_link = ""
        if self.config.comments_chat_id and short_message_id:
            message_link = f"https://max.ru/c/{self.config.comments_chat_id}/{short_message_id}"

        channel_buttons: List[List[Dict]] = []
        if self.config.comments_chat_link:
            channel_buttons.append(
                [{"type": "link", "text": self.config.comments_chat_text, "url": self.config.comments_chat_link}]
            )
        if message_link:
            channel_buttons.append([{"type": "link", "text": "💬 Перейти к сообщению", "url": message_link}])

        channel_attachments = list(clean_attachments)
        if channel_buttons:
            channel_attachments.append({"type": "inline_keyboard", "payload": {"buttons": channel_buttons}})

        if message_id:
            await self.edit_message(message_id, text, channel_attachments)

    async def process_admin_message(self, msg: Dict[str, Any]) -> None:
        sender_id = msg.get("sender", {}).get("user_id")
        text = (msg.get("body", {}).get("text") or "").strip()
        state = self.admin_states.get(sender_id, AdminState.NONE)

        if text.lower() == "/admin":
            self.admin_states[sender_id] = AdminState.NONE
            await self.send_admin_menu(sender_id)
            return

        if state == AdminState.AWAITING_AD_TEXT:
            self.config.ad_text = text
            self.config.save()
            self.admin_states[sender_id] = AdminState.NONE
            await self.send_message(sender_id, f"Текст рекламы изменен: {text}")
            await self.send_ad_submenu(sender_id)
            return

        if state == AdminState.AWAITING_AD_LINK:
            if not text.startswith("http://") and not text.startswith("https://"):
                await self.send_message(sender_id, "Ссылка должна начинаться с http:// или https://")
                return
            self.config.ad_url = text
            self.config.save()
            self.admin_states[sender_id] = AdminState.NONE
            await self.send_message(sender_id, "Ссылка рекламы изменена")
            await self.send_ad_submenu(sender_id)
            return

        if state == AdminState.AWAITING_CHAT_TEXT:
            self.config.comments_chat_text = text
            self.config.save()
            self.admin_states[sender_id] = AdminState.NONE
            await self.send_message(sender_id, f"Текст кнопки чата изменен: {text}")
            await self.send_chat_link_submenu(sender_id)
            return

        if state == AdminState.AWAITING_CHAT_LINK:
            if not text.startswith("http://") and not text.startswith("https://"):
                await self.send_message(sender_id, "Ссылка должна начинаться с http:// или https://")
                return
            self.config.comments_chat_link = text
            self.config.save()
            self.admin_states[sender_id] = AdminState.NONE
            await self.send_message(sender_id, "Ссылка на чат изменена")
            await self.send_chat_link_submenu(sender_id)
            return

        if state == AdminState.AWAITING_NEW_ADMIN:
            try:
                new_admin_id = int(text)
            except ValueError:
                await self.send_message(sender_id, "Нужно отправить только числовой user_id нового админа.")
                return
            self.config.admin_ids = sorted(set(self.config.admin_ids + [new_admin_id]))
            self.config.save()
            self.admin_states[sender_id] = AdminState.NONE
            await self.send_message(sender_id, f"Админ добавлен: {new_admin_id}")
            await self.send_admins_submenu(sender_id)
            return

        if state == AdminState.AWAITING_QUIET_HOURS:
            try:
                self.config.quiet_hours = normalize_quiet_hours(text)
            except ValueError:
                await self.send_message(sender_id, "Формат: HH:MM-HH:MM, например 12:00-14:00 или 21:33-07:00")
                return
            self.config.save()
            self.admin_states[sender_id] = AdminState.NONE
            await self.send_message(sender_id, f"Тихие часы обновлены: {self.config.quiet_hours} (МСК)")
            await self.send_quiet_hours_submenu(sender_id)

    async def send_admin_menu(self, user_id: int) -> None:
        buttons = [
            [{"type": "callback", "text": "Рекламная ссылка", "payload": "admin_ad_submenu"}],
            [{"type": "callback", "text": "Ссылка на чат", "payload": "admin_chat_link_submenu"}],
            [{"type": "callback", "text": "Админы", "payload": "admin_admins_submenu"}],
            [{"type": "callback", "text": "Тихие часы", "payload": "admin_quiet_hours_submenu"}],
        ]
        await self.send_message(user_id, "Админ-панель", [{"type": "inline_keyboard", "payload": {"buttons": buttons}}])

    async def send_ad_submenu(self, user_id: int) -> None:
        buttons = [
            [{"type": "callback", "text": "Изменить текст", "payload": "admin_set_text"}],
            [{"type": "callback", "text": "Изменить ссылку", "payload": "admin_set_link"}],
            [{"type": "callback", "text": "Назад", "payload": "admin_menu"}],
        ]
        text = f"Реклама\nТекст: {self.config.ad_text}\nСсылка: {self.config.ad_url}"
        await self.send_message(user_id, text, [{"type": "inline_keyboard", "payload": {"buttons": buttons}}])

    async def send_chat_link_submenu(self, user_id: int) -> None:
        buttons = [
            [{"type": "callback", "text": "Изменить текст кнопки", "payload": "admin_set_chat_text"}],
            [{"type": "callback", "text": "Изменить ссылку на чат", "payload": "admin_set_chat_link"}],
            [{"type": "callback", "text": "Назад", "payload": "admin_menu"}],
        ]
        text = (
            f"Кнопка чата\n"
            f"Текст: {self.config.comments_chat_text}\n"
            f"Ссылка: {self.config.comments_chat_link}"
        )
        await self.send_message(user_id, text, [{"type": "inline_keyboard", "payload": {"buttons": buttons}}])

    async def send_admins_submenu(self, user_id: int) -> None:
        buttons = [
            [{"type": "callback", "text": "Добавить админа", "payload": "admin_add_admin"}],
            [{"type": "callback", "text": "Назад", "payload": "admin_menu"}],
        ]
        admins_text = ", ".join(str(admin_id) for admin_id in self.config.admin_ids) or "-"
        text = f"Админы\nСписок: {admins_text}"
        await self.send_message(user_id, text, [{"type": "inline_keyboard", "payload": {"buttons": buttons}}])

    async def send_quiet_hours_submenu(self, user_id: int) -> None:
        buttons = [
            [{"type": "callback", "text": "Изменить диапазон", "payload": "admin_set_quiet_hours"}],
            [{"type": "callback", "text": "Назад", "payload": "admin_menu"}],
        ]
        current = self.config.quiet_hours or "не настроены"
        text = (
            "Тихие часы\n"
            f"Текущий диапазон: {current}\n"
            "Часовой пояс: Europe/Moscow (МСК)"
        )
        await self.send_message(user_id, text, [{"type": "inline_keyboard", "payload": {"buttons": buttons}}])

    async def on_callback(self, update: Dict[str, Any]) -> None:
        callback_data = update.get("callback", {})
        payload = callback_data.get("payload")
        user_data = callback_data.get("user", {})
        sender_id = int(user_data.get("user_id")) if user_data.get("user_id") else None

        if not self.is_admin(sender_id):
            return

        if payload == "admin_menu":
            await self.send_admin_menu(sender_id)
        elif payload == "admin_ad_submenu":
            await self.send_ad_submenu(sender_id)
        elif payload == "admin_chat_link_submenu":
            await self.send_chat_link_submenu(sender_id)
        elif payload == "admin_admins_submenu":
            await self.send_admins_submenu(sender_id)
        elif payload == "admin_quiet_hours_submenu":
            await self.send_quiet_hours_submenu(sender_id)
        elif payload == "admin_set_text":
            self.admin_states[sender_id] = AdminState.AWAITING_AD_TEXT
            await self.send_message(sender_id, "Введите новый текст рекламной кнопки:")
        elif payload == "admin_set_link":
            self.admin_states[sender_id] = AdminState.AWAITING_AD_LINK
            await self.send_message(sender_id, "Введите новую ссылку рекламы:")
        elif payload == "admin_set_chat_text":
            self.admin_states[sender_id] = AdminState.AWAITING_CHAT_TEXT
            await self.send_message(sender_id, "Введите новый текст кнопки чата:")
        elif payload == "admin_set_chat_link":
            self.admin_states[sender_id] = AdminState.AWAITING_CHAT_LINK
            await self.send_message(sender_id, "Введите новую ссылку на чат:")
        elif payload == "admin_add_admin":
            self.admin_states[sender_id] = AdminState.AWAITING_NEW_ADMIN
            await self.send_message(sender_id, "Введите user_id нового админа:")
        elif payload == "admin_set_quiet_hours":
            self.admin_states[sender_id] = AdminState.AWAITING_QUIET_HOURS
            await self.send_message(sender_id, "Введите диапазон, например 12:00-14:00 или 21:33-07:00")

    async def run(self) -> None:
        await self.get_me()
        marker = None
        logger.info("Bot started polling in Moscow timezone.")
        while True:
            try:
                params = {"limit": 100, "timeout": 30}
                if marker is not None:
                    params["marker"] = marker
                r = await self.client.get("/updates", params=params)
                r.raise_for_status()
                data = r.json()
                for update in data.get("updates", []):
                    if isinstance(update, dict):
                        await self.handle_update(update)
                marker = data.get("marker")
            except Exception as e:
                logger.error("Error: %s", e)
                await asyncio.sleep(5)


async def main() -> None:
    token = os.environ.get("MAX_BOT_TOKEN")
    if not token:
        logger.error("MAX_BOT_TOKEN not found in environment!")
        return
    bot = MaxBot(token, Config())
    try:
        await bot.run()
    finally:
        await bot.client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
