"""
artlist toolkit api client.

flow:
  1. signup (через NextAuth credentials, type=SIGN_UP)
  2. создаём chatSession
  3. загружаем картинку: getPresignedUrl -> PUT в S3 -> getPresignedUrlFromKey (GET-URL)
  4. modelRouter.getCostQuote -> получаем costQuoteDigitalSignature (JWT)
  5. userGenerationRouter.createUserGeneration -> id генерации
  6. полл getUserGenerationsBySession пока status=succeeded
  7. скачиваем outputUrl

всё реверс-инженеринг из сетевого лога живой сессии (см. session.jsonl).
turnstile на signup присутствует на фронте, но сервер принимает turnstileToken=undefined.
если в будущем включат жёсткую проверку — этот клиент перестанет работать без headless+turnstile-bypass.
"""
from __future__ import annotations

import json
import mimetypes
import random
import re
import string
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx
from curl_cffi import requests as cr


TOOLKIT = "https://toolkit.artlist.io"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)


def _rand_password(n: int = 14) -> str:
    chars = string.ascii_letters + string.digits
    return "".join(random.choices(chars, k=n)) + "A1!"


def _rand_name() -> str:
    return "".join(random.choices(string.ascii_lowercase, k=8))


def _uuid7() -> str:
    """
    uuid v7 (timestamp-prefixed). artlist использует именно v7 для chatSessionId/x-request-id.
    реализация по спеке: 48 бит unix_ts_ms + 4 бита version + 12 бит rand_a + 2 бита var + 62 бита rand_b.
    """
    ts_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand_a = random.getrandbits(12)
    rand_b = random.getrandbits(62)
    msb = (ts_ms << 16) | (0x7 << 12) | rand_a
    lsb = (0b10 << 62) | rand_b
    val = (msb << 64) | lsb
    return str(uuid.UUID(int=val))


@dataclass
class ArtlistAccount:
    email: str
    password: str
    name: str
    user_id: Optional[str] = None
    cookies: dict[str, str] = field(default_factory=dict)


class ArtlistError(Exception):
    pass


