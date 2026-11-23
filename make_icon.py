# -*- coding: utf-8 -*-
# pyright: reportMissingImports=false, reportMissingModuleSource=false
"""生成无外圈蓝边、纯净 Apple Liquid Glass 立体磨砂质感图标（icon.png + icon.ico）。

设计规范：
1) 彻底去除外圈大蓝框，将中央通透磨砂冰晶玻璃圆角方牌放大为核心主体（透明底 + Apple 连续曲率超椭圆）；
2) 采用 numpy 亚像素解析 SDF 抗锯齿与多层折射光影渲染：亚表面冰青极光光晕、顶部弧面高光、
   双层棱镜晶边（左上冰白 + 右下冰青折射）、微粒磨砂质感与 3 根 3D 发光凝胶玻璃柱状图；
3) 右上角叠加悬浮冰透蓝液态玻璃钉钉翼标徽章，导出 512px icon.png 与全尺寸 Win32 DIB+PNG icon.ico。
"""

import argparse
import io
import struct
from pathlib import Path

import numpy as np  # type: ignore
from PIL import Image, ImageChops, ImageDraw, ImageFilter  # type: ignore

DEFAULT_WING = Path(__file__).resolve().parent / "dingtalk_wing.png"
ICO_SIZES = [(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)]


def _squircle_sdf_alpha(h, w, cx, cy, half_size, power=4.6, aa_px=1.6):
    """计算连续曲率超椭圆 |(x-cx)/R|^p + |(y-cy)/R|^p <= 1 的亚像素抗锯齿遮罩 (float32 0..1)。"""
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    dx = np.abs(xx + 0.5 - cx) / float(half_size)
    dy = np.abs(yy + 0.5 - cy) / float(half_size)
    d = (np.power(dx, power) + np.power(dy, power)) ** (1.0 / power)
    # 按半径近似将 1 归化为像素距离做平滑过渡
    dist_px = (1.0 - d) * float(half_size)
    return np.clip(dist_px / float(aa_px) + 0.5, 0.0, 1.0)


def _rounded_rect_alpha(h, w, x0, y0, x1, y1, r, aa_px=1.5):
    """计算圆角矩形有符号距离场亚像素平滑遮罩 (float32 0..1)。"""
    cx = (x0 + x1) * 0.5
    cy = (y0 + y1) * 0.5
    bx = (x1 - x0) * 0.5 - r
    by = (y1 - y0) * 0.5 - r
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    qx = np.abs(xx + 0.5 - cx) - bx
    qy = np.abs(yy + 0.5 - cy) - by
    out_dist = np.hypot(np.maximum(qx, 0.0), np.maximum(qy, 0.0))
    in_dist = np.minimum(np.maximum(qx, qy), 0.0)
    sdf = out_dist + in_dist - float(r)
    return np.clip(-sdf / float(aa_px) + 0.5, 0.0, 1.0)


def _gaussian_blur_np(arr, radius):
    im = Image.fromarray(np.clip(arr * 255.0, 0, 255).astype(np.uint8), mode="L")
    im = im.filter(ImageFilter.GaussianBlur(radius))
    return np.asarray(im, dtype=np.float32) / 255.0


def _alpha_over(dst_rgba, src_rgba):
    """标准 premultiplied/straight RGBA 混合（输入输出均为 float32 [H,W,4] 0..1）。"""
    sa = src_rgba[..., 3:4]
    da = dst_rgba[..., 3:4]
    out_a = sa + da * (1.0 - sa)
    safe_a = np.where(out_a > 1e-6, out_a, 1.0)
    out_rgb = (src_rgba[..., :3] * sa + dst_rgba[..., :3] * da * (1.0 - sa)) / safe_a
    return np.concatenate([np.clip(out_rgb, 0.0, 1.0), np.clip(out_a, 0.0, 1.0)], axis=-1)


