#!/usr/bin/env python3
"""generate_icon.py — draw SOC Ultralight's app icon and its freedesktop ladder.

SOC had no app icon of its own: assets/ob_icon.ico is the OUTBOX icon ("OB"),
which only ever stood in for one. This draws a real "SOC" mark in the same
house style as the sibling icons — a square near-black tile with heavy sans
letters filling ~70% of the width, measured off ob_icon.ico:
    tile   #010101       glyph bbox (39, 74)-(217, 176) of 256x256
Master Widget's mark is blue on the same tile, so SOC stays white to keep the
two apart at dash size.

Usage:  python3 packaging/generate_icon.py [--install]
        --install also writes ~/.local/share/icons/hicolor/<N>x<N>/apps/.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SLUG = "soc-ultralight"
TEXT = "SOC"
TILE = (1, 1, 1, 255)          # #010101, matching ob_icon.ico and master_widget.ico
INK = (254, 254, 254, 255)     # same white the OB mark uses
SIZES = [16, 24, 32, 48, 64, 128, 256]
TARGET_W_FRAC = 0.70           # 178/256, measured off the OB mark
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# Render at 4x and downsample: the ladder's small sizes (16, 24) are where
# aliasing on the S and C curves shows, and LANCZOS off a large master is
# cleaner than hinting each size independently.
SUPER = 1024


def render(size: int = SUPER) -> Image.Image:
    img = Image.new("RGBA", (size, size), TILE)
    draw = ImageDraw.Draw(img)

    # Binary-search the point size whose rendered ink is TARGET_W_FRAC wide.
    target_w = size * TARGET_W_FRAC
    lo, hi, best = 1, size * 2, None
    while lo <= hi:
        mid = (lo + hi) // 2
        font = ImageFont.truetype(FONT, mid)
        l, t, r, b = draw.textbbox((0, 0), TEXT, font=font)
        if r - l <= target_w:
            best = (mid, font, (l, t, r, b))
            lo = mid + 1
        else:
            hi = mid - 1
    if best is None:
        raise RuntimeError("could not fit text")
    _, font, (l, t, r, b) = best

    # Centre on the INK bbox, not the font's line box: the ascender/descender
    # slack would otherwise push a caps-only word visibly above centre.
    draw.text(((size - (r - l)) / 2 - l, (size - (b - t)) / 2 - t),
              TEXT, font=font, fill=INK)
    return img


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--install", action="store_true",
                    help="also write the hicolor ladder under ~/.local/share/icons")
    args = ap.parse_args(argv)

    if not Path(FONT).is_file():
        print(f"font not found: {FONT}", file=sys.stderr)
        return 1

    assets = Path(__file__).resolve().parent.parent / "assets"
    assets.mkdir(exist_ok=True)
    master = render()

    hicolor = Path.home() / ".local/share/icons/hicolor"
    for n in SIZES:
        img = master.resize((n, n), Image.LANCZOS)
        if n == 256:
            img.save(assets / f"{SLUG}.png", "PNG")
            print(f"wrote  {assets / f'{SLUG}.png'}")
        if args.install:
            out = hicolor / f"{n}x{n}" / "apps" / f"{SLUG}.png"
            out.parent.mkdir(parents=True, exist_ok=True)
            img.save(out, "PNG")
            print(f"wrote  {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
