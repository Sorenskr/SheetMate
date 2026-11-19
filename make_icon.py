# -*- coding: utf-8 -*-
# pyright: reportMissingImports=false, reportMissingModuleSource=false
"""生成 Apple Liquid Glass 质感多尺寸图标（icon.png + icon.ico）。

特性：
1) 若指定原图或 scratch 艺术底图则自动加载，否则内置高品质无泥灰渐变矢量绘制兜底；
2) 在 1024×1024 主画布上施加 Apple 连续曲率超椭圆（|x/R|^4.6 + |y/R|^4.6 <= 1）
   与边界 4×4 子像素超采样抗锯齿遮罩；
3) 叠加顶部与四周细腻液态玻璃内高光描边 + 轻柔悬浮投影，导出 512px icon.png 与全尺寸 icon.ico。
"""
import argparse
import io
import math
import struct
from pathlib import Path
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter  # type: ignore

DEFAULT_SRC = Path(
    r"C:\Users\OMEN 11\.gemini\antigravity\brain\d1399b42-fa79-40c5-8a81-d64e95737dbe\app_icon_liquid_glass_1795055309553.jpg"
)
DEFAULT_WING = Path(__file__).resolve().parent / "dingtalk_wing.png"
ICO_SIZES = [(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)]


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


def _patch_old_plane_badge(art, cx=709, cy=297, radius=88):
    """平滑修补底图右上角原纸飞机徽标圆域，确保无任何残留边缘影响新徽章合成。"""
    blurred = art.crop((cx - radius - 24, cy - radius - 24, cx + radius + 24, cy + radius + 24)).filter(
        ImageFilter.GaussianBlur(26)
    )
    patch = Image.new("RGBA", art.size, (0, 0, 0, 0))
    patch.paste(blurred, (cx - radius - 24, cy - radius - 24))

    mask = Image.new("L", art.size, 0)
    md = ImageDraw.Draw(mask)
    md.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(12))
    out = art.copy()
    out.paste(patch, (0, 0), mask)
    return out


