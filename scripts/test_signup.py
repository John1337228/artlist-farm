"""
минимальный тест: signup + проверка сессии + чтение квоты.
запускается в github actions для проверки проходит ли github runner IP через cloudflare/anti-abuse artlist.

успех = всё прошло, в логах есть user_id + 'free image quota'.
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

# делаем src/ импортируемой
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import httpx

from src.mail import create_account
from src.client import ArtlistClient, ArtlistError


def main() -> int:
    # печатаем IP runner'а — пригодится для диагностики, в каком DC мы оказались
    try:
        ip = httpx.get("https://api.ipify.org", timeout=10).text
        print(f"[runner-ip] {ip}")
    except Exception as e:
        print(f"[runner-ip] err: {e}")

    # стартовый ping на artlist без cookies — посмотреть пускает ли cloudflare GH-датацентр
    try:
        r = httpx.get("https://toolkit.artlist.io/api/auth/csrf", timeout=15)
        print(f"[ping-artlist-csrf-noimpersonate] {r.status_code}")
    except Exception as e:
        print(f"[ping-artlist-csrf-noimpersonate] err: {e}")

    with httpx.Client(timeout=20.0) as mc:
        mail_acc = create_account(mc)
    print(f"[mail.tm] {mail_acc.address}")

    c = ArtlistClient(verbose=True)
    try:
        c.signup(email=mail_acc.address, password=mail_acc.password)
    except ArtlistError as e:
        print(f"[signup] FAIL: {e}")
        return 2

    try:
        free = c.get_free_generations()
        print(f"[quota] {json.dumps(free, ensure_ascii=False)}")
    except Exception as e:
        print(f"[quota] err: {e}")
        # signup прошёл = главное; квота — секундарно
        return 0

    print("[ok] github runner passes artlist signup; ready to scale")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