class ArtlistClient:
    """
    клиент с curl_cffi под капотом для обхода cloudflare bot management:
    impersonate='chrome131' даёт реальный TLS-fingerprint chrome'а, cf нас не палит.
    """

    IMPERSONATE = "chrome131"

    def __init__(self, *, proxy: Optional[str] = None, ua: str = DEFAULT_UA, verbose: bool = True):
        self.verbose = verbose
        self.proxy = proxy
        self.ua = ua
        self.session = cr.Session(
            impersonate=self.IMPERSONATE,
            timeout=45,
            proxy=proxy,
        )
        # ВАЖНО: origin/referer глобально НЕ ставим — для прямого GET корневой страницы
        # их быть не должно (реальный браузер их не шлёт при навигации),
        # cloudflare ругается на нетипичный паттерн. ставим точечно в trpc/auth-callback.
        self.session.headers.update({
            "accept-language": "en-US,en;q=0.9",
        })
        self.account: Optional[ArtlistAccount] = None
        self._warmed = False

    def log(self, *a):
        if self.verbose:
            print("[artlist]", *a)

    NAV_HEADERS = {
        "accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8"
        ),
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "none",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
    }

    def _warmup(self) -> None:
        """warmup необязателен — __cf_bm выдаётся cloudflare на любой запрос (включая csrf).
        оставлен как no-op для совместимости."""
        self._warmed = True

    def _request(self, method: str, path: str, **kwargs):
        url = path if path.startswith("http") else f"{TOOLKIT}{path}"
        kwargs.setdefault("allow_redirects", True)
        return self.session.request(method, url, **kwargs)

    # ---------------- auth ----------------

    def _csrf(self) -> str:
        self._warmup()
        r = self._request("GET", "/api/auth/csrf")
        if r.status_code != 200:
            raise ArtlistError(f"csrf HTTP {r.status_code}: {r.text[:200]}")
        return r.json()["csrfToken"]

    def signup(self, email: str, password: Optional[str] = None, name: Optional[str] = None) -> ArtlistAccount:
        password = password or _rand_password()
        name = name or _rand_name()
        csrf = self._csrf()
        self.log(f"signup as {email} ...")
        r = self._request(
            "POST",
            "/api/auth/callback/credentials",
            data={
                "type": "SIGN_UP",
                "email": email,
                "name": name,
                "password": password,
                "turnstileToken": "undefined",
                "redirect": "false",
                "csrfToken": csrf,
                "callbackUrl": f"{TOOLKIT}/image-video-generator?mode=image",
                "json": "true",
            },
            headers={
                "content-type": "application/x-www-form-urlencoded",
                "origin": TOOLKIT,
                "referer": f"{TOOLKIT}/",
            },
        )
        if r.status_code != 200:
            raise ArtlistError(f"signup HTTP {r.status_code}: {r.text[:300]}")
        body = r.json()
        if "url" not in body:
            raise ArtlistError(f"signup unexpected: {body}")
        sess_r = self._request("GET", "/api/auth/session")
        sess = sess_r.json()
        if "user" not in sess:
            raise ArtlistError(f"session empty after signup: {sess}")
        user_id = sess["user"]["id"]
        self.log(f"signup ok: user_id={user_id}")
        acc = ArtlistAccount(
            email=email,
            password=password,
            name=name,
            user_id=str(user_id),
            cookies={c.name: c.value for c in self.session.cookies.jar},
        )
        self.account = acc
        return acc

    def login(self, email: str, password: str) -> ArtlistAccount:
        csrf = self._csrf()
        r = self._request(
            "POST",
            "/api/auth/callback/credentials",
            data={
                "type": "SIGN_IN",
                "email": email,
                "password": password,
                "turnstileToken": "undefined",
                "redirect": "false",
                "csrfToken": csrf,
                "callbackUrl": f"{TOOLKIT}/image-video-generator?mode=image",
                "json": "true",
            },
            headers={
                "content-type": "application/x-www-form-urlencoded",
                "origin": TOOLKIT,
                "referer": f"{TOOLKIT}/",
            },
        )
        if r.status_code != 200:
            raise ArtlistError(f"login HTTP {r.status_code}: {r.text[:300]}")
        sess = self._request("GET", "/api/auth/session").json()
        if "user" not in sess:
            raise ArtlistError(f"session empty after login: {sess}")
        acc = ArtlistAccount(
            email=email, password=password, name=sess["user"].get("firstName", ""),
            user_id=str(sess["user"]["id"]),
            cookies={c.name: c.value for c in self.session.cookies.jar},
        )
        self.account = acc
        return acc

    # ---------------- tRPC primitives ----------------

    def trpc_get(self, proc: str, input_json: Any = None) -> Any:
        payload = {"json": input_json}
        if input_json is None:
            payload["meta"] = {"values": ["undefined"]}
        params = {"input": json.dumps(payload, separators=(",", ":"))}
        r = self._request(
            "GET",
            f"/api/trpc/{proc}",
            params=params,
            headers={
                "x-trpc-source": "nextjs-react",
                "x-request-id": _uuid7(),
                "referer": f"{TOOLKIT}/image-video-generator?mode=image",
            },
        )
        return self._unwrap(proc, r)

    def trpc_post(self, proc: str, input_json: Any) -> Any:
        r = self._request(
            "POST",
            f"/api/trpc/{proc}",
            json={"json": input_json},
            headers={
                "x-trpc-source": "nextjs-react",
                "x-request-id": _uuid7(),
                "content-type": "application/json",
                "origin": TOOLKIT,
                "referer": f"{TOOLKIT}/image-video-generator?mode=image",
            },
        )
        return self._unwrap(proc, r)

    def _unwrap(self, proc: str, r) -> Any:
        if r.status_code >= 400:
            raise ArtlistError(f"tRPC {proc} HTTP {r.status_code}: {r.text[:500]}")
        try:
            body = r.json()
        except Exception:
            raise ArtlistError(f"tRPC {proc}: non-json response: {r.text[:300]}")
        if isinstance(body, list):
            body = body[0]
        if "error" in body:
            raise ArtlistError(f"tRPC {proc} error: {json.dumps(body['error'])[:500]}")
        try:
            return body["result"]["data"]["json"]
        except (KeyError, TypeError):
            return body

    # ---------------- generation flow ----------------

    def get_free_generations(self) -> dict:
        return self.trpc_get("chatSession.getFreeGenerations")

    _UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")

    def get_team_id(self) -> str:
        """teamId создаётся при signup, отдаётся в SSR-payload страницы генератора.
        используем /image-video-generator?mode=image — это post-login route, cloudflare не блочит."""
        # navigation headers — без origin/referer, с sec-fetch-dest=document
        r = self._request("GET", "/image-video-generator?mode=image", headers=self.NAV_HEADERS)
        if r.status_code != 200:
            # фоллбэк на /
            r = self._request("GET", "/", headers=self.NAV_HEADERS)
            if r.status_code != 200:
                raise ArtlistError(f"get_team_id: HTTP {r.status_code} body={r.text[:200]}")
        m = self._UUID_RE.search(r.text)
        if not m:
            raise ArtlistError("get_team_id: no UUID v4 found")
        return m.group(0)

    def create_chat_session(self, name: str = "auto session", team_id: Optional[str] = None) -> str:
        if not team_id:
            team_id = self.get_team_id()
            self.log(f"team_id resolved: {team_id}")
        res = self.trpc_post("chatSession.createChatSession", {"name": name, "teamId": team_id})
        if isinstance(res, dict):
            if "id" in res:
                return res["id"]
            if "data" in res and isinstance(res["data"], dict) and "id" in res["data"]:
                return res["data"]["id"]
        raise ArtlistError(f"createChatSession: unexpected shape {res}")

    def get_presigned_upload(self, file_name: str, mime_type: str) -> dict:
        """
        реальный шейп (из реверса фронт-кода chunk_10b27bb7ec7ecf11.js):
          { fileName, fileType, expiresIn }  где fileType — MIME-тип ("image/jpeg")
        ответ: { presignedUrl, fileUrl, fileKey, thumbnailUrl?, compressedUrl? }
        """
        return self.trpc_post(
            "uploadRouter.getPresignedUrl",
            {"fileName": file_name, "fileType": mime_type, "expiresIn": 86400},
        )

    def upload_file_to_s3(self, presigned: dict, file_bytes: bytes, mime_type: str) -> str:
        """
        грузим PUT в S3 по presignedUrl. возвращает только fileKey.
        НЕ возвращаем fileUrl из presigned — это прямой неподписанный URL, fal.ai по нему получит 403.
        читаемый presigned GET URL берём отдельно через get_signed_get_url().
        """
        url = presigned.get("presignedUrl") or presigned.get("url") or presigned.get("uploadUrl")
        if not url:
            raise ArtlistError(f"unknown presigned shape: {list(presigned)} sample={json.dumps(presigned)[:300]}")
        r = httpx.put(url, content=file_bytes, headers={"content-type": mime_type}, timeout=120)
        if r.status_code not in (200, 201, 204):
            raise ArtlistError(f"S3 upload HTTP {r.status_code}: {r.text[:300]}")
        file_key = presigned.get("fileKey") or presigned.get("key")
        if not file_key:
            raise ArtlistError(f"presigned response has no fileKey: {presigned}")
        return file_key

    def get_signed_get_url(self, file_key: str) -> str:
        """вызывает getPresignedUrlFromKey — отдаёт ПОДПИСАННЫЙ GET URL.
        нужен для передачи fal.ai в createUserGeneration (иначе он получит 403 на S3)."""
        res = self.trpc_post("uploadRouter.getPresignedUrlFromKey", {"fileKey": file_key})
        u = res.get("url") or res.get("presignedUrl")
        if not u and isinstance(res.get("data"), dict):
            u = res["data"].get("url") or res["data"].get("presignedUrl")
        if not u:
            raise ArtlistError(f"getPresignedUrlFromKey: no url in {res}")
        return u

    def get_cost_quote(
        self,
        *,
        model_group_id: int,
        prompt: str,
        image_url: Optional[str] = None,
        aspect_ratio: str = "16:9",
        resolution: str = "medium",
        num_images: int = 1,
    ) -> dict:
        inp: dict = {
            "modelGroupId": model_group_id,
            "input": {
                "prompt": prompt,
                "aspect_ratio": aspect_ratio,
                "resolution": resolution,
                "num_images": num_images,
            },
        }
        if image_url:
            inp["input"]["image_urls"] = [{"fileUrl": image_url}]
        return self.trpc_post("modelRouter.getCostQuote", inp)

    def create_generation(
        self,
        *,
        chat_session_id: str,
        prompt: str,
        feature: str,
        cost_quote: dict,
        image_url: Optional[str] = None,
        file_key: Optional[str] = None,
        file_name: Optional[str] = None,
        mime_type: str = "image/png",
        aspect_ratio: str = "16:9",
        resolution: str = "medium",
        num_images: int = 1,
    ) -> str:
        data = cost_quote.get("data", cost_quote)
        model_id = data["modelId"]
        signature = (
            data.get("digitalSignature")
            or data.get("costQuoteDigitalSignature")
            or cost_quote.get("digitalSignature")
            or cost_quote.get("costQuoteDigitalSignature")
        )
        if not signature:
            raise ArtlistError(f"no signature in cost_quote: data_keys={list(data)} top_keys={list(cost_quote)}")

        inputs: dict = {"prompt": prompt}
        if image_url:
            inputs["image_urls"] = [{"fileUrl": image_url}]
        body: dict = {
            "chatSessionId": chat_session_id,
            "inputs": inputs,
            "modelGroupId": model_id,
            "feature": feature,
            "price": cost_quote["data"].get("cost", 0),
            "settings": {
                "prompt": prompt,
                "aspect_ratio": aspect_ratio,
                "resolution": resolution,
                "num_images": num_images,
            },
            "costQuoteDigitalSignature": signature,
            "timestamp": int(time.time() * 1000),
            "generationMethod": "free",
            "isCopyCmsFileEnabled": False,
        }
        if image_url and file_key:
            body["inputs"]["image_urls"] = [{"fileUrl": image_url}]
            body["artifacts"] = [{
                "fileKey": file_key,
                "metadata": {
                    "fileUrl": image_url,
                    "mimeType": mime_type,
                    "inputSettingKey": "image_urls",
                    "fileType": "deviceUpload",
                    "fileName": file_name or file_key,
                },
            }]
        self.log(f"createUserGeneration body (truncated): {json.dumps(body, ensure_ascii=False)[:600]}...")
        res = self.trpc_post("userGenerationRouter.createUserGeneration", body)
        gen_id = res["data"]["id"] if "data" in res else res["id"]
        self.log(f"generation queued id={gen_id}")
        return gen_id

    def poll_generation(self, chat_session_id: str, gen_id: str, timeout: float = 240.0, interval: float = 3.0) -> dict:
        """возвращает финальный item с outputs/files."""
        deadline = time.monotonic() + timeout
        last: dict | None = None
        while time.monotonic() < deadline:
            res = self.trpc_get(
                "userGenerationRouter.getUserGenerationsBySession",
                {"sessionId": chat_session_id},
            )
            items = res.get("items", [])
            for it in items:
                if it.get("id") == gen_id:
                    last = it
                    status = it.get("status")
                    self.log(f"  gen {gen_id} status={status}")
                    if status in ("succeeded", "completed", "success", "done"):
                        return it
                    if status in ("failed", "error", "cancelled", "rejected"):
                        # передаём ПОЛНЫЙ item — без обрезки чтоб видеть errorCode и любые подсказки
                        raise ArtlistError(f"generation failed: {json.dumps(it, ensure_ascii=False)}")
                    break
            time.sleep(interval)
        raise ArtlistError(f"generation timeout. last state: {json.dumps(last, ensure_ascii=False) if last else '<none>'}")

    def extract_output_urls(self, item: dict) -> list[str]:
        """достаём image URL(s) из completed item.
        реальная структура (увидел из debug дампа):
          item.imageUrl  — прямой URL на fal.media, открытый, без подписи
          item.thumbnailUrl — превью
        для num_images > 1 — fallback на массивы outputs/files."""
        urls: list[str] = []
        for k in ("imageUrl",):
            if isinstance(item.get(k), str):
                urls.append(item[k])
        for k in ("outputs", "files", "results", "generationOutputs"):
            arr = item.get(k)
            if isinstance(arr, list):
                for a in arr:
                    if isinstance(a, dict):
                        for f in ("imageUrl", "fileUrl", "url", "s3Url", "downloadUrl"):
                            if a.get(f):
                                urls.append(a[f])
                                break
        # уникальные с сохранением порядка
        seen: set[str] = set()
        out: list[str] = []
        for u in urls:
            if u not in seen:
                seen.add(u)
                out.append(u)
        return out

    # ---------------- helper composite ----------------

    def run_one_generation(
        self,
        *,
        chat_session_id: str,
        prompt: str,
        image_path: Optional[Path] = None,
        resolution: str = "medium",
        aspect_ratio: str = "16:9",
        text_to_image_group_id: int = 380,
        image_to_image_group_id: int = 380,
    ) -> dict:
        """полный цикл: (опц.) аплоад -> quote -> create -> poll. возвращает completed item."""
        image_url = None
        file_key = None
        file_name = None
        mime_type = "image/png"
        feature = "text-to-image"
        model_group_id = text_to_image_group_id

        if image_path:
            file_name = image_path.name
            mime_type = mimetypes.guess_type(file_name)[0] or "image/jpeg"
            self.log(f"upload {file_name} ({mime_type})")
            presigned = self.get_presigned_upload(file_name, mime_type)
            data = presigned.get("data", presigned)
            file_key = self.upload_file_to_s3(data, image_path.read_bytes(), mime_type)
            # signed GET URL — без него провайдер получит 403 на S3
            image_url = self.get_signed_get_url(file_key)
            self.log(f"signed GET url: {image_url[:120]}...")
            feature = "image-to-image"
            model_group_id = image_to_image_group_id

        self.log(f"getCostQuote modelGroupId={model_group_id} feature={feature} res={resolution}")
        quote = self.get_cost_quote(
            model_group_id=model_group_id,
            prompt=prompt,
            image_url=image_url,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
        )

        gen_id = self.create_generation(
            chat_session_id=chat_session_id,
            prompt=prompt,
            feature=feature,
            cost_quote=quote,
            image_url=image_url,
            file_key=file_key,
            file_name=file_name,
            mime_type=mime_type,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
        )

        item = self.poll_generation(chat_session_id, gen_id)
        return item

    def close(self):
        try:
            self.session.close()
        except Exception:
            pass


if __name__ == "__main__":
    c = ArtlistClient(verbose=True)
    print("csrf:", c._csrf()[:24], "...")
    c.close()
