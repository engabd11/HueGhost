"""Draw the Hue Ghost icon (same shape the app paints) into installer/hueghost.ico."""
import os
import sys

from PIL import Image, ImageDraw

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "installer", "hueghost.ico")
ACCENT = (124, 92, 255, 255)
BG = (15, 17, 21, 255)


def ghost(size: int) -> Image.Image:
    s = size * 4  # supersample
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((s * 0.12, s * 0.08, s * 0.88, s * 0.92), radius=s * 0.38, fill=ACCENT)
    for i in range(3):
        x = s * (0.12 + i * 0.253)
        d.ellipse((x, s * 0.80, x + s * 0.253, s * 1.04), fill=(0, 0, 0, 0))
    e = s * 0.11
    for cx in (0.36, 0.64):
        d.ellipse((s * cx - e / 2, s * 0.40 - e / 2, s * cx + e / 2, s * 0.40 + e / 2), fill=(255, 255, 255, 255))
    return im.resize((size, size), Image.LANCZOS)


def main() -> int:
    sizes = [16, 24, 32, 48, 64, 128, 256]
    frames = [ghost(n) for n in sizes]
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    frames[-1].save(OUT, format="ICO", sizes=[(n, n) for n in sizes], append_images=frames[:-1])
    print("wrote", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