def _render_frosted_glass_card(S=1024):
    """渲染纯净无蓝边的中央 Apple Liquid Glass 立体磨砂方卡 + 三根通透发光柱。"""
    cx, cy, half_r = 496.0, 532.0, 424.0
    yy, xx = np.mgrid[0:S, 0:S].astype(np.float32)
    mask = _squircle_sdf_alpha(S, S, cx, cy, half_r, power=4.6, aa_px=1.6)

    # 归一化卡内局部坐标 u, v ∈ [-1, 1]
    u = (xx - cx) / half_r
    v = (yy - cy) / half_r

    # 1) 卡背柔和冷蓝空气悬浮投影（透明外底无任何蓝边框）
    sh_mask = _squircle_sdf_alpha(S, S, cx, cy + 20.0, half_r * 0.995, power=4.6, aa_px=2.0)
    sh_blur = _gaussian_blur_np(sh_mask * 0.34, radius=26)
    canvas = np.zeros((S, S, 4), dtype=np.float32)
    canvas[..., 0] = 16.0 / 255.0
    canvas[..., 1] = 44.0 / 255.0
    canvas[..., 2] = 98.0 / 255.0
    canvas[..., 3] = sh_blur

    # 2) 磨砂玻璃主基调（左上淡薰衣草冰蓝 -> 中部晴空雾蓝 -> 右下清透晶青，让纯白立体柱跃然而出）
    diag = np.clip((u * 0.45 + v * 0.55 + 1.0) * 0.5, 0.0, 1.0)
    base_r = (212.0 * (1.0 - diag) + 148.0 * diag) / 255.0
    base_g = (224.0 * (1.0 - diag) + 206.0 * diag) / 255.0
    base_b = (252.0 * (1.0 - diag) + 250.0 * diag) / 255.0

    # 左下柔和冰紫折射韵
    g_lavender = np.exp(-(((u + 0.56) / 0.78) ** 2 + ((v - 0.46) / 0.85) ** 2))
    base_r = np.clip(base_r + g_lavender * 0.055, 0.0, 1.0)
    base_g = np.clip(base_g - g_lavender * 0.025, 0.0, 1.0)
    base_b = np.clip(base_b + g_lavender * 0.06, 0.0, 1.0)

    # 右中下方清澈天青亚表面极光光晕（衬托柱图立体感）
    g_cyan = np.exp(-(((u - 0.36) / 0.62) ** 2 + ((v - 0.30) / 0.68) ** 2))
    base_r = np.clip(base_r - g_cyan * 0.14, 0.0, 1.0)
    base_g = np.clip(base_g + g_cyan * 0.06, 0.0, 1.0)
    base_b = np.clip(base_b + g_cyan * 0.09, 0.0, 1.0)

    # 右上角偏光冷蓝折射（呼应右上角悬浮钉钉徽章）
    g_badge_glow = np.exp(-(((u - 0.62) / 0.52) ** 2 + ((v + 0.60) / 0.52) ** 2))
    base_r = np.clip(base_r - g_badge_glow * 0.14, 0.0, 1.0)
    base_g = np.clip(base_g + g_badge_glow * 0.03, 0.0, 1.0)
    base_b = np.clip(base_b + g_badge_glow * 0.07, 0.0, 1.0)

    # 极细磨砂晶粒感（固定种子确保每次渲染完全确定）
    rng = np.random.default_rng(20261120)
    grain = (rng.normal(0.0, 0.0085, (S, S))).astype(np.float32)
    base_r = np.clip(base_r + grain, 0.0, 1.0)
    base_g = np.clip(base_g + grain, 0.0, 1.0)
    base_b = np.clip(base_b + grain, 0.0, 1.0)

    # 3) 玻璃弧面高光折射带（45度左上柔白面光 + 底部微妙内亮散射）
    arc = np.exp(-(((u + 0.18) / 1.12) ** 2 + ((v + 0.86) / 0.54) ** 2)) * 0.28
    bot_bounce = np.exp(-(((u - 0.08) / 0.90) ** 2 + ((v - 0.84) / 0.32) ** 2)) * 0.12
    spec = arc + bot_bounce
    base_r = np.clip(base_r + spec, 0.0, 1.0)
    base_g = np.clip(base_g + spec, 0.0, 1.0)
    base_b = np.clip(base_b + spec, 0.0, 1.0)

    # 卡片中心稍透、边沿棱镜略实（Apple Liquid Glass 厚度感）
    radial = np.clip((np.abs(u) ** 4.2 + np.abs(v) ** 4.2) ** (1.0 / 4.2), 0.0, 1.0)
    card_alpha = mask * (0.86 + 0.115 * radial)

    card_layer = np.stack([base_r, base_g, base_b, card_alpha], axis=-1)
    canvas = _alpha_over(canvas, card_layer)

    # 4) 卡片内三根 3D 磨砂水晶柱状图（高度黄金递进，顶部完全避让右上角钉钉徽章）
    bars = [
        # (x0, y0, x1, y1, r, left_col, right_col, glow_col)
        (216.0, 562.0, 344.0, 798.0, 42.0, (1.00, 1.00, 1.00), (0.88, 0.94, 1.00), (0.65, 0.80, 0.98)),
        (404.0, 452.0, 532.0, 798.0, 42.0, (1.00, 1.00, 1.00), (0.76, 0.95, 1.00), (0.35, 0.78, 0.98)),
        (592.0, 342.0, 720.0, 798.0, 42.0, (0.98, 1.00, 1.00), (0.62, 0.95, 1.00), (0.18, 0.78, 0.98)),
    ]

    for x0, y0, x1, y1, br, lcol, rcol, gcol in bars:
        bm = _rounded_rect_alpha(S, S, x0, y0, x1, y1, br, aa_px=1.4)
        # 柱底柔和蓝色扩散投影
        sh_bm = _rounded_rect_alpha(S, S, x0 + 5, y0 + 16, x1 + 5, y1 + 20, br, aa_px=2.0)
        sh_bm = _gaussian_blur_np(sh_bm * 0.28, radius=18) * mask
        sh_layer = np.stack([
            np.full((S, S), gcol[0] * 0.18, np.float32),
            np.full((S, S), gcol[1] * 0.36, np.float32),
            np.full((S, S), gcol[2] * 0.68, np.float32),
            sh_bm,
        ], axis=-1)
        canvas = _alpha_over(canvas, sh_layer)

        bw = max(1.0, x1 - x0)
        bh = max(1.0, y1 - y0)
        bx_t = np.clip((xx - x0) / bw, 0.0, 1.0)
        by_t = np.clip((yy - y0) / bh, 0.0, 1.0)

        cyl_shine = np.exp(-((bx_t - 0.24) / 0.22) ** 2) * 0.28 + (1.0 - by_t) * 0.10
        grad_t = np.clip(bx_t * 0.65 + by_t * 0.35, 0.0, 1.0)
        cr = np.clip(lcol[0] * (1.0 - grad_t) + rcol[0] * grad_t + cyl_shine, 0.0, 1.0)
        cg = np.clip(lcol[1] * (1.0 - grad_t) + rcol[1] * grad_t + cyl_shine, 0.0, 1.0)
        cb = np.clip(lcol[2] * (1.0 - grad_t) + rcol[2] * grad_t + cyl_shine * 0.9, 0.0, 1.0)

        bm_in = _rounded_rect_alpha(S, S, x0 + 3.0, y0 + 3.0, x1 - 3.0, y1 - 3.0, max(1.0, br - 3.0), aa_px=1.4)
        bar_rim = np.clip(bm - bm_in, 0.0, 1.0) * (0.90 - 0.40 * bx_t)
        cr = np.clip(cr + bar_rim * 0.38, 0.0, 1.0)
        cg = np.clip(cg + bar_rim * 0.38, 0.0, 1.0)
        cb = np.clip(cb + bar_rim * 0.38, 0.0, 1.0)

        bar_layer = np.stack([cr, cg, cb, bm * mask * 0.96], axis=-1)
        canvas = _alpha_over(canvas, bar_layer)

    # 5) 主毛玻璃卡片外层双色液态棱镜高光边（完全替换外围蓝框，靠自身晶亮折射轮廓立住）
    outer_in = _squircle_sdf_alpha(S, S, cx, cy, half_r - 6.0, power=4.6, aa_px=1.5)
    outer_rim = np.clip(mask - outer_in, 0.0, 1.0)
    rim_top = np.clip(1.0 - (u * 0.35 + v * 0.50 + 0.5) * 0.50, 0.42, 1.0)
    rim_r = np.clip(1.0 - 0.14 * np.maximum(0.0, diag - 0.3), 0.80, 1.0)
    rim_g = np.clip(1.0 - 0.04 * np.maximum(0.0, diag - 0.3), 0.92, 1.0)
    rim_b = np.ones((S, S), dtype=np.float32)
    rim_layer = np.stack([rim_r, rim_g, rim_b, outer_rim * rim_top * 0.95], axis=-1)
    canvas = _alpha_over(canvas, rim_layer)

    edge_band = np.clip(
        mask - _squircle_sdf_alpha(S, S, cx, cy, half_r - 2.4, power=4.6, aa_px=1.2),
        0.0,
        1.0,
    )
    contour = np.stack([
        np.full((S, S), 116.0 / 255.0, np.float32),
        np.full((S, S), 174.0 / 255.0, np.float32),
        np.full((S, S), 240.0 / 255.0, np.float32),
        edge_band * (0.38 + 0.38 * diag),
    ], axis=-1)
    canvas = _alpha_over(canvas, contour)

    return Image.fromarray(np.clip(canvas * 255.0, 0, 255).astype(np.uint8), mode="RGBA")


