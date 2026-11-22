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
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import uuid


def _make_svg_data_uri(bg1, bg2, inner_svg):
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 96">'
        '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0%" stop-color="{bg1}"/>'
        f'<stop offset="100%" stop-color="{bg2}"/>'
        '</linearGradient></defs>'
        '<rect width="96" height="96" rx="28" fill="url(#g)"/>'
        f'{inner_svg}'
        '</svg>'
    )
    b64 = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{b64}"


PRESET_AVATARS = [
    {
        "id": "spark",
        "name": "极速闪电",
        "url": _make_svg_data_uri(
            "#0A84FF",
            "#5E5CE6",
            '<path d="M52 18L28 52h18l-4 26 26-36H50l2-24z" fill="#FFFFFF" opacity="0.95"/>',
        ),
    },
    {
        "id": "rocket",
        "name": "数据火箭",
        "url": _make_svg_data_uri(
            "#FF6B6B",
            "#FF9F0A",
            '<path d="M48 18c10 10 14 23 14 35l-14 8-14-8c0-12 4-25 14-35z" fill="#FFF"/>'
            '<circle cx="48" cy="40" r="6" fill="#FF6B6B"/>'
            '<path d="M36 59l12 19 12-19z" fill="#FFE082"/>',
        ),
    },
    {
        "id": "star",
        "name": "王牌之星",
        "url": _make_svg_data_uri(
            "#AF52DE",
            "#FF2D55",
            '<polygon points="48,18 56,36 76,38 61,51 66,71 48,60 30,71 35,51 20,38 40,36" fill="#FFFFFF" opacity="0.95"/>',
        ),
    },
    {
        "id": "shield",
        "name": "稳健守护",
        "url": _make_svg_data_uri(
            "#28CD41",
            "#00A878",
            '<path d="M48 18l24 9v18c0 17-11 27-24 33-13-6-24-16-24-33V27l24-9z" fill="#FFFFFF" opacity="0.92"/>'
            '<path d="M39 48l6 6 13-13" stroke="#00A878" stroke-width="5" fill="none" stroke-linecap="round" stroke-linejoin="round"/>',
        ),
    },
    {
        "id": "wave",
        "name": "清风海洋",
        "url": _make_svg_data_uri(
            "#32ADE6",
            "#007AFF",
            '<circle cx="48" cy="48" r="22" fill="none" stroke="#FFFFFF" stroke-width="6" opacity="0.9"/>'
            '<path d="M26 52c8-8 14 8 22 0s14 8 22 0" fill="none" stroke="#FFFFFF" stroke-width="5" stroke-linecap="round"/>',
        ),
    },
    {
        "id": "sun",
        "name": "晴空暖阳",
        "url": _make_svg_data_uri(
            "#FFB300",
            "#FF7043",
            '<circle cx="48" cy="48" r="14" fill="#FFFFFF"/>'
            '<g stroke="#FFFFFF" stroke-width="4.5" stroke-linecap="round">'
            '<line x1="48" y1="18" x2="48" y2="26"/><line x1="48" y1="70" x2="48" y2="78"/>'
            '<line x1="18" y1="48" x2="26" y2="48"/><line x1="70" y1="48" x2="78" y2="48"/>'
            '<line x1="27" y1="27" x2="33" y2="33"/><line x1="63" y1="63" x2="69" y2="69"/>'
            '<line x1="69" y1="27" x2="63" y2="33"/><line x1="33" y1="63" x2="27" y2="69"/>'
            '</g>',
        ),
    },
    {
        "id": "crystal",
        "name": "极光菱晶",
        "url": _make_svg_data_uri(
            "#00C7BE",
            "#5856D6",
            '<polygon points="48,16 74,48 48,80 22,48" fill="#FFFFFF" opacity="0.92"/>'
            '<polygon points="48,26 62,48 48,70 34,48" fill="#5856D6" opacity="0.32"/>',
        ),
    },
    {
        "id": "owl",
        "name": "值守猫头鹰",
        "url": _make_svg_data_uri(
            "#3A4B6C",
            "#1E293B",
            '<circle cx="37" cy="44" r="11" fill="#FFFFFF"/>'
            '<circle cx="59" cy="44" r="11" fill="#FFFFFF"/>'
            '<circle cx="38" cy="44" r="5" fill="#0F172A"/>'
            '<circle cx="58" cy="44" r="5" fill="#0F172A"/>'
            '<polygon points="48,51 43,60 53,60" fill="#FF9F0A"/>',
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
    ("can_edit_cells", "允许改表格自定义单元格", "控制是否能增删或修改 D2/D4/E4/L3/L4/S4 等单元格规则"),
    ("can_edit_schedule", "允许修改定时自动排期", "控制运行周期、白夜时段和单点计划覆盖的修改权"),
    ("can_edit_accounts", "允许修改WPS链接与账号", "保护源数据后台账号密码与在线表格链接不被误改"),
    ("can_edit_logic", "允许改等待秒数与内部顺序", "控制粘贴等待秒数、发后最小化与模块执行顺序"),
    ("can_use_ai", "允许使用内置 AI 顾问", "控制首页 AI 对话总结与智能复盘功能"),
]

DEFAULT_PERMISSIONS = {k: True for k, _t, _d in PERMISSION_META}


MAX_HUB_BODY_BYTES = 2 * 1024 * 1024
ADMIN_MAX_FAILS = 5
ADMIN_LOCKOUT_SEC = 60.0
SYNC_PULL_COOLDOWN_SEC = 1.5
_SAFE_AVATAR_RE = re.compile(r"^data:image/(?:png|jpeg|jpg|webp|svg\+xml);base64,[A-Za-z0-9+/=]+$", re.ASCII)


def sanitize_avatar_url(raw_url):
    """严格校验头像 URI，只允许纯净 Base64 图片载荷，阻断存储型 XSS 注入。"""
    s = "".join(str(raw_url or "").split())
    if s and len(s) <= 260000 and _SAFE_AVATAR_RE.match(s):
        return s
    return PRESET_AVATARS[0]["url"] if PRESET_AVATARS else ""


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


def _sha256_secret(val):
    return hashlib.sha256(f"SheetMateHub::{val}".encode("utf-8")).hexdigest()


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
        ("can_edit_logic", "调整粘贴等待秒数、防撞与内部模块顺序", (
            "dt_paste_delay_sec", "dt_minimize_after_send", "pipeline_tuning", "open_wps_after_run",
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
        self._push_lock = threading.Lock()
        self._push_thread = None
        self._push_dirty = False
        self._profile = self._load_or_init_profile()
        self._state = self._load_state_raw()
        self._ensure_state_schema()
        self._backfill_local_runs_once()
        self._maybe_start_center_server()

    def _load_or_init_profile(self):
        if self._profile_path.exists():
            try:
                data = json.loads(self._profile_path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("user_id"):
                    data.setdefault("member_code", f"成员-{str(data['user_id'])[:4].upper()}")
                    data.setdefault("nickname", "")
                    data.setdefault("avatar", PRESET_AVATARS[0]["url"])
                    data.setdefault("initialized", bool(data.get("nickname")))
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
        return {
            "ok": True,
            "duplicate": False,
            "message": f"已保存您的专属名字：{prof.get('display_name', '')}",
            "profile": prof,
            **prof,
        }

    def _default_state(self):
        return {
            "admin_auth": {
                "passcode_hash": _sha256_secret("admin"),
                "password_hash": _sha256_secret("888888"),
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

    def _sync_pull_if_needed(self, force=False):
        sync = self._state.get("sync_settings") or {}
        mode = sync.get("mode", "local_or_shared")
        if mode in ("http_hub", "gist_cloud"):
            now_m = time.monotonic()
            if not force and (now_m - self._last_pull_ts) < SYNC_PULL_COOLDOWN_SEC:
                return False
            self._last_pull_ts = now_m
        if mode == "http_hub":
            url = str(sync.get("hub_url") or "").strip().rstrip("/")
            if url:
                try:
                    req = urllib.request.Request(
                        f"{url}/hub/state",
                        headers={"X-Hub-Secret": str(sync.get("hub_secret") or "sheetmate-hub")},
                    )
                    with urllib.request.urlopen(req, timeout=4.0) as resp:
                        remote = json.loads(resp.read().decode("utf-8"))
                    if isinstance(remote, dict) and "lock" in remote:
                        self._merge_remote_state(remote)
                        return True
                except Exception:
                    return False
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
                    with urllib.request.urlopen(req, timeout=6.0) as resp:
                        gd = json.loads(resp.read().decode("utf-8"))
                    raw_c = (((gd.get("files") or {}).get("sheetmate_hub.json") or {}).get("content")) or ""
                    if raw_c:
                        remote = json.loads(raw_c)
                        if isinstance(remote, dict):
                            self._merge_remote_state(remote)
                            return True
                except Exception:
                    return False
        else:
            sf = self._shared_state_file()
            if sf is not None and sf.exists():
                try:
                    remote = json.loads(sf.read_text(encoding="utf-8"))
                    if isinstance(remote, dict) and "lock" in remote:
                        self._merge_remote_state(remote)
                        return True
                except Exception:
                    return False
        return False

    def _merge_remote_state(self, remote):
        cur_sync = dict(self._state.get("sync_settings") or {})
        for k in ("lock", "queue", "members", "runs", "errors", "ai_chats", "feedbacks", "global_logic", "admin_auth"):
            if k in remote:
                self._state[k] = remote[k]
        for mem in (self._state.get("members") or {}).values():
            if isinstance(mem, dict) and mem.get("avatar"):
                mem["avatar"] = sanitize_avatar_url(mem.get("avatar"))
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
            # 本地/共享文件毫秒级完成，直接写入以便多进程单元测试即时可见
            self._push_remote_snapshot(sync, self._export_sync_payload())
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
            return {
                **sc,
                "server_running": self._server is not None,
                "server_port": self._server_port or int(sc.get("server_port") or 18790),
                "lan_urls": [f"http://{ip}:{self._server_port or int(sc.get('server_port') or 18790)}" for ip in self._detect_lan_ips()],
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
                mem["config"] = pending
                self._save_state_local_and_push()
                return pending
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
                "config_snapshot": cfg if isinstance(cfg, dict) else (mem.get("config") or {}),
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
            rec = self._admin_fail_tracker.get(client_id) or {"cnt": 0, "until": 0.0}
            if rec.get("until", 0.0) > now:
                wait_s = max(1, int(rec["until"] - now))
                return {"ok": False, "locked": True, "message": f"管理员验证连续失败过多，请 {wait_s} 秒后再试"}
            self._sync_pull_if_needed(force=True)
            auth = self._state.get("admin_auth") or {}
            ok = (
                hmac.compare_digest(_sha256_secret(str(passcode or "").strip()), str(auth.get("passcode_hash") or ""))
                and hmac.compare_digest(_sha256_secret(str(password or "").strip()), str(auth.get("password_hash") or ""))
            )
            if not ok:
                cnt = int(rec.get("cnt", 0)) + 1
                until = (now + ADMIN_LOCKOUT_SEC) if cnt >= ADMIN_MAX_FAILS else 0.0
                self._admin_fail_tracker[client_id] = {"cnt": cnt, "until": until}
                if cnt >= ADMIN_MAX_FAILS:
                    return {
                        "ok": False,
                        "locked": True,
                        "message": f"管理员验证连续失败过多，已锁定 {int(ADMIN_LOCKOUT_SEC)} 秒后再试",
                    }
                return {"ok": False, "message": "超级管理员口令或密码不正确"}
            self._admin_fail_tracker.pop(client_id, None)
            # 清理已过期 Token，防止内存增长
            for k in [tk for tk, exp_t in self._admin_tokens.items() if exp_t <= now]:
                self._admin_tokens.pop(k, None)
            tok = secrets.token_urlsafe(24)
            self._admin_tokens[tok] = now + 12 * 3600
            return {"ok": True, "admin_token": tok, "message": "已进入超级管理员后台"}

    def is_admin_token_valid(self, tok):
        if not tok:
            return False
        now = time.time()
        exp = self._admin_tokens.get(tok, 0)
        if exp <= now:
            self._admin_tokens.pop(tok, None)
            return False
        return True

    def update_admin_credentials(self, new_passcode="", new_password=""):
        pc = str(new_passcode or "").strip()
        pw = str(new_password or "").strip()
        if not pc or not pw:
            return {"ok": False, "message": "口令和密码均不能为空"}
        with self._lock:
            self._sync_pull_if_needed()
            self._state["admin_auth"] = {
                "passcode_hash": _sha256_secret(pc),
                "password_hash": _sha256_secret(pw),
            }
            self._save_state_local_and_push()
            return {"ok": True, "message": "超级管理员口令与密码已更新保存"}

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

    def admin_update_sync_settings(self, patch):
        p = patch if isinstance(patch, dict) else {}
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

    def _detect_lan_ips(self):
        ips = []
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            if ip and ip != "127.0.0.1":
                ips.append(ip)
        except Exception:
            pass
        try:
            for item in socket.getaddrinfo(socket.gethostname(), None):
                ip = item[4][0]
                if "." in ip and not ip.startswith("127.") and ip not in ips:
                    ips.append(ip)
        except Exception:
            pass
        return ips or ["127.0.0.1"]

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
