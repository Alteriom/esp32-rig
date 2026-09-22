#!/usr/bin/env python3
"""Generate the farm's raster brand assets from mark.svg.

The mark is drawn once, as SVG (mark.svg; favicon.svg is the same idea drawn
for 16 pixels). Everything that wants a PNG -- a home screen, a mail client,
a link preview -- is rasterised from that one file here, so there is no
second drawing to keep in step.

    python3 generate_brand_assets.py        # writes the PNGs beside this file

What it writes (all committed, so the site needs no build step):

    icon-192.png, icon-512.png    the web app manifest's icons
    apple-touch-icon.png          180 x 180, iOS home screen
    logo-email.png                the lockup on a transparent ground, for
                                  NorthRelay's brand theme (a light email)
    og.png                        1200 x 630, link previews

Needs `resvg-py` (pip) to rasterise and Pillow to compose; both are for the
machine that regenerates these, not for the site. Fonts for the words:
whatever that machine has, tried in order.
"""

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
MARK = HERE / "mark.svg"

# ---- the palette, as app.css has it -------------------------------------------
INK = (237, 247, 243)
MUTED = (143, 168, 160)
GROUND = (8, 13, 12)
PANEL = (17, 26, 24)
ACCENT = (99, 230, 190)
INK_ON_LIGHT = (16, 24, 22)
MUTED_ON_LIGHT = (92, 111, 105)


def rasterise(size: int) -> Image.Image:
    """mark.svg at `size` pixels square, RGBA."""
    import resvg_py  # the one dependency the site itself never needs

    png = resvg_py.svg_to_bytes(svg_path=str(MARK), width=size, height=size)
    return Image.open(BytesIO(png)).convert("RGBA")


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    names = ["seguisb.ttf", "segoeui.ttf", "arialbd.ttf", "arial.ttf",
             "DejaVuSans-Bold.ttf", "DejaVuSans.ttf"] if bold else [
             "segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"]
    for name in names:
        for folder in ("C:/Windows/Fonts/", "/usr/share/fonts/truetype/dejavu/", ""):
            try:
                return ImageFont.truetype(folder + name, size)
            except OSError:
                continue
    return ImageFont.load_default(size)


def write_icons() -> None:
    for size in (192, 512):
        rasterise(size).save(HERE / f"icon-{size}.png")
    # iOS puts no rounded corner of its own on it and shows it on whatever
    # wallpaper: the tile's ground goes square to the edge behind the mark.
    apple = Image.new("RGB", (180, 180), PANEL)
    mark = rasterise(180)
    apple.paste(mark, (0, 0), mark)
    apple.save(HERE / "apple-touch-icon.png")


def write_email_logo() -> None:
    """The lockup for a light email: the mark and the two-line name, on a
    transparent ground, at twice the size a mail client shows it."""
    width, height = 560, 160
    logo = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    mark = rasterise(136)
    logo.paste(mark, (12, 12), mark)
    draw = ImageDraw.Draw(logo)
    draw.text((176, 36), "ALTERIOM", font=font(30, bold=True), fill=MUTED_ON_LIGHT)
    draw.text((176, 74), "ESP32 FARM", font=font(48, bold=True), fill=INK_ON_LIGHT)
    logo.save(HERE / "logo-email.png")


def write_og() -> None:
    """1200 x 630, the one image a link preview shows: the mark large, the
    name, and the three lines the world page opens with."""
    width, height = 1200, 630
    card = Image.new("RGBA", (width, height), (*GROUND, 255))
    big = rasterise(440)
    card.paste(big, (700, 95), big)
    draw = ImageDraw.Draw(card)
    mark = rasterise(84)
    card.paste(mark, (72, 74), mark)
    draw.text((176, 84), "ALTERIOM", font=font(26, bold=True), fill=MUTED)
    draw.text((176, 116), "ESP32 FARM", font=font(34, bold=True), fill=INK)
    draw.text((72, 250), "Real boards.", font=font(76, bold=True), fill=INK)
    draw.text((72, 330), "Real radio.", font=font(76, bold=True), fill=INK)
    draw.text((72, 410), "Real runs.", font=font(76, bold=True), fill=ACCENT)
    draw.text((72, 516), "Hardware-in-the-loop testing for ESP32 firmware,",
              font=font(26), fill=MUTED)
    draw.text((72, 552), "on rigs you can see.", font=font(26), fill=MUTED)
    card.convert("RGB").save(HERE / "og.png")


def main() -> None:
    write_icons()
    write_email_logo()
    write_og()
    for name in ("icon-192.png", "icon-512.png", "apple-touch-icon.png", "logo-email.png", "og.png"):
        print(f"wrote {name}")


if __name__ == "__main__":
    main()
