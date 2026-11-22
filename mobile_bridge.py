# -*- coding: utf-8 -*-
# pyright: reportMissingImports=false, reportMissingModuleSource=false
"""
暗号表助手 SheetMate 手机端专属远程伴侣网站与实时双向同步服务（纯标准库实现）：
- 电脑端自动启动高颜值手机网页服务（同 Wi-Fi 秒开，并支持一键拉起公网穿透链接供 4G/5G 访问）
- 纯标准库实现的离线 ISO QR Code SVG 生成器（零外部 API 依赖）
- 手机端凭唯一昵称验证连接后，可与电脑内置智能小猫伙伴实时双向同步对话、远程跑表、截图投递给指定钉钉对象
"""

import base64
from collections import deque
import hashlib
import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import threading
import time
import unicodedata
import urllib.error
from urllib.parse import parse_qs, urlparse
import urllib.request

DEFAULT_REMOTE_PASSWORD = "Qq@112233"
GITHUB_PAGES_URL = "https://sorenskr.github.io/SheetMate/"
NTFY_BASE_URL = "https://ntfy.sh"
MAX_HTTP_BODY_BYTES = 2 * 1024 * 1024
AUTH_MAX_FAILS = 5
AUTH_LOCKOUT_SEC = 60.0
RELAY_REPLAY_WINDOW_SEC = 180


def _safe_int(val, default=0, min_val=None, max_val=None):
    try:
        n = int(val)
    except Exception:
        return default
    if min_val is not None and n < min_val:
        n = min_val
    if max_val is not None and n > max_val:
        n = max_val
    return n


def _safe_read_json(rfile, headers, max_bytes=MAX_HTTP_BODY_BYTES):
    try:
        ln = int(headers.get("Content-Length") or 0)
    except Exception:
        return {}
    if ln <= 0:
        return {}
    if ln > max_bytes:
        raise ValueError("PayloadTooLarge")
    raw = rfile.read(ln)
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