def _build_dingtalk_liquid_badge(wing_path, size=310):
    """以超采样生成带冰透天青玻璃折射、纯白翼标与柔和投影的右上角悬浮徽章。"""
    pad = 36
    W = size + pad * 2
    cx = cy = W * 0.5
    half_r = size * 0.455
    yy, xx = np.mgrid[0:W, 0:W].astype(np.float32)

    mask = _squircle_sdf_alpha(W, W, cx, cy, half_r, power=4.5, aa_px=1.5)
    u = (xx - cx) / half_r
    v = (yy - cy) / half_r

    # 1) 柔和环境投影
    sh_mask = _squircle_sdf_alpha(W, W, cx, cy + 12.0, half_r, power=4.5, aa_px=2.0)
    sh_blur = _gaussian_blur_np(sh_mask * 0.42, radius=18)
    canvas = np.zeros((W, W, 4), dtype=np.float32)
    canvas[..., 0] = 12.0 / 255.0
    canvas[..., 1] = 68.0 / 255.0
    canvas[..., 2] = 160.0 / 255.0
    canvas[..., 3] = sh_blur

    # 2) 通透晴空冰青 -> 宝石蔚蓝液态渐变
    t = np.clip(0.50 * (v + 1.0) * 0.62 + 0.50 * (u + 1.0) * 0.38, 0.0, 1.0)
    br = (46.0 * (1.0 - t) + 10.0 * t) / 255.0
    bg = (218.0 * (1.0 - t) + 122.0 * t) / 255.0
    bb = (255.0 * (1.0 - t) + 255.0 * t) / 255.0

    # 左上顶部水润玻璃光斑
    sheen = np.exp(-(((u + 0.25) / 0.90) ** 2 + ((v + 0.75) / 0.55) ** 2)) * 0.34
    br = np.clip(br + sheen, 0.0, 1.0)
    bg = np.clip(bg + sheen, 0.0, 1.0)
    bb = np.clip(bb + sheen * 0.6, 0.0, 1.0)

    badge_body = np.stack([br, bg, bb, mask * 0.94], axis=-1)
    canvas = _alpha_over(canvas, badge_body)

    # 3) 徽章冰白玻璃内边框
    rim_in = _squircle_sdf_alpha(W, W, cx, cy, half_r - 3.6, power=4.5, aa_px=1.4)
    rim_ring = np.clip(mask - rim_in, 0.0, 1.0) * np.clip(1.0 - t * 0.45, 0.4, 1.0)
    rim_layer = np.stack([
        np.ones((W, W), np.float32),
        np.ones((W, W), np.float32),
        np.ones((W, W), np.float32),
        rim_ring * 0.85,
    ], axis=-1)
    canvas = _alpha_over(canvas, rim_layer)

    out_im = Image.fromarray(np.clip(canvas * 255.0, 0, 255).astype(np.uint8), mode="RGBA")

    # 4) 嵌入居中钉钉纯白立体翼标
    if wing_path and Path(wing_path).exists():
        with Image.open(wing_path) as raw_wing:
            wing = raw_wing.convert("RGBA")
        try:
            target_w = int(size * 0.72)
            wing_rsz = wing.resize((target_w, target_w), Image.Resampling.LANCZOS)
            try:
                wx = (W - target_w) // 2 + int(size * 0.015)
                wy = (W - target_w) // 2 + int(size * 0.015)

                w_alpha = wing_rsz.getchannel("A")
                w_shadow_alpha = Image.new("L", (W, W), 0)
                w_shadow_alpha.paste(ImageChops.multiply(w_alpha, Image.new("L", wing_rsz.size, 110)), (wx, wy + 5))
                w_shadow_alpha = w_shadow_alpha.filter(ImageFilter.GaussianBlur(5))
                w_shadow_fill = Image.new("RGBA", (W, W), (6, 48, 146, 255))
                w_shadow_fill.putalpha(w_shadow_alpha)
                out_im = Image.alpha_composite(out_im, w_shadow_fill)

                wing_layer = Image.new("RGBA", (W, W), (0, 0, 0, 0))
                wing_layer.paste(wing_rsz, (wx, wy), wing_rsz)
                out_im = Image.alpha_composite(out_im, wing_layer)
            finally:
                wing_rsz.close()
        finally:
            wing.close()

    return out_im


