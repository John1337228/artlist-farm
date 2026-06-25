"""
полный image-to-image E2E на одном раннере:
  signup -> chatSession -> upload sample.jpg -> 2 generations -> download outputs.
input — test_inputs/sample.jpg (одна фотка для qa).
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import httpx

from src.mail import create_account, delete_account
from src.client import ArtlistClient, ArtlistError
from src.image_prep import ensure_jpg


PROMPTS = [
    # без слова watermark, очень нейтральный; и aspect_ratio=1:1 (детали по сути квадрат)
    ("the same industrial spare part, photo on a plain white studio background", "1:1"),
    # text2image baseline — он должен пройти content filter
    # (если text2image работает а image-to-image падает на разных промптах — значит блок в самом инпуте)
    ("a small black industrial spare part on a plain white studio background, photo", "1:1"),
]
INPUT_RELPATH = "test_inputs/sample.jpg"


def download(url: str, dst: Path) -> int:
    with httpx.stream("GET", url, timeout=60, follow_redirects=True) as r:
        r.raise_for_status()
        dst.write_bytes(r.read())
    return dst.stat().st_size


def main() -> int:
    out_dir = HERE / "out"
    out_dir.mkdir(exist_ok=True)
    inp = HERE / INPUT_RELPATH
    if not inp.exists():
        print(f"[fatal] input not found: {inp}")
        return 2
    jpg = ensure_jpg(inp)
    print(f"[input] {jpg.name} ({jpg.stat().st_size} bytes)")
    # копируем оригинал в out/ для сравнения side-by-side в артефакте
    (out_dir / f"_orig_{jpg.name}").write_bytes(jpg.read_bytes())

    print(f"[runner-ip] {httpx.get('https://api.ipify.org', timeout=10).text}")

    with httpx.Client(timeout=20.0) as mc:
        mail_acc = create_account(mc)
    print(f"[mail.tm] {mail_acc.address}")

    c = ArtlistClient(verbose=True)
    saved = 0
    try:
        c.signup(email=mail_acc.address, password=mail_acc.password)
        chat_id = c.create_chat_session("auto-i2i")
        print(f"[chat] {chat_id}")
        free = c.get_free_generations()
        gens = {g["name"]: g for g in free["data"]["freeGenerations"]["perGenerationType"]}
        img_quota = gens.get("generatedImage", {})
        n = max(0, img_quota.get("limit", 0) - img_quota.get("used", 0))
        print(f"[quota] image: {img_quota.get('used')}/{img_quota.get('limit')} → run {n}")

        for i in range(n):
            prompt, ar = PROMPTS[i % len(PROMPTS)]
            # i==1 → text2image (без image_path) — baseline для проверки content filter
            use_input = (i == 0)
            print(f"[gen {i+1}/{n}] mode={'i2i' if use_input else 't2i'} ar={ar} prompt={prompt!r}")
            t0 = time.time()
            try:
                item = c.run_one_generation(
                    chat_session_id=chat_id,
                    prompt=prompt,
                    image_path=jpg if use_input else None,
                    aspect_ratio=ar,
                )
            except ArtlistError as e:
                print(f"[gen {i+1}] FAIL: {e}")
                (out_dir / f"_err_gen{i}.txt").write_text(str(e), encoding="utf-8")
                continue
            elapsed = time.time() - t0
            urls = c.extract_output_urls(item)
            (out_dir / f"_meta_gen{i}.json").write_text(
                json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"[gen {i+1}] {len(urls)} url(s) in {elapsed:.1f}s")
            for u_idx, u in enumerate(urls):
                ext = ".png"
                lu = u.lower()
                if ".jpg" in lu or ".jpeg" in lu:
                    ext = ".jpg"
                elif ".webp" in lu:
                    ext = ".webp"
                dst = out_dir / f"clean_gen{i:02d}_{u_idx}{ext}"
                try:
                    sz = download(u, dst)
                    print(f"  saved {dst.name} ({sz//1024} KB)")
                    saved += 1
                except Exception as e:
                    print(f"  download fail: {e}")
        print(f"[done] saved {saved}")
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
