"""
утилиты для подготовки картинок под artlist toolkit.
artlist отказывается принимать PNG с альфа-каналом — сводим к JPG на белом фоне.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PIL import Image

JPG_QUALITY = 92
MAX_SIDE = 2048  # ограничим длинную сторону, чтобы не таскать гигабайты в S3


def ensure_jpg(src: Path, out_dir: Optional[Path] = None, bg_color: tuple[int, int, int] = (255, 255, 255)) -> Path:
    """
    если src — PNG/WEBP/etc с альфой — flatten на белом фоне и сохраняет .jpg.
    если src — уже JPG — возвращает как есть.
    если src — PNG без альфы — конвертит в JPG (всё равно безопаснее).
    out_dir: куда сохранять. по умолчанию рядом с src.
    """
    src = Path(src).resolve()
    if not src.exists():
        raise FileNotFoundError(src)
    ext = src.suffix.lower()
    if ext in (".jpg", ".jpeg"):
        return src

    out_dir = (out_dir or src.parent).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / (src.stem + ".jpg")

    with Image.open(src) as img:
        img.load()
        # ресайз если слишком большая
        if max(img.size) > MAX_SIDE:
            scale = MAX_SIDE / max(img.size)
            new_size = (int(img.size[0] * scale), int(img.size[1] * scale))
            img = img.resize(new_size, Image.LANCZOS)

        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            rgba = img.convert("RGBA")
            bg = Image.new("RGB", rgba.size, bg_color)
            bg.paste(rgba, mask=rgba.split()[3])
            rgba.close()
            out = bg
        elif img.mode != "RGB":
            out = img.convert("RGB")
        else:
            out = img.copy()

        out.save(dst, "JPEG", quality=JPG_QUALITY, optimize=True, progressive=True)
        if out is not img:
            try:
                out.close()
            except Exception:
                pass
    return dst


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        r = ensure_jpg(Path(p))
        print(f"{p} -> {r}  ({r.stat().st_size} bytes)")
