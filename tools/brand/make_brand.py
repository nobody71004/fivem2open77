"""Make the app's icon and logo files from the two F2077 images in docs/brand.

    python tools/brand/make_brand.py          (a dev tool: needs Pillow and numpy)

docs/brand/f2077-emblem.jpg, the emblem, is the icon:
    host/app.ico                      the exe, its windows, the taskbar, shortcuts, the setup
    host/emblem.png                   the emblem on its own (no tile): the start screen and the installer
    studio/web/img/icon-32.png        favicon
    studio/web/img/icon-192.png       favicon (large)
    studio/web/img/emblem.png         the title bar's mark
docs/brand/f2077-logo.jpg, the neon wall, is the logo:
    studio/web/img/logo.jpg           the Home page's banner (the README shows docs/brand/f2077-logo.jpg)

The icon is the emblem on its own navy, cut to a rounded tile: the 16 to 64 px sizes as 32-bit
bitmaps (what every Windows API reads), 128 and 256 as PNG.  The small sizes are cut a little
tighter and sharpened, so the F still reads at 16 px.
"""
from __future__ import annotations

import io
import os
import shutil
import struct

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BRAND = os.path.join(ROOT, "docs", "brand")
WEB_IMG = os.path.join(ROOT, "fivem2open77", "studio", "web", "img")
HOST = os.path.join(ROOT, "host")

ICO_BMP = (16, 20, 24, 32, 40, 48, 64)
ICO_PNG = (128, 256)
RADIUS = 0.21                    # tile corner radius, of the side
EMBLEM_BOX = (460, 159, 980, 612)  # the emblem in f2077-emblem.jpg (x0, y0, x1, y1), from its bright pixels
TEXT_BELOW = 630                 # the "F2077 / FUTURE INNOVATIONS" lettering starts under this row


def _blur(a: np.ndarray, sigma: float) -> np.ndarray:
    """Gaussian blur of a float image (H x W or H x W x C), separable, numpy only."""
    r = int(sigma * 3 + 0.5)
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    k /= k.sum()
    out = a.astype(np.float64)
    for axis in (0, 1):
        pad = [(0, 0)] * out.ndim
        pad[axis] = (r, r)
        p = np.pad(out, pad, mode="edge")
        acc = np.zeros_like(out)
        for i, w in enumerate(k):
            sl = [slice(None)] * out.ndim
            sl[axis] = slice(i, i + out.shape[axis])
            acc += w * p[tuple(sl)]
        out = acc
    return out


def emblem_source() -> np.ndarray:
    """f2077-emblem.jpg with the lettering under the emblem painted out (the icon is the emblem):
    the masked rows are filled with the background around them (normalised convolution)."""
    a = np.asarray(Image.open(os.path.join(BRAND, "f2077-emblem.jpg")).convert("RGB")).astype(np.float64)
    lum = a @ np.array([0.299, 0.587, 0.114])
    mask = np.zeros(lum.shape, bool)
    mask[TEXT_BELOW:TEXT_BELOW + 100, 540:880] = True
    mask &= lum > 40
    m = _blur(mask.astype(np.float64), 4.0) > 0.02                 # the letters and their glow
    keep = (~m).astype(np.float64)
    fill = _blur(a * keep[..., None], 14.0) / np.maximum(_blur(keep, 14.0), 1e-6)[..., None]
    a[m] = fill[m]
    return a


def tile(src: np.ndarray, size: int, side: float, sharpen=False) -> Image.Image:
    """A rounded square of the emblem: `side` source pixels around the emblem's centre, at `size`."""
    x0, y0, x1, y1 = EMBLEM_BOX
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    box = (int(round(cx - side / 2)), int(round(cy - side / 2)), int(round(cx + side / 2)), int(round(cy + side / 2)))
    im = Image.fromarray(np.clip(src, 0, 255).astype(np.uint8)).crop(box).resize((size, size), Image.LANCZOS)
    if sharpen:
        im = im.filter(ImageFilter.UnsharpMask(radius=0.8, percent=70, threshold=1))
    ss = 8
    mask = Image.new("L", (size * ss, size * ss), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size * ss - 1, size * ss - 1], radius=RADIUS * size * ss, fill=255)
    im = im.convert("RGBA")
    im.putalpha(mask.resize((size, size), Image.LANCZOS).point(lambda v: 0 if v < 8 else v))   # no ringing outside
    return im


