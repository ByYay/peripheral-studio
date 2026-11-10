"""Draw the original application icon: python packaging/make_icon.py

Writes desktop/assets/upm.ico (used by the EXE and the window) and desktop/assets/upm.png
(the tray icon). A cute 16x16 pixel creature wearing a headset, scaled only by whole
multiples so every pixel stays sharp.
"""
from pathlib import Path

from PIL import Image

PIXELS = [
    "................",
    "....oooooooo....",
    "..oohhhhhhhhoo..",
    ".ohhoooooooohho.",
    ".ohobbbbbbbboho.",
    "ohhobllbbbbdohho",
    "oghobwebbwedogho",
    "oghobeebbeedogho",
    "ohhobeebbeedohho",
    "ohhokbebbebkohho",
    "ohhobbbeebbdohho",
    ".ooobbbbbbbdooo.",
    "...obbbbbbbdo...",
    "...obboooobdo...",
    "....oo....oo....",
    "................",
]
COLORS = {
    "o": "#0b2233",  # outline, eyes and mouth
    "e": "#0b2233",
    "b": "#2ad4e6",  # body
    "l": "#b4f6ff",  # shine
    "d": "#1199b4",  # shade
    "h": "#8b5cf6",  # headset
    "g": "#c4b5fd",  # headset shine
    "w": "#ffffff",  # eye sparkle
    "k": "#ff8fb3",  # cheeks
}
SIZES = [16, 32, 48, 64, 128, 256]  # whole multiples of the 16 px grid
assets = Path(__file__).resolve().parents[1] / 'desktop' / 'assets'


def draw():
    image = Image.new('RGBA', (16, 16), (0, 0, 0, 0))
    for y, row in enumerate(PIXELS):
        for x, cell in enumerate(row):
            if cell != '.':
                image.putpixel((x, y), Image.new('RGB', (1, 1), COLORS[cell]).getpixel((0, 0)) + (255,))
    return image


if __name__ == '__main__':
    assert len(PIXELS) == 16 and all(len(row) == 16 for row in PIXELS)
    assets.mkdir(parents=True, exist_ok=True)
    # Every size is scaled here with NEAREST; letting the ICO writer resize would blur the pixels.
    frames = [draw().resize((size, size), Image.NEAREST) for size in SIZES]
    frames[-1].save(assets / 'upm.ico', sizes=[(s, s) for s in SIZES], append_images=frames[:-1])
    frames[-1].save(assets / 'upm.png')
    print('written', assets)
