"""
mail.tm minimal client.

mail.tm: бесплатный disposable email с публичным api.
docs: https://docs.mail.tm/
"""
from __future__ import annotations

import random
import string
import time
from dataclasses import dataclass
from typing import Optional

import httpx


BASE = "https://api.mail.tm"


@dataclass
class MailAccount:
    address: str
    password: str
    account_id: str
    token: str


def _rand(length: int, alphabet: str = string.ascii_lowercase + string.digits) -> str:
    return "".join(random.choices(alphabet, k=length))


def get_domain(client: httpx.Client) -> str:
    """берём активный домен mail.tm. иногда они переключают, поэтому динамически."""
    r = client.get(f"{BASE}/domains")
    r.raise_for_status()
    data = r.json()
    # формат: {"hydra:member":[{"domain":"..."}]}
    members = data.get("hydra:member") or data.get("member") or []
    for d in members:
        if d.get("isActive", True):
            return d["domain"]
    raise RuntimeError("mail.tm: no active domain")


def create_account(
    client: httpx.Client,
    domain: Optional[str] = None,
    local: Optional[str] = None,
    password: Optional[str] = None,
) -> MailAccount:
    """регает новый ящик и сразу логинится."""
    if domain is None:
        domain = get_domain(client)
    if local is None:
        local = _rand(12)
    if password is None:
        password = _rand(16) + "A1!"
    address = f"{local}@{domain}"

    r = client.post(
        f"{BASE}/accounts",
        json={"address": address, "password": password},
    )
    if r.status_code in (400, 422):
        # коллизия адреса — повторим раз
        return create_account(client)
    r.raise_for_status()
    acc_id = r.json()["id"]

    # сразу логинимся
    r = client.post(
        f"{BASE}/token",
        json={"address": address, "password": password},
    )
    r.raise_for_status()
    token = r.json()["token"]

    return MailAccount(address=address, password=password, account_id=acc_id, token=token)


def wait_for_message(
    client: httpx.Client,
    account: MailAccount,
    subject_contains: Optional[str] = None,
    from_contains: Optional[str] = None,
    timeout: float = 90.0,
    poll_interval: float = 3.0,
) -> dict:
    """полл inbox пока не появится подходящее письмо. возвращает полный объект письма (с body)."""
    headers = {"Authorization": f"Bearer {account.token}"}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = client.get(f"{BASE}/messages", headers=headers)
        if r.status_code == 200:
            for m in r.json().get("hydra:member", []):
                subj = m.get("subject", "")
                frm = (m.get("from") or {}).get("address", "")
                if subject_contains and subject_contains.lower() not in subj.lower():
                    continue
                if from_contains and from_contains.lower() not in frm.lower():
                    continue
                # тянем тело письма
                rr = client.get(f"{BASE}/messages/{m['id']}", headers=headers)
                rr.raise_for_status()
                return rr.json()
        time.sleep(poll_interval)
    raise TimeoutError("mail.tm: message not received in time")


def delete_account(client: httpx.Client, account: MailAccount) -> None:
    headers = {"Authorization": f"Bearer {account.token}"}
    try:
        client.delete(f"{BASE}/accounts/{account.account_id}", headers=headers)
    except Exception:
        pass


if __name__ == "__main__":
    with httpx.Client(timeout=20.0) as c:
        acc = create_account(c)
        print(f"created: {acc.address}")
        print(f"token:   {acc.token[:40]}...")
