"""
batch-воркер для farm-workflow.

аргументы (env):
  BATCH_ID            — например "0042"
  INPUTS_REPO         — "John1337228/artlist-inputs"
  INPUTS_TOKEN        — PAT с repo:read для приватного inputs репо
  PROMPT              — текст промпта
  ACCOUNTS_PER_BATCH  — сколько акков создавать (по умолч. = ceil(N_items / 2))
  IMAGES_PER_ACCOUNT  — 2 (по нашим замерам quota)

алгоритм:
1. качаем batch_<id>.zip из release inputs репо (тег "latest" или batches/<id>)
2. распаковываем в /tmp/in/
3. читаем _manifest.tsv (slug → site → original_rel)
4. бьём slug-список на чанки по IMAGES_PER_ACCOUNT
5. для каждого чанка:
     - создаём mail.tm + signup на artlist
     - делаем 2 i2i генерации (по 1 на каждую картинку)
     - сохраняем результат как out/<slug>.png
     - пишем строчку в out/_results.tsv: slug TAB status TAB error_or_url
6. при выходе — out/ упаковывается в артефакт.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import traceback
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import httpx

from src.mail import create_account, delete_account
from src.client import ArtlistClient, ArtlistError


def env(k: str, default: str | None = None, required: bool = False) -> str:
    v = os.environ.get(k, default)
    if required and not v:
        raise SystemExit(f"env {k} required")
    return v or ""


ASSETS_PER_RELEASE = 1000


def fetch_batch_zip(batch_id: str, repo: str, token: str, dst: Path) -> None:
    """
    качаем asset из release-шарда: batches-<id//1000>.
    fallback: если не нашли в expected shard — пытаемся соседние shard'ы (на случай
    sharding-schema drift или ghost-asset перетекания).
    """
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    asset_name = f"batch_{batch_id}.zip"
    primary = int(batch_id) // ASSETS_PER_RELEASE
    tags_to_try = [f"batches-{primary}", f"batches-{primary - 1}", f"batches-{primary + 1}"]
    last_err = None
    for tag in tags_to_try:
        api = f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
        r = httpx.get(api, headers=headers, timeout=30)
        if r.status_code != 200:
            last_err = f"{tag}: HTTP {r.status_code}"
            continue
        rel = r.json()
        for a in rel.get("assets", []):
            if a["name"] == asset_name:
                dl = httpx.get(
                    a["url"],
                    headers={**headers, "Accept": "application/octet-stream"},
                    follow_redirects=True,
                    timeout=120,
                )
                dl.raise_for_status()
                dst.write_bytes(dl.content)
                print(f"[batch] downloaded {asset_name} from {tag}: {dst.stat().st_size} bytes")
                return
    raise SystemExit(f"asset {asset_name} not found in any of {tags_to_try}. last={last_err}")


def process_account(
    *,
    chunk: list[tuple[str, Path]],
    prompt: str,
    out_dir: Path,
    results_tsv,
) -> int:
    """один акк + до 2 генераций (по числу элементов в chunk)."""
    saved = 0
    with httpx.Client(timeout=20) as mc:
        mail_acc = create_account(mc)
    c = ArtlistClient(verbose=False)
    try:
        c.signup(email=mail_acc.address, password=mail_acc.password)
        chat_id = c.create_chat_session("auto-batch")
        for slug, jpg_path in chunk:
            try:
                item = c.run_one_generation(
                    chat_session_id=chat_id,
                    prompt=prompt,
                    image_path=jpg_path,
                )
            except ArtlistError as e:
                results_tsv.write(f"{slug}\tfailed\t{str(e)[:300]}\n")
                results_tsv.flush()
                continue
            urls = c.extract_output_urls(item)
            if not urls:
                results_tsv.write(f"{slug}\tno_url\t{item.get('errorCode') or '-'}\n")
                results_tsv.flush()
                continue
            u = urls[0]
            ext = ".png" if ".png" in u.lower() else ".jpg" if (".jpg" in u.lower() or ".jpeg" in u.lower()) else ".webp" if ".webp" in u.lower() else ".png"
            dst = out_dir / f"{slug}{ext}"
            try:
                with httpx.stream("GET", u, timeout=120, follow_redirects=True) as r:
                    r.raise_for_status()
                    dst.write_bytes(r.read())
                results_tsv.write(f"{slug}\tok\t{u}\n")
                results_tsv.flush()
                saved += 1
            except Exception as e:
                results_tsv.write(f"{slug}\tdl_fail\t{e}\n")
                results_tsv.flush()
    except Exception as e:
        # signup или create_chat_session упали — пометим все элементы chunk
        for slug, _ in chunk:
            results_tsv.write(f"{slug}\tsignup_fail\t{e}\n")
        results_tsv.flush()
    finally:
        c.close()
        try:
            with httpx.Client(timeout=10) as mc:
                delete_account(mc, mail_acc)
        except Exception:
            pass
    return saved


def main() -> int:
    batch_id = env("BATCH_ID", required=True)
    repo = env("INPUTS_REPO", required=True)
    token = env("INPUTS_TOKEN", required=True)
    prompt = env("PROMPT", "remove the watermark from the photo and keep the original detail")
    per_acc = int(env("IMAGES_PER_ACCOUNT", "2"))

    tmp = Path("/tmp/farm") if os.name != "nt" else Path(os.environ.get("RUNNER_TEMP", ".")) / "farm"
    in_dir = tmp / "in"
    out_dir = tmp / "out"
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    zip_path = tmp / f"batch_{batch_id}.zip"
    fetch_batch_zip(batch_id, repo, token, zip_path)

    with zipfile.ZipFile(zip_path) as z:
        z.extractall(in_dir)

    manifest_path = in_dir / "_manifest.tsv"
    if not manifest_path.exists():
        raise SystemExit("manifest missing in batch zip")
    items: list[tuple[str, Path]] = []
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        slug, _site, _rel = line.split("\t", 2)
        jpg = in_dir / f"{slug}.jpg"
        if jpg.exists():
            items.append((slug, jpg))
    print(f"[batch {batch_id}] {len(items)} items to process")

    # выходную папку артефакта переносим в RUNNER_TEMP_OUT (root для actions upload-artifact)
    artifact_dir = Path(os.environ.get("ARTIFACT_DIR", "./batch_out"))
    artifact_dir.mkdir(parents=True, exist_ok=True)
    results_tsv = (artifact_dir / "_results.tsv").open("w", encoding="utf-8")
    results_tsv.write("slug\tstatus\tnote\n")

    saved_total = 0
    chunks = [items[i : i + per_acc] for i in range(0, len(items), per_acc)]
    for ci, chunk in enumerate(chunks):
        print(f"[acc {ci + 1}/{len(chunks)}] {[s for s, _ in chunk]}")
        t0 = time.time()
        try:
            saved = process_account(
                chunk=chunk,
                prompt=prompt,
                out_dir=artifact_dir,
                results_tsv=results_tsv,
            )
        except Exception:
            traceback.print_exc()
            saved = 0
        saved_total += saved
        print(f"[acc {ci + 1}] saved {saved}/{len(chunk)} in {time.time()-t0:.1f}s")

    results_tsv.close()
    print(f"[done] saved {saved_total}/{len(items)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
