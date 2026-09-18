"""「信」アイコン生成スクリプト。favicon-32.png / favicon-192.png / apple-touch-icon.png を生成する。"""
from PIL import Image, ImageDraw, ImageFont
import numpy as np
import os

FONT_PATH = r"C:\Windows\Fonts\YuGothB.ttc"
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "docs")

C_TOP    = (10, 61, 110)    # #0a3d6e
C_BOT    = (13, 110, 189)   # #0d6ebd
C_TEXT   = (255, 255, 255)
C_GOLD   = (240, 185, 50)   # #f0b932


def gradient_image(size: int) -> Image.Image:
    w = h = size
    arr = np.zeros((h, w, 4), dtype=np.uint8)
    xs = np.linspace(0, 1, w)
    ys = np.linspace(0, 1, h)
    xg, yg = np.meshgrid(xs, ys)
    t = (xg + yg) / 2  # diagonal gradient
    for c in range(3):
        arr[:, :, c] = (C_TOP[c] + t * (C_BOT[c] - C_TOP[c])).astype(np.uint8)
    arr[:, :, 3] = 255
    return Image.fromarray(arr, "RGBA")


def rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return mask


def make_icon(size: int, font_size: int, corner_r: int, show_gold: bool = True) -> Image.Image:
    base = gradient_image(size)

    # Rounded corner composite
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(base, mask=rounded_mask(size, corner_r))

    draw = ImageDraw.Draw(out)
    font = ImageFont.truetype(FONT_PATH, font_size)
    text = "貸"

    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]

    # 文字をやや上寄りにして金ラインのスペースを確保
    cy_frac = 0.44 if show_gold else 0.50
    cx, cy = size // 2, int(size * cy_frac)
    tx = cx - tw // 2 - bbox[0]
    ty = cy - th // 2 - bbox[1]

    # ドロップシャドウ（大きいサイズのみ）
    if size >= 64:
        shadow_offset = max(1, size // 96)
        draw.text((tx + shadow_offset, ty + shadow_offset), text,
                  font=font, fill=(0, 0, 0, 70))

    draw.text((tx, ty), text, font=font, fill=C_TEXT)

    # ゴールドアクセントライン
    if show_gold:
        lw  = int(size * 0.44)
        lh  = max(2, int(size * 0.022))
        lx  = (size - lw) // 2
        ly  = int(size * 0.73)
        draw.rounded_rectangle([lx, ly, lx + lw, ly + lh], radius=lh // 2, fill=C_GOLD)

    return out


def save(img: Image.Image, path: str) -> None:
    # Apple Touch Icon / ホーム画面アイコンは白背景に乗せた RGB で保存するのが安全
    if path.endswith("apple-touch-icon.png"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[3])
        bg.save(path, "PNG", optimize=True)
    else:
        img.save(path, "PNG", optimize=True)
    print(f"  saved: {os.path.basename(path)} ({os.path.getsize(path)//1024}KB)")


if __name__ == "__main__":
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    configs = [
        ("favicon-32.png",        32,  24,  6, False),
        ("apple-touch-icon.png", 180, 126, 36, True),
        ("favicon-192.png",      192, 134, 38, True),
    ]

    for fname, size, fsize, cr, gold in configs:
        img = make_icon(size, fsize, cr, gold)
        save(img, os.path.join(OUTPUT_DIR, fname))

    print("Done.")
