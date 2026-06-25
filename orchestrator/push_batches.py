"""
заливает все batch_*.zip из staging/ в release "batches" приватного inputs-репо.
требует установленного gh CLI с авторизацией.

политика: одна release-тэг 'batches', все zip'ы — assets к ней (как pre-built artifacts).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
STAGING = HERE / "staging"
REPO = "John1337228/artlist-inputs"
TAG = "batches"


def gh(*args: str) -> tuple[int, str]:
    p = subprocess.run(
        ["gh", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return p.returncode, (p.stdout + p.stderr)


def ensure_release() -> None:
    code, _ = gh("release", "view", TAG, "-R", REPO)
    if code == 0:
        return
    code, out = gh("release", "create", TAG, "-R", REPO, "--title", "batches", "--notes", "private input batches")
    if code != 0:
        raise SystemExit(f"failed to create release: {out}")


def upload_batches(zips: list[Path]) -> None:
    """gh release upload принимает много файлов за раз — батчим по 25 чтоб не упереться в argv-лимит."""
    paths = [str(z) for z in zips]
    code, out = gh("release", "upload", TAG, *paths, "-R", REPO, "--clobber")
    if code != 0:
        raise SystemExit(f"failed to upload batch: {out[:500]}")


def main():
    if not STAGING.exists():
        sys.exit("no staging dir")
    zips = sorted(STAGING.glob("batch_*.zip"))
    if not zips:
        sys.exit("no zips in staging")
    print(f"found {len(zips)} zips")
    ensure_release()
    GROUP = 25
    import time as _t
    t0 = _t.time()
    for i in range(0, len(zips), GROUP):
        chunk = zips[i : i + GROUP]
        upload_batches(chunk)
        done = i + len(chunk)
        elapsed = _t.time() - t0
        rate = done / max(elapsed, 0.1)
        eta = (len(zips) - done) / max(rate, 0.01)
        print(f"  uploaded {done}/{len(zips)} ({rate:.1f}/s, ETA {eta/60:.1f} min)")
    print("done.")


if __name__ == "__main__":
    main()
