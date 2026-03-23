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

class Config:
    def __init__(self, filename: str):
        self.filename = filename
        self.ad_text = "Реклама"
        self.ad_url = "https://max.ru"
        self.channel_id = 0
        self.comments_chat_id = 0
        self.comments_chat_link = ""
        self.admin_ids = []
        self.load()

    def load(self):
        if os.path.exists(self.filename):
            try:
                with open(self.filename, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.ad_text = data.get("ad_text", self.ad_text)
                    self.ad_url = data.get("ad_url", self.ad_url)
                    self.channel_id = int(data.get("channel_id", 0))
                    self.comments_chat_id = int(data.get("comments_chat_id", 0))
                    self.comments_chat_link = data.get("comments_chat_link", "")
                    self.admin_ids = data.get("admin_ids", [])
            except Exception as e:
                logger.error(f"Failed to load config: {e}")

    def save(self):
        try:
            with open(self.filename, "w", encoding="utf-8") as f:
                json.dump({
                    "ad_text": self.ad_text,
                    "ad_url": self.ad_url,
                    "channel_id": self.channel_id,
                    "comments_chat_id": self.comments_chat_id,
                    "comments_chat_link": self.comments_chat_link,
                    "admin_ids": self.admin_ids
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Failed to save config: {e}")

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
            
            r = await self.client.post("/messages", params={"chat_id": chat_id}, json=payload)
            r.raise_for_status()
            return r.json().get("message")
        except Exception as e:
            logger.error(f"Failed to send message to {chat_id}: {e}")
            return None

    async def edit_message(self, message_id: str, text: str, attachments: Optional[List[Dict]] = None) -> bool:
        try:
            payload = {"text": text}
            if attachments is not None:
                payload["attachments"] = attachments
            
            r = await self.client.put("/messages", params={"message_id": message_id}, json=payload)
            r.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Failed to edit message {message_id}: {e}")
            return False

    async def pin_message(self, chat_id: int, message_id: str):
        try:
            r = await self.client.put(f"/chats/{chat_id}/pin", json={"message_id": message_id, "notify": True})
            r.raise_for_status()
        except Exception as e:
            logger.error(f"Failed to pin message {message_id} in {chat_id}: {e}")

    def get_standard_buttons(self, include_comments: bool = True) -> List[List[Dict]]:
        buttons = []
        row = []
        
        # Кнопка рекламы
        if self.config.ad_text and self.config.ad_url:
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
        sender_id = sender.get("user_id")
        
        recipient = msg.get("recipient", {})
        # Проверяем возможные места нахождения chat_id
        chat_id = recipient.get("chat_id") or recipient.get("chat", {}).get("chat_id")
        chat_type = recipient.get("type") or recipient.get("chat", {}).get("type")
        
        logger.info(f"New message: mid={msg.get('body', {}).get('mid')}, sender={sender_id}, chat_id={chat_id}, chat_type={chat_type}")

        # Игнорируем свои сообщения
        if sender_id == self.bot_id:
            logger.debug("Ignoring message from self.")
            return
            
        # Если сообщение из канала
        if chat_id == self.config.channel_id:
            logger.info(f"MATCH: Post from channel {chat_id} detected.")
            await self.process_channel_post(msg)
            return
        else:
            logger.debug(f"Message chat_id {chat_id} does not match channel_id {self.config.channel_id}")

        # Если сообщение от админа (в личку боту)
        if sender_id in self.config.admin_ids:
            if not chat_id or chat_type == "user" or chat_id == self.bot_id:
                logger.info(f"Admin command from {sender_id} detected.")
                await self.process_admin_message(msg)

    async def process_channel_post(self, msg: Dict[str, Any]):
        mid = msg.get("body", {}).get("mid")
        text = msg.get("body", {}).get("text") or ""
        atts = msg.get("body", {}).get("attachments") or []
        
        # 1. Добавляем кнопки к посту в канале
        new_atts = list(atts)
        standard_buttons = self.get_standard_buttons(include_comments=True)
        if standard_buttons:
            new_atts.append({
                "type": "inline_keyboard",
                "payload": {"buttons": standard_buttons}
            })
        
        # Редактируем пост в канале
        await self.edit_message(mid, text, new_atts)
        logger.info(f"Post {mid} in channel edited with buttons.")

        # 2. Пересылаем в чат комментариев
        if self.config.comments_chat_id:
            # Формируем вложения для копии (без кнопки комменты, но с рекламой)
            copy_atts = list(atts)
            ad_only_buttons = self.get_standard_buttons(include_comments=False)
            if ad_only_buttons:
                copy_atts.append({
                    "type": "inline_keyboard",
                    "payload": {"buttons": ad_only_buttons}
                })
            
            # Вставляем ссылку на оригинал в начало текста, если нужно, или просто текст
            # Для "пересылки" в MAX обычно просто создаем новое сообщение
            new_msg = await self.send_message(self.config.comments_chat_id, text, copy_atts)
            
            if new_msg:
                new_mid = new_msg.get("body", {}).get("mid")
                # 3. Закрепляем в чате комментариев
                await self.pin_message(self.config.comments_chat_id, new_mid)
                logger.info(f"Post forwarded to comments chat and pinned: {new_mid}")

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
            await self.send_admin_menu(sender_id)
            
        elif state == AdminState.AWAITING_AD_LINK:
            if not (text.startswith("http://") or text.startswith("https://")):
                await self.send_message(sender_id, "❌ Ошибка: Ссылка должна начинаться с http:// или https://. Попробуйте еще раз:")
                return
            self.config.ad_url = text
            self.config.save()
            self.admin_states[sender_id] = AdminState.NONE
            await self.send_message(sender_id, f"✅ Ссылка рекламы изменена на: {text}")
            await self.send_admin_menu(sender_id)

    async def send_admin_menu(self, user_id: int):
        buttons = [
            [
                {"type": "callback", "text": "📝 Изменить текст", "payload": "admin_set_text"},
                {"type": "callback", "text": "🔗 Изменить ссылку", "payload": "admin_set_link"}
            ],
            [
                {"type": "callback", "text": "🔙 Назад", "payload": "admin_close"}
            ]
        ]
        text = (
            "🛠 **Админ-панель**\n\n"
            f"Текущий текст: `{self.config.ad_text}`\n"
            f"Текущая ссылка: `{self.config.ad_url}`\n\n"
            "Выберите действие:"
        )
        await self.send_message(user_id, text, [{
            "type": "inline_keyboard",
            "payload": {"buttons": buttons}
        }])

    async def on_callback(self, update: Dict[str, Any]):
        payload = update.get("payload")
        sender_id = update.get("sender", {}).get("user_id")
        
        if sender_id not in self.config.admin_ids:
            return

        if payload == "admin_set_text":
            self.admin_states[sender_id] = AdminState.AWAITING_AD_TEXT
            await self.send_message(sender_id, "Введите новый текст для рекламной кнопки:")
            
        elif payload == "admin_set_link":
            self.admin_states[sender_id] = AdminState.AWAITING_AD_LINK
            await self.send_message(sender_id, "Введите новую URL-ссылку для рекламы (с http/https):")
            
        elif payload == "admin_close":
            self.admin_states[sender_id] = AdminState.NONE
            await self.send_message(sender_id, "Админ-панель закрыта. Для вызова используйте /admin")

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
    
    # Приоритет переменным из окружения, если в файле пусто
    env_channel = os.environ.get("CHANNEL_CHAT_ID", "0")
    if not config.channel_id and env_channel != "0":
        config.channel_id = int(env_channel)
        
    env_comments_id = os.environ.get("COMMENTS_CHAT_ID", "0")
    if not config.comments_chat_id and env_comments_id != "0":
        config.comments_chat_id = int(env_comments_id)
        
    env_comments_link = os.environ.get("COMMENTS_CHAT_LINK", "")
    if not config.comments_chat_link and env_comments_link:
        config.comments_chat_link = env_comments_link
        
    env_admins = os.environ.get("ADMIN_USER_IDS", "")
    if not config.admin_ids and env_admins:
        config.admin_ids = [int(x.strip()) for x in env_admins.split(",") if x.strip()]
    
    config.save()

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
