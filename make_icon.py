# -*- coding: utf-8 -*-
"""生成 Apple Liquid Glass 质感多尺寸图标（icon.png + icon.ico）。

特性：
1) 若指定原图或 scratch 艺术底图则自动加载，否则内置高品质无泥灰渐变矢量绘制兜底；
2) 在 1024×1024 主画布上施加 Apple 连续曲率超椭圆（|x/R|^4.6 + |y/R|^4.6 <= 1）
   与边界 4×4 子像素超采样抗锯齿遮罩；
3) 叠加顶部与四周细腻液态玻璃内高光描边 + 轻柔悬浮投影，导出 512px icon.png 与全尺寸 icon.ico。
"""
import argparse
import math
from pathlib import Path
from PIL import Image, ImageChops, ImageDraw, ImageFilter

DEFAULT_SRC = Path(
    r"C:\Users\OMEN 11\.gemini\antigravity\brain\d1399b42-fa79-40c5-8a81-d64e95737dbe\app_icon_liquid_glass_1795055309553.jpg"
)
ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def build_squircle_mask(size=1024, radius_frac=0.455, power=4.6):
    """生成带边界 4×4 超采样抗锯齿的 Apple 连续曲率超椭圆 Alpha 遮罩。"""
    half = size // 2
    r = size * radius_frac
    q = Image.new("L", (half, half), 0)
    pix = q.load()

    inv_p = 1.0 / power
    for y in range(half):
        dy = (half - 0.5 - y)
        ay = dy / r
        for x in range(half):
            dx = (half - 0.5 - x)
            ax = dx / r
            # 先按欧氏与切比雪夫快捷界剔除内外纯色像素，仅对窄边缘带做 4×4 超采样
            d_est = (ax ** power + ay ** power) ** inv_p
            if d_est < 0.96:
                pix[x, y] = 255
            elif d_est > 1.04:
                pix[x, y] = 0
            else:
                inside = 0
                for sy in range(4):
                    py_sub = (half - 1 - y) + (sy + 0.5) / 4.0
                    ny = (py_sub / r) ** power
                    for sx in range(4):
                        px_sub = (half - 1 - x) + (sx + 0.5) / 4.0
                        if (px_sub / r) ** power + ny <= 1.0:
                            inside += 1
                pix[x, y] = int(round(inside * 255 / 16))

    mask = Image.new("L", (size, size), 0)
    q_tr = q.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    q_bl = q.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    q_br = q_tr.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    mask.paste(q, (0, 0))
    mask.paste(q_tr, (half, 0))
    mask.paste(q_bl, (0, half))
    mask.paste(q_br, (half, half))
    return mask


def _fallback_vector_art(size=1024):
    """当外部艺术原图不存在时的内置精致深邃蓝紫渐变 + 磨砂柱图兜底绘制。"""
    img = Image.new("RGBA", (size, size))
    d = ImageDraw.Draw(img)
    c_top_l = (0, 102, 255)
    c_bot_r = (79, 70, 229)
    for y in range(size):
        t = y / (size - 1)
        r = int(c_top_l[0] + (c_bot_r[0] - c_top_l[0]) * t)
        g = int(c_top_l[1] + (c_bot_r[1] - c_top_l[1]) * t)
        b = int(c_top_l[2] + (c_bot_r[2] - c_top_l[2]) * t)
        d.line([(0, y), (size, y)], fill=(r, g, b, 255))
    return img


def render_icons(src_path, out_dir):
    out_dir = Path(out_dir)
    S = 1024

    if src_path and Path(src_path).exists():
        art = Image.open(src_path).convert("RGBA")
        # 居中裁为正方形并略微放大 4% 贴合圆角边距
        min_dim = min(art.width, art.height)
        left = (art.width - min_dim) // 2
        top = (art.height - min_dim) // 2
        art = art.crop((left, top, left + min_dim, top + min_dim))
        art = art.resize((S, S), Image.Resampling.LANCZOS)
    else:
        art = _fallback_vector_art(S)

    # 1) 生成 Apple Squircle 遮罩
    mask = build_squircle_mask(S, radius_frac=0.455, power=4.6)

    # 2) 外侧极轻环境悬浮投影（保持清晰 4.5% 安全边距内）
    shadow_alpha = ImageChops.multiply(
        mask,
        Image.new("L", (S, S), 90),
    )
    shadow = Image.new("RGBA", (S, S), (8, 22, 54, 0))
    shadow_layer = Image.new("RGBA", (S, S), (8, 22, 54, 255))
    shadow_layer.putalpha(shadow_alpha)
    shadow.paste(shadow_layer, (0, 14), shadow_alpha)
    shadow = shadow.filter(ImageFilter.GaussianBlur(18))

    # 3) 裁剪主体 + 添加细腻玻璃内边缘高光（1.5px 内缩环带）
    body = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    body.paste(art, (0, 0), mask)

    eroded = mask.filter(ImageFilter.MinFilter(5))
    rim_ring = ImageChops.subtract(mask, eroded)
    # 顶部高光更强、底部极柔
    rim_grad = Image.new("L", (S, S), 0)
    rgd = ImageDraw.Draw(rim_grad)
    for y in range(S):
        alpha_v = int(125 * (1.0 - (y / (S - 1)) * 0.72))
        rgd.line([(0, y), (S, y)], fill=alpha_v)
    rim_alpha = ImageChops.multiply(rim_ring, rim_grad)
    rim = Image.new("RGBA", (S, S), (255, 255, 255, 255))
    rim.putalpha(rim_alpha)
    body = Image.alpha_composite(body, rim)

    master = Image.alpha_composite(shadow, body)

    # 4) 导出高清 icon.png (512×512) 与全尺寸 icon.ico
    png_path = out_dir / "icon.png"
    ico_path = out_dir / "icon.ico"
    master.resize((512, 512), Image.Resampling.LANCZOS).save(png_path, "PNG", optimize=True)
    master.save(ico_path, format="ICO", sizes=ICO_SIZES)
    print(f"ICON_SAVED_OK: {png_path} & {ico_path}")
    return master


def make_showcase(master, dest_path):
    """生成浅色/深色双背景的多尺寸图标预览对照图。"""
    W, H = 960, 480
    canvas = Image.new("RGBA", (W, H), (242, 242, 247, 255))
    draw = ImageDraw.Draw(canvas)
    # 右半为深色背景对比
    draw.rectangle([W // 2, 0, W, H], fill=(28, 28, 30, 255))

    for side, x_base in enumerate((0, W // 2)):
        big = master.resize((220, 220), Image.Resampling.LANCZOS)
        canvas.paste(big, (x_base + 130, 66), big)

        x_cur = x_base + 82
        for sz in (96, 64, 48, 32, 16):
            ico_sm = master.resize((sz, sz), Image.Resampling.LANCZOS)
            y_pos = 340 + (96 - sz) // 2
            canvas.paste(ico_sm, (x_cur, y_pos), ico_sm)
            x_cur += sz + 24

    canvas.convert("RGB").save(dest_path, "PNG", optimize=True)
    print(f"SHOWCASE_SAVED_OK: {dest_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", default=str(DEFAULT_SRC if DEFAULT_SRC.exists() else ""))
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent))
    parser.add_argument("--showcase", default="")
    args = parser.parse_args()

    master_img = render_icons(args.src, args.out)
    if args.showcase:
        make_showcase(master_img, args.showcase)
