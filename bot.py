"""
MAX-бот: 
1. Добавляет кнопки (Комментарии, Реклама) к постам в канале.
2. Пересылает пост в чат комментариев и закрепляет его там.
3. Управление рекламной ссылкой через /admin.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import base64
import struct
from enum import Enum
from typing import Any, Dict, List, Optional

import httpx

# Настройки логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("MaxBot")

API_BASE = "https://platform-api.max.ru"

class AdminState(Enum):
    NONE = "none"
    AWAITING_AD_TEXT = "awaiting_ad_text"
    AWAITING_AD_LINK = "awaiting_ad_link"

def get_short_id(seq: Any) -> str:
    """Вычисляет короткий ID сообщения (как AZ0a_VPec8o) из поля seq."""
    try:
        if not seq:
            return ""
        # Кодируем 8-байтовое число (Big Endian) в Base64 и убираем лишнее
        b = struct.pack(">Q", int(seq))
        # Используем стандартный Base64, заменяя + на - и / на _ (urlsafe)
        # Убираем паддинг '=' и лидирующие нули (если они есть в байтах)
        short = base64.urlsafe_b64encode(b).decode().rstrip("=")
        # Обычно в MAX короткий ID это именно такая перепаковка seq
        return short
    except Exception:
        return ""

class Config:
    def __init__(self, filename: str = "config.json"):
        self.filename = filename
        # Дефолты из окружения
        self.ad_text = os.environ.get("AD_TEXT", "Реклама")
        self.ad_url = os.environ.get("AD_URL", "https://max.ru")
        self.channel_id = int(os.environ.get("CHANNEL_CHAT_ID", "0"))
        self.comments_chat_id = int(os.environ.get("COMMENTS_CHAT_ID", "0"))
        self.comments_chat_link = os.environ.get("COMMENTS_CHAT_LINK", "")
        
        raw_admins = os.environ.get("ADMIN_USER_IDS", "")
        self.admin_ids = [int(x.strip()) for x in raw_admins.split(",") if x.strip()]
        
        # Загружаем сохраненные настройки, если есть
        self.load()
        logger.info(f"Config initialized: channel={self.channel_id}, comments_chat={self.comments_chat_id}, link_len={len(self.comments_chat_link)}")

    def load(self):
        if os.path.exists(self.filename):
            try:
                with open(self.filename, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.ad_text = data.get("ad_text", self.ad_text)
                    self.ad_url = data.get("ad_url", self.ad_url)
                    logger.info("Config loaded from file.")
            except Exception as e:
                logger.error(f"Failed to load config file: {e}")

    def save(self):
        try:
            with open(self.filename, "w", encoding="utf-8") as f:
                json.dump({
                    "ad_text": self.ad_text,
                    "ad_url": self.ad_url
                }, f, ensure_ascii=False, indent=2)
                logger.info("Config saved to file.")
        except Exception as e:
            logger.error(f"Failed to save config file: {e}")

class MaxBot:
    def __init__(self, token: str, config: Config):
        self.token = token
        self.config = config
        self.headers = {"Authorization": self.token}
        self.client = httpx.AsyncClient(base_url=API_BASE, headers=self.headers, timeout=60.0)
        self.bot_id = None
        self.admin_states: Dict[int, AdminState] = {}

    async def get_me(self):
        try:
            r = await self.client.get("/me")
            r.raise_for_status()
            data = r.json()
            self.bot_id = data.get("user_id")
            logger.info(f"Logged in as bot ID {self.bot_id} (@{data.get('username')})")
        except Exception as e:
            logger.critical(f"Failed to get bot info: {e}")
            sys.exit(1)

    async def send_message(self, chat_id: int, text: str, attachments: Optional[List[Dict]] = None) -> Optional[Dict]:
        try:
            payload = {"text": text}
            if attachments:
                payload["attachments"] = attachments
            
            # В MAX для лички используется user_id, для групп/каналов - chat_id
            params = {}
            if chat_id > 0:
                params["user_id"] = chat_id
            else:
                params["chat_id"] = chat_id
            
            r = await self.client.post("/messages", params=params, json=payload)
            r.raise_for_status()
            return r.json().get("message")
        except Exception as e:
            logger.error(f"Failed to send message to {chat_id} (params={params}): {e}")
            return None

    async def edit_message(self, message_id: str, text: str, attachments: Optional[List[Dict]] = None) -> bool:
        try:
            payload = {"text": text}
            if attachments is not None:
                payload["attachments"] = attachments
            
            r = await self.client.put("/messages", params={"message_id": message_id}, json=payload)
            if r.status_code != 200:
                logger.error(f"Edit failed: {r.status_code} {r.text}")
            r.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Failed to edit message {message_id}: {e}")
            return False

    def get_standard_buttons(self, include_comments: bool = True, include_ad: bool = True) -> List[List[Dict]]:
        buttons = []
        row = []
        
        # Кнопка рекламы
        if include_ad and self.config.ad_text and self.config.ad_url:
            row.append({
                "type": "link",
                "text": self.config.ad_text,
                "url": self.config.ad_url
            })
            
        # Кнопка комментариев
        if include_comments and self.config.comments_chat_link:
            row.append({
                "type": "link",
                "text": "💬 Комментарии",
                "url": self.config.comments_chat_link
            })
            
        if row:
            buttons.append(row)
        return buttons

    async def handle_update(self, update: Dict[str, Any]):
        u_type = update.get("update_type")
        logger.debug(f"Received update type: {u_type}")
        
        if u_type == "message_created":
            await self.on_message_created(update.get("message", {}))
        elif u_type == "message_callback":
            await self.on_callback(update)
        else:
            logger.info(f"Skipping update type: {u_type}")

    async def on_message_created(self, msg: Dict[str, Any]):
        sender = msg.get("sender", {})
        raw_sender_id = sender.get("user_id")
        sender_id = int(raw_sender_id) if raw_sender_id is not None else None
        
        recipient = msg.get("recipient", {})
        # В MAX для лички chat_id может быть равен user_id бота
        raw_chat_id = recipient.get("chat_id") or recipient.get("chat", {}).get("chat_id") or recipient.get("user_id")
        chat_id = int(raw_chat_id) if raw_chat_id is not None else None
        
        logger.info(f"New message: mid={msg.get('body', {}).get('mid')}, sender={sender_id}, chat_id={chat_id}")

        # Игнорируем сообщения от самого себя (бота)
        if sender_id and self.bot_id and int(sender_id) == int(self.bot_id):
            return
            
        # 1. Проверка на канал (пересылка и кнопки)
        if chat_id is not None and int(chat_id) == int(self.config.channel_id):
            logger.info(f"MATCH: Post from channel {chat_id} detected.")
            await self.process_channel_post(msg)
            return
        
        # 2. Проверка на админа (команды ТОЛЬКО в личке)
        if sender_id and sender_id in self.config.admin_ids:
            # Если это не канал и не чат комментариев — считаем личкой (для надежности)
            is_not_public = (chat_id != self.config.channel_id) and (chat_id != self.config.comments_chat_id)
            
            if is_not_public:
                logger.info(f"Admin {sender_id} command detected in chat {chat_id}. Processing.")
                await self.process_admin_message(msg)
                return
            else:
                logger.info(f"Admin {sender_id} sent message in public chat/channel. Ignoring command.")

        logger.info(f"No match. Sender {sender_id} not in admins {self.config.admin_ids} or message in channel.")

    async def process_channel_post(self, msg: Dict[str, Any]):
        mid = msg.get("body", {}).get("mid")
        text = msg.get("body", {}).get("text") or ""
        atts = msg.get("body", {}).get("attachments") or []
        
        logger.info(f"Processing new channel post {mid}. Forwarding to comments chat first...")

        # 1. Очищаем вложения от клавиатур и служебных полей GET-запроса
        clean_atts = []
        for a in atts:
            if a.get("type") == "inline_keyboard":
                continue
            p = a.get("payload", {})
            # Оставляем только те поля, которые нужны для переотправки (file_id и т.д.)
            # Удаляем url, size, callback_id, так как они ломают POST/PUT
            new_payload = {k: v for k, v in p.items() if k not in ("callback_id", "url", "size", "width", "height", "duration")}
            clean_atts.append({"type": a.get("type"), "payload": new_payload})

        # 2. Сначала пересылаем в чат комментариев
        new_mid = None
        if self.config.comments_chat_id:
            ad_buttons = self.get_standard_buttons(include_comments=False, include_ad=True)
            copy_atts = list(clean_atts)
            if ad_buttons:
                copy_atts.append({
                    "type": "inline_keyboard",
                    "payload": {"buttons": ad_buttons}
                })
            
            new_msg = await self.send_message(self.config.comments_chat_id, text, copy_atts)
            if new_msg:
                # Логируем полный JSON для анализа полей
                logger.info(f"Forwarded message JSON: {json.dumps(new_msg, ensure_ascii=False)}")
                
                body = new_msg.get("body", {})
                seq = body.get("seq")
                mid_full = body.get("mid")
                
                # Пробуем вычислить короткий ID из seq
                short_id = get_short_id(seq)
                
                # Приоритет: вычисленный short_id, если не вышло - часть mid
                new_mid = short_id or str(mid_full).split(".")[-1]
                
                logger.info(f"Post forwarded to comments chat. seq={seq}, Selected ID: {new_mid}")

        # 3. Редактируем оригинал в канале
        # Формируем ссылку на конкретное сообщение
        msg_link = ""
        if new_mid:
            msg_id_part = str(new_mid).split(".")[-1]
            msg_link = f"https://max.ru/c/{self.config.comments_chat_id}/{msg_id_part}"
        
        # Ссылка на вступление в чат
        join_link = self.config.comments_chat_link

        logger.info(f"Buttons for channel: join={join_link}, msg={msg_link}")

        channel_atts = list(clean_atts)
        buttons_row = []
        
        if join_link:
            buttons_row.append({
                "type": "link",
                "text": "Чат комментариев",
                "url": join_link
            })
            
        if msg_link:
            buttons_row.append({
                "type": "link",
                "text": "💬 Перейти к сообщению",
                "url": msg_link
            })

        if buttons_row:
            channel_atts.append({
                "type": "inline_keyboard",
                "payload": {"buttons": [buttons_row]}
            })
        
        if await self.edit_message(mid, text, channel_atts):
            logger.info(f"Original post {mid} edited with two buttons.")

    async def process_admin_message(self, msg: Dict[str, Any]):
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
            await self.send_message(sender_id, f"✅ Текст рекламы изменен на: {text}")
            await self.send_ad_submenu(sender_id)
            
        elif state == AdminState.AWAITING_AD_LINK:
            if not (text.startswith("http://") or text.startswith("https://")):
                await self.send_message(sender_id, "❌ Ошибка: Ссылка должна начинаться с http:// или https://. Попробуйте еще раз:")
                return
            self.config.ad_url = text
            self.config.save()
            self.admin_states[sender_id] = AdminState.NONE
            await self.send_message(sender_id, f"✅ Ссылка рекламы изменена на: {text}")
            await self.send_ad_submenu(sender_id)

    async def send_admin_menu(self, user_id: int):
        buttons = [
            [{"type": "callback", "text": "🔗 Рекламная ссылка", "payload": "admin_ad_submenu"}]
        ]
        text = "Админ-панель\nВыберите раздел:"
        await self.send_message(user_id, text, [{
            "type": "inline_keyboard",
            "payload": {"buttons": buttons}
        }])

    async def send_ad_submenu(self, user_id: int):
        buttons = [
            [{"type": "callback", "text": "📝 Изменить текст", "payload": "admin_set_text"}],
            [{"type": "callback", "text": "🔗 Изменить ссылку", "payload": "admin_set_link"}],
            [{"type": "callback", "text": "🔙 Назад", "payload": "admin_menu"}]
        ]
        text = (
            "Настройка рекламной ссылки\n\n"
            f"Текущий текст: {self.config.ad_text}\n"
            f"Текущая ссылка: {self.config.ad_url}\n\n"
            "Выберите действие:"
        )
        await self.send_message(user_id, text, [{
            "type": "inline_keyboard",
            "payload": {"buttons": buttons}
        }])

    async def on_callback(self, update: Dict[str, Any]):
        # Логируем весь update для отладки структуры
        logger.debug(f"Callback raw update: {json.dumps(update, ensure_ascii=False)}")
        
        callback_data = update.get("callback", {})
        payload = callback_data.get("payload")
        
        # ID того, кто НАЖАЛ кнопку
        user_data = callback_data.get("user", {})
        sender_id = int(user_data.get("user_id")) if user_data.get("user_id") else None
        
        logger.info(f"Callback parsed: payload={payload}, user_id={sender_id}")

        if not sender_id or sender_id not in self.config.admin_ids:
            logger.warning(f"Unauthorized callback from {sender_id}. Admins are: {self.config.admin_ids}")
            return

        if payload == "admin_menu":
            await self.send_admin_menu(sender_id)

        elif payload == "admin_ad_submenu":
            await self.send_ad_submenu(sender_id)

        elif payload == "admin_set_text":
            self.admin_states[sender_id] = AdminState.AWAITING_AD_TEXT
            await self.send_message(sender_id, "Введите новый текст для рекламной кнопки:")
            
        elif payload == "admin_set_link":
            self.admin_states[sender_id] = AdminState.AWAITING_AD_LINK
            await self.send_message(sender_id, "Введите новую URL-ссылку для рекламы (с http/https):")

    async def run(self):
        await self.get_me()
        marker = None
        logger.info("Bot started polling...")
        
        while True:
            try:
                params = {"limit": 100, "timeout": 30}
                if marker:
                    params["marker"] = marker
                
                r = await self.client.get("/updates", params=params)
                r.raise_for_status()
                data = r.json()
                
                updates = data.get("updates") or []
                for u in updates:
                    if isinstance(u, dict):
                        await self.handle_update(u)
                
                # Обновляем маркер для следующей итерации
                new_marker = data.get("marker")
                if new_marker is not None:
                    marker = new_marker
            except httpx.HTTPError as e:
                logger.error(f"Polling HTTP error ({type(e).__name__}): {e}")
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"Polling error: {e}")
                await asyncio.sleep(5)

async def main():
    token = os.environ.get("MAX_BOT_TOKEN")
    if not token:
        logger.error("MAX_BOT_TOKEN not found in environment!")
        return

    config = Config()
    bot = MaxBot(token, config)
    try:
        await bot.run()
    finally:
        await bot.client.aclose()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
