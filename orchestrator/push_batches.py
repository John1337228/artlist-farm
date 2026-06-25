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
# github limit: 1000 assets per release
ASSETS_PER_RELEASE = 1000


def tag_for(batch_id: int) -> str:
    return f"batches-{batch_id // ASSETS_PER_RELEASE}"


def batch_id_of(zip_name: str) -> int:
    # batch_0042.zip -> 42
    return int(zip_name.split("_")[1].split(".")[0])


def gh(*args: str) -> tuple[int, str]:
    p = subprocess.run(
        ["gh", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return p.returncode, (p.stdout + p.stderr)


def ensure_release(tag: str) -> None:
    code, _ = gh("release", "view", tag, "-R", REPO)
    if code == 0:
        return
    code, out = gh("release", "create", tag, "-R", REPO, "--title", tag, "--notes", f"input batches shard {tag}")
    if code != 0:
        raise SystemExit(f"failed to create release {tag}: {out}")


import time as _t


def upload_batches(tag: str, zips: list[Path]) -> int:
    """upload группы файлов в указанный release. возвращает кол-во успешно загруженных."""
    paths = [str(z) for z in zips]
    backoff = 30
    for attempt in range(6):
        code, out = gh("release", "upload", tag, *paths, "-R", REPO, "--clobber")
        if code == 0:
            return len(zips)
        if "secondary rate limit" in out.lower() or "HTTP 403" in out:
            print(f"  rate-limit hit on {tag}, sleep {backoff}s")
            _t.sleep(backoff)
            backoff = min(backoff * 2, 300)
            continue
        if "1000 assets per release" in out:
            # release заполнен — это терминальная ошибка для текущего тэга
            print(f"  [{tag}] release full (1000 limit); will skip remaining in this shard")
            return 0
        # неизвестная ошибка — фолбэк на одиночные uploads чтобы максимизировать сохранённое
        print(f"  group failed ({out[:200]}), falling back to per-file")
        ok = 0
        for z in zips:
            c2, o2 = gh("release", "upload", tag, str(z), "-R", REPO, "--clobber")
            if c2 == 0:
                ok += 1
            else:
                print(f"    ! skip {z.name}: {o2[:200]}")
        return ok
    print(f"  [{tag}] upload retries exhausted; skipping group")
    return 0


def already_uploaded(tag: str) -> set[str]:
    code, out = gh("release", "view", tag, "-R", REPO, "--json", "assets", "-q", ".assets[].name")
    if code != 0:
        return set()
    return {n.strip() for n in out.splitlines() if n.strip()}


def main():
    if not STAGING.exists():
        sys.exit("no staging dir")
    zips = sorted(STAGING.glob("batch_*.zip"))
    if not zips:
        sys.exit("no zips in staging")
    # шардируем по release-тэгам
    by_tag: dict[str, list[Path]] = {}
    for z in zips:
        t = tag_for(batch_id_of(z.name))
        by_tag.setdefault(t, []).append(z)
    print(f"{len(zips)} zips -> {len(by_tag)} releases: " + ", ".join(f"{k}={len(v)}" for k, v in by_tag.items()))

    GROUP = 25
    INTER_GROUP_SLEEP = 12.0
    t0 = _t.time()
    total_done = 0
    grand_total = sum(len(v) for v in by_tag.values())
    for tag, lst in by_tag.items():
        ensure_release(tag)
        have = already_uploaded(tag)
        pending = [z for z in lst if z.name not in have]
        print(f"[{tag}] {len(lst)} total, {len(have)} already uploaded, {len(pending)} pending")
        for i in range(0, len(pending), GROUP):
            chunk = pending[i : i + GROUP]
            ok = upload_batches(tag, chunk)
            if ok == 0 and "release full" in (locals().get('_last', '')):
                break
            total_done += ok
            elapsed = _t.time() - t0
            rate = total_done / max(elapsed, 0.1)
            eta = (grand_total - total_done) / max(rate, 0.01)
            print(f"  [{tag}] +{len(chunk)} (total {total_done}/{grand_total}, {rate:.1f}/s, ETA {eta/60:.1f} min)")
            if i + GROUP < len(pending):
                _t.sleep(INTER_GROUP_SLEEP)
    print("done.")


if __name__ == "__main__":
    main()