def render_icons(src_path="", out_dir=".", wing_path=None):
    """生成 1024 主图与 icon.png (512x512) / icon.ico（彻底无蓝边）。"""
    out_dir = Path(out_dir)
    wing_p = Path(wing_path) if wing_path else DEFAULT_WING
    S = 1024

    master = _render_frosted_glass_card(S)
    badge = _build_dingtalk_liquid_badge(wing_p, size=278)
    # 放置在毛玻璃主体右上角，微微破框而出，增强悬浮层叠感
    bx = 812 - badge.width // 2
    by = 216 - badge.height // 2
    master = Image.alpha_composite(master, Image.new("RGBA", (S, S), (0, 0, 0, 0)))
    badge_canvas = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    badge_canvas.paste(badge, (bx, by), badge)
    master = Image.alpha_composite(master, badge_canvas)

    # 针对 16/20/24/32 小尺寸，紧裁去外圈纯阴影留白并保留主体饱满度
    bbox = master.getbbox()
    if bbox:
        tight_src = master.crop((max(0, bbox[0] + 18), max(0, bbox[1] + 10), min(S, bbox[2] - 18), min(S, bbox[3] - 24)))
    else:
        tight_src = master

    png_path = out_dir / "icon.png"
    ico_path = out_dir / "icon.ico"
    master.resize((512, 512), Image.Resampling.LANCZOS).save(png_path, "PNG", optimize=True)
    _write_win32_ico(ico_path, master, tight_src, ICO_SIZES)
    print(f"ICON_SAVED_OK: {png_path} & {ico_path}")
    return master


