"""Generate the VoiceType app logo (a mic on a rounded gradient tile).

Outputs (committed, so the build doesn't need Pillow):
    voicetype/assets/voicetype.ico   - multi-size icon for shortcuts + the .exe
    voicetype/assets/voicetype.png   - 256px logo for the README

Run:  .venv/Scripts/python.exe tools/make_icon.py
"""
from __future__ import annotations

import os
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "voicetype", "assets")

TOP = (99, 102, 241)     # indigo
BOTTOM = (147, 81, 236)  # violet
WHITE = (255, 255, 255, 255)


def _lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def render(size: int = 256) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    # Vertical gradient tile.
    grad = Image.new("RGB", (size, size), TOP)
    gd = ImageDraw.Draw(grad)
    for y in range(size):
        gd.line([(0, y), (size, y)], fill=_lerp(TOP, BOTTOM, y / (size - 1)))

    # Rounded-square mask (app-icon look).
    mask = Image.new("L", (size, size), 0)
    md = ImageDraw.Draw(mask)
    m = max(1, int(size * 0.06))
    r = int(size * 0.23)
    md.rounded_rectangle([m, m, size - m, size - m], radius=r, fill=255)
    img.paste(grad, (0, 0), mask)

    # White microphone, scaled from a 256px design.
    d = ImageDraw.Draw(img)
    s = size / 256.0
    def S(v):  # noqa: E306
        return v * s
    lw = max(1, int(S(13)))
    d.rounded_rectangle([S(106), S(62), S(150), S(150)], radius=S(22), fill=WHITE)  # head
    d.arc([S(82), S(94), S(174), S(186)], start=20, end=160, fill=WHITE, width=lw)   # cradle
    d.line([S(128), S(184), S(128), S(208)], fill=WHITE, width=lw)                   # stem
    d.line([S(104), S(208), S(152), S(208)], fill=WHITE, width=lw)                   # base
    return img


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    base = render(256)
    base.save(os.path.join(OUT, "voicetype.png"))
    base.save(
        os.path.join(OUT, "voicetype.ico"),
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print("wrote", os.path.join(OUT, "voicetype.ico"))
    print("wrote", os.path.join(OUT, "voicetype.png"))


if __name__ == "__main__":
    main()
