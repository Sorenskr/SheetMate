# -*- coding: utf-8 -*-
# pyright: reportMissingImports=false, reportMissingModuleSource=false
"""
暗号表助手 SheetMate 手机端专属远程伴侣网站与实时双向同步服务（纯标准库实现）：
- 电脑端自动启动高颜值手机网页服务（同 Wi-Fi 秒开，并支持一键拉起公网穿透链接供 4G/5G 访问）
- 纯标准库实现的离线 ISO QR Code SVG 生成器（零外部 API 依赖）
- 手机端凭唯一昵称验证连接后，可与电脑内置智能小猫伙伴实时双向同步对话、远程跑表、截图投递给指定钉钉对象
"""

import base64
import functools
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
import struct
import subprocess
import threading
import time
import unicodedata
from urllib.parse import parse_qs, urlparse

DEFAULT_REMOTE_PASSWORD = "admin"
GITHUB_PAGES_URL = "https://sorenskr.github.io/SheetMate/"
MQTT_TLS_BROKERS = (
    ("broker-cn.emqx.io", 8883),
    ("broker.emqx.io", 8883),
)
MAX_HTTP_BODY_BYTES = 2 * 1024 * 1024
AUTH_MAX_FAILS = 5
AUTH_LOCKOUT_SEC = 60.0
RELAY_REPLAY_WINDOW_SEC = 180


def _mqtt_enc_varlen(n):
    out = bytearray()
    n = int(n)
    while True:
        b = n % 128
        n //= 128
        if n > 0:
            b |= 0x80
        out.append(b)
        if n == 0:
            break
    return bytes(out)


def _mqtt_str(s):
    b = str(s or "").encode("utf-8")
    return struct.pack("!H", len(b)) + b


def _mqtt_build_connect(client_id, keepalive=45):
    vh = _mqtt_str("MQTT") + bytes([4, 2, (keepalive >> 8) & 0xFF, keepalive & 0xFF])
    pl = _mqtt_str(client_id)
    return b"\x10" + _mqtt_enc_varlen(len(vh) + len(pl)) + vh + pl


def _mqtt_build_subscribe(pkt_id, topic):
    vh = struct.pack("!H", pkt_id)
    pl = _mqtt_str(topic) + b"\x00"
    return b"\x82" + _mqtt_enc_varlen(len(vh) + len(pl)) + vh + pl


def _mqtt_build_publish(topic, payload_bytes, retain=False):
    hdr = 0x31 if retain else 0x30
    vh = _mqtt_str(topic)
    return bytes([hdr]) + _mqtt_enc_varlen(len(vh) + len(payload_bytes)) + vh + payload_bytes


def _mqtt_recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("MQTT EOF")
        buf.extend(chunk)
    return bytes(buf)


def _mqtt_read_packet(sock):
    hdr = _mqtt_recv_exact(sock, 1)[0]
    mult = 1
    rem = 0
    for idx in range(4):
        b = _mqtt_recv_exact(sock, 1)[0]
        rem += (b & 0x7F) * mult
        if (b & 0x80) == 0:
            break
        if idx == 3:
            raise ValueError("Malformed MQTT remaining length")
        mult *= 128
    if rem > MAX_HTTP_BODY_BYTES:
        raise ValueError("MQTT packet exceeds size limit")
    body = _mqtt_recv_exact(sock, rem) if rem else b""
    return hdr, body


def _mqtt_parse_publish(hdr, body):
    if len(body) < 2:
        return "", b""
    tlen = struct.unpack("!H", body[:2])[0]
    topic = body[2: 2 + tlen].decode("utf-8", errors="replace")
    pos = 2 + tlen
    if (hdr & 0x06) != 0:
        pos += 2
    return topic, body[pos:]


