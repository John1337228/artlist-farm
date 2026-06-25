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


def upload_batch(zip_path: Path) -> None:
    code, out = gh("release", "upload", TAG, str(zip_path), "-R", REPO, "--clobber")
    if code != 0:
        raise SystemExit(f"failed to upload {zip_path.name}: {out}")


def main():
    if not STAGING.exists():
        sys.exit("no staging dir")
    zips = sorted(STAGING.glob("batch_*.zip"))
    if not zips:
        sys.exit("no zips in staging")
    print(f"found {len(zips)} zips")
    ensure_release()
    for i, z in enumerate(zips, 1):
        sz_mb = z.stat().st_size / 1024 / 1024
        print(f"[{i}/{len(zips)}] uploading {z.name} ({sz_mb:.1f} MB) ...")
        upload_batch(z)
    print("done.")


if __name__ == "__main__":
    main()
