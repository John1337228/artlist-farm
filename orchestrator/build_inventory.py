"""
inventory builder для фермы artlist.

- сканит SRC_ROOT (детали_photos), берёт все image-файлы (webp/jpg/png)
- конвертит → JPG flatten (no alpha) через ensure_jpg
- даёт каждому короткий безопасный slug (хеш от original_path), чтоб не таскать кириллицу
- ведёт sqlite manifest: rowid | site | original_path | slug | batch_id | status
- режет на батчи по BATCH_SIZE штук
- упаковывает каждый batch в `staging/batch_<NNNN>.zip`

второй шаг — push_batches.py — публикует эти zip'ы в приватный inputs-репо как release assets.
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sqlite3
import sys
import zipfile
from pathlib import Path
from typing import Iterable

# импортируем ensure_jpg из farm-кода
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from src.image_prep import ensure_jpg


SRC_ROOT = Path(r"c:\Users\John\Desktop\project\detali_photos")
STAGING = HERE / "staging"
DB_PATH = HERE / "inventory.db"

# =2: один батч = один акк = одно signup на одном github-runner с уникальным IP.
# на каждом runner делаем РОВНО ОДИН signup (cf пропускает первый, 2-й даёт 429),
# поэтому ставим минимально полезный размер = IMAGES_PER_ACCOUNT.
BATCH_SIZE = 2
EXTS = {".webp", ".jpg", ".jpeg", ".png"}


def slug_for(rel_path: str) -> str:
    h = hashlib.sha1(rel_path.encode("utf-8")).hexdigest()[:16]
    return h


def iter_inputs() -> Iterable[tuple[str, Path]]:
    for site_dir in sorted(SRC_ROOT.iterdir()):
        if not site_dir.is_dir():
            continue
        for p in sorted(site_dir.rglob("*")):
            if p.is_file() and p.suffix.lower() in EXTS:
                rel = p.relative_to(SRC_ROOT).as_posix()
                yield rel, p


def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS items (
            slug TEXT PRIMARY KEY,
            site TEXT NOT NULL,
            original_rel TEXT NOT NULL UNIQUE,
            batch_id INTEGER,
            status TEXT NOT NULL DEFAULT 'pending'  -- pending|done|failed|skipped
        );
        CREATE INDEX IF NOT EXISTS idx_batch ON items(batch_id);
        CREATE INDEX IF NOT EXISTS idx_status ON items(status);
    """)
    return conn


def cmd_scan(_args):
    conn = init_db()
    cur = conn.cursor()
    seen = 0
    new = 0
    for rel, _src in iter_inputs():
        site = rel.split("/", 1)[0]
        slug = slug_for(rel)
        seen += 1
        try:
            cur.execute(
                "INSERT INTO items(slug, site, original_rel, status) VALUES(?,?,?,'pending')",
                (slug, site, rel),
            )
            new += 1
        except sqlite3.IntegrityError:
            pass
        if seen % 1000 == 0:
            conn.commit()
            print(f"  scanned {seen}, new {new}")
    conn.commit()
    total = cur.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    print(f"done. scanned {seen}, new {new}, total in db {total}")
    conn.close()


def cmd_batch(args):
    conn = init_db()
    cur = conn.cursor()
    # сбрасываем batch_id у pending — пересоберём
    if args.reset:
        cur.execute("UPDATE items SET batch_id=NULL WHERE status='pending'")
        conn.commit()
    pending = cur.execute(
        "SELECT slug, site, original_rel FROM items WHERE status='pending' AND batch_id IS NULL ORDER BY site, slug"
    ).fetchall()
    print(f"pending without batch: {len(pending)}")
    if not pending:
        return
    # выясним следующий batch_id
    max_id = cur.execute("SELECT COALESCE(MAX(batch_id), -1) FROM items").fetchone()[0]
    bid = max_id + 1
    written = 0
    STAGING.mkdir(exist_ok=True)
    for i in range(0, len(pending), BATCH_SIZE):
        chunk = pending[i : i + BATCH_SIZE]
        zip_path = STAGING / f"batch_{bid:04d}.zip"
        manifest_lines: list[str] = []
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=4) as z:
            for slug, site, rel in chunk:
                src = SRC_ROOT / rel
                if not src.exists():
                    continue
                # конвертим в JPG
                tmp_jpg = STAGING / f"_tmp_{slug}.jpg"
                try:
                    jpg_path = ensure_jpg(src, out_dir=STAGING)
                    if jpg_path.name != f"{src.stem}.jpg":
                        # подменим имя на slug.jpg
                        target = STAGING / f"{slug}.jpg"
                        shutil.move(str(jpg_path), str(target))
                        jpg_path = target
                    else:
                        target = STAGING / f"{slug}.jpg"
                        shutil.move(str(jpg_path), str(target))
                        jpg_path = target
                    z.write(jpg_path, arcname=f"{slug}.jpg")
                    manifest_lines.append(f"{slug}\t{site}\t{rel}")
                    jpg_path.unlink(missing_ok=True)
                    cur.execute("UPDATE items SET batch_id=? WHERE slug=?", (bid, slug))
                    written += 1
                except Exception as e:
                    print(f"  ! skip {slug} {rel}: {e}")
                    cur.execute("UPDATE items SET status='skipped' WHERE slug=?", (slug,))
                finally:
                    if tmp_jpg.exists():
                        tmp_jpg.unlink(missing_ok=True)
            # manifest внутри zip — мапа slug → original
            z.writestr("_manifest.tsv", "\n".join(manifest_lines))
        size_kb = zip_path.stat().st_size / 1024
        print(f"  batch {bid:04d}: {len(chunk)} items, {size_kb:.0f} KB")
        bid += 1
        conn.commit()
    print(f"done. wrote {written} items into {bid - max_id - 1} batches in {STAGING}")
    conn.close()


def cmd_status(_args):
    conn = init_db()
    cur = conn.cursor()
    for row in cur.execute("SELECT status, COUNT(*) FROM items GROUP BY status"):
        print(f"  {row[0]}: {row[1]}")
    n_batches = cur.execute("SELECT COUNT(DISTINCT batch_id) FROM items WHERE batch_id IS NOT NULL").fetchone()[0]
    print(f"  batches: {n_batches}")
    if STAGING.exists():
        zips = list(STAGING.glob("batch_*.zip"))
        total = sum(z.stat().st_size for z in zips)
        print(f"  staging: {len(zips)} zips, {total/1024/1024:.1f} MB")
    conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan", help="walk SRC_ROOT and seed db")
    bp = sub.add_parser("batch", help="form zip batches in staging/")
    bp.add_argument("--reset", action="store_true", help="rebuild all pending batches")
    sub.add_parser("status", help="show counters")
    args = ap.parse_args()
    {"scan": cmd_scan, "batch": cmd_batch, "status": cmd_status}[args.cmd](args)
