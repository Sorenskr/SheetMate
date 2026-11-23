# -*- coding: utf-8 -*-
# pyright: reportMissingImports=false, reportMissingModuleSource=false
"""
暗号表助手 SheetMate 多成员协作与超级管理员中心引擎（纯标准库实现）：
- 成员档案（自定义昵称 / 随机起名 / 跳过分配易读编号 / 上传或预设矢量头像）
- 三合一跨网同步：本机或共享盘目录 / HTTP 局域网或公网中心服务 / GitHub Gist 云端同步
- 多人互斥运行锁 + 自动排队接力（别人在跑时拦住并支持“排在他后面自动跑”）
- 自定义起止时间统计、全员报错与 AI 对话同步、成员配置自动体检与精细权限管控
- 双口令+密码验证隐藏超级管理员后台、视角切换预览与用户意见反馈箱
"""

import base64
import copy
from datetime import datetime, timedelta
import hashlib
import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import random
import re
import secrets
import socket
import ssl
import struct
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import uuid
import zlib

_PET_V2_MARKER = "<!--sm-pet-v2-->"


def _make_svg_data_uri(bg1, bg2, inner_svg):
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 96">'
        f'{_PET_V2_MARKER}'
        '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0%" stop-color="{bg1}"/>'
        f'<stop offset="100%" stop-color="{bg2}"/>'
        '</linearGradient></defs>'
        '<rect width="96" height="96" rx="28" fill="url(#g)"/>'
        '<circle cx="48" cy="48" r="38" fill="#FFFFFF" opacity="0.14"/>'
        f'{inner_svg}'
        '</svg>'
    )
    b64 = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{b64}"


PRESET_AVATARS = [
    {
        "id": "duo_owl",
        "name": "多多猫头鹰",
        "url": _make_svg_data_uri(
            "#58CC02",
            "#2EAD00",
            # 双耳羽+圆脑袋+多邻国风格超大明眸
            '<path d="M24 31 L20 15 L36 25 Z" fill="#89E219"/>'
            '<path d="M72 31 L76 15 L60 25 Z" fill="#89E219"/>'
            '<ellipse cx="48" cy="54" rx="29" ry="27" fill="#58CC02"/>'
            '<ellipse cx="48" cy="69" rx="18" ry="11" fill="#D7FFB8" opacity="0.9"/>'
            '<path d="M38 66 Q42 71 46 66 Q50 71 54 66" fill="none" stroke="#58CC02" stroke-width="2.2" stroke-linecap="round"/>'
            '<circle cx="36" cy="46" r="13.5" fill="#FFFFFF"/>'
            '<circle cx="60" cy="46" r="13.5" fill="#FFFFFF"/>'
            '<circle cx="37.5" cy="46.5" r="7.5" fill="#1E293B"/>'
            '<circle cx="58.5" cy="46.5" r="7.5" fill="#1E293B"/>'
            '<circle cx="35" cy="43.5" r="2.8" fill="#FFFFFF"/>'
            '<circle cx="56" cy="43.5" r="2.8" fill="#FFFFFF"/>'
            '<circle cx="40.5" cy="49" r="1.3" fill="#FFFFFF" opacity="0.8"/>'
            '<circle cx="61.5" cy="49" r="1.3" fill="#FFFFFF" opacity="0.8"/>'
            '<ellipse cx="25" cy="58" rx="4.5" ry="2.6" fill="#FF7EB6" opacity="0.72"/>'
            '<ellipse cx="71" cy="58" rx="4.5" ry="2.6" fill="#FF7EB6" opacity="0.72"/>'
            '<path d="M42 53 Q48 50 54 53 Q50 62 48 62 Q46 62 42 53 Z" fill="#FFB800" stroke="#EA580C" stroke-width="1"/>',
        ),
    },
    {
        "id": "shiba",
        "name": "奶糖小柴犬",
        "url": _make_svg_data_uri(
            "#FF9F1C",
            "#EA580C",
            '<path d="M22 40 L20 15 L40 27 Z" fill="#E07A22"/>'
            '<path d="M24 35 L24 21 L35 28 Z" fill="#FDE68A"/>'
            '<path d="M74 40 L76 15 L56 27 Z" fill="#E07A22"/>'
            '<path d="M72 35 L72 21 L61 28 Z" fill="#FDE68A"/>'
            '<ellipse cx="48" cy="54" rx="28" ry="25" fill="#F59E0B"/>'
            '<path d="M21 56 Q34 45 48 53 Q62 45 75 56 Q72 78 48 79 Q24 78 21 56 Z" fill="#FFFBEB"/>'
            '<ellipse cx="36" cy="39" rx="3.8" ry="2.5" fill="#FFFBEB"/>'
            '<ellipse cx="60" cy="39" rx="3.8" ry="2.5" fill="#FFFBEB"/>'
            '<circle cx="35" cy="49" r="5" fill="#1E293B"/>'
            '<circle cx="61" cy="49" r="5" fill="#1E293B"/>'
            '<circle cx="33.5" cy="47.2" r="1.9" fill="#FFFFFF"/>'
            '<circle cx="59.5" cy="47.2" r="1.9" fill="#FFFFFF"/>'
            '<ellipse cx="27" cy="59" rx="4.6" ry="2.7" fill="#FB7185" opacity="0.65"/>'
            '<ellipse cx="69" cy="59" rx="4.6" ry="2.7" fill="#FB7185" opacity="0.65"/>'
            '<ellipse cx="48" cy="56.5" rx="4.5" ry="3.2" fill="#1E293B"/>'
            '<path d="M41 63 Q44.5 67 48 62 Q51.5 67 55 63" fill="none" stroke="#1E293B" stroke-width="2.2" stroke-linecap="round"/>'
            '<path d="M45.2 65 Q48 72 50.8 65 Z" fill="#FB7185"/>',
        ),
    },
    {
        "id": "panda",
        "name": "团团大熊猫",
        "url": _make_svg_data_uri(
            "#38BDF8",
            "#0284C7",
            '<circle cx="25" cy="28" r="10.5" fill="#1E293B"/>'
            '<circle cx="71" cy="28" r="10.5" fill="#1E293B"/>'
            '<ellipse cx="48" cy="54" rx="30" ry="26" fill="#FFFFFF"/>'
            '<ellipse cx="35" cy="50" rx="9" ry="11" transform="rotate(-18 35 50)" fill="#1E293B"/>'
            '<ellipse cx="61" cy="50" rx="9" ry="11" transform="rotate(18 61 50)" fill="#1E293B"/>'
            '<circle cx="36" cy="49" r="4.2" fill="#FFFFFF"/>'
            '<circle cx="60" cy="49" r="4.2" fill="#FFFFFF"/>'
            '<circle cx="36" cy="49" r="2.6" fill="#0F172A"/>'
            '<circle cx="60" cy="49" r="2.6" fill="#0F172A"/>'
            '<circle cx="35" cy="47.8" r="1.1" fill="#FFFFFF"/>'
            '<circle cx="59" cy="47.8" r="1.1" fill="#FFFFFF"/>'
            '<ellipse cx="26" cy="61" rx="4.8" ry="2.8" fill="#F472B6" opacity="0.68"/>'
            '<ellipse cx="70" cy="61" rx="4.8" ry="2.8" fill="#F472B6" opacity="0.68"/>'
            '<ellipse cx="48" cy="58" rx="4.8" ry="3.2" fill="#1E293B"/>'
            '<path d="M42 64 Q48 69 54 64" fill="none" stroke="#1E293B" stroke-width="2.2" stroke-linecap="round"/>',
        ),
    },
    {
        "id": "kitty",
        "name": "蜜桃小橘猫",
        "url": _make_svg_data_uri(
            "#F472B6",
            "#DB2777",
            '<path d="M22 41 L21 15 L41 28 Z" fill="#FDBA74"/>'
            '<path d="M24 36 L24 21 L36 28 Z" fill="#FBCFE8"/>'
            '<path d="M74 41 L75 15 L55 28 Z" fill="#FDBA74"/>'
            '<path d="M72 36 L72 21 L60 28 Z" fill="#FBCFE8"/>'
            '<ellipse cx="48" cy="55" rx="28" ry="24.5" fill="#FFEDD5"/>'
            '<path d="M48 30.5 V38 M42 31.5 L44 37 M54 31.5 L52 37" stroke="#FB923C" stroke-width="2.8" stroke-linecap="round"/>'
            '<circle cx="35.5" cy="51" r="5.3" fill="#1E293B"/>'
            '<circle cx="60.5" cy="51" r="5.3" fill="#1E293B"/>'
            '<circle cx="33.8" cy="49" r="2.1" fill="#FFFFFF"/>'
            '<circle cx="58.8" cy="49" r="2.1" fill="#FFFFFF"/>'
            '<ellipse cx="26.5" cy="60" rx="4.5" ry="2.6" fill="#FB7185" opacity="0.65"/>'
            '<ellipse cx="69.5" cy="60" rx="4.5" ry="2.6" fill="#FB7185" opacity="0.65"/>'
            '<polygon points="48,59 44.5,55.5 51.5,55.5" fill="#F43F5E"/>'
            '<path d="M42 63 Q45 66.5 48 62 Q51 66.5 54 63" fill="none" stroke="#334155" stroke-width="2" stroke-linecap="round"/>'
            '<path d="M16 55 L26 57 M16 61 L26 61 M80 55 L70 57 M80 61 L70 61" stroke="#94A3B8" stroke-width="1.6" stroke-linecap="round"/>',
        ),
    },
    {
        "id": "bunny",
        "name": "草莓垂耳兔",
        "url": _make_svg_data_uri(
            "#C084FC",
            "#9333EA",
            '<ellipse cx="29" cy="29" rx="7.5" ry="17" transform="rotate(-14 29 29)" fill="#FFFFFF"/>'
            '<ellipse cx="29" cy="30" rx="4.2" ry="11.5" transform="rotate(-14 29 30)" fill="#F9A8D4"/>'
            '<ellipse cx="67" cy="29" rx="7.5" ry="17" transform="rotate(14 67 29)" fill="#FFFFFF"/>'
            '<ellipse cx="67" cy="30" rx="4.2" ry="11.5" transform="rotate(14 67 30)" fill="#F9A8D4"/>'
            '<ellipse cx="48" cy="57" rx="27" ry="23.5" fill="#FFFFFF"/>'
            '<circle cx="36" cy="53" r="5" fill="#1E293B"/>'
            '<circle cx="60" cy="53" r="5" fill="#1E293B"/>'
            '<circle cx="34.5" cy="51.2" r="2" fill="#FFFFFF"/>'
            '<circle cx="58.5" cy="51.2" r="2" fill="#FFFFFF"/>'
            '<ellipse cx="27" cy="62" rx="4.8" ry="2.7" fill="#F472B6" opacity="0.65"/>'
            '<ellipse cx="69" cy="62" rx="4.8" ry="2.7" fill="#F472B6" opacity="0.65"/>'
            '<ellipse cx="48" cy="59.5" rx="3.4" ry="2.4" fill="#FB7185"/>'
            '<path d="M43 64 Q45.5 67.5 48 63.5 Q50.5 67.5 53 64" fill="none" stroke="#334155" stroke-width="2" stroke-linecap="round"/>',
        ),
    },
    {
        "id": "fox",
        "name": "机灵小赤狐",
        "url": _make_svg_data_uri(
            "#FB7185",
            "#E11D48",
            '<path d="M21 41 L18 14 L41 27 Z" fill="#EA580C"/>'
            '<path d="M23 36 L22 20 L35 28 Z" fill="#FFF7ED"/>'
            '<path d="M75 41 L78 14 L55 27 Z" fill="#EA580C"/>'
            '<path d="M73 36 L74 20 L61 28 Z" fill="#FFF7ED"/>'
            '<ellipse cx="48" cy="54" rx="28" ry="24.5" fill="#F97316"/>'
            '<path d="M20 55 Q33 50 48 60 Q63 50 76 55 Q72 78 48 79 Q24 78 20 55 Z" fill="#FFFFFF"/>'
            '<circle cx="35.5" cy="49" r="5" fill="#1E293B"/>'
            '<circle cx="60.5" cy="49" r="5" fill="#1E293B"/>'
            '<circle cx="34" cy="47.2" r="2" fill="#FFFFFF"/>'
            '<circle cx="59" cy="47.2" r="2" fill="#FFFFFF"/>'
            '<ellipse cx="27" cy="59.5" rx="4.3" ry="2.5" fill="#FB7185" opacity="0.65"/>'
            '<ellipse cx="69" cy="59.5" rx="4.3" ry="2.5" fill="#FB7185" opacity="0.65"/>'
            '<circle cx="48" cy="59.5" r="3.6" fill="#0F172A"/>'
            '<path d="M43 65 Q48 69 53 65" fill="none" stroke="#334155" stroke-width="2" stroke-linecap="round"/>',
        ),
    },
    {
        "id": "capybara",
        "name": "佛系卡皮巴拉",
        "url": _make_svg_data_uri(
            "#FBBF24",
            "#D97706",
            '<circle cx="25" cy="36" r="6.5" fill="#92400E"/>'
            '<circle cx="71" cy="36" r="6.5" fill="#92400E"/>'
            '<ellipse cx="48" cy="56" rx="30" ry="24" fill="#B45309"/>'
            '<ellipse cx="48" cy="61" rx="18" ry="13.5" fill="#D97706"/>'
            '<path d="M31 48 Q36 51 41 48" fill="none" stroke="#1E293B" stroke-width="2.8" stroke-linecap="round"/>'
            '<path d="M55 48 Q60 51 65 48" fill="none" stroke="#1E293B" stroke-width="2.8" stroke-linecap="round"/>'
            '<ellipse cx="27" cy="57" rx="4.5" ry="2.5" fill="#FB7185" opacity="0.58"/>'
            '<ellipse cx="69" cy="57" rx="4.5" ry="2.5" fill="#FB7185" opacity="0.58"/>'
            '<ellipse cx="48" cy="56.5" rx="5.2" ry="3.4" fill="#451A03"/>'
            '<path d="M48 60 V64 M43 66 Q48 69 53 66" fill="none" stroke="#451A03" stroke-width="2" stroke-linecap="round"/>'
            # 头顶一只多邻国感超可爱小橘子
            '<circle cx="48" cy="25" r="7.5" fill="#FF6B00"/>'
            '<path d="M48 18 Q53 14 55 18 Q51 20 48 18 Z" fill="#22C55E"/>',
        ),
    },
    {
        "id": "penguin",
        "name": "冰川小企鹅",
        "url": _make_svg_data_uri(
            "#2DD4BF",
            "#0D9488",
            '<ellipse cx="48" cy="54" rx="28" ry="27" fill="#1E3A8A"/>'
            '<path d="M24 57 C24 41 36 37 48 44 C60 37 72 41 72 57 C72 73 61 80 48 80 C35 80 24 73 24 57 Z" fill="#FFFFFF"/>'
            '<circle cx="36" cy="50" r="5" fill="#0F172A"/>'
            '<circle cx="60" cy="50" r="5" fill="#0F172A"/>'
            '<circle cx="34.5" cy="48.2" r="2" fill="#FFFFFF"/>'
            '<circle cx="58.5" cy="48.2" r="2" fill="#FFFFFF"/>'
            '<ellipse cx="28" cy="59" rx="4.5" ry="2.6" fill="#FB7185" opacity="0.65"/>'
            '<ellipse cx="68" cy="59" rx="4.5" ry="2.6" fill="#FB7185" opacity="0.65"/>'
            '<path d="M41 56 Q48 53 55 56 Q52 64 48 64 Q44 64 41 56 Z" fill="#FBBF24" stroke="#EA580C" stroke-width="1"/>',
        ),
    },
]

RANDOM_NICKNAMES = [
    "数据捕手",
    "冲榜小火箭",
    "稳健操盘手",
    "早八满绩王",
    "战报指挥官",
    "丝滑传送门",
    "零失误选手",
    "金牌统计官",
    "高效摸鱼侠",
    "发群先锋官",
    "汇总小管家",
    "晴空逐浪者",
    "神速快捷键",
    "表格魔法师",
    "夜航守表人",
    "全能多面手",
]

