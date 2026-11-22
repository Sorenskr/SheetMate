# -*- coding: utf-8 -*-
# pyright: reportMissingImports=false, reportMissingModuleSource=false
"""
暗号表助手 · 新拟态 UI 模块
纯 tkinter + PIL 自绘：软阴影、凸起/按压、开关、步进器、分段、输入框、
数字翻牌、文字切换动画、加载动画（旋转环/跳动点/脉冲），支持主题切换重绘。
"""
import ctypes
import math
import os
import queue
import tkinter as tk
from PIL import Image, ImageDraw, ImageFilter, ImageTk


def enable_dpi_awareness():
    """开启 Windows Per-Monitor 高分屏 DPI 感知，避免高分辨率屏幕发虚。"""
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


enable_dpi_awareness()


# ==================== 颜色 / 缓动 ====================
def hex2rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def rgb2hex(c):
    return "#%02x%02x%02x" % c


def mix(c1, c2, t):
    return tuple(int(a + (b - a) * t) for a, b in zip(c1, c2))


def ease_out_cubic(t):
    return 1 - (1 - t) ** 3


def ease_out_back(t):
    c1 = 1.70158
    c3 = c1 + 1
    return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2


# ==================== PIL 软阴影渲染 ====================
def _rrect_mask(size, radius):
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).rounded_rectangle(
        [0, 0, size[0] - 1, size[1] - 1], radius=radius, fill=255)
    return m


def _soft(box, pad, mask, color, blur, dx, dy):
    layer = Image.new("RGBA", box, (0, 0, 0, 0))
    solid = Image.new("RGBA", (mask.width, mask.height), color + (255,))
    layer.paste(solid, (pad, pad), mask)
    layer = layer.filter(ImageFilter.GaussianBlur(blur))
    out = Image.new("RGBA", box, (0, 0, 0, 0))
    out.paste(layer, (dx, dy), layer)
    return out


def render_neu(w, h, radius, base, state="raised", pad=14,
               blur=9, off=6, depth=0.28):
    W, H = w + 2 * pad, h + 2 * pad
    light = mix(base, (255, 255, 255), 0.55)
    dark = mix(base, (0, 0, 0), depth)
    mask = _rrect_mask((w, h), radius)
    bg = Image.new("RGBA", (W, H), base + (255,))
    o = off if state == "raised" else off + 2
    if state in ("raised", "hover"):
        out = Image.alpha_composite(bg, _soft((W, H), pad, mask, dark, blur, o, o))
        out = Image.alpha_composite(out, _soft((W, H), pad, mask, light, blur, -o, -o))
    else:  # pressed 内凹
        out = Image.alpha_composite(bg, _soft((W, H), pad, mask, dark, blur, -off, -off))
        out = Image.alpha_composite(out, _soft((W, H), pad, mask, light, blur, off, off))
    body = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    body.paste(Image.new("RGBA", (w, h), base + (255,)), (pad, pad), mask)
    return Image.alpha_composite(out, body)


def render_flat(w, h, radius, color, state="raised", pad=8):
    """Liquid Glass 控件：纵向透光、细描边和按压明暗变化。"""
    if state == "hover":
        color = mix(color, (255, 255, 255), 0.12)
    elif state == "pressed":
        color = mix(color, (18, 28, 45), 0.13)
    W, H = w + 2 * pad, h + 2 * pad
    mask = _rrect_mask((w, h), radius)
    out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    # 极轻的环境投影，不使用双向浮雕阴影。
    if state != "pressed":
        shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        sm = Image.new("RGBA", (w, h), (28, 45, 72, 30 if state == "hover" else 20))
        shadow.paste(sm, (pad, pad + 2), mask)
        out = Image.alpha_composite(out, shadow.filter(ImageFilter.GaussianBlur(3)))
    glass = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glass)
    for y in range(h):
        t = y / max(1, h - 1)
        row = mix(mix(color, (255, 255, 255), 0.20 * (1 - t)),
                  mix(color, (20, 35, 60), 0.07), t)
        gd.line((0, y, w, y), fill=row + (255,))
    out.paste(glass, (pad, pad), mask)
    d = ImageDraw.Draw(out)
    border = mix(color, (255, 255, 255), 0.48) + (210,)
    d.rounded_rectangle([pad, pad, pad + w - 1, pad + h - 1],
                        radius=radius, outline=border, width=1)
    d.line([pad + radius, pad + 1, pad + w - radius, pad + 1],
           fill=(255, 255, 255, 150), width=1)
    return out


def render_card(w, h, radius, base, pad=14):
    """轻量 Liquid Glass 卡片：透光渐变、顶部高光与一圈细玻璃边。"""
    W, H = w + 2 * pad, h + 2 * pad
    mask = _rrect_mask((w, h), radius)
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sm = Image.new("RGBA", (w, h), (33, 57, 91, 22))
    shadow.paste(sm, (pad, pad + 3), mask)
    out = shadow.filter(ImageFilter.GaussianBlur(7))
    body = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    bd = ImageDraw.Draw(body)
    for y in range(h):
        t = y / max(1, h - 1)
        row = mix(mix(base, (255, 255, 255), 0.28),
                  mix(base, (210, 226, 248), 0.08), t)
        bd.line((0, y, w, y), fill=row + (255,))
    out.paste(body, (pad, pad), mask)
    d = ImageDraw.Draw(out)
    d.rounded_rectangle([pad, pad, pad + w - 1, pad + h - 1],
                        radius=radius,
                        outline=mix(base, (255, 255, 255), 0.70) + (235,), width=1)
    d.line([pad + radius, pad + 1, pad + w - radius, pad + 1],
           fill=(255, 255, 255, 235), width=2)
    return out


class NeuFactory:
    def __init__(self, base_hex):
        self.base_hex = base_hex
        self.base = hex2rgb(base_hex)
        self._cache = {}

    def img(self, w, h, radius, state, pad=14, fill_hex=None):
        key = (w, h, radius, state, pad, fill_hex)
        if key not in self._cache:
            self._cache[key] = ImageTk.PhotoImage(
                render_neu(w, h, radius, hex2rgb(fill_hex) if fill_hex else self.base,
                           state, pad))
        return self._cache[key]

    def flat_img(self, w, h, radius, state, pad=8, fill_hex=None):
        key = ("flat", w, h, radius, state, pad, fill_hex)
        if key not in self._cache:
            color = hex2rgb(fill_hex) if fill_hex else self.base
            self._cache[key] = ImageTk.PhotoImage(
                render_flat(w, h, radius, color, state, pad))
        return self._cache[key]

    def card_img(self, w, h, radius, pad=14):
        key = ("card", w, h, radius, pad)
        if key not in self._cache:
            self._cache[key] = ImageTk.PhotoImage(
                render_card(w, h, radius, self.base, pad))
        return self._cache[key]


class NeuContext:
    """持有当前主题工厂，登记所有组件以便统一重绘。"""
    def __init__(self, base_hex, window_hex=None):
        self.widgets = []
        self.window_hex = window_hex or base_hex
        self.set_base(base_hex)

    def set_base(self, base_hex):
        self.base_hex = base_hex
        self.base = hex2rgb(base_hex)
        self.fac = NeuFactory(base_hex)

    def register(self, w):
        self.widgets.append(w)

    def redraw_all(self):
        for w in self.widgets:
            try:
                w.redraw()
            except Exception:
                pass


