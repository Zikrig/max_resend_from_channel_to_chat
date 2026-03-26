"""
MAX-бот:
1. Добавляет кнопки к постам в канале и пересылает пост в чат комментариев (привязка канал→чат в /admin → «Каналы»).
2. Реклама под постом в чате — одна на все каналы (текст и ссылка в /admin).
3. Управляет кнопками, админами и мутом чата через /admin.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
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
    AWAITING_COMMENTS_MESSAGE_BUTTON_TEXT = "awaiting_comments_message_button_text"
    AWAITING_BIND_CHANNEL_INVITE = "awaiting_bind_channel_invite"
    AWAITING_BIND_COMMENTS_INVITE = "awaiting_bind_comments_invite"
    AWAITING_NEW_ADMIN = "awaiting_new_admin"
    AWAITING_MUTE_RANGE = "awaiting_mute_range"


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


def normalize_max_url(url: str) -> str:
    u = (url or "").strip()
    if not u.startswith("http"):
        u = "https://" + u
    return u.rstrip("/")


def extract_join_token(url: str) -> str:
    m = re.search(r"/join/([^/?#]+)", url, re.IGNORECASE)
    return m.group(1) if m else ""


def links_match(a: str, b: str) -> bool:
    return normalize_max_url(a).lower() == normalize_max_url(b).lower()


def try_parse_chat_id_from_text(text: str) -> Optional[int]:
    raw = text.strip()
    if re.fullmatch(r"-?\d+", raw):
        try:
            return int(raw)
        except ValueError:
            return None
    m = re.search(r"/c/(-?\d+)", raw)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def channel_admin_permissions_ok(m: dict) -> bool:
    if m.get("is_owner"):
        return True
    if not m.get("is_admin"):
        return False
    perms = m.get("permissions")
    if not perms:
        return True
    pset = set(perms)
    return bool(pset & {"edit_message", "post_edit_delete_message"})


def comments_chat_admin_permissions_ok(m: dict) -> bool:
    if m.get("is_owner"):
        return True
    if not m.get("is_admin"):
        return False
    perms = m.get("permissions")
    if not perms:
        return True
    pset = set(perms)
    return "write" in pset and "delete_message" in pset


class Config:
    def __init__(self, filename: str = "config.json"):
        self.filename = filename
        self.ad_text = os.environ.get("AD_TEXT", "Реклама")
        self.ad_url = os.environ.get("AD_URL", "https://max.ru")
        self.comments_chat_text = os.environ.get("COMMENTS_CHAT_TEXT", "Чат комментариев")
        self.comments_message_button_text = os.environ.get(
            "COMMENTS_MESSAGE_BUTTON_TEXT", "💬 Перейти к сообщению"
        )
        self.quiet_hours = os.environ.get("QUIET_HOURS", "").strip()
        self.chat_mute_enabled = os.environ.get("CHAT_MUTE_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
        self.root_admin_ids = parse_admin_ids(os.environ.get("ADMIN_USER_IDS", ""))
        self.admin_ids: List[int] = []
        self.channel_bindings: List[Dict[str, Any]] = []

        self.load()
        self.admin_ids = [admin_id for admin_id in self.admin_ids if admin_id not in self.root_admin_ids]
        logger.info(
            "Config initialized: bindings=%s root_admins=%s config_admins=%s chat_mute_enabled=%s quiet_hours=%s",
            len(self.channel_bindings),
            self.root_admin_ids,
            self.admin_ids,
            self.chat_mute_enabled,
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
            self.comments_message_button_text = data.get(
                "comments_message_button_text", self.comments_message_button_text
            )
            self.quiet_hours = data.get("quiet_hours", self.quiet_hours)
            self.chat_mute_enabled = bool(data.get("chat_mute_enabled", self.chat_mute_enabled))
            self.admin_ids = parse_admin_ids(data.get("admin_ids", self.admin_ids))
            self.admin_ids = [admin_id for admin_id in self.admin_ids if admin_id not in self.root_admin_ids]
            self.channel_bindings = self._load_channel_bindings(data)
            logger.info("Config loaded from file.")
        except Exception as e:
            logger.error("Failed to load config file: %s", e)

    def _load_channel_bindings(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        raw = data.get("channel_bindings")
        if isinstance(raw, list) and raw:
            out: List[Dict[str, Any]] = []
            for item in raw:
                if not isinstance(item, dict):
                    continue
                try:
                    cid = int(item["channel_id"])
                    ccid = int(item["comments_chat_id"])
                    link = str(item.get("comments_chat_link", "")).strip()
                except (KeyError, TypeError, ValueError):
                    continue
                if not link:
                    continue
                out.append(
                    {
                        "channel_id": cid,
                        "comments_chat_id": ccid,
                        "comments_chat_link": link,
                        "channel_title": (item.get("channel_title") or "") or None,
                        "comments_chat_title": (item.get("comments_chat_title") or "") or None,
                    }
                )
            return out
        legacy_ch = data.get("channel_id")
        legacy_cc = data.get("comments_chat_id")
        legacy_link = data.get("comments_chat_link", "")
        try:
            if legacy_ch is not None and legacy_cc is not None and legacy_link:
                cid = int(legacy_ch)
                ccid = int(legacy_cc)
                link = str(legacy_link).strip()
                if link:
                    return [
                        {
                            "channel_id": cid,
                            "comments_chat_id": ccid,
                            "comments_chat_link": link,
                            "channel_title": None,
                            "comments_chat_title": None,
                        }
                    ]
        except (TypeError, ValueError):
            pass
        return []

    def binding_for_channel(self, channel_id: int) -> Optional[Dict[str, Any]]:
        for b in self.channel_bindings:
            if int(b["channel_id"]) == int(channel_id):
                return b
        return None

    def all_channel_ids(self) -> set[int]:
        return {int(b["channel_id"]) for b in self.channel_bindings}

    def all_comments_chat_ids(self) -> set[int]:
        return {int(b["comments_chat_id"]) for b in self.channel_bindings}

    def save(self) -> None:
        try:
            with open(self.filename, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "ad_text": self.ad_text,
                        "ad_url": self.ad_url,
                        "comments_chat_text": self.comments_chat_text,
                        "comments_message_button_text": self.comments_message_button_text,
                        "channel_bindings": self.channel_bindings,
                        "admin_ids": self.admin_ids,
                        "chat_mute_enabled": self.chat_mute_enabled,
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
        self.channel_bind_draft: Dict[int, Dict[str, Any]] = {}

    def is_admin(self, user_id: int | None) -> bool:
        return user_id is not None and (
            user_id in self.config.root_admin_ids or user_id in self.config.admin_ids
        )

    def in_quiet_hours(self) -> bool:
        return self.config.chat_mute_enabled and is_time_in_range(
            datetime.now(MOSCOW_TZ).time(),
            self.config.quiet_hours,
        )

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

    async def fetch_chat_by_id(self, chat_id: int) -> Optional[Dict[str, Any]]:
        try:
            r = await self.client.get(f"/chats/{chat_id}")
            if r.status_code != 200:
                logger.warning("GET /chats/%s -> %s %s", chat_id, r.status_code, r.text)
                return None
            data = r.json()
            return data if isinstance(data, dict) else None
        except Exception as e:
            logger.error("fetch_chat_by_id %s: %s", chat_id, e)
            return None

    async def find_chat_by_invite_url(self, url: str) -> tuple[Optional[int], Optional[Dict[str, Any]], str]:
        norm = normalize_max_url(url)
        token = extract_join_token(norm)
        marker: int | None = None
        while True:
            params: Dict[str, Any] = {"count": 100}
            if marker is not None:
                params["marker"] = marker
            try:
                r = await self.client.get("/chats", params=params)
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                return None, None, f"Не удалось получить список чатов: {e}"
            chats = data.get("chats") or []
            if not isinstance(chats, list):
                chats = []
            for c in chats:
                if not isinstance(c, dict):
                    continue
                cid = c.get("chat_id")
                clink = (c.get("link") or "").strip()
                if clink and links_match(clink, norm):
                    return int(cid) if cid is not None else None, c, ""
                if token and clink:
                    if extract_join_token(clink) == token:
                        return int(cid) if cid is not None else None, c, ""
            next_m = data.get("marker")
            if next_m is None or not chats:
                break
            try:
                marker = int(next_m)
            except (TypeError, ValueError):
                break
        return None, None, (
            "Чат не найден среди чатов бота. Добавьте бота в канал/чат по этой ссылке, "
            "затем повторите ввод."
        )

    async def resolve_chat_from_input(self, text: str) -> tuple[Optional[int], Optional[Dict[str, Any]], str]:
        raw = text.strip()
        if not raw:
            return None, None, "Пустой ввод."
        maybe_id = try_parse_chat_id_from_text(raw)
        if maybe_id is not None:
            info = await self.fetch_chat_by_id(maybe_id)
            if info:
                return maybe_id, info, ""
            return None, None, f"Чат с id={maybe_id} не найден или бот не состоит в нём."
        if not raw.startswith("http"):
            raw = "https://" + raw.lstrip("/")
        return await self.find_chat_by_invite_url(raw)

    async def get_bot_membership(self, chat_id: int) -> tuple[Optional[Dict[str, Any]], str]:
        try:
            r = await self.client.get(f"/chats/{chat_id}/members/me")
            if r.status_code != 200:
                return None, f"Не удалось проверить права бота (HTTP {r.status_code})."
            data = r.json()
            return (data if isinstance(data, dict) else None), ""
        except Exception as e:
            return None, str(e)

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

        if chat_id is not None and chat_id in self.config.all_channel_ids():
            await self.process_channel_post(msg)
            return

        if (
            chat_id is not None
            and chat_id in self.config.all_comments_chat_ids()
            and message_id
            and self.in_quiet_hours()
        ):
            logger.info("Deleting message %s due to quiet hours %s", message_id, self.config.quiet_hours)
            await self.delete_message(message_id)
            return

        if self.is_admin(sender_id):
            public_ids = self.config.all_channel_ids() | self.config.all_comments_chat_ids()
            is_not_public = chat_id not in public_ids
            if is_not_public:
                await self.process_admin_message(msg)

    async def process_channel_post(self, msg: Dict[str, Any]) -> None:
        message_id = msg.get("body", {}).get("mid")
        recipient = msg.get("recipient", {})
        raw_chat_id = recipient.get("chat_id") or recipient.get("chat", {}).get("chat_id")
        channel_id = int(raw_chat_id) if raw_chat_id is not None else None
        binding = self.config.binding_for_channel(channel_id) if channel_id is not None else None
        if not binding:
            logger.warning("No channel binding for chat_id=%s", channel_id)
            return

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

        comments_chat_id = int(binding["comments_chat_id"])
        comments_invite_link = str(binding.get("comments_chat_link", "")).strip()

        short_message_id = ""
        if comments_chat_id:
            copy_attachments = list(clean_attachments)
            ad_buttons = self.get_standard_buttons(include_ad=True)
            if ad_buttons:
                copy_attachments.append({"type": "inline_keyboard", "payload": {"buttons": ad_buttons}})

            forwarded = await self.send_message(comments_chat_id, text, copy_attachments)
            if forwarded:
                body = forwarded.get("body", {})
                short_message_id = get_short_id(body.get("seq")) or str(body.get("mid")).split(".")[-1]

        message_link = ""
        if comments_chat_id and short_message_id:
            message_link = f"https://max.ru/c/{comments_chat_id}/{short_message_id}"

        channel_buttons_row: List[Dict] = []
        if comments_invite_link:
            channel_buttons_row.append(
                {"type": "link", "text": self.config.comments_chat_text, "url": comments_invite_link}
            )
        if message_link and (self.config.comments_message_button_text or "").strip():
            channel_buttons_row.append(
                {
                    "type": "link",
                    "text": self.config.comments_message_button_text.strip(),
                    "url": message_link,
                }
            )

        channel_attachments = list(clean_attachments)
        if channel_buttons_row:
            channel_attachments.append(
                {"type": "inline_keyboard", "payload": {"buttons": [channel_buttons_row]}}
            )

        if message_id:
            await self.edit_message(message_id, text, channel_attachments)

    async def process_admin_message(self, msg: Dict[str, Any]) -> None:
        raw_sid = msg.get("sender", {}).get("user_id")
        if raw_sid is None:
            return
        sender_id = int(raw_sid)
        text = (msg.get("body", {}).get("text") or "").strip()
        state = self.admin_states.get(sender_id, AdminState.NONE)

        if text.lower() == "/admin":
            self.admin_states[sender_id] = AdminState.NONE
            self.channel_bind_draft.pop(sender_id, None)
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

        if state == AdminState.AWAITING_BIND_CHANNEL_INVITE:
            cid, info, err = await self.resolve_chat_from_input(text)
            if err or cid is None:
                await self.send_message(sender_id, err or "Не удалось определить канал.")
                return
            mem, merr = await self.get_bot_membership(cid)
            if merr or not mem:
                await self.send_message(sender_id, merr or "Ошибка проверки прав бота.")
                return
            if not channel_admin_permissions_ok(mem):
                await self.send_message(
                    sender_id,
                    "Бот должен быть администратором канала с правом редактировать сообщения."
                    " (или владельцем канала)",
                )
                return
            for b in self.config.channel_bindings:
                if int(b["channel_id"]) == cid:
                    await self.send_message(sender_id, "Этот канал уже подключён. Удалите запись в меню «Каналы» перед повторной привязкой.")
                    self.admin_states[sender_id] = AdminState.NONE
                    self.channel_bind_draft.pop(sender_id, None)
                    await self.send_channels_submenu(sender_id)
                    return
            title = (info or {}).get("title") if info else None
            self.channel_bind_draft[sender_id] = {
                "channel_id": cid,
                "channel_title": title,
            }
            self.admin_states[sender_id] = AdminState.AWAITING_BIND_COMMENTS_INVITE
            await self.send_message(
                sender_id,
                "Канал принят. Теперь отправьте ссылку-приглашение в чат комментариев "
                "(или числовой chat_id чата). Бот должен быть администратором с правами "
                "писать и удалять сообщения.",
            )
            return

        if state == AdminState.AWAITING_BIND_COMMENTS_INVITE:
            draft = self.channel_bind_draft.get(sender_id)
            if not draft or "channel_id" not in draft:
                self.admin_states[sender_id] = AdminState.NONE
                await self.send_message(sender_id, "Сессия добавления канала сброшена. Начните снова из меню «Каналы».")
                await self.send_channels_submenu(sender_id)
                return
            ccid, cinfo, err = await self.resolve_chat_from_input(text)
            if err or ccid is None:
                await self.send_message(sender_id, err or "Не удалось определить чат.")
                return
            if ccid == int(draft["channel_id"]):
                await self.send_message(sender_id, "Чат комментариев не должен совпадать с каналом. Укажите другой чат.")
                return
            mem, merr = await self.get_bot_membership(ccid)
            if merr or not mem:
                await self.send_message(sender_id, merr or "Ошибка проверки прав бота.")
                return
            if not comments_chat_admin_permissions_ok(mem):
                await self.send_message(
                    sender_id,
                    "Бот должен быть администратором чата с правами писать и удалять сообщения (или владельцем).",
                )
                return
            t = text.strip()
            if try_parse_chat_id_from_text(t) is not None:
                invite_url = ((cinfo or {}).get("link") or "").strip()
            else:
                invite_url = normalize_max_url(t if t.startswith("http") else "https://" + t.lstrip("/"))
            if not invite_url:
                await self.send_message(
                    sender_id,
                    "Не удалось сохранить ссылку-приглашение: пришлите полную https-ссылку из приглашения в чат "
                    "(или chat_id, если у чата есть публичная ссылка в данных API).",
                )
                return
            ch_title = (cinfo or {}).get("title") if cinfo else None
            new_binding = {
                "channel_id": int(draft["channel_id"]),
                "comments_chat_id": ccid,
                "comments_chat_link": invite_url,
                "channel_title": draft.get("channel_title") or None,
                "comments_chat_title": ch_title or None,
            }
            self.config.channel_bindings.append(new_binding)
            self.config.save()
            self.channel_bind_draft.pop(sender_id, None)
            self.admin_states[sender_id] = AdminState.NONE
            await self.send_message(sender_id, "Канал и чат комментариев подключены.")
            await self.send_channels_submenu(sender_id)
            return

        if state == AdminState.AWAITING_COMMENTS_MESSAGE_BUTTON_TEXT:
            self.config.comments_message_button_text = text
            self.config.save()
            self.admin_states[sender_id] = AdminState.NONE
            await self.send_message(sender_id, f"Текст кнопки к сообщению изменен: {text}")
            await self.send_chat_link_submenu(sender_id)
            return

        if state == AdminState.AWAITING_NEW_ADMIN:
            try:
                new_admin_id = int(text)
            except ValueError:
                await self.send_message(sender_id, "Нужно отправить только числовой user_id нового админа.")
                return
            if new_admin_id in self.config.root_admin_ids:
                self.admin_states[sender_id] = AdminState.NONE
                await self.send_message(sender_id, "Этот админ уже задан в .env и не входит в управляемый список.")
                await self.send_admins_submenu(sender_id)
                return
            self.config.admin_ids = sorted(set(self.config.admin_ids + [new_admin_id]))
            self.config.save()
            self.admin_states[sender_id] = AdminState.NONE
            await self.send_message(sender_id, f"Админ добавлен: {new_admin_id}")
            await self.send_admins_submenu(sender_id)
            return

        if state == AdminState.AWAITING_MUTE_RANGE:
            try:
                self.config.quiet_hours = normalize_quiet_hours(text)
            except ValueError:
                await self.send_message(sender_id, "Формат: HH:MM-HH:MM, например 12:00-14:00 или 21:33-07:00")
                return
            self.config.save()
            self.admin_states[sender_id] = AdminState.NONE
            await self.send_message(sender_id, f"Диапазон Mute обновлен: {self.config.quiet_hours} (МСК)")
            await self.send_chat_mute_submenu(sender_id)

    async def send_admin_menu(self, user_id: int) -> None:
        buttons = [
            [{"type": "callback", "text": "Каналы", "payload": "admin_channels_submenu"}],
            [{"type": "callback", "text": "Рекламная ссылка", "payload": "admin_ad_submenu"}],
            [{"type": "callback", "text": "Кнопки в посте", "payload": "admin_chat_link_submenu"}],
            [{"type": "callback", "text": "Админы", "payload": "admin_admins_submenu"}],
            [{"type": "callback", "text": "Mute чата", "payload": "admin_chat_mute_submenu"}],
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
            [{"type": "callback", "text": "Текст: вход в чат", "payload": "admin_set_chat_text"}],
            [{"type": "callback", "text": "Текст: к сообщению", "payload": "admin_set_comments_message_button_text"}],
            [{"type": "callback", "text": "Назад", "payload": "admin_menu"}],
        ]
        text = (
            "Кнопки под постом в канале (одинаковые для всех подключённых каналов).\n"
            "Ссылку-приглашение в чат комментариев для каждого канала задаёте в разделе «Каналы».\n\n"
            f"Текст кнопки входа в чат: {self.config.comments_chat_text}\n"
            f"Текст кнопки к сообщению: {self.config.comments_message_button_text}"
        )
        await self.send_message(user_id, text, [{"type": "inline_keyboard", "payload": {"buttons": buttons}}])

    async def send_channels_submenu(self, user_id: int) -> None:
        bindings = self.config.channel_bindings
        lines = ["Подключённые каналы (канал → чат комментариев)."]
        if not bindings:
            lines.append("Пока ничего не подключено — нажмите «Добавить канал».")
        else:
            for i, b in enumerate(bindings, start=1):
                cid = b["channel_id"]
                ccid = b["comments_chat_id"]
                ct = b.get("channel_title") or f"id {cid}"
                cct = b.get("comments_chat_title") or f"id {ccid}"
                lines.append(f"{i}. {ct} ({cid}) → {cct} ({ccid})")
        text = "\n".join(lines)
        buttons: List[List[Dict]] = [[{"type": "callback", "text": "Добавить канал", "payload": "admin_add_channel_start"}]]
        for b in bindings:
            cid = int(b["channel_id"])
            label = str(b.get("channel_title") or f"Канал {cid}")[:40]
            buttons.append(
                [{"type": "callback", "text": f"Удалить: {label}", "payload": f"admin_remove_channel:{cid}"}]
            )
        buttons.append([{"type": "callback", "text": "Назад", "payload": "admin_menu"}])
        await self.send_message(user_id, text, [{"type": "inline_keyboard", "payload": {"buttons": buttons}}])

    async def send_admins_submenu(self, user_id: int) -> None:
        admins_text = ", ".join(str(admin_id) for admin_id in self.config.admin_ids) or "-"
        buttons = [
            [{"type": "callback", "text": "Добавить админа", "payload": "admin_add_admin"}],
        ]
        for admin_id in self.config.admin_ids:
            buttons.append(
                [{"type": "callback", "text": f"Удалить {admin_id}", "payload": f"admin_remove_admin:{admin_id}"}]
            )
        buttons.append([{"type": "callback", "text": "Назад", "payload": "admin_menu"}])
        text = f"Админы\nДобавленные: {admins_text}"
        await self.send_message(user_id, text, [{"type": "inline_keyboard", "payload": {"buttons": buttons}}])

    async def send_chat_mute_submenu(self, user_id: int) -> None:
        toggle_text = "Выключить Mute" if self.config.chat_mute_enabled else "Включить Mute"
        buttons = [
            [{"type": "callback", "text": toggle_text, "payload": "admin_toggle_chat_mute"}],
            [{"type": "callback", "text": "Изменить диапазон", "payload": "admin_set_mute_range"}],
            [{"type": "callback", "text": "Назад", "payload": "admin_menu"}],
        ]
        current = self.config.quiet_hours or "не настроены"
        text = (
            "Mute чата\n"
            f"Статус: {'включен' if self.config.chat_mute_enabled else 'выключен'}\n"
            f"Диапазон: {current}\n"
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
        elif payload == "admin_channels_submenu":
            await self.send_channels_submenu(sender_id)
        elif payload == "admin_add_channel_start":
            self.channel_bind_draft.pop(sender_id, None)
            self.admin_states[sender_id] = AdminState.AWAITING_BIND_CHANNEL_INVITE
            await self.send_message(
                sender_id,
                "Отправьте ссылку-приглашение в канал или числовой chat_id канала.\n"
                "Бот уже должен быть в канале администратором с правом редактировать сообщения.",
            )
        elif isinstance(payload, str) and payload.startswith("admin_remove_channel:"):
            raw_id = payload.split(":", 1)[1]
            try:
                remove_cid = int(raw_id)
            except ValueError:
                await self.send_message(sender_id, "Некорректный id канала.")
                return
            before = len(self.config.channel_bindings)
            self.config.channel_bindings = [b for b in self.config.channel_bindings if int(b["channel_id"]) != remove_cid]
            if len(self.config.channel_bindings) == before:
                await self.send_message(sender_id, "Такой привязки не найдено.")
            else:
                self.config.save()
                await self.send_message(sender_id, "Привязка канала удалена.")
            await self.send_channels_submenu(sender_id)
        elif payload == "admin_admins_submenu":
            await self.send_admins_submenu(sender_id)
        elif payload == "admin_chat_mute_submenu":
            await self.send_chat_mute_submenu(sender_id)
        elif payload == "admin_set_text":
            self.admin_states[sender_id] = AdminState.AWAITING_AD_TEXT
            await self.send_message(sender_id, "Введите новый текст рекламной кнопки:")
        elif payload == "admin_set_link":
            self.admin_states[sender_id] = AdminState.AWAITING_AD_LINK
            await self.send_message(sender_id, "Введите новую ссылку рекламы:")
        elif payload == "admin_set_chat_text":
            self.admin_states[sender_id] = AdminState.AWAITING_CHAT_TEXT
            await self.send_message(sender_id, "Введите новый текст кнопки входа в чат комментариев:")
        elif payload == "admin_set_comments_message_button_text":
            self.admin_states[sender_id] = AdminState.AWAITING_COMMENTS_MESSAGE_BUTTON_TEXT
            await self.send_message(
                sender_id,
                "Введите новый текст кнопки, которая ведёт к конкретному сообщению в чате комментариев:",
            )
        elif payload == "admin_add_admin":
            self.admin_states[sender_id] = AdminState.AWAITING_NEW_ADMIN
            await self.send_message(sender_id, "Введите user_id нового админа:")
        elif payload == "admin_toggle_chat_mute":
            self.config.chat_mute_enabled = not self.config.chat_mute_enabled
            self.config.save()
            await self.send_message(
                sender_id,
                f"Mute чата {'включен' if self.config.chat_mute_enabled else 'выключен'}",
            )
            await self.send_chat_mute_submenu(sender_id)
        elif payload == "admin_set_mute_range":
            self.admin_states[sender_id] = AdminState.AWAITING_MUTE_RANGE
            await self.send_message(sender_id, "Введите диапазон, например 12:00-14:00 или 21:33-07:00")
        elif isinstance(payload, str) and payload.startswith("admin_remove_admin:"):
            raw_admin_id = payload.split(":", 1)[1]
            try:
                remove_admin_id = int(raw_admin_id)
            except ValueError:
                await self.send_message(sender_id, "Некорректный user_id для удаления.")
                return
            self.config.admin_ids = [admin_id for admin_id in self.config.admin_ids if admin_id != remove_admin_id]
            self.config.save()
            await self.send_message(sender_id, f"Админ удален: {remove_admin_id}")
            await self.send_admins_submenu(sender_id)

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