class CloudRelayCrypto:
    """零知识频道派生 + PBKDF2-HMAC-SHA256 + CTR-SHA256 流加密 + 16B MAC 防篡改防重放套件。"""

    @staticmethod
    def normalize_nick_key(name):
        s = unicodedata.normalize("NFKC", str(name or ""))
        return "".join(ch for ch in s if not ch.isspace()).casefold()

    @classmethod
    def derive_room_and_key(cls, nickname, password):
        nk = cls.normalize_nick_key(nickname)
        pw = str(password or "")
        room_mac = hmac.new(
            b"SheetMate-Room-v28",
            f"{nk}::{pw}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()[:26]
        master_key = hashlib.pbkdf2_hmac(
            "sha256",
            pw.encode("utf-8"),
            f"SheetMate-Key-v28::{nk}".encode("utf-8"),
            4000,
            dklen=32,
        )
        return {
            "room_id": room_mac,
            "up_topic": f"sm28_u_{room_mac}",
            "down_topic": f"sm28_d_{room_mac}",
            "master_key": master_key,
        }

    @staticmethod
    def _keystream(master_key, nonce, length):
        out = bytearray()
        idx = 0
        while len(out) < length:
            out.extend(hashlib.sha256(master_key + nonce + idx.to_bytes(4, "big")).digest())
            idx += 1
        return bytes(out[:length])

    @classmethod
    def encrypt_packet(cls, master_key, payload_dict):
        obj = dict(payload_dict if isinstance(payload_dict, dict) else {})
        obj.setdefault("ts", int(time.time()))
        pt = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        nonce = secrets.token_bytes(12)
        ks = cls._keystream(master_key, nonce, len(pt))
        ct = bytes(p ^ k for p, k in zip(pt, ks))
        mac = hmac.new(master_key, nonce + ct, hashlib.sha256).digest()[:16]
        b64 = base64.b64encode(nonce + mac + ct).decode("ascii")
        return f"SM28:{b64}"

    @classmethod
    def decrypt_packet(cls, master_key, token_str, seen_nonces=None, now_ts=None):
        s = str(token_str or "").strip()
        if s.startswith("SM28:"):
            s = s[5:]
        if not s:
            return None
        try:
            raw = base64.b64decode(s.encode("ascii"), validate=True)
        except Exception:
            return None
        if len(raw) < 28:
            return None
        nonce, mac, ct = raw[:12], raw[12:28], raw[28:]
        exp_mac = hmac.new(master_key, nonce + ct, hashlib.sha256).digest()[:16]
        if not hmac.compare_digest(mac, exp_mac):
            return None
        n_hex = nonce.hex()
        if seen_nonces is not None and n_hex in seen_nonces:
            return None
        ks = cls._keystream(master_key, nonce, len(ct))
        pt = bytes(c ^ k for c, k in zip(ct, ks))
        try:
            obj = json.loads(pt.decode("utf-8"))
        except Exception:
            return None
        if not isinstance(obj, dict):
            return None
        cur_t = int(time.time() if now_ts is None else now_ts)
        pkt_ts = _safe_int(obj.get("ts"), default=0)
        if abs(cur_t - pkt_ts) > RELAY_REPLAY_WINDOW_SEC:
            return None
        if seen_nonces is not None:
            seen_nonces.append(n_hex)
        return obj


# ==================== 纯标准库离线 QR Code (Byte Mode, Version 1..8, ECC-L) ====================

_QR_CAP_L = {
    1: (19, 7, 1, 19),
    2: (34, 10, 1, 34),
    3: (55, 15, 1, 55),
    4: (80, 20, 1, 80),
    5: (108, 26, 1, 108),
    6: (136, 18, 2, 68),
    7: (156, 20, 2, 78),
    8: (194, 24, 2, 97),
}

_QR_ALIGN = {
    1: [],
    2: [6, 18],
    3: [6, 22],
    4: [6, 26],
    5: [6, 30],
    6: [6, 34],
    7: [6, 22, 38],
    8: [6, 24, 42],
}

_GF_EXP = [0] * 512
_GF_LOG = [0] * 256
_x = 1
for _i in range(255):
    _GF_EXP[_i] = _x
    _GF_LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _GF_EXP[_i] = _GF_EXP[_i - 255]


def _gf_mul(a, b):
    if a == 0 or b == 0:
        return 0
    return _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


def _rs_gen_poly(deg):
    poly = [1]
    for i in range(deg):
        nxt = [0] * (len(poly) + 1)
        root = _GF_EXP[i]
        for j, coef in enumerate(poly):
            nxt[j] ^= coef
            nxt[j + 1] ^= _gf_mul(coef, root)
        poly = nxt
    return poly


def _rs_encode(data_bytes, ec_len):
    gen = _rs_gen_poly(ec_len)
    res = list(data_bytes) + [0] * ec_len
    for i in range(len(data_bytes)):
        factor = res[i]
        if factor != 0:
            for j in range(1, len(gen)):
                res[i + j] ^= _gf_mul(gen[j], factor)
    return res[len(data_bytes):]


def _qr_bch_format(data5):
    d = data5 << 10
    g = 0b10100110111
    for i in range(4, -1, -1):
        if d & (1 << (i + 10)):
            d ^= g << i
    return ((data5 << 10) | d) ^ 0b101010000010010


def _build_qr_matrix(text):
    raw = str(text or "").encode("utf-8")
    ver = None
    for v in range(1, 9):
        total_dc, _ec, _blocks, _bdata = _QR_CAP_L[v]
        # Mode(4) + Len(8) + Data(8*N) + Term(4)
        need_bits = 4 + 8 + len(raw) * 8
        if need_bits <= total_dc * 8:
            ver = v
            break
    if ver is None:
        raw = raw[:190]
        ver = 8

    total_dc, ec_per_blk, num_blocks, blk_data_len = _QR_CAP_L[ver]
    bits = []

    def _push(val, nbits):
        for sh in range(nbits - 1, -1, -1):
            bits.append((val >> sh) & 1)

    _push(0b0100, 4)  # Byte mode
    _push(len(raw), 8)
    for b in raw:
        _push(b, 8)
    rem_cap = total_dc * 8 - len(bits)
    _push(0, min(4, rem_cap))
    while len(bits) % 8 != 0:
        bits.append(0)
    pad_bytes = (0xEC, 0x11)
    pi = 0
    while len(bits) < total_dc * 8:
        _push(pad_bytes[pi % 2], 8)
        pi += 1

    codewords = []
    for i in range(0, len(bits), 8):
        val = 0
        for j in range(8):
            val = (val << 1) | bits[i + j]
        codewords.append(val)

    data_blocks = []
    ec_blocks = []
    pos = 0
    for _ in range(num_blocks):
        blk = codewords[pos:pos + blk_data_len]
        pos += blk_data_len
        data_blocks.append(blk)
        ec_blocks.append(_rs_encode(blk, ec_per_blk))

    interleaved = []
    for i in range(blk_data_len):
        for blk in data_blocks:
            interleaved.append(blk[i])
    for i in range(ec_per_blk):
        for eblk in ec_blocks:
            interleaved.append(eblk[i])

    all_bits = []
    for cw in interleaved:
        for sh in range(7, -1, -1):
            all_bits.append((cw >> sh) & 1)
    # Remainder bits (V2..V6 have 7, V7..V8 have 0)
    rem_mod = 7 if 2 <= ver <= 6 else 0
    all_bits.extend([0] * rem_mod)

    size = 17 + 4 * ver
    grid = [[None] * size for _ in range(size)]
    func = [[False] * size for _ in range(size)]

    def _set(r, c, v, is_fn=True):
        if 0 <= r < size and 0 <= c < size:
            grid[r][c] = 1 if v else 0
            if is_fn:
                func[r][c] = True

    def _finder(r0, c0):
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                r, c = r0 + dr, c0 + dc
                if not (0 <= r < size and 0 <= c < size):
                    continue
                if 0 <= dr <= 6 and 0 <= dc <= 6:
                    dark = (dr in (0, 6) or dc in (0, 6) or (2 <= dr <= 4 and 2 <= dc <= 4))
                else:
                    dark = False
                _set(r, c, dark, True)

    _finder(0, 0)
    _finder(0, size - 7)
    _finder(size - 7, 0)

    for i in range(8, size - 8):
        _set(6, i, i % 2 == 0, True)
        _set(i, 6, i % 2 == 0, True)

    align_pos = _QR_ALIGN[ver]
    for ar in align_pos:
        for ac in align_pos:
            if func[ar][ac]:
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    dark = max(abs(dr), abs(dc)) != 1
                    _set(ar + dr, ac + dc, dark, True)

    # Dark module + reserve format areas
    _set(size - 8, 8, 1, True)
    for i in range(9):
        if not func[8][i]:
            _set(8, i, 0, True)
        if not func[i][8]:
            _set(i, 8, 0, True)
    for i in range(8):
        _set(8, size - 1 - i, 0, True)
        if i < 7:
            _set(size - 1 - i, 8, 0, True)
    if ver >= 7:
        for i in range(6):
            for j in range(3):
                _set(i, size - 11 + j, 0, True)
                _set(size - 11 + j, i, 0, True)

    # Zig-zag data placement
    bit_i = 0
    upward = True
    col = size - 1
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for row in rows:
            for dc in (0, -1):
                cc = col + dc
                if not func[row][cc]:
                    b = all_bits[bit_i] if bit_i < len(all_bits) else 0
                    bit_i += 1
                    grid[row][cc] = b
        upward = not upward
        col -= 2

    def _mask_cond(m, r, c):
        if m == 0:
            return (r + c) % 2 == 0
        if m == 1:
            return r % 2 == 0
        if m == 2:
            return c % 3 == 0
        if m == 3:
            return (r + c) % 3 == 0
        if m == 4:
            return ((r // 2) + (c // 3)) % 2 == 0
        if m == 5:
            return ((r * c) % 2 + (r * c) % 3) == 0
        if m == 6:
            return (((r * c) % 2 + (r * c) % 3) % 2) == 0
        return (((r + c) % 2 + (r * c) % 3) % 2) == 0

    def _apply_format(g, mask_id):
        fmt = _qr_bch_format((0b01 << 3) | mask_id)  # ECC L = 01
        c_coords = [(8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5), (8, 7), (8, 8),
                    (7, 8), (5, 8), (4, 8), (3, 8), (2, 8), (1, 8), (0, 8)]
        c_coords2 = [(size - 1, 8), (size - 2, 8), (size - 3, 8), (size - 4, 8),
                     (size - 5, 8), (size - 6, 8), (size - 7, 8), (8, size - 8),
                     (8, size - 7), (8, size - 6), (8, size - 5), (8, size - 4),
                     (8, size - 3), (8, size - 2), (8, size - 1)]
        for k in range(15):
            b = (fmt >> (14 - k)) & 1
            r1, col1 = c_coords[k]
            r2, col2 = c_coords2[k]
            g[r1][col1] = b
            g[r2][col2] = b

    if ver >= 7:
        rem_v = ver << 12
        gv = 0b1111100100101
        for i in range(5, -1, -1):
            if rem_v & (1 << (i + 12)):
                rem_v ^= gv << i
        vbits = (ver << 12) | rem_v
        for i in range(18):
            vb = (vbits >> i) & 1
            r = i // 3
            c = size - 11 + (i % 3)
            grid[r][c] = vb
            grid[c][r] = vb

    best_mask = 0
    best_pen = 10**9
    best_grid = None
    for m_id in range(8):
        cand = [[grid[r][c] for c in range(size)] for r in range(size)]
        for r in range(size):
            for c in range(size):
                if not func[r][c] and _mask_cond(m_id, r, c):
                    cand[r][c] ^= 1
        _apply_format(cand, m_id)
        # 快速行/列连续同色惩罚计算选优掩码
        pen = 0
        for r in range(size):
            run = 1
            for c in range(1, size):
                if cand[r][c] == cand[r][c - 1]:
                    run += 1
                    if run == 5:
                        pen += 3
                    elif run > 5:
                        pen += 1
                else:
                    run = 1
        for c in range(size):
            run = 1
            for r in range(1, size):
                if cand[r][c] == cand[r - 1][c]:
                    run += 1
                    if run == 5:
                        pen += 3
                    elif run > 5:
                        pen += 1
                else:
                    run = 1
        if pen < best_pen:
            best_pen = pen
            best_mask = m_id
            best_grid = cand

    return best_grid


def generate_qr_svg(url_text, box_px=200):
    """纯离线生成清晰可扫描的 SVG 二维码。"""
    mat = _build_qr_matrix(url_text)
    n = len(mat)
    quiet = 3
    full = n + quiet * 2
    paths = []
    for r in range(n):
        c = 0
        while c < n:
            if mat[r][c]:
                start = c
                while c < n and mat[r][c]:
                    c += 1
                paths.append(f"M{start + quiet},{r + quiet}h{c - start}v1h-{c - start}z")
            else:
                c += 1
    d_str = "".join(paths)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {full} {full}" '
        f'width="{box_px}" height="{box_px}" shape-rendering="crispEdges">'
        f'<rect width="{full}" height="{full}" rx="2" fill="#FFFFFF"/>'
        f'<path d="{d_str}" fill="#0F172A"/>'
        f'</svg>'
    )


# ==================== 手机/跨网远程伴侣 Web 应用模板 ====================

MOBILE_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,viewport-fit=cover" />
<meta name="apple-mobile-web-app-capable" content="yes" />
<title>暗号表助手 · 全球远程智能协同站</title>
<style>
  :root {
    --bg: #F2F4F9;
    --card: rgba(255, 255, 255, 0.90);
    --text: #0F172A;
    --sub: #64748B;
    --sep: rgba(15, 23, 42, 0.09);
    --blue: #0A84FF;
    --indigo: #5856D6;
    --green: #34C759;
    --orange: #FF9500;
    --red: #FF3B30;
    --pill: rgba(10, 132, 255, 0.12);
    --bubble-ai: #FFFFFF;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0B1020;
      --card: rgba(22, 30, 52, 0.88);
      --text: #F8FAFC;
      --sub: #94A3B8;
      --sep: rgba(248, 250, 252, 0.12);
      --pill: rgba(56, 189, 248, 0.16);
      --bubble-ai: rgba(30, 41, 68, 0.95);
    }
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html, body {
    margin: 0;
    padding: 0;
    height: 100%;
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", "Microsoft YaHei", sans-serif;
    background: var(--bg);
    color: var(--text);
  }
  .m-shell {
    max-width: 600px;
    margin: 0 auto;
    height: 100%;
    display: flex;
    flex-direction: column;
    padding-bottom: env(safe-area-inset-bottom);
  }
  .m-header {
    padding: 12px 14px 10px;
    padding-top: max(12px, env(safe-area-inset-top));
    background: var(--card);
    backdrop-filter: blur(20px);
    border-bottom: 0.5px solid var(--sep);
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    flex-shrink: 0;
  }
  .m-brand { display: flex; align-items: center; gap: 9px; min-width: 0; }
  .m-cat-ico {
    width: 38px;
    height: 38px;
    border-radius: 12px;
    background: linear-gradient(135deg, rgba(10, 132, 255, 0.16), rgba(99, 102, 241, 0.24));
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    animation: m-cat-bob 3.6s ease-in-out infinite;
  }
  @keyframes m-cat-bob {
    0%, 100% { transform: translateY(0) rotate(-2deg); }
    50% { transform: translateY(-2.5px) rotate(2.5deg); }
  }
  .m-title { font-size: 15px; font-weight: 800; line-height: 1.2; }
  .m-sub { font-size: 11px; color: var(--sub); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .m-pill {
    border: none;
    background: var(--pill);
    color: var(--blue);
    font-family: inherit;
    font-size: 11.5px;
    font-weight: 700;
    padding: 6px 11px;
    border-radius: 999px;
    cursor: pointer;
    flex-shrink: 0;
  }
  .m-login-wrap {
    flex: 1;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    justify-content: center;
    padding: 18px 16px;
  }
  .m-intro-box {
    background: linear-gradient(135deg, rgba(10, 132, 255, 0.10), rgba(88, 86, 214, 0.10));
    border: 0.5px solid rgba(10, 132, 255, 0.24);
    border-radius: 14px;
    padding: 11px 13px;
    font-size: 12.5px;
    line-height: 1.6;
    color: var(--text);
  }
  .m-intro-title {
    font-size: 13px;
    font-weight: 800;
    color: var(--blue);
    margin-bottom: 4px;
  }
  .m-card {
    background: var(--card);
    border: 0.5px solid var(--sep);
    border-radius: 20px;
    padding: 18px;
    box-shadow: 0 12px 32px rgba(15, 23, 42, 0.08);
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .m-label { font-size: 12px; font-weight: 700; color: var(--sub); margin-bottom: -6px; }
  .m-input {
    width: 100%;
    border: 1px solid var(--sep);
    background: rgba(120, 120, 128, 0.08);
    color: var(--text);
    border-radius: 12px;
    padding: 11px 13px;
    font-size: 16px;
    font-family: inherit;
    outline: none;
  }
  .m-input:focus { border-color: var(--blue); }
  .m-btn-primary {
    border: none;
    background: linear-gradient(135deg, #0A84FF, #4F46E5);
    color: #FFFFFF;
    font-family: inherit;
    font-size: 15px;
    font-weight: 700;
    padding: 12px;
    border-radius: 13px;
    cursor: pointer;
  }
  .m-btn-primary:disabled { opacity: 0.6; cursor: wait; }
  .m-main { flex: 1; display: none; flex-direction: column; min-height: 0; }
  .m-status-card {
    margin: 10px 12px 6px;
    padding: 10px 12px;
    background: var(--card);
    border: 0.5px solid var(--sep);
    border-radius: 15px;
    display: flex;
    flex-direction: column;
    gap: 7px;
    flex-shrink: 0;
  }
  .m-st-row { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
  .m-st-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--green); flex-shrink: 0; }
  .m-st-dot.busy { background: var(--blue); animation: m-pulse 1.1s infinite; }
  .m-st-dot.err { background: var(--red); }
  @keyframes m-pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
  .m-track { height: 5px; border-radius: 99px; background: rgba(120, 120, 128, 0.18); overflow: hidden; }
  .m-fill { height: 100%; width: 0%; background: linear-gradient(90deg, #0A84FF, #34C759); transition: width 0.3s; }
  .m-actions-row {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 6px;
  }
  .m-act-btn {
    border: 0.5px solid var(--sep);
    background: rgba(120, 120, 128, 0.1);
    color: var(--text);
    font-family: inherit;
    font-size: 12px;
    font-weight: 700;
    padding: 7px 4px;
    border-radius: 10px;
    cursor: pointer;
  }
  .m-act-btn.primary { background: rgba(10, 132, 255, 0.16); color: var(--blue); }
  .m-act-btn.danger { background: rgba(255, 59, 48, 0.14); color: var(--red); }
  .m-hands-off {
    display: none;
    margin: 0 12px 6px;
    padding: 7px 11px;
    border-radius: 10px;
    background: rgba(255, 149, 0, 0.16);
    color: var(--orange);
    font-size: 12px;
    font-weight: 700;
    flex-shrink: 0;
  }
  .m-hands-off.show { display: block; }
  .m-quick-bar {
    display: flex;
    gap: 6px;
    overflow-x: auto;
    padding: 4px 12px 6px;
    flex-shrink: 0;
    scrollbar-width: none;
  }
  .m-quick-bar::-webkit-scrollbar { display: none; }
  .m-chip {
    white-space: nowrap;
    border: 0.5px solid var(--sep);
    background: var(--card);
    color: var(--text);
    font-family: inherit;
    font-size: 12px;
    font-weight: 600;
    padding: 6px 11px;
    border-radius: 999px;
    cursor: pointer;
  }
  .m-feed {
    flex: 1;
    overflow-y: auto;
    padding: 8px 12px;
    display: flex;
    flex-direction: column;
    gap: 9px;
  }
  .m-bubble {
    max-width: 88%;
    padding: 9px 12px;
    border-radius: 15px;
    font-size: 13.5px;
    line-height: 1.5;
    white-space: pre-wrap;
    word-break: break-word;
  }
  .m-bubble.user {
    align-self: flex-end;
    background: linear-gradient(135deg, #0A84FF, #2563EB);
    color: #FFFFFF;
    border-bottom-right-radius: 4px;
  }
  .m-bubble.model {
    align-self: flex-start;
    background: var(--bubble-ai);
    border: 0.5px solid var(--sep);
    color: var(--text);
    border-bottom-left-radius: 4px;
  }
  .m-bubble.is-spot {
    border-color: rgba(52, 199, 89, 0.45);
    background: linear-gradient(135deg, rgba(52, 199, 89, 0.14), rgba(10, 132, 255, 0.1));
  }
  .m-b-meta { font-size: 10.5px; opacity: 0.72; margin-top: 5px; }
  .m-composer {
    padding: 8px 12px 10px;
    background: var(--card);
    border-top: 0.5px solid var(--sep);
    display: flex;
    gap: 8px;
    align-items: center;
    flex-shrink: 0;
  }
  .m-composer input {
    flex: 1;
    border: 1px solid var(--sep);
    background: rgba(120, 120, 128, 0.1);
    color: var(--text);
    border-radius: 999px;
    padding: 10px 14px;
    font-size: 16px;
    font-family: inherit;
    outline: none;
  }
  .m-send-btn {
    border: none;
    background: var(--blue);
    color: #FFFFFF;
    font-family: inherit;
    font-size: 14px;
    font-weight: 700;
    padding: 10px 16px;
    border-radius: 999px;
    cursor: pointer;
    flex-shrink: 0;
  }
  .m-send-btn:disabled { opacity: 0.5; }
  .m-toast {
    position: fixed;
    top: 64px;
    left: 50%;
    transform: translateX(-50%);
    background: rgba(15, 23, 42, 0.88);
    color: #FFFFFF;
    padding: 8px 16px;
    border-radius: 999px;
    font-size: 12.5px;
    font-weight: 600;
    z-index: 999;
    display: none;
    max-width: 88vw;
    text-align: center;
  }
</style>
</head>
<body>
<div class="m-shell">
  <header class="m-header">
    <div class="m-brand">
      <div class="m-cat-ico">
        <svg viewBox="0 0 48 48" width="32" height="32" fill="none">
          <path d="M10 19L8 7l11 7h10l11-7-2 12v10a13 13 0 01-26 0V19z" fill="#4F46E5" stroke="#BAE6FD" stroke-width="1.3"/>
          <rect x="11" y="19" width="26" height="18" rx="8" fill="#0F172A"/>
          <circle cx="18" cy="26.5" r="2.6" fill="#38BDF8"/>
          <circle cx="30" cy="26.5" r="2.6" fill="#38BDF8"/>
          <path d="M20.5 32 Q24 35 27.5 32" stroke="#F8FAFC" stroke-width="1.7" stroke-linecap="round"/>
        </svg>
      </div>
      <div style="min-width:0;">
        <div class="m-title">暗号表助手 · 全球远程智能协同站</div>
        <div class="m-sub" id="mHeaderSub">输入唯一名字与访问密码，随时随地安全同步电脑小猫</div>
      </div>
    </div>
    <button type="button" class="m-pill" id="mSwitchUserBtn" style="display:none;">退出/切换</button>
  </header>

  <div class="m-login-wrap" id="mLoginGate">
    <div class="m-card">
      <div class="m-intro-box">
        <div class="m-intro-title">关于本远程沟通站（由 Soren Drake 设计）</div>
        <div>
          无论您或同事身处公司、家中还是出差在外，只需输入目标电脑在「暗号表助手」里设置的<strong>唯一名字</strong>和<strong>访问密码</strong>，就能安全加密连接电脑上的专属智能小猫伙伴——随时对话查数、一句话指挥电脑自动跑表，或把指定日期报表直接静默发给钉钉指定对象，所有对话与进度都在电脑端实时同步！
        </div>
      </div>

      <div class="m-label">1. 目标电脑的唯一名字（必填 · 全局不可重名）</div>
      <input type="text" class="m-input" id="mLoginNameInput" placeholder="输入在电脑端【设置昵称】里起好的名字" autocomplete="username" />

      <div class="m-label">2. 远程访问密码（必填 · 自己与他人访问均需验证）</div>
      <input type="password" class="m-input" id="mLoginPinInput" placeholder="请输入访问密码（当前默认 Qq@112233）" autocomplete="current-password" />

      <details id="mBaseDetails" style="font-size:12px;color:var(--sub);">
        <summary style="cursor:pointer;">高级：手动指定内网直连网址（外地/跨网访问无需填写，自动端到端加密连线）</summary>
        <input type="text" class="m-input" id="mServerBaseInput" style="margin-top:8px;font-size:13px;" placeholder="同 Wi-Fi 下一般自动连接或留空" />
      </details>

      <div id="mLoginErr" style="font-size:12.5px;color:var(--red);display:none;line-height:1.45;"></div>
      <button type="button" class="m-btn-primary" id="mLoginSubmitBtn">密码验证并连接电脑</button>
    </div>
  </div>

  <div class="m-main" id="mMainApp">
    <div class="m-status-card">
      <div class="m-st-row">
        <div style="display:flex;align-items:center;gap:7px;min-width:0;">
          <span class="m-st-dot" id="mStatusDot"></span>
          <span style="font-size:13px;font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" id="mStepText">正在同步电脑状态…</span>
        </div>
        <span style="font-size:11.5px;font-weight:700;color:var(--blue);white-space:nowrap;" id="mBriefPill">实时在线</span>
      </div>
      <div class="m-track"><div class="m-fill" id="mProgFill"></div></div>
      <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--sub);">
        <span id="mLastRun">上次：—</span>
        <span id="mNextRun">下次：—</span>
      </div>
      <div class="m-actions-row">
        <button type="button" class="m-act-btn primary" data-mact="run_now">立即全流程</button>
        <button type="button" class="m-act-btn primary" data-mact="run_capture">仅截图发钉钉</button>
        <button type="button" class="m-act-btn" id="mPauseResumeBtn" data-mact="pause">暂停/续跑</button>
        <button type="button" class="m-act-btn danger" data-mact="stop">取消停止</button>
      </div>
    </div>

    <div class="m-hands-off" id="mHandsOffBanner">电脑正在自动粘贴发钉钉中，请勿操作电脑键鼠</div>

    <div class="m-quick-bar" id="mQuickChips">
      <button type="button" class="m-chip" data-mp="我要看昨天的数据，发给我">看昨天数据发给我</button>
      <button type="button" class="m-chip" data-mp="帮我跑一下程序，最新数据发给我">跑程序发给我</button>
      <button type="button" class="m-chip" data-mp="请按简报总结一下当前最新数据变化">总结当前数据</button>
      <button type="button" class="m-chip" data-mp="汇报一下电脑现在的工作状态与下次计划">查电脑状态</button>
    </div>

    <div class="m-feed" id="mChatFeed"></div>

    <div class="m-composer">
      <input type="text" id="mChatInput" placeholder="跟电脑小猫说：看昨天数据发给我 / 跑一下发给小王…" />
      <button type="button" class="m-send-btn" id="mSendBtn">发送</button>
    </div>
  </div>
</div>
<div class="m-toast" id="mToast"></div>

<script>
(function() {
  const SMCrypto = (function() {
    const te = new TextEncoder();
    const td = new TextDecoder();
    const K = new Uint32Array([
      0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
      0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
      0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
      0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
      0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
      0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
      0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
      0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2
    ]);
    const H0 = [0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19];
    function rotr(x, n) { return (x >>> n) | (x << (32 - n)); }
    function sha256(msg) {
      const len = msg.length;
      const total = Math.ceil((len + 9) / 64) * 64;
      const buf = new Uint8Array(total);
      buf.set(msg, 0);
      buf[len] = 0x80;
      const bitLenHi = Math.floor((len * 8) / 4294967296);
      const bitLenLo = (len * 8) >>> 0;
      const dv = new DataView(buf.buffer);
      dv.setUint32(total - 8, bitLenHi, false);
      dv.setUint32(total - 4, bitLenLo, false);
      let h = H0.slice();
      const w = new Uint32Array(64);
      for (let off = 0; off < total; off += 64) {
        for (let i = 0; i < 16; i++) w[i] = dv.getUint32(off + i * 4, false);
        for (let i = 16; i < 64; i++) {
          const s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3);
          const s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10);
          w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0;
        }
        let [a, b, c, d, e, f, g, hh] = h;
        for (let i = 0; i < 64; i++) {
          const S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
          const ch = (e & f) ^ ((~e) & g);
          const t1 = (hh + S1 + ch + K[i] + w[i]) >>> 0;
          const S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
          const maj = (a & b) ^ (a & c) ^ (b & c);
          const t2 = (S0 + maj) >>> 0;
          hh = g; g = f; f = e; e = (d + t1) >>> 0; d = c; c = b; b = a; a = (t1 + t2) >>> 0;
        }
        h = [(h[0]+a)>>>0,(h[1]+b)>>>0,(h[2]+c)>>>0,(h[3]+d)>>>0,(h[4]+e)>>>0,(h[5]+f)>>>0,(h[6]+g)>>>0,(h[7]+hh)>>>0];
      }
      const out = new Uint8Array(32);
      const odv = new DataView(out.buffer);
      for (let i = 0; i < 8; i++) odv.setUint32(i * 4, h[i], false);
      return out;
    }
    function concat(parts) {
      let tot = 0;
      for (const p of parts) tot += p.length;
      const out = new Uint8Array(tot);
      let off = 0;
      for (const p of parts) { out.set(p, off); off += p.length; }
      return out;
    }
    function hmacSha256(keyBytes, dataBytes) {
      const k = keyBytes.length > 64 ? sha256(keyBytes) : keyBytes;
      const ipad = new Uint8Array(64);
      const opad = new Uint8Array(64);
      ipad.fill(0x36); opad.fill(0x5c);
      for (let i = 0; i < k.length; i++) { ipad[i] ^= k[i]; opad[i] ^= k[i]; }
      return sha256(concat([opad, sha256(concat([ipad, dataBytes]))]));
    }
    function pbkdf2(pwBytes, saltBytes, iters) {
      const blk = concat([saltBytes, new Uint8Array([0, 0, 0, 1])]);
      let u = hmacSha256(pwBytes, blk);
      const t = u.slice();
      for (let i = 1; i < iters; i++) {
        u = hmacSha256(pwBytes, u);
        for (let j = 0; j < 32; j++) t[j] ^= u[j];
      }
      return t;
    }
    function toHex(u8) {
      let s = "";
      for (let i = 0; i < u8.length; i++) s += u8[i].toString(16).padStart(2, "0");
      return s;
    }
    function toB64(u8) {
      if (typeof Buffer !== "undefined") return Buffer.from(u8).toString("base64");
      let bin = "";
      for (let i = 0; i < u8.length; i++) bin += String.fromCharCode(u8[i]);
      return btoa(bin);
    }
    function fromB64(s) {
      if (typeof Buffer !== "undefined") return new Uint8Array(Buffer.from(s, "base64"));
      const bin = atob(s);
      const out = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
      return out;
    }
    function rand12() {
      const out = new Uint8Array(12);
      if (typeof crypto !== "undefined" && crypto.getRandomValues) {
        crypto.getRandomValues(out);
      } else {
        for (let i = 0; i < 12; i++) out[i] = Math.floor(Math.random() * 256);
      }
      return out;
    }
    function keystream(masterKey, nonce, len) {
      const chunks = [];
      let total = 0;
      let idx = 0;
      while (total < len) {
        const ctr = new Uint8Array(4);
        new DataView(ctr.buffer).setUint32(0, idx, false);
        chunks.push(sha256(concat([masterKey, nonce, ctr])));
        total += 32;
        idx += 1;
      }
      return concat(chunks).slice(0, len);
    }
    function normalizeNickKey(name) {
      return String(name || "").normalize("NFKC").replace(/\s+/g, "").toLowerCase();
    }
    function deriveRoomAndKey(nickname, password) {
      const nk = normalizeNickKey(nickname);
      const pw = String(password || "");
      const roomHex = toHex(hmacSha256(te.encode("SheetMate-Room-v28"), te.encode(nk + "::" + pw))).slice(0, 26);
      const masterKey = pbkdf2(te.encode(pw), te.encode("SheetMate-Key-v28::" + nk), 4000);
      return {
        roomId: roomHex,
        upTopic: "sm28_u_" + roomHex,
        downTopic: "sm28_d_" + roomHex,
        masterKey
      };
    }
    function encryptPacket(masterKey, obj) {
      const copy = Object.assign({ ts: Math.floor(Date.now() / 1000) }, obj || {});
      const pt = te.encode(JSON.stringify(copy));
      const nonce = rand12();
      const ks = keystream(masterKey, nonce, pt.length);
      const ct = new Uint8Array(pt.length);
      for (let i = 0; i < pt.length; i++) ct[i] = pt[i] ^ ks[i];
      const mac = hmacSha256(masterKey, concat([nonce, ct])).slice(0, 16);
      return "SM28:" + toB64(concat([nonce, mac, ct]));
    }
    function decryptPacket(masterKey, rawStr) {
      let s = String(rawStr || "").trim();
      if (s.startsWith("SM28:")) s = s.slice(5);
      if (!s) return null;
      let raw;
      try { raw = fromB64(s); } catch (_e) { return null; }
      if (!raw || raw.length < 28) return null;
      const nonce = raw.slice(0, 12);
      const mac = raw.slice(12, 28);
      const ct = raw.slice(28);
      const expMac = hmacSha256(masterKey, concat([nonce, ct])).slice(0, 16);
      let diff = 0;
      for (let i = 0; i < 16; i++) diff |= (mac[i] ^ expMac[i]);
      if (diff !== 0) return null;
      const ks = keystream(masterKey, nonce, ct.length);
      const pt = new Uint8Array(ct.length);
      for (let i = 0; i < ct.length; i++) pt[i] = ct[i] ^ ks[i];
      try {
        const obj = JSON.parse(td.decode(pt));
        if (!obj || typeof obj !== "object") return null;
        const now = Math.floor(Date.now() / 1000);
        if (Math.abs(now - (Number(obj.ts) || 0)) > 180) return null;
        return obj;
      } catch (_e) {
        return null;
      }
    }
    return { deriveRoomAndKey, encryptPacket, decryptPacket, normalizeNickKey };
  })();
  if (typeof globalThis !== "undefined") globalThis.SMCrypto = SMCrypto;
  if (typeof window === "undefined" || typeof document === "undefined") return;

  const NTFY_BASE = "https://ntfy.sh";
  const qs = new URLSearchParams(window.location.search);
  const $ = (id) => document.getElementById(id);
  let mobileToken = localStorage.getItem("sm_mobile_token") || "";
  let myNick = localStorage.getItem("sm_mobile_nick") || qs.get("name") || "";
  let serverBase = localStorage.getItem("sm_mobile_base") || qs.get("hub") || "";
  let connMode = localStorage.getItem("sm_conn_mode") || "";
  let relayCred = null; // { nick, pin, upTopic, downTopic, masterKey }
  let relayEs = null;
  let relayLastMsgId = "";
  const pendingReqs = new Map();
  let lastChatSeq = -1;
  let lastPaused = false;
  let pollTimer = null;

  if ($("mLoginNameInput") && myNick) $("mLoginNameInput").value = myNick;
  if ($("mServerBaseInput") && serverBase) $("mServerBaseInput").value = serverBase;

  function showToast(msg) {
    const t = $("mToast");
    t.textContent = msg;
    t.style.display = "block";
    clearTimeout(t._tm);
    t._tm = setTimeout(() => { t.style.display = "none"; }, 2800);
  }

  function baseUrl() {
    const raw = ($("mServerBaseInput") && $("mServerBaseInput").value.trim()) || serverBase || "";
    return raw.replace(/\/+$/, "");
  }

  function isLocalServed() {
    const b = baseUrl();
    if (b) return true;
    const h = window.location.hostname || "";
    if (h.endsWith("github.io") || window.location.protocol === "file:") return false;
    return true;
  }

  async function mApi(path, method = "GET", body = null) {
    const headers = { "Content-Type": "application/json" };
    if (mobileToken) headers["X-Mobile-Token"] = mobileToken;
    const opt = { method, headers };
    if (body !== null) opt.body = JSON.stringify(body);
    const resp = await fetch(baseUrl() + path, opt);
    return await resp.json();
  }

  function formatMsgText(raw) {
    return String(raw || "").replace(/\*\*([^*]+)\*\*/g, "$1");
  }

  function renderChats(list) {
    const feed = $("mChatFeed");
    const items = Array.isArray(list) ? list : [];
    if (!items.length) {
      feed.innerHTML = "";
      const welcome = document.createElement("div");
      welcome.className = "m-bubble model";
      const d = document.createElement("div");
      d.textContent = "你好！我是电脑上的专属智能小猫伙伴。你在网页发出的任何指令和对话都会实时同步到电脑屏幕上~";
      welcome.appendChild(d);
      feed.appendChild(welcome);
      return;
    }
    feed.innerHTML = "";
    for (const it of items) {
      const div = document.createElement("div");
      const isUser = it.role === "user";
      const isSpot = it.source === "data_spotlight";
      div.className = `m-bubble ${isUser ? "user" : "model"}${isSpot ? " is-spot" : ""}`;
      const body = document.createElement("div");
      body.textContent = formatMsgText(it.text || "");
      div.appendChild(body);
      const meta = document.createElement("div");
      meta.className = "m-b-meta";
      const viaTag = isUser ? (it.via === "mobile" ? "远程发出" : "电脑发出") : "小猫伙伴";
      meta.textContent = [it.time, viaTag, it.shot_info].filter(Boolean).join(" · ");
      div.appendChild(meta);
      feed.appendChild(div);
    }
    feed.scrollTop = feed.scrollHeight;
  }

  function applyPcState(st) {
    if (!st || typeof st !== "object") return;
    const dot = $("mStatusDot");
    dot.className = "m-st-dot" + (st.busy ? " busy" : (st.status_level === "err" ? " err" : ""));
    $("mStepText").textContent = st.step_text || "空闲中";
    $("mProgFill").style.width = `${Math.round((st.progress || 0) * 100)}%`;
    $("mLastRun").textContent = "上次：" + (st.last_run_str || "—");
    $("mNextRun").textContent = "下次：" + (st.next_run_str || "—");
    lastPaused = Boolean(st.paused);
    $("mPauseResumeBtn").textContent = lastPaused ? "继续执行" : "暂停运行";
    $("mPauseResumeBtn").dataset.mact = lastPaused ? "resume" : "pause";

    const db = st.data_brief || {};
    if (db.latest_total_reg !== null && db.latest_total_reg !== undefined) {
      $("mBriefPill").textContent = `总注册 ${db.latest_total_reg}`;
    } else {
      $("mBriefPill").textContent = connMode === "cloud" ? "跨网加密已连" : "实时在线";
    }
    const ho = $("mHandsOffBanner");
    if (st.hands_off && st.hands_off_text) {
      ho.textContent = st.hands_off_text;
      ho.classList.add("show");
    } else {
      ho.classList.remove("show");
    }
    if (typeof st.chat_seq === "number" && st.chat_seq !== lastChatSeq) {
      lastChatSeq = st.chat_seq;
      renderChats(st.ai_history || []);
    }
  }

  function showMainView() {
    $("mLoginGate").style.display = "none";
    $("mMainApp").style.display = "flex";
    $("mSwitchUserBtn").style.display = "inline-flex";
    const modeTag = connMode === "cloud" ? "跨网加密直连" : "内网直连";
    $("mHeaderSub").textContent = `${modeTag} · 用户：${myNick || "已连接"}`;
  }

  function closeRelayStream() {
    if (relayEs) {
      try { relayEs.close(); } catch (_e) {}
      relayEs = null;
    }
    for (const [_k, p] of pendingReqs.entries()) {
      clearTimeout(p.tm);
      p.reject(new Error("closed"));
    }
    pendingReqs.clear();
  }

  function showLoginView(errText = "") {
    $("mLoginGate").style.display = "flex";
    $("mMainApp").style.display = "none";
    $("mSwitchUserBtn").style.display = "none";
    $("mHeaderSub").textContent = "输入唯一名字与访问密码，随时随地安全同步电脑小猫";
    if (errText) {
      $("mLoginErr").textContent = errText;
      $("mLoginErr").style.display = "block";
    } else {
      $("mLoginErr").style.display = "none";
    }
  }

  function handleRelayIncomingText(rawMsg) {
    if (!relayCred || !rawMsg) return;
    const obj = SMCrypto.decryptPacket(relayCred.masterKey, rawMsg);
    if (!obj) return;
    if (obj.req_id && pendingReqs.has(obj.req_id)) {
      const p = pendingReqs.get(obj.req_id);
      pendingReqs.delete(obj.req_id);
      clearTimeout(p.tm);
      p.resolve(obj);
    }
    if (obj.state) applyPcState(obj.state);
    if (Array.isArray(obj.ai_history) && typeof obj.chat_seq === "number" && obj.chat_seq !== lastChatSeq) {
      lastChatSeq = obj.chat_seq;
      renderChats(obj.ai_history);
    }
  }

  function startRelaySubscribe() {
    if (!relayCred) return;
    closeRelayStream();
    if (typeof EventSource !== "undefined") {
      try {
        const es = new EventSource(`${NTFY_BASE}/${relayCred.downTopic}/sse?since=10s`);
        es.onmessage = (ev) => {
          try {
            const parsed = JSON.parse(ev.data);
            if (parsed && parsed.id) relayLastMsgId = parsed.id;
            if (parsed && parsed.message) handleRelayIncomingText(parsed.message);
          } catch (_e) {}
        };
        relayEs = es;
      } catch (_e) {}
    }
  }

  async function pollRelayOnce() {
    if (!relayCred) return;
    try {
      const sinceP = relayLastMsgId ? encodeURIComponent(relayLastMsgId) : "15s";
      const resp = await fetch(`${NTFY_BASE}/${relayCred.downTopic}/json?poll=1&since=${sinceP}`);
      const txt = await resp.text();
      for (const ln of txt.split(/\r?\n/)) {
        const s = ln.trim();
        if (!s) continue;
        try {
          const ev = JSON.parse(s);
          if (ev && ev.id) relayLastMsgId = ev.id;
          if (ev && ev.event === "message" && ev.message) handleRelayIncomingText(ev.message);
        } catch (_e) {}
      }
    } catch (_e) {}
  }

  async function sendRelayCommand(cmd, params = {}, timeoutMs = 15000) {
    if (!relayCred) throw new Error("未配置云桥连接");
    const reqId = Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
    const pkt = SMCrypto.encryptPacket(relayCred.masterKey, { req_id: reqId, cmd, params });
    const waitP = new Promise((resolve, reject) => {
      const tm = setTimeout(() => {
        if (pendingReqs.has(reqId)) {
          pendingReqs.delete(reqId);
          reject(new Error("timeout"));
        }
      }, timeoutMs);
      pendingReqs.set(reqId, { resolve, reject, tm });
    });
    const postP = fetch(`${NTFY_BASE}/${relayCred.upTopic}`, {
      method: "POST",
      headers: { "Content-Type": "text/plain; charset=utf-8" },
      body: pkt,
    });
    const quickPoll = setInterval(pollRelayOnce, 650);
    try {
      await postP;
      return await waitP;
    } finally {
      clearInterval(quickPoll);
    }
  }

  async function doPoll() {
    if (connMode === "cloud" && relayCred) {
      await pollRelayOnce();
      return;
    }
    if (!mobileToken) return;
    try {
      const st = await mApi("/api/mobile/state");
      if (st && st.auth_required) {
        mobileToken = "";
        localStorage.removeItem("sm_mobile_token");
        clearInterval(pollTimer);
        showLoginView("登录已失效，请重新输入唯一名字和访问密码");
        return;
      }
      if (st && st.ok) applyPcState(st);
    } catch (_e) {}
  }

  async function sendChat(text) {
    const msg = String(text !== undefined ? text : $("mChatInput").value).trim();
    if (!msg) return;
    $("mChatInput").value = "";
    $("mSendBtn").disabled = true;
    try {
      if (connMode === "cloud" && relayCred) {
        const res = await sendRelayCommand("chat", { prompt: msg, style_id: "custom" }, 32000);
        if (res && res.result) {
          const r = res.result;
          if (Array.isArray(r.history)) {
            lastChatSeq = typeof r.chat_seq === "number" ? r.chat_seq : lastChatSeq + 1;
            renderChats(r.history);
          } else if (r.reply) {
            showToast("电脑已回复");
          }
        }
      } else {
        const res = await mApi("/api/mobile/chat", "POST", { prompt: msg });
        if (res && Array.isArray(res.history)) {
          lastChatSeq = typeof res.chat_seq === "number" ? res.chat_seq : lastChatSeq + 1;
          renderChats(res.history);
        } else if (res && res.reply) {
          showToast("已发送给电脑处理");
          doPoll();
        } else {
          showToast((res && res.message) || "发送未成功");
        }
      }
    } catch (_e) {
      showToast("暂未收到电脑回应，请确认电脑软件处于开启状态");
    } finally {
      $("mSendBtn").disabled = false;
    }
  }

  $("mLoginSubmitBtn").onclick = async () => {
    const nick = $("mLoginNameInput").value.trim();
    const pin = $("mLoginPinInput").value.trim();
    const b = $("mServerBaseInput").value.trim();
    if (b) {
      serverBase = b;
      localStorage.setItem("sm_mobile_base", b);
    }
    if (!nick) {
      showLoginView("请输入目标电脑设置好的唯一名字");
      return;
    }
    if (!pin) {
      showLoginView("请输入访问密码（默认 Qq@112233）");
      return;
    }
    const btn = $("mLoginSubmitBtn");
    btn.disabled = true;
    btn.textContent = "正在加密校验连接…";
    try {
      let localWorked = false;
      if (isLocalServed()) {
        try {
          const r = await mApi("/api/mobile/auth", "POST", { nickname: nick, pin });
          if (r && r.ok && r.mobile_token) {
            connMode = "local";
            mobileToken = r.mobile_token;
            myNick = (r.profile && (r.profile.nickname || r.profile.display_name)) || nick;
            localStorage.setItem("sm_conn_mode", "local");
            localStorage.setItem("sm_mobile_token", mobileToken);
            localStorage.setItem("sm_mobile_nick", myNick);
            showMainView();
            await doPoll();
            clearInterval(pollTimer);
            pollTimer = setInterval(doPoll, 1200);
            localWorked = true;
          } else if (r && r.message && !b) {
            showLoginView(r.message);
            return;
          }
        } catch (_localErr) {}
      }
      if (!localWorked) {
        const derived = SMCrypto.deriveRoomAndKey(nick, pin);
        relayCred = Object.assign({ nick, pin }, derived);
        startRelaySubscribe();
        await new Promise((r) => setTimeout(r, 250));
        try {
          const ack = await sendRelayCommand("auth_state", {}, 9000);
          if (ack && ack.ok) {
            connMode = "cloud";
            myNick = ack.nickname || nick;
            localStorage.setItem("sm_conn_mode", "cloud");
            localStorage.setItem("sm_mobile_nick", myNick);
            sessionStorage.setItem("sm_relay_pin", pin);
            showMainView();
            if (ack.state) applyPcState(ack.state);
            clearInterval(pollTimer);
            pollTimer = setInterval(doPoll, 2200);
            return;
          }
        } catch (_relayErr) {
          closeRelayStream();
          relayCred = null;
          showLoginView("未能连上目标电脑：请核对「唯一名字」和「访问密码」是否与电脑端一致，并确认电脑正在运行加密暗号表助手。");
        }
      }
    } finally {
      btn.disabled = false;
      btn.textContent = "密码验证并连接电脑";
    }
  };

  $("mSwitchUserBtn").onclick = () => {
    mobileToken = "";
    connMode = "";
    relayCred = null;
    closeRelayStream();
    localStorage.removeItem("sm_mobile_token");
    localStorage.removeItem("sm_conn_mode");
    sessionStorage.removeItem("sm_relay_pin");
    clearInterval(pollTimer);
    showLoginView("");
  };

  $("mSendBtn").onclick = () => sendChat();
  $("mChatInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      sendChat();
    }
  });
  for (const ch of $("mQuickChips").querySelectorAll("[data-mp]")) {
    ch.onclick = () => sendChat(ch.dataset.mp || "");
  }
  for (const ab of document.querySelectorAll("[data-mact]")) {
    ab.onclick = async () => {
      const act = ab.dataset.mact;
      try {
        if (connMode === "cloud" && relayCred) {
          const r = await sendRelayCommand("action", { action: act }, 12000);
          showToast((r && r.result && r.result.message) || "加密指令已送达电脑");
        } else {
          const r = await mApi("/api/mobile/action", "POST", { action: act });
          showToast((r && r.message) || "指令已下发给电脑");
        }
        setTimeout(doPoll, 350);
      } catch (_e) {
        showToast("操作指令暂未送达，请重试");
      }
    };
  }

  (async function bootMobile() {
    if (connMode === "local" && mobileToken && isLocalServed()) {
      showMainView();
      await doPoll();
      clearInterval(pollTimer);
      pollTimer = setInterval(doPoll, 1200);
      return;
    }
    const savedPin = sessionStorage.getItem("sm_relay_pin") || "";
    if (connMode === "cloud" && myNick && savedPin) {
      $("mLoginPinInput").value = savedPin;
      const derived = SMCrypto.deriveRoomAndKey(myNick, savedPin);
      relayCred = Object.assign({ nick: myNick, pin: savedPin }, derived);
      startRelaySubscribe();
      try {
        const ack = await sendRelayCommand("auth_state", {}, 6500);
        if (ack && ack.ok) {
          showMainView();
          if (ack.state) applyPcState(ack.state);
          clearInterval(pollTimer);
          pollTimer = setInterval(doPoll, 2200);
          return;
        }
      } catch (_e) {
        closeRelayStream();
        relayCred = null;
      }
    }
  })();
})();
</script>
</body>
</html>
"""


def _detect_lan_ipv4_list():
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip0 = s.getsockname()[0]
        s.close()
        if ip0 and not ip0.startswith(("127.", "169.254.")):
            ips.append(ip0)
    except Exception:
        pass
    try:
        for res in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = res[4][0]
            if ip and not ip.startswith(("127.", "169.254.")) and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    return ips or ["127.0.0.1"]


class MobileBridgeManager:
    """电脑端内嵌远程网站服务端 + GitHub Pages 端到端加密云桥 + 一键隧道管理器。"""

    def __init__(self, ctrl, base_port=18792):
        self.ctrl = ctrl
        self.base_port = int(base_port)
        self._lock = threading.RLock()
        self._server = None
        self._port = 0
        self._tokens = {}  # token -> {"nickname": str, "avatar": str, "ts": float}
        self._auth_fail_tracker = {}
        self.mobile_pin = DEFAULT_REMOTE_PASSWORD
        self.cloud_relay_enabled = True
        self._load_remote_cfg()

        # 云桥加密实时双向状态
        self._relay_stop = threading.Event()
        self._relay_thread = None
        self._relay_push_thread = None
        self._relay_seen_nonces = deque(maxlen=250)
        self._relay_last_active_ts = 0.0
        self._relay_last_pushed_seq = -1
        self._relay_connected = False
        self._relay_error = ""

        # 外网隧道状态
        self._tunnel_proc = None
        self._tunnel_running = False
        self._public_url = ""
        self._tunnel_error = ""
        self._tunnel_kind = ""

    def _remote_cfg_path(self):
        try:
            app_d = self.ctrl.app.app_dir() if hasattr(self.ctrl, "app") and hasattr(self.ctrl.app, "app_dir") else Path.cwd()
        except Exception:
            app_d = Path.cwd()
        return Path(app_d) / "remote_access.json"

    def _load_remote_cfg(self):
        p = self._remote_cfg_path()
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    pw = str(data.get("remote_password") or "").strip()
                    self.mobile_pin = pw or DEFAULT_REMOTE_PASSWORD
                    self.cloud_relay_enabled = bool(data.get("cloud_relay_enabled", True))
                    return
            except Exception:
                pass
        self.mobile_pin = DEFAULT_REMOTE_PASSWORD
        self.cloud_relay_enabled = True
        self._save_remote_cfg()

    def _save_remote_cfg(self):
        p = self._remote_cfg_path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "remote_password": self.mobile_pin,
                "cloud_relay_enabled": bool(self.cloud_relay_enabled),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(p)
        except Exception:
            pass

    def _get_local_nick(self):
        hub = getattr(self.ctrl, "hub", None)
        if hub is None:
            return ""
        try:
            prof = hub.get_profile() or {}
            nk = str(prof.get("nickname") or "").strip()
            if nk and not nk.startswith("未命名"):
                return nk
        except Exception:
            pass
        return ""

    def verify_pin_and_login(self, nick, pin, client_id="local"):
        now = time.time()
        with self._lock:
            rec = self._auth_fail_tracker.get(client_id) or {"cnt": 0, "until": 0.0}
            if rec.get("until", 0.0) > now:
                wait_s = max(1, int(rec["until"] - now))
                return {"ok": False, "locked": True, "message": f"密码连续输错过多，请 {wait_s} 秒后再试"}
            want_pin = str(self.mobile_pin or DEFAULT_REMOTE_PASSWORD)
            got_pin = str(pin or "").strip()
            if not got_pin or not hmac.compare_digest(got_pin, want_pin):
                cnt = int(rec.get("cnt", 0)) + 1
                until = (now + AUTH_LOCKOUT_SEC) if cnt >= AUTH_MAX_FAILS else 0.0
                self._auth_fail_tracker[client_id] = {"cnt": cnt, "until": until}
                if cnt >= AUTH_MAX_FAILS:
                    return {
                        "ok": False,
                        "locked": True,
                        "message": f"密码连续输错过多，已锁定 {int(AUTH_LOCKOUT_SEC)} 秒后再试",
                    }
                return {"ok": False, "message": "访问密码不正确，请核对后重试（默认密码 Qq@112233）"}
            self._auth_fail_tracker.pop(client_id, None)
        return self.ctrl.verify_mobile_member_login(nick)

    def start(self):
        with self._lock:
            if self._server is not None:
                return True, self._port
            mgr = self

            class MobileHandler(BaseHTTPRequestHandler):
                def log_message(self, fmt, *args):
                    return

                def _cors(self):
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Mobile-Token")
                    self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

                def do_OPTIONS(self):
                    self.send_response(HTTPStatus.NO_CONTENT)
                    self._cors()
                    self.end_headers()

                def _send_json(self, obj, code=HTTPStatus.OK):
                    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                    self.send_response(code)
                    self._cors()
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)

                def _authed_user(self):
                    tok = str(self.headers.get("X-Mobile-Token") or "").strip()
                    if not tok:
                        return None
                    now = time.time()
                    with mgr._lock:
                        info = mgr._tokens.get(tok)
                        if info and (now - info.get("ts", 0)) < 86400 * 30:
                            return dict(info)
                        if info:
                            mgr._tokens.pop(tok, None)
                    return None

                def do_GET(self):
                    parsed = urlparse(self.path)
                    if parsed.path in ("/", "/m", "/index.html"):
                        body = MOBILE_HTML_TEMPLATE.encode("utf-8")
                        self.send_response(HTTPStatus.OK)
                        self._cors()
                        self.send_header("Content-Type", "text/html; charset=utf-8")
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return

                    if parsed.path == "/api/mobile/ping":
                        self._send_json({
                            "ok": True,
                            "version": getattr(mgr.ctrl.app, "APP_VERSION", "2.8.0"),
                            "need_pin": True,
                        })
                        return

                    if parsed.path == "/api/mobile/state":
                        u = self._authed_user()
                        if u is None:
                            self._send_json({"ok": False, "auth_required": True, "message": "请先输入唯一名字与访问密码连接"}, HTTPStatus.FORBIDDEN)
                            return
                        qs = parse_qs(parsed.query)
                        since_log = _safe_int((qs.get("since_log") or ["0"])[0], default=0, min_val=0)
                        st = mgr.ctrl.get_state(since=since_log)
                        self._send_json(mgr._pack_mobile_state(st))
                        return

                    self._send_json({"ok": False, "message": "Not Found"}, HTTPStatus.NOT_FOUND)

                def do_POST(self):
                    parsed = urlparse(self.path)
                    try:
                        body = _safe_read_json(self.rfile, self.headers)
                    except ValueError:
                        self._send_json({"ok": False, "message": "请求体超出安全大小限制"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                        return

                    if parsed.path == "/api/mobile/auth":
                        nick = str(body.get("nickname") or "").strip()
                        pin = str(body.get("pin") or "").strip()
                        c_ip = str(self.client_address[0] if self.client_address else "local")
                        chk = mgr.verify_pin_and_login(nick, pin, client_id=c_ip)
                        if not chk.get("ok"):
                            code = HTTPStatus.TOO_MANY_REQUESTS if chk.get("locked") else HTTPStatus.OK
                            self._send_json(chk, code)
                            return
                        tok = secrets.token_urlsafe(24)
                        prof = chk.get("profile") or {}
                        now = time.time()
                        with mgr._lock:
                            for tk in [k for k, v in mgr._tokens.items() if (now - v.get("ts", 0)) >= 86400 * 30]:
                                mgr._tokens.pop(tk, None)
                            mgr._tokens[tok] = {
                                "nickname": prof.get("nickname") or prof.get("display_name") or nick,
                                "avatar": prof.get("avatar") or "",
                                "ts": now,
                            }
                            if len(mgr._tokens) > 80:
                                oldest = sorted(mgr._tokens.items(), key=lambda x: x[1]["ts"])[:20]
                                for k, _ in oldest:
                                    mgr._tokens.pop(k, None)
                        self._send_json({"ok": True, "mobile_token": tok, "profile": prof})
                        return

                    u = self._authed_user()
                    if u is None:
                        self._send_json({"ok": False, "auth_required": True, "message": "请先输入唯一名字与访问密码连接"}, HTTPStatus.FORBIDDEN)
                        return

                    if parsed.path == "/api/mobile/chat":
                        prompt = str(body.get("prompt") or "").strip()
                        style_id = str(body.get("style_id") or "custom").strip()
                        res = mgr.ctrl.summarize_ai(
                            style_id=style_id,
                            prompt=prompt,
                            action="send",
                            source_device="mobile",
                            speaker_name=u.get("nickname") or "",
                        )
                        if isinstance(res, dict):
                            res["chat_seq"] = getattr(mgr.ctrl, "_chat_seq", 0)
                        self._send_json(res)
                        return

                    if parsed.path == "/api/mobile/action":
                        r = mgr._exec_remote_action(str(body.get("action") or "").strip())
                        self._send_json(r if isinstance(r, dict) else {"ok": True})
                        return

                    self._send_json({"ok": False, "message": "Not Found"}, HTTPStatus.NOT_FOUND)

            for p in range(self.base_port, self.base_port + 20):
                try:
                    srv = ThreadingHTTPServer(("0.0.0.0", p), MobileHandler)
                    srv.daemon_threads = True
                    threading.Thread(target=srv.serve_forever, daemon=True, name="SheetMateMobileServer").start()
                    self._server = srv
                    self._port = srv.server_address[1]
                    self.start_cloud_relay()
                    return True, self._port
                except Exception:
                    continue
            return False, 0

    def _pack_mobile_state(self, st):
        return {
            "ok": True,
            "busy": st.get("busy"),
            "paused": st.get("paused"),
            "status_level": st.get("status_level"),
            "step_text": st.get("step_text"),
            "progress": st.get("progress"),
            "hands_off": st.get("hands_off"),
            "hands_off_text": st.get("hands_off_text"),
            "last_run_str": st.get("last_run_str"),
            "next_run_str": st.get("next_run_str"),
            "data_brief": st.get("data_brief"),
            "chat_seq": st.get("chat_seq", 0),
            "ai_history": st.get("ai_history", []),
            "logs": (st.get("logs") or [])[-15:],
        }

    def _exec_remote_action(self, act):
        if act == "run_now":
            return self.ctrl.start_run_now()
        if act == "run_capture":
            return self.ctrl.start_capture_only()
        if act == "pause":
            return self.ctrl.pause_current_task()
        if act == "resume":
            r = self.ctrl.resume_current_task("continue")
            return {"ok": bool(r[0]), "message": r[1]} if isinstance(r, tuple) else r
        if act == "stop":
            return self.ctrl.stop_current_task()
        return {"ok": False, "message": f"未知动作：{act}"}

    def start_cloud_relay(self):
        if self._relay_thread is not None and self._relay_thread.is_alive():
            return
        self._relay_stop.clear()
        self._relay_thread = threading.Thread(target=self._cloud_relay_loop, daemon=True, name="SheetMateCloudRelay")
        self._relay_thread.start()
        if self._relay_push_thread is None or not self._relay_push_thread.is_alive():
            self._relay_push_thread = threading.Thread(target=self._cloud_state_push_loop, daemon=True, name="SheetMateCloudPush")
            self._relay_push_thread.start()

    def _send_relay_down_packet(self, down_topic, master_key, payload):
        try:
            pkt = CloudRelayCrypto.encrypt_packet(master_key, payload)
            req = urllib.request.Request(
                f"{NTFY_BASE_URL}/{down_topic}",
                data=pkt.encode("utf-8"),
                headers={"Content-Type": "text/plain; charset=utf-8", "User-Agent": "SheetMateRelay/2.8"},
                method="POST",
            )
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=8.0, context=ctx):
                pass
            return True
        except Exception as exc:
            self._relay_error = str(exc)[:100]
            return False

    def _handle_relay_command(self, obj, nick, down_topic, master_key):
        self._relay_last_active_ts = time.time()
        cmd = str(obj.get("cmd") or "").strip()
        req_id = str(obj.get("req_id") or "")
        params = obj.get("params") if isinstance(obj.get("params"), dict) else {}
        if cmd == "auth_state":
            st = self.ctrl.get_state(since=0)
            self._relay_last_pushed_seq = int(st.get("chat_seq") or 0)
            self._send_relay_down_packet(down_topic, master_key, {
                "ok": True,
                "req_id": req_id,
                "nickname": nick,
                "state": self._pack_mobile_state(st),
            })
        elif cmd == "chat":
            res = self.ctrl.summarize_ai(
                style_id=str(params.get("style_id") or "custom"),
                prompt=str(params.get("prompt") or ""),
                action="send",
                source_device="mobile",
                speaker_name=nick,
            )
            st = self.ctrl.get_state(since=0)
            self._relay_last_pushed_seq = int(st.get("chat_seq") or 0)
            self._send_relay_down_packet(down_topic, master_key, {
                "ok": True,
                "req_id": req_id,
                "result": res,
                "state": self._pack_mobile_state(st),
            })
        elif cmd == "action":
            r = self._exec_remote_action(str(params.get("action") or ""))
            st = self.ctrl.get_state(since=0)
            self._send_relay_down_packet(down_topic, master_key, {
                "ok": True,
                "req_id": req_id,
                "result": r,
                "state": self._pack_mobile_state(st),
            })

    def _cloud_state_push_loop(self):
        while not self._relay_stop.is_set():
            try:
                if self.cloud_relay_enabled and (time.time() - self._relay_last_active_ts) < 600:
                    nick = self._get_local_nick()
                    if nick:
                        seq_now = int(getattr(self.ctrl, "_chat_seq", 0))
                        if seq_now != self._relay_last_pushed_seq:
                            self._relay_last_pushed_seq = seq_now
                            cred = CloudRelayCrypto.derive_room_and_key(nick, self.mobile_pin)
                            st = self.ctrl.get_state(since=0)
                            self._send_relay_down_packet(cred["down_topic"], cred["master_key"], {
                                "ok": True,
                                "state": self._pack_mobile_state(st),
                            })
            except Exception:
                pass
            time.sleep(1.1)

    def _cloud_relay_loop(self):
        backoff = 1.5
        while not self._relay_stop.is_set():
            nick = self._get_local_nick()
            if not self.cloud_relay_enabled or not nick:
                self._relay_connected = False
                time.sleep(2.0)
                continue
            pin_snap = self.mobile_pin
            cred = CloudRelayCrypto.derive_room_and_key(nick, pin_snap)
            up_topic = cred["up_topic"]
            down_topic = cred["down_topic"]
            master_key = cred["master_key"]
            stream_url = f"{NTFY_BASE_URL}/{up_topic}/json?since=10s"
            try:
                req = urllib.request.Request(stream_url, headers={"User-Agent": "SheetMateRelay/2.8"})
                ctx = ssl.create_default_context()
                with urllib.request.urlopen(req, timeout=75.0, context=ctx) as resp:
                    self._relay_connected = True
                    self._relay_error = ""
                    backoff = 1.5
                    for raw_ln in resp:
                        if self._relay_stop.is_set() or self._get_local_nick() != nick or self.mobile_pin != pin_snap or not self.cloud_relay_enabled:
                            break
                        s = raw_ln.decode("utf-8", errors="ignore").strip()
                        if not s:
                            continue
                        try:
                            ev = json.loads(s)
                        except Exception:
                            continue
                        if ev.get("event") != "message":
                            continue
                        obj = CloudRelayCrypto.decrypt_packet(master_key, ev.get("message", ""), seen_nonces=self._relay_seen_nonces)
                        if obj is not None:
                            threading.Thread(
                                target=self._handle_relay_command,
                                args=(obj, nick, down_topic, master_key),
                                daemon=True,
                            ).start()
            except Exception as exc:
                self._relay_connected = False
                self._relay_error = str(exc)[:100]
                time.sleep(backoff)
                backoff = min(15.0, backoff * 1.5)

    def stop(self):
        self._relay_stop.set()
        self.stop_tunnel()
        with self._lock:
            srv = self._server
            self._server = None
            self._port = 0
        if srv is not None:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:
                pass

    def start_tunnel(self):
        self.start()
        with self._lock:
            if self._tunnel_running and self._tunnel_proc is not None and self._tunnel_proc.poll() is None:
                return {"ok": True, "running": True, "public_url": self._public_url}
            port = self._port
            if not port:
                return {"ok": False, "message": "本地手机端口未就绪"}
            self._tunnel_error = ""
            self._public_url = ""

            cf_exe = shutil.which("cloudflared")
            ssh_exe = shutil.which("ssh")
            if cf_exe:
                cmd = [cf_exe, "tunnel", "--url", f"http://127.0.0.1:{port}", "--no-autoupdate"]
                kind = "cloudflared"
            elif ssh_exe:
                null_host = "NUL" if os.name == "nt" else "/dev/null"
                cmd = [
                    ssh_exe,
                    "-o", "StrictHostKeyChecking=no",
                    "-o", f"UserKnownHostsFile={null_host}",
                    "-o", "ServerAliveInterval=30",
                    "-R", f"80:127.0.0.1:{port}",
                    "nokey@localhost.run",
                ]
                kind = "ssh"
            else:
                self._tunnel_error = "当前系统未找到 ssh 或 cloudflared，推荐直接用 GitHub 全球远程网址或局域网访问"
                return {"ok": False, "message": self._tunnel_error}

            creation = subprocess.CREATE_NO_WINDOW if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW") else 0
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=creation,
                )
            except Exception as exc:
                self._tunnel_error = f"启动公网穿透失败：{exc}"
                return {"ok": False, "message": self._tunnel_error}

            self._tunnel_proc = proc
            self._tunnel_running = True
            self._tunnel_kind = kind

            def _watch_stream():
                url_pat = re.compile(r"https://[A-Za-z0-9.\-]+\.(?:trycloudflare\.com|lhr\.life|localhost\.run)")
                try:
                    for line in proc.stdout:
                        m = url_pat.search(line)
                        if m:
                            u = m.group(0).strip()
                            if "admin.localhost.run" in u:
                                continue
                            with self._lock:
                                self._public_url = u
                except Exception:
                    pass
                finally:
                    try:
                        if proc.stdout:
                            proc.stdout.close()
                    except Exception:
                        pass
                    with self._lock:
                        if self._tunnel_proc is proc:
                            self._tunnel_running = False

            threading.Thread(target=_watch_stream, daemon=True).start()
            return {"ok": True, "running": True, "kind": kind, "message": "正在打通外网直连隧道，约需 2~5 秒…"}

    def stop_tunnel(self):
        with self._lock:
            proc = self._tunnel_proc
            self._tunnel_proc = None
            self._tunnel_running = False
            self._public_url = ""
            self._tunnel_error = ""
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=1.5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                if proc.stdout:
                    proc.stdout.close()
            except Exception:
                pass
        return {"ok": True, "running": False, "message": "已关闭临时穿透隧道"}

    def set_pin(self, pin_str):
        val = str(pin_str or "").strip()[:32]
        if not val:
            return {"ok": False, "message": "为了远程安全，访问密码不能为空！"}
        with self._lock:
            self.mobile_pin = val
            self._relay_last_pushed_seq = -1
            self._save_remote_cfg()
        return {"ok": True, "need_pin": True, "remote_password": val, "message": f"访问密码已更新（已同步刷新加密通道）"}

    def get_brief_info(self):
        with self._lock:
            return {
                "running": self._server is not None,
                "port": self._port,
                "tunnel_running": bool(self._tunnel_running),
                "public_url": self._public_url,
                "github_pages_url": GITHUB_PAGES_URL,
                "cloud_relay_connected": bool(self._relay_connected),
                "has_local_nick": bool(self._get_local_nick()),
            }

    def get_full_info(self):
        if self._server is None:
            self.start()
        with self._lock:
            port = self._port
            lan_urls = [f"http://{ip}:{port}" for ip in _detect_lan_ipv4_list()] if port else []
            pub = self._public_url
            qr_github_svg = generate_qr_svg(GITHUB_PAGES_URL, box_px=192)
            local_target = pub or (lan_urls[0] if lan_urls else "")
            qr_local_svg = generate_qr_svg(local_target, box_px=192) if local_target else ""
            return {
                "ok": True,
                "running": self._server is not None,
                "port": port,
                "lan_urls": lan_urls,
                "tunnel_running": bool(self._tunnel_running),
                "public_url": pub,
                "tunnel_error": self._tunnel_error,
                "github_pages_url": GITHUB_PAGES_URL,
                "remote_password": self.mobile_pin,
                "cloud_relay_enabled": bool(self.cloud_relay_enabled),
                "cloud_relay_connected": bool(self._relay_connected),
                "cloud_relay_error": self._relay_error,
                "local_nick": self._get_local_nick(),
                "qr_target": GITHUB_PAGES_URL,
                "qr_svg": qr_github_svg,
                "qr_local_target": local_target,
                "qr_local_svg": qr_local_svg,
                "need_pin": True,
            }


def export_static_mobile_web(dest_path):
    """导出独立的便携远程网页 HTML 文件。"""
    p = Path(dest_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(MOBILE_HTML_TEMPLATE, encoding="utf-8")
    return p


def export_github_pages_site(docs_dir):
    """导出 GitHub Pages 静态部署目录（docs/index.html 与 docs/.nojekyll）。"""
    d = Path(docs_dir)
    d.mkdir(parents=True, exist_ok=True)
    idx = d / "index.html"
    idx.write_text(MOBILE_HTML_TEMPLATE, encoding="utf-8")
    nj = d / ".nojekyll"
    if not nj.exists():
        nj.write_text("", encoding="utf-8")
    return idx
