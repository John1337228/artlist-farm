"""
локальный оркестратор фермы.
github matrix max = 256, у нас сейчас 2500+ батчей — режем на чанки по 256
и запускаем workflow последовательно. ждём каждый run до завершения.

usage:
    python run_farm.py                       # все ещё-не-обработанные батчи
    python run_farm.py --chunk-size 256      # размер matrix-чанка
    python run_farm.py --max-chunks 1        # только первый чанк (smoke)
    python run_farm.py --from 256 --to 511   # конкретный диапазон
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
FARM_DIR = HERE.parent
DB_PATH = HERE / "inventory.db"
SCRATCH = HERE / "fetched"
OUT_ROOT = Path(r"c:\Users\John\Desktop\project\detali_clean_artlist")
REPO = "John1337228/artlist-farm"


def gh(*args: str, capture: bool = True) -> tuple[int, str]:
    p = subprocess.run(
        ["gh", *args],
        capture_output=capture, text=True, encoding="utf-8", errors="replace",
    )
    return p.returncode, (p.stdout + p.stderr)


def total_batches() -> int:
    conn = sqlite3.connect(DB_PATH)
    n = conn.execute("SELECT COALESCE(MAX(batch_id), -1) + 1 FROM items").fetchone()[0]
    conn.close()
    return int(n)


def update_status_from_artifacts(run_id: str) -> tuple[int, int]:
    """тянем все артефакты run'а, обновляем inventory.db, раскладываем картинки."""
    SCRATCH.mkdir(exist_ok=True)
    for p in SCRATCH.iterdir():
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)
    code, out = gh("run", "download", run_id, "--dir", str(SCRATCH))
    if code != 0:
        print(f"  ! download failed: {out[:300]}")
        return 0, 0
    conn = sqlite3.connect(DB_PATH)
    slug_map = {row[0]: (row[1], row[2]) for row in conn.execute("SELECT slug, site, original_rel FROM items")}
    OUT_ROOT.mkdir(parents=True, exist_ok=True)

    ok = 0
    fail = 0
    for batch_dir in sorted(SCRATCH.iterdir()):
        if not batch_dir.is_dir():
            continue
        rt = batch_dir / "_results.tsv"
        if rt.exists():
            for line in rt.read_text(encoding="utf-8").splitlines()[1:]:
                if not line.strip():
                    continue
                parts = line.split("\t", 2)
                slug = parts[0]
                status = parts[1]
                db_status = "done" if status == "ok" else "failed"
                conn.execute("UPDATE items SET status=? WHERE slug=?", (db_status, slug))
                if status == "ok":
                    ok += 1
                else:
                    fail += 1
        for img in batch_dir.iterdir():
            if img.is_file() and img.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
                slug = img.stem
                if slug.startswith("_"):
                    continue
                meta = slug_map.get(slug)
                if not meta:
                    continue
                site, rel = meta
                orig_stem = Path(rel).stem
                target_dir = OUT_ROOT / site
                target_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(img, target_dir / f"{orig_stem}{img.suffix}")
    conn.commit()
    conn.close()
    return ok, fail


def run_chunk(start: int, end: int, prompt: str) -> str | None:
    """запускает farm workflow на batches=start-end, ждёт завершения. возвращает run_id."""
    spec = f"{start}-{end}"
    print(f"\n=== chunk {spec} ({end - start + 1} batches) ===")
    code, out = gh("workflow", "run", "farm",
                   "-F", f"batches={spec}",
                   "-F", f"prompt={prompt}",
                   "-R", REPO)
    if code != 0:
        print(f"  ! workflow run failed: {out[:300]}")
        return None
    # ждём пока появится run в списке
    time.sleep(7)
    code, out = gh("run", "list", "--workflow", "farm", "-R", REPO,
                   "--limit", "1", "--json", "databaseId,status,createdAt",
                   "-q", ".[0].databaseId")
    run_id = out.strip()
    if not run_id:
        print("  ! no run_id picked")
        return None
    print(f"  run_id={run_id}, watching...")
    code, _ = gh("run", "watch", run_id, "-R", REPO, "--exit-status", capture=True)
    print(f"  finished (exit={code}). collecting artifacts...")
    ok, fail = update_status_from_artifacts(run_id)
    print(f"  chunk done: ok={ok}, fail={fail}")
    return run_id


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunk-size", type=int, default=256)
    ap.add_argument("--max-chunks", type=int, default=999)
    ap.add_argument("--from", dest="from_", type=int, default=None)
    ap.add_argument("--to", type=int, default=None)
    ap.add_argument("--prompt", default="remove the watermark from the photo and keep the original detail")
    args = ap.parse_args()

    total = total_batches()
    print(f"total batches: {total}")

    if args.from_ is not None and args.to is not None:
        starts = list(range(args.from_, args.to + 1, args.chunk_size))
    else:
        starts = list(range(0, total, args.chunk_size))

    starts = starts[: args.max_chunks]
    print(f"will process {len(starts)} chunks of <= {args.chunk_size}")

    for s in starts:
        e = min(s + args.chunk_size - 1, total - 1)
        run_chunk(s, e, args.prompt)


if __name__ == "__main__":
    main()
