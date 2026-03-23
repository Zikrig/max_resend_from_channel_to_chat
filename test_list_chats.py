"""
Список чатов, в которых участвует бот: GET /chats с пагинацией по marker.

В документации MAX метод описан как «групповые чаты»; каналы и диалоги, если API
их отдаёт в том же списке, будут с другим полем type — скрипт выводит все строки
и при необходимости сводку по type.

Использование:
  set MAX_BOT_TOKEN=...   && python test_list_chats.py
  python test_list_chats.py --token YOUR_TOKEN
  python test_list_chats.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from typing import Any

import httpx

API_BASE = "https://platform-api.max.ru"


def fetch_all_chats(token: str, page_size: int = 100) -> list[dict[str, Any]]:
    headers = {"Authorization": token}
    out: list[dict[str, Any]] = []
    marker: int | None = None

    with httpx.Client(base_url=API_BASE, headers=headers, timeout=60.0) as client:
        while True:
            params: dict[str, Any] = {"count": page_size}
            if marker is not None:
                params["marker"] = marker
            r = client.get("/chats", params=params)
            r.raise_for_status()
            data = r.json()
            batch = data.get("chats") or []
            if not isinstance(batch, list):
                batch = []
            for item in batch:
                if isinstance(item, dict):
                    out.append(item)
            next_marker = data.get("marker")
            if next_marker is None:
                break
            try:
                marker = int(next_marker)
            except (TypeError, ValueError):
                break
            if not batch:
                break

    return out


def main() -> None:
    p = argparse.ArgumentParser(
        description="Список чатов бота (GET /chats)",
    )
    p.add_argument(
        "--token",
        default=os.environ.get("MAX_BOT_TOKEN", "").strip() or None,
        help="Токен бота (иначе MAX_BOT_TOKEN)",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Вывести весь список одним JSON-массивом",
    )
    p.add_argument(
        "--page-size",
        type=int,
        default=100,
        metavar="N",
        help="count на страницу (1–100, по умолчанию 100)",
    )
    args = p.parse_args()
    if not args.token:
        print("Укажите токен: MAX_BOT_TOKEN или --token", file=sys.stderr)
        sys.exit(1)
    size = max(1, min(100, args.page_size))

    try:
        chats = fetch_all_chats(args.token, page_size=size)
    except httpx.HTTPStatusError as e:
        print("HTTP", e.response.status_code, file=sys.stderr)
        print(e.response.text, file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(chats, ensure_ascii=False, indent=2))
        print(f"\n# всего: {len(chats)}", file=sys.stderr)
        return

    types = Counter()
    for c in chats:
        t = c.get("type")
        types[str(t)] += 1

    print(f"Всего записей: {len(chats)}")
    if types:
        print("По полю type:", dict(types))
    print()

    for c in chats:
        cid = c.get("chat_id")
        typ = c.get("type")
        title = c.get("title") or ""
        status = c.get("status")
        link = c.get("link") or ""
        is_pub = c.get("is_public")
        parts = [
            f"chat_id={cid}",
            f"type={typ!r}",
            f"status={status!r}",
            f"title={title!r}",
        ]
        if is_pub is not None:
            parts.append(f"is_public={is_pub}")
        if link:
            parts.append(f"link={link}")
        print(" | ".join(parts))

    print()
    print("Полный JSON каждого чата: python test_list_chats.py --json")


if __name__ == "__main__":
    main()