def _build_dingtalk_liquid_badge(wing_path, size=214):
    """以 4× 超采样生成带液态玻璃高光、晶体描边与悬浮阴影的钉钉翼标超椭圆徽章。"""
    pad = 24
    hi_pad = pad * 4
    hi_s = size * 4
    hi_canvas_s = hi_s + hi_pad * 2

    # 1) 天青至宝石蓝对角渐变底色 + 内部高光折射光晕
    bg = Image.new("RGBA", (hi_s, hi_s), (0, 0, 0, 0))
    px = bg.load()
    for y in range(hi_s):
        ty = y / (hi_s - 1)
        for x in range(hi_s):
            tx = x / (hi_s - 1)
            t = 0.56 * ty + 0.44 * tx
            r = int(34 * (1 - t) + 0 * t)
            g = int(214 * (1 - t) + 84 * t)
            b = int(255 * (1 - t) + 242 * t)
            # 左上角内部水润青白折射光斑
            dx = (x - hi_s * 0.28) / (hi_s * 0.65)
            dy = (y - hi_s * 0.22) / (hi_s * 0.65)
            glow = max(0.0, 1.0 - math.sqrt(dx * dx + dy * dy)) ** 1.8
            r = min(255, int(r + glow * 45))
            g = min(255, int(g + glow * 32))
            px[x, y] = (r, g, b, 255)

    # 2) 顶部弧面玻璃反光
    sheen = Image.new("RGBA", (hi_s, hi_s), (0, 0, 0, 0))
    sd = ImageDraw.Draw(sheen)
    sd.ellipse([-hi_s * 0.15, -hi_s * 0.35, hi_s * 1.15, hi_s * 0.48], fill=(255, 255, 255, 72))
    sheen = sheen.filter(ImageFilter.GaussianBlur(28))
    bg = Image.alpha_composite(bg, sheen)

    sq_mask = build_squircle_mask(hi_s, radius_frac=0.455, power=4.5)

    # 3) 超椭圆裁剪 + 双层玻璃内高光环带
    badge_body = Image.new("RGBA", (hi_s, hi_s), (0, 0, 0, 0))
    badge_body.paste(bg, (0, 0), sq_mask)

    eroded = sq_mask.filter(ImageFilter.MinFilter(13))
    rim_ring = ImageChops.subtract(sq_mask, eroded)
    rim_grad = Image.new("L", (hi_s, hi_s), 0)
    rgd = ImageDraw.Draw(rim_grad)
    for y in range(hi_s):
        av = int(225 * (1.0 - (y / (hi_s - 1)) * 0.62))
        rgd.line([(0, y), (hi_s, y)], fill=av)
    rim = Image.new("RGBA", (hi_s, hi_s), (255, 255, 255, 255))
    rim.putalpha(ImageChops.multiply(rim_ring, rim_grad))
    badge_body = Image.alpha_composite(badge_body, rim)

    # 4) 嵌入居中钉钉纯白立体翼标
    if wing_path and Path(wing_path).exists():
        wing = Image.open(wing_path).convert("RGBA")
        target_w = int(hi_s * 0.72)
        wing = wing.resize((target_w, target_w), Image.Resampling.LANCZOS)
        wx = (hi_s - target_w) // 2 + int(hi_s * 0.01)
        wy = (hi_s - target_w) // 2 + int(hi_s * 0.01)

        w_alpha = wing.getchannel("A")
        w_shadow_layer = Image.new("RGBA", (hi_s, hi_s), (0, 38, 138, 0))
        w_shadow_alpha = Image.new("L", (hi_s, hi_s), 0)
        w_shadow_alpha.paste(ImageChops.multiply(w_alpha, Image.new("L", wing.size, 115)), (wx, wy + 12))
        w_shadow_alpha = w_shadow_alpha.filter(ImageFilter.GaussianBlur(14))
        w_shadow_fill = Image.new("RGBA", (hi_s, hi_s), (0, 38, 138, 255))
        w_shadow_fill.putalpha(w_shadow_alpha)
        badge_body = Image.alpha_composite(badge_body, w_shadow_fill)

        wing_layer = Image.new("RGBA", (hi_s, hi_s), (0, 0, 0, 0))
        wing_layer.paste(wing, (wx, wy), wing)
        badge_body = Image.alpha_composite(badge_body, wing_layer)

    # 5) 柔和深海蓝悬浮外投影
    canvas = Image.new("RGBA", (hi_canvas_s, hi_canvas_s), (0, 0, 0, 0))
    sh_alpha = Image.new("L", (hi_canvas_s, hi_canvas_s), 0)
    sh_alpha.paste(ImageChops.multiply(sq_mask, Image.new("L", (hi_s, hi_s), 150)), (hi_pad, hi_pad + 18))
    sh_alpha = sh_alpha.filter(ImageFilter.GaussianBlur(30))
    sh_fill = Image.new("RGBA", (hi_canvas_s, hi_canvas_s), (4, 34, 116, 255))
    sh_fill.putalpha(sh_alpha)
    canvas = Image.alpha_composite(canvas, sh_fill)
    canvas.paste(badge_body, (hi_pad, hi_pad), badge_body)

    out_s = size + pad * 2
    return canvas.resize((out_s, out_s), Image.Resampling.LANCZOS)