def _mqtt_publish_once(topic, payload_bytes, brokers=MQTT_TLS_BROKERS, timeout=4.0):
    pkt = _mqtt_build_publish(topic, payload_bytes, retain=False)
    ctx = ssl.create_default_context()
    last_err = ""
    for host, port in brokers:
        try:
            with socket.create_connection((host, port), timeout=timeout) as raw_sock:
                with ctx.wrap_socket(raw_sock, server_hostname=host) as ss:
                    ss.settimeout(timeout)
                    ss.sendall(_mqtt_build_connect(f"smtmp_{os.getpid()}_{secrets.token_hex(3)}", keepalive=20))
                    hdr, _ = _mqtt_read_packet(ss)
                    if (hdr & 0xF0) != 0x20:
                        continue
                    ss.sendall(pkt)
                    try:
                        ss.sendall(b"\xe0\x00")
                    except Exception:
                        pass
                    return True, ""
        except Exception as exc:
            last_err = str(exc)[:100]
            continue
    return False, last_err


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
        obj = json.loads(raw.decode("utf-8"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


class CloudRelayCrypto:
    """零知识频道派生 + PBKDF2-HMAC-SHA256 + CTR-SHA256 流加密 + 16B MAC 防篡改防重放套件。"""

    @staticmethod
    def normalize_nick_key(name):
        s = unicodedata.normalize("NFKC", str(name or ""))
        return "".join(ch for ch in s if not ch.isspace()).casefold()

    @staticmethod
    @functools.lru_cache(maxsize=16)
    def _derive_cached(nk, pw):
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

    @classmethod
    def derive_room_and_key(cls, nickname, password):
        nk = cls.normalize_nick_key(nickname)
        pw = str(password or "")
        return dict(cls._derive_cached(nk, pw))

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
        cur_chk_t = int(time.time() if now_ts is None else now_ts)
        n_hex = nonce.hex()
        if seen_nonces is not None:
            if isinstance(seen_nonces, dict):
                exp_existing = seen_nonces.get(n_hex)
                if exp_existing is not None and exp_existing > cur_chk_t:
                    return None
            elif n_hex in seen_nonces:
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
            if isinstance(seen_nonces, dict):
                expired_k = [k for k, exp_v in seen_nonces.items() if exp_v <= cur_t]
                for k in expired_k:
                    seen_nonces.pop(k, None)
                if len(seen_nonces) > 4096:
                    oldest = sorted(seen_nonces.items(), key=lambda it: it[1])[: len(seen_nonces) - 2048]
                    for k, _ in oldest:
                        seen_nonces.pop(k, None)
                seen_nonces[n_hex] = cur_t + RELAY_REPLAY_WINDOW_SEC
            elif hasattr(seen_nonces, "append"):
                seen_nonces.append(n_hex)
            elif hasattr(seen_nonces, "add"):
                seen_nonces.add(n_hex)
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


@functools.lru_cache(maxsize=16)
def _build_qr_svg_cached(url_text, box_px):
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


def generate_qr_svg(url_text, box_px=200):
    """纯离线生成清晰可扫描的 SVG 二维码（按 URL 与尺寸 LRU 缓存）。"""
    return _build_qr_svg_cached(str(url_text or ""), int(box_px or 200))


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
  }
  .m-title { font-size: 15px; font-weight: 800; line-height: 1.2; }
  .m-sub { font-size: 11px; color: var(--sub); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .m-hdr-actions {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-shrink: 0;
  }
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
    text-decoration: none;
    display: inline-flex;
    align-items: center;
    gap: 4px;
    transition: opacity 0.15s ease;
  }
  .m-pill:hover { opacity: 0.85; }
  .m-login-wrap {
    flex: 1;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    justify-content: flex-start;
    gap: 12px;
    padding: 20px 16px 28px;
  }
  .m-login-wrap > .m-card:first-child { margin-top: auto; }
  .m-login-wrap > :last-child { margin-bottom: auto; }
  .m-dl-card {
    background: var(--card);
    border: 0.5px solid var(--sep);
    border-radius: 18px;
    padding: 14px 16px;
    box-shadow: 0 8px 24px rgba(15, 23, 42, 0.06);
    display: flex;
    flex-direction: column;
    gap: 9px;
  }
  .m-dl-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }
  .m-dl-title { font-size: 13.5px; font-weight: 800; color: var(--text); }
  .m-dl-badge {
    font-size: 11px;
    font-weight: 700;
    color: var(--green);
    background: rgba(52, 199, 89, 0.14);
    padding: 3px 8px;
    border-radius: 999px;
    white-space: nowrap;
  }
  .m-dl-desc {
    font-size: 12px;
    line-height: 1.55;
    color: var(--sub);
  }
  .m-dl-btn-main {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 7px;
    text-decoration: none;
    background: rgba(10, 132, 255, 0.12);
    border: 1px solid rgba(10, 132, 255, 0.25);
    color: var(--blue);
    font-size: 13.5px;
    font-weight: 700;
    padding: 10px 14px;
    border-radius: 12px;
    transition: background 0.15s ease;
  }
  .m-dl-btn-main:hover { background: rgba(10, 132, 255, 0.20); }
  .m-dl-subrow {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 8px;
    padding-top: 2px;
  }
  .m-dl-link {
    font-size: 11.5px;
    font-weight: 600;
    color: var(--blue);
    text-decoration: none;
  }
  .m-dl-link:hover { text-decoration: underline; }
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
    -webkit-overflow-scrolling: touch;
    overscroll-behavior: contain;
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
  .m-bubble.is-err {
    border-color: rgba(255, 59, 48, 0.45);
    background: rgba(255, 59, 48, 0.08);
  }
  .m-typing-dots { display: inline-flex; gap: 4px; align-items: center; padding: 3px 2px; }
  .m-typing-dots span {
    width: 6px; height: 6px; border-radius: 50%; background: var(--sub);
    animation: m-dot-bounce 1s infinite ease-in-out;
  }
  .m-typing-dots span:nth-child(2) { animation-delay: 0.16s; }
  .m-typing-dots span:nth-child(3) { animation-delay: 0.32s; }
  @keyframes m-dot-bounce { 0%, 80%, 100% { opacity: 0.35; transform: scale(0.8); } 40% { opacity: 1; transform: scale(1.1); } }
  .m-bubble.thinking {
    max-width: 92%;
    background: linear-gradient(135deg, rgba(10, 132, 255, 0.09), rgba(88, 86, 214, 0.08));
    border: 1px solid rgba(10, 132, 255, 0.25);
    padding: 10px 12px;
    animation: m-think-glow 2.2s ease-in-out infinite;
  }
  @keyframes m-think-glow {
    0%, 100% { border-color: rgba(10, 132, 255, 0.22); }
    50% { border-color: rgba(88, 86, 214, 0.44); }
  }
  .m-think-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .m-think-cat {
    width: 26px; height: 26px; flex-shrink: 0;
    animation: m-cat-hop 1.4s ease-in-out infinite;
  }
  @keyframes m-cat-hop {
    0%, 100% { transform: translateY(0) rotate(0deg); }
    45% { transform: translateY(-3px) rotate(-3deg); }
    75% { transform: translateY(-1px) rotate(3deg); }
  }
  .m-think-title { font-size: 12.5px; font-weight: 700; color: var(--blue); }
  .m-think-dots { display: inline-flex; align-items: center; gap: 4px; }
  .m-think-dots span {
    width: 5.5px; height: 5.5px; border-radius: 50%;
    animation: m-dot-bounce 1.05s infinite ease-in-out;
  }
  .m-think-dots span:nth-child(1) { background: #0A84FF; }
  .m-think-dots span:nth-child(2) { background: #5856D6; animation-delay: 0.16s; }
  .m-think-dots span:nth-child(3) { background: #30B0C7; animation-delay: 0.32s; }
  .m-think-timer {
    margin-left: auto; font-size: 11px; font-weight: 700;
    color: var(--sub); background: rgba(120, 120, 128, 0.12);
    padding: 2px 7px; border-radius: 999px;
  }
  .m-think-steps {
    margin-top: 7px; padding-top: 6px; border-top: 0.5px dashed rgba(10, 132, 255, 0.22);
    display: flex; flex-direction: column; gap: 4px; font-size: 11.5px; color: var(--sub);
  }
  .m-think-step { display: flex; align-items: center; gap: 6px; line-height: 1.38; }
  .m-step-dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }
  .m-step-dot.done { background: var(--green); }
  .m-step-dot.now { background: var(--blue); animation: m-step-pulse 1.1s infinite ease-in-out; }
  .m-step-dot.wait { background: rgba(120, 120, 128, 0.35); }
  @keyframes m-step-pulse { 0%, 100% { opacity: 0.45; transform: scale(0.85); } 50% { opacity: 1; transform: scale(1.2); } }
  .m-thought-box {
    margin-bottom: 7px; border-radius: 10px;
    background: rgba(10, 132, 255, 0.06); border: 0.5px solid rgba(10, 132, 255, 0.18);
    padding: 5px 9px; font-size: 11.5px;
  }
  .m-thought-box summary {
    cursor: pointer; list-style: none; display: flex; align-items: center; gap: 5px;
    font-weight: 700; color: var(--blue); user-select: none; outline: none;
  }
  .m-thought-box summary::-webkit-details-marker { display: none; }
  .m-thought-list {
    margin-top: 5px; padding-top: 5px; border-top: 0.5px dashed rgba(10, 132, 255, 0.2);
    display: flex; flex-direction: column; gap: 3px; color: var(--sub); line-height: 1.4;
  }
  .m-card-toggle {
    border: none; background: rgba(10, 132, 255, 0.1); color: var(--blue);
    font-family: inherit; font-size: 11px; font-weight: 700;
    padding: 3px 9px; border-radius: 999px; cursor: pointer;
  }
  .m-status-compact .m-track,
  .m-status-compact .m-status-times,
  .m-status-compact .m-actions-row { display: none; }
  .m-remember-row {
    display: flex; align-items: center; justify-content: space-between; gap: 8px; font-size: 12px; color: var(--sub);
  }
  .m-remember-row label { display: inline-flex; align-items: center; gap: 6px; cursor: pointer; font-weight: 600; color: var(--text); }
  .m-saved-pills { display: flex; flex-wrap: wrap; gap: 6px; }
  .m-saved-chip {
    display: inline-flex; align-items: center; gap: 6px; padding: 5px 10px; border-radius: 999px;
    border: 1px solid rgba(10, 132, 255, 0.3); background: rgba(10, 132, 255, 0.08); color: var(--blue);
    font-size: 12px; font-weight: 700; cursor: pointer;
  }
  .m-modal-mask {
    position: fixed; inset: 0; background: rgba(15, 23, 42, 0.46); z-index: 950;
    display: none; align-items: center; justify-content: center; padding: 16px;
  }
  .m-modal-mask.show { display: flex; }
  .m-modal-card {
    width: 100%; max-width: 390px; background: var(--card); border: 0.5px solid var(--sep);
    border-radius: 20px; padding: 16px; display: flex; flex-direction: column; gap: 10px;
    box-shadow: 0 20px 48px rgba(15, 23, 42, 0.24);
  }
  .m-acct-item {
    display: flex; align-items: center; justify-content: space-between; gap: 8px;
    padding: 9px 11px; border-radius: 12px; border: 0.5px solid var(--sep); background: rgba(120, 120, 128, 0.07);
  }
  .m-shot-wrap {
    margin: 7px 0 4px;
    border-radius: 11px;
    overflow: hidden;
    border: 1px solid rgba(10, 132, 255, 0.25);
    background: #0F172A;
    cursor: pointer;
  }
  .m-shot-img { display: block; width: 100%; max-height: 220px; object-fit: contain; }
  .m-shot-tip {
    padding: 4px 8px;
    font-size: 11px;
    color: #E2E8F0;
    background: rgba(15, 23, 42, 0.85);
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .m-confirm-card {
    margin-top: 8px;
    padding: 9px 10px;
    border-radius: 12px;
    background: rgba(255, 149, 0, 0.10);
    border: 1px solid rgba(255, 149, 0, 0.36);
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .m-confirm-card.done {
    background: rgba(52, 199, 89, 0.08);
    border-color: rgba(52, 199, 89, 0.30);
  }
  .m-confirm-hdr { font-size: 12px; font-weight: 700; color: var(--orange); }
  .m-confirm-card.done .m-confirm-hdr { color: var(--green); }
  .m-confirm-btns { display: flex; gap: 6px; flex-wrap: wrap; }
  .m-confirm-btn {
    border: 0.5px solid var(--sep);
    background: var(--card);
    color: var(--text);
    font-family: inherit;
    font-size: 12px;
    font-weight: 700;
    padding: 7px 11px;
    border-radius: 9px;
    cursor: pointer;
    flex: 1;
    min-width: 112px;
  }
  .m-confirm-btn.primary { background: var(--blue); color: #FFFFFF; border-color: var(--blue); }
  .m-confirm-btn.danger { background: rgba(255, 59, 48, 0.14); color: var(--red); border-color: rgba(255, 59, 48, 0.3); }
  .m-confirm-btn:disabled { opacity: 0.55; cursor: not-allowed; }
  .m-confirm-chips { display: flex; flex-wrap: wrap; gap: 5px; }
  .m-confirm-chip {
    border: 0.5px solid rgba(10, 132, 255, 0.34);
    background: rgba(10, 132, 255, 0.09);
    color: var(--blue);
    font-family: inherit;
    font-size: 11.5px;
    font-weight: 600;
    padding: 4px 9px;
    border-radius: 999px;
    cursor: pointer;
  }
  .m-confirm-input-row { display: flex; gap: 6px; align-items: center; }
  .m-confirm-input-row input {
    flex: 1;
    min-width: 0;
    border: 1px solid var(--sep);
    background: var(--card);
    color: var(--text);
    border-radius: 9px;
    padding: 6px 9px;
    font-size: 13px;
    font-family: inherit;
    outline: none;
  }
  .m-confirm-done { font-size: 12px; font-weight: 700; color: var(--green); }
  .m-confirm-alert-bar {
    display: none;
    margin: 0 12px 6px;
    padding: 8px 11px;
    border-radius: 11px;
    background: rgba(255, 149, 0, 0.16);
    border: 1px solid rgba(255, 149, 0, 0.44);
    color: var(--orange);
    font-size: 12px;
    font-weight: 700;
    cursor: pointer;
    flex-shrink: 0;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
  }
  .m-confirm-alert-bar.show { display: flex; }
  .m-zoom-mask {
    position: fixed; inset: 0; background: rgba(6, 10, 18, 0.90); z-index: 980;
    display: none; flex-direction: column; align-items: center; justify-content: center; padding: 14px; gap: 10px;
  }
  .m-zoom-mask.show { display: flex; }
  .m-zoom-img { max-width: 96vw; max-height: 82vh; border-radius: 12px; object-fit: contain; background: #0F172A; }
  .m-zoom-tip { color: #E2E8F0; font-size: 12px; }
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
      <div class="m-cat-ico" style="background:linear-gradient(135deg,#58CC02,#2EAD00);box-shadow:0 6px 16px rgba(88,204,2,0.28);">
        <svg viewBox="0 0 96 96" width="32" height="32" fill="none">
          <path d="M24 31 L20 15 L36 25 Z" fill="#89E219"/>
          <path d="M72 31 L76 15 L60 25 Z" fill="#89E219"/>
          <ellipse cx="48" cy="54" rx="29" ry="27" fill="#58CC02"/>
          <circle cx="36" cy="46" r="13" fill="#FFFFFF"/>
          <circle cx="60" cy="46" r="13" fill="#FFFFFF"/>
          <circle cx="37.5" cy="46.5" r="7" fill="#1E293B"/>
          <circle cx="58.5" cy="46.5" r="7" fill="#1E293B"/>
          <circle cx="35" cy="43.5" r="2.6" fill="#FFFFFF"/>
          <circle cx="56" cy="43.5" r="2.6" fill="#FFFFFF"/>
          <path d="M42 53 Q48 50 54 53 Q50 62 48 62 Q46 62 42 53 Z" fill="#FFB800"/>
        </svg>
      </div>
      <div style="min-width:0;">
        <div class="m-title">暗号表助手 · 云端远程</div>
        <div class="m-sub" id="mHeaderSub">无论身在何处，云端连上电脑就能查数据、聊工作、遥控跑表</div>
      </div>
    </div>
    <div class="m-hdr-actions">
      <a class="m-pill" href="https://github.com/Sorenskr/SheetMate/releases/latest/download/SheetMate.exe" target="_blank" rel="noopener noreferrer" title="下载 Windows 电脑端客户端">下载电脑版</a>
      <button type="button" class="m-pill" id="mSwitchUserBtn" style="display:none;">切换账号</button>
      <button type="button" class="m-pill" id="mLogoutBtn" style="display:none;background:rgba(255,59,48,0.12);color:var(--red);">退出</button>
    </div>
  </header>

  <div class="m-login-wrap" id="mLoginGate">
    <div class="m-card">
      <div class="m-intro-box">
        <div class="m-intro-title">云端异地连电脑</div>
        <div>
          无论您在家、出差还是用手机流量，只要填上在电脑右上角起的<strong>名字</strong>和<strong>远程密码</strong>，就能直接从云端连回电脑——随时问数据、遥控自动跑表发钉钉，两边的聊天记录会实时同步。
        </div>
      </div>

      <div id="mSavedAccountsWrap" style="display:none;flex-direction:column;gap:6px;">
        <div class="m-label" style="margin-bottom:0;">已记住账号（点击一键直连）</div>
        <div class="m-saved-pills" id="mSavedAccountsBar"></div>
      </div>

      <div class="m-label">1. 电脑上起好的名字</div>
      <input type="text" class="m-input" id="mLoginNameInput" placeholder="填电脑右上角设置的名字" autocomplete="username" />

      <div class="m-label">2. 远程密码</div>
      <input type="password" class="m-input" id="mLoginPinInput" placeholder="填电脑【远程网站】里设置的密码" autocomplete="current-password" />

      <div class="m-label">3. 专属连接链接（选填，留空默认云端直连）</div>
      <input type="url" class="m-input" id="mServerBaseInput" placeholder="一般留空即可；局域网/临时链接可贴这里" autocomplete="url" />

      <div class="m-remember-row">
        <label><input type="checkbox" id="mRememberCheck" checked /> 记住账号密码，下次打开自动登录</label>
        <button type="button" id="mClearSavedBtn" style="border:none;background:none;color:var(--sub);font-size:11.5px;cursor:pointer;display:none;">清除已存</button>
      </div>

      <div id="mLoginErr" style="font-size:12.5px;color:var(--red);display:none;line-height:1.45;"></div>
      <button type="button" class="m-btn-primary" id="mLoginSubmitBtn">云端连接我的电脑</button>
    </div>

    <div class="m-dl-card" id="mDownloadCard">
      <div class="m-dl-head">
        <div class="m-dl-title">电脑还没装软件？免费下载客户端</div>
        <span class="m-dl-badge">v2.9.7 纯净单文件版</span>
      </div>
      <div class="m-dl-desc">
        下载这单个 SheetMate.exe 双击直接运行（绝不生成杂乱文件夹、无需安装环境、关闭窗口直接结束退出），支持自动抓数写 WPS 表、钉钉静音发群与云端异地遥控。
      </div>
      <a class="m-dl-btn-main" href="https://github.com/Sorenskr/SheetMate/releases/latest/download/SheetMate.exe" target="_blank" rel="noopener noreferrer">
        <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
          <polyline points="7 10 12 15 17 10"></polyline>
          <line x1="12" y1="15" x2="12" y2="3"></line>
        </svg>
        <span>直接下载电脑客户端 (SheetMate.exe)</span>
      </a>
      <div class="m-dl-subrow">
        <span style="font-size:11.5px;color:var(--sub);">支持 Windows 10 / 11 (x64)</span>
        <a class="m-dl-link" href="https://github.com/Sorenskr/SheetMate/releases/latest" target="_blank" rel="noopener noreferrer">查看 GitHub 发行说明</a>
      </div>
    </div>
  </div>

  <div class="m-main" id="mMainApp">
    <div class="m-status-card" id="mStatusCard">
      <div class="m-st-row">
        <div style="display:flex;align-items:center;gap:7px;min-width:0;">
          <span class="m-st-dot" id="mStatusDot"></span>
          <span style="font-size:13px;font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" id="mStepText">正在同步电脑状态…</span>
        </div>
        <div style="display:flex;align-items:center;gap:6px;flex-shrink:0;">
          <span style="font-size:11.5px;font-weight:700;color:var(--blue);white-space:nowrap;" id="mBriefPill">实时在线</span>
          <button type="button" class="m-card-toggle" id="mToggleCardBtn">收起</button>
        </div>
      </div>
      <div class="m-track"><div class="m-fill" id="mProgFill"></div></div>
      <div class="m-status-times" style="display:flex;justify-content:space-between;font-size:11px;color:var(--sub);">
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
    <div class="m-confirm-alert-bar" id="mConfirmAlertBar">
      <span id="mConfirmAlertText">电脑正等待您看图确认钉钉发送对象</span>
      <span style="text-decoration:underline;flex-shrink:0;">去确认</span>
    </div>

    <div class="m-quick-bar" id="mQuickChips">
      <button type="button" class="m-chip" data-mp="暂停运行">暂停运行</button>
      <button type="button" class="m-chip" data-mp="继续运行">继续运行</button>
      <button type="button" class="m-chip" data-mp="取消当前任务">取消当前任务</button>
      <button type="button" class="m-chip" data-mp="我要看昨天的数据，发给我">看昨天数据发给我</button>
      <button type="button" class="m-chip" data-mp="帮我跑一下程序，最新数据发给我">跑程序发给我</button>
      <button type="button" class="m-chip" data-mp="请按简报总结一下当前最新数据变化">总结当前数据</button>
      <button type="button" class="m-chip" data-mp="汇报一下电脑现在的工作状态与下次计划">查电脑状态</button>
      <button type="button" class="m-chip" data-mtpl="帮我在定时计划里新增一个每天 14:30 发到「目标群名」，发日期+时间，内容写「午间数据情况」，非静音">加定时：某点发某群</button>
      <button type="button" class="m-chip" data-mtpl="帮我在定时计划里新增一个每天 09:00 发昨天数据到「目标群名」，只发年月日日期，内容写「昨日数据情况」">加定时：发昨天日期</button>
    </div>

    <div class="m-feed" id="mChatFeed"></div>

    <div class="m-composer">
      <input type="text" id="mChatInput" placeholder="跟电脑说：暂停运行 / 取消当前任务 / 看昨天数据发给我…" />
      <button type="button" class="m-send-btn" id="mSendBtn">发送</button>
    </div>
  </div>
</div>
<div class="m-modal-mask" id="mSwitchModalMask">
  <div class="m-modal-card">
    <div style="display:flex;align-items:center;justify-content:space-between;">
      <div style="font-size:15px;font-weight:800;">切换账号 / 切换用户</div>
      <button type="button" class="m-pill" id="mSwitchCloseBtn">关闭</button>
    </div>
    <div style="font-size:12px;color:var(--sub);">选择已记住的电脑账号一键秒切，或新登录其他成员：</div>
    <div id="mSwitchAcctList" style="display:flex;flex-direction:column;gap:7px;max-height:240px;overflow-y:auto;"></div>
    <button type="button" class="m-btn-primary" id="mSwitchAddNewBtn" style="padding:10px;font-size:13.5px;">＋ 登录其他账号 / 换一台电脑</button>
  </div>
</div>
<div class="m-zoom-mask" id="mImgZoomMask">
  <img class="m-zoom-img" id="mImgZoomPreview" src="" alt="钉钉现场截图" />
  <div class="m-zoom-tip">点击画面或遮罩即可关闭放大视图</div>
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
    const defaultSeenNonces = new Map();
    function decryptPacket(masterKey, rawStr, seenMap = defaultSeenNonces) {
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
      const now = Math.floor(Date.now() / 1000);
      const nHex = toHex(nonce);
      if (seenMap instanceof Map) {
        for (const [k, exp] of seenMap.entries()) {
          if (exp <= now) seenMap.delete(k);
        }
        const ex = seenMap.get(nHex);
        if (ex && ex > now) return null;
      }
      const ks = keystream(masterKey, nonce, ct.length);
      const pt = new Uint8Array(ct.length);
      for (let i = 0; i < ct.length; i++) pt[i] = ct[i] ^ ks[i];
      try {
        const obj = JSON.parse(td.decode(pt));
        if (!obj || typeof obj !== "object") return null;
        if (Math.abs(now - (Number(obj.ts) || 0)) > 180) return null;
        if (seenMap instanceof Map) {
          seenMap.set(nHex, now + 180);
          while (seenMap.size > 2048) {
            const firstKey = seenMap.keys().next().value;
            seenMap.delete(firstKey);
          }
        }
        return obj;
      } catch (_e) {
        return null;
      }
    }
    return { deriveRoomAndKey, encryptPacket, decryptPacket, normalizeNickKey };
  })();
  if (typeof globalThis !== "undefined") globalThis.SMCrypto = SMCrypto;

  const SMRelay = (function() {
    const WS_BROKERS = [
      "wss://broker-cn.emqx.io:8084/mqtt",
      "wss://broker.emqx.io:8084/mqtt"
    ];
    const te = new TextEncoder();
    const td = new TextDecoder();
    let activeDownTopic = "";
    let onMessageCb = null;
    let manualClosed = true;
    let conns = [];
    let waitResolvers = [];

    function encVarLen(n) {
      const out = [];
      let v = n >>> 0;
      do {
        let b = v % 128;
        v = Math.floor(v / 128);
        if (v > 0) b |= 0x80;
        out.push(b);
      } while (v > 0);
      return out;
    }
    function strBytes(s) {
      const b = te.encode(String(s || ""));
      return new Uint8Array([(b.length >> 8) & 0xff, b.length & 0xff, ...b]);
    }
    function joinU8(arrs) {
      let tot = 0;
      for (const a of arrs) tot += a.length;
      const res = new Uint8Array(tot);
      let off = 0;
      for (const a of arrs) { res.set(a, off); off += a.length; }
      return res;
    }
    function buildConnect(cid) {
      const vh = joinU8([strBytes("MQTT"), new Uint8Array([4, 2, 0, 45])]);
      const pl = strBytes(cid);
      const lenB = new Uint8Array(encVarLen(vh.length + pl.length));
      return joinU8([new Uint8Array([0x10]), lenB, vh, pl]);
    }
    function buildSubscribe(pid, topic) {
      const vh = new Uint8Array([(pid >> 8) & 0xff, pid & 0xff]);
      const pl = joinU8([strBytes(topic), new Uint8Array([0])]);
      const lenB = new Uint8Array(encVarLen(vh.length + pl.length));
      return joinU8([new Uint8Array([0x82]), lenB, vh, pl]);
    }
    function buildPublish(topic, payloadStr) {
      const vh = strBytes(topic);
      const pl = te.encode(String(payloadStr || ""));
      const lenB = new Uint8Array(encVarLen(vh.length + pl.length));
      return joinU8([new Uint8Array([0x30]), lenB, vh, pl]);
    }
    function notifyReady() {
      if (!waitResolvers.length) return;
      const list = waitResolvers.splice(0, waitResolvers.length);
      for (const fn of list) fn(true);
    }
    function drainRx(entry) {
      while (entry.rxBuf.length >= 2) {
        const hdr = entry.rxBuf[0];
        let pos = 1;
        let mult = 1;
        let rem = 0;
        let doneLen = false;
        while (pos < entry.rxBuf.length && pos <= 4) {
          const b = entry.rxBuf[pos++];
          rem += (b & 0x7f) * mult;
          if ((b & 0x80) === 0) { doneLen = true; break; }
          mult *= 128;
        }
        if (!doneLen) {
          if (pos > 4) {
            try { entry.ws.close(); } catch (_e) {}
          }
          return;
        }
        if (entry.rxBuf.length < pos + rem) return;
        const body = entry.rxBuf.slice(pos, pos + rem);
        entry.rxBuf = entry.rxBuf.slice(pos + rem);
        const ptype = hdr & 0xf0;
        if (ptype === 0x20) {
          if (body.length >= 2 && body[1] === 0 && activeDownTopic) {
            try { entry.ws.send(buildSubscribe(1, activeDownTopic)); } catch (_e) {}
          } else {
            entry.ready = false;
            try { entry.ws.close(); } catch (_e2) {}
            return;
          }
        } else if (ptype === 0x90) {
          entry.ready = true;
          notifyReady();
        } else if (ptype === 0x30 && body.length >= 2) {
          const tlen = (body[0] << 8) | body[1];
          let pOff = 2 + tlen;
          if ((hdr & 0x06) !== 0) pOff += 2;
          if (pOff <= body.length && onMessageCb) {
            try { onMessageCb(td.decode(body.slice(pOff))); } catch (_e) {}
          }
        }
      }
    }
    function spawnBroker(url, topicSnap) {
      const entry = {
        url,
        ws: null,
        ready: false,
        rxBuf: new Uint8Array(0),
        pingTm: null,
        reconTm: null,
      };
      function connectOne() {
        if (manualClosed || activeDownTopic !== topicSnap) return;
        if (typeof WebSocket === "undefined") return;
        let ws;
        try {
          ws = new WebSocket(url, "mqtt");
        } catch (_e) {
          entry.reconTm = setTimeout(connectOne, 2500);
          return;
        }
        ws.binaryType = "arraybuffer";
        entry.ws = ws;
        entry.ready = false;
        entry.rxBuf = new Uint8Array(0);
        ws.onopen = () => {
          if (manualClosed || activeDownTopic !== topicSnap) {
            try { ws.close(); } catch (_e) {}
            return;
          }
          const cid = "smweb_" + Math.random().toString(36).slice(2, 9) + "_" + Date.now().toString(36);
          try { ws.send(buildConnect(cid)); } catch (_e) {}
          clearInterval(entry.pingTm);
          entry.pingTm = setInterval(() => {
            if (ws.readyState === 1 && entry.ready) {
              try { ws.send(new Uint8Array([0xc0, 0x00])); } catch (_e) {}
            }
          }, 22000);
        };
        ws.onmessage = (ev) => {
          try {
            const chunk = ev.data instanceof ArrayBuffer
              ? new Uint8Array(ev.data)
              : (ev.data ? te.encode(String(ev.data)) : new Uint8Array(0));
            if (!chunk.length) return;
            if (entry.rxBuf.length + chunk.length > 524288) {
              entry.rxBuf = new Uint8Array(0);
              try { ws.close(); } catch (_e2) {}
              return;
            }
            entry.rxBuf = joinU8([entry.rxBuf, chunk]);
            drainRx(entry);
          } catch (_e) {}
        };
        ws.onerror = () => {};
        ws.onclose = () => {
          entry.ready = false;
          clearInterval(entry.pingTm);
          if (!manualClosed && activeDownTopic === topicSnap) {
            clearTimeout(entry.reconTm);
            entry.reconTm = setTimeout(connectOne, 2200);
          }
        };
      }
      connectOne();
      return entry;
    }
    function closeAll() {
      manualClosed = true;
      activeDownTopic = "";
      for (const e of conns) {
        clearInterval(e.pingTm);
        clearTimeout(e.reconTm);
        e.ready = false;
        if (e.ws) {
          try {
            if (e.ws.readyState === 1) e.ws.send(new Uint8Array([0xe0, 0x00]));
            e.ws.close();
          } catch (_e) {}
        }
      }
      conns = [];
      const list = waitResolvers.splice(0, waitResolvers.length);
      for (const fn of list) fn(false);
    }
    function hasReady() {
      return conns.some((e) => e.ready && e.ws && e.ws.readyState === 1);
    }
    function hasConnectingOrReady() {
      return conns.some((e) => e.ws && (e.ws.readyState === 0 || (e.ready && e.ws.readyState === 1)));
    }
    async function connectAndSub(downTopic, cb, timeoutMs = 5500) {
      if (cb) onMessageCb = cb;
      if (!manualClosed && activeDownTopic === downTopic && hasReady()) return true;
      closeAll();
      manualClosed = false;
      activeDownTopic = downTopic;
      conns = WS_BROKERS.map((u) => spawnBroker(u, downTopic));
      const firstReady = await new Promise((resolve) => {
        if (hasReady()) { resolve(true); return; }
        const tm = setTimeout(() => {
          const idx = waitResolvers.indexOf(onReady);
          if (idx >= 0) waitResolvers.splice(idx, 1);
          resolve(hasReady());
        }, timeoutMs);
        function onReady(ok) {
          clearTimeout(tm);
          resolve(ok);
        }
        waitResolvers.push(onReady);
      });
      return hasReady();
    }
    function publish(upTopic, packetStr) {
      const frame = buildPublish(upTopic, packetStr);
      let sent = 0;
      for (const e of conns) {
        if (e.ready && e.ws && e.ws.readyState === 1) {
          try { e.ws.send(frame); sent += 1; } catch (_e) {}
        }
      }
      return sent;
    }
    return { connectAndSub, publish, closeAll, hasReady, hasConnectingOrReady, WS_BROKERS };
  })();
  if (typeof globalThis !== "undefined") globalThis.SMRelay = SMRelay;
  if (typeof window === "undefined" || typeof document === "undefined") return;

  const qs = new URLSearchParams(window.location.search);
  const $ = (id) => document.getElementById(id);
  const LS_CREDS_KEY = "sm_saved_cred_v2";
  const LS_ACCTS_KEY = "sm_saved_accounts_v2";
  const LS_AUTO_KEY = "sm_auto_login";
  const LS_CACHE_STATE_KEY = "sm_cached_state_v2";

  function safeStorageGet(k, defVal = "") {
    try { const v = localStorage.getItem(k); return v === null ? defVal : v; } catch (_e) { return defVal; }
  }
  function safeStorageSet(k, v) {
    try { localStorage.setItem(k, String(v)); } catch (_e) {}
  }
  function safeStorageRemove(k) {
    try { localStorage.removeItem(k); } catch (_e) {}
  }
  function safeJsonParse(raw, fallback) {
    try { return raw ? JSON.parse(raw) : fallback; } catch (_e) { return fallback; }
  }
  function loadSavedAccounts() {
    const arr = safeJsonParse(safeStorageGet(LS_ACCTS_KEY, ""), []);
    return Array.isArray(arr) ? arr.filter((x) => x && x.nick && x.pin) : [];
  }
  function saveAccountRecord(rec) {
    if (!rec || !rec.nick || !rec.pin) return;
    const key = SMCrypto.normalizeNickKey(rec.nick);
    const list = loadSavedAccounts().filter((x) => SMCrypto.normalizeNickKey(x.nick) !== key);
    list.unshift({
      nick: String(rec.nick).trim(),
      pin: String(rec.pin).trim(),
      serverBase: String(rec.serverBase || "").trim(),
      connMode: String(rec.connMode || "cloud"),
      updatedAt: Date.now(),
    });
    if (list.length > 8) list.length = 8;
    safeStorageSet(LS_ACCTS_KEY, JSON.stringify(list));
    safeStorageSet(LS_CREDS_KEY, JSON.stringify(list[0]));
  }
  function removeAccountRecord(nick) {
    const key = SMCrypto.normalizeNickKey(nick);
    const list = loadSavedAccounts().filter((x) => SMCrypto.normalizeNickKey(x.nick) !== key);
    safeStorageSet(LS_ACCTS_KEY, JSON.stringify(list));
    const cur = safeJsonParse(safeStorageGet(LS_CREDS_KEY, ""), null);
    if (cur && SMCrypto.normalizeNickKey(cur.nick) === key) {
      if (list.length) safeStorageSet(LS_CREDS_KEY, JSON.stringify(list[0]));
      else safeStorageRemove(LS_CREDS_KEY);
    }
  }

  let savedCred = safeJsonParse(safeStorageGet(LS_CREDS_KEY, ""), null);
  let mobileToken = safeStorageGet("sm_mobile_token", "") || "";
  let myNick = (savedCred && savedCred.nick) || safeStorageGet("sm_mobile_nick", "") || qs.get("name") || "";
  let myPin = (savedCred && savedCred.pin) || "";
  let serverBase = (savedCred && savedCred.serverBase) || safeStorageGet("sm_mobile_base", "") || qs.get("hub") || "";
  let connMode = (savedCred && savedCred.connMode) || safeStorageGet("sm_conn_mode", "") || "";
  let relayCred = null; // { nick, pin, upTopic, downTopic, masterKey }
  const pendingReqs = new Map();
  let lastChatSeq = -1;
  let lastPaused = false;
  let pollTimer = null;
  let delayedPollTm = null;
  let pollInFlight = false;
  let loginGen = 0;
  let cachedChatHistory = [];
  let pendingUserMsgs = [];
  function scheduleDelayedPoll(ms = 220) {
    if (delayedPollTm) clearTimeout(delayedPollTm);
    delayedPollTm = setTimeout(() => {
      delayedPollTm = null;
      doPoll();
    }, ms);
  }
  function clearPollTimers() {
    clearInterval(pollTimer);
    pollTimer = null;
    if (delayedPollTm) {
      clearTimeout(delayedPollTm);
      delayedPollTm = null;
    }
  }
  function restartPollTimer() {
    clearInterval(pollTimer);
    pollTimer = null;
    if (connMode === "cloud" && relayCred) {
      pollTimer = setInterval(doPoll, 5000);
    } else if (mobileToken) {
      pollTimer = setInterval(doPoll, 1200);
    }
  }

  if ($("mLoginNameInput") && myNick) $("mLoginNameInput").value = myNick;
  if ($("mLoginPinInput") && myPin) $("mLoginPinInput").value = myPin;
  if ($("mServerBaseInput") && serverBase) $("mServerBaseInput").value = serverBase;

  function showToast(msg) {
    const t = $("mToast");
    t.textContent = msg;
    t.style.display = "block";
    clearTimeout(t._tm);
    t._tm = setTimeout(() => { t.style.display = "none"; }, 2600);
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

  async function mApi(path, method = "GET", body = null, customBase = null, timeoutMs = 6000) {
    const headers = { "Content-Type": "application/json" };
    if (mobileToken && customBase === null) headers["X-Mobile-Token"] = mobileToken;
    const opt = { method, headers };
    if (body !== null) opt.body = JSON.stringify(body);
    let tm = null;
    if (typeof AbortController !== "undefined" && timeoutMs > 0) {
      const ac = new AbortController();
      opt.signal = ac.signal;
      tm = setTimeout(() => ac.abort(), timeoutMs);
    }
    try {
      const root = customBase !== null ? customBase : baseUrl();
      const resp = await fetch(root + path, opt);
      return await resp.json();
    } finally {
      if (tm) clearTimeout(tm);
    }
  }

  function buildMobileThinkingCatSvg() {
    return `<svg class="m-think-cat" viewBox="0 0 36 36" fill="none"><path d="M7 15 L5 5 L13.5 11 L22.5 11 L31 5 L29 15 A11 10 0 0 1 7 15 Z" fill="#4F46E5" stroke="#38BDF8" stroke-width="1.3" stroke-linejoin="round"/><rect x="9" y="14" width="18" height="12" rx="5.5" fill="#0F172A"/><circle cx="14" cy="19.5" r="2" fill="#38BDF8"/><circle cx="22" cy="19.5" r="2" fill="#38BDF8"/><circle cx="14.7" cy="18.8" r="0.7" fill="#FFFFFF"/><circle cx="22.7" cy="18.8" r="0.7" fill="#FFFFFF"/><path d="M15.3 23.2 Q18 24.8 20.7 23.2" stroke="#F8FAFC" stroke-width="1.4" stroke-linecap="round"/></svg>`;
  }

  function formatMsgText(raw) {
    return String(raw || "").replace(/\*\*([^*]+)\*\*/g, "$1").replace(/\*\*([^*:\n]+)\*\s*:/g, "$1:");
  }

  function getMobileThinkingStages(promptText) {
    const q = String(promptText || "").toLowerCase();
    const isData = !q || /(表|图|截图|汇总|数据|团队|小组|排行|排名|注册|转化|华人|暗号|对比|增量|总结|简报|昨天|今天|本周)/.test(q);
    if (isData) {
      return [
        "正在连接电脑，理解分析意图",
        "正在读取汇总表最新截图与历史数据档案",
        "正在生成数据解读与趋势要点",
      ];
    }
    return [
      "正在将指令加密送达电脑端",
      "正在匹配快捷动作或检索本地配置知识库",
      "正在组织清晰答复并同步返回",
    ];
  }

  function buildMobileThinkingNode(promptText, elapsedSec) {
    const box = document.createElement("div");
    box.className = "m-bubble model thinking m-think-bubble";
    box.id = "mActiveThinkBubble";
    const stages = getMobileThinkingStages(promptText);
    const activeIdx = elapsedSec >= 5 ? 2 : (elapsedSec >= 2 ? 1 : 0);
    const stepsHtml = stages.map((label, idx) => {
      const cls = idx < activeIdx ? "done" : (idx === activeIdx ? "now" : "wait");
      return `<div class="m-think-step" data-idx="${idx}"><span class="m-step-dot ${cls}"></span><span>${escapeHtml(label)}</span></div>`;
    }).join("");
    box.innerHTML = `<div class="m-think-head">${buildMobileThinkingCatSvg()}<span class="m-think-title">小猫正在认真思考中</span><span class="m-think-dots"><span></span><span></span><span></span></span><span class="m-think-timer" id="mThinkTimerVal">已思考 ${elapsedSec}s</span></div><div class="m-think-steps">${stepsHtml}</div>`;
    return box;
  }

  function escapeHtml(s) {
    return String(s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function safeShotImageUrl(rawUrl) {
    const s = String(rawUrl || "").trim();
    if (s.length > 240000) return "";
    return /^data:image\/(?:png|jpeg|jpg|webp);base64,[A-Za-z0-9+/=]{20,}$/i.test(s) ? s : "";
  }

  async function sendMobileTargetConfirm(pendingId, act, newTarget = "", onFailUnlock = null) {
    if (!pendingId) return;
    const cleanTgt = String(newTarget || "").trim();
    if (act === "switch_target" && !cleanTgt) {
      showToast("请先选择或输入要改发的群名或联系人");
      if (typeof onFailUnlock === "function") onFailUnlock();
      return;
    }
    try {
      if (connMode === "cloud" && relayCred) {
        const res = await sendRelayCommand("confirm_target", {
          pending_id: pendingId,
          action: act,
          new_target: cleanTgt,
        }, 12000);
        const r = res && res.result;
        showToast((r && r.message) || "已提交您的确认选择");
        if (r && r.ok === false && typeof onFailUnlock === "function") onFailUnlock();
        if (res && res.state) applyPcState(res.state);
        else if (r && Array.isArray(r.history)) {
          lastChatSeq = typeof r.chat_seq === "number" ? r.chat_seq : lastChatSeq + 1;
          renderChats(r.history);
        }
      } else {
        const r = await mApi("/api/mobile/confirm-target", "POST", {
          pending_id: pendingId,
          action: act,
          new_target: cleanTgt,
        });
        showToast((r && r.message) || "已提交您的确认选择");
        if (r && r.ok === false && typeof onFailUnlock === "function") onFailUnlock();
        if (r && Array.isArray(r.history)) {
          lastChatSeq = typeof r.chat_seq === "number" ? r.chat_seq : lastChatSeq + 1;
          renderChats(r.history);
        }
        scheduleDelayedPoll(200);
      }
    } catch (_e) {
      if (typeof onFailUnlock === "function") onFailUnlock();
      showToast("提交确认失败，请稍后重试或直接在聊天框回复");
    }
  }

  function buildBubbleNode(it) {
    const div = document.createElement("div");
    const isUser = it.role === "user";
    const isSpot = it.source === "data_spotlight";
    const isConfirm = it.source === "target_confirm";
    const txt = formatMsgText(it.text || "");
    const isErr = !isUser && !isConfirm && (txt.startsWith("\u274c") || it.source === "on_demand_not_found" || txt.includes("发送失败") || txt.includes("未找到"));
    div.className = `m-bubble ${isUser ? "user" : "model"}${isSpot ? " is-spot" : ""}${isErr ? " is-err" : ""}`;
    if (!isUser && Array.isArray(it.thinking_steps) && it.thinking_steps.length) {
      const det = document.createElement("details");
      det.className = "m-thought-box";
      const durTxt = (typeof it.elapsed_sec === "number") ? ` · 用时 ${it.elapsed_sec}s` : "";
      const listHtml = it.thinking_steps.map((s, idx) => `<div>${idx + 1}. ${escapeHtml(s)}</div>`).join("");
      det.innerHTML = `<summary>思考过程（共 ${it.thinking_steps.length} 步${escapeHtml(durTxt)}，点击查看）</summary><div class="m-thought-list">${listHtml}</div>`;
      div.appendChild(det);
    }
    const body = document.createElement("div");
    body.textContent = txt;
    div.appendChild(body);

    const safeImg = !isUser ? safeShotImageUrl(it.image_data_url) : "";
    if (safeImg) {
      const wrap = document.createElement("div");
      wrap.className = "m-shot-wrap";
      const img = document.createElement("img");
      img.className = "m-shot-img";
      img.src = safeImg;
      img.alt = "电脑钉钉现场截图";
      const tip = document.createElement("div");
      tip.className = "m-shot-tip";
      tip.textContent = "电脑钉钉当前界面（点击放大查看清晰群名）";
      wrap.appendChild(img);
      wrap.appendChild(tip);
      wrap.onclick = () => {
        if ($("mImgZoomPreview") && $("mImgZoomMask")) {
          $("mImgZoomPreview").src = safeImg;
          $("mImgZoomMask").classList.add("show");
        }
      };
      div.appendChild(wrap);
    }

    const cc = (!isUser && it.confirm_card && typeof it.confirm_card === "object") ? it.confirm_card : null;
    if (cc) {
      const isPend = cc.status === "pending";
      const card = document.createElement("div");
      card.className = "m-confirm-card" + (isPend ? "" : " done");
      const hdr = document.createElement("div");
      hdr.className = "m-confirm-hdr";
      hdr.textContent = isPend
        ? `待您确认：预设「${cc.target_name || ""}」 / 桌面看到「${cc.opened_name || ""}」`
        : (cc.resolved_text || "已处理");
      card.appendChild(hdr);

      if (isPend) {
        const btns = document.createElement("div");
        btns.className = "m-confirm-btns";
        const okBtn = document.createElement("button");
        okBtn.type = "button";
        okBtn.className = "m-confirm-btn primary";
        okBtn.textContent = "确认仍发当前会话";
        const cancelBtn = document.createElement("button");
        cancelBtn.type = "button";
        cancelBtn.className = "m-confirm-btn danger";
        cancelBtn.textContent = "取消发送";

        const inpRow = document.createElement("div");
        inpRow.className = "m-confirm-input-row";
        const inp = document.createElement("input");
        inp.type = "text";
        inp.placeholder = "输入其他群名或联系人后点改发";
        const swBtn = document.createElement("button");
        swBtn.type = "button";
        swBtn.className = "m-confirm-btn";
        swBtn.style.flex = "0 0 auto";
        swBtn.textContent = "改发此对象";

        const setBtnsBusy = (busyFlag) => {
          okBtn.disabled = busyFlag;
          cancelBtn.disabled = busyFlag;
          swBtn.disabled = busyFlag;
        };
        okBtn.onclick = () => { setBtnsBusy(true); sendMobileTargetConfirm(cc.pending_id, "confirm_send", "", () => setBtnsBusy(false)); };
        cancelBtn.onclick = () => { setBtnsBusy(true); sendMobileTargetConfirm(cc.pending_id, "cancel_send", "", () => setBtnsBusy(false)); };
        swBtn.onclick = () => {
          const val = inp.value.trim();
          if (!val) { showToast("请先输入或点选一个发送对象"); inp.focus(); return; }
          setBtnsBusy(true);
          sendMobileTargetConfirm(cc.pending_id, "switch_target", val, () => setBtnsBusy(false));
        };
        btns.appendChild(okBtn);
        btns.appendChild(cancelBtn);
        card.appendChild(btns);

        const cands = Array.isArray(cc.candidates) ? cc.candidates.filter(Boolean) : [];
        if (cands.length) {
          const chipBar = document.createElement("div");
          chipBar.className = "m-confirm-chips";
          for (const cname of cands) {
            const cb = document.createElement("button");
            cb.type = "button";
            cb.className = "m-confirm-chip";
            cb.textContent = `改发：${cname}`;
            cb.onclick = () => {
              inp.value = cname;
              inp.focus();
            };
            chipBar.appendChild(cb);
          }
          card.appendChild(chipBar);
        }
        inpRow.appendChild(inp);
        inpRow.appendChild(swBtn);
        card.appendChild(inpRow);
      } else {
        const doneEl = document.createElement("div");
        doneEl.className = "m-confirm-done";
        doneEl.textContent = cc.resolved_text || "已处理";
        card.appendChild(doneEl);
      }
      div.appendChild(card);
    }

    const meta = document.createElement("div");
    meta.className = "m-b-meta";
    const viaTag = isUser ? (it.via === "mobile" ? "远程发出" : "电脑发出") : "智能管家";
    meta.textContent = [it.time, viaTag, it.shot_info].filter(Boolean).join(" · ");
    div.appendChild(meta);
    return div;
  }

  let mAiThinking = { active: false, prompt: "", startTs: 0, timerId: null };
  function startMobileThinking(promptText) {
    if (mAiThinking.timerId) clearInterval(mAiThinking.timerId);
    mAiThinking = { active: true, prompt: String(promptText || ""), startTs: Date.now(), timerId: null };
    mAiThinking.timerId = setInterval(() => {
      if (!mAiThinking.active) return;
      const elapsed = Math.max(0, Math.floor((Date.now() - mAiThinking.startTs) / 1000));
      const tEl = $("mThinkTimerVal");
      if (tEl) tEl.textContent = `已思考 ${elapsed}s`;
      const bubble = $("mActiveThinkBubble");
      if (bubble) {
        const activeIdx = elapsed >= 5 ? 2 : (elapsed >= 2 ? 1 : 0);
        for (const row of bubble.querySelectorAll(".m-think-step")) {
          const idx = Number(row.dataset.idx);
          const dot = row.querySelector(".m-step-dot");
          if (dot) dot.className = "m-step-dot " + (idx < activeIdx ? "done" : (idx === activeIdx ? "now" : "wait"));
        }
      }
    }, 1000);
  }
  function stopMobileThinking() {
    mAiThinking.active = false;
    if (mAiThinking.timerId) {
      clearInterval(mAiThinking.timerId);
      mAiThinking.timerId = null;
    }
  }

  let _chatRenderRaf = 0;
  function renderChats(list) {
    cachedChatHistory = Array.isArray(list) ? list : [];
    if (_chatRenderRaf) cancelAnimationFrame(_chatRenderRaf);
    const draw = () => {
      _chatRenderRaf = 0;
      const feed = $("mChatFeed");
      const items = cachedChatHistory;
      const frag = document.createDocumentFragment();
      if (!items.length && !pendingUserMsgs.length && !mAiThinking.active) {
        const welcome = document.createElement("div");
        welcome.className = "m-bubble model";
        const d = document.createElement("div");
        d.textContent = "你好！在这里直接打字就能指挥电脑跑表、找人发钉钉，消息点击即出、零延迟同步~";
        welcome.appendChild(d);
        frag.appendChild(welcome);
      } else {
        for (const it of items) frag.appendChild(buildBubbleNode(it));
        for (const pMsg of pendingUserMsgs) {
          const pTxt = typeof pMsg === "string" ? pMsg : (pMsg && pMsg.text) || "";
          if (pTxt) frag.appendChild(buildBubbleNode({ role: "user", text: pTxt, time: "刚刚", via: "mobile", shot_info: "已发出" }));
        }
        if (pendingUserMsgs.length || mAiThinking.active) {
          const elapsed = mAiThinking.startTs ? Math.max(0, Math.floor((Date.now() - mAiThinking.startTs) / 1000)) : 0;
          const pFirst = pendingUserMsgs[0];
          const pFirstTxt = typeof pFirst === "string" ? pFirst : (pFirst && pFirst.text) || "";
          frag.appendChild(buildMobileThinkingNode(mAiThinking.prompt || pFirstTxt || "", elapsed));
        }
      }
      feed.innerHTML = "";
      feed.appendChild(frag);
      feed.scrollTop = feed.scrollHeight;
    };
    if (typeof requestAnimationFrame === "function") _chatRenderRaf = requestAnimationFrame(draw);
    else draw();
  }

  function applyPcState(st) {
    if (!st || typeof st !== "object") return;
    try {
      const liteHist = Array.isArray(st.ai_history)
        ? st.ai_history.map((m) => {
            if (!m || typeof m !== "object") return m;
            const c = Object.assign({}, m);
            delete c.image_data_url;
            return c;
          })
        : [];
      const liteSt = Object.assign({}, st, { ai_history: liteHist, pending_target_confirm: null });
      safeStorageSet(LS_CACHE_STATE_KEY, JSON.stringify({ st: liteSt, ts: Date.now() }));
    } catch (_e) {}
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
    const cfBar = $("mConfirmAlertBar");
    const pendCf = st.pending_target_confirm && typeof st.pending_target_confirm === "object" ? st.pending_target_confirm : null;
    if (cfBar) {
      cfBar.classList.toggle("show", Boolean(pendCf && pendCf.pending_id));
      if (pendCf && $("mConfirmAlertText")) {
        $("mConfirmAlertText").textContent = `待确认对象：预设「${pendCf.target_name || ""}」 / 桌面「${pendCf.opened_name || ""}」`;
      }
    }
    if (typeof st.chat_seq === "number" && st.chat_seq !== lastChatSeq) {
      lastChatSeq = st.chat_seq;
      const incoming = Array.isArray(st.ai_history) ? st.ai_history : [];
      if (pendingUserMsgs.length) {
        const p0 = pendingUserMsgs[0];
        const pTxt = typeof p0 === "string" ? p0 : (p0 && p0.text) || "";
        const bLen = (p0 && typeof p0.baseLen === "number") ? p0.baseLen : 0;
        const startIdx = incoming.length >= bLen ? bLen : 0;
        if (incoming.slice(startIdx).some((m) => m.role === "user" && m.text === pTxt)) {
          pendingUserMsgs.shift();
        }
      }
      renderChats(incoming);
    }
  }

  function renderSavedAccountUI() {
    const list = loadSavedAccounts();
    const wrap = $("mSavedAccountsWrap");
    const bar = $("mSavedAccountsBar");
    const clearBtn = $("mClearSavedBtn");
    if (clearBtn) clearBtn.style.display = list.length ? "inline" : "none";
    if (!wrap || !bar) return;
    bar.innerHTML = "";
    if (!list.length) {
      wrap.style.display = "none";
      return;
    }
    wrap.style.display = "flex";
    for (const acc of list) {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "m-saved-chip";
      chip.textContent = `${acc.nick}`;
      chip.onclick = () => quickSwitchToAccount(acc);
      bar.appendChild(chip);
    }
  }

  function showMainView() {
    $("mLoginGate").style.display = "none";
    $("mMainApp").style.display = "flex";
    $("mSwitchUserBtn").style.display = "inline-flex";
    if ($("mLogoutBtn")) $("mLogoutBtn").style.display = "inline-flex";
    const modeTag = connMode === "cloud" ? "跨网加密直连" : "本机/内网直连";
    $("mHeaderSub").textContent = `${modeTag} · 当前用户：${myNick || "已连接"}`;
  }

  function closeRelayStream() {
    SMRelay.closeAll();
    for (const [_k, p] of pendingReqs.entries()) {
      clearTimeout(p.tm);
      clearTimeout(p.retryTm);
      p.reject(new Error("closed"));
    }
    pendingReqs.clear();
  }

  function showLoginView(errText = "") {
    $("mLoginGate").style.display = "flex";
    $("mMainApp").style.display = "none";
    $("mSwitchUserBtn").style.display = "none";
    if ($("mLogoutBtn")) $("mLogoutBtn").style.display = "none";
    if ($("mSwitchModalMask")) $("mSwitchModalMask").classList.remove("show");
    $("mHeaderSub").textContent = "无论身在何处，云端连上电脑就能查数据、聊工作、遥控跑表";
    renderSavedAccountUI();
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
      clearTimeout(p.retryTm);
      p.resolve(obj);
    }
    if (obj.state) applyPcState(obj.state);
    if (Array.isArray(obj.ai_history) && typeof obj.chat_seq === "number" && obj.chat_seq !== lastChatSeq) {
      lastChatSeq = obj.chat_seq;
      renderChats(obj.ai_history);
    }
  }

  async function startRelaySubscribe() {
    if (!relayCred) return false;
    return await SMRelay.connectAndSub(relayCred.downTopic, handleRelayIncomingText, 5500);
  }

  async function sendRelayCommand(cmd, params = {}, timeoutMs = 12000) {
    if (!relayCred) throw new Error("未配置云桥连接");
    const credSnap = relayCred;
    const upTopicSnap = credSnap.upTopic;
    const okSub = await SMRelay.connectAndSub(credSnap.downTopic, handleRelayIncomingText, 5500);
    if (!okSub) throw new Error("broker_unreachable");
    const reqId = Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
    const pkt = SMCrypto.encryptPacket(credSnap.masterKey, { req_id: reqId, cmd, params });
    return await new Promise((resolve, reject) => {
      const tm = setTimeout(() => {
        if (pendingReqs.has(reqId)) {
          pendingReqs.delete(reqId);
          clearTimeout(retryTm);
          reject(new Error("timeout"));
        }
      }, timeoutMs);
      const retryTm = setTimeout(() => {
        if (pendingReqs.has(reqId) && relayCred === credSnap) {
          SMRelay.publish(upTopicSnap, pkt);
        }
      }, 1800);
      pendingReqs.set(reqId, { resolve, reject, tm, retryTm });
      const sent = SMRelay.publish(upTopicSnap, pkt);
      if (!sent) {
        pendingReqs.delete(reqId);
        clearTimeout(tm);
        clearTimeout(retryTm);
        reject(new Error("send_failed"));
      }
    });
  }

  async function doPoll() {
    if (pollInFlight) return;
    pollInFlight = true;
    try {
      if (connMode === "cloud" && relayCred) {
        if (!SMRelay.hasConnectingOrReady()) await startRelaySubscribe();
        return;
      }
      if (!mobileToken) return;
      try {
        const st = await mApi("/api/mobile/state");
        if (st && st.auth_required) {
          mobileToken = "";
          safeStorageRemove("sm_mobile_token");
          clearPollTimers();
          showLoginView("登录已失效，请重新输入唯一名字和远程密码");
          return;
        }
        if (st && st.ok) applyPcState(st);
      } catch (_e) {}
    } finally {
      pollInFlight = false;
    }
  }

  async function sendChat(text) {
    const inp = $("mChatInput");
    const msg = String(text !== undefined ? text : inp.value).trim();
    if (!msg) return;
    if (text === undefined || inp.value.trim() === msg) inp.value = "";
    const pendItem = { text: msg, baseLen: cachedChatHistory.length };
    pendingUserMsgs.push(pendItem);
    startMobileThinking(msg);
    renderChats(cachedChatHistory);
    try {
      if (connMode === "cloud" && relayCred) {
        const res = await sendRelayCommand("chat", { prompt: msg, style_id: "custom" }, 58000);
        const idx = pendingUserMsgs.indexOf(pendItem);
        if (idx >= 0) pendingUserMsgs.splice(idx, 1);
        stopMobileThinking();
        if (res && res.state) applyPcState(res.state);
        if (res && res.result) {
          const r = res.result;
          if (Array.isArray(r.history)) {
            lastChatSeq = typeof r.chat_seq === "number" ? r.chat_seq : lastChatSeq + 1;
            renderChats(r.history);
          } else {
            renderChats(cachedChatHistory);
          }
        } else {
          renderChats(cachedChatHistory);
        }
      } else {
        const res = await mApi("/api/mobile/chat", "POST", { prompt: msg }, null, 65000);
        const idx = pendingUserMsgs.indexOf(pendItem);
        if (idx >= 0) pendingUserMsgs.splice(idx, 1);
        stopMobileThinking();
        if (res && Array.isArray(res.history)) {
          lastChatSeq = typeof res.chat_seq === "number" ? res.chat_seq : lastChatSeq + 1;
          renderChats(res.history);
          doPoll();
        } else if (res && res.reply) {
          renderChats(cachedChatHistory);
          doPoll();
        } else {
          renderChats(cachedChatHistory);
          showToast((res && res.message) || "发送未成功");
        }
      }
    } catch (_e) {
      const idx = pendingUserMsgs.indexOf(pendItem);
      if (idx >= 0) pendingUserMsgs.splice(idx, 1);
      stopMobileThinking();
      renderChats(cachedChatHistory);
      showToast("暂未收到电脑回应，请确认电脑软件处于开启状态");
    }
  }

  async function performLogin(nick, pin, b, { remember = true, silentBoot = false } = {}) {
    const myGen = ++loginGen;
    const cleanNick = String(nick || "").trim();
    const cleanPin = String(pin || "").trim();
    const cleanBase = String(b || "").trim().replace(/\/+$/, "");
    if (!cleanNick) {
      showLoginView("请输入目标电脑设置好的名字");
      return false;
    }
    if (!cleanPin) {
      showLoginView("请输入远程密码");
      return false;
    }
    if (cleanBase) {
      serverBase = cleanBase;
      safeStorageSet("sm_mobile_base", cleanBase);
    } else if (!isLocalServed()) {
      serverBase = "";
      safeStorageRemove("sm_mobile_base");
    }

    const persistSuccess = (mode, tok, resolvedNick) => {
      connMode = mode;
      myNick = resolvedNick || cleanNick;
      myPin = cleanPin;
      if (tok) {
        mobileToken = tok;
        safeStorageSet("sm_mobile_token", tok);
      }
      safeStorageSet("sm_conn_mode", mode);
      safeStorageSet("sm_mobile_nick", myNick);
      if (remember) {
        safeStorageSet(LS_AUTO_KEY, "1");
        saveAccountRecord({ nick: myNick, pin: cleanPin, serverBase: serverBase, connMode: mode });
      } else {
        safeStorageSet(LS_AUTO_KEY, "0");
      }
      if ($("mLoginNameInput")) $("mLoginNameInput").value = myNick;
      if ($("mLoginPinInput")) $("mLoginPinInput").value = cleanPin;
    };

    let localWorked = false;
    if (isLocalServed()) {
      try {
        const r = await mApi("/api/mobile/auth", "POST", { nickname: cleanNick, pin: cleanPin }, cleanBase || baseUrl());
        if (myGen !== loginGen) return false;
        if (r && r.ok && r.mobile_token) {
          const rNick = (r.profile && (r.profile.nickname || r.profile.display_name)) || cleanNick;
          persistSuccess("local", r.mobile_token, rNick);
          showMainView();
          await doPoll();
          if (myGen !== loginGen) return false;
          restartPollTimer();
          localWorked = true;
          return true;
        } else if (r && r.message && !cleanBase) {
          showLoginView(r.message);
          return false;
        }
      } catch (_localErr) {}
    } else if (!cleanBase) {
      try {
        const localHostBase = "http://127.0.0.1:18792";
        const r = await mApi("/api/mobile/auth", "POST", { nickname: cleanNick, pin: cleanPin }, localHostBase, 750);
        if (myGen !== loginGen) return false;
        if (r && r.ok && r.mobile_token) {
          serverBase = localHostBase;
          safeStorageSet("sm_mobile_base", localHostBase);
          const rNick = (r.profile && (r.profile.nickname || r.profile.display_name)) || cleanNick;
          persistSuccess("local", r.mobile_token, rNick);
          showMainView();
          await doPoll();
          if (myGen !== loginGen) return false;
          restartPollTimer();
          localWorked = true;
          return true;
        }
      } catch (_loopErr) {}
    }
    if (myGen !== loginGen) return false;
    if (!localWorked) {
      const derived = SMCrypto.deriveRoomAndKey(cleanNick, cleanPin);
      relayCred = Object.assign({ nick: cleanNick, pin: cleanPin }, derived);
      try {
        const ack = await sendRelayCommand("auth_state", {}, silentBoot ? 7500 : 9000);
        if (myGen !== loginGen) return false;
        if (ack && ack.ok) {
          persistSuccess("cloud", "", ack.nickname || cleanNick);
          showMainView();
          if (ack.state) applyPcState(ack.state);
          restartPollTimer();
          return true;
        }
        closeRelayStream();
        relayCred = null;
        showLoginView((ack && ack.message) || "登录被电脑端拒绝，请核对昵称与密码");
        return false;
      } catch (_relayErr) {
        if (myGen !== loginGen) return false;
        closeRelayStream();
        relayCred = null;
        const isNetErr = _relayErr && _relayErr.message === "broker_unreachable";
        showLoginView(
          isNetErr
            ? "云端通道暂未连通：请检查当前网络是否可上网后再试。"
            : "没连上电脑：请核对【名字】和【远程密码】是否和电脑端完全一样，并确认电脑软件正开着并连着网。"
        );
        return false;
      }
    }
    return false;
  }

  function resetAccountViewCache() {
    cachedChatHistory = [];
    pendingUserMsgs = [];
    stopMobileThinking();
    lastChatSeq = -1;
    safeStorageRemove(LS_CACHE_STATE_KEY);
    renderChats([]);
  }

  function logoutCurrentSession() {
    loginGen += 1;
    mobileToken = "";
    connMode = "";
    relayCred = null;
    resetAccountViewCache();
    closeRelayStream();
    safeStorageSet(LS_AUTO_KEY, "0");
    safeStorageRemove("sm_mobile_token");
    safeStorageRemove("sm_conn_mode");
    clearPollTimers();
    showLoginView("");
    showToast("已退出当前连接");
  }

  async function quickSwitchToAccount(acc) {
    if (!acc || !acc.nick || !acc.pin) return;
    loginGen += 1;
    if ($("mSwitchModalMask")) $("mSwitchModalMask").classList.remove("show");
    if ($("mLoginNameInput")) $("mLoginNameInput").value = acc.nick;
    if ($("mLoginPinInput")) $("mLoginPinInput").value = acc.pin;
    if ($("mServerBaseInput")) $("mServerBaseInput").value = acc.serverBase || "";
    serverBase = acc.serverBase || "";
    mobileToken = "";
    connMode = "";
    relayCred = null;
    resetAccountViewCache();
    closeRelayStream();
    clearPollTimers();
    showToast(`正在切换到「${acc.nick}」…`);
    await performLogin(acc.nick, acc.pin, acc.serverBase || "", { remember: true });
  }

  function renderSwitchModalList() {
    const box = $("mSwitchAcctList");
    if (!box) return;
    box.innerHTML = "";
    const list = loadSavedAccounts();
    if (!list.length) {
      const tip = document.createElement("div");
      tip.style.cssText = "font-size:12px;color:var(--sub);padding:8px 0;";
      tip.textContent = "暂未记住其他账号，点下方按钮即可登录新账号并自动记住。";
      box.appendChild(tip);
      return;
    }
    for (const acc of list) {
      const row = document.createElement("div");
      row.className = "m-acct-item";
      const left = document.createElement("div");
      left.style.cssText = "min-width:0;cursor:pointer;flex:1;";
      const nm = document.createElement("div");
      nm.style.cssText = "font-size:13.5px;font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;";
      const isCur = myNick && SMCrypto.normalizeNickKey(acc.nick) === SMCrypto.normalizeNickKey(myNick);
      nm.textContent = acc.nick + (isCur ? "（当前）" : "");
      const sub = document.createElement("div");
      sub.style.cssText = "font-size:11px;color:var(--sub);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;";
      sub.textContent = acc.serverBase || "云端暗号加密通道";
      left.appendChild(nm);
      left.appendChild(sub);
      left.onclick = () => quickSwitchToAccount(acc);

      const btns = document.createElement("div");
      btns.style.cssText = "display:flex;gap:5px;flex-shrink:0;";
      const swBtn = document.createElement("button");
      swBtn.type = "button";
      swBtn.className = "m-pill";
      swBtn.textContent = isCur ? "重连" : "切换";
      swBtn.onclick = () => quickSwitchToAccount(acc);
      const delBtn = document.createElement("button");
      delBtn.type = "button";
      delBtn.className = "m-pill";
      delBtn.style.cssText = "background:rgba(255,59,48,0.12);color:var(--red);";
      delBtn.textContent = "删";
      delBtn.onclick = () => {
        removeAccountRecord(acc.nick);
        renderSwitchModalList();
        renderSavedAccountUI();
      };
      btns.appendChild(swBtn);
      btns.appendChild(delBtn);
      row.appendChild(left);
      row.appendChild(btns);
      box.appendChild(row);
    }
  }

  $("mLoginSubmitBtn").onclick = async () => {
    const nick = $("mLoginNameInput").value.trim();
    const pin = $("mLoginPinInput").value.trim();
    const b = $("mServerBaseInput") ? $("mServerBaseInput").value.trim() : "";
    const remember = $("mRememberCheck") ? Boolean($("mRememberCheck").checked) : true;
    const btn = $("mLoginSubmitBtn");
    btn.disabled = true;
    btn.textContent = "正在加密连接电脑…";
    try {
      await performLogin(nick, pin, b, { remember });
    } finally {
      btn.disabled = false;
      btn.textContent = "云端连接我的电脑";
    }
  };

  if ($("mClearSavedBtn")) {
    $("mClearSavedBtn").onclick = () => {
      safeStorageRemove(LS_ACCTS_KEY);
      safeStorageRemove(LS_CREDS_KEY);
      safeStorageRemove(LS_AUTO_KEY);
      safeStorageRemove("sm_mobile_nick");
      safeStorageRemove("sm_mobile_base");
      if ($("mLoginNameInput")) $("mLoginNameInput").value = "";
      if ($("mLoginPinInput")) $("mLoginPinInput").value = "";
      if ($("mServerBaseInput")) $("mServerBaseInput").value = "";
      renderSavedAccountUI();
      showToast("已清除本地记住的账号与密码");
    };
  }
  $("mSwitchUserBtn").onclick = () => {
    renderSwitchModalList();
    if ($("mSwitchModalMask")) $("mSwitchModalMask").classList.add("show");
  };
  if ($("mSwitchCloseBtn")) $("mSwitchCloseBtn").onclick = () => $("mSwitchModalMask").classList.remove("show");
  if ($("mSwitchModalMask")) {
    $("mSwitchModalMask").onclick = (ev) => {
      if (ev.target === $("mSwitchModalMask")) $("mSwitchModalMask").classList.remove("show");
    };
  }
  if ($("mSwitchAddNewBtn")) {
    $("mSwitchAddNewBtn").onclick = () => {
      if ($("mSwitchModalMask")) $("mSwitchModalMask").classList.remove("show");
      logoutCurrentSession();
      if ($("mLoginNameInput")) { $("mLoginNameInput").value = ""; $("mLoginNameInput").focus(); }
      if ($("mLoginPinInput")) $("mLoginPinInput").value = "";
    };
  }
  if ($("mLogoutBtn")) $("mLogoutBtn").onclick = logoutCurrentSession;

  if ($("mToggleCardBtn") && $("mStatusCard")) {
    const syncCardCompact = (compact) => {
      $("mStatusCard").classList.toggle("m-status-compact", !!compact);
      $("mToggleCardBtn").textContent = compact ? "展开详情" : "收起";
      safeStorageSet("sm_m_card_compact", compact ? "1" : "0");
    };
    if (safeStorageGet("sm_m_card_compact", "") === "1") syncCardCompact(true);
    $("mToggleCardBtn").onclick = () => {
      syncCardCompact(!$("mStatusCard").classList.contains("m-status-compact"));
    };
  }

  if ($("mImgZoomMask")) {
    $("mImgZoomMask").onclick = () => $("mImgZoomMask").classList.remove("show");
  }
  if ($("mConfirmAlertBar")) {
    $("mConfirmAlertBar").onclick = () => {
      const feed = $("mChatFeed");
      if (feed) feed.scrollTop = feed.scrollHeight;
    };
  }
  function resumePollIfLoggedIn() {
    if ($("mMainApp") && $("mMainApp").style.display !== "none" && ((connMode === "cloud" && relayCred) || mobileToken)) {
      restartPollTimer();
      doPoll();
    }
  }
  window.addEventListener("pagehide", () => {
    pollInFlight = false;
    clearPollTimers();
    stopMobileThinking();
  });
  window.addEventListener("pageshow", resumePollIfLoggedIn);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") resumePollIfLoggedIn();
  });

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
  for (const tp of $("mQuickChips").querySelectorAll("[data-mtpl]")) {
    tp.onclick = () => {
      const inp = $("mChatInput");
      if (!inp) return;
      inp.value = tp.dataset.mtpl || "";
      inp.focus();
      showToast("已填入定时模板，改好时间与发送对象后点发送即可");
    };
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
        scheduleDelayedPoll(250);
      } catch (_e) {
        showToast("操作指令暂未送达，请重试");
      }
    };
  }

  (async function bootMobile() {
    renderSavedAccountUI();
    const allowAuto = safeStorageGet(LS_AUTO_KEY, "") !== "0";
    if (connMode === "local" && mobileToken && isLocalServed() && allowAuto) {
      showMainView();
      const cached = safeJsonParse(safeStorageGet(LS_CACHE_STATE_KEY, ""), null);
      if (cached && cached.st) applyPcState(cached.st);
      await doPoll();
      restartPollTimer();
      return;
    }
    const useCred = savedCred || (myNick && myPin ? { nick: myNick, pin: myPin, serverBase: serverBase } : null);
    if (allowAuto && useCred && useCred.nick && useCred.pin) {
      if ($("mLoginNameInput")) $("mLoginNameInput").value = useCred.nick;
      if ($("mLoginPinInput")) $("mLoginPinInput").value = useCred.pin;
      if ($("mServerBaseInput") && useCred.serverBase) $("mServerBaseInput").value = useCred.serverBase;
      showMainView();
      const cached = safeJsonParse(safeStorageGet(LS_CACHE_STATE_KEY, ""), null);
      if (cached && cached.st) applyPcState(cached.st);
      $("mHeaderSub").textContent = `自动登录中… · ${useCred.nick}`;
      await performLogin(useCred.nick, useCred.pin, useCred.serverBase || "", { remember: true, silentBoot: true });
    }
  })();
})();
</script>
</body>
</html>
"""


_LAN_IP_CACHE = {"ts": 0.0, "ips": ["127.0.0.1"]}


def _detect_lan_ipv4_list(use_cache=True):
    now_m = time.monotonic()
    if use_cache and _LAN_IP_CACHE["ts"] and (now_m - _LAN_IP_CACHE["ts"]) < 30.0:
        return list(_LAN_IP_CACHE["ips"])
    ips = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip0 = s.getsockname()[0]
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
    res_list = ips or ["127.0.0.1"]
    _LAN_IP_CACHE["ts"] = now_m
    _LAN_IP_CACHE["ips"] = list(res_list)
    return list(res_list)


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

        # 云桥加密实时双向状态（双节点 MQTT over TLS）
        self._relay_stop = threading.Event()
        self._push_wakeup = threading.Event()
        self._force_push_flag = threading.Event()
        self._relay_cmd_sem = threading.BoundedSemaphore(8)
        self._relay_threads = []
        self._relay_push_thread = None
        self._mqtt_conns = {}
        self._mqtt_send_lock = threading.Lock()
        self._nonce_lock = threading.Lock()
        self._relay_seen_nonces = {}
        self._relay_last_active_ts = 0.0
        self._relay_last_pushed_seq = -1
        self._relay_connected = False
        self._relay_error = ""

        # 外网隧道状态
        self._tunnel_proc = None
        self._tunnel_running = False
        self._tunnel_starting = False
        self._tunnel_gen = 0
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
                    schema_v = _safe_int(data.get("schema_v"), default=1)
                    if schema_v < 2 and pw and hashlib.sha256(pw.encode("utf-8")).hexdigest() in (
                        "d88fb3e6b6700bffa5f040818fa206a698b9c675627f817f11075fb026217015",
                        "729cd4e8f2872e7c4ad2999b61d30827903c40d8f2de7a2cae5085e369128e3f",
                    ):
                        self.mobile_pin = DEFAULT_REMOTE_PASSWORD
                        self.cloud_relay_enabled = bool(data.get("cloud_relay_enabled", True))
                        self._save_remote_cfg()
                        return
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
        tmp = p.with_name(f".{p.name}.tmp.{os.getpid()}.{threading.get_ident()}")
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8", newline="") as fp:
                fp.write(json.dumps({
                    "schema_v": 2,
                    "remote_password": self.mobile_pin,
                    "cloud_relay_enabled": bool(self.cloud_relay_enabled),
                }, ensure_ascii=False, indent=2))
                fp.flush()
            os.replace(str(tmp), str(p))
        except Exception:
            try:
                if tmp.exists():
                    tmp.unlink()
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

    @staticmethod
    def _prune_fail_tracker(tracker, now, max_entries=256):
        expired = [
            k for k, v in tracker.items()
            if (v.get("until", 0.0) > 0.0 and v["until"] <= now)
            or (now - float(v.get("last_ts", now))) > 3600.0
        ]
        for k in expired:
            tracker.pop(k, None)
        if len(tracker) > max_entries:
            oldest = sorted(tracker.items(), key=lambda it: float(it[1].get("last_ts", 0.0)))[: len(tracker) - max_entries]
            for k, _ in oldest:
                tracker.pop(k, None)

    def verify_pin_and_login(self, nick, pin, client_id="local"):
        now = time.time()
        with self._lock:
            if len(self._auth_fail_tracker) > 64:
                self._prune_fail_tracker(self._auth_fail_tracker, now)
            rec = self._auth_fail_tracker.get(client_id) or {"cnt": 0, "until": 0.0}
            if rec.get("until", 0.0) > now:
                wait_s = max(1, int(rec["until"] - now))
                return {"ok": False, "locked": True, "message": f"密码连续输错过多，请 {wait_s} 秒后再试"}
            if rec.get("until", 0.0) > 0.0 and rec["until"] <= now:
                rec = {"cnt": 0, "until": 0.0}
                self._auth_fail_tracker.pop(client_id, None)
            want_pin = str(self.mobile_pin or DEFAULT_REMOTE_PASSWORD)
            got_pin = str(pin or "").strip()
            if not got_pin or not hmac.compare_digest(got_pin, want_pin):
                cnt = int(rec.get("cnt", 0)) + 1
                until = (now + AUTH_LOCKOUT_SEC) if cnt >= AUTH_MAX_FAILS else 0.0
                self._auth_fail_tracker[client_id] = {"cnt": cnt, "until": until, "last_ts": now}
                self._prune_fail_tracker(self._auth_fail_tracker, now)
                if cnt >= AUTH_MAX_FAILS:
                    return {
                        "ok": False,
                        "locked": True,
                        "message": f"密码连续输错过多，已锁定 {int(AUTH_LOCKOUT_SEC)} 秒后再试",
                    }
                return {"ok": False, "message": "访问密码不正确，请核对电脑端设置的远程密码后重试"}
            self._auth_fail_tracker.pop(client_id, None)
        return self.ctrl.verify_mobile_member_login(nick)

    def _resolve_cors_origin(self, origin_hdr):
        origin = str(origin_hdr or "").strip()
        if not origin:
            return ""
        p = urlparse(origin)
        if p.scheme not in ("http", "https") or not p.netloc:
            return ""
        allowed = {"https://sorenskr.github.io"}
        with self._lock:
            port = self._port
            pub = str(self._public_url or "").strip()
        if port:
            allowed.add(f"http://127.0.0.1:{port}")
            allowed.add(f"http://localhost:{port}")
            for ip in _detect_lan_ipv4_list():
                allowed.add(f"http://{ip}:{port}")
        if pub:
            pp = urlparse(pub)
            if pp.scheme in ("http", "https") and pp.netloc:
                allowed.add(f"{pp.scheme}://{pp.netloc}")
        norm_origin = f"{p.scheme}://{p.netloc}"
        return norm_origin if norm_origin in allowed else ""

    def start(self):
        with self._lock:
            if self._server is not None:
                return True, self._port
            mgr = self

            class MobileHandler(BaseHTTPRequestHandler):
                def setup(self):
                    super().setup()
                    try:
                        self.request.settimeout(15.0)
                    except Exception:
                        pass

                def log_message(self, fmt, *args):
                    return

                def _cors(self):
                    allowed_ori = mgr._resolve_cors_origin(self.headers.get("Origin"))
                    if allowed_ori:
                        self.send_header("Access-Control-Allow-Origin", allowed_ori)
                        self.send_header("Vary", "Origin")
                        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Mobile-Token")
                        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                        self.send_header("Access-Control-Allow-Private-Network", "true")

                def _send_security_headers(self):
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("X-Frame-Options", "DENY")
                    self.send_header("Referrer-Policy", "no-referrer")

                def do_OPTIONS(self):
                    self.send_response(HTTPStatus.NO_CONTENT)
                    self._cors()
                    self.end_headers()

                def _send_json(self, obj, code=HTTPStatus.OK):
                    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                    self.send_response(code)
                    self._cors()
                    self._send_security_headers()
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
                    try:
                        parsed = urlparse(self.path)
                        if parsed.path in ("/", "/m", "/index.html"):
                            body = MOBILE_HTML_TEMPLATE.encode("utf-8")
                            self.send_response(HTTPStatus.OK)
                            self._cors()
                            self._send_security_headers()
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
                            st = mgr._get_ctrl_state(since=since_log)
                            self._send_json(mgr._pack_mobile_state(st))
                            return

                        self._send_json({"ok": False, "message": "Not Found"}, HTTPStatus.NOT_FOUND)
                    except (ConnectionError, BrokenPipeError):
                        pass
                    except Exception as exc:
                        try:
                            self._send_json({"ok": False, "message": f"服务端异常：{str(exc)[:120]}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
                        except Exception:
                            pass

                def do_POST(self):
                    try:
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
                            with mgr._lock:
                                tun_on = bool(mgr._tunnel_running)
                                tun_kind = str(mgr._tunnel_kind or "")
                            if c_ip in ("127.0.0.1", "::1") and tun_on:
                                if tun_kind == "cloudflared":
                                    cf_ip = str(self.headers.get("CF-Connecting-IP") or "").strip()
                                    c_ip = f"cf:{cf_ip[:45]}" if cf_ip else "tunnel:cloudflared"
                                else:
                                    c_ip = f"tunnel:{tun_kind or 'remote'}"
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

                        if parsed.path == "/api/mobile/confirm-target":
                            if hasattr(mgr.ctrl, "resolve_target_confirmation"):
                                r = mgr.ctrl.resolve_target_confirmation(
                                    action=str(body.get("action") or "").strip(),
                                    new_target=str(body.get("new_target") or "").strip(),
                                    pending_id=str(body.get("pending_id") or "").strip(),
                                    source_device="mobile",
                                    speaker_name=u.get("nickname") or "",
                                )
                            else:
                                r = {"ok": False, "message": "当前版本不支持远程看图确认"}
                            self._send_json(r if isinstance(r, dict) else {"ok": False})
                            return

                        self._send_json({"ok": False, "message": "Not Found"}, HTTPStatus.NOT_FOUND)
                    except (ConnectionError, BrokenPipeError):
                        pass
                    except Exception as exc:
                        try:
                            self._send_json({"ok": False, "message": f"服务端异常：{str(exc)[:120]}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
                        except Exception:
                            pass

            class _ExclusiveMobileHTTPServer(ThreadingHTTPServer):
                allow_reuse_address = False

                def server_bind(self):
                    if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                        try:
                            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                        except Exception:
                            pass
                    super().server_bind()

            for p in range(self.base_port, self.base_port + 20):
                in_use = False
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                        probe.settimeout(0.08)
                        if probe.connect_ex(("127.0.0.1", p)) == 0:
                            in_use = True
                except Exception:
                    in_use = False
                if in_use:
                    continue
                try:
                    srv = _ExclusiveMobileHTTPServer(("0.0.0.0", p), MobileHandler)
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
        hist_raw = list(st.get("ai_history") or [])[-30:]
        hist_packed = []
        img_kept = 0
        for item in reversed(hist_raw):
            if not isinstance(item, dict):
                continue
            cp = dict(item)
            if cp.get("image_data_url"):
                img_kept += 1
                if img_kept > 2:
                    cp.pop("image_data_url", None)
            hist_packed.append(cp)
        hist_packed.reverse()
        pend = st.get("pending_target_confirm")
        if isinstance(pend, dict):
            pend_out = dict(pend)
            pend_out.pop("image_data_url", None)
        else:
            pend_out = None
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
            "pending_target_confirm": pend_out,
            "chat_seq": st.get("chat_seq", 0),
            "ai_history": hist_packed,
            "logs": (st.get("logs") or [])[-15:],
        }

    def _exec_remote_action(self, act):
        if act == "run_now":
            return self.ctrl.start_run_now()
        if act == "run_capture":
            return self.ctrl.start_capture_only()
        if act == "pause":
            r = self.ctrl.pause_current_task()
            return {"ok": bool(r[0]), "message": r[1]} if isinstance(r, tuple) else r
        if act == "resume":
            r = self.ctrl.resume_current_task("continue")
            return {"ok": bool(r[0]), "message": r[1]} if isinstance(r, tuple) else r
        if act == "stop":
            r = self.ctrl.stop_current_task()
            return {"ok": bool(r[0]), "message": r[1]} if isinstance(r, tuple) else r
        return {"ok": False, "message": f"未知动作：{act}"}

    def notify_credentials_changed(self):
        """改名或改远程密码后，立刻断开旧云中继订阅并瞬时切换至新房间。"""
        with self._lock:
            self._relay_last_pushed_seq = -1
            conns = list(self._mqtt_conns.values())
            self._mqtt_conns.clear()
            self._relay_connected = False
        for item in conns:
            try:
                item["sock"].close()
            except Exception:
                pass

    def start_cloud_relay(self):
        self._relay_stop.clear()
        alive = [th for th in self._relay_threads if th.is_alive()]
        if not alive:
            self._relay_threads = []
            for host, port in MQTT_TLS_BROKERS:
                th = threading.Thread(
                    target=self._mqtt_broker_loop,
                    args=(host, port),
                    daemon=True,
                    name=f"SheetMateMqttRelay-{host}",
                )
                th.start()
                self._relay_threads.append(th)
        if self._relay_push_thread is None or not self._relay_push_thread.is_alive():
            self._relay_push_thread = threading.Thread(
                target=self._cloud_state_push_loop,
                daemon=True,
                name="SheetMateCloudPush",
            )
            self._relay_push_thread.start()

    def _send_relay_down_packet(self, down_topic, master_key, payload):
        pkt = CloudRelayCrypto.encrypt_packet(master_key, payload)
        frame = _mqtt_build_publish(down_topic, pkt.encode("utf-8"), retain=False)
        with self._lock:
            conns = list(self._mqtt_conns.items())
        sent_any = False
        dead_hosts = []
        for host, item in conns:
            try:
                with item["lock"]:
                    item["sock"].sendall(frame)
                sent_any = True
            except Exception as exc:
                self._relay_error = str(exc)[:100]
                dead_hosts.append((host, item["sock"]))
        for host, sock in dead_hosts:
            with self._lock:
                if self._mqtt_conns.get(host, {}).get("sock") is sock:
                    self._mqtt_conns.pop(host, None)
                    self._relay_connected = bool(self._mqtt_conns)
            try:
                sock.close()
            except Exception:
                pass
        if sent_any:
            return True
        ok_tmp, err_tmp = _mqtt_publish_once(down_topic, pkt.encode("utf-8"), timeout=3.5)
        if not ok_tmp and err_tmp:
            self._relay_error = err_tmp
        return ok_tmp

    def _get_ctrl_state(self, since=0):
        try:
            return self.ctrl.get_state(since=since, source="mobile")
        except TypeError:
            return self.ctrl.get_state(since=since)

    def _handle_relay_command(self, obj, nick, down_topic, master_key):
        self._relay_last_active_ts = time.time()
        cmd = str(obj.get("cmd") or "").strip()
        req_id = str(obj.get("req_id") or "")
        params = obj.get("params") if isinstance(obj.get("params"), dict) else {}
        try:
            if cmd == "auth_state":
                st = self._get_ctrl_state(since=0)
                seq_val = int(st.get("chat_seq") or 0)
                if self._send_relay_down_packet(down_topic, master_key, {
                    "ok": True,
                    "req_id": req_id,
                    "nickname": nick,
                    "state": self._pack_mobile_state(st),
                }):
                    self._relay_last_pushed_seq = seq_val
            elif cmd == "chat":
                res = self.ctrl.summarize_ai(
                    style_id=str(params.get("style_id") or "custom"),
                    prompt=str(params.get("prompt") or ""),
                    action="send",
                    source_device="mobile",
                    speaker_name=nick,
                )
                st = self._get_ctrl_state(since=0)
                seq_val = int(st.get("chat_seq") or 0)
                if self._send_relay_down_packet(down_topic, master_key, {
                    "ok": True,
                    "req_id": req_id,
                    "result": res,
                    "state": self._pack_mobile_state(st),
                }):
                    self._relay_last_pushed_seq = seq_val
            elif cmd == "action":
                r = self._exec_remote_action(str(params.get("action") or ""))
                st = self._get_ctrl_state(since=0)
                self._send_relay_down_packet(down_topic, master_key, {
                    "ok": True,
                    "req_id": req_id,
                    "result": r,
                    "state": self._pack_mobile_state(st),
                })
            elif cmd == "confirm_target":
                if hasattr(self.ctrl, "resolve_target_confirmation"):
                    r = self.ctrl.resolve_target_confirmation(
                        action=str(params.get("action") or ""),
                        new_target=str(params.get("new_target") or ""),
                        pending_id=str(params.get("pending_id") or ""),
                        source_device="mobile",
                        speaker_name=nick,
                    )
                else:
                    r = {"ok": False, "message": "当前版本不支持远程看图确认"}
                st = self._get_ctrl_state(since=0)
                seq_val = int(st.get("chat_seq") or 0)
                if self._send_relay_down_packet(down_topic, master_key, {
                    "ok": True,
                    "req_id": req_id,
                    "result": r,
                    "state": self._pack_mobile_state(st),
                }):
                    self._relay_last_pushed_seq = seq_val
            else:
                if req_id:
                    self._send_relay_down_packet(down_topic, master_key, {
                        "ok": False,
                        "req_id": req_id,
                        "message": f"未知云端指令：{cmd}",
                    })
        except Exception as exc:
            self._relay_error = f"cmd:{cmd}: {str(exc)[:80]}"
            if req_id:
                try:
                    self._send_relay_down_packet(down_topic, master_key, {
                        "ok": False,
                        "req_id": req_id,
                        "message": f"电脑处理异常：{str(exc)[:120]}",
                    })
                except Exception:
                    pass

    def notify_state_changed(self, force_push=False):
        """电脑端聊天或运行状态变化时，立即唤醒云中继推流线程做 0 延迟推送。"""
        try:
            if force_push:
                self._force_push_flag.set()
            self._push_wakeup.set()
        except Exception:
            pass

    def _cloud_state_push_loop(self):
        while not self._relay_stop.is_set():
            try:
                self._push_wakeup.clear()
                force_now = self._force_push_flag.is_set()
                has_pending = bool(getattr(self.ctrl, "_pending_target_confirm", None))
                recent_active = (time.time() - self._relay_last_active_ts) < 3600
                if self.cloud_relay_enabled and (recent_active or force_now or has_pending):
                    nick = self._get_local_nick()
                    if nick:
                        seq_now = int(getattr(self.ctrl, "_chat_seq", 0))
                        if force_now or seq_now != self._relay_last_pushed_seq:
                            cred = CloudRelayCrypto.derive_room_and_key(nick, self.mobile_pin)
                            st = self._get_ctrl_state(since=0)
                            if self._send_relay_down_packet(cred["down_topic"], cred["master_key"], {
                                "ok": True,
                                "state": self._pack_mobile_state(st),
                            }):
                                self._relay_last_pushed_seq = seq_now
                                if force_now:
                                    self._force_push_flag.clear()
                        elif force_now:
                            self._force_push_flag.clear()
                    elif force_now:
                        self._force_push_flag.clear()
            except Exception:
                pass
            self._push_wakeup.wait(timeout=0.7)

    def _mqtt_broker_loop(self, host, port):
        backoff = 1.2
        ctx = ssl.create_default_context()
        while not self._relay_stop.is_set():
            nick = self._get_local_nick()
            if not self.cloud_relay_enabled or not nick:
                with self._lock:
                    self._mqtt_conns.pop(host, None)
                    self._relay_connected = bool(self._mqtt_conns)
                time.sleep(1.5)
                continue
            pin_snap = self.mobile_pin
            cred = CloudRelayCrypto.derive_room_and_key(nick, pin_snap)
            up_topic = cred["up_topic"]
            down_topic = cred["down_topic"]
            master_key = cred["master_key"]
            raw_sock = None
            ssock = None
            try:
                raw_sock = socket.create_connection((host, port), timeout=8.0)
                ssock = ctx.wrap_socket(raw_sock, server_hostname=host)
                raw_sock = None
                ssock.settimeout(8.0)
                send_lock = threading.Lock()
                cid = f"smpc_{os.getpid()}_{secrets.token_hex(4)}"
                with send_lock:
                    ssock.sendall(_mqtt_build_connect(cid, keepalive=45))
                hdr0, body0 = _mqtt_read_packet(ssock)
                if (hdr0 & 0xF0) != 0x20 or len(body0) < 2 or body0[1] != 0:
                    raise ConnectionError("MQTT CONNACK rejected")
                with send_lock:
                    ssock.sendall(_mqtt_build_subscribe(1, up_topic))
                hdr1, _ = _mqtt_read_packet(ssock)
                if (hdr1 & 0xF0) != 0x90:
                    raise ConnectionError("MQTT SUBACK missing")

                with self._lock:
                    self._mqtt_conns[host] = {"sock": ssock, "lock": send_lock}
                    self._relay_connected = True
                    self._relay_error = ""
                backoff = 1.2

                ssock.settimeout(22.0)
                awaiting_pong = False
                while not self._relay_stop.is_set():
                    if (
                        not self.cloud_relay_enabled
                        or self._get_local_nick() != nick
                        or self.mobile_pin != pin_snap
                    ):
                        break
                    try:
                        hdr, body = _mqtt_read_packet(ssock)
                        awaiting_pong = False
                    except (socket.timeout, TimeoutError):
                        if awaiting_pong:
                            raise ConnectionError("MQTT PINGRESP timeout")
                        with send_lock:
                            ssock.sendall(b"\xc0\x00")
                        awaiting_pong = True
                        continue
                    ptype = hdr & 0xF0
                    if ptype == 0xD0:
                        continue
                    if ptype == 0x30:
                        _tp, payload_b = _mqtt_parse_publish(hdr, body)
                        raw_txt = payload_b.decode("utf-8", errors="ignore")
                        with self._nonce_lock:
                            obj = CloudRelayCrypto.decrypt_packet(
                                master_key,
                                raw_txt,
                                seen_nonces=self._relay_seen_nonces,
                            )
                        if obj is not None and self._relay_cmd_sem.acquire(blocking=False):
                            def _run_cmd(_o=obj):
                                try:
                                    self._handle_relay_command(_o, nick, down_topic, master_key)
                                except Exception:
                                    pass
                                finally:
                                    self._relay_cmd_sem.release()

                            threading.Thread(
                                target=_run_cmd,
                                daemon=True,
                            ).start()
            except Exception as exc:
                if not self._relay_stop.is_set():
                    self._relay_error = f"{host}: {str(exc)[:80]}"
            finally:
                with self._lock:
                    if self._mqtt_conns.get(host, {}).get("sock") is ssock:
                        self._mqtt_conns.pop(host, None)
                    self._relay_connected = bool(self._mqtt_conns)
                for _s_cls in (ssock, raw_sock):
                    if _s_cls is not None:
                        try:
                            _s_cls.close()
                        except Exception:
                            pass
            if not self._relay_stop.is_set():
                if self._get_local_nick() == nick and self.mobile_pin == pin_snap:
                    time.sleep(backoff)
                    backoff = min(12.0, backoff * 1.5)
                else:
                    backoff = 0.5
                    time.sleep(0.15)

    def stop(self):
        self._relay_stop.set()
        self._push_wakeup.set()
        self.notify_credentials_changed()
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
            if self._tunnel_starting:
                return {"ok": True, "running": True, "message": "正在打通外网直连隧道，约需 2~5 秒…"}
            port = self._port
            if not port:
                return {"ok": False, "message": "本地手机端口未就绪"}
            self._tunnel_starting = True
            self._tunnel_error = ""
            self._public_url = ""
            gen_snap = self._tunnel_gen

        try:
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
                with self._lock:
                    self._tunnel_starting = False
                    self._tunnel_error = "当前系统未找到 ssh 或 cloudflared，推荐直接用 GitHub 全球远程网址或局域网访问"
                    msg = self._tunnel_error
                return {"ok": False, "message": msg}

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
                with self._lock:
                    self._tunnel_starting = False
                    self._tunnel_error = f"启动公网穿透失败：{exc}"
                    msg = self._tunnel_error
                return {"ok": False, "message": msg}

            with self._lock:
                self._tunnel_starting = False
                if gen_snap != self._tunnel_gen:
                    proc_aborted = True
                else:
                    proc_aborted = False
                    self._tunnel_proc = proc
                    self._tunnel_running = True
                    self._tunnel_kind = kind

            if proc_aborted:
                try:
                    proc.terminate()
                    proc.wait(timeout=1.0)
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
                return {"ok": False, "running": False, "message": "穿透隧道启动已取消"}

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
        except Exception as exc:
            with self._lock:
                self._tunnel_starting = False
                self._tunnel_error = f"启动公网穿透失败：{exc}"
            return {"ok": False, "message": self._tunnel_error}

    def stop_tunnel(self):
        with self._lock:
            self._tunnel_gen += 1
            self._tunnel_starting = False
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
            self._tokens.clear()
            self._save_remote_cfg()
        self.notify_credentials_changed()
        return {"ok": True, "need_pin": True, "remote_password": val, "message": "访问密码已更新（已同步刷新加密通道）"}

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
            running = self._server is not None
            tunnel_running = bool(self._tunnel_running)
            pub = self._public_url
            tunnel_error = self._tunnel_error
            remote_pin = self.mobile_pin
            relay_enabled = bool(self.cloud_relay_enabled)
            relay_conn = bool(self._relay_connected)
            relay_err = self._relay_error
            local_nick = self._get_local_nick()
        lan_urls = [f"http://{ip}:{port}" for ip in _detect_lan_ipv4_list()] if port else []
        local_target = pub or (lan_urls[0] if lan_urls else "")
        qr_github_svg = generate_qr_svg(GITHUB_PAGES_URL, box_px=192)
        qr_local_svg = generate_qr_svg(local_target, box_px=192) if local_target else ""
        return {
            "ok": True,
            "running": running,
            "port": port,
            "lan_urls": lan_urls,
            "tunnel_running": tunnel_running,
            "public_url": pub,
            "tunnel_error": tunnel_error,
            "github_pages_url": GITHUB_PAGES_URL,
            "remote_password": remote_pin,
            "cloud_relay_enabled": relay_enabled,
            "cloud_relay_connected": relay_conn,
            "cloud_relay_error": relay_err,
            "local_nick": local_nick,
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
