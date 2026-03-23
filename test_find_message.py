"""
Скрипт для поиска сообщения в канале по тексту и вывода содержимого его клавиатуры.
Использует метод GET /messages?chat_id={channel_id}

Использование:
  python test_find_message.py "текст поста"
  python test_find_message.py --token "..." --chat "-123" "текст"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import httpx

# Попытка загрузить переменные из .env файла
def load_dotenv(path: str = ".env"):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, value = line.split("=", 1)
                    os.environ[key.strip()] = value.strip()

load_dotenv()

API_BASE = "https://platform-api.max.ru"


def find_message_by_text(token: str, chat_id: int, search_text: str, limit: int = 50) -> dict[str, Any] | None:
    headers = {"Authorization": token}
    params = {
        "chat_id": chat_id,
        "count": limit
    }
    
    with httpx.Client(base_url=API_BASE, headers=headers, timeout=30.0) as client:
        r = client.get("/messages", params=params)
        r.raise_for_status()
        data = r.json()
        messages = data.get("messages") or []
        
        for msg in messages:
            body = msg.get("body", {})
            text = body.get("text") or ""
            if search_text.lower() in text.lower():
                return msg
    return None


def main() -> None:
    p = argparse.ArgumentParser(description="Поиск клавиатуры сообщения по тексту")
    p.add_argument("text", help="Текст сообщения (или часть текста) для поиска")
    p.add_argument(
        "--token",
        default=os.environ.get("MAX_BOT_TOKEN", "").strip() or None,
        help="Токен бота (иначе MAX_BOT_TOKEN)",
    )
    p.add_argument(
        "--chat",
        type=int,
        default=int(os.environ.get("CHANNEL_CHAT_ID", 0)),
        help="ID канала (иначе CHANNEL_CHAT_ID)",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Количество последних сообщений для поиска (1-100)",
    )
    
    args = p.parse_args()
    
    if not args.token:
        print("Укажите токен через --token или MAX_BOT_TOKEN", file=sys.stderr)
        sys.exit(1)
    if not args.chat:
        print("Укажите ID канала через --chat или CHANNEL_CHAT_ID", file=sys.stderr)
        sys.exit(1)

    print(f"Поиск сообщения с текстом '{args.text}' в чате {args.chat}...")
    
    try:
        msg = find_message_by_text(args.token, args.chat, args.text, args.limit)
    except httpx.HTTPStatusError as e:
        print(f"Ошибка API: {e.response.status_code} {e.response.text}")
        sys.exit(1)
    except Exception as e:
        print(f"Ошибка: {e}")
        sys.exit(1)

    if not msg:
        print("Сообщение не найдено среди последних", args.limit, "записей.")
        return

    mid = msg.get("body", {}).get("mid")
    print(f"\nНайдено сообщение ID: {mid}")
    print(f"Текст: {msg.get('body', {}).get('text')}")
    
    attachments = msg.get("body", {}).get("attachments") or []
    keyboards = [a for a in attachments if a.get("type") == "inline_keyboard"]
    
    if not keyboards:
        print("\nУ этого сообщения нет клавиатуры.")
    else:
        for i, kb in enumerate(keyboards, 1):
            print(f"\n--- Клавиатура #{i} ---")
            buttons = kb.get("payload", {}).get("buttons", [])
            for row_idx, row in enumerate(buttons):
                print(f"Ряд {row_idx + 1}:")
                for btn in row:
                    b_type = btn.get("type")
                    b_text = btn.get("text")
                    b_val = btn.get("url") or btn.get("payload")
                    print(f"  [{b_text}] ({b_type}) -> {b_val}")

    print("\nПолный JSON сообщения: python test_find_message.py ... --limit 1 (и далее через --json если нужно доработать)")


if __name__ == "__main__":
    main()