def render_icons(src_path, out_dir, wing_path=None):
    out_dir = Path(out_dir)
    wing_p = Path(wing_path) if wing_path else DEFAULT_WING
    S = 1024

    if src_path and Path(src_path).exists():
        art = Image.open(src_path).convert("RGBA")
        min_dim = min(art.width, art.height)
        left = (art.width - min_dim) // 2
        top = (art.height - min_dim) // 2
        art = art.crop((left, top, left + min_dim, top + min_dim))
        art = art.resize((S, S), Image.Resampling.LANCZOS)
        # 抹除原右上角小飞机，增强整体玻璃色彩清透度与对比度
        art = _patch_old_plane_badge(art, cx=709, cy=297, radius=88)
        art = ImageEnhance.Color(art).enhance(1.09)
        art = ImageEnhance.Contrast(art).enhance(1.04)
    else:
        art = _fallback_vector_art(S)

    # 合成右上角 Apple Liquid Glass 立体钉钉翼标徽章
    badge = _build_dingtalk_liquid_badge(wing_p, size=214)
    bx = 704 - badge.width // 2
    by = 294 - badge.height // 2
    art.paste(badge, (bx, by), badge)

    # 1) 生成 Apple Squircle 遮罩
    mask = build_squircle_mask(S, radius_frac=0.455, power=4.6)

    # 2) 外侧极轻环境悬浮投影（保持清晰 4.5% 安全边距内）
    shadow_alpha = ImageChops.multiply(
        mask,
        Image.new("L", (S, S), 96),
    )
    shadow = Image.new("RGBA", (S, S), (8, 22, 54, 0))
    shadow_layer = Image.new("RGBA", (S, S), (8, 22, 54, 255))
    shadow_layer.putalpha(shadow_alpha)
    shadow.paste(shadow_layer, (0, 14), shadow_alpha)
    shadow = shadow.filter(ImageFilter.GaussianBlur(18))

    # 3) 裁剪主体 + 添加细腻双层液态玻璃内高光与顶部微泛光
    body = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    body.paste(art, (0, 0), mask)

    top_sheen = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    tsd = ImageDraw.Draw(top_sheen)
    tsd.ellipse([S * 0.08, -S * 0.25, S * 0.92, S * 0.36], fill=(255, 255, 255, 22))
    top_sheen = top_sheen.filter(ImageFilter.GaussianBlur(24))
    body.paste(Image.alpha_composite(body, top_sheen), (0, 0), mask)

    eroded = mask.filter(ImageFilter.MinFilter(5))
    rim_ring = ImageChops.subtract(mask, eroded)
    rim_grad = Image.new("L", (S, S), 0)
    rgd = ImageDraw.Draw(rim_grad)
    for y in range(S):
        alpha_v = int(152 * (1.0 - (y / (S - 1)) * 0.70))
        rgd.line([(0, y), (S, y)], fill=alpha_v)
    rim_alpha = ImageChops.multiply(rim_ring, rim_grad)
    rim = Image.new("RGBA", (S, S), (255, 255, 255, 255))
    rim.putalpha(rim_alpha)
    body = Image.alpha_composite(body, rim)

    master = Image.alpha_composite(shadow, body)
    # 针对 16/20/24/32 小尺寸列表视图，裁去外阴影透明空白区，让超椭圆主体饱满填满视野
    tight_pad = int(S * (0.5 - 0.455))
    body_tight = body.crop((tight_pad, tight_pad, S - tight_pad, S - tight_pad))

    # 4) 导出高清 icon.png (512×512) 与全尺寸标准 Win32 DIB+PNG 混合 icon.ico
    png_path = out_dir / "icon.png"
    ico_path = out_dir / "icon.ico"
    master.resize((512, 512), Image.Resampling.LANCZOS).save(png_path, "PNG", optimize=True)
    _write_win32_ico(ico_path, master, body_tight, ICO_SIZES)
    print(f"ICON_SAVED_OK: {png_path} & {ico_path}")
    return master


def _encode_ico_dib(im):
    """将 RGBA Image 编码为标准 Win32 32-bit BGRA BITMAPINFOHEADER + XOR + AND 数据块。"""
    w, h = im.size
    rgba = im.convert("RGBA")
    rows_bgra = []
    # Win32 DIB 像素自底向上存储
    for y in range(h - 1, -1, -1):
        for x in range(w):
            r, g, b, a = rgba.getpixel((x, y))
            rows_bgra.append(bytes((b, g, r, a)))
    xor_data = b"".join(rows_bgra)
    and_stride = ((w + 31) // 32) * 4
    and_data = b"\x00" * (and_stride * h)
    bmi = struct.pack(
        "<IIIHHIIIIII",
        40,             # biSize
        w,              # biWidth
        h * 2,          # biHeight (XOR + AND)
        1,              # biPlanes
        32,             # biBitCount
        0,              # biCompression = BI_RGB
        len(xor_data),  # biSizeImage
        0, 0, 0, 0,
    )
    return bmi + xor_data + and_data


def _write_win32_ico(ico_path, master_img, tight_img, sizes):
    """生成 <256px 为原生 32-bit DIB、256px 为高清 PNG 的标准 Windows 图标文件。"""
    entries = []
    payloads = []
    for w, h in sizes:
        src = tight_img if w <= 32 else master_img
        frame = src.resize((w, h), Image.Resampling.LANCZOS)
        if w >= 256:
            buf = io.BytesIO()
            frame.save(buf, format="PNG", optimize=True)
            data = buf.getvalue()
        else:
            data = _encode_ico_dib(frame)
        entries.append((w, h, len(data)))
        payloads.append(data)

    offset = 6 + 16 * len(entries)
    out = bytearray(struct.pack("<HHH", 0, 1, len(entries)))
    for (w, h, size), _data in zip(entries, payloads):
        bw = 0 if w >= 256 else w
        bh = 0 if h >= 256 else h
        out.extend(struct.pack("<BBBBHHII", bw, bh, 0, 0, 1, 32, size, offset))
        offset += size
    for data in payloads:
        out.extend(data)
    Path(ico_path).write_bytes(bytes(out))


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