# ==================== 基础画布 ====================
class _NeuCanvas(tk.Canvas):
    def __init__(self, parent, ctx, w, h, pad, **kw):
        self.ctx, self.pad = ctx, pad
        self.ew, self.eh = w, h
        super().__init__(parent, width=w + 2 * pad, height=h + 2 * pad,
                         bg=ctx.base_hex, highlightthickness=0, bd=0, **kw)
        ctx.register(self)

    def redraw(self):
        self.configure(bg=self.ctx.base_hex)


# ==================== 卡片容器 ====================
class NeuCard(_NeuCanvas):
    def __init__(self, parent, ctx, w, h, radius=22, pad=14, outer_bg=False):
        self.outer_bg = outer_bg
        super().__init__(parent, ctx, w, h, pad)
        self.radius = radius
        self._img = self.create_image(0, 0, anchor="nw")
        self.inner = tk.Frame(self, bg=ctx.base_hex)
        self.create_window(pad, pad, anchor="nw", window=self.inner,
                           width=w, height=h)
        self.redraw()

    def redraw(self):
        super().redraw()
        if self.outer_bg:
            self.configure(bg=self.ctx.window_hex)
        self.itemconfig(self._img, image=self.ctx.fac.card_img(
            self.ew, self.eh, self.radius, self.pad))
        self.inner.configure(bg=self.ctx.base_hex)


# ==================== 内凹面板（放日志/内容区） ====================
class NeuInset(_NeuCanvas):
    def __init__(self, parent, ctx, w, h, radius=14, pad=10):
        super().__init__(parent, ctx, w, h, pad)
        self.radius = radius
        self._img = self.create_image(0, 0, anchor="nw")
        self.inner = tk.Frame(self, bg=ctx.base_hex)
        self.create_window(pad, pad, anchor="nw", window=self.inner,
                           width=w, height=h)
        self.redraw()

    def redraw(self):
        super().redraw()
        self.itemconfig(self._img, image=self.ctx.fac.flat_img(
            self.ew, self.eh, self.radius, "raised", self.pad))
        self.inner.configure(bg=self.ctx.base_hex)


# ==================== 按钮（hover 上浮 / 按压内凹） ====================
class NeuButton(_NeuCanvas):
    def __init__(self, parent, ctx, w, h, text, command=None,
                 radius=16, accent=None, font_size=15, emoji="", pad=12,
                 filled=False, outer_bg=False):
        self.outer_bg = outer_bg
        super().__init__(parent, ctx, w, h, pad, cursor="hand2")
        self.filled = filled
        self.radius, self.command, self.accent = radius, command, accent
        self.text, self.emoji, self.fs = text, emoji, font_size
        self._img = self.create_image(0, 0, anchor="nw")
        self._tid = self.create_text(self.winfo_reqwidth() / 2,
                                     self.winfo_reqheight() / 2)
        self._press = False
        self._motion_job = None
        self._motion_offset = 0.0
        self.enabled = True
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<ButtonPress-1>", self._down)
        self.bind("<ButtonRelease-1>", self._up)
        self.bind("<Destroy>", self._cancel_motion)
        self.redraw()

    def _cancel_motion(self, _event=None):
        if self._motion_job is not None:
            try:
                self.after_cancel(self._motion_job)
            except Exception:
                pass
            self._motion_job = None

    def set_enabled(self, on):
        self.enabled = bool(on)
        self.configure(cursor="hand2" if on else "arrow")
        if not on:
            self._set("raised")

    def _label(self):
        return (self.emoji + " " + self.text) if self.emoji else self.text

    def redraw(self):
        super().redraw()
        if self.outer_bg:
            self.configure(bg=self.ctx.window_hex)
        f = self.ctx.fac
        self._states = {s: f.flat_img(self.ew, self.eh, self.radius, s, self.pad,
                                      fill_hex=self.accent if self.filled else None)
                        for s in ("raised", "hover", "pressed")}
        self.itemconfig(self._img, image=self._states["raised"])
        col = "#ffffff" if self.filled else (self.accent if self.accent else "#3a3a3f")
        self.itemconfig(self._tid, text=self._label(), fill=col,
                        font=("Microsoft YaHei UI", self.fs, "bold"))
        self.coords(self._tid, self.winfo_reqwidth() / 2,
                    self.winfo_reqheight() / 2)

    def _set(self, state, dy=0):
        self.itemconfig(self._img, image=self._states[state])
        self.coords(self._tid, self.winfo_reqwidth() / 2,
                    self.winfo_reqheight() / 2 + dy)

    def _animate_visual(self, state, target):
        if self._motion_job:
            try:
                self.after_cancel(self._motion_job)
            except Exception:
                pass
        start = self._motion_offset
        distance = target - start

        def frame(i=1):
            if not self.winfo_exists():
                return
            t = ease_out_cubic(i / 5)
            self._motion_offset = start + distance * t
            self._set(state, self._motion_offset)
            size = self.fs - (1 if state == "pressed" else 0)
            self.itemconfig(self._tid,
                            font=("Microsoft YaHei UI", size, "bold"))
            if i < 5:
                self._motion_job = self.after(16, lambda: frame(i + 1))
            else:
                self._motion_job = None
        frame()

    def _enter(self, _):
        if self.enabled and not self._press:
            self._animate_visual("hover", -1.2)

    def _leave(self, _):
        if self.enabled:
            self._press = False
            self._animate_visual("raised", 0)

    def _down(self, _):
        if not self.enabled:
            return "break"
        self._press = True
        self._animate_visual("pressed", 2.2)
        return "break"

    def _up(self, event):
        if not self.enabled or not self._press:
            return "break"
        self._press = False
        inside = 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height()
        self._animate_visual("hover" if inside else "raised", -1.2 if inside else 0)
        if inside and self.command:
            self.after(70, self.command)
        return "break"


# ==================== 开关 ====================
class NeuSwitch(_NeuCanvas):
    def __init__(self, parent, ctx, accent_hex="#34c759", default=False,
                 command=None, w=66, h=34, pad=10):
        super().__init__(parent, ctx, w, h, pad)
        self.accent, self.command = accent_hex, command
        self.on, self.w, self.h = bool(default), w, h
        self._track = self.create_image(0, 0, anchor="nw")
        self._knob = self.create_oval(0, 0, 1, 1, fill="#ffffff", outline="")
        self._x = self._knob_target()
        self.bind("<Button-1>", self.toggle)
        self.redraw()

    def _knob_target(self):
        k = self.h - 8
        return (4 if not self.on else self.w - 4 - k) + self.pad

    def redraw(self):
        super().redraw()
        self._draw()

    def _draw(self, x=None):
        state = "hover" if self.on else "raised"
        self.itemconfig(self._track, image=self.ctx.fac.flat_img(
            self.w, self.h, self.h // 2, state, self.pad,
            fill_hex=self.accent if self.on else "#c8d0dc"))
        k = self.h - 8
        kx = self._x if x is None else x
        self.itemconfig(self._knob, fill="#ffffff", outline="#e8edf4")
        self.coords(self._knob, kx, self.pad + 4, kx + k, self.pad + 4 + k)

    def toggle(self, _=None):
        self.on = not self.on
        target = self._knob_target()
        self._animate(target)
        if self.command:
            self.command(self.on)

    def set(self, v):
        self.on = bool(v)
        self._x = self._knob_target()
        self._draw()

    def get(self):
        return self.on

    def _animate(self, target):
        step = 2 if target > self._x else -2

        def frame():
            self._x += step
            if (step > 0 and self._x >= target) or (step < 0 and self._x <= target):
                self._x = target
                self._draw()
                return
            self._draw(self._x)
            self.after(10, frame)
        frame()


