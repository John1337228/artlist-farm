"""
полный E2E на одном раннере: signup -> chatSession -> 2 text-to-image -> скачать.
text-to-image выбран намеренно: без input-картинки, чтоб обкатать конвейер без сложности загрузки.
сохраняет PNG в out/, после workflow они подбираются как artifact.
"""
from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import httpx

from src.mail import create_account, delete_account
from src.client import ArtlistClient, ArtlistError


PROMPT = "a cute orange tabby cat sitting on a wooden chair in a sunlit kitchen, photorealistic studio photo"


def download(url: str, dst: Path) -> int:
    with httpx.stream("GET", url, timeout=60, follow_redirects=True) as r:
        r.raise_for_status()
        dst.write_bytes(r.read())
    return dst.stat().st_size


def main() -> int:
    out_dir = HERE / "out"
    out_dir.mkdir(exist_ok=True)

    print(f"[runner-ip] {httpx.get('https://api.ipify.org', timeout=10).text}")

    with httpx.Client(timeout=20.0) as mc:
        mail_acc = create_account(mc)
    print(f"[mail.tm] {mail_acc.address}")

    c = ArtlistClient(verbose=True)
    try:
        c.signup(email=mail_acc.address, password=mail_acc.password)
        chat_id = c.create_chat_session("auto-1")
        print(f"[chat] {chat_id}")
        free = c.get_free_generations()
        gens = {g["name"]: g for g in free["data"]["freeGenerations"]["perGenerationType"]}
        img_quota = gens.get("generatedImage", {})
        n = max(0, img_quota.get("limit", 0) - img_quota.get("used", 0))
        print(f"[quota] image: used={img_quota.get('used')} limit={img_quota.get('limit')} → run {n}")

        saved = 0
        for i in range(n):
            t0 = time.time()
            try:
                item = c.run_one_generation(
                    chat_session_id=chat_id,
                    prompt=PROMPT,
                )
            except ArtlistError as e:
                print(f"[gen {i+1}] FAIL: {e}")
                break
            urls = c.extract_output_urls(item)
            print(f"[gen {i+1}] {len(urls)} url(s) in {time.time()-t0:.1f}s")
            if not urls:
                debug = out_dir / f"debug_gen{i}.json"
                import json
                debug.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"[gen {i+1}] no urls; dumped item -> {debug}")
                continue
            for u_idx, u in enumerate(urls):
                ext = ".png"
                lu = u.lower()
                if ".jpg" in lu or ".jpeg" in lu:
                    ext = ".jpg"
                elif ".webp" in lu:
                    ext = ".webp"
                dst = out_dir / f"gen{i:02d}_{u_idx}{ext}"
                try:
                    sz = download(u, dst)
                    print(f"  saved {dst.name} ({sz//1024} KB)")
                    saved += 1
                except Exception as e:
                    print(f"  download fail: {e}")
        print(f"[done] saved {saved}/{n}")
        return 0 if saved > 0 else 3
    except Exception:
        traceback.print_exc()
        return 1
    finally:
        c.close()
        try:
            with httpx.Client(timeout=10) as mc:
                delete_account(mc, mail_acc)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
