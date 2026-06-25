"""
скачивает все артефакты последнего successful farm-workflow, распаковывает,
раскладывает в `detali_clean_artlist/<site>/<original_basename>.png`.

маппинг slug → site/original_rel берётся из inventory.db.
обновляет статус items: ok/failed.

usage:
  python fetch_results.py [--run-id 12345]
если --run-id не задан, берём последний run worflow 'farm'.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "inventory.db"
SCRATCH = HERE / "fetched"
OUT_ROOT = Path(r"c:\Users\John\Desktop\project\detali_clean_artlist")


def gh(*args: str) -> tuple[int, str]:
    p = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout + p.stderr)


def latest_farm_run_id() -> str:
    code, out = gh("run", "list", "--workflow", "farm", "--limit", "1", "--json", "databaseId", "-q", ".[0].databaseId")
    if code != 0 or not out.strip():
        raise SystemExit(f"can't get last farm run: {out}")
    return out.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", default=None)
    args = ap.parse_args()
    run_id = args.run_id or latest_farm_run_id()
    print(f"[*] downloading artifacts from run {run_id}")
    SCRATCH.mkdir(exist_ok=True)
    # очищаем
    for p in SCRATCH.iterdir():
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)
    code, out = gh("run", "download", str(run_id), "--dir", str(SCRATCH))
    if code != 0:
        raise SystemExit(out)

    conn = sqlite3.connect(DB_PATH)
    slug_map = {row[0]: (row[1], row[2]) for row in conn.execute("SELECT slug, site, original_rel FROM items")}
    OUT_ROOT.mkdir(parents=True, exist_ok=True)

    total_ok = 0
    total_fail = 0
    for batch_dir in sorted(SCRATCH.iterdir()):
        if not batch_dir.is_dir():
            continue
        # читаем results.tsv для апдейта статусов
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
        # раскладываем картинки
        for img in batch_dir.iterdir():
            if img.is_file() and img.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
                slug = img.stem
                if slug.startswith("_"):
                    continue
                meta = slug_map.get(slug)
                if not meta:
                    print(f"  ? unknown slug {slug}")
                    continue
                site, rel = meta
                # имя как у оригинала, но без расширения исходника + новое
                orig_stem = Path(rel).stem
                target_dir = OUT_ROOT / site
                target_dir.mkdir(parents=True, exist_ok=True)
                target = target_dir / f"{orig_stem}{img.suffix}"
                shutil.copy2(img, target)
                total_ok += 1
        total_fail += sum(1 for line in (rt.read_text(encoding="utf-8").splitlines()[1:] if rt.exists() else []) if line.split("\t",2)[1] != "ok")
    conn.commit()
    conn.close()
    print(f"[done] ok={total_ok}, failed={total_fail}, out={OUT_ROOT}")


if __name__ == "__main__":
    main()