# ==================== 数字步进器 ====================
class NeuStepper(_NeuCanvas):
    def __init__(self, parent, ctx, low, high, default, command=None,
                 w_box=40, h=40, pad=4):
        # 两个扁平加减按钮 + 清晰数字，不再使用空白浮雕圆钮。
        self.btn = h
        self.mid = 42
        w = 2 * self.btn + self.mid
        super().__init__(parent, ctx, w, h, pad)
        self.low, self.high, self.command = low, high, command
        self.value = default
        self._minus_img = self.create_image(0, 0, anchor="nw")
        self._plus_img = self.create_image(0, 0, anchor="nw")
        self._val = self.create_text(0, 0, text="%02d" % default)
        self._val2 = self.create_text(0, 0, state="hidden")
        self._minus_txt = self.create_text(0, 0, text="−",
                                           font=("Microsoft YaHei UI", 17, "bold"))
        self._plus_txt = self.create_text(0, 0, text="+",
                                          font=("Microsoft YaHei UI", 16, "bold"))
        self._mstate = self._pstate = "raised"
        self._anim = None
        self.bind("<Button-1>", self._click)
        # 时间只通过明确的加减按钮调整；滚轮始终用于滚动主页面，避免误改计划。
        self.redraw()

    def _ink(self):
        # 按背景亮度选深/浅数字色，保证三主题下都清晰
        r, g, b = self.ctx.base
        lum = 0.299 * r + 0.587 * g + 0.114 * b
        return (58, 58, 63) if lum > 140 else (232, 234, 237)

    def _positions(self):
        iw = self.winfo_reqwidth()
        ih = self.winfo_reqheight()
        cy = ih / 2
        mx1 = self.pad
        mx2 = mx1 + self.btn
        cx_minus = (mx1 + mx2) / 2
        vx = mx2 + self.mid / 2
        px1 = mx2 + self.mid
        px2 = px1 + self.btn
        cx_plus = (px1 + px2) / 2
        return cy, cx_minus, vx, cx_plus

    def redraw(self):
        super().redraw()
        if self._anim is not None:
            try:
                self.after_cancel(self._anim)
            except Exception:
                pass
            self._anim = None
        self.itemconfig(self._val2, state="hidden")
        self._draw()

    def _draw(self):
        f = self.ctx.fac
        cy, cm, vx, cp = self._positions()
        lum = 0.299 * self.ctx.base[0] + 0.587 * self.ctx.base[1] + 0.114 * self.ctx.base[2]
        btn_color = mix(self.ctx.base, (0, 0, 0) if lum > 140 else (255, 255, 255), 0.055)
        fill_hex = rgb2hex(btn_color)
        ink = rgb2hex(self._ink())
        self.itemconfig(self._minus_img, image=f.flat_img(
            self.btn, self.btn, 10, self._mstate, self.pad, fill_hex=fill_hex))
        self.coords(self._minus_img, 0, 0)
        self.itemconfig(self._plus_img, image=f.flat_img(
            self.btn, self.btn, 10, self._pstate, self.pad, fill_hex=fill_hex))
        self.coords(self._plus_img, self.btn + self.mid, 0)
        self.coords(self._minus_txt, cm, cy)
        self.coords(self._plus_txt, cp, cy)
        self.itemconfig(self._minus_txt, fill=ink)
        self.itemconfig(self._plus_txt, fill=ink)
        self.coords(self._val, vx, cy)
        self.itemconfig(self._val, text="%02d" % self.value, fill=ink,
                        font=("Microsoft YaHei UI", 13, "bold"))

    def _click(self, e):
        _, cm, _, cp = self._positions()
        x = e.x
        if abs(x - cm) < self.btn / 2 + 4:
            self._bump("minus")
            self.dec()
        elif abs(x - cp) < self.btn / 2 + 4:
            self._bump("plus")
            self.inc()

    def _bump(self, which):
        # 简单按压反馈：立即重绘为 pressed 再回 raised
        if which == "minus":
            self._mstate = "pressed"
        else:
            self._pstate = "pressed"
        self._draw()

        def back():
            self._mstate = self._pstate = "raised"
            self._draw()
        self.after(90, back)

    def _wheel(self, e):
        if e.delta > 0:
            self.inc()
        else:
            self.dec()
        return "break"

    def _flip(self, old, new, direction):
        # 旧数字滑出淡出、新数字滑入淡入（纵向翻牌）
        if self._anim is not None:
            try:
                self.after_cancel(self._anim)
            except Exception:
                pass
            self._anim = None
        _, _, vx, _ = self._positions()
        cy = self.winfo_reqheight() / 2
        base, ink = self.ctx.base, self._ink()
        shift = 14 * direction
        self.itemconfig(self._val, text="%02d" % old, state="normal",
                        fill=rgb2hex(ink))
        self.coords(self._val, vx, cy)
        self.itemconfig(self._val2, text="%02d" % new, state="normal",
                        fill=rgb2hex(base))
        self.coords(self._val2, vx, cy + shift)
        frames, step = 14, 0

        def tick():
            nonlocal step
            step += 1
            t = ease_out_cubic(step / frames)
            self.coords(self._val, vx, cy - shift * t)
            self.itemconfig(self._val, fill=rgb2hex(mix(ink, base, t)))
            self.coords(self._val2, vx, cy + shift * (1 - t))
            self.itemconfig(self._val2, fill=rgb2hex(mix(base, ink, t)))
            if step >= frames:
                self.itemconfig(self._val, text="%02d" % new,
                                fill=rgb2hex(ink))
                self.coords(self._val, vx, cy)
                self.itemconfig(self._val2, state="hidden")
                self._anim = None
                if self.command:
                    self.command()
            else:
                self._anim = self.after(12, tick)
        self._anim = self.after(12, tick)

    def inc(self):
        if self.value < self.high:
            self.value += 1
            self._draw()
            if self.command:
                self.command()

    def dec(self):
        if self.value > self.low:
            self.value -= 1
            self._draw()
            if self.command:
                self.command()

    def set(self, v):
        nv = max(self.low, min(self.high, int(v)))
        if nv == self.value:
            self._draw()
            return
        self.value = nv
        self._draw()
        if self.command:
            self.command()

    def get(self):
        return self.value


