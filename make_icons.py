"""Generate the Noriki app icons.

The mark is the product thesis: two lanes, unequal. The short amber bar is
yours; the long cyan one is the machine's. If the amber bar is ever the
longer of the two, the app has failed at its job.

    python make_icons.py
"""

import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")

GROUND = (14, 18, 22)
MANUAL = (232, 163, 61)
AUTO = (79, 179, 201)


def make(size: int) -> Image.Image:
    img = Image.new("RGB", (size, size), GROUND)
    d = ImageDraw.Draw(img)

    u = size / 24.0                      # design on a 24-unit grid
    bar_h = 3.1 * u
    radius = bar_h / 2
    left = 4 * u
    gap = 2.6 * u
    top = (size - (bar_h * 2 + gap)) / 2

    # yours — short
    d.rounded_rectangle(
        [left, top, left + 8.4 * u, top + bar_h],
        radius=radius, fill=MANUAL,
    )
    # the machine's — long
    d.rounded_rectangle(
        [left, top + bar_h + gap, size - left, top + bar_h * 2 + gap],
        radius=radius, fill=AUTO,
    )
    return img


if __name__ == "__main__":
    for px in (192, 512):
        out = os.path.join(STATIC, f"icon-{px}.png")
        make(px).save(out, "PNG", optimize=True)
        print(f"wrote {out}")

    make(180).save(os.path.join(STATIC, "apple-touch-icon.png"), "PNG", optimize=True)
    print("wrote apple-touch-icon.png")

    make(32).save(os.path.join(STATIC, "favicon.png"), "PNG", optimize=True)
    print("wrote favicon.png")