def icon_image(src, size: int) -> Image.Image:
    return tile(src, size, 520 if size <= 32 else 560, sharpen=size <= 48)


def keyed_emblem(src: np.ndarray, width: int) -> Image.Image:
    """The emblem without its background: difference matting against the navy behind it, kept to
    the emblem and its glow (the circuit lines further out go)."""
    x0, y0, x1, y1 = EMBLEM_BOX
    m = 34
    crop = src[y0 - m:y1 + m, x0 - m:x1 + m]
    border = np.concatenate([crop[:6].reshape(-1, 3), crop[-6:].reshape(-1, 3), crop[:, :6].reshape(-1, 3),
                             crop[:, -6:].reshape(-1, 3)])
    bg = np.median(border, axis=0)
    d = np.abs(crop - bg).max(axis=2)
    alpha = np.clip((d - 10.0) / 70.0, 0, 1)
    core = _blur((d > 60).astype(np.float64), 10.0) > 0.02            # near the emblem itself
    alpha *= np.clip(_blur(core.astype(np.float64), 6.0) * 1.6, 0, 1)
    rgb = bg + (crop - bg) / np.maximum(alpha, 1e-3)[..., None]       # the colour that over `bg` gives the source
    rgb = np.where(alpha[..., None] > 0.02, rgb, bg)
    rgba = np.dstack([np.clip(rgb, 0, 255), alpha * 255]).astype(np.uint8)
    im = Image.fromarray(rgba, "RGBA")
    h = round(im.height * width / im.width)
    return im.resize((width, h), Image.LANCZOS)


def write_ico(path: str, images: dict):
    """images: {size: RGBA Image}.  Bitmaps for ICO_BMP sizes, PNG for the rest."""
    entries = []
    for size in sorted(images):
        im = images[size]
        if size in ICO_BMP:
            a = np.asarray(im.convert("RGBA"))
            bgra = a[::-1, :, [2, 1, 0, 3]].tobytes()                 # bottom-up rows, BGRA
            row = ((size + 31) // 32) * 4
            mask = bytearray(row * size)
            for y in range(size):                                     # AND mask: 1 where fully transparent
                src_row = a[size - 1 - y, :, 3]
                for x in range(size):
                    if src_row[x] == 0:
                        mask[y * row + x // 8] |= 0x80 >> (x % 8)
            header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, len(bgra) + len(mask), 0, 0, 0, 0)
            blob = header + bgra + bytes(mask)
        else:
            buf = io.BytesIO()
            im.save(buf, "PNG", optimize=True)
            blob = buf.getvalue()
        entries.append((size, blob))
    out = struct.pack("<HHH", 0, 1, len(entries))
    offset = 6 + 16 * len(entries)
    for size, blob in entries:
        dim = 0 if size >= 256 else size
        out += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
    with open(path, "wb") as f:
        f.write(out + b"".join(b for _, b in entries))


def main():
    src = emblem_source()
    write_ico(os.path.join(HOST, "app.ico"), {s: icon_image(src, s) for s in ICO_BMP + ICO_PNG})
    keyed_emblem(src, 320).save(os.path.join(HOST, "emblem.png"), optimize=True)
    os.makedirs(WEB_IMG, exist_ok=True)
    icon_image(src, 32).save(os.path.join(WEB_IMG, "icon-32.png"), optimize=True)
    icon_image(src, 192).save(os.path.join(WEB_IMG, "icon-192.png"), optimize=True)
    keyed_emblem(src, 128).save(os.path.join(WEB_IMG, "emblem.png"), optimize=True)
    shutil.copyfile(os.path.join(BRAND, "f2077-logo.jpg"), os.path.join(WEB_IMG, "logo.jpg"))   # as it is
    for p in (os.path.join(HOST, "app.ico"), os.path.join(HOST, "emblem.png"), os.path.join(WEB_IMG, "icon-32.png"),
              os.path.join(WEB_IMG, "icon-192.png"), os.path.join(WEB_IMG, "emblem.png"), os.path.join(WEB_IMG, "logo.jpg")):
        print(f"{os.path.relpath(p, ROOT)}: {os.path.getsize(p)} bytes")


if __name__ == "__main__":
    main()