PERMISSION_META = [
    ("can_run", "允许启动跑表与截图", "控制是否允许点击开始全流程、截图或开启定时值守"),
    ("can_edit_targets", "允许改钉钉群与文案后缀", "控制目标群聊、发送时间样式与后缀规则的修改权"),
    ("can_edit_cells", "允许改表格自定义单元格", "控制是否能增删或修改 D3/D4/E4/D5/L3/Y3/AA5/R4 等单元格规则"),
    ("can_edit_schedule", "允许修改定时自动排期", "控制运行周期、白夜时段和单点计划覆盖的修改权"),
    ("can_edit_accounts", "允许修改WPS链接与账号", "保护源数据后台账号密码与在线表格链接不被误改"),
    ("can_edit_logic", "允许改等待秒数与内部顺序", "控制粘贴等待秒数、发后最小化与模块执行顺序"),
    ("can_use_ai", "允许使用内置 AI 顾问", "控制首页 AI 对话总结与智能复盘功能"),
]

DEFAULT_PERMISSIONS = {k: True for k, _t, _d in PERMISSION_META}


MAX_HUB_BODY_BYTES = 2 * 1024 * 1024
MAX_DECOMPRESSED_BYTES = 4 * 1024 * 1024
ADMIN_MAX_FAILS = 5
ADMIN_LOCKOUT_SEC = 60.0
ACCOUNT_MAX_FAILS = 5
ACCOUNT_LOCKOUT_SEC = 60.0
SYNC_PULL_COOLDOWN_SEC = 2.0
AUTO_CLOUD_PULL_COOLDOWN_SEC = 18.0
_AUTO_CLOUD_TOPIC = "sm28_th_" + hmac.new(
    b"SheetMate-HubRoom",
    b"SheetMate-AutoHub-v281::DefaultTeamWorkspace",
    hashlib.sha256,
).hexdigest()[:22]
_AUTO_CLOUD_KEY = hashlib.pbkdf2_hmac(
    "sha256",
    b"SheetMate-TeamHub-E2EE-Key",
    b"SheetMate-Salt-v281",
    2000,
    dklen=32,
)
_AUTO_CLOUD_BROKERS = (
    ("broker-cn.emqx.io", 8883),
    ("broker.emqx.io", 8883),
)


def _th_mqtt_enc_varlen(n):
    out = bytearray()
    while True:
        b = n % 128
        n //= 128
        if n > 0:
            b |= 0x80
        out.append(b)
        if n == 0:
            break
    return bytes(out)


def _th_mqtt_str(s):
    b = str(s or "").encode("utf-8")
    return struct.pack("!H", len(b)) + b


def _th_mqtt_recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("EOF")
        buf.extend(chunk)
    return bytes(buf)


def _th_mqtt_read_packet(sock):
    hdr = _th_mqtt_recv_exact(sock, 1)[0]
    mult = 1
    rem = 0
    for _ in range(4):
        b = _th_mqtt_recv_exact(sock, 1)[0]
        rem += (b & 0x7F) * mult
        if (b & 0x80) == 0:
            break
        mult *= 128
    if rem > MAX_HUB_BODY_BYTES:
        raise ValueError("MQTT payload too large")
    body = _th_mqtt_recv_exact(sock, rem) if rem else b""
    return hdr, body


def _th_mqtt_retained_pull(topic, timeout=2.2):
    ctx = ssl.create_default_context()
    for host, port in _AUTO_CLOUD_BROKERS:
        try:
            with socket.create_connection((host, port), timeout=timeout) as raw_sock:
                with ctx.wrap_socket(raw_sock, server_hostname=host) as ss:
                    ss.settimeout(timeout)
                    cid = f"smth_r_{os.getpid()}_{secrets.token_hex(3)}"
                    vh = _th_mqtt_str("MQTT") + bytes([4, 2, 0, 20])
                    pl = _th_mqtt_str(cid)
                    ss.sendall(b"\x10" + _th_mqtt_enc_varlen(len(vh) + len(pl)) + vh + pl)
                    hdr0, _ = _th_mqtt_read_packet(ss)
                    if (hdr0 & 0xF0) != 0x20:
                        continue
                    s_vh = struct.pack("!H", 1)
                    s_pl = _th_mqtt_str(topic) + b"\x00"
                    ss.sendall(b"\x82" + _th_mqtt_enc_varlen(len(s_vh) + len(s_pl)) + s_vh + s_pl)
                    ss.settimeout(0.55)
                    deadline = time.monotonic() + 0.65
                    while time.monotonic() < deadline:
                        try:
                            hdr, body = _th_mqtt_read_packet(ss)
                        except (socket.timeout, TimeoutError, ConnectionError, OSError):
                            break
                        if (hdr & 0xF0) == 0x30 and len(body) >= 2:
                            tlen = struct.unpack("!H", body[:2])[0]
                            pos = 2 + tlen + (2 if (hdr & 0x06) != 0 else 0)
                            if pos <= len(body):
                                try:
                                    ss.sendall(b"\xe0\x00")
                                except Exception:
                                    pass
                                return body[pos:].decode("utf-8", errors="ignore")
                    # 首个健康节点已顺利连上但云端尚无保留消息，直接结束以免串行空等第二个节点
                    try:
                        ss.sendall(b"\xe0\x00")
                    except Exception:
                        pass
                    return None
        except Exception:
            continue
    return None


def _th_mqtt_retained_push(topic, text, timeout=3.0):
    ctx = ssl.create_default_context()
    data_b = str(text or "").encode("utf-8")
    vh_p = _th_mqtt_str(topic)
    pub_pkt = b"\x31" + _th_mqtt_enc_varlen(len(vh_p) + len(data_b)) + vh_p + data_b
    ok_any = False
    for host, port in _AUTO_CLOUD_BROKERS:
        try:
            with socket.create_connection((host, port), timeout=timeout) as raw_sock:
                with ctx.wrap_socket(raw_sock, server_hostname=host) as ss:
                    ss.settimeout(timeout)
                    cid = f"smth_w_{os.getpid()}_{secrets.token_hex(3)}"
                    vh = _th_mqtt_str("MQTT") + bytes([4, 2, 0, 20])
                    pl = _th_mqtt_str(cid)
                    ss.sendall(b"\x10" + _th_mqtt_enc_varlen(len(vh) + len(pl)) + vh + pl)
                    hdr0, _ = _th_mqtt_read_packet(ss)
                    if (hdr0 & 0xF0) != 0x20:
                        continue
                    ss.sendall(pub_pkt)
                    ss.sendall(b"\xe0\x00")
                    ok_any = True
                    break
        except Exception:
            continue
    return ok_any
def _safe_zlib_decompress(data, max_bytes=MAX_DECOMPRESSED_BYTES):
    """带最大解压字节上限的安全解压，阻断压缩炸弹造成内存溢出。"""
    dobj = zlib.decompressobj()
    out = dobj.decompress(data, max_bytes)
    if dobj.unconsumed_tail:
        raise ValueError("Decompressed payload exceeds limit")
    tail = dobj.flush(max(1, max_bytes - len(out) + 1))
    if len(out) + len(tail) > max_bytes:
        raise ValueError("Decompressed payload exceeds limit")
    return out + tail


def _acct_mqtt_pull_all(topic, timeout=4.5):
    """并发从国内/海外双公共云节点拉取账号加密保留包，返回所有读到的密文列表。"""
    results = []
    res_lock = threading.Lock()
    ctx = ssl.create_default_context()

    def _pull_one(host, port):
        try:
            with socket.create_connection((host, port), timeout=timeout) as raw_sock:
                with ctx.wrap_socket(raw_sock, server_hostname=host) as ss:
                    ss.settimeout(timeout)
                    cid = f"sma_r_{os.getpid()}_{secrets.token_hex(3)}"
                    vh = _th_mqtt_str("MQTT") + bytes([4, 2, 0, 20])
                    pl = _th_mqtt_str(cid)
                    ss.sendall(b"\x10" + _th_mqtt_enc_varlen(len(vh) + len(pl)) + vh + pl)
                    hdr0, _ = _th_mqtt_read_packet(ss)
                    if (hdr0 & 0xF0) != 0x20:
                        return
                    s_vh = struct.pack("!H", 1)
                    s_pl = _th_mqtt_str(topic) + b"\x00"
                    ss.sendall(b"\x82" + _th_mqtt_enc_varlen(len(s_vh) + len(s_pl)) + s_vh + s_pl)
                    ss.settimeout(1.6)
                    deadline = time.monotonic() + 1.9
                    while time.monotonic() < deadline:
                        try:
                            hdr, body = _th_mqtt_read_packet(ss)
                        except (socket.timeout, TimeoutError, ConnectionError, OSError):
                            break
                        if (hdr & 0xF0) == 0x30 and len(body) >= 2:
                            tlen = struct.unpack("!H", body[:2])[0]
                            pos = 2 + tlen + (2 if (hdr & 0x06) != 0 else 0)
                            if pos <= len(body):
                                txt = body[pos:].decode("utf-8", errors="ignore").strip()
                                if txt:
                                    with res_lock:
                                        results.append(txt)
                                break
                    try:
                        ss.sendall(b"\xe0\x00")
                    except Exception:
                        pass
        except Exception:
            pass

    ths = [threading.Thread(target=_pull_one, args=(h, p), daemon=True) for h, p in _AUTO_CLOUD_BROKERS]
    for th in ths:
        th.start()
    for th in ths:
        th.join(timeout=timeout + 2.5)
    return results


def _acct_mqtt_push_all(topic, text, timeout=5.0):
    """并发将账号加密保留包推送至双节点云服务（QoS 1 确认落盘），确保跨地域登录即得最新配置。"""
    data_b = str(text or "").encode("utf-8")
    vh_p = _th_mqtt_str(topic) + struct.pack("!H", 1)
    pub_pkt = b"\x33" + _th_mqtt_enc_varlen(len(vh_p) + len(data_b)) + vh_p + data_b
    ctx = ssl.create_default_context()
    ok_flags = []
    ok_lock = threading.Lock()

    def _push_one(host, port):
        try:
            with socket.create_connection((host, port), timeout=timeout) as raw_sock:
                with ctx.wrap_socket(raw_sock, server_hostname=host) as ss:
                    ss.settimeout(timeout)
                    cid = f"sma_w_{os.getpid()}_{secrets.token_hex(3)}"
                    vh = _th_mqtt_str("MQTT") + bytes([4, 2, 0, 20])
                    pl = _th_mqtt_str(cid)
                    ss.sendall(b"\x10" + _th_mqtt_enc_varlen(len(vh) + len(pl)) + vh + pl)
                    hdr0, _ = _th_mqtt_read_packet(ss)
                    if (hdr0 & 0xF0) != 0x20:
                        return
                    ss.sendall(pub_pkt)
                    hdr1, _ = _th_mqtt_read_packet(ss)
                    if (hdr1 & 0xF0) == 0x40:
                        with ok_lock:
                            ok_flags.append(True)
                    try:
                        ss.sendall(b"\xe0\x00")
                    except Exception:
                        pass
        except Exception:
            pass

    ths = [threading.Thread(target=_push_one, args=(h, p), daemon=True) for h, p in _AUTO_CLOUD_BROKERS]
    for th in ths:
        th.start()
    for th in ths:
        th.join(timeout=timeout + 2.5)
    return bool(ok_flags)


_SAFE_AVATAR_RE = re.compile(r"^data:image/(?:png|jpeg|jpg|webp|svg\+xml);base64,[A-Za-z0-9+/=]+$", re.ASCII)


def upgrade_legacy_avatar(raw_url):
    """若头像为空或为旧版几何图标 SVG，自动无缝升级为新款多邻国风萌宠头像；自定义照片保持不变。"""
    s = "".join(str(raw_url or "").split())
    if not s:
        return PRESET_AVATARS[0]["url"]
    if s.startswith("data:image/svg+xml;base64,"):
        try:
            decoded = base64.b64decode(s.split(",", 1)[1]).decode("utf-8", errors="ignore")
            if _PET_V2_MARKER not in decoded:
                if "M52 18L28 52" in decoded:
                    return PRESET_AVATARS[0]["url"]
                idx = hashlib.sha256(decoded.encode("utf-8")).digest()[0] % len(PRESET_AVATARS)
                return PRESET_AVATARS[idx]["url"]
        except Exception:
            return PRESET_AVATARS[0]["url"]
    return raw_url


def sanitize_avatar_url(raw_url):
    """严格校验头像 URI，只允许纯净 Base64 图片载荷，阻断存储型 XSS 注入。"""
    s = "".join(str(raw_url or "").split())
    if s and len(s) <= 260000 and _SAFE_AVATAR_RE.match(s):
        return s
    return PRESET_AVATARS[0]["url"] if PRESET_AVATARS else ""


def _is_ephemeral_test_dir(path_obj):
    try:
        base_r = Path(path_obj).resolve()
        tmp_r = Path(tempfile.gettempdir()).resolve()
        return base_r == tmp_r or tmp_r in base_r.parents
    except Exception:
        return False


