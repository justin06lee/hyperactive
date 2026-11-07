"""Render the catalog image (2:1) and screenshots from dev/media.json into docs/media/.

The messages in media.json are copied from a dev/demo.sh run (made-up demo goals), so the
screenshots show what the plugin actually sends. Needs Pillow; run from the repo root:
    python dev/media.py
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "media"
FONTS = [Path.home() / ".local/share/fonts/inter/extras/ttf", Path("/usr/share/fonts/truetype/dejavu")]

BG, FG, DIM, LINE = "#f4f3ef", "#1d1d1b", "#76746d", "#d9d6cc"
YOU, HERMES = "#dfe9f7", "#ffffff"


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    for name in (("Inter-SemiBold.ttf" if bold else "Inter-Regular.ttf"),
                 ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")):
        for d in FONTS:
            if (d / name).exists():
                return ImageFont.truetype(str(d / name), size)
    return ImageFont.load_default(size)


def wrap(text: str, width: int) -> list[str]:
    lines = []
    for para in text.split("\n"):
        lines += textwrap.wrap(para, width) or [""]
    return lines


def bubble(draw, x, y, w, text, fill, f, small, meta=None) -> int:
    """Draw one message bubble at (x, y), width w. Returns the y below it."""
    lines = wrap(text, int((w - 48) / (f.size * 0.53)))
    lh = int(f.size * 1.4)
    h = 28 + lh * len(lines) + (int(small.size * 1.6) if meta else 0)
    draw.rounded_rectangle((x, y, x + w, y + h), radius=22, fill=fill, outline=LINE, width=2)
    ty = y + 14
    for ln in lines:
        draw.text((x + 24, ty), ln, font=f, fill=FG)
        ty += lh
    if meta:
        draw.text((x + 24, ty + 4), meta, font=small, fill=DIM)
    return y + h + 22


def screen(spec: dict, path: Path) -> None:
    W, H = 1200, 1000
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((60, 44), spec["title"], font=font(40, True), fill=FG)
    d.text((60, 100), spec["caption"], font=font(26), fill=DIM)
    y = 170
    f, small = font(30), font(22)
    for m in spec["messages"]:
        mine = m["from"] == "you"
        w = 820
        x = W - 60 - w if mine else 60
        y = bubble(d, x, y, w, m["text"], YOU if mine else HERMES, f, small, m.get("meta"))
    img = img.crop((0, 0, W, min(H, y + 30)))
    img.save(path, optimize=True)


def banner(spec: dict, path: Path) -> None:
    W, H = 1200, 600  # 2:1, as the catalog asks
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((70, 80), "hyperactive", font=font(76, True), fill=FG)
    d.text((74, 180), spec["tagline"], font=font(34), fill=DIM)
    y = 260
    for ln in wrap(spec["blurb"], 40):
        d.text((74, y), ln, font=font(26), fill=FG)
        y += 38
    bubble(d, 660, 250, 480, spec["card"], HERMES, font(26), font(20), spec["card_meta"])
    img.save(path, optimize=True)


def main() -> None:
    spec = json.loads((ROOT / "dev" / "media.json").read_text())
    (OUT / "screens").mkdir(parents=True, exist_ok=True)
    banner(spec["banner"], OUT / "banner.png")
    for i, s in enumerate(spec["screens"], 1):
        screen(s, OUT / "screens" / f"{i:02d}-{s['slug']}.png")
    print("\n".join(str(p.relative_to(ROOT)) for p in sorted(OUT.rglob("*.png"))))


if __name__ == "__main__":
    main()