# ==================== 分段控件 ====================
class NeuSegment(_NeuCanvas):
    def __init__(self, parent, ctx, options, default, command,
                 h=44, pad=10, btn_w=None):
        n = len(options)
        self.btn_w = btn_w or (120 if n == 2 else 90)
        w = n * self.btn_w
        super().__init__(parent, ctx, w, h, pad)
        self.options, self.command, self.h = options, command, h
        self.value = default
        self._bg = self.create_image(0, 0, anchor="nw")
        self._sel = self.create_image(0, 0, anchor="nw")
        self._texts = []
        for text, val in options:
            t = self.create_text(0, 0, text=text,
                                 font=("Microsoft YaHei UI", 14))
            self._texts.append((val, t))
        self.bind("<Button-1>", self._click)
        self.redraw()

    def redraw(self):
        super().redraw()
        self._draw()

    def _draw(self):
        f = self.ctx.fac
        iw = self.winfo_reqwidth()
        ih = self.winfo_reqheight()
        bg_hex = rgb2hex(mix(self.ctx.base, (0, 0, 0), 0.045))
        self.itemconfig(self._bg, image=f.flat_img(
            self.ew, self.eh, 12, "raised", self.pad, fill_hex=bg_hex))
        self.coords(self._bg, 0, 0)
        idx = next((i for i, (_, v) in enumerate(self.options)
                    if v == self.value), 0)
        sw = self.btn_w
        sel_x = self.pad + idx * sw + 4
        sel_hex = rgb2hex(mix(self.ctx.base, hex2rgb("#007aff"), 0.12))
        sel_img = f.flat_img(sw - 8, self.eh - 10, 10, "raised", self.pad,
                             fill_hex=sel_hex)
        self.itemconfig(self._sel, image=sel_img)
        self.coords(self._sel, sel_x, self.pad + 5)
        for i, (val, t) in enumerate(self._texts):
            cx = self.pad + i * sw + sw / 2
            self.coords(t, cx, ih / 2)
            if val == self.value:
                self.itemconfig(t, fill="#007aff",
                                font=("Microsoft YaHei UI", 14, "bold"))
            else:
                self.itemconfig(t, fill="#6b6b70",
                                font=("Microsoft YaHei UI", 14))

    def _click(self, e):
        x = e.x - self.pad
        idx = int(x // self.btn_w)
        if 0 <= idx < len(self.options):
            self.select(self.options[idx][1])

    def select(self, val):
        self.value = val
        self._draw()
        if self.command:
            self.command(val)


# ==================== 内凹输入框 ====================
class NeuField(_NeuCanvas):
    def __init__(self, parent, ctx, w, h=46, text="", textvariable=None, pad=12):
        super().__init__(parent, ctx, w, h, pad)
        self._img = self.create_image(0, 0, anchor="nw")
        self.var = textvariable or tk.StringVar(value=text)
        self.entry = tk.Entry(self, textvariable=self.var, bd=0,
                              font=("Microsoft YaHei UI", 14),
                              relief=tk.FLAT, highlightthickness=0)
        self.create_window(pad + 6, pad, anchor="nw", window=self.entry,
                           width=w - 12, height=h)
        self.redraw()

    def redraw(self):
        super().redraw()
        self.itemconfig(self._img, image=self.ctx.fac.flat_img(
            self.ew, self.eh, 14, "raised", self.pad))
        self.entry.configure(bg=self.ctx.base_hex, fg="#3a3a3f",
                             insertbackground="#007aff")


# ==================== 数字翻牌 ====================
class AnimatedNumber(_NeuCanvas):
    def __init__(self, parent, ctx, value=0, color="#1c1c1e",
                 font_size=26, w=90, h=46, pad=0):
        super().__init__(parent, ctx, w, h, pad)
        self.value, self.color, self.fs = value, color, font_size
        self.cx, self.cy = (w + 2 * pad) / 2, (h + 2 * pad) / 2
        self._t = self.create_text(self.cx, self.cy, text=str(value),
                                   fill=color,
                                   font=("Microsoft YaHei UI", font_size, "bold"))

    def redraw(self):
        super().redraw()
        self.itemconfig(self._t, fill=self.color)

    def set(self, new):
        if new == self.value:
            return
        self.value = new
        base = self.ctx.base
        other = self.create_text(self.cx, self.cy + 18, text=str(new),
                                 fill=rgb2hex(mix(hex2rgb(self.color), base, 0.8)),
                                 font=("Microsoft YaHei UI", self.fs, "bold"))
        n = 16

        def frame(i=0):
            e = ease_out_cubic(i / n)
            self.coords(self._t, self.cx, self.cy - 18 * e)
            self.itemconfig(self._t, fill=rgb2hex(mix(hex2rgb(self.color), base, e)))
            self.coords(other, self.cx, self.cy + 18 * (1 - e))
            self.itemconfig(other, fill=rgb2hex(mix(base, hex2rgb(self.color), e)))
            if i < n:
                self.after(12, lambda: frame(i + 1))
            else:
                self.delete(self._t)
                self._t = other
        frame()


# ==================== 文字切换动画 ====================
class AnimatedText(_NeuCanvas):
    def __init__(self, parent, ctx, text="", color="#1c1c1e", font_size=15,
                 bold=True, w=300, h=28, pad=0, anchor="center"):
        super().__init__(parent, ctx, w, h, pad)
        self.color, self.fs, self.bold, self.anchor = color, font_size, bold, anchor
        self.cx = (w + 2 * pad) / 2
        self.cy = (h + 2 * pad) / 2
        x = self.cx if anchor == "center" else self.pad
        self._t = self.create_text(x, self.cy, text=text, anchor=anchor,
                                   fill=color,
                                   font=("Microsoft YaHei UI", font_size,
                                         "bold" if bold else "normal"))

    def redraw(self):
        super().redraw()
        self.itemconfig(self._t, fill=self.color)

    def set(self, text, color=None):
        if color:
            self.color = color
        # 状态更新必须只保留一行：旧动画叠加会造成文字互相覆盖。
        self.delete("all")
        x = self.cx if self.anchor == "center" else self.pad
        self._t = self.create_text(
            x, self.cy, anchor=self.anchor, text=text, fill=self.color,
            font=("Microsoft YaHei UI", self.fs,
                  "bold" if self.bold else "normal"))


# ==================== 加载动画 ====================
class _Spinner(tk.Canvas):
    def __init__(self, parent, ctx, size, color):
        self.ctx, self.size, self.color = ctx, size, color
        super().__init__(parent, width=size, height=size, bg=ctx.base_hex,
                         highlightthickness=0, bd=0)
        ctx.register(self)
        self._after_id = None
        self.bind("<Destroy>", self._cancel_tick)

    def _cancel_tick(self, _event=None):
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _schedule_tick(self, delay):
        self._after_id = self.after(delay, self._tick)

    def redraw(self):
        self.configure(bg=self.ctx.base_hex)


class RingSpinner(_Spinner):
    def __init__(self, parent, ctx, size=46, color="#007aff"):
        super().__init__(parent, ctx, size, color)
        self.ang = 0
        self._tick()

    def _tick(self):
        self.delete("all")
        pad = 5
        bg = hex2rgb(self.ctx.base_hex)
        for k, alpha in enumerate((0.15, 0.45, 1.0)):
            col = rgb2hex(mix(hex2rgb(self.color), bg, 1 - alpha))
            self.create_arc(pad, pad, self.size - pad, self.size - pad,
                            start=self.ang + k * 40, extent=70,
                            style=tk.ARC, outline=col, width=4)
        self.ang = (self.ang + 12) % 360
        self._schedule_tick(28)


class DotsSpinner(_Spinner):
    def __init__(self, parent, ctx, size=46, color="#ff9500"):
        super().__init__(parent, ctx, size, color)
        self.phase = 0.0
        self._tick()

    def _tick(self):
        self.delete("all")
        base = hex2rgb(self.ctx.base_hex)
        gap = self.size / 4
        for i in range(3):
            p = (math.sin(self.phase - i * 0.9) + 1) / 2
            r = 3 + 4 * p
            cy = self.size / 2 - 6 * p
            col = rgb2hex(mix(base, hex2rgb(self.color), 0.35 + 0.65 * p))
            x = gap * (i + 1)
            self.create_oval(x - r, cy - r, x + r, cy + r, fill=col, outline="")
        self.phase += 0.28
        self._schedule_tick(30)


class PulseSpinner(_Spinner):
    def __init__(self, parent, ctx, size=46, color="#34c759"):
        super().__init__(parent, ctx, size, color)
        self.phase = 0.0
        self._tick()

    def _tick(self):
        self.delete("all")
        base = hex2rgb(self.ctx.base_hex)
        for i in range(2):
            t = (self.phase + i * 0.5) % 1.0
            r = 6 + (self.size / 2 - 8) * ease_out_cubic(t)
            alpha = (1 - t) * 0.9
            col = rgb2hex(mix(hex2rgb(self.color), base, 1 - alpha))
            c = self.size / 2
            self.create_oval(c - r, c - r, c + r, c + r, outline=col, width=3)
        self.phase = (self.phase + 0.035) % 1.0
        self._schedule_tick(30)


# ==================== 入场动画（上滑） ====================
def slide_in(widget, parent, x, y, distance=26, duration=260, **place_kw):
    widget.place(x=x, y=y + distance, **place_kw)
    steps = max(1, int(duration / 16))
    id_ = getattr(parent, "_slide_id", [0])

    def frame(i=0):
        e = ease_out_cubic(i / steps)
        widget.place(x=x, y=y + distance * (1 - e), **place_kw)
        if i < steps:
            widget.after(16, lambda: frame(i + 1))
    widget.after(60 + id_[0] * 70, frame)
    id_[0] += 1
    parent._slide_id = id_


# ==================== 桌面灵动岛毛玻璃进度浮窗 ====================
class GlassProgress:
    """置顶无边框 Dynamic Island 灵动岛风格浮窗：显示进度、暂停/继续/取消按键与异常排障恢复指引。"""
    # 使用极暗近似黑而非纯 black(#000000)，避免深色文字/图标被 Windows 透明色键误穿透
    KEY = "#010203"

    def __init__(self, master, title="暗号表助手", on_pause=None, on_resume=None, on_cancel=None):
        self.master = master
        self.title_text = title
        self._on_pause = on_pause
        self._on_resume = on_resume
        self._on_cancel = on_cancel
        self.win = None
        self.closed = False
        self.glass_ok = False
        self._cur = 0.0
        self._target = 0.0
        self._bar_id = None
        self._bar_color = "#0a84ff"
        self._close_timer = None
        self._drag_offset = None
        self._rt_state = {
            "paused": False,
            "waiting_recovery": False,
            "hands_off": False,
            "hands_off_text": "",
            "prompt": {
                "mode": "running",
                "title": "",
                "detail": "",
                "hint": "按键提示：临时用电脑可点【暂停】，忙完点【继续】；点【取消】终止本轮",
                "actions": [],
                "hands_off": False,
                "hands_off_text": "",
            },
        }
        self._action_widgets = []
        self.OPACITY = 0.96
        self.W = 508
        self.H_COMPACT = 154
        self.H_PAUSED = 224
        self.H_EXPANDED = 286
        self.H = self.H_COMPACT
        sw = master.winfo_screenwidth()
        self.x, self.y = max(12, (sw - self.W) // 2), 18
        self._ui_queue = queue.SimpleQueue()
        try:
            self.master.after(35, self._pump_ui)
        except Exception:
            pass

    def set_callbacks(self, on_pause=None, on_resume=None, on_cancel=None):
        if on_pause is not None:
            self._on_pause = on_pause
        if on_resume is not None:
            self._on_resume = on_resume
        if on_cancel is not None:
            self._on_cancel = on_cancel

    def _ensure_win(self):
        # 延迟到第一次 show 时才真正创建窗口，避免初始窗口闪现/未隐藏
        if self.win is not None:
            return
        self.win = tk.Toplevel(self.master)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.geometry(f"{self.W}x{self.H}+{self.x}+{self.y}")
        self.win.configure(bg=self.KEY)
        self.win.update_idletasks()
        self._apply_glass()
        self._build()
        self.win.withdraw()

    def _apply_glass(self):
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        top = user32.GetAncestor(self.win.winfo_id(), 2)
        self.win.attributes("-transparentcolor", self.KEY)

        class ACCENT(ctypes.Structure):
            _fields_ = [("s", ctypes.c_int), ("f", ctypes.c_int),
                        ("color", ctypes.c_int), ("a", ctypes.c_int)]

        class WDATA(ctypes.Structure):
            _fields_ = [("attr", ctypes.c_int),
                        ("p", ctypes.POINTER(ctypes.c_int)),
                        ("sz", ctypes.c_ulong)]

        try:
            # WS_EX_NOACTIVATE(0x08000000)：允许直接左键点击浮窗「暂停/取消」，同时绝不抢夺钉钉输入焦点
            GWL_EXSTYLE = -20
            WS_EX_NOACTIVATE = 0x08000000
            WS_EX_TOOLWINDOW = 0x00000080
            ex_st = user32.GetWindowLongW(top, GWL_EXSTYLE)
            user32.SetWindowLongW(top, GWL_EXSTYLE, ex_st | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
        except Exception:
            pass
        try:
            swca = user32.SetWindowCompositionAttribute
            swca.restype = ctypes.c_int
            swca.argtypes = [wintypes.HWND, ctypes.POINTER(WDATA)]
            r, g, b, al = 18, 20, 28, 218
            grad = (al << 24) | (b << 16) | (g << 8) | r
            acc = ACCENT(4, 2, grad, 0)
            wd = WDATA(19, ctypes.cast(ctypes.pointer(acc),
                                       ctypes.POINTER(ctypes.c_int)),
                       ctypes.sizeof(acc))
            swca(top, ctypes.byref(wd))
            corner = ctypes.c_int(2)   # DWMWCP_ROUND
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                top, 33, ctypes.byref(corner), ctypes.sizeof(corner))
            self.glass_ok = True
        except Exception:
            self.glass_ok = False
            self.win.configure(bg="#151822")

    def _bind_drag(self, widget):
        def drag_start(e):
            self._drag_offset = (e.x_root - self.win.winfo_x(),
                                 e.y_root - self.win.winfo_y())

        def drag_move(e):
            if self._drag_offset is None:
                return
            self.x = e.x_root - self._drag_offset[0]
            self.y = e.y_root - self._drag_offset[1]
            self.win.geometry(f"+{self.x}+{self.y}")

        def drag_end(_e):
            self._drag_offset = None

        widget.bind("<Button-1>", drag_start)
        widget.bind("<B1-Motion>", drag_move)
        widget.bind("<ButtonRelease-1>", drag_end)

    def _build(self):
        w = self.win
        self.dot_lab = tk.Label(w, text="●", bg=self.KEY, fg="#0a84ff",
                                font=("Microsoft YaHei UI", 10))
        self.dot_lab.place(x=20, y=13)
        self.title_lab = tk.Label(w, text=f"{self.title_text} · 运行中", bg=self.KEY, fg="#a5b1c2",
                                  font=("Microsoft YaHei UI", 10, "bold"), anchor="w")
        self.title_lab.place(x=38, y=13, width=self.W - 125)
        self.pct_lab = tk.Label(w, text="0%", bg=self.KEY, fg="#f3f4f6",
                                font=("Microsoft YaHei UI", 11, "bold"), anchor="e")
        self.pct_lab.place(x=self.W - 78, y=12, width=56)

        self.step_lab = tk.Label(w, text="准备开始…", bg=self.KEY, fg="#ffffff",
                                 font=("Microsoft YaHei UI", 11, "bold"), anchor="w")
        self.step_lab.place(x=20, y=38, width=self.W - 40)

        self.cv = tk.Canvas(w, bg=self.KEY, highlightthickness=0, bd=0)
        self.cv.place(x=20, y=67, width=self.W - 40, height=8)
        self.bw = self.W - 40

        self.issue_title_lab = tk.Label(
            w, text="", bg=self.KEY, fg="#ffb340",
            font=("Microsoft YaHei UI", 10, "bold"), anchor="w",
        )
        self.hands_off_lab = tk.Label(
            w, text="", bg="#4a2c00", fg="#ffd60a",
            font=("Microsoft YaHei UI", 9, "bold"), anchor="w", justify="left",
            padx=8, pady=4, wraplength=self.W - 56,
        )
        self.detail_lab = tk.Label(
            w, text="", bg=self.KEY, fg="#dce4f0",
            font=("Microsoft YaHei UI", 9), anchor="nw", justify="left",
            wraplength=self.W - 44,
        )
        self.hint_lab = tk.Label(
            w, text="按键提示：随时按【F8】或点【暂停】让出电脑，忙完按【F8】/【继续】；【取消】终止",
            bg=self.KEY, fg="#8fb3e8",
            font=("Microsoft YaHei UI", 9), anchor="w", justify="left",
            wraplength=self.W - 44,
        )
        self.btn_frame = tk.Frame(w, bg=self.KEY)

        # 仅在非按钮展示区绑定拖动，绝不干扰交互按钮点击
        for widget in (w, self.dot_lab, self.title_lab, self.pct_lab, self.step_lab,
                       self.issue_title_lab, self.hands_off_lab, self.detail_lab, self.hint_lab):
            self._bind_drag(widget)
            if widget is not w:
                widget.configure(cursor="fleur")

        self._layout_mode("running")

    def _make_pill_btn(self, parent, label, bg_norm, bg_hover, fg_col, cmd):
        btn = tk.Label(
            parent, text=label, bg=bg_norm, fg=fg_col,
            font=("Microsoft YaHei UI", 9, "bold"),
            padx=12, pady=5, cursor="hand2",
        )
        btn.bind("<Enter>", lambda _e: btn.configure(bg=bg_hover))
        btn.bind("<Leave>", lambda _e: btn.configure(bg=bg_norm))
        btn.bind("<Button-1>", lambda _e: cmd())
        return btn

    def _rebuild_buttons(self, mode, actions):
        for child in self.btn_frame.winfo_children():
            child.destroy()
        self._action_widgets.clear()

        specs = []
        if mode in ("waiting_fix", "login_wps"):
            for act in (actions or []):
                act_id = act.get("id", "retry")
                label = act.get("label", "继续")
                style = act.get("style", "primary")
                if style == "primary":
                    b_n, b_h, f_c = "#0a84ff", "#2596ff", "#ffffff"
                else:
                    b_n, b_h, f_c = "#323a4c", "#404a60", "#eef3fa"
                specs.append((label, b_n, b_h, f_c, lambda aid=act_id: self._trigger_resume(aid)))
            specs.append(("取消本轮", "#d9382e", "#f0493e", "#ffffff", self._trigger_cancel))
        elif mode == "paused":
            specs.append(("继续运行 (F8)", "#28b84d", "#34cf5c", "#ffffff", lambda: self._trigger_resume("continue")))
            specs.append(("取消本轮", "#d9382e", "#f0493e", "#ffffff", self._trigger_cancel))
        else:
            specs.append(("暂停 (F8)", "#e08e0b", "#f5a21d", "#ffffff", self._trigger_pause))
            specs.append(("取消", "#d9382e", "#f0493e", "#ffffff", self._trigger_cancel))

        avail_w = self.W - 40
        cur_x = 0
        cur_y = 0
        row_h = 0
        gap_x = 8
        gap_y = 6
        for label, b_n, b_h, f_c, cmd in specs:
            b = self._make_pill_btn(self.btn_frame, label, b_n, b_h, f_c, cmd)
            bw = max(56, b.winfo_reqwidth())
            bh = max(28, b.winfo_reqheight())
            if cur_x > 0 and cur_x + bw > avail_w:
                cur_x = 0
                cur_y += row_h + gap_y
                row_h = 0
            b.place(x=cur_x, y=cur_y, width=bw, height=bh)
            cur_x += bw + gap_x
            row_h = max(row_h, bh)
            self._action_widgets.append(b)
        return max(34, cur_y + row_h + 2)

    def _trigger_pause(self):
        if self._on_pause:
            try:
                self._on_pause()
            except Exception:
                pass

    def _trigger_resume(self, action="continue"):
        if self._on_resume:
            try:
                self._on_resume(action)
            except Exception:
                pass

    def _trigger_cancel(self):
        if self._on_cancel:
            try:
                self._on_cancel()
            except Exception:
                pass

    def _layout_mode(self, mode, prompt=None):
        if self.win is None or not hasattr(self, "hint_lab"):
            return
        prompt = prompt or {}
        btn_h = self._rebuild_buttons(mode, prompt.get("actions") or [])
        if mode in ("waiting_fix", "login_wps"):
            self.hands_off_lab.place_forget()
            title_s = self._clean_text(prompt.get("title") or "需要确认后继续")
            detail_s = self._clean_text(prompt.get("detail") or "")
            hint_s = self._clean_text(prompt.get("hint") or "处理完成后点下方恢复按钮继续，或点【取消】终止")
            self._bar_color = "#0a84ff" if mode == "login_wps" else "#ff9f0a"
            self.title_lab.config(
                text=f"{self.title_text} · {'等待扫码' if mode == 'login_wps' else '等待排障'}",
                fg="#ffb340" if mode == "waiting_fix" else "#5eb0ff",
            )
            self.issue_title_lab.config(text=title_s, fg="#ffb340" if mode == "waiting_fix" else "#5eb0ff")
            self.detail_lab.config(text=detail_s)
            self.hint_lab.config(text=hint_s, fg="#8cc4ff")

            self.issue_title_lab.place(x=20, y=82, width=self.W - 40)
            self.detail_lab.place(x=20, y=106, width=self.W - 40)
            self.win.update_idletasks()
            req_detail_h = max(44, self.detail_lab.winfo_reqheight() + 6)
            hint_y = 108 + req_detail_h + 4
            self.hint_lab.place(x=20, y=hint_y, width=self.W - 40)
            self.win.update_idletasks()
            req_hint_h = max(20, self.hint_lab.winfo_reqheight() + 4)
            btn_y = hint_y + req_hint_h + 6
            self.btn_frame.place(x=20, y=btn_y, width=self.W - 40, height=btn_h)
            target_h = btn_y + btn_h + 14
        elif mode == "paused":
            self.hands_off_lab.place_forget()
            self._bar_color = "#ff9f0a"
            self.title_lab.config(text=f"{self.title_text} · 已暂停", fg="#ffb340")
            self.issue_title_lab.place_forget()
            detail_s = self._clean_text(
                prompt.get("detail") or "自动键鼠操作已挂起，处理完其他事务后按【F8】或点【继续运行 (F8)】接着跑。"
            )
            hint_s = self._clean_text(
                prompt.get("hint") or "按键提示：按【F8】或点【继续运行 (F8)】恢复未完成步骤，点【取消本轮】终止。"
            )
            self.detail_lab.config(text=detail_s)
            self.hint_lab.config(text=hint_s, fg="#ffd080")
            self.detail_lab.place(x=20, y=84, width=self.W - 40)
            self.win.update_idletasks()
            req_detail_h = max(36, self.detail_lab.winfo_reqheight() + 6)
            hint_y = 86 + req_detail_h + 4
            self.hint_lab.place(x=20, y=hint_y, width=self.W - 40)
            self.win.update_idletasks()
            req_hint_h = max(20, self.hint_lab.winfo_reqheight() + 4)
            btn_y = hint_y + req_hint_h + 6
            self.btn_frame.place(x=20, y=btn_y, width=self.W - 40, height=btn_h)
            target_h = btn_y + btn_h + 14
        elif mode == "stopped":
            self.hands_off_lab.place_forget()
            self._bar_color = "#ff3b30"
            self.title_lab.config(text=f"{self.title_text} · 已取消", fg="#ff6961")
            self.issue_title_lab.place_forget()
            self.detail_lab.place_forget()
            self.hint_lab.config(text="任务已安全中止", fg="#ff9b95")
            self.hint_lab.place(x=20, y=82, width=self.W - 40)
            self.btn_frame.place_forget()
            target_h = 122
        else:
            hands_off = bool(prompt.get("hands_off") or self._rt_state.get("hands_off"))
            ho_txt = self._clean_text(prompt.get("hands_off_text") or self._rt_state.get("hands_off_text") or "")
            self.issue_title_lab.place_forget()
            self.detail_lab.place_forget()
            if hands_off and ho_txt:
                self._bar_color = "#ff9f0a"
                self.title_lab.config(text=f"{self.title_text} · 勿动键鼠", fg="#ffd60a")
                self.hands_off_lab.config(text=ho_txt)
                self.hands_off_lab.place(x=20, y=82, width=self.W - 40)
                self.win.update_idletasks()
                ho_h = max(26, self.hands_off_lab.winfo_reqheight() + 4)
                hint_s = self._clean_text(
                    prompt.get("hint") or "重要提示：此刻正在自动键鼠粘贴，切勿动鼠标键盘！（急停按 F8）"
                )
                self.hint_lab.config(text=hint_s, fg="#ffd080")
                hint_y = 82 + ho_h + 5
                self.hint_lab.place(x=20, y=hint_y, width=self.W - 40)
                self.win.update_idletasks()
                req_hint_h = max(20, self.hint_lab.winfo_reqheight() + 2)
                btn_y = hint_y + req_hint_h + 6
                self.btn_frame.place(x=20, y=btn_y, width=self.W - 40, height=btn_h)
                target_h = max(self.H_PAUSED - 10, btn_y + btn_h + 14)
            else:
                self.hands_off_lab.place_forget()
                if self._bar_color == "#ff9f0a":
                    self._bar_color = "#0a84ff"
                self.title_lab.config(text=f"{self.title_text} · 运行中", fg="#a5b1c2")
                hint_s = self._clean_text(
                    prompt.get("hint") or "按键提示：随时按【F8】或点【暂停】让出电脑，忙完按【F8】/【继续】；【取消】终止"
                )
                self.hint_lab.config(text=hint_s, fg="#8fb3e8")
                self.hint_lab.place(x=20, y=82, width=self.W - 40)
                self.win.update_idletasks()
                req_hint_h = max(20, self.hint_lab.winfo_reqheight() + 2)
                btn_y = 82 + req_hint_h + 6
                self.btn_frame.place(x=20, y=btn_y, width=self.W - 40, height=btn_h)
                target_h = max(self.H_COMPACT, btn_y + btn_h + 14)

        self.H = target_h
        try:
            sh = self.master.winfo_screenheight()
            if self.y + self.H > sh - 16:
                self.y = max(12, sh - self.H - 16)
        except Exception:
            pass
        self.win.geometry(f"{self.W}x{self.H}+{self.x}+{self.y}")
        self._draw_bar(self._cur)

    def set_runtime_prompt(self, state_dict):
        self._ui(lambda: self._apply_runtime_prompt(state_dict))

    def _apply_runtime_prompt(self, state_dict):
        if self.closed or self.win is None:
            return
        self._rt_state = dict(state_dict or {})
        prompt = self._rt_state.get("prompt") or {}
        mode = prompt.get("mode", "running")
        if self._rt_state.get("paused") and mode != "waiting_fix":
            mode = "paused"
        if mode == "running" and self._bar_color == "#ff9f0a":
            self._bar_color = "#0a84ff"
        self._layout_mode(mode, prompt)

    def _draw_capsule(self, x0, y0, x1, y1, color):
        h = y1 - y0
        if x1 - x0 <= h:
            r = max(1, (x1 - x0) / 2)
            cy = (y0 + y1) / 2
            self.cv.create_oval(x0, cy - r, x1, cy + r, fill=color, outline="")
            return
        self.cv.create_oval(x0, y0, x0 + h, y1, fill=color, outline="")
        self.cv.create_oval(x1 - h, y0, x1, y1, fill=color, outline="")
        self.cv.create_rectangle(x0 + h / 2, y0, x1 - h / 2, y1, fill=color, outline="")

    def _draw_bar(self, frac):
        if self.win is None or not hasattr(self, "cv"):
            return
        self.cv.delete("all")
        self._draw_capsule(0, 0, self.bw, 8, "#2c313d")
        fw = int(self.bw * max(0.0, min(1.0, frac)))
        if fw > 0:
            self._draw_capsule(0, 0, max(8, fw), 8, self._bar_color)
        if hasattr(self, "dot_lab"):
            self.dot_lab.config(fg=self._bar_color)
        if hasattr(self, "pct_lab"):
            self.pct_lab.config(text=f"{int(round(frac * 100))}%")

    # ---- 线程安全：使用 SimpleQueue 由主线程定时消费，杜绝跨线程调用 Tcl 崩溃或丢消息 ----
    def _drain_ui(self):
        while True:
            try:
                fn = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:
                pass

    def _pump_ui(self):
        self._drain_ui()
        try:
            self.master.after(35, self._pump_ui)
        except Exception:
            pass

    def _ui(self, fn):
        try:
            self._ui_queue.put(fn)
        except Exception:
            pass

    # ---- 生命周期（公开方法可在任意线程调用） ----
    def show(self):
        self._ui(self._show)

    def _show(self):
        self._ensure_win()
        if self._close_timer is not None:
            try:
                self.win.after_cancel(self._close_timer)
            except Exception:
                pass
            self._close_timer = None
        # 每次显示都重置为初始蓝色、进度归零、恢复紧凑运行态与按键
        self.closed = False
        self._bar_color = "#0a84ff"
        self._cur = 0.0
        self._target = 0.0
        if hasattr(self, "step_lab"):
            self.step_lab.config(text="准备开始…", fg="#ffffff")
        self._layout_mode("running", {})
        self.win.deiconify()
        self.win.attributes("-alpha", 0)
        self._fade_in(0, 8)
        if self._bar_id is None:
            self._bar_tick()

    def _fade_in(self, i, n):
        if self.closed or self.win is None:
            return
        self.win.attributes("-alpha", self.OPACITY * ease_out_cubic(i / n))
        if i < n:
            self.win.after(18, lambda: self._fade_in(i + 1, n))

    @staticmethod
    def _clean_text(text):
        import re
        s = re.sub(r"[\u2600-\u27BF\U0001F300-\U0001FAFF\u23F0-\u23FA\u25A0-\u25FF\uFE0F]+", "", str(text or "")).strip()
        return s

    def update_step(self, text, progress, color=None):
        self._ui(lambda: self._update_step(text, progress, color))

    def _update_step(self, text, progress, color=None):
        if self.closed or self.win is None or not hasattr(self, "step_lab"):
            return
        text = self._clean_text(text)
        self._target = max(0.0, min(1.0, progress))
        if color:
            self._bar_color = color
        if text and self.step_lab.cget("text") != text:
            self._text_anim(text)
        if self._bar_id is None and not self.closed:
            self._bar_tick()

    def _text_anim(self, text):
        if self.win is None or not hasattr(self, "step_lab"):
            return
        self.step_lab.config(text=text)
        start = hex2rgb("#7b8290")
        end = hex2rgb("#ffffff")
        n = 8

        def f(i=0):
            if self.win is None or not hasattr(self, "step_lab"):
                return
            e = ease_out_cubic(i / n)
            col = rgb2hex(tuple(
                int(start[k] + (end[k] - start[k]) * e) for k in range(3)))
            self.step_lab.config(fg=col)
            if i < n:
                self.win.after(16, lambda: f(i + 1))
        f()

    def _bar_tick(self):
        if self.win is None:
            self._bar_id = None
            return
        diff = self._target - self._cur
        if abs(diff) < 0.002:
            self._cur = self._target
        else:
            self._cur += diff * 0.25
        self._draw_bar(self._cur)
        if self._cur != self._target and not self.closed:
            self._bar_id = self.win.after(16, self._bar_tick)
        else:
            self._bar_id = None

    def finish(self, text="本轮已完成"):
        self._ui(lambda: self._finish(text))

    def _finish(self, text):
        if self.closed or self.win is None or not hasattr(self, "step_lab"):
            return
        text = self._clean_text(text) or "本轮已完成"
        self._bar_color = "#34c759"
        self._target = 1.0
        self._layout_mode("running", {"hint": "本轮已完成，浮窗将自动收起"})
        self.title_lab.config(text=f"{self.title_text} · 已完成", fg="#4cd964")
        if self.step_lab.cget("text") != text:
            self._text_anim(text)
        if self._bar_id is None and not self.closed:
            self._bar_tick()

    def fail(self, text):
        self._ui(lambda: self._fail(text))

    def _fail(self, text):
        if self.win is None or not hasattr(self, "step_lab"):
            return
        text = self._clean_text(text)
        self._bar_color = "#ff3b30"
        self.title_lab.config(text=f"{self.title_text} · 已结束", fg="#ff6961")
        self._draw_bar(self._cur)
        if self.step_lab.cget("text") != text:
            self._text_anim(text)

    def close(self, delay=900):
        self._ui(lambda: self._close(delay))

    def _close(self, delay):
        if self.win is None:
            return
        if self._close_timer is not None:
            try:
                self.win.after_cancel(self._close_timer)
            except Exception:
                pass
        self._close_timer = self.win.after(delay, self._do_close)

    def _do_close(self):
        self._close_timer = None
        if self.closed or self.win is None:
            return
        self.closed = True
        self._fade_out(0, 8)

    def _fade_out(self, i, n):
        if not self.closed or self.win is None:
            return  # 新一轮已开始，旧关闭动画不得隐藏新浮窗
        self.win.attributes("-alpha", self.OPACITY * max(0, 1 - i / n))
        if i < n:
            self.win.after(18, lambda: self._fade_out(i + 1, n))
        else:
            self.win.withdraw()   # 隐藏并保留，供下一轮复用


# ==================== Liquid Glass 模态弹窗 ====================
def glass_dialog(parent, title, message, ok_text="好", cancel_text=None,
                 accent="#0a84ff"):
    """带淡入、上浮与按钮回弹的模态弹窗，返回是否确认。"""
    parent.update_idletasks()
    base = "#f5f8fd"
    try:
        rgb = parent.winfo_rgb(parent.cget("bg"))
        if sum(rgb[:3]) / 3 < 30000:
            base = "#202a38"
    except Exception:
        pass
    dark = base == "#202a38"
    ctx = NeuContext(base, base)
    w = 460
    h = 280 if len(message) < 55 else 318
    top = tk.Toplevel(parent)
    top.title("暗号表助手")
    # Windows 11 对 overrideredirect + transparentcolor + alpha 的组合
    # 可能把整窗合成成完全透明。弹窗使用原生边框与 DWM 圆角，淡入淡出
    # 仅通过 alpha 完成，优先保证所有机器都能稳定看见和操作。
    top.configure(bg=base)
    top.attributes("-alpha", 0.0)
    top.attributes("-topmost", True)
    top.resizable(False, False)
    try:
        import ctypes
        top.update_idletasks()
        corner = ctypes.c_int(2)  # DWMWCP_ROUND
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            top.winfo_id(), 33, ctypes.byref(corner), ctypes.sizeof(corner))
    except Exception:
        pass
    px, py = parent.winfo_rootx(), parent.winfo_rooty()
    pw, ph = parent.winfo_width(), parent.winfo_height()
    if pw < 50 or ph < 50:
        pw, ph = parent.winfo_screenwidth(), parent.winfo_screenheight()
        px = py = 0
    top.geometry(f"{w}x{h}+{px + (pw - w) // 2}+{py + (ph - h) // 2}")
    inner_w, inner_h = w - 56, h - 76
    card = NeuCard(top, ctx, inner_w, inner_h, radius=28, pad=14)
    card.place(x=14, y=14)
    fg = "#f5f7fb" if dark else "#152238"
    sub = "#b8c2d2" if dark else "#667085"
    tk.Label(card.inner, text=title, bg=base, fg=fg,
             font=("Microsoft YaHei UI", 16, "bold")).place(x=26, y=20)
    tk.Label(card.inner, text=message, bg=base, fg=sub, justify="left",
             wraplength=inner_w - 52, font=("Microsoft YaHei UI", 11)).place(x=26, y=64)
    result = {"value": False}
    closing = {"on": False}

    def close(value):
        if closing["on"]:
            return
        closing["on"] = True
        result["value"] = value

        def out(i=0):
            if i <= 6:
                top.attributes("-alpha", 0.96 * (1 - i / 6))
                card.place(y=14 + i)
                top.after(16, lambda: out(i + 1))
            else:
                try:
                    top.grab_release()
                except Exception:
                    pass
                top.destroy()
        out()

    button_y = inner_h - 72
    if cancel_text:
        NeuButton(card.inner, ctx, 112, 40, cancel_text,
                  command=lambda: close(False), radius=15).place(
                      x=inner_w - 284, y=button_y)
        NeuButton(card.inner, ctx, 112, 40, ok_text,
                  command=lambda: close(True), accent=accent,
                  filled=True, radius=15).place(x=inner_w - 136, y=button_y)
    else:
        NeuButton(card.inner, ctx, 132, 40, ok_text,
                  command=lambda: close(True), accent=accent,
                  filled=True, radius=15).place(x=inner_w - 156, y=button_y)
    top.bind("<Escape>", lambda _: close(False))
    top.bind("<Return>", lambda _: close(True))
    top.transient(parent)
    top.grab_set()

    def enter(i=0):
        if not top.winfo_exists():
            return
        t = ease_out_cubic(i / 7)
        top.attributes("-alpha", 0.96 * t)
        card.place(y=20 - 6 * t)
        if i < 7:
            top.after(16, lambda: enter(i + 1))
        else:
            top.focus_force()
    enter()
    parent.wait_window(top)
    return result["value"]


def notify(parent, title, message, ok_text="知道了"):
    return glass_dialog(parent, title, message, ok_text=ok_text)


def confirm(parent, title, message, ok_text="确认", cancel_text="取消"):
    return glass_dialog(parent, title, message, ok_text=ok_text,
                        cancel_text=cancel_text)