def _encode_ico_dib(im):
    """将 RGBA Image 编码为标准 Win32 32-bit BGRA BITMAPINFOHEADER + XOR + AND 数据块。"""
    w, h = im.size
    rgba = im.convert("RGBA")
    rows_bgra = []
    for y in range(h - 1, -1, -1):
        for x in range(w):
            r, g, b, a = rgba.getpixel((x, y))
            rows_bgra.append(bytes((b, g, r, a)))
    xor_data = b"".join(rows_bgra)
    and_stride = ((w + 31) // 32) * 4
    and_data = b"\x00" * (and_stride * h)
    bmi = struct.pack(
        "<IIIHHIIIIII",
        40, w, h * 2, 1, 32, 0, len(xor_data), 0, 0, 0, 0,
    )
    return bmi + xor_data + and_data


def _write_win32_ico(ico_path, master_img, tight_img, sizes):
    """生成 <256px 为原生 32-bit DIB、256px 为高清 PNG 的标准 Windows 图标文件。"""
    entries = []
    payloads = []
    for w, h in sizes:
        src = tight_img if w <= 32 else master_img
        if src.width != src.height:
            side = max(src.width, src.height)
            sq = Image.new("RGBA", (side, side), (0, 0, 0, 0))
            sq.paste(src, ((side - src.width) // 2, (side - src.height) // 2), src)
            src = sq
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
    canvas = Image.new("RGBA", (W, H), (242, 244, 248, 255))
    draw = ImageDraw.Draw(canvas)
    draw.rectangle([W // 2, 0, W, H], fill=(24, 26, 32, 255))

    for _side, x_base in enumerate((0, W // 2)):
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
    parser.add_argument("--src", default="")
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent))
    parser.add_argument("--showcase", default="")
    args = parser.parse_args()

    master_img = render_icons(args.src, args.out)
    if args.showcase:
        make_showcase(master_img, args.showcase)
