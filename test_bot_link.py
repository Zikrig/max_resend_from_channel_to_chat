"""
По токену бота запрашивает GET /me и выводит публичные ссылки на бота в MAX.

Использование:
  set MAX_BOT_TOKEN=...   && python test_bot_link.py
  python test_bot_link.py --token YOUR_TOKEN
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import httpx

API_BASE = "https://platform-api.max.ru"


def main() -> None:
    p = argparse.ArgumentParser(description="Узнать ссылку на бота по токену (GET /me)")
    p.add_argument(
        "--token",
        default=os.environ.get("MAX_BOT_TOKEN", "").strip() or None,
        help="Токен бота (иначе переменная окружения MAX_BOT_TOKEN)",
    )
    args = p.parse_args()
    if not args.token:
        print("Укажите токен: MAX_BOT_TOKEN в окружении или --token ...", file=sys.stderr)
        sys.exit(1)

    headers = {"Authorization": args.token}
    with httpx.Client(base_url=API_BASE, headers=headers, timeout=30.0) as client:
        r = client.get("/me")

    print("HTTP", r.status_code)
    if r.status_code != 200:
        print(r.text)
        sys.exit(1)

    data = r.json()
    print(json.dumps(data, ensure_ascii=False, indent=2))

    username = data.get("username")
    uid = data.get("user_id")
    name = data.get("first_name") or data.get("name")

    print()
    if username:
        u = str(username).lstrip("@")
        print("Ссылка (браузер / приложение):", f"https://max.ru/{u}")
        print("Схема max://:", f"max://max.ru/{u}")
    else:
        print("Поле username пустое — у бота может не быть публичного ника; user_id:", uid)
    if name:
        print("Отображаемое имя:", name)


if __name__ == "__main__":
    main()