def _auto_cloud_pack(payload_dict):
    raw = json.dumps(payload_dict, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    comp = zlib.compress(raw, level=6)
    nonce = secrets.token_bytes(12)
    ks = bytearray()
    idx = 0
    while len(ks) < len(comp):
        ks.extend(hashlib.sha256(_AUTO_CLOUD_KEY + nonce + idx.to_bytes(4, "big")).digest())
        idx += 1
    ct = bytes(a ^ b for a, b in zip(comp, ks[:len(comp)]))
    mac = hmac.new(_AUTO_CLOUD_KEY, nonce + ct, hashlib.sha256).digest()[:16]
    return "SMH28:" + base64.b64encode(nonce + mac + ct).decode("ascii")


def _auto_cloud_unpack(token_str):
    s = str(token_str or "").strip()
    if not s.startswith("SMH28:"):
        return None
    try:
        raw = base64.b64decode(s[6:].encode("ascii"), validate=True)
    except Exception:
        return None
    if len(raw) < 28:
        return None
    nonce, mac, ct = raw[:12], raw[12:28], raw[28:]
    exp = hmac.new(_AUTO_CLOUD_KEY, nonce + ct, hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(mac, exp):
        return None
    ks = bytearray()
    idx = 0
    while len(ks) < len(ct):
        ks.extend(hashlib.sha256(_AUTO_CLOUD_KEY + nonce + idx.to_bytes(4, "big")).digest())
        idx += 1
    comp = bytes(a ^ b for a, b in zip(ct, ks[:len(ct)]))
    try:
        obj = json.loads(_safe_zlib_decompress(comp).decode("utf-8"))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def safe_read_json_body(rfile, headers, max_bytes=MAX_HUB_BODY_BYTES):
    """带体积上限与格式防御的安全 JSON 请求体读取函数。"""
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


_SENSITIVE_CFG_KEYS = frozenset({"luya_pass", "api_key", "gist_token", "hub_secret"})


def redact_sensitive_config(obj):
    """递归深拷贝配置或成员快照并剥离密码与 API 密钥，防止云端与反馈载荷泄密。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in _SENSITIVE_CFG_KEYS:
                out[k] = ""
            else:
                out[k] = redact_sensitive_config(v)
        return out
    if isinstance(obj, list):
        return [redact_sensitive_config(x) for x in obj]
    return copy.deepcopy(obj)


def _prune_fail_tracker(tracker, now, max_entries=256):
    """清理过期的登录失败锁记录并限制追踪字典上限，防止恶意枚举导致内存泄漏。"""
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


def _sha256_secret(val):
    return hashlib.sha256(f"SheetMateHub::{val}".encode("utf-8")).hexdigest()


_DEFAULT_ADMIN_PASSCODE_HASH = "6ff431d101324476ed36a697cb43b4a46b8c74118d3ad950e8415ec40f36ade9"
_DEFAULT_ADMIN_PASSWORD_HASH = "6011f04699646c70fc2b2e861b8ba0a570b8c2f209f900a09a14abfb7951c1b1"
_COMPAT_ADMIN_PASSWORD_HASHES = (
    "6011f04699646c70fc2b2e861b8ba0a570b8c2f209f900a09a14abfb7951c1b1",
    "41492bcbcf0beda3e0ab90a0db689a19129de809d2cb26f9083e29d314fb80d8",
)
_LEGACY_ADMIN_PASSCODE_HASH = "37a491493857d5f0dde3bd6af4520c2bc584cce05ea4bd5647df2640a5ad6eb2"
_LEGACY_ADMIN_PASSWORD_HASHES = (
    "1639db4be408463baedd13400614163c8f1399c441c2fe90f950a8ffc43c9e33",
)


def _norm_admin_passcode(val):
    s = unicodedata.normalize("NFKC", str(val or ""))
    return " ".join(s.strip().split())


def _norm_admin_password(val):
    return unicodedata.normalize("NFKC", str(val or "")).strip()


def normalize_nickname_key(name):
    """将昵称按 NFKC 统一并剥离空白后折叠大小写，用于全局唯一性防重比对。"""
    s = unicodedata.normalize("NFKC", str(name or ""))
    return "".join(ch for ch in s if not ch.isspace()).casefold()


def pick_random_nickname(exclude="", forbidden_keys=None):
    ex_key = normalize_nickname_key(exclude)
    blocked = set(forbidden_keys or ())
    if ex_key:
        blocked.add(ex_key)
    pool = [n for n in RANDOM_NICKNAMES if normalize_nickname_key(n) not in blocked]
    if pool:
        return random.choice(pool)
    for _ in range(30):
        cand = f"{random.choice(RANDOM_NICKNAMES)}{random.randint(11, 99)}"
        if normalize_nickname_key(cand) not in blocked:
            return cand
    return f"{random.choice(RANDOM_NICKNAMES)}{random.randint(100, 999)}"


def normalize_permissions(raw):
    out = dict(DEFAULT_PERMISSIONS)
    if isinstance(raw, dict):
        for k in DEFAULT_PERMISSIONS:
            if k in raw:
                out[k] = bool(raw[k])
    return out


def check_config_edit_permission(old_cfg, new_cfg, perms):
    """核对成员是否有权修改变更的配置区块；无权改动则返回错误说明与受保护回退配置。"""
    import copy
    p = normalize_permissions(perms)
    old_c = copy.deepcopy(old_cfg) if isinstance(old_cfg, dict) else {}
    new_c = copy.deepcopy(new_cfg) if isinstance(new_cfg, dict) else {}

    groups = [
        ("can_edit_targets", "修改钉钉群与消息文案设置", (
            "enable_dingtalk", "dt_auto_send", "dt_clear_before_send", "dt_silent_send",
            "dt_search", "dt_targets", "dt_suffix", "dt_time_mode", "dt_date_fmt",
            "dt_day_offset", "dt_cutoff_enabled", "dt_cutoff_hm", "dt_before_cutoff_offset",
            "suffix_rules",
        )),
        ("can_edit_cells", "修改表格截图与自定义单元格规则", (
            "enable_capture", "summary_presets", "update_time_cell",
        )),
        ("can_edit_schedule", "修改定时自动排期计划", (
            "day_mode", "custom_days", "day", "night", "night_enabled", "point_overrides", "capture_plan",
        )),
        ("can_edit_accounts", "修改 WPS 链接或 Luya 后台账号密码", (
            "wps_url", "luya_user", "luya_pass",
        )),
        ("can_edit_logic", "调整粘贴与发前等待秒数、防撞与内部模块顺序", (
            "dt_paste_delay_sec", "dt_send_wait_sec", "dt_minimize_after_send", "pipeline_tuning", "open_wps_after_run",
            "prevent_sleep", "save_shot_history",
        )),
        ("can_use_ai", "修改内置 AI 设置", ("ai_assistant",)),
    ]

    denied = []
    safe_cfg = copy.deepcopy(new_c)
    for perm_key, label, fields in groups:
        if p.get(perm_key, True):
            continue
        for f in fields:
            if f in old_c and safe_cfg.get(f) != old_c.get(f):
                safe_cfg[f] = copy.deepcopy(old_c.get(f))
                if label not in denied:
                    denied.append(label)
    return (len(denied) == 0), denied, safe_cfg


def audit_member_config(cfg):
    """扫描成员当前配置，用大白话标出『哪里设置不对』及如何改。"""
    c = cfg if isinstance(cfg, dict) else {}
    issues = []
    if not c.get("enable_dingtalk", False):
        issues.append({
            "level": "warn",
            "item": "未开启「自动发送到钉钉」",
            "reason": "跑完表格并截图后不会自动发到钉钉群里。",
            "fix": "去【钉钉投递】打开「自动发送到钉钉」开关。",
        })
    if c.get("enable_dingtalk", False) and not c.get("dt_auto_send", False):
        issues.append({
            "level": "warn",
            "item": "未开启「粘贴后自动发出」",
            "reason": "贴好图片和文字后软件会停住等人工敲回车，定时无人值守发不出去。",
            "fix": "若需无人自动发群，打开「粘贴后自动发出」。",
        })
    if c.get("dt_minimize_after_send", True):
        issues.append({
            "level": "warn",
            "item": "开启了「发送后最小化钉钉」",
            "reason": "第1次发完收起后，第2次重新唤醒最容易导致聊天框未加载完而漏贴或卡住。",
            "fix": "强烈建议关闭「发送后最小化钉钉」，让钉钉自然留在桌面即可。",
        })
    try:
        p_delay = int(c.get("dt_paste_delay_sec", 3))
    except Exception:
        p_delay = 3
    if p_delay < 2:
        issues.append({
            "level": "warn",
            "item": f"打开会话后等待仅 {p_delay} 秒过短",
            "reason": "钉钉加载群消息或大图时来不及接收粘贴，容易漏图。",
            "fix": "建议把「打开会话后等待几秒再粘贴」调到 3~5 秒。",
        })
    targets = c.get("dt_targets") or []
    has_active_tgt = any(isinstance(t, dict) and t.get("enabled", True) and str(t.get("name") or "").strip() for t in targets)
    if not has_active_tgt and not str(c.get("dt_search") or "").strip():
        issues.append({
            "level": "err",
            "item": "未设置有效钉钉目标会话",
            "reason": "一个启用的目标群聊/联系人都没有，钉钉发送步会被直接跳过。",
            "fix": "去【钉钉投递】至少开启或添加 1 个目标群聊。",
        })
    if c.get("day_mode") == "custom" and not (c.get("custom_days") or []):
        issues.append({
            "level": "err",
            "item": "自定义运行星期为空",
            "reason": "周期选了「自定义」但周一到周日一个都没勾，定时计划永远不会启动。",
            "fix": "去【定时计划】勾上需要运行的星期。",
        })
    if not str(c.get("wps_url") or "").strip() or not str(c.get("luya_user") or "").strip():
        issues.append({
            "level": "err",
            "item": "WPS 表格链接或后台账号为空",
            "reason": "缺少抓取或写入目标地址，启动全流程会直接失败。",
            "fix": "去【记录账号】补全 WPS 链接与 Luya 账号密码。",
        })
    return issues


def _parse_dt_str(s):
    txt = str(s or "").strip()
    if not txt:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(txt[:len(fmt) + 2 if "T" not in fmt else 16], fmt)
        except Exception:
            pass
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:\s+(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?)?", txt)
    if m:
        try:
            return datetime(
                int(m.group(1)), int(m.group(2)), int(m.group(3)),
                int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0),
            )
        except Exception:
            return None
    return None


class TeamHubManager:
    """线程安全的本地 + 共享盘 + 跨网 HTTP/Gist 多人锁与超级管理员中心控制器。"""

    def __init__(self, base_dir, app=None):
        self.base_dir = Path(base_dir)
        self.app = app
        self._lock = threading.RLock()
        self._profile_path = self.base_dir / "member_profile.json"
        self._state_path = self.base_dir / "team_hub_state.json"
        self._server = None
        self._server_port = 0
        self._admin_tokens = {}  # token -> expire_ts
        self._admin_fail_tracker = {}
        self._last_pull_ts = 0.0
        self._last_auto_pull_ts = 0.0
        self._lan_ips_cache = (0.0, ["127.0.0.1"])
        self._auto_cloud_enabled = not _is_ephemeral_test_dir(self.base_dir)
        self._push_lock = threading.Lock()
        self._push_thread = None
        self._push_dirty = False
        self._pull_lock = threading.Lock()
        self._pull_thread = None
        self._profile = self._load_or_init_profile()
        self._state = self._load_state_raw()
        self._ensure_state_schema()
        self._backfill_local_runs_once()
        self.account_sync = AccountCloudSyncManager(self.base_dir, hub=self)
        self._maybe_start_center_server()
        if self._auto_cloud_enabled:
            self._schedule_async_remote_pull(force=True)

    def _load_or_init_profile(self):
        if self._profile_path.exists():
            try:
                data = json.loads(self._profile_path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("user_id"):
                    data.setdefault("member_code", f"成员-{str(data['user_id'])[:4].upper()}")
                    data.setdefault("nickname", "")
                    old_av = data.get("avatar") or ""
                    new_av = sanitize_avatar_url(upgrade_legacy_avatar(old_av))
                    data["avatar"] = new_av
                    data.setdefault("initialized", bool(data.get("nickname")))
                    if new_av != old_av:
                        try:
                            self._profile_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                        except Exception:
                            pass
                    return data
            except Exception:
                pass
        uid = uuid.uuid4().hex[:12]
        code = f"成员-{uid[:4].upper()}"
        prof = {
            "user_id": uid,
            "member_code": code,
            "nickname": "",
            "avatar": PRESET_AVATARS[0]["url"],
            "initialized": False,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        try:
            self._profile_path.parent.mkdir(parents=True, exist_ok=True)
            self._profile_path.write_text(json.dumps(prof, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        return prof

    def _save_profile(self):
        try:
            self._profile_path.write_text(json.dumps(self._profile, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def get_profile(self):
        with self._lock:
            p = dict(self._profile)
            up = sanitize_avatar_url(upgrade_legacy_avatar(p.get("avatar")))
            if up != p.get("avatar"):
                self._profile["avatar"] = up
                self._save_profile()
            p["avatar"] = up
            p["display_name"] = p.get("nickname") or f"未命名·{p.get('member_code', '编号001')}"
            return p

    def _collect_taken_nickname_keys(self, exclude_uid=None):
        taken = {}
        members = self._state.get("members") or {}
        for uid, mem in members.items():
            if exclude_uid and str(uid) == str(exclude_uid):
                continue
            if not isinstance(mem, dict):
                continue
            for raw_n in (mem.get("nickname"), mem.get("display_name")):
                k = normalize_nickname_key(raw_n)
                if k:
                    taken[k] = str(mem.get("display_name") or raw_n or uid)
        return taken

    def check_nickname_available(self, nickname, exclude_uid=None):
        """跨本机与协同共享池检查昵称是否全局唯一且合法。"""
        nm = str(nickname or "").strip()[:24]
        key = normalize_nickname_key(nm)
        if not key:
            return False, "名字不能为空哦，请输入一个好记的名字或点随机推荐"
        reserved = {"管理员", "超级管理员", "admin", "root", "系统", "所有人", "全员", "未命名"}
        if key in reserved or nm.startswith("未命名·") or nm.startswith("未命名-"):
            return False, f"「{nm}」为系统保留称呼，请换一个专属名字吧"
        with self._lock:
            self._sync_pull_if_needed(force=True)
            ex_id = exclude_uid or self._profile.get("user_id")
            taken = self._collect_taken_nickname_keys(exclude_uid=ex_id)
            if key in taken:
                return False, f"名字「{nm}」已经被其他小伙伴占用啦，每个人的名字不能重复，请换一个或点旁边随机挑一个吧！"
        return True, "名字可用"

    def pick_unique_random_nickname(self, exclude=""):
        """结合全局成员花名册推荐一个当前未被人占用的昵称。"""
        with self._lock:
            self._sync_pull_if_needed()
            taken = self._collect_taken_nickname_keys(exclude_uid=self._profile.get("user_id"))
        return pick_random_nickname(exclude=exclude, forbidden_keys=set(taken.keys()))

    def get_member_roster_names(self):
        """导出团队已注册成员花名册（脱敏，供 AI 人名消歧与手机端按昵称校验使用）。"""
        prof = self.get_profile()
        with self._lock:
            self._sync_pull_if_needed()
            members = self._state.get("members") or {}
            out = []
            seen_ids = set()
            for uid, mem in members.items():
                if not isinstance(mem, dict):
                    continue
                seen_ids.add(str(uid))
                out.append({
                    "user_id": str(uid),
                    "nickname": str(mem.get("nickname") or "").strip(),
                    "display_name": str(mem.get("display_name") or mem.get("nickname") or "").strip(),
                    "remark": str(mem.get("admin_remark") or "").strip(),
                })
            my_uid = str(prof.get("user_id") or "")
            if my_uid and my_uid not in seen_ids:
                out.insert(0, {
                    "user_id": my_uid,
                    "nickname": str(prof.get("nickname") or "").strip(),
                    "display_name": str(prof.get("display_name") or "").strip(),
                    "remark": "",
                })
            return out

    def find_member_by_nickname(self, name):
        """按全局唯一昵称查找成员档案（优先命中本机当前用户）。"""
        key = normalize_nickname_key(name)
        if not key:
            return None
        prof = self.get_profile()
        my_nick = str(prof.get("nickname") or "").strip()
        if my_nick and not my_nick.startswith("未命名") and key in (
            normalize_nickname_key(my_nick),
            normalize_nickname_key(prof.get("display_name")),
        ):
            return {**prof, "is_local": True}
        with self._lock:
            self._sync_pull_if_needed()
            for uid, mem in (self._state.get("members") or {}).items():
                if not isinstance(mem, dict):
                    continue
                mn = str(mem.get("nickname") or "").strip()
                md = str(mem.get("display_name") or "").strip()
                if key in (normalize_nickname_key(mn), normalize_nickname_key(md)):
                    return {
                        "user_id": str(uid),
                        "nickname": mn,
                        "display_name": md or mn,
                        "avatar": mem.get("avatar") or PRESET_AVATARS[0]["url"],
                        "is_local": (str(uid) == str(prof.get("user_id") or "")),
                    }
        return None

    def update_profile(self, nickname=None, avatar=None, skip_name=False):
        with self._lock:
            cur_prof = self.get_profile()
            my_uid = cur_prof.get("user_id")
            if nickname is None and not skip_name:
                if avatar is not None and str(avatar).strip():
                    self._profile["avatar"] = sanitize_avatar_url(avatar)
                else:
                    self._profile["avatar"] = sanitize_avatar_url(self._profile.get("avatar"))
                self._save_profile()
                prof = self.get_profile()
            else:
                if skip_name or (nickname is not None and not str(nickname).strip()):
                    self._sync_pull_if_needed(force=True)
                    taken = self._collect_taken_nickname_keys(exclude_uid=my_uid)
                    base_nm = f"未命名·{self._profile['member_code']}"
                    cand = base_nm
                    suffix_i = 2
                    while normalize_nickname_key(cand) in taken:
                        cand = f"{base_nm}-{suffix_i}"
                        suffix_i += 1
                    self._profile["nickname"] = cand
                    self._profile["initialized"] = True
                elif nickname is not None:
                    nm = str(nickname).strip()[:24]
                    ok_av, msg_av = self.check_nickname_available(nm, exclude_uid=my_uid)
                    if not ok_av:
                        return {
                            "ok": False,
                            "duplicate": True,
                            "message": msg_av,
                            "profile": cur_prof,
                            **cur_prof,
                        }
                    self._profile["nickname"] = nm
                    self._profile["initialized"] = True
                if avatar is not None and str(avatar).strip():
                    self._profile["avatar"] = sanitize_avatar_url(avatar)
                else:
                    self._profile["avatar"] = sanitize_avatar_url(self._profile.get("avatar"))
                self._save_profile()
                prof = self.get_profile()
        self.sync_member_snapshot(status_level="idle", step_text="更新了个人名片")
        ok_msg = "已更换头像" if (nickname is None and not skip_name) else f"已保存您的专属名字：{prof.get('display_name', '')}"
        return {
            "ok": True,
            "duplicate": False,
            "message": ok_msg,
            "profile": prof,
            **prof,
        }

    def apply_synced_profile(self, nickname=None, avatar=None, initialized=None, reset_guest=False):
        """用于账号云同步登录或退出时，免重名冲突地切换本地成员名片与头像。"""
        with self._lock:
            if reset_guest:
                self._profile["nickname"] = ""
                self._profile["avatar"] = PRESET_AVATARS[0]["url"] if PRESET_AVATARS else ""
                self._profile["initialized"] = False
            else:
                if nickname is not None:
                    nm = str(nickname or "").strip()[:24]
                    if nm and not nm.startswith("未命名"):
                        self._profile["nickname"] = nm
                if avatar is not None and str(avatar).strip():
                    self._profile["avatar"] = sanitize_avatar_url(upgrade_legacy_avatar(avatar))
                else:
                    self._profile["avatar"] = sanitize_avatar_url(self._profile.get("avatar"))
                if initialized is not None:
                    self._profile["initialized"] = bool(initialized)
                elif self._profile.get("nickname"):
                    self._profile["initialized"] = True
            self._save_profile()
            prof = self.get_profile()
        try:
            self.sync_member_snapshot(status_level="idle", step_text="恢复访客默认态" if reset_guest else "已同步云账号名片")
        except Exception:
            pass
        return prof

    def _get_account_vault_record(self, topic):
        with self._lock:
            vaults = self._state.get("account_vaults") or {}
            rec = vaults.get(str(topic or ""))
            return dict(rec) if isinstance(rec, dict) else None

    def _set_account_vault_record(self, topic, pkt, rev, updated_ts):
        t = str(topic or "").strip()
        p = str(pkt or "").strip()
        if not t or not p.startswith("SMV29:"):
            return
        with self._lock:
            vaults = self._state.setdefault("account_vaults", {})
            if not isinstance(vaults, dict):
                vaults = {}
                self._state["account_vaults"] = vaults
            vaults[t] = {
                "pkt": p,
                "rev": int(rev or 1),
                "updated_ts": float(updated_ts or time.time()),
            }
            if len(vaults) > 200:
                keep = sorted(
                    vaults.items(),
                    key=lambda kv: (int((kv[1] or {}).get("rev") or 0), float((kv[1] or {}).get("updated_ts") or 0.0)),
                )[-200:]
                self._state["account_vaults"] = dict(keep)
            self._save_state_local_and_push()

    def _pull_account_vault_from_hub_sources(self):
        with self._lock:
            sync = dict(self._state.get("sync_settings") or {})
        mode = sync.get("mode", "local_or_shared")
        if mode == "local_or_shared":
            sf = self._shared_state_file()
            if sf is not None and sf.exists():
                try:
                    remote = json.loads(sf.read_text(encoding="utf-8"))
                    if isinstance(remote, dict):
                        with self._lock:
                            self._merge_remote_state(remote)
                except Exception:
                    pass
        elif mode in ("http_hub", "gist_cloud"):
            remote = self._fetch_remote_state_unlocked(sync)
            if isinstance(remote, dict):
                with self._lock:
                    self._merge_remote_state(remote)

    def _push_account_vault_to_hub_sources(self):
        with self._lock:
            sync = dict(self._state.get("sync_settings") or {})
            payload = self._export_sync_payload()
        if sync.get("mode", "local_or_shared") in ("http_hub", "gist_cloud"):
            self._push_remote_snapshot(sync, payload)

    def _default_state(self):
        return {
            "admin_auth": {
                "passcode_hash": _DEFAULT_ADMIN_PASSCODE_HASH,
                "password_hash": _DEFAULT_ADMIN_PASSWORD_HASH,
                "custom_set": False,
                "admin_v2_migrated": True,
                "admin_v3_migrated": True,
                "admin_v4_migrated": True,
            },
            "global_logic": {
                "lock_timeout_sec": 180,
                "enforce_paste_delay_sec": None,
                "enforce_compose_order": "",
                "enforce_silent_order": "",
                "enforce_minimize_after_send": None,
                "default_permissions": dict(DEFAULT_PERMISSIONS),
                "global_ai_api": {
                    "provider": "gemini",
                    "api_base": "",
                    "api_key": "",
                    "model": "",
                },
            },
            "sync_settings": {
                "mode": "local_or_shared",  # local_or_shared | http_hub | gist_cloud
                "shared_dir": "",
                "hub_url": "",
                "hub_secret": "sheetmate-hub",
                "gist_token": "",
                "gist_id": "",
                "server_port": 18790,
                "server_autostart": False,
            },
            "lock": {
                "holder_id": "",
                "holder_name": "",
                "holder_avatar": "",
                "holder_code": "",
                "mode_label": "",
                "step_text": "",
                "acquired_ts": 0.0,
                "heartbeat_ts": 0.0,
            },
            "queue": [],
            "members": {},
            "runs": [],
            "errors": [],
            "ai_chats": [],
            "feedbacks": [],
            "backfilled_keys": [],
            "account_vaults": {},
        }

    def _load_state_raw(self):
        if self._state_path.exists():
            try:
                data = json.loads(self._state_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        return self._default_state()

    def _ensure_state_schema(self):
        d = self._default_state()
        for k, v in d.items():
            if k not in self._state or not isinstance(self._state[k], type(v)):
                self._state[k] = v
        for sk, sv in d["admin_auth"].items():
            self._state["admin_auth"].setdefault(sk, sv)
        auth_blk = self._state["admin_auth"]
        if not auth_blk.get("custom_set") and (
            not auth_blk.get("admin_v4_migrated")
            or auth_blk.get("passcode_hash") == _LEGACY_ADMIN_PASSCODE_HASH
            or auth_blk.get("password_hash") in _LEGACY_ADMIN_PASSWORD_HASHES
            or auth_blk.get("password_hash") != _DEFAULT_ADMIN_PASSWORD_HASH
        ):
            changed_auth = False
            if auth_blk.get("passcode_hash") != _DEFAULT_ADMIN_PASSCODE_HASH:
                auth_blk["passcode_hash"] = _DEFAULT_ADMIN_PASSCODE_HASH
                changed_auth = True
            if auth_blk.get("password_hash") != _DEFAULT_ADMIN_PASSWORD_HASH:
                auth_blk["password_hash"] = _DEFAULT_ADMIN_PASSWORD_HASH
                changed_auth = True
            if not auth_blk.get("admin_v4_migrated"):
                changed_auth = True
            auth_blk["custom_set"] = False
            auth_blk["admin_v2_migrated"] = True
            auth_blk["admin_v3_migrated"] = True
            auth_blk["admin_v4_migrated"] = True
            if changed_auth:
                self._save_state_local_and_push(push_remote=False)
        for sk, sv in d["global_logic"].items():
            self._state["global_logic"].setdefault(sk, sv)
        self._state["global_logic"]["default_permissions"] = normalize_permissions(
            self._state["global_logic"].get("default_permissions")
        )
        for sk, sv in d["sync_settings"].items():
            self._state["sync_settings"].setdefault(sk, sv)
        for sk, sv in d["lock"].items():
            self._state["lock"].setdefault(sk, sv)

    def _shared_state_file(self):
        sync = self._state.get("sync_settings") or {}
        if sync.get("mode") == "local_or_shared":
            sd = str(sync.get("shared_dir") or "").strip()
            if sd:
                p = Path(sd)
                return p / "team_hub_state.json"
        return None

    def _fetch_remote_state_unlocked(self, sync):
        mode = sync.get("mode", "local_or_shared")
        if mode == "http_hub":
            url = str(sync.get("hub_url") or "").strip().rstrip("/")
            if url:
                try:
                    req = urllib.request.Request(
                        f"{url}/hub/state",
                        headers={"X-Hub-Secret": str(sync.get("hub_secret") or "sheetmate-hub")},
                    )
                    with urllib.request.urlopen(req, timeout=3.5) as resp:
                        remote = json.loads(resp.read().decode("utf-8"))
                    if isinstance(remote, dict) and "lock" in remote:
                        return remote
                except Exception:
                    return None
        elif mode == "gist_cloud":
            tok = str(sync.get("gist_token") or "").strip()
            gid = str(sync.get("gist_id") or "").strip()
            if tok and gid:
                try:
                    req = urllib.request.Request(
                        f"https://api.github.com/gists/{gid}",
                        headers={
                            "Authorization": f"Bearer {tok}",
                            "Accept": "application/vnd.github+json",
                            "User-Agent": "SheetMateHub",
                        },
                    )
                    with urllib.request.urlopen(req, timeout=5.0) as resp:
                        gd = json.loads(resp.read().decode("utf-8"))
                    raw_c = (((gd.get("files") or {}).get("sheetmate_hub.json") or {}).get("content")) or ""
                    if raw_c:
                        remote = json.loads(raw_c)
                        if isinstance(remote, dict):
                            return remote
                except Exception:
                    return None
        elif self._auto_cloud_enabled:
            try:
                raw_msg = _th_mqtt_retained_pull(_AUTO_CLOUD_TOPIC, timeout=2.2)
                if raw_msg:
                    latest_obj = _auto_cloud_unpack(raw_msg)
                    if isinstance(latest_obj, dict) and ("members" in latest_obj or "lock" in latest_obj):
                        # 公共频道强制降级为 compact 并标记来源，阻断管理员凭据与全局 AI 接口投毒
                        return {**latest_obj, "compact": True, "_from_public_cloud": True}
            except Exception:
                return None
        return None

    def _schedule_async_remote_pull(self, force=False):
        with self._lock:
            sync_snap = dict(self._state.get("sync_settings") or {})
        mode = sync_snap.get("mode", "local_or_shared")
        if mode == "local_or_shared" and not self._auto_cloud_enabled:
            return
        if mode == "http_hub" and not str(sync_snap.get("hub_url") or "").strip():
            return
        if mode == "gist_cloud" and (
            not str(sync_snap.get("gist_token") or "").strip()
            or not str(sync_snap.get("gist_id") or "").strip()
        ):
            return
        now_m = time.monotonic()
        with self._pull_lock:
            if self._pull_thread is not None and self._pull_thread.is_alive():
                return
            if mode in ("http_hub", "gist_cloud"):
                cooldown = 1.0 if force else SYNC_PULL_COOLDOWN_SEC
                if (now_m - self._last_pull_ts) < cooldown:
                    return
                self._last_pull_ts = now_m
            else:
                cooldown = 2.5 if force else AUTO_CLOUD_PULL_COOLDOWN_SEC
                if (now_m - self._last_auto_pull_ts) < cooldown:
                    return
                self._last_auto_pull_ts = now_m

            def _pull_worker():
                try:
                    remote = self._fetch_remote_state_unlocked(sync_snap)
                    if isinstance(remote, dict):
                        with self._lock:
                            self._merge_remote_state(remote)
                except Exception:
                    pass
                finally:
                    with self._pull_lock:
                        self._pull_thread = None

            th = threading.Thread(target=_pull_worker, daemon=True, name="TeamHubAsyncPull")
            self._pull_thread = th
            th.start()

    def _sync_pull_if_needed(self, force=False):
        sync = self._state.get("sync_settings") or {}
        mode = sync.get("mode", "local_or_shared")
        if mode == "local_or_shared":
            sf = self._shared_state_file()
            if sf is not None:
                if sf.exists():
                    try:
                        remote = json.loads(sf.read_text(encoding="utf-8"))
                        if isinstance(remote, dict) and "lock" in remote:
                            self._merge_remote_state(remote)
                            return True
                    except Exception:
                        return False
                return False
        # 所有网络拉取全部走后台异步线程，绝不在持锁状态阻塞前端接口与按钮点击
        self._schedule_async_remote_pull(force=force)
        return False

    def _merge_remote_state(self, remote):
        if not isinstance(remote, dict):
            return
        cur_sync = dict(self._state.get("sync_settings") or {})
        from_public = bool(remote.get("_from_public_cloud"))
        if isinstance(remote.get("account_vaults"), dict):
            cur_v = self._state.setdefault("account_vaults", {})
            if not isinstance(cur_v, dict):
                cur_v = {}
                self._state["account_vaults"] = cur_v
            for tk, rv in remote["account_vaults"].items():
                tks = str(tk or "").strip()
                if not tks or not isinstance(rv, dict):
                    continue
                pkt = str(rv.get("pkt") or "").strip()
                if not pkt.startswith("SMV29:"):
                    continue
                try:
                    n_rev = int(rv.get("rev") or 0)
                    n_ts = float(rv.get("updated_ts") or 0.0)
                except Exception:
                    continue
                old_v = cur_v.get(tks)
                if isinstance(old_v, dict):
                    try:
                        o_ver = (int(old_v.get("rev") or 0), float(old_v.get("updated_ts") or 0.0))
                    except Exception:
                        o_ver = (-1, -1.0)
                else:
                    o_ver = (-1, -1.0)
                if (n_rev, n_ts) > o_ver:
                    cur_v[tks] = {"pkt": pkt, "rev": n_rev, "updated_ts": n_ts}
            if len(cur_v) > 200:
                keep_v = sorted(
                    cur_v.items(),
                    key=lambda kv: (int((kv[1] or {}).get("rev") or 0), float((kv[1] or {}).get("updated_ts") or 0.0)),
                )[-200:]
                self._state["account_vaults"] = dict(keep_v)

        if remote.get("compact") or from_public:
            if isinstance(remote.get("lock"), dict):
                self._state["lock"] = remote["lock"]
            if isinstance(remote.get("queue"), list):
                self._state["queue"] = remote["queue"]
            if isinstance(remote.get("global_logic"), dict):
                old_gl = self._state.get("global_logic") if isinstance(self._state.get("global_logic"), dict) else {}
                new_gl = copy.deepcopy(remote["global_logic"])
                old_ai = old_gl.get("global_ai_api") if isinstance(old_gl.get("global_ai_api"), dict) else {}
                if from_public:
                    # 公共云频道禁止改写全局 AI 接口与 api_base，防止接口地址投毒重定向
                    new_gl["global_ai_api"] = copy.deepcopy(old_ai) if old_ai else {
                        "provider": "gemini", "api_base": "", "api_key": "", "model": "",
                    }
                else:
                    new_ai = new_gl.get("global_ai_api") if isinstance(new_gl.get("global_ai_api"), dict) else {}
                    if not new_ai.get("api_key") and old_ai.get("api_key"):
                        new_gl.setdefault("global_ai_api", {})["api_key"] = old_ai["api_key"]
                self._state["global_logic"] = new_gl
            if isinstance(remote.get("members"), dict):
                mems = self._state.setdefault("members", {})
                for uid, rmem in remote["members"].items():
                    if not isinstance(rmem, dict):
                        continue
                    old_m = mems.get(uid) if isinstance(mems.get(uid), dict) else {}
                    merged_m = {**old_m, **rmem}
                    old_ov = old_m.get("ai_api_override") if isinstance(old_m.get("ai_api_override"), dict) else {}
                    new_ov = merged_m.get("ai_api_override") if isinstance(merged_m.get("ai_api_override"), dict) else {}
                    if not new_ov.get("api_key") and old_ov.get("api_key"):
                        merged_m["ai_api_override"] = {**new_ov, "api_key": old_ov["api_key"]}
                    if merged_m.get("avatar"):
                        merged_m["avatar"] = sanitize_avatar_url(upgrade_legacy_avatar(merged_m.get("avatar")))
                    mems[uid] = merged_m
            for lk in ("runs", "errors", "ai_chats", "feedbacks"):
                r_list = remote.get(lk)
                if isinstance(r_list, list) and r_list:
                    exist = list(self._state.get(lk) or [])
                    seen_sigs = {json.dumps(x, sort_keys=True, ensure_ascii=False) for x in exist[-120:]}
                    for it in r_list:
                        if not isinstance(it, dict):
                            continue
                        sig = json.dumps(it, sort_keys=True, ensure_ascii=False)
                        if sig not in seen_sigs:
                            exist.append(it)
                            seen_sigs.add(sig)
                    self._state[lk] = exist[-800:]
        else:
            typed_checks = {
                "lock": dict,
                "queue": list,
                "members": dict,
                "runs": list,
                "errors": list,
                "ai_chats": list,
                "feedbacks": list,
                "global_logic": dict,
            }
            for k, expected_t in typed_checks.items():
                if k in remote and isinstance(remote[k], expected_t):
                    if k == "members":
                        clean_mems = {}
                        for uid, mem in remote[k].items():
                            if isinstance(mem, dict):
                                mem_c = copy.deepcopy(mem)
                                if mem_c.get("avatar"):
                                    mem_c["avatar"] = sanitize_avatar_url(upgrade_legacy_avatar(mem_c.get("avatar")))
                                clean_mems[uid] = mem_c
                        self._state[k] = clean_mems
                    else:
                        self._state[k] = copy.deepcopy(remote[k])
            if isinstance(remote.get("admin_auth"), dict) and remote["admin_auth"].get("custom_set") is True:
                self._state["admin_auth"] = copy.deepcopy(remote["admin_auth"])
        self._state["sync_settings"] = cur_sync
        self._ensure_state_schema()

    def _push_remote_snapshot(self, sync_cfg, payload_data):
        mode = sync_cfg.get("mode", "local_or_shared")
        if mode == "http_hub":
            url = str(sync_cfg.get("hub_url") or "").strip().rstrip("/")
            if url:
                payload = json.dumps(payload_data, ensure_ascii=False).encode("utf-8")
                req = urllib.request.Request(
                    f"{url}/hub/state",
                    data=payload,
                    headers={
                        "Content-Type": "application/json",
                        "X-Hub-Secret": str(sync_cfg.get("hub_secret") or "sheetmate-hub"),
                    },
                    method="POST",
                )
                try:
                    urllib.request.urlopen(req, timeout=4.0).read()
                except Exception:
                    pass
        elif mode == "gist_cloud":
            tok = str(sync_cfg.get("gist_token") or "").strip()
            gid = str(sync_cfg.get("gist_id") or "").strip()
            if tok and gid:
                payload = json.dumps({
                    "files": {
                        "sheetmate_hub.json": {
                            "content": json.dumps(payload_data, ensure_ascii=False),
                        }
                    }
                }, ensure_ascii=False).encode("utf-8")
                req = urllib.request.Request(
                    f"https://api.github.com/gists/{gid}",
                    data=payload,
                    headers={
                        "Authorization": f"Bearer {tok}",
                        "Accept": "application/vnd.github+json",
                        "Content-Type": "application/json",
                        "User-Agent": "SheetMateHub",
                    },
                    method="PATCH",
                )
                try:
                    urllib.request.urlopen(req, timeout=6.0).read()
                except Exception:
                    pass
        else:
            sf = self._shared_state_file()
            if sf is not None:
                try:
                    sf.parent.mkdir(parents=True, exist_ok=True)
                    tmp_s = sf.with_suffix(".tmp")
                    tmp_s.write_text(json.dumps(payload_data, ensure_ascii=False, indent=2), encoding="utf-8")
                    tmp_s.replace(sf)
                except Exception:
                    pass
            elif self._auto_cloud_enabled:
                try:
                    slim_members = {}
                    for uid, mem in (payload_data.get("members") or {}).items():
                        if not isinstance(mem, dict):
                            continue
                        slim_m = {
                            k: mem.get(k)
                            for k in (
                                "user_id", "member_code", "nickname", "display_name",
                                "avatar", "admin_remark", "permissions", "step_text",
                                "status_level", "last_seen_str", "last_seen_ts",
                                "last_run_str", "next_run_str", "config_audit",
                                "ai_api_override", "pending_remote_config",
                            )
                            if k in mem
                        }
                        if "ai_api_override" in slim_m:
                            slim_m["ai_api_override"] = redact_sensitive_config(slim_m["ai_api_override"])
                        if "pending_remote_config" in slim_m:
                            slim_m["pending_remote_config"] = redact_sensitive_config(slim_m["pending_remote_config"])
                        slim_members[uid] = slim_m
                    compact_obj = {
                        "compact": True,
                        "lock": payload_data.get("lock"),
                        "queue": payload_data.get("queue"),
                        "global_logic": redact_sensitive_config(payload_data.get("global_logic")),
                        "members": slim_members,
                        "runs": (payload_data.get("runs") or [])[-15:],
                        "errors": (payload_data.get("errors") or [])[-12:],
                        "ai_chats": (payload_data.get("ai_chats") or [])[-12:],
                        "feedbacks": redact_sensitive_config((payload_data.get("feedbacks") or [])[-12:]),
                    }
                    pkt = _auto_cloud_pack(compact_obj)
                    _th_mqtt_retained_push(_AUTO_CLOUD_TOPIC, pkt, timeout=3.0)
                except Exception:
                    pass

    def _schedule_async_remote_push(self):
        with self._push_lock:
            self._push_dirty = True
            if self._push_thread is not None and self._push_thread.is_alive():
                return

            def _worker():
                while True:
                    with self._push_lock:
                        if not self._push_dirty:
                            self._push_thread = None
                            return
                        self._push_dirty = False
                    with self._lock:
                        sync_snap = dict(self._state.get("sync_settings") or {})
                        payload_snap = self._export_sync_payload()
                    self._push_remote_snapshot(sync_snap, payload_snap)

            self._push_thread = threading.Thread(target=_worker, daemon=True, name="TeamHubAsyncPush")
            self._push_thread.start()

    def _save_state_local_and_push(self, push_remote=True):
        # 保留紧凑历史上限防止单文件膨胀
        self._state["runs"] = (self._state.get("runs") or [])[-800:]
        self._state["errors"] = (self._state.get("errors") or [])[-200:]
        self._state["ai_chats"] = (self._state.get("ai_chats") or [])[-300:]
        self._state["feedbacks"] = (self._state.get("feedbacks") or [])[-150:]
        self._state["backfilled_keys"] = (self._state.get("backfilled_keys") or [])[-300:]
        try:
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._state_path)
        except Exception:
            pass
        if not push_remote:
            return
        sync = self._state.get("sync_settings") or {}
        if sync.get("mode", "local_or_shared") == "local_or_shared":
            sf = self._shared_state_file()
            if sf is not None:
                # 共享目录毫秒级完成，直接写入以便多进程单元测试即时可见
                self._push_remote_snapshot(sync, self._export_sync_payload())
            elif self._auto_cloud_enabled:
                # 自动联网协同通过后台异步合并推送，绝不阻塞本地界面锁
                self._schedule_async_remote_push()
        else:
            # 远端 HTTP / Gist 不持锁同步阻塞，转后台异步合并发送
            self._schedule_async_remote_push()

    def _export_sync_payload(self):
        return {
            "admin_auth": self._state.get("admin_auth"),
            "global_logic": self._state.get("global_logic"),
            "lock": self._state.get("lock"),
            "queue": self._state.get("queue"),
            "members": self._state.get("members"),
            "runs": (self._state.get("runs") or [])[-800:],
            "errors": (self._state.get("errors") or [])[-200:],
            "ai_chats": (self._state.get("ai_chats") or [])[-300:],
            "feedbacks": (self._state.get("feedbacks") or [])[-150:],
            "account_vaults": dict(self._state.get("account_vaults") or {}),
        }

    def _backfill_local_runs_once(self):
        """从已有的 run_journal.jsonl 与 run.log 补录历史运行次数到统计池。"""
        with self._lock:
            seen = set(self._state.get("backfilled_keys") or [])
            runs = list(self._state.get("runs") or [])
            prof = self.get_profile()
            added = 0
            try:
                if self.app is not None and hasattr(self.app, "load_run_journal"):
                    for j in self.app.load_run_journal(limit=60):
                        ts = str(j.get("ts") or "").strip()
                        if not ts:
                            continue
                        key = f"j|{ts}|{j.get('round', '')}|{j.get('status', '')}"
                        if key in seen:
                            continue
                        seen.add(key)
                        runs.append({
                            "id": uuid.uuid4().hex[:10],
                            "ts": ts,
                            "user_id": prof["user_id"],
                            "member_name": prof["display_name"],
                            "member_code": prof["member_code"],
                            "mode": j.get("mode", "full"),
                            "trigger_type": "schedule" if str(j.get("round", "")).isdigit() else "manual",
                            "status": "ok" if j.get("status") == "ok" else "err",
                            "err_code": j.get("err_code", ""),
                            "duration_sec": int(j.get("duration_sec") or 0),
                        })
                        added += 1
                if added == 0 and self.app is not None and hasattr(self.app, "parse_run_log_sessions"):
                    for s in self.app.parse_run_log_sessions(max_sessions=20):
                        st = str(s.get("start_time") or "")
                        if len(st) < 12 or s.get("status") not in ("ok", "err"):
                            continue
                        key = f"l|{st}|{s.get('round', '')}|{s.get('status', '')}"
                        if key in seen:
                            continue
                        seen.add(key)
                        errs = s.get("err_codes") or []
                        runs.append({
                            "id": uuid.uuid4().hex[:10],
                            "ts": st,
                            "user_id": prof["user_id"],
                            "member_name": prof["display_name"],
                            "member_code": prof["member_code"],
                            "mode": "capture_only" if "截图" in str(s.get("round", "")) else "full",
                            "trigger_type": "manual" if "手动" in str(s.get("round", "")) else "schedule",
                            "status": s.get("status", "ok"),
                            "err_code": errs[-1] if errs else "",
                            "duration_sec": 0,
                        })
                        added += 1
            except Exception:
                pass
            if added:
                runs.sort(key=lambda x: str(x.get("ts") or ""))
                self._state["runs"] = runs[-800:]
                self._state["backfilled_keys"] = list(seen)[-300:]
                self._save_state_local_and_push(push_remote=False)

    # ==================== 锁与自动排队 ====================
    def _clean_expired_lock_locked(self):
        lk = self._state["lock"]
        if not lk.get("holder_id"):
            return
        timeout_s = max(30, int((self._state.get("global_logic") or {}).get("lock_timeout_sec") or 180))
        last_hb = float(lk.get("heartbeat_ts") or lk.get("acquired_ts") or 0)
        if last_hb and (time.time() - last_hb) > timeout_s:
            self._state["lock"] = {
                "holder_id": "", "holder_name": "", "holder_avatar": "", "holder_code": "",
                "mode_label": "", "step_text": "", "acquired_ts": 0.0, "heartbeat_ts": 0.0,
            }

    def get_lock_status(self):
        with self._lock:
            self._sync_pull_if_needed()
            self._clean_expired_lock_locked()
            lk = dict(self._state["lock"])
            q = [dict(it) for it in (self._state.get("queue") or [])]
            now = time.time()
            held_sec = int(max(0, now - float(lk.get("acquired_ts") or now))) if lk.get("holder_id") else 0
            lk["held_seconds"] = held_sec
            return {
                "locked": bool(lk.get("holder_id")),
                "lock": lk,
                "queue": q,
            }

    def acquire_run_lock(self, mode_label="全流程", step_text="准备运行…"):
        prof = self.get_profile()
        with self._lock:
            self._sync_pull_if_needed(force=True)
            self._clean_expired_lock_locked()
            lk = self._state["lock"]
            holder = str(lk.get("holder_id") or "")
            if holder and holder != prof["user_id"]:
                held_s = int(max(0, time.time() - float(lk.get("acquired_ts") or time.time())))
                return {
                    "ok": False,
                    "locked": True,
                    "locked_by": {**lk, "held_seconds": held_s},
                    "queue": list(self._state.get("queue") or []),
                }
            now = time.time()
            self._state["lock"] = {
                "holder_id": prof["user_id"],
                "holder_name": prof["display_name"],
                "holder_avatar": prof["avatar"],
                "holder_code": prof["member_code"],
                "mode_label": str(mode_label or "全流程"),
                "step_text": str(step_text or "运行中…")[:80],
                "acquired_ts": now,
                "heartbeat_ts": now,
            }
            # 若自己在队列里顺便清出自己
            self._state["queue"] = [q for q in (self._state.get("queue") or []) if q.get("user_id") != prof["user_id"]]
            self._save_state_local_and_push()
            return {"ok": True, "locked": False, "lock": dict(self._state["lock"])}

    def heartbeat_run_lock(self, step_text=""):
        prof = self.get_profile()
        with self._lock:
            self._sync_pull_if_needed()
            lk = self._state["lock"]
            if lk.get("holder_id") == prof["user_id"]:
                lk["heartbeat_ts"] = time.time()
                if step_text:
                    lk["step_text"] = str(step_text)[:80]
                self._save_state_local_and_push()
                return True
            return False

    def release_run_lock(self, user_id=None):
        prof = self.get_profile()
        uid = user_id or prof["user_id"]
        with self._lock:
            self._sync_pull_if_needed(force=True)
            lk = self._state["lock"]
            if not lk.get("holder_id") or lk.get("holder_id") == uid:
                self._state["lock"] = {
                    "holder_id": "", "holder_name": "", "holder_avatar": "", "holder_code": "",
                    "mode_label": "", "step_text": "", "acquired_ts": 0.0, "heartbeat_ts": 0.0,
                }
                self._save_state_local_and_push()
                return True
            return False

    def force_unlock_all(self, clear_queue=False):
        with self._lock:
            self._sync_pull_if_needed(force=True)
            self._state["lock"] = {
                "holder_id": "", "holder_name": "", "holder_avatar": "", "holder_code": "",
                "mode_label": "", "step_text": "", "acquired_ts": 0.0, "heartbeat_ts": 0.0,
            }
            if clear_queue:
                self._state["queue"] = []
            self._save_state_local_and_push()
            return True

    def join_run_queue(self, run_type="full"):
        prof = self.get_profile()
        with self._lock:
            self._sync_pull_if_needed(force=True)
            q = [it for it in (self._state.get("queue") or []) if it.get("user_id") != prof["user_id"]]
            q.append({
                "user_id": prof["user_id"],
                "name": prof["display_name"],
                "avatar": sanitize_avatar_url(prof.get("avatar")),
                "member_code": prof["member_code"],
                "run_type": str(run_type or "full"),
                "joined_at": time.strftime("%H:%M:%S"),
                "joined_ts": time.time(),
            })
            self._state["queue"] = q
            pos = len(q)
            self._save_state_local_and_push()
            return {"ok": True, "position": pos, "queue": list(q)}

    def leave_run_queue(self, user_id=None):
        prof = self.get_profile()
        uid = user_id or prof["user_id"]
        with self._lock:
            self._sync_pull_if_needed(force=True)
            self._state["queue"] = [it for it in (self._state.get("queue") or []) if it.get("user_id") != uid]
            self._save_state_local_and_push()
            return {"ok": True, "queue": list(self._state["queue"])}

    def poll_queue_turn(self):
        """检查自己是否在队列第一位且当前锁已空，若轮到自己则自动返回 run_type 准备接力执行。"""
        prof = self.get_profile()
        with self._lock:
            self._sync_pull_if_needed(force=True)
            self._clean_expired_lock_locked()
            q = list(self._state.get("queue") or [])
            my_idx = next((i for i, it in enumerate(q) if it.get("user_id") == prof["user_id"]), -1)
            if my_idx < 0:
                return {"in_queue": False, "my_turn": False, "position": 0}
            locked = bool(self._state["lock"].get("holder_id"))
            if not locked and my_idx == 0:
                run_type = q[0].get("run_type", "full")
                return {"in_queue": True, "my_turn": True, "position": 1, "run_type": run_type}
            return {
                "in_queue": True,
                "my_turn": False,
                "position": my_idx + 1,
                "run_type": q[my_idx].get("run_type", "full"),
                "locked_by": dict(self._state["lock"]) if locked else None,
            }

    # ==================== 成员快照 / 权限 / 统计 / 反馈 ====================
    def get_my_permissions(self):
        prof = self.get_profile()
        with self._lock:
            self._sync_pull_if_needed()
            mem = (self._state.get("members") or {}).get(prof["user_id"]) or {}
            def_p = (self._state.get("global_logic") or {}).get("default_permissions") or DEFAULT_PERMISSIONS
            return normalize_permissions(mem.get("permissions") or def_p)

    def get_effective_global_overrides(self):
        with self._lock:
            gl = dict(self._state.get("global_logic") or {})
            return gl

    def get_sync_settings(self):
        with self._lock:
            sc = dict(self._state.get("sync_settings") or {})
            srv_running = self._server is not None
            srv_port = self._server_port or int(sc.get("server_port") or 18790)
        local_ips = self._detect_lan_ips(use_cache=True)
        return {
            **sc,
            "server_running": srv_running,
            "server_port": srv_port,
            "lan_urls": [f"http://{ip}:{srv_port}" for ip in local_ips],
        }

    def get_ui_poll_bundle(self, cur_cfg=None):
        """单次短锁聚合前端轮询所需的全部协同状态，绝不持锁做 DNS/网络阻塞调用。"""
        with self._lock:
            self._sync_pull_if_needed()
            self._clean_expired_lock_locked()

            prof = dict(self._profile or {})
            nick = str(prof.get("nickname") or "").strip()
            prof["avatar"] = sanitize_avatar_url(prof.get("avatar"))
            prof["display_name"] = nick or prof.get("member_code", "成员")
            uid = prof.get("user_id", "")

            members = self._state.get("members") or {}
            mem = members.get(uid)
            pending_cfg = None
            if isinstance(mem, dict):
                pending = mem.get("pending_remote_config")
                if isinstance(pending, dict):
                    mem["pending_remote_config"] = None
                    restored = copy.deepcopy(pending)
                    if isinstance(cur_cfg, dict):
                        if not restored.get("luya_pass") and cur_cfg.get("luya_pass"):
                            restored["luya_pass"] = cur_cfg["luya_pass"]
                        cur_ai = cur_cfg.get("ai_assistant")
                        res_ai = restored.get("ai_assistant")
                        if isinstance(cur_ai, dict) and isinstance(res_ai, dict):
                            if not res_ai.get("api_key") and cur_ai.get("api_key"):
                                res_ai["api_key"] = cur_ai["api_key"]
                    mem["config"] = restored
                    self._save_state_local_and_push()
                    pending_cfg = restored

            mem_safe = mem if isinstance(mem, dict) else {}
            gl = copy.deepcopy(self._state.get("global_logic") or {})
            def_p = gl.get("default_permissions") or DEFAULT_PERMISSIONS
            perms = normalize_permissions(mem_safe.get("permissions") or def_p)

            lk = dict(self._state.get("lock") or {})
            q = [dict(it) for it in (self._state.get("queue") or [])]
            now = time.time()
            held_sec = int(max(0, now - float(lk.get("acquired_ts") or now))) if lk.get("holder_id") else 0
            lk["held_seconds"] = held_sec
            lock_status = {
                "locked": bool(lk.get("holder_id")),
                "lock": lk,
                "queue": q,
            }

            my_idx = next((i for i, it in enumerate(q) if it.get("user_id") == uid), -1)
            if my_idx < 0:
                queue_status = {"in_queue": False, "my_turn": False, "position": 0}
            elif (not lock_status["locked"]) and my_idx == 0:
                queue_status = {"in_queue": True, "my_turn": True, "position": 1, "run_type": q[0].get("run_type", "full")}
            else:
                queue_status = {
                    "in_queue": True,
                    "my_turn": False,
                    "position": my_idx + 1,
                    "run_type": q[my_idx].get("run_type", "full"),
                    "locked_by": lk if lock_status["locked"] else None,
                }

            sc = dict(self._state.get("sync_settings") or {})
            srv_running = self._server is not None
            srv_port = self._server_port or int(sc.get("server_port") or 18790)

        local_ips = self._detect_lan_ips(use_cache=True)
        sync_settings = {
            **sc,
            "server_running": srv_running,
            "server_port": srv_port,
            "lan_urls": [f"http://{ip}:{srv_port}" for ip in local_ips],
        }
        return {
            "pending_cfg": pending_cfg,
            "member_profile": prof,
            "permissions": perms,
            "global_logic": gl,
            "lock_status": lock_status,
            "queue_status": queue_status,
            "sync_settings": sync_settings,
        }

    def pull_remote_config_if_updated(self, cur_cfg):
        """若超管后台远程改动了本成员的配置，自动将远程配置下发回本地并返回新配置。"""
        prof = self.get_profile()
        with self._lock:
            self._sync_pull_if_needed()
            mem = (self._state.get("members") or {}).get(prof["user_id"])
            if not isinstance(mem, dict):
                return None
            pending = mem.get("pending_remote_config")
            if isinstance(pending, dict):
                mem["pending_remote_config"] = None
                restored = copy.deepcopy(pending)
                if isinstance(cur_cfg, dict):
                    if not restored.get("luya_pass") and cur_cfg.get("luya_pass"):
                        restored["luya_pass"] = cur_cfg["luya_pass"]
                    cur_ai = cur_cfg.get("ai_assistant")
                    res_ai = restored.get("ai_assistant")
                    if isinstance(cur_ai, dict) and isinstance(res_ai, dict):
                        if not res_ai.get("api_key") and cur_ai.get("api_key"):
                            res_ai["api_key"] = cur_ai["api_key"]
                mem["config"] = restored
                self._save_state_local_and_push()
                return restored
            return None

    def resolve_ai_api_for_me(self):
        """解析当前电脑实际应使用的 AI 接口（成员专属覆盖优先 -> 超管全局接口设置 -> 空字典回退本地）。"""
        prof = self.get_profile()
        with self._lock:
            self._sync_pull_if_needed()
            mem = (self._state.get("members") or {}).get(prof["user_id"]) or {}
            ov = mem.get("ai_api_override") if isinstance(mem.get("ai_api_override"), dict) else {}
            gl_api = (self._state.get("global_logic") or {}).get("global_ai_api") or {}

        mode = str(ov.get("mode") or "default").strip()
        if mode == "local_only":
            return {"provider": "local_only", "api_base": "", "api_key": "", "model": ""}
        if mode == "custom":
            prov = str(ov.get("provider") or gl_api.get("provider") or "gemini").strip()
            return {
                "provider": prov if prov in ("gemini", "openai_compat", "local_only") else "gemini",
                "api_base": str(ov.get("api_base") or "").strip(),
                "api_key": str(ov.get("api_key") or "").strip(),
                "model": str(ov.get("model") or "").strip(),
            }
        prov_g = str(gl_api.get("provider") or "gemini").strip()
        return {
            "provider": prov_g if prov_g in ("gemini", "openai_compat", "local_only") else "gemini",
            "api_base": str(gl_api.get("api_base") or "").strip(),
            "api_key": str(gl_api.get("api_key") or "").strip(),
            "model": str(gl_api.get("model") or "").strip(),
        }

    def sync_member_snapshot(self, cfg=None, status_level="idle", step_text="", last_run_str="—", next_run_str="—", recent_logs=None, error_detail=None):
        prof = self.get_profile()
        with self._lock:
            members = self._state.setdefault("members", {})
            existing = dict(members.get(prof["user_id"]) or {})
            def_p = (self._state.get("global_logic") or {}).get("default_permissions") or DEFAULT_PERMISSIONS
            perms = normalize_permissions(existing.get("permissions") or def_p)
            use_cfg = cfg if isinstance(cfg, dict) else (existing.get("config") or {})
            if use_cfg:
                # 同步到后台的密码字段先保留可查结构但只截断长度安全存储
                audit = audit_member_config(use_cfg)
            else:
                audit = existing.get("config_audit") or []
            members[prof["user_id"]] = {
                "user_id": prof["user_id"],
                "member_code": prof["member_code"],
                "nickname": prof.get("nickname") or "",
                "display_name": prof["display_name"],
                "avatar": sanitize_avatar_url(prof.get("avatar")),
                "initialized": bool(prof.get("initialized")),
                "admin_remark": existing.get("admin_remark", ""),
                "permissions": perms,
                "ai_api_override": existing.get("ai_api_override") or {"mode": "default", "provider": "gemini", "api_base": "", "api_key": "", "model": ""},
                "status_level": str(status_level or "idle"),
                "step_text": str(step_text or "待命")[:100],
                "last_run_str": str(last_run_str or "—"),
                "next_run_str": str(next_run_str or "—"),
                "last_seen_ts": time.time(),
                "last_seen_str": time.strftime("%Y-%m-%d %H:%M:%S"),
                "config": use_cfg,
                "config_audit": audit,
                "recent_logs": list(recent_logs or existing.get("recent_logs") or [])[-25:],
                "last_error": error_detail if error_detail is not None else existing.get("last_error"),
                "pending_remote_config": existing.get("pending_remote_config"),
            }
            self._save_state_local_and_push()
            return members[prof["user_id"]]

    def record_run_event(self, mode="full", trigger_type="manual", status="ok", err_code="", duration_sec=0):
        prof = self.get_profile()
        with self._lock:
            self._sync_pull_if_needed()
            entry = {
                "id": uuid.uuid4().hex[:10],
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "user_id": prof["user_id"],
                "member_name": prof["display_name"],
                "member_code": prof["member_code"],
                "mode": str(mode or "full"),
                "trigger_type": str(trigger_type or "manual"),
                "status": str(status or "ok"),
                "err_code": str(err_code or ""),
                "duration_sec": int(duration_sec or 0),
            }
            runs = self._state.setdefault("runs", [])
            runs.append(entry)
            self._save_state_local_and_push()
            return entry

    def record_member_error(self, error_detail, recent_logs=None):
        prof = self.get_profile()
        ed = error_detail if isinstance(error_detail, dict) else {}
        if not ed.get("code"):
            return None
        with self._lock:
            self._sync_pull_if_needed()
            mem = (self._state.get("members") or {}).get(prof["user_id"]) or {}
            entry = {
                "id": uuid.uuid4().hex[:10],
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "user_id": prof["user_id"],
                "display_name": prof["display_name"],
                "member_code": prof["member_code"],
                "avatar": sanitize_avatar_url(prof.get("avatar")),
                "admin_remark": mem.get("admin_remark", ""),
                "code": str(ed.get("code") or "ERR-RUN-900"),
                "title": str(ed.get("title") or "运行异常"),
                "plain_reason": str(ed.get("plain_reason") or ""),
                "raw_error": str(ed.get("raw_error") or "")[:200],
                "location": str(ed.get("location") or ""),
                "logs": list(recent_logs or [])[-12:],
            }
            errs = self._state.setdefault("errors", [])
            errs.append(entry)
            self._save_state_local_and_push()
            return entry

    def record_member_ai_chat(self, role, text, meta=None):
        prof = self.get_profile()
        txt = str(text or "").strip()
        if not txt:
            return None
        with self._lock:
            self._sync_pull_if_needed()
            mem = (self._state.get("members") or {}).get(prof["user_id"]) or {}
            entry = {
                "id": uuid.uuid4().hex[:10],
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "user_id": prof["user_id"],
                "display_name": prof["display_name"],
                "member_code": prof["member_code"],
                "avatar": sanitize_avatar_url(prof.get("avatar")),
                "admin_remark": mem.get("admin_remark", ""),
                "role": str(role or "user"),
                "text": txt[:2000],
                "meta": str(meta or ""),
            }
            chats = self._state.setdefault("ai_chats", [])
            chats.append(entry)
            self._save_state_local_and_push()
            return entry

    def submit_feedback(self, category="问题建议", content="", cfg=None, error_detail=None):
        prof = self.get_profile()
        txt = str(content or "").strip()
        if not txt:
            return {"ok": False, "message": "请先填写反馈内容"}
        with self._lock:
            self._sync_pull_if_needed()
            mem = (self._state.get("members") or {}).get(prof["user_id"]) or {}
            fb = {
                "id": uuid.uuid4().hex[:10],
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "user_id": prof["user_id"],
                "display_name": prof["display_name"],
                "member_code": prof["member_code"],
                "avatar": sanitize_avatar_url(prof.get("avatar")),
                "admin_remark": mem.get("admin_remark", ""),
                "category": str(category or "问题建议"),
                "content": txt[:1500],
                "status": "pending",
                "admin_reply": "",
                "config_snapshot": redact_sensitive_config(cfg if isinstance(cfg, dict) else (mem.get("config") or {})),
                "last_error": error_detail if isinstance(error_detail, dict) else mem.get("last_error"),
            }
            fbs = self._state.setdefault("feedbacks", [])
            fbs.append(fb)
            self._save_state_local_and_push()
            return {"ok": True, "feedback": fb, "message": "反馈已提交成功，感谢您的宝贵建议！"}

    def query_statistics(self, start_time="", end_time="", user_id=""):
        """按自定义起止时间与成员筛选统计运行次数、成功/失败数、活跃人数与成员排行。"""
        with self._lock:
            self._sync_pull_if_needed()
            runs = list(self._state.get("runs") or [])
            members = dict(self._state.get("members") or {})

        s_dt = _parse_dt_str(start_time)
        e_dt = _parse_dt_str(end_time)
        if e_dt is not None and len(str(end_time or "").strip()) <= 10:
            e_dt = e_dt.replace(hour=23, minute=59, second=59)

        matched = []
        for r in runs:
            if user_id and r.get("user_id") != user_id:
                continue
            rt = _parse_dt_str(r.get("ts"))
            if s_dt is not None and (rt is None or rt < s_dt):
                continue
            if e_dt is not None and (rt is None or rt > e_dt):
                continue
            matched.append(r)

        total = len(matched)
        ok_cnt = sum(1 for r in matched if r.get("status") == "ok")
        err_cnt = sum(1 for r in matched if r.get("status") != "ok")
        durs = [int(r.get("duration_sec") or 0) for r in matched if int(r.get("duration_sec") or 0) > 0]
        avg_dur = int(sum(durs) / len(durs)) if durs else 0
        manual_cnt = sum(1 for r in matched if r.get("trigger_type") == "manual")
        sched_cnt = sum(1 for r in matched if r.get("trigger_type") == "schedule")

        by_uid = {}
        for uid, m in members.items():
            if user_id and uid != user_id:
                continue
            by_uid[uid] = {
                "user_id": uid,
                "display_name": m.get("display_name") or m.get("member_code") or uid,
                "member_code": m.get("member_code") or "",
                "admin_remark": m.get("admin_remark") or "",
                "avatar": m.get("avatar") or PRESET_AVATARS[0]["url"],
                "total_runs": 0,
                "ok_runs": 0,
                "err_runs": 0,
                "last_ts": m.get("last_seen_str") or "",
            }
        for r in matched:
            uid = r.get("user_id") or "unknown"
            row = by_uid.setdefault(uid, {
                "user_id": uid,
                "display_name": r.get("member_name") or uid,
                "member_code": r.get("member_code") or "",
                "admin_remark": "",
                "avatar": PRESET_AVATARS[0]["url"],
                "total_runs": 0,
                "ok_runs": 0,
                "err_runs": 0,
                "last_ts": "",
            })
            row["total_runs"] += 1
            if r.get("status") == "ok":
                row["ok_runs"] += 1
            else:
                row["err_runs"] += 1
            if str(r.get("ts") or "") > str(row.get("last_ts") or ""):
                row["last_ts"] = r.get("ts")

        per_member = sorted(by_uid.values(), key=lambda x: (-x["total_runs"], str(x["last_ts"])), )
        active_users = sum(1 for x in per_member if x["total_runs"] > 0)
        return {
            "total_runs": total,
            "ok_runs": ok_cnt,
            "err_runs": err_cnt,
            "success_rate": round((ok_cnt / total) * 100, 1) if total else 0.0,
            "avg_duration_sec": avg_dur,
            "manual_runs": manual_cnt,
            "schedule_runs": sched_cnt,
            "total_registered_members": len(members),
            "active_users_in_range": active_users,
            "per_member": per_member,
            "recent_runs": list(reversed(matched[-40:])),
        }

    # ==================== 超级管理员验证与管理操作 ====================
    def verify_admin(self, passcode, password, client_id="local"):
        with self._lock:
            now = time.time()
            if len(self._admin_fail_tracker) > 64:
                _prune_fail_tracker(self._admin_fail_tracker, now)
            rec = self._admin_fail_tracker.get(client_id) or {"cnt": 0, "until": 0.0}
            if rec.get("until", 0.0) > now:
                wait_s = max(1, int(rec["until"] - now))
                return {"ok": False, "locked": True, "message": f"管理员验证连续失败过多，请 {wait_s} 秒后再试"}

            pc_norm = _norm_admin_passcode(passcode)
            pw_norm = _norm_admin_password(password)
            pc_cands = [h for h in dict.fromkeys([
                _sha256_secret(pc_norm),
                _sha256_secret(pc_norm.title()),
            ])]
            pw_hash = _sha256_secret(pw_norm)

            def _check_auth_dict(auth_obj):
                a = auth_obj if isinstance(auth_obj, dict) else {}
                stored_pc = str(a.get("passcode_hash") or "")
                stored_pw = str(a.get("password_hash") or "")
                if not pc_norm or not pw_norm or pw_hash in _LEGACY_ADMIN_PASSWORD_HASHES:
                    return False
                pc_ok = any(hmac.compare_digest(c, stored_pc) for c in pc_cands)
                if not pc_ok:
                    return False
                if a.get("custom_set"):
                    return hmac.compare_digest(pw_hash, stored_pw)
                allowed_pw = {h for h in (stored_pw, *_COMPAT_ADMIN_PASSWORD_HASHES) if h and h not in _LEGACY_ADMIN_PASSWORD_HASHES}
                return any(hmac.compare_digest(pw_hash, h) for h in allowed_pw)

            ok = _check_auth_dict(self._state.get("admin_auth"))
            if not ok:
                self._sync_pull_if_needed()
                ok = _check_auth_dict(self._state.get("admin_auth"))
            if not ok:
                cnt = int(rec.get("cnt", 0)) + 1
                until = (now + ADMIN_LOCKOUT_SEC) if cnt >= ADMIN_MAX_FAILS else 0.0
                self._admin_fail_tracker[client_id] = {"cnt": cnt, "until": until, "last_ts": now}
                _prune_fail_tracker(self._admin_fail_tracker, now)
                if cnt >= ADMIN_MAX_FAILS:
                    return {
                        "ok": False,
                        "locked": True,
                        "message": f"管理员验证连续失败过多，已锁定 {int(ADMIN_LOCKOUT_SEC)} 秒后再试",
                    }
                return {"ok": False, "message": "管理员姓名或密码不正确"}
            self._admin_fail_tracker.pop(client_id, None)
            # 清理已过期 Token，防止内存增长
            for k in [tk for tk, exp_t in self._admin_tokens.items() if exp_t <= now]:
                self._admin_tokens.pop(k, None)
            tok = secrets.token_urlsafe(24)
            self._admin_tokens[tok] = now + 12 * 3600
            return {"ok": True, "admin_token": tok, "message": "已进入超级管理员后台"}

    def is_admin_token_valid(self, tok):
        t_str = str(tok or "")
        if not t_str:
            return False
        now = time.time()
        with self._lock:
            expired = [k for k, exp_t in self._admin_tokens.items() if exp_t <= now]
            for k in expired:
                self._admin_tokens.pop(k, None)
            for k, exp_t in self._admin_tokens.items():
                if exp_t > now and hmac.compare_digest(k, t_str):
                    return True
            return False

    def update_admin_credentials(self, new_passcode="", new_password=""):
        pc = _norm_admin_passcode(new_passcode)
        pw = _norm_admin_password(new_password)
        if not pc or not pw:
            return {"ok": False, "message": "管理员姓名和密码均不能为空"}
        with self._lock:
            self._sync_pull_if_needed()
            self._state["admin_auth"] = {
                "passcode_hash": _sha256_secret(pc),
                "password_hash": _sha256_secret(pw),
                "custom_set": True,
                "admin_v2_migrated": True,
                "admin_v3_migrated": True,
                "admin_v4_migrated": True,
            }
            self._save_state_local_and_push()
            return {"ok": True, "message": "管理员姓名与密码已更新保存"}

    def get_admin_dashboard(self, start_time="", end_time="", user_id=""):
        with self._lock:
            self._sync_pull_if_needed()
            self._clean_expired_lock_locked()
            members = sorted(
                [dict(v) for v in (self._state.get("members") or {}).values()],
                key=lambda m: float(m.get("last_seen_ts") or 0),
                reverse=True,
            )
            errors = list(reversed((self._state.get("errors") or [])[-80:]))
            ai_chats = list(reversed((self._state.get("ai_chats") or [])[-120:]))
            feedbacks = list(reversed((self._state.get("feedbacks") or [])[-80:]))
            lock_info = dict(self._state.get("lock") or {})
            queue_info = list(self._state.get("queue") or [])
            gl = dict(self._state.get("global_logic") or {})
            sync_cfg = dict(self._state.get("sync_settings") or {})

        if user_id:
            errors = [e for e in errors if e.get("user_id") == user_id]
            ai_chats = [c for c in ai_chats if c.get("user_id") == user_id]
        stats = self.query_statistics(start_time=start_time, end_time=end_time, user_id=user_id)
        local_ips = self._detect_lan_ips()
        return {
            "ok": True,
            "stats": stats,
            "members": members,
            "errors": errors,
            "ai_chats": ai_chats,
            "feedbacks": feedbacks,
            "lock_status": {"locked": bool(lock_info.get("holder_id")), "lock": lock_info, "queue": queue_info},
            "global_logic": gl,
            "sync_settings": sync_cfg,
            "server_running": self._server is not None,
            "server_port": self._server_port or int(sync_cfg.get("server_port") or 18790),
            "lan_urls": [f"http://{ip}:{self._server_port or int(sync_cfg.get('server_port') or 18790)}" for ip in local_ips],
            "permission_meta": [{"key": k, "title": t, "desc": d} for k, t, d in PERMISSION_META],
        }

    def admin_update_member(self, target_uid, remark=None, permissions=None, new_config=None, ai_api_override=None):
        with self._lock:
            self._sync_pull_if_needed()
            members = self._state.setdefault("members", {})
            mem = members.get(target_uid)
            if not isinstance(mem, dict):
                return {"ok": False, "message": "未找到该成员记录"}
            if remark is not None:
                mem["admin_remark"] = str(remark).strip()[:40]
            if isinstance(permissions, dict):
                mem["permissions"] = normalize_permissions(permissions)
            if isinstance(new_config, dict):
                mem["config"] = new_config
                mem["pending_remote_config"] = new_config
                mem["config_audit"] = audit_member_config(new_config)
            if isinstance(ai_api_override, dict):
                m_mode = str(ai_api_override.get("mode") or "default").strip()
                prov = str(ai_api_override.get("provider") or "gemini").strip()
                mem["ai_api_override"] = {
                    "mode": m_mode if m_mode in ("default", "custom", "local_only") else "default",
                    "provider": prov if prov in ("gemini", "openai_compat", "local_only") else "gemini",
                    "api_base": str(ai_api_override.get("api_base") or "").strip(),
                    "api_key": str(ai_api_override.get("api_key") or "").strip(),
                    "model": str(ai_api_override.get("model") or "").strip(),
                }
            self._save_state_local_and_push()
            return {"ok": True, "member": dict(mem), "message": "已保存对该成员的设置、权限与 AI 接口配置"}

    def admin_update_global_logic(self, patch):
        p = patch if isinstance(patch, dict) else {}
        with self._lock:
            self._sync_pull_if_needed()
            gl = self._state.setdefault("global_logic", {})
            if "lock_timeout_sec" in p:
                try:
                    gl["lock_timeout_sec"] = max(30, min(1800, int(p["lock_timeout_sec"])))
                except Exception:
                    pass
            if "enforce_paste_delay_sec" in p:
                v = p["enforce_paste_delay_sec"]
                gl["enforce_paste_delay_sec"] = None if v in (None, "", "none") else max(0, min(20, int(v)))
            if "enforce_compose_order" in p:
                v = str(p["enforce_compose_order"] or "")
                gl["enforce_compose_order"] = v if v in ("image_text_silent", "text_silent_image") else ""
            if "enforce_silent_order" in p:
                v = str(p["enforce_silent_order"] or "")
                gl["enforce_silent_order"] = v if v in ("uia_first", "arrow_img_then_uia") else ""
            if "enforce_minimize_after_send" in p:
                v = p["enforce_minimize_after_send"]
                gl["enforce_minimize_after_send"] = v if isinstance(v, bool) else None
            if isinstance(p.get("default_permissions"), dict):
                gl["default_permissions"] = normalize_permissions(p["default_permissions"])
            if isinstance(p.get("global_ai_api"), dict):
                ga = p["global_ai_api"]
                prov = str(ga.get("provider") or "gemini").strip()
                gl["global_ai_api"] = {
                    "provider": prov if prov in ("gemini", "openai_compat", "local_only") else "gemini",
                    "api_base": str(ga.get("api_base") or "").strip(),
                    "api_key": str(ga.get("api_key") or "").strip(),
                    "model": str(ga.get("model") or "").strip(),
                }
            self._save_state_local_and_push()
            return {"ok": True, "global_logic": dict(gl), "message": "全局软件运行逻辑与 AI 接口已更新并下发"}

    def force_cloud_sync(self):
        """立即执行一次云端异地数据拉取并同步上传本机快照。"""
        with self._lock:
            pulled = bool(self._sync_pull_if_needed(force=True))
            self._save_state_local_and_push(push_remote=False)
            mem_cnt = len(self._state.get("members") or {})
        self.sync_member_snapshot(status_level="idle", step_text="云端同步完成")
        return {
            "ok": True,
            "pulled": pulled,
            "member_count": mem_cnt,
            "message": f"已完成云端异地数据双向同步（当前共汇集 {mem_cnt} 位成员）",
        }

    def admin_update_sync_settings(self, patch):
        p = patch if isinstance(patch, dict) else {}
        if p.get("action") == "cloud_refresh":
            return self.force_cloud_sync()
        with self._lock:
            sc = self._state.setdefault("sync_settings", {})
            if p.get("mode") in ("local_or_shared", "http_hub", "gist_cloud"):
                sc["mode"] = p["mode"]
            for k in ("shared_dir", "hub_url", "hub_secret", "gist_token", "gist_id"):
                if k in p:
                    sc[k] = str(p[k] or "").strip()
            if "server_port" in p:
                try:
                    sc["server_port"] = max(1025, min(65530, int(p["server_port"])))
                except Exception:
                    pass
            if "server_autostart" in p:
                sc["server_autostart"] = bool(p["server_autostart"])
            # 若选了 Gist 模式且有 Token 但没有 gist_id，自动创建云端 Gist
            if sc.get("mode") == "gist_cloud" and sc.get("gist_token") and not sc.get("gist_id"):
                created_id = self._create_cloud_gist(sc["gist_token"])
                if created_id:
                    sc["gist_id"] = created_id
            self._save_state_local_and_push(push_remote=False)
            self._sync_pull_if_needed()
            if sc.get("server_autostart") and self._server is None:
                self.start_center_server(sc.get("server_port", 18790))
            elif not sc.get("server_autostart") and self._server is not None:
                self.stop_center_server()
            return {"ok": True, "sync_settings": dict(sc), "message": "多人联机同步方式已保存"}

    def _create_cloud_gist(self, token):
        try:
            body = json.dumps({
                "description": "SheetMate 暗号表助手多端防撞锁与超管中心同步档案",
                "public": False,
                "files": {"sheetmate_hub.json": {"content": json.dumps(self._export_sync_payload(), ensure_ascii=False)}},
            }, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                "https://api.github.com/gists",
                data=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "Content-Type": "application/json",
                    "User-Agent": "SheetMateHub",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=8.0) as resp:
                obj = json.loads(resp.read().decode("utf-8"))
            return str(obj.get("id") or "")
        except Exception:
            return ""

    def admin_resolve_feedback(self, fb_id, status="resolved", reply=""):
        with self._lock:
            self._sync_pull_if_needed()
            for fb in (self._state.get("feedbacks") or []):
                if fb.get("id") == fb_id:
                    fb["status"] = str(status or "resolved")
                    if reply:
                        fb["admin_reply"] = str(reply).strip()[:300]
                    self._save_state_local_and_push()
                    return {"ok": True, "message": "反馈状态已更新"}
            return {"ok": False, "message": "未找到该条反馈"}

    def _detect_lan_ips(self, use_cache=True):
        now_m = time.monotonic()
        cached_ts, cached_ips = getattr(self, "_lan_ips_cache", (0.0, ["127.0.0.1"]))
        if use_cache and cached_ts and (now_m - cached_ts) < 60.0 and cached_ips:
            return list(cached_ips)
        ips = []
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                ip = s.getsockname()[0]
            if ip and ip != "127.0.0.1":
                ips.append(ip)
        except Exception:
            pass
        if not ips:
            try:
                for item in socket.getaddrinfo(socket.gethostname(), None):
                    ip = item[4][0]
                    if "." in ip and not ip.startswith("127.") and ip not in ips:
                        ips.append(ip)
            except Exception:
                pass
        res = ips or ["127.0.0.1"]
        self._lan_ips_cache = (now_m, list(res))
        return res

    def _maybe_start_center_server(self):
        sc = self._state.get("sync_settings") or {}
        if sc.get("server_autostart"):
            self.start_center_server(int(sc.get("server_port") or 18790))

    def start_center_server(self, port=18790):
        with self._lock:
            if self._server is not None:
                return True, self._server_port
            mgr = self

            class HubHandler(BaseHTTPRequestHandler):
                def log_message(self, fmt, *args):
                    return

                def _authorized(self):
                    want = str((mgr._state.get("sync_settings") or {}).get("hub_secret") or "sheetmate-hub")
                    got = str(self.headers.get("X-Hub-Secret") or "")
                    return (not want) or hmac.compare_digest(got, want)

                def _json(self, obj, code=HTTPStatus.OK):
                    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)

                def do_GET(self):
                    if not self._authorized():
                        self._json({"ok": False, "message": "Forbidden"}, HTTPStatus.FORBIDDEN)
                        return
                    if self.path.startswith("/hub/ping"):
                        self._json({"ok": True, "service": "SheetMateHub"})
                        return
                    if self.path.startswith("/hub/state"):
                        with mgr._lock:
                            self._json(mgr._export_sync_payload())
                        return
                    self._json({"ok": False}, HTTPStatus.NOT_FOUND)

                def do_POST(self):
                    if not self._authorized():
                        self._json({"ok": False, "message": "Forbidden"}, HTTPStatus.FORBIDDEN)
                        return
                    try:
                        body = safe_read_json_body(self.rfile, self.headers)
                    except ValueError:
                        self._json({"ok": False, "message": "请求体体积超过安全限制"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                        return
                    if self.path.startswith("/hub/state") and isinstance(body, dict):
                        with mgr._lock:
                            mgr._merge_remote_state(body)
                            mgr._save_state_local_and_push(push_remote=False)
                        self._json({"ok": True})
                        return
                    self._json({"ok": False}, HTTPStatus.NOT_FOUND)

            try:
                srv = ThreadingHTTPServer(("0.0.0.0", int(port)), HubHandler)
                srv.daemon_threads = True
                threading.Thread(target=srv.serve_forever, daemon=True, name="SheetMateTeamHubServer").start()
                self._server = srv
                self._server_port = srv.server_address[1]
                return True, self._server_port
            except Exception:
                return False, 0

    def stop_center_server(self):
        with self._lock:
            srv = self._server
            self._server = None
            self._server_port = 0
        if srv is not None:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:
                pass


class AccountAuthError(ValueError):
    """账号云同步密码错误或密文校验失败异常。"""


def _hub_atomic_write_json(path_obj, data):
    p = Path(path_obj)
    tmp = p.with_name(f".{p.name}.tmp.{os.getpid()}.{threading.get_ident()}")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8", newline="") as fp:
            fp.write(json.dumps(data, ensure_ascii=False, indent=2))
            fp.flush()
        os.replace(str(tmp), str(p))
        return True
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        return False


def _derive_account_credentials(account, password):
    raw_acct = " ".join(unicodedata.normalize("NFKC", str(account or "")).strip().split())
    acct_key = normalize_nickname_key(raw_acct)
    pw = _norm_admin_password(password)
    if len(acct_key) < 2 or len(raw_acct) > 32:
        raise ValueError("账号长度需在 2~32 个字符之间")
    if len(pw) < 4 or len(pw) > 64:
        raise ValueError("同步密码长度需在 4~64 个字符之间")
    topic = "sm29_acct_" + hmac.new(
        b"SheetMate-AcctTopic-v29",
        acct_key.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:28]
    master_key = hashlib.pbkdf2_hmac(
        "sha256",
        pw.encode("utf-8"),
        f"SheetMate-AcctVault-v29::{acct_key}".encode("utf-8"),
        60000,
        dklen=32,
    )
    return raw_acct, acct_key, topic, master_key


def _account_vault_subkeys(master_key):
    mk = bytes(master_key)
    enc_key = hmac.new(mk, b"SheetMate-Vault-ENC-v29", hashlib.sha256).digest()
    mac_key = hmac.new(mk, b"SheetMate-Vault-MAC-v29", hashlib.sha256).digest()
    return enc_key, mac_key


def pack_account_vault(master_key, vault_dict):
    """使用密钥分离 Encrypt-then-MAC (CTR-SHA256 + 32B HMAC-SHA256) 打包跨电脑账号金库。"""
    enc_key, mac_key = _account_vault_subkeys(master_key)
    raw = json.dumps(vault_dict, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    comp = zlib.compress(raw, level=6)
    if len(comp) > MAX_HUB_BODY_BYTES:
        raise ValueError("同步配置体积超过安全限制")
    nonce = secrets.token_bytes(12)
    ks = bytearray()
    idx = 0
    while len(ks) < len(comp):
        ks.extend(hashlib.sha256(enc_key + nonce + idx.to_bytes(4, "big")).digest())
        idx += 1
    ct = bytes(a ^ b for a, b in zip(comp, ks[:len(comp)]))
    mac = hmac.new(mac_key, b"SMV29|" + nonce + ct, hashlib.sha256).digest()
    return "SMV29:" + base64.b64encode(nonce + mac + ct).decode("ascii")


def unpack_account_vault(master_key, token_str):
    """先验证完整 32 字节 MAC 再解密并受限解压账号金库；密码错或密文篡改抛出 AccountAuthError。"""
    s = str(token_str or "").strip()
    if not s.startswith("SMV29:"):
        raise AccountAuthError("云端数据包格式无效")
    try:
        raw = base64.b64decode(s[6:].encode("ascii"), validate=True)
    except Exception as exc:
        raise AccountAuthError("云端数据包编码损坏") from exc
    if len(raw) < 44:
        raise AccountAuthError("云端数据包长度异常")
    nonce, mac, ct = raw[:12], raw[12:44], raw[44:]
    enc_key, mac_key = _account_vault_subkeys(master_key)
    exp = hmac.new(mac_key, b"SMV29|" + nonce + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, exp):
        raise AccountAuthError("账号或同步密码不正确")
    ks = bytearray()
    idx = 0
    while len(ks) < len(ct):
        ks.extend(hashlib.sha256(enc_key + nonce + idx.to_bytes(4, "big")).digest())
        idx += 1
    comp = bytes(a ^ b for a, b in zip(ct, ks[:len(ct)]))
    try:
        obj = json.loads(_safe_zlib_decompress(comp, max_bytes=MAX_DECOMPRESSED_BYTES).decode("utf-8"))
    except Exception as exc:
        raise AccountAuthError("账号数据解压或解析失败") from exc
    if not isinstance(obj, dict):
        raise AccountAuthError("账号数据结构无效")
    return obj


class AccountCloudSyncManager:
    """跨电脑可选账号登录与端到端加密同步管理器（支持免登录默认使用 + 登录后设置/AI话术全同步）。"""

    def __init__(self, base_dir, hub=None):
        self.base_dir = Path(base_dir)
        self._hub = hub
        self._lock = threading.RLock()
        self._session_path = self.base_dir / "cloud_account.json"
        self._guest_backup_path = self.base_dir / "guest_backup.json"
        self._fail_tracker = {}
        self._session = self._load_session()

    def _load_session(self):
        if self._session_path.exists():
            try:
                data = json.loads(self._session_path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("topic") and data.get("master_key_b64"):
                    mk = base64.b64decode(str(data["master_key_b64"]).encode("ascii"), validate=True)
                    if len(mk) == 32:
                        return {
                            "account": str(data.get("account") or "").strip(),
                            "acct_key": str(data.get("acct_key") or "").strip(),
                            "topic": str(data.get("topic") or "").strip(),
                            "master_key_b64": str(data.get("master_key_b64") or "").strip(),
                            "rev": int(data.get("rev") or 0),
                            "last_synced_ts": float(data.get("last_synced_ts") or 0.0),
                            "last_sync_str": str(data.get("last_sync_str") or ""),
                            "logged_in_at": str(data.get("logged_in_at") or ""),
                        }
            except Exception:
                pass
        return {}

    def _save_session(self, sess):
        self._session = dict(sess) if isinstance(sess, dict) else {}
        if self._session:
            _hub_atomic_write_json(self._session_path, {"version": 1, **self._session})
        else:
            try:
                if self._session_path.exists():
                    self._session_path.unlink()
            except Exception:
                pass

    def get_status(self):
        with self._lock:
            s = self._session
            logged = bool(s and s.get("topic") and s.get("master_key_b64"))
            return {
                "logged_in": logged,
                "account": s.get("account", "") if logged else "",
                "rev": int(s.get("rev") or 0) if logged else 0,
                "last_synced_ts": float(s.get("last_synced_ts") or 0.0) if logged else 0.0,
                "last_sync_str": s.get("last_sync_str", "") if logged else "",
                "logged_in_at": s.get("logged_in_at", "") if logged else "",
            }

    def _collect_cloud_vault_candidates(self, topic):
        cands = []
        seen = set()

        def _add(pkt):
            ps = str(pkt or "").strip()
            if ps.startswith("SMV29:") and ps not in seen:
                seen.add(ps)
                cands.append(ps)

        if self._hub is not None:
            try:
                self._hub._pull_account_vault_from_hub_sources()
                rec = self._hub._get_account_vault_record(topic)
                if isinstance(rec, dict):
                    _add(rec.get("pkt"))
            except Exception:
                pass
            if getattr(self._hub, "_auto_cloud_enabled", False):
                for mp in _acct_mqtt_pull_all(topic, timeout=4.5):
                    _add(mp)
        return cands

    def _publish_vault_packet(self, topic, pkt, rev, updated_ts):
        cloud_ok = False
        if self._hub is not None:
            try:
                self._hub._set_account_vault_record(topic, pkt, rev, updated_ts)
                self._hub._push_account_vault_to_hub_sources()
                sync = (self._hub._state.get("sync_settings") or {})
                if sync.get("shared_dir") or sync.get("hub_url") or sync.get("gist_id"):
                    cloud_ok = True
            except Exception:
                pass
            if getattr(self._hub, "_auto_cloud_enabled", False):
                if _acct_mqtt_push_all(topic, pkt, timeout=5.0):
                    cloud_ok = True
        return cloud_ok

    @staticmethod
    def _decrypt_best_vault(master_key, candidates):
        if not candidates:
            return None, None, False, False
        valid_vaults = []
        for pkt in candidates:
            try:
                obj = unpack_account_vault(master_key, pkt)
                rev_n = int(obj.get("rev") or 0)
                ts_n = float(obj.get("updated_ts") or 0.0)
                valid_vaults.append(((rev_n, ts_n), obj, pkt))
            except AccountAuthError:
                continue
        if not valid_vaults:
            return None, None, True, True
        valid_vaults.sort(key=lambda x: x[0], reverse=True)
        _, best_obj, best_pkt = valid_vaults[0]
        return best_obj, best_pkt, True, False

    def login_or_register(self, account, password, current_bundle, client_id="local"):
        """登录已有同步账号（拉取恢复云端设置与 AI 话术）或首次创建同步账号并上传本机配置。"""
        now = time.time()
        with self._lock:
            if len(self._fail_tracker) > 64:
                _prune_fail_tracker(self._fail_tracker, now)
            rec = self._fail_tracker.get(client_id) or {"cnt": 0, "until": 0.0}
            if rec.get("until", 0.0) > now:
                wait_s = max(1, int(rec["until"] - now))
                return {"ok": False, "locked": True, "message": f"账号密码连续输错过多，请 {wait_s} 秒后再试"}

        try:
            raw_acct, acct_key, topic, master_key = _derive_account_credentials(account, password)
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}

        # 若此前处于未登录访客态且尚无访客备份，保留非敏感名片备份以便退出恢复
        if not self.get_status()["logged_in"] and not self._guest_backup_path.exists() and self._hub is not None:
            try:
                gp = self._hub.get_profile()
                _hub_atomic_write_json(self._guest_backup_path, {
                    "guest_profile": {
                        "nickname": str(gp.get("nickname") or ""),
                        "avatar": sanitize_avatar_url(gp.get("avatar")),
                        "initialized": bool(gp.get("initialized")),
                    }
                })
            except Exception:
                pass

        cands = self._collect_cloud_vault_candidates(topic)
        best_obj, best_pkt, _had_cands, auth_failed = self._decrypt_best_vault(master_key, cands)

        if auth_failed:
            with self._lock:
                cnt = int((self._fail_tracker.get(client_id) or {}).get("cnt", 0)) + 1
                until = (now + ACCOUNT_LOCKOUT_SEC) if cnt >= ACCOUNT_MAX_FAILS else 0.0
                self._fail_tracker[client_id] = {"cnt": cnt, "until": until, "last_ts": now}
                _prune_fail_tracker(self._fail_tracker, now)
                if cnt >= ACCOUNT_MAX_FAILS:
                    return {
                        "ok": False,
                        "locked": True,
                        "wrong_password": True,
                        "message": f"账号或同步密码连续错误过多，已锁定 {int(ACCOUNT_LOCKOUT_SEC)} 秒后再试",
                    }
            return {
                "ok": False,
                "wrong_password": True,
                "message": "同步密码不正确（该账号已存在云端档案，请输入正确密码）",
            }

        with self._lock:
            self._fail_tracker.pop(client_id, None)

        mk_b64 = base64.b64encode(master_key).decode("ascii")
        now_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))

        if best_obj is None:
            rev = 1
            vault = {
                "schema_v": 1,
                "account": raw_acct,
                "rev": rev,
                "updated_ts": now,
                "bundle": copy.deepcopy(current_bundle if isinstance(current_bundle, dict) else {}),
            }
            pkt = pack_account_vault(master_key, vault)
            cloud_ok = self._publish_vault_packet(topic, pkt, rev, now)
            with self._lock:
                self._save_session({
                    "account": raw_acct,
                    "acct_key": acct_key,
                    "topic": topic,
                    "master_key_b64": mk_b64,
                    "rev": rev,
                    "last_synced_ts": now,
                    "last_sync_str": now_str,
                    "logged_in_at": now_str,
                })
            return {
                "ok": True,
                "mode": "registered",
                "rev": rev,
                "cloud_pushed": cloud_ok,
                "bundle": vault["bundle"],
                "account_status": self.get_status(),
                "message": f"已绑定同步账号「{raw_acct}」，当前设置与 AI 话术已加密上传云端",
            }

        rev = int(best_obj.get("rev") or 1)
        upd_ts = float(best_obj.get("updated_ts") or now)
        if self._hub is not None and best_pkt:
            try:
                self._hub._set_account_vault_record(topic, best_pkt, rev, upd_ts)
            except Exception:
                pass
        with self._lock:
            self._save_session({
                "account": str(best_obj.get("account") or raw_acct),
                "acct_key": acct_key,
                "topic": topic,
                "master_key_b64": mk_b64,
                "rev": rev,
                "last_synced_ts": upd_ts,
                "last_sync_str": now_str,
                "logged_in_at": now_str,
            })
        return {
            "ok": True,
            "mode": "logged_in",
            "rev": rev,
            "bundle": best_obj.get("bundle") if isinstance(best_obj.get("bundle"), dict) else {},
            "account_status": self.get_status(),
            "message": f"已登录「{raw_acct}」，已同步该账号在云端保存的全部设置与 AI 话术（v{rev}）",
        }

    def push_bundle(self, bundle):
        """将本机最新设置与 AI 话术/记忆加密推送至当前登录账号的云端金库。"""
        with self._lock:
            s = dict(self._session or {})
        if not s or not s.get("topic") or not s.get("master_key_b64"):
            return {"ok": False, "not_logged_in": True, "message": "当前处于免登录默认模式"}
        try:
            mk = base64.b64decode(s["master_key_b64"].encode("ascii"), validate=True)
        except Exception:
            return {"ok": False, "message": "本地同步凭据异常，请重新登录"}

        now = time.time()
        now_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
        new_rev = int(s.get("rev") or 0) + 1
        vault = {
            "schema_v": 1,
            "account": s.get("account") or "",
            "rev": new_rev,
            "updated_ts": now,
            "bundle": copy.deepcopy(bundle if isinstance(bundle, dict) else {}),
        }
        pkt = pack_account_vault(mk, vault)
        cloud_ok = self._publish_vault_packet(s["topic"], pkt, new_rev, now)
        with self._lock:
            if self._session.get("topic") == s["topic"]:
                self._save_session({
                    **self._session,
                    "rev": new_rev,
                    "last_synced_ts": now,
                    "last_sync_str": now_str,
                })
        return {
            "ok": True,
            "rev": new_rev,
            "cloud_pushed": cloud_ok,
            "account_status": self.get_status(),
            "message": f"已将最新设置与 AI 话术同步到账号「{s.get('account')}」（v{new_rev}）",
        }

    def pull_if_newer(self):
        """检测云端是否有其他电脑提交的更高版本金库；若有则返回最新同步包。"""
        with self._lock:
            s = dict(self._session or {})
        if not s or not s.get("topic") or not s.get("master_key_b64"):
            return {"ok": False, "updated": False, "not_logged_in": True}
        try:
            mk = base64.b64decode(s["master_key_b64"].encode("ascii"), validate=True)
        except Exception:
            return {"ok": False, "updated": False}

        cands = self._collect_cloud_vault_candidates(s["topic"])
        best_obj, best_pkt, _, auth_failed = self._decrypt_best_vault(mk, cands)
        if auth_failed or best_obj is None:
            return {"ok": True, "updated": False}
        rem_ver = (int(best_obj.get("rev") or 0), float(best_obj.get("updated_ts") or 0.0))
        loc_ver = (int(s.get("rev") or 0), float(s.get("last_synced_ts") or 0.0))
        if rem_ver > loc_ver:
            now_str = time.strftime("%Y-%m-%d %H:%M:%S")
            with self._lock:
                if self._session.get("topic") == s["topic"]:
                    self._save_session({
                        **self._session,
                        "rev": rem_ver[0],
                        "last_synced_ts": rem_ver[1],
                        "last_sync_str": now_str,
                    })
            if self._hub is not None and best_pkt:
                try:
                    self._hub._set_account_vault_record(s["topic"], best_pkt, rem_ver[0], rem_ver[1])
                except Exception:
                    pass
            return {
                "ok": True,
                "updated": True,
                "rev": rem_ver[0],
                "bundle": best_obj.get("bundle") if isinstance(best_obj.get("bundle"), dict) else {},
                "account_status": self.get_status(),
            }
        return {"ok": True, "updated": False, "account_status": self.get_status()}

    def sync_now(self, local_bundle, direction="auto"):
        """手动立即执行一次同步：若云端更新或明确要求拉取则下载恢复，否则将本机改动推送到云端。"""
        st = self.get_status()
        if not st["logged_in"]:
            return {"ok": False, "not_logged_in": True, "message": "当前为免登录默认模式，请先登录同步账号"}
        pulled = self.pull_if_newer()
        if direction == "pull" and pulled.get("updated"):
            return {
                "ok": True,
                "action": "pulled",
                "bundle": pulled["bundle"],
                "account_status": self.get_status(),
                "message": f"已从云端拉取最新配置与 AI 话术（v{pulled.get('rev')}）",
            }
        if direction == "auto" and pulled.get("updated"):
            return {
                "ok": True,
                "action": "pulled",
                "bundle": pulled["bundle"],
                "account_status": self.get_status(),
                "message": f"发现另一台电脑的新配置，已自动同步拉取（v{pulled.get('rev')}）",
            }
        res_p = self.push_bundle(local_bundle)
        return {**res_p, "action": "pushed"}

    def logout(self):
        """退出当前同步账号，清除本机账号凭据并恢复为未登录默认模式信息。"""
        guest_prof = None
        if self._guest_backup_path.exists():
            try:
                raw_g = json.loads(self._guest_backup_path.read_text(encoding="utf-8"))
                if isinstance(raw_g, dict) and isinstance(raw_g.get("guest_profile"), dict):
                    guest_prof = raw_g["guest_profile"]
            except Exception:
                guest_prof = None
        try:
            if self._guest_backup_path.exists():
                self._guest_backup_path.unlink()
        except Exception:
            pass
        with self._lock:
            old_acct = self._session.get("account", "")
            self._save_session({})
        return {
            "ok": True,
            "logged_out_account": old_acct,
            "guest_profile": guest_prof,
            "account_status": self.get_status(),
            "message": "已退出同步账号，本机已恢复免登录默认设置与纯净状态",
        }
