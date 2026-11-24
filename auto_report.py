# -*- coding: utf-8 -*-
# pyright: reportMissingImports=false, reportMissingModuleSource=false
"""
暗号表助手 SheetMate 核心自动化引擎（v2.9.7）：
- 自动登录数据后台拉取、格式清洗与 WPS 在线表格自动写入
- 汇总表自定义格位校准、高清选区截图与多尺度无坐标钉钉静音发群
- 支持白天/夜间自定义排期、AI 个人习惯持久化记忆与跨电脑云同步扩展
"""

import copy
import ipaddress
import os
import sys
import time
import subprocess
import re
import io
import socket
import threading
import urllib.request
import urllib.parse
import urllib.error
import ctypes
from ctypes import wintypes
from pathlib import Path
from datetime import datetime, timedelta
import json
import base64
import ssl
from PIL import Image

# 固定浏览器目录（必须在 import playwright 之前设置）
# 优先使用程序同级或上级目录中的 ms-playwright（随项目放在 E 盘不占 C 盘），其次才回退环境变量或系统目录
def _early_app_dir():
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        if (exe_dir / "_internal").is_dir():
            return exe_dir
        # 单文件纯净模式：数据隔离到 %LOCALAPPDATA%\SheetMate，EXE 所在文件夹不产生任何杂乱文件或子目录
        d = Path(os.environ.get("LOCALAPPDATA") or str(Path.home())) / "SheetMate"
        try:
            d.mkdir(parents=True, exist_ok=True)
            legacy_cfg = exe_dir / "config.json"
            new_cfg = d / "config.json"
            if legacy_cfg.is_file() and not new_cfg.exists():
                new_cfg.write_bytes(legacy_cfg.read_bytes())
        except Exception:
            pass
        return d
    return Path(__file__).resolve().parent


def _resolve_playwright_browsers_dir():
    base = _early_app_dir()
    for cand in (base / "ms-playwright", base.parent / "ms-playwright"):
        try:
            if cand.exists() and any(cand.glob("chromium-*")):
                return cand
        except Exception:
            pass
    env_dir = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if env_dir and Path(env_dir).exists():
        return Path(env_dir)
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ms-playwright"


_browsers_dir = _resolve_playwright_browsers_dir()
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(_browsers_dir)

def sync_playwright():
    from playwright.sync_api import sync_playwright as _sp
    return _sp()


class _LazyModuleProxy:
    """延迟加载重型第三方库，避免双击打开程序首屏时阻塞数秒。"""

    def __init__(self, mod_name):
        self._mod_name = mod_name
        self._mod = None

    def _load(self):
        if self._mod is None:
            import importlib
            self._mod = importlib.import_module(self._mod_name)
        return self._mod

    def __getattr__(self, item):
        return getattr(self._load(), item)


openpyxl = _LazyModuleProxy("openpyxl")
from tkinter import messagebox

# ==================== 配置 ====================
LUYA_USER = "test@test.com"
LUYA_PASS = "wodedaodun111"
WPS_URL = "https://www.kdocs.cn/l/cd2lJNEI1wen"

DATA_URLS = [
    ("暗号数据", "https://bb.luyaapp.live/question/161"),
    ("注册数据", "https://bb.luyaapp.live/question/154?pkg=&device="),
    ("短链数据", "https://bb.luyaapp.live/question/155?pkg=&device="),
]

MAX_KEEP_WPS_TABS = 2
# ==============================================


def app_dir():
    """兼容源码、目录版与单文件纯净版（单文件自动隔离数据目录，不在 exe 旁生成杂乱文件夹）。"""
    return _early_app_dir()


_FILE_WRITE_LOCK = threading.RLock()


def _atomic_write_text(path_obj, text):
    """使用同目录临时文件 + os.replace 原子写盘，防止并发或断电导致 JSON/日志截断损坏。"""
    p = Path(path_obj)
    tmp = p.with_name(f".{p.name}.tmp.{os.getpid()}.{threading.get_ident()}")
    with _FILE_WRITE_LOCK:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8", newline="") as fp:
                fp.write(text)
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


def _atomic_write_json(path_obj, data):
    """线程安全地以 UTF-8 JSON 格式原子落盘。"""
    return _atomic_write_text(path_obj, json.dumps(data, ensure_ascii=False, indent=2))


def _read_head_nonempty_lines(path_obj, max_lines=800, count_total=False):
    """流式读取文件前 max_lines 条非空行，避免大体积 TSV/日志整文件一次性读入内存。"""
    lines = []
    total = 0
    lim = max(1, int(max_lines))
    with open(path_obj, "r", encoding="utf-8", errors="ignore") as fp:
        for raw_ln in fp:
            s = raw_ln.rstrip("\r\n")
            if not s.strip():
                continue
            total += 1
            if len(lines) < lim:
                lines.append(s)
            elif not count_total:
                break
    return (lines, total) if count_total else lines


def get_local_runtime_dir():
    """将 UI 壳体等本地运行缓存固定保存在数据目录内（单文件版落在 LocalAppData，不污染桌面）。"""
    base = app_dir()
    try:
        rt = base / "runtime_data"
        rt.mkdir(parents=True, exist_ok=True)
        return rt
    except Exception:
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SheetMate" / "runtime_data"


def get_browser_user_data_dir():
    """返回浏览器登录态目录（按需创建，不在程序启动瞬间提前在 exe 旁新建文件夹）。"""
    base = app_dir()
    if getattr(sys, "frozen", False) and not (Path(sys.executable).resolve().parent / "_internal").is_dir():
        return base / "browser_data"
    for cand in (base / "browser_data", base.parent / "browser_data"):
        if cand.exists():
            return cand
    return base / "browser_data"


BROWSER_USER_DATA = get_browser_user_data_dir()

_single_instance_mutex = None


def acquire_single_instance():
    """只允许运行一个实例；重复双击时直接恢复并置顶已有窗口。"""
    global _single_instance_mutex
    try:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _single_instance_mutex = kernel32.CreateMutexW(
            None, False, "SheetMate_v1_Mutex")
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            user32 = ctypes.windll.user32
            for _ in range(12):
                all_wins = _enum_top_windows(target_classes=None, target_titles=None)
                windows = [
                    w for w in all_wins
                    if "暗号表助手" in (w[2] or "")
                    and w[1] in ("TkTopLevel", "Chrome_WidgetWin_1", "Chrome_WidgetWin_0")
                ]
                if not windows:
                    windows = [
                        w for w in all_wins
                        if "暗号表助手" in (w[2] or "") and w[1] != "ConsoleWindowClass"
                    ]
                if windows:
                    hwnd = windows[0][0]
                    current_tid = kernel32.GetCurrentThreadId()
                    target_tid = user32.GetWindowThreadProcessId(hwnd, None)
                    attached = False
                    try:
                        if current_tid != target_tid:
                            attached = user32.AttachThreadInput(
                                current_tid, target_tid, True)
                        user32.ShowWindow(hwnd, 9)       # SW_RESTORE
                        user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0003)
                        user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x0003)
                        user32.BringWindowToTop(hwnd)
                        user32.SetForegroundWindow(hwnd)
                        user32.SetActiveWindow(hwnd)
                    finally:
                        if attached:
                            user32.AttachThreadInput(current_tid, target_tid, False)
                    break
                time.sleep(0.15)
            try:
                kernel32.CloseHandle(_single_instance_mutex)
            except Exception:
                pass
            return False
    except Exception:
        pass
    return True


def _expected_chromium_revision():
    """读取当前 playwright 期望的 chromium 版本号（revision）；读不到返回 None。"""
    try:
        import json
        from playwright._impl._driver import compute_driver_executable
        drv = compute_driver_executable()
        node = Path(drv[0] if isinstance(drv, (tuple, list)) else drv)
        for jf in (node.parent / "package" / "browsers.json", node.parent / "browsers.json"):
            if jf.exists():
                data = json.loads(jf.read_text(encoding="utf-8"))
                for b in data.get("browsers", []):
                    if b.get("name") == "chromium":
                        return b.get("revision")
                break
    except Exception:
        return None
    return None


def _chromium_installed():
    """按版本号精确核对当前 playwright 需要的 chromium 是否装好，
    避免本机留有旧版本目录就被误判为已装而跳过下载。"""
    try:
        base = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""))
        rev = _expected_chromium_revision()
        if rev:
            return (base / f"chromium-{rev}" / "chrome-win64" / "chrome.exe").exists()
        # 读不到版本信息时回退：任一 chromium 目录下存在 chrome.exe 即视为已装
        for d in base.glob("chromium-*"):
            if (d / "chrome-win64" / "chrome.exe").exists():
                return True
    except Exception:
        return False
    return False


def _run_driver_install():
    """调用打包进程序的 playwright driver 静默下载 chromium 内核。"""
    creation = subprocess.CREATE_NO_WINDOW if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    try:
        from playwright._impl._driver import compute_driver_executable
        drv = compute_driver_executable()
        if isinstance(drv, (tuple, list)):
            cmd = [str(drv[0]), *[str(x) for x in drv[1:]], "install", "chromium"]
        else:
            cmd = [str(drv), "install", "chromium"]
        subprocess.run(cmd, check=False, timeout=300, creationflags=creation)
    except Exception:
        try:
            subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"],
                           check=False, timeout=300, creationflags=creation)
        except Exception:
            pass


def ensure_browser_installed():
    """检测 chromium 内核是否已装，没装则自动安装（首次运行联网下载）。
    浏览器目录由 PLAYWRIGHT_BROWSERS_PATH 固定到用户目录，不随临时解压目录丢失。"""
    if _chromium_installed():
        return True
    _run_driver_install()
    return _chromium_installed()


_active_pw_lock = threading.Lock()
_active_pw_context = None
_active_pw_thread_id = 0


def _set_active_pw_context(ctx):
    global _active_pw_context, _active_pw_thread_id
    with _active_pw_lock:
        _active_pw_context = ctx
        _active_pw_thread_id = threading.get_ident() if ctx is not None else 0


def _terminate_owned_pw_browser_pids():
    """在非 Playwright 创建线程请求停止时，仅安全终止本进程子树下的 Playwright 浏览器子进程以即时解除阻塞。"""
    if os.name != "nt":
        return
    try:
        class _PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
        k32.Process32FirstW.restype = wintypes.BOOL
        k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
        k32.Process32NextW.restype = wintypes.BOOL
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k32.TerminateProcess.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL

        snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)
        if not snap or snap == wintypes.HANDLE(-1).value:
            return
        procs = {}
        try:
            pe = _PROCESSENTRY32W()
            pe.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
            if k32.Process32FirstW(snap, ctypes.byref(pe)):
                while True:
                    procs[int(pe.th32ProcessID)] = (int(pe.th32ParentProcessID), str(pe.szExeFile or "").lower())
                    if not k32.Process32NextW(snap, ctypes.byref(pe)):
                        break
        finally:
            k32.CloseHandle(snap)

        my_pid = os.getpid()
        browser_names = ("chrome.exe", "chromium.exe", "chrome-headless-shell.exe", "msedge.exe")
        for pid, (ppid, exe) in list(procs.items()):
            if pid == my_pid or exe not in browser_names:
                continue
            cur = ppid
            owned = False
            for _ in range(6):
                if cur == my_pid:
                    owned = True
                    break
                info = procs.get(cur)
                if not info or info[0] == cur or cur <= 4:
                    break
                cur = info[0]
            if owned:
                h_proc = k32.OpenProcess(0x0001, False, pid)
                if h_proc:
                    try:
                        k32.TerminateProcess(h_proc, 1)
                    finally:
                        k32.CloseHandle(h_proc)
    except Exception:
        pass


def _force_close_active_pw_context():
    with _active_pw_lock:
        ctx = _active_pw_context
        tid = _active_pw_thread_id
    if ctx is None:
        return
    if tid and tid == threading.get_ident():
        try:
            ctx.close()
        except Exception:
            pass
    else:
        _terminate_owned_pw_browser_pids()


def _launch_context(p, retried=False):
    """启动持久化浏览器上下文；若报内核缺失，先自动补装一次再重试。"""
    BROWSER_USER_DATA.mkdir(parents=True, exist_ok=True)
    try:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(BROWSER_USER_DATA),
            headless=False,
            viewport={"width": 1920, "height": 1080},
            args=["--start-maximized",
                  "--disable-blink-features=AutomationControlled",
                  "--no-sandbox"],
            accept_downloads=True,
            permissions=["clipboard-read", "clipboard-write"],
        )
        _set_active_pw_context(ctx)
        return ctx
    except Exception as e:
        msg = str(e)
        if not retried and "Executable doesn't exist" in msg:
            log("  浏览器内核版本不符，正在自动补装浏览器，请稍候…")
            ensure_browser_installed()
            return _launch_context(p, retried=True)
        raise

_gui_logger = None

APP_VERSION = "2.9.7"
# 更新检查地址：GitHub 仓库 version.json 的 raw 直链
# version.json 格式：{"version":"2.9.7","url":"新版exe的下载直链"}
DEFAULT_GEMINI_API_KEY = os.environ.get("SHEETMATE_AI_KEY", "")
GEMINI_MODEL_CANDIDATES = (
    "gemini-flash-latest",
    "gemini-3-flash-preview",
    "gemini-3.1-flash-lite-preview",
    "gemini-3.1-pro-preview",
)
UPDATE_CHECK_URL = "https://raw.githubusercontent.com/Sorenskr/SheetMate/main/version.json"

_DEFAULT_RUNNING_HINT = "按键提示：随时按【F8】或点【暂停】让出电脑，忙完按【F8】或点【继续】；点【取消】终止"
_runtime_lock = threading.RLock()
_runtime_cond = threading.Condition(_runtime_lock)
_active_operator_name = "当前成员"
_stop_requested = False
_pause_requested = False
_waiting_recovery = False
_recovery_choice = None
_AUTO_CONFIRM_LOCK = threading.Lock()
_auto_confirmed_recovery_titles = set()
_auto_confirmed_recovery_counts = {}
_dt_logged_in_user_cache = {"name": "", "ts": 0.0}
_runtime_callback = None
_spotlight_callback = None
_training_summary_callback = None
_target_confirm_callback = None
_latest_round_spotlight = None
_last_diagnosed_detail = None
_runtime_prompt = {
    "mode": "running",
    "title": "",
    "detail": "",
    "hint": _DEFAULT_RUNNING_HINT,
    "actions": [],
    "error_detail": None,
    "hands_off": False,
    "hands_off_text": "",
}


_hands_off_depth = 0
_hands_off_text_cur = ""


class TaskStoppedError(RuntimeError):
    """用户手动停止或取消当前任务时抛出的可控中断异常。"""


def _is_stop_exception(exc):
    return isinstance(exc, TaskStoppedError) or ("手动停止" in str(exc or ""))


class DingTalkInterrupted(RuntimeError):
    """发送钉钉过程中检测到用户关闭、最小化或主动切走窗口时抛出的可恢复中断异常。"""


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


_WIN32_IDLE_APIS = None


def _get_win32_idle_apis():
    global _WIN32_IDLE_APIS
    if _WIN32_IDLE_APIS is None and os.name == "nt":
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.GetLastInputInfo.argtypes = [ctypes.POINTER(_LASTINPUTINFO)]
        user32.GetLastInputInfo.restype = wintypes.BOOL
        kernel32.GetTickCount.argtypes = []
        kernel32.GetTickCount.restype = wintypes.DWORD
        _WIN32_IDLE_APIS = (user32, kernel32)
    return _WIN32_IDLE_APIS


def get_user_idle_seconds():
    """读取用户最后一次键鼠输入距今的秒数（复用 Win32 API 句柄，失败时返回较大值以免误判忙碌）。"""
    if os.name != "nt":
        return 999.0
    try:
        apis = _get_win32_idle_apis()
        if not apis:
            return 999.0
        user32, kernel32 = apis
        lii = _LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        if not user32.GetLastInputInfo(ctypes.byref(lii)):
            return 999.0
        elapsed_ms = (int(kernel32.GetTickCount()) - int(lii.dwTime)) & 0xFFFFFFFF
        return max(0.0, elapsed_ms / 1000.0)
    except Exception:
        return 999.0


def set_runtime_callback(fn):
    global _runtime_callback
    with _runtime_lock:
        _runtime_callback = fn


def set_spotlight_callback(fn):
    """注册数据变化播报回调：每轮粘贴或汇总截图更新后实时推送数据增减播报。"""
    global _spotlight_callback
    with _runtime_lock:
        _spotlight_callback = fn


def set_target_confirm_callback(fn):
    """注册钉钉发送对象核对不一致时的双端本人看图确认回调。"""
    global _target_confirm_callback
    with _runtime_lock:
        _target_confirm_callback = fn


def parse_target_confirm_chat_reply(text):
    """解析用户在桌面或手机聊天框中对当前待确认钉钉对象的自然语言答复。"""
    raw = str(text or "").strip()
    if not raw or len(raw) > 80:
        return None
    norm = re.sub(r"[，。！？!?,.~～\s]+", "", raw)
    if not norm:
        return None
    if norm in ("取消", "取消发送", "不发", "不发了", "别发", "别发了", "算了", "中止", "停止发送", "拒绝发送"):
        return {"action": "cancel_send"}
    if norm in ("确认", "确定", "确认发送", "确定发送", "确定发", "确认发", "可以发", "就发这个", "继续发送", "继续", "没错发吧", "确认发当前", "确定继续", "同意发送", "发送吧"):
        return {"action": "confirm_send"}
    m = re.match(
        r"^(?:改为?发给?|改投递给?|换个?群(?:发给)?|换成|切换(?:为|到|成)?|发给|发送给|改到|选群)\s*[：:「\"']?([^」\"'\n，。！？!?,]{1,60})[」\"']?$",
        raw,
    )
    if m:
        cand = str(m.group(1) or "").strip("「」\"'，。！？!?, ")
        if cand and cand not in ("当前", "这个", "上面那个", "本人", "我"):
            return {"action": "switch_target", "new_target": cand}
    return None


def wait_for_target_confirmation(info, timeout_sec=600):
    """当 AI 桌面识屏发现对象不一致或无法确认时，阻塞等待本人看图确认（绝不自动跳过）。"""
    check_stop()
    cb = _target_confirm_callback
    if cb is None:
        return {"action": "cancel_send", "reason": "no_callback"}
    payload = dict(info) if isinstance(info, dict) else {}
    res = cb(payload, timeout_sec=timeout_sec)
    check_stop()
    if not isinstance(res, dict):
        return {"action": "cancel_send", "reason": "invalid_response"}
    act = str(res.get("action") or "cancel_send").strip()
    if act not in ("confirm_send", "switch_target", "cancel_send"):
        act = "cancel_send"
    out = {"action": act, "new_target": str(res.get("new_target") or "").strip()[:80]}
    if act == "switch_target" and not out["new_target"]:
        return {"action": "cancel_send", "reason": "empty_new_target"}
    return out


def get_latest_round_spotlight():
    with _runtime_lock:
        return dict(_latest_round_spotlight) if isinstance(_latest_round_spotlight, dict) else None


def _default_running_prompt():
    return {
        "mode": "running",
        "title": "",
        "detail": "",
        "hint": _DEFAULT_RUNNING_HINT,
        "actions": [],
        "error_detail": None,
        "hands_off": False,
        "hands_off_text": "",
    }


def set_active_operator_name(name):
    """设置当前操作者昵称，用于在粘贴表格或发钉钉期间醒目提示防误触。"""
    global _active_operator_name
    clean = str(name or "").strip()
    with _runtime_lock:
        _active_operator_name = clean or "当前成员"


def get_runtime_state():
    """返回线程安全的运行时暂停、恢复、防误触提醒与操作指引状态快照。"""
    with _runtime_lock:
        p = _runtime_prompt or _default_running_prompt()
        ed = p.get("error_detail")
        return {
            "paused": bool(_pause_requested),
            "stop_requested": bool(_stop_requested),
            "waiting_recovery": bool(_waiting_recovery),
            "hands_off": bool(p.get("hands_off")),
            "hands_off_text": str(p.get("hands_off_text") or ""),
            "operator_name": _active_operator_name,
            "prompt": {
                "mode": p.get("mode", "running"),
                "title": p.get("title", ""),
                "detail": p.get("detail", ""),
                "hint": p.get("hint", _DEFAULT_RUNNING_HINT),
                "actions": [dict(a) for a in (p.get("actions") or [])],
                "error_detail": dict(ed) if isinstance(ed, dict) else None,
                "hands_off": bool(p.get("hands_off")),
                "hands_off_text": str(p.get("hands_off_text") or ""),
            },
        }


def _notify_runtime_state():
    with _runtime_lock:
        cb = _runtime_callback
    if cb:
        try:
            cb(get_runtime_state())
        except Exception:
            pass


def _apply_hands_off_to_prompt(prompt_dict):
    if isinstance(prompt_dict, dict) and _hands_off_depth > 0 and _hands_off_text_cur:
        prompt_dict["hands_off"] = True
        prompt_dict["hands_off_text"] = _hands_off_text_cur
    return prompt_dict


def hands_off_guard(action_desc):
    """键鼠敏感粘贴/操作上下文守卫：全程展示『目前XX正在操作，请勿动键鼠』醒目提示。"""
    from contextlib import contextmanager

    @contextmanager
    def _cm():
        global _hands_off_depth, _hands_off_text_cur
        prev_txt = ""
        with _runtime_lock:
            op = (_active_operator_name or "当前成员").strip() or "当前成员"
            msg = f"目前【{op}】正在{action_desc}，请勿触碰鼠标或键盘！（急停按 F8）"
            prev_txt = _hands_off_text_cur
            _hands_off_depth += 1
            _hands_off_text_cur = msg
            if not _pause_requested and not _waiting_recovery and not _stop_requested:
                _runtime_prompt["hands_off"] = True
                _runtime_prompt["hands_off_text"] = msg
        _notify_runtime_state()
        try:
            yield
        finally:
            with _runtime_lock:
                _hands_off_depth = max(0, _hands_off_depth - 1)
                _hands_off_text_cur = prev_txt if _hands_off_depth > 0 else ""
                if _hands_off_depth <= 0 or _stop_requested:
                    _runtime_prompt["hands_off"] = False
                    _runtime_prompt["hands_off_text"] = ""
                elif not _pause_requested and not _waiting_recovery:
                    _runtime_prompt["hands_off"] = True
                    _runtime_prompt["hands_off_text"] = _hands_off_text_cur
            _notify_runtime_state()

    return _cm()


def _dismiss_dt_popup_nonblocking():
    """取消任务时在后台瞬间清理残留搜索框或弹出菜单，避免卡住桌面。"""
    if os.name != "nt":
        return
    try:
        hwnd = int(_active_dt_hwnd or 0)
        if hwnd:
            u32 = ctypes.windll.user32
            u32.PostMessageW(hwnd, 0x0100, 0x1B, 0)  # WM_KEYDOWN VK_ESCAPE
            u32.PostMessageW(hwnd, 0x0101, 0x1B, 0)  # WM_KEYUP VK_ESCAPE
    except Exception:
        pass


def request_stop():
    """请求立即取消/停止当前任务（毫秒级唤醒所有休眠/暂停/等待，绝不转圈拖延）。"""
    global _stop_requested, _pause_requested, _recovery_choice, _runtime_prompt
    with _runtime_cond:
        _stop_requested = True
        _pause_requested = False
        if _waiting_recovery:
            _recovery_choice = "cancel"
        _runtime_prompt = {
            "mode": "stopped",
            "title": "已取消本轮任务",
            "detail": "",
            "hint": "任务已终止",
            "actions": [],
            "error_detail": None,
            "hands_off": False,
            "hands_off_text": "",
        }
        _runtime_cond.notify_all()
    threading.Thread(target=_force_close_active_pw_context, daemon=True).start()
    threading.Thread(target=_dismiss_dt_popup_nonblocking, daemon=True).start()
    _notify_runtime_state()


def request_pause():
    """请求暂停当前执行，毫秒级挂起可中断点并保持浏览器与图片完好。"""
    global _pause_requested, _runtime_prompt
    with _runtime_cond:
        if _stop_requested:
            return False, "任务已取消"
        if _waiting_recovery:
            return False, "当前已在等待排障，请按提示处理后点击恢复按钮"
        if _runtime_prompt.get("mode") == "login_wps":
            return False, "当前正在等待 WPS 登录，请扫码后继续"
        if _pause_requested:
            return True, "当前已处于暂停状态"
        _pause_requested = True
        _runtime_prompt = {
            "mode": "paused",
            "title": "任务已暂停",
            "detail": "自动键鼠与浏览器操作已安全挂起，您可先处理其他工作；\n已下载或已截取的内容均会保留，无需重头开始。",
            "hint": "按键提示：忙完后按键盘【F8】或点【继续运行 (F8)】接着跑未完成步骤；点【取消】结束本轮。",
            "actions": [
                {"id": "continue", "label": "继续运行 (F8)", "style": "primary"},
            ],
            "hands_off": False,
            "hands_off_text": "",
        }
        _runtime_cond.notify_all()
    _notify_runtime_state()
    log("⏸ 已暂停执行（进度已保留，按【F8】或点【继续】可接着运行）")
    return True, "已暂停，按【F8】或点击继续恢复"


def request_resume(action="continue"):
    """恢复暂停的任务，或在等待故障处理时传入用户的恢复选项。"""
    global _pause_requested, _recovery_choice, _runtime_prompt
    act = str(action or "continue").strip() or "continue"
    if act == "cancel":
        request_stop()
        return True, "已取消任务"
    msg = "已发送继续指令"
    need_log = False
    with _runtime_cond:
        if _waiting_recovery:
            _recovery_choice = act
            msg = "已收到恢复操作，正在继续当前流程…"
            try:
                record_run_trace_event("user_recovery", act)
            except Exception:
                pass
        elif _pause_requested:
            _pause_requested = False
            _runtime_prompt = _apply_hands_off_to_prompt(_default_running_prompt())
            msg = "已恢复运行，将继续执行下一步"
            need_log = True
        elif _runtime_prompt.get("mode") == "login_wps":
            _recovery_choice = "check_now"
            msg = "正在立即检测 WPS 登录状态…"
        _runtime_cond.notify_all()
    _notify_runtime_state()
    if need_log:
        log("▶ 已恢复运行，继续执行未完成步骤…")
    return True, msg


def trigger_hotkey_action(slot="f8"):
    """响应全局快捷键（F8=暂停或继续未完成步骤；F9=已开群直接粘贴；cancel=取消）。"""
    key = str(slot or "f8").strip().lower()
    if key == "cancel":
        request_stop()
        return True, "已发送取消指令"

    if key == "f9":
        with _runtime_lock:
            if not _waiting_recovery:
                return False, "当前未处于等待直接粘贴排障状态"
            actions = list(_runtime_prompt.get("actions") or [])
        preferred = ("skip_search", "retry_paste", "manual_copied", "continue")
        chosen = None
        for pid in preferred:
            if any(a.get("id") == pid for a in actions):
                chosen = pid
                break
        if chosen is None and len(actions) >= 2:
            chosen = actions[1].get("id")
        elif chosen is None and actions:
            chosen = actions[0].get("id")
        if not chosen:
            return False, "当前暂无可执行快捷键动作"
        log(f"⌨️ 快捷键【F9】触发快速恢复：{chosen}")
        return request_resume(chosen)

    # 默认 F8：运行中 -> 暂停；暂停中/排障等待 -> 继续或执行主恢复动作
    with _runtime_lock:
        if _stop_requested:
            return False, "任务正在取消中"
        in_rec = _waiting_recovery or (_runtime_prompt.get("mode") == "login_wps")
        actions = list(_runtime_prompt.get("actions") or [])
        is_paused = _pause_requested
    if in_rec:
        act_id = (actions[0].get("id") if actions else None) or "retry"
        log(f"⌨️ 快捷键【F8】触发继续未完成步骤（{act_id}）")
        return request_resume(act_id)
    if is_paused:
        log("⌨️ 快捷键【F8】触发恢复执行")
        return request_resume("continue")
    log("⌨️ 快捷键【F8】触发暂停任务")
    return request_pause()


class _RuntimeHotkeyManager:
    """任务运行期间按需注册系统级快捷键（F8/F9），任务结束立即卸载避免干扰日常软件。"""

    _ID_F8 = 0x5101
    _ID_F9 = 0x5102

    def __init__(self):
        self._lock = threading.Lock()
        self._ref_count = 0
        self._stop_evt = None
        self._thread = None
        self._f8_label = "F8"
        self._f9_label = "F9"

    def active_labels(self):
        with self._lock:
            return self._f8_label, self._f9_label

    def start(self):
        if os.name != "nt":
            return
        old_join = None
        with self._lock:
            if self._thread is not None and self._thread.is_alive() and self._stop_evt is not None and self._stop_evt.is_set():
                old_join = self._thread
        if old_join is not None and old_join is not threading.current_thread():
            old_join.join(timeout=0.8)
        with self._lock:
            self._ref_count += 1
            if self._thread is not None and self._thread.is_alive() and (self._stop_evt is None or not self._stop_evt.is_set()):
                return
            self._stop_evt = threading.Event()
            ev = self._stop_evt
            t = threading.Thread(target=self._loop, args=(ev,), daemon=True, name="SheetMateHotkeys")
            self._thread = t
            t.start()

    def stop(self):
        if os.name != "nt":
            return
        t_join = None
        with self._lock:
            self._ref_count = max(0, self._ref_count - 1)
            if self._ref_count > 0:
                return
            if self._stop_evt is not None:
                self._stop_evt.set()
            t_join = self._thread
        if t_join is not None and t_join is not threading.current_thread():
            t_join.join(timeout=0.9)
        with self._lock:
            if self._ref_count == 0:
                if self._thread is t_join and (t_join is None or not t_join.is_alive()):
                    self._thread = None
                self._f8_label = "F8"
                self._f9_label = "F9"

    def _loop(self, stop_evt):
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
            user32.RegisterHotKey.restype = wintypes.BOOL
            user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.UnregisterHotKey.restype = wintypes.BOOL
            user32.PeekMessageW.argtypes = [
                ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT,
            ]
            user32.PeekMessageW.restype = wintypes.BOOL

            MOD_NOREPEAT = 0x4000
            MOD_CTRL_SHIFT = 0x0002 | 0x0004
            VK_F8, VK_F9 = 0x77, 0x78
            WM_HOTKEY = 0x0312
            PM_REMOVE = 0x0001

            msg = wintypes.MSG()
            user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)

            registered = []
            f8_lbl = "F8"
            if (user32.RegisterHotKey(None, self._ID_F8, MOD_NOREPEAT, VK_F8)
                    or user32.RegisterHotKey(None, self._ID_F8, 0, VK_F8)):
                registered.append(self._ID_F8)
            elif user32.RegisterHotKey(None, self._ID_F8, MOD_CTRL_SHIFT | MOD_NOREPEAT, VK_F8):
                registered.append(self._ID_F8)
                f8_lbl = "Ctrl+Shift+F8"
            else:
                f8_lbl = ""

            f9_lbl = "F9"
            if (user32.RegisterHotKey(None, self._ID_F9, MOD_NOREPEAT, VK_F9)
                    or user32.RegisterHotKey(None, self._ID_F9, 0, VK_F9)):
                registered.append(self._ID_F9)
            elif user32.RegisterHotKey(None, self._ID_F9, MOD_CTRL_SHIFT | MOD_NOREPEAT, VK_F9):
                registered.append(self._ID_F9)
                f9_lbl = "Ctrl+Shift+F9"
            else:
                f9_lbl = ""

            with self._lock:
                if f8_lbl:
                    self._f8_label = f8_lbl
                if f9_lbl:
                    self._f9_label = f9_lbl

            try:
                while not stop_evt.is_set():
                    while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                        if msg.message == WM_HOTKEY:
                            hid = int(msg.wParam)
                            if hid == self._ID_F8:
                                trigger_hotkey_action("f8")
                            elif hid == self._ID_F9:
                                trigger_hotkey_action("f9")
                    stop_evt.wait(0.08)
            finally:
                for hid in registered:
                    try:
                        user32.UnregisterHotKey(None, hid)
                    except Exception:
                        pass
        except Exception:
            pass


_HOTKEY_MGR = _RuntimeHotkeyManager()


def reset_stop():
    global _stop_requested, _pause_requested, _waiting_recovery, _recovery_choice, _runtime_prompt, _last_diagnosed_detail
    with _runtime_cond:
        _stop_requested = False
        global _hands_off_depth, _hands_off_text_cur
        _pause_requested = False
        _waiting_recovery = False
        _recovery_choice = None
        _last_diagnosed_detail = None
        _hands_off_depth = 0
        _hands_off_text_cur = ""
        with _AUTO_CONFIRM_LOCK:
            _auto_confirmed_recovery_titles.clear()
            _auto_confirmed_recovery_counts.clear()
        _runtime_prompt = _default_running_prompt()
        _runtime_cond.notify_all()
    _notify_runtime_state()


def check_stop():
    """在任何可安全中断点检查取消信号与暂停信号（通过 Condition 毫秒级唤醒，绝不卡顿）。"""
    was_paused = False
    with _runtime_cond:
        while True:
            if _stop_requested:
                raise TaskStoppedError("用户手动停止了任务")
            if not _pause_requested:
                break
            was_paused = True
            _runtime_cond.wait(timeout=0.5)
    if was_paused:
        _notify_runtime_state()


def sleep_check(seconds):
    """可毫秒级瞬时响应暂停/继续/取消的分段休眠（暂停期间不扣减等待时长）。"""
    remaining = max(0.0, float(seconds))
    was_paused = False
    with _runtime_cond:
        while True:
            if _stop_requested:
                raise TaskStoppedError("用户手动停止了任务")
            if _pause_requested:
                was_paused = True
                _runtime_cond.wait(timeout=0.5)
                continue
            if remaining <= 0:
                break
            step = min(0.25, remaining)
            t0 = time.monotonic()
            _runtime_cond.wait(timeout=step)
            if not _pause_requested and not _stop_requested:
                remaining -= (time.monotonic() - t0)
    if was_paused:
        _notify_runtime_state()


def probe_network(timeout=2.4):
    """快速探测国内基础网络与 Luya 后台连通性，区分断网与未开启 VPN/代理异常。"""
    def _http_ok(url):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "SheetMate"})
            with urllib.request.urlopen(req, timeout=timeout):
                return True
        except urllib.error.HTTPError as he:
            # 只要收到 HTTP 响应（如 401/403/404/302），说明网络与节点是通的
            try:
                he.close()
            except Exception:
                pass
            return True
        except Exception:
            return False

    def _tcp_ok(host, port=443):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except Exception:
            return False

    internet_ok = _http_ok("https://www.baidu.com") or _http_ok("https://www.kdocs.cn")
    if not internet_ok:
        internet_ok = _tcp_ok("223.5.5.5", 443) or _tcp_ok("119.29.29.29", 443)
    luya_ok = _http_ok("https://bb.luyaapp.live") if internet_ok else False
    wps_ok = _http_ok("https://www.kdocs.cn") if internet_ok else False
    return {
        "internet_ok": bool(internet_ok),
        "luya_ok": bool(luya_ok),
        "wps_ok": bool(wps_ok),
    }


def classify_error_detail(exc=None, stage="", extra=None):
    """将异常与运行阶段映射为结构化报错代码、大白话原因、自助解决步骤与给开发者的修改指令。"""
    import traceback
    extra = extra or {}
    err_s = str(exc or "").strip()
    low = err_s.lower()
    st = str(stage or "").strip().lower()

    tb_loc = ""
    if isinstance(exc, BaseException) and getattr(exc, "__traceback__", None) is not None:
        try:
            frames = traceback.extract_tb(exc.__traceback__)
            if frames:
                last_f = frames[-1]
                tb_loc = f"{Path(last_f.filename).name}:{last_f.lineno} ({last_f.name})"
        except Exception:
            tb_loc = ""

    def _pack(code, title, plain_reason, self_fix, tell_dev_goal):
        loc_part = f"（定位：{tb_loc}）" if tb_loc else ""
        raw_short = err_s[:160] if err_s else title
        tell_dev = (
            f"我的暗号表助手 v{APP_VERSION} 报错了，错误代码是【{code}】（{title}）。"
            f"系统提示是：「{raw_short}」{loc_part}。请你帮我{tell_dev_goal}，改好后打包新版本给我。"
        )
        return {
            "code": code,
            "title": title,
            "plain_reason": plain_reason,
            "self_fix": self_fix,
            "tell_dev": tell_dev,
            "raw_error": raw_short,
            "location": tb_loc,
        }

    # 1. 网络与 VPN
    if ("err_internet_disconnected" in low or "err_name_not_resolved" in low
            or st == "net_offline" or extra.get("internet_ok") is False):
        return _pack(
            "ERR-NET-101",
            "电脑没有连上网",
            "电脑当前断网或 DNS 没连上，后台和在线表格都打不开。",
            "1. 检查右下角 Wi-Fi 或网线图标，先确保浏览器能打开百度；\n2. 连上网后点【已连网·重试当前步】直接接着跑。",
            "增强断网重连的等待时间和自动重试次数",
        )

    if (st == "vpn_blocked"
            or (st in ("luya_open", "luya_login", "luya_download")
                and any(k in low for k in ("vpn", "proxy", "tunnel", "err_timed_out", "err_connection", "timeout", "net::err")))
            or extra.get("luya_ok") is False):
        return _pack(
            "ERR-VPN-102",
            "能上网，但没开 VPN 或后台连不通",
            "电脑外网通着，但打不开 Luya 数据后台（bb.luyaapp.live），通常是忘了开梯子/VPN。",
            "1. 打开您电脑上的 VPN 软件，确认节点已连上且系统代理打开；\n2. 点【已开VPN·重试当前步】就会从这一步继续往下抓。",
            "检查 Luya 后台域名连通与代理超时策略",
        )

    # 2. 浏览器内核
    if "executable doesn't exist" in low or "chromium" in low or "browser" in low or "launch" in low:
        return _pack(
            "ERR-BRW-105",
            "内置浏览器打不开或组件缺损",
            "自动跑表用的内置浏览器没找到文件，或者旧浏览器进程卡住占用了目录。",
            "1. 先在任务管理器确认没有残留卡死的 chrome.exe 后重新运行；\n2. 确保联网后再打开软件，让它自动补齐浏览器组件。",
            "优化内置浏览器启动失败与残余进程自动清理逻辑",
        )

    # 3. Luya 后台登录 / 下载
    if st == "luya_login" or extra.get("luya_need_login") or ("luya" in low and ("login" in low or "登录" in err_s)):
        return _pack(
            "ERR-LUYA-103",
            "Luya 数据后台需要重新登录",
            "后台账号密码过期了，或者网页弹了验证码需要手动点一下。",
            "1. 在弹出的浏览器窗口里手动完成登录，进入数据页面；\n2. 点【我已登录·继续】；也可以在「记录账号」页核对 Luya 账号密码。",
            "适配 Luya 后台最新的登录页表单结构或密码填充逻辑",
        )

    if st == "luya_download" or "download" in low or "数据表" in err_s or "下载" in err_s:
        tbl = extra.get("name") or "数据表"
        return _pack(
            "ERR-LUYA-104",
            f"下载【{tbl}】超时或没找到下载按钮",
            f"后台网页加载太慢或按钮位置变了，导致【{tbl}】没有按时下到本地。",
            "1. 先看下 VPN 节点是不是掉线或卡顿，看浏览器里数据出来没；\n2. 点击【重试下载当前表】，已经下完的其他表会自动保留不重抓。",
            f"排查 Luya 后台【{tbl}】页面下载按钮或表格渲染等待逻辑",
        )

    # 4. 钉钉相关错误码（需先于通用 WPS/timeout 判断）
    if "权限不一致" in err_s or "ui.pi" in low or "uipi" in low:
        return _pack(
            "ERR-DT-302",
            "软件和钉钉的权限高低不一样，点击被系统拦截",
            "一个用了管理员权限运行，另一个是普通权限，Windows 安全机制禁止软件帮您点钉钉。",
            "1. 把钉钉和暗号表助手都关闭；\n2. 两个软件都按普通双击打开（或都右键“以管理员身份运行”）。",
            "增强钉钉与软件权限一致性校验及弹窗引导",
        )

    if st == "dingtalk_login" or "钉钉主窗口" in err_s or "扫码" in err_s and "钉钉" in err_s:
        return _pack(
            "ERR-DT-301",
            "钉钉还没打开或停留在扫码登录界面",
            "程序找不到已经登录进聊天主界面的钉钉窗口。",
            "1. 在电脑上点开钉钉并扫码登录，看到聊天列表主界面；\n2. 按键盘【F8】或点【已登录钉钉·继续发送】，刚截好的图片会直接发出。",
            "优化钉钉托盘唤起与登录主窗口的识别类名",
        )

    if st == "dingtalk_search" or "未能自动搜到" in err_s or "没能定位到钉钉会话" in err_s or "一对一匹配" in err_s:
        tgt = extra.get("search_name") or "目标联系人"
        if "一对一匹配" in err_s or "文件传输助手" in err_s:
            return _pack(
                "ERR-DT-303",
                f"未找到与「{tgt}」一对一匹配的钉钉对象",
                err_s,
                "1. 请核对人名或群名是否有误；\n2. 确认数据文件与钉钉通讯录中有此人后再发（已安全拦截，不会误发给文件传输助手）。",
                f"核对联系人「{tgt}」姓名拼写与花名册匹配逻辑",
            )
        return _pack(
            "ERR-DT-303",
            f"钉钉里没搜到「{tgt}」",
            f"按 Ctrl+Shift+F 搜索「{tgt}」时没有直接进入聊天框，可能是群名有特殊符号或搜索被遮挡。",
            "1. 去「钉钉投递」页核对目标群名是不是一字不差；\n2. 或者您直接在钉钉手动点开这个群窗口，再按【F9】直接贴图发送。",
            f"优化钉钉搜索会话「{tgt}」时的焦点激活和群聊名称模糊匹配",
        )

    if (
        st == "dingtalk_interrupted"
        or isinstance(exc, DingTalkInterrupted)
        or "自动暂停" in err_s
        or "主动切换" in err_s
        or "最小化" in err_s
        or "关闭" in err_s
        or "隐藏" in err_s
        or "托盘" in err_s
    ):
        return _pack(
            "ERR-DT-305",
            "检测到您正在操作或已中途收起钉钉，已自动暂停避让",
            "发送中途检测到您手动切换了其他窗口或收起了钉钉，软件先暂停以免误点错发；图片都安全保留着。",
            "1. 忙完后无需手动找窗口，直接按键盘【F8】或点【继续未完成步骤 (F8)】，软件会自动唤醒钉钉接着发；\n2. 若您已手动点开要发的群，按【F9】可直接贴图继续。",
            "调整钉钉前台窗口占用的检测灵敏度，避免最大化或重绘时误触发暂停",
        )

    if (st == "dingtalk_paste" or "钉钉" in err_s or "静音" in err_s or "搜索框" in err_s
            or "光标" in err_s or "uia" in low):
        return _pack(
            "ERR-DT-304",
            "钉钉粘贴图片、切输入焦点或点静音菜单卡住了",
            "钉钉窗口最大化切换、焦点停在搜索框或弹出菜单层级变化，导致下一步动作没接上。",
            "1. 点开钉钉里要发送的目标群聊天窗口，关闭遮挡小浮窗，点一下打字输入框；\n2. 按【F9】或点【直接粘贴重试】把已保存的图片直接贴进去继续；\n3. 如果每次最大化都报这个错，请点下方【复制修改指令】发给我处理。",
            "检查钉钉窗口最大化后 send_dingtalk_image 与设为静音的定位和焦点校验逻辑",
        )

    # 5. WPS 相关错误码
    if st == "wps_open" or "wps" in low and ("登录" in err_s or "超时" in err_s or "open" in low):
        return _pack(
            "ERR-WPS-201",
            "WPS 在线表格打不开或扫码登录超时",
            "金山文档链接没打开、网络卡顿或者需要重新扫码登录 WPS。",
            "1. 去「记录账号」里核对 WPS 链接是不是最新地址；\n2. 在弹出的浏览器窗口里扫码登录后点【我已扫码·继续】。",
            "延长 WPS 在线表格打开等待时间或兼容新版登录页面元素",
        )

    if st == "wps_paste" or "工作表" in err_s or "写入" in err_s:
        tbl = extra.get("name") or "工作表"
        return _pack(
            "ERR-WPS-202",
            f"往 WPS 的【{tbl}】写入数据时失败",
            f"在表格底部没找到【{tbl}】标签页，或者表格当前只读/被弹窗挡住了。",
            f"1. 打开 WPS 表格看底部是否有【{tbl}】标签、自己账号是否有编辑权限；\n2. 点开表格关闭弹窗后点【重试写入当前表】。",
            f"检查切换 WPS【{tbl}】工作表标签或粘贴写入的稳定性",
        )

    if "预设" in err_s or "名称框" in err_s or "edit-box" in low:
        return _pack(
            "ERR-WPS-203",
            "汇总表自动校准单元格（如 D3/L3/Y3/AA5）没成功",
            "往左上角名称框输入位置并写入固定值或日期时没有生效。",
            "1. 检查「钉钉投递 -> 格位校准」里填写的单元格位置（例如 D3/L3/Y3/AA5）对不对；\n2. 确认您对数据汇总表有编辑权限（没有被锁定保护）。",
            "优化 WPS 左上角名称框校准单元格及公式编辑栏写入回读逻辑",
        )

    if st == "wps_capture" or "截图" in err_s or "选区" in err_s or "复制为图片" in err_s or "画布" in err_s:
        return _pack(
            "ERR-WPS-204",
            "WPS 汇总表选区截图或「复制为图片」失败",
            "选中 C6:AK 后右键菜单没弹出“复制为图片”，或图表还没加载完。",
            "1. 点【重试自动截图】再试一次；\n2. 也可以自己在弹出的表格里框选后右键「复制为图片」，再点【我已手动复制·发钉钉】。",
            "优化汇总表选区定位、右键「复制为图片」与剪贴板等图超时阈值",
        )

    if "剪贴板" in err_s or "clipboard" in low or "uapg" in low:
        return _pack(
            "ERR-CLIP-205",
            "系统剪贴板写入/读取被其他软件占用或编码异常",
            "电脑上的剪贴板历史、翻译或录屏工具抢占了剪贴板，导致贴图/贴字没成功。",
            "1. 暂时关掉抢占剪贴板的工具（如微信截图、剪贴板工具）后点重试；\n2. 如出现乱码（例如慕杰组变奇怪字母），请复制下方报错代码让我立即修复。",
            "排查剪贴板写入竞争与 Win32 Unicode 编码传输问题",
        )

    # 6. Python 内部未预期代码异常（如 TypeError / AttributeError 等）
    if ("nonetype" in low or "typeerror" in low or "attributeerror" in low
            or "keyerror" in low or "indexerror" in low or isinstance(exc, (TypeError, AttributeError, KeyError, IndexError))):
        return _pack(
            "ERR-SYS-500",
            "程序内部代码遇到空值或逻辑小漏洞",
            f"这不是您操作的问题，是程序内部代码遇到了意外空值（原报错：{err_s[:80]}）。",
            "1. 先点【重试】或重新打开软件看能否绕过；\n2. 强烈建议点击下方【复制给开发者的话】直接发给我，我可以对照位置一分钟帮您修好！",
            f"修复【{err_s[:100]}】导致的内部逻辑异常",
        )

    return _pack(
        "ERR-RUN-900",
        "运行步骤中遇到意外阻断",
        f"当前步骤执行没成功（原因：{err_s[:100] or '未知提示'}）。",
        "1. 查看右侧/日志页红字提示，确认网络、WPS 和钉钉都在正常登录状态后重试；\n2. 搞不定可点【让 AI 分析】或【复制给开发者的话】发给我。",
        f"排查并修复运行时异常「{err_s[:100]}」",
    )


_SHEETMATE_CODE_ARCHITECTURE = """【软件源码模块地图（E:\\cord\\SheetMate）】
- 抓数与填表（auto_report.py）：run_once -> _run_once_impl -> download_xlsx -> xlsx_to_tsv -> _open_and_login_wps -> paste_to_wps
- 汇总表校准与截图（auto_report.py）：_capture_summary_with_recovery -> switch_wps_sheet -> apply_wps_summary_presets -> apply_wps_update_timestamp -> 等待 wps_refresh_wait_sec -> capture_wps_region
- 钉钉投递模块（auto_report.py）：send_dingtalk_image -> _send_one_target -> _enum_dingtalk_im_windows（过滤钉钉-文档）-> _restore_and_activate_hwnd（唤醒）-> 全局搜索（Ctrl+Shift+F）-> 等待 dt_paste_delay_sec -> 默认/防卡顿执行顺序（_do_paste_image_step / _do_paste_text_step / _do_toggle_silent_step）-> _enable_dingtalk_silent_send（_enable_dingtalk_silent_send_uia / _enable_dingtalk_silent_send_image）-> _commit_dingtalk_send（富文本补点发送按钮）
- 智能复盘与前端联动（auto_report.py + ios_ui.py）：classify_error_detail -> build_historical_failure_diff_context -> recommend_pipeline_tuning_for_error -> build_antigravity_prompt -> ask_gemini_assistant / ios_ui._SheetMateController._on_runtime_state / apply_ai_tuning"""


def _run_journal_path():
    return app_dir() / "run_journal.jsonl"


def _extract_log_traits(logs_lines):
    """从单轮运行日志提取关键环境与行为特征，便于自动比较两次运行为何一次成功一次失败。"""
    txt = "\n".join(str(x) for x in (logs_lines or []))
    return {
        "woke_from_min": ("最小化状态" in txt or "隐藏在后台托盘" in txt),
        "minimized_at_end": ("已自动最小化钉钉窗口" in txt),
        "used_uia_silent": ("已选择「静音发送」" in txt and "pywinauto-UIA" in txt),
        "used_img_silent": ("pyautogui-图像匹配兜底" in txt),
        "paste_retried": ("自动补粘" in txt),
        "search_retried": ("搜索窗口没打开" in txt),
    }


_JOURNAL_IO_LOCK = threading.RLock()


def record_run_journal(cfg, status, mode="full", round_label="", error_detail=None, duration_sec=0, logs_tail=None):
    """追加记录每次成功或受阻/失败的运行快照至 run_journal.jsonl（保留最近 100 条）。"""
    try:
        c = normalize_config(cfg if isinstance(cfg, dict) else {})
        pt = c.get("pipeline_tuning") or {}
        tail = list(logs_tail) if logs_tail is not None else _get_recent_logs_tail(45)
        ed = error_detail if isinstance(error_detail, dict) else {}
        entry = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "status": str(status or "ok"),
            "mode": str(mode or "full"),
            "round": str(round_label or ""),
            "err_code": str(ed.get("code") or ""),
            "err_title": str(ed.get("title") or ""),
            "raw_error": str(ed.get("raw_error") or "")[:180],
            "duration_sec": int(duration_sec or 0),
            "traits": _extract_log_traits(tail),
            "pipeline_cfg": {
                "dt_paste_delay_sec": c.get("dt_paste_delay_sec", 3),
                "dt_send_wait_sec": c.get("dt_send_wait_sec", 5),
                "dt_minimize_after_send": bool(c.get("dt_minimize_after_send", True)),
                "compose_step_order": pt.get("compose_step_order", "image_text_silent"),
                "silent_step_order": pt.get("silent_step_order", "uia_first"),
                "wps_refresh_wait_sec": pt.get("wps_refresh_wait_sec", 5),
            },
            "log_excerpt": [str(x) for x in tail[-12:]],
        }
        with _JOURNAL_IO_LOCK:
            p = _run_journal_path()
            lines = []
            if p.exists():
                lines = [ln for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip()]
            lines.append(json.dumps(entry, ensure_ascii=False))
            lines = lines[-100:]
            _atomic_write_text(p, "\n".join(lines) + "\n")
        return entry
    except Exception:
        return None


def load_run_journal(limit=30):
    """读取最近 N 条结构化运行日记（按时间由旧到新排序）。"""
    p = _run_journal_path()
    if not p.exists():
        return []
    out = []
    try:
        for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = ln.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
                if isinstance(obj, dict):
                    out.append(obj)
            except Exception:
                pass
    except Exception:
        return []
    return out[-max(1, int(limit)):]


def parse_run_log_sessions(log_path=None, max_sessions=12):
    """解析 run.log 尾部（最多 600 行），按轮次提取每轮成功/失败状态、错误码与关键特征。"""
    p = Path(log_path) if log_path is not None else (app_dir() / "run.log")
    if not p.exists():
        return []
    try:
        raw_lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return []
    lines = raw_lines[-600:]
    sessions = []
    cur = None

    for ln in lines:
        if "📊 开始第 " in ln or "📸 开始独立截图发送" in ln:
            if cur is not None:
                sessions.append(cur)
            m_dt = re.search(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})", ln)
            m_tm = re.search(r"\[(\d{2}:\d{2}:\d{2})\]", ln)
            start_t = m_dt.group(1) if m_dt else (m_tm.group(1) if m_tm else "")
            m_rnd = re.search(r"开始(第\s*\S+\s*轮)", ln)
            r_lbl = m_rnd.group(1) if m_rnd else ("独立截图" if "独立截图" in ln else "运行轮")
            cur = {"start_time": start_t, "round": r_lbl, "lines": [ln]}
        elif cur is not None:
            cur["lines"].append(ln)

    if cur is not None:
        sessions.append(cur)

    parsed = []
    for s in sessions[-max(1, int(max_sessions)):]:
        slines = s["lines"]
        joined = "\n".join(slines)
        errs = []
        for c in re.findall(r"ERR-[A-Z]+-\d+", joined):
            if c not in errs:
                errs.append(c)
        if "本轮全部完成" in joined or "截图发送已完成" in joined:
            st = "ok"
        elif errs or any(k in joined for k in ("失败", "受阻", "自动暂停", "手动停止")):
            st = "err"
        else:
            st = "running"
        parsed.append({
            "start_time": s["start_time"],
            "round": s["round"],
            "status": st,
            "err_codes": errs,
            "traits": _extract_log_traits(slines),
            "tail": slines[-8:],
        })
    return parsed


def build_historical_failure_diff_context(cfg, error_detail=None, recent_logs=None):
    """对比最近一次成功轮与本次/最近失败轮，自动提炼“为何上次能成、这次失败”的因果依据。"""
    from collections import Counter

    c = normalize_config(cfg if isinstance(cfg, dict) else {})
    ed = error_detail if isinstance(error_detail, dict) else {}
    cur_code = str(ed.get("code") or "")
    journal = load_run_journal(30)
    sessions = parse_run_log_sessions(max_sessions=12)

    err_counts = Counter()
    for s in sessions:
        for ec in (s.get("err_codes") or []):
            err_counts[ec] += 1
    for j in journal:
        ec = j.get("err_code")
        if ec and not any(ec in (s.get("err_codes") or []) for s in sessions):
            err_counts[ec] += 1
    if cur_code and err_counts[cur_code] == 0:
        err_counts[cur_code] = 1

    ok_candidates = [
        {"when": s.get("start_time") or s.get("round"), "traits": s.get("traits") or {}, "label": s.get("round") or "成功轮"}
        for s in sessions if s.get("status") == "ok"
    ]
    if not ok_candidates:
        ok_candidates = [
            {"when": j.get("ts") or j.get("round"), "traits": j.get("traits") or {}, "label": j.get("round") or "成功轮"}
            for j in journal if j.get("status") == "ok"
        ]
    latest_ok = ok_candidates[-1] if ok_candidates else None

    curr_lines = list(recent_logs) if recent_logs else _get_recent_logs_tail(30)
    curr_traits = _extract_log_traits(curr_lines)
    curr_err_session = next((s for s in reversed(sessions) if s.get("status") == "err"), None)
    if not curr_lines and curr_err_session:
        curr_traits = curr_err_session.get("traits") or {}

    parts = []
    if latest_ok:
        ok_t = latest_ok["traits"]
        ok_desc = []
        if ok_t.get("used_uia_silent"):
            ok_desc.append("走 UIA 控件树顺利点了「设为静音」")
        if ok_t.get("minimized_at_end"):
            ok_desc.append("发完后执行了「自动最小化钉钉」")
        if ok_t.get("woke_from_min"):
            ok_desc.append("开头经历过最小化唤醒")
        parts.append(
            f"• 最近一次成功记录（{latest_ok['when']}）：{('；'.join(ok_desc)) if ok_desc else '全流程顺利发完'}。"
        )
    else:
        parts.append("• 暂未检索到历史完整成功记录（本次为首次或较早日志已被滚动清理）。")

    fail_desc = []
    if curr_traits.get("woke_from_min"):
        fail_desc.append("从最小化/托盘刚唤醒钉钉")
    if curr_traits.get("search_retried"):
        fail_desc.append("打开搜索窗重试过")
    if curr_traits.get("paste_retried"):
        fail_desc.append("贴图触发过画面无变化补粘")
    if curr_traits.get("used_img_silent"):
        fail_desc.append("UIA 没点到菜单后退到了图像兜底")
    fail_tail_str = "；".join(fail_desc) if fail_desc else (ed.get("title") or "卡在当前投递/执行步")
    parts.append(f"• 当前/最近失败特征（{cur_code or '运行受阻'}）：{fail_tail_str}。")

    causal = []
    ok_min_end = bool(latest_ok and (latest_ok.get("traits") or {}).get("minimized_at_end"))
    if ok_min_end and (curr_traits.get("woke_from_min") or cur_code in ("ERR-DT-303", "ERR-DT-304", "ERR-DT-305")):
        causal.append(
            "因果链对比：第1次成功时钉钉处于可用态，但第1次结束触发了『发送后自动最小化钉钉』，"
            "导致第2次必须从最小化唤醒、聊天框和菜单层级还在重绘加载时就紧接着搜索/贴图，因而卡住！"
        )
    elif curr_traits.get("woke_from_min") and c.get("dt_minimize_after_send", True):
        causal.append(
            "因果链对比：当前开启了『发送后最小化钉钉』，每次从最小化拉起后易导致搜索焦点或控件树延迟。"
        )

    if latest_ok and (latest_ok.get("traits") or {}).get("used_uia_silent") and (curr_traits.get("used_img_silent") or cur_code == "ERR-DT-304"):
        causal.append(
            "路径差异：上次成功时 UIA 直接命中了静音菜单第 2 项；这次控件树层级变化或菜单受焦点干扰未弹出，"
            "退到图像匹配时又受 4K/深色模式影响没认出文字。"
        )
    if err_counts:
        top_errs = ", ".join(f"{k}({v}次)" for k, v in err_counts.most_common(4))
        causal.append(f"近期历史错误统计：{top_errs}。")

    if causal:
        parts.append("• " + " ".join(causal))
    return "\n".join(parts)


def recommend_pipeline_tuning_for_error(cfg, error_detail=None, hist_diff_text=""):
    """根据当前错误码与历史对比，自动给出软件内部可直接切换的模块顺序与等待参数建议。"""
    c = normalize_config(cfg if isinstance(cfg, dict) else {})
    pt = c.get("pipeline_tuning") or {}
    ed = error_detail if isinstance(error_detail, dict) else {}
    code = str(ed.get("code") or "")
    raw_low = f"{ed.get('raw_error') or ''} {hist_diff_text or ''}".lower()

    patch = {}
    reasons = []

    if code == "ERR-DT-304":
        if pt.get("compose_step_order") != "text_silent_image":
            patch["compose_step_order"] = "text_silent_image"
            reasons.append("改内部顺序：先把短文字贴进输入框激活底栏与「设为静音」，最后再贴大图防卡顿吞图")
        cur_delay = int(c.get("dt_paste_delay_sec", 3))
        if cur_delay < 5:
            patch["dt_paste_delay_sec"] = min(20, max(5, cur_delay + 2))
            reasons.append(f"粘贴前等待从 {cur_delay} 秒提高到 {patch['dt_paste_delay_sec']} 秒")
        if any(k in raw_low for k in ("静音", "菜单", "send_arrow", "uia", "图像兜底", "qui")) and pt.get("silent_step_order") != "arrow_img_then_uia":
            patch["silent_step_order"] = "arrow_img_then_uia"
            reasons.append("改静音方式：直接用图像点开右下角箭头，再走控件树命中「设为静音」第 2 项")
        if c.get("dt_minimize_after_send", True):
            patch["dt_minimize_after_send"] = False
            reasons.append("关闭「发送后最小化钉钉」，避免下轮冷唤醒重复卡住")
    elif code in ("ERR-DT-305", "ERR-DT-303"):
        if c.get("dt_minimize_after_send", True):
            patch["dt_minimize_after_send"] = False
            reasons.append("关闭「发送后最小化钉钉」，让钉钉保持就绪不再反复最小化还原")
        cur_delay = int(c.get("dt_paste_delay_sec", 3))
        if cur_delay < 4:
            patch["dt_paste_delay_sec"] = 4
            reasons.append("粘贴前等待提高到 4 秒，留足切换与搜索加载时间")
    elif code in ("ERR-WPS-203", "ERR-WPS-204"):
        cur_wps = int(pt.get("wps_refresh_wait_sec", 5))
        if cur_wps < 8:
            patch["wps_refresh_wait_sec"] = min(15, max(8, cur_wps + 3))
            reasons.append(f"汇总表刷新等待从 {cur_wps} 秒提高到 {patch['wps_refresh_wait_sec']} 秒")

    return {
        "has_tuning": bool(patch),
        "patch": patch,
        "summary": "；".join(reasons) if reasons else "当前配置已是稳妥顺序，暂无需改动",
        "reasons": reasons,
    }


def apply_recommended_tuning(cfg, patch):
    """将推荐的模块顺序或等待秒数补丁安全合并写入配置并返回规范化后的最新配置。"""
    import copy
    base = normalize_config(cfg if isinstance(cfg, dict) else {})
    p = copy.deepcopy(patch) if isinstance(patch, dict) else {}
    pt = dict(base.get("pipeline_tuning") or {})
    for k in ("compose_step_order", "silent_step_order", "paste_key_order", "wps_refresh_wait_sec"):
        if k in p:
            pt[k] = p[k]
    base["pipeline_tuning"] = pt
    if "dt_paste_delay_sec" in p:
        base["dt_paste_delay_sec"] = p["dt_paste_delay_sec"]
    if "dt_minimize_after_send" in p:
        base["dt_minimize_after_send"] = bool(p["dt_minimize_after_send"])
    return normalize_config(base)


def build_antigravity_prompt(cfg, error_detail=None, hist_ctx_text="", recent_logs=None, ai_code_advice=""):
    """自动整理可直接发给 Antigravity 改代码/调模块顺序的一体化精准提示词。"""
    c = normalize_config(cfg if isinstance(cfg, dict) else {})
    pt = c.get("pipeline_tuning") or {}
    ed = error_detail if isinstance(error_detail, dict) and error_detail.get("code") else classify_error_detail(RuntimeError("运行异常"))
    hist_txt = hist_ctx_text or build_historical_failure_diff_context(c, ed, recent_logs=recent_logs)
    rec = recommend_pipeline_tuning_for_error(c, ed, hist_txt)
    logs_excerpt = "\n".join(str(x) for x in (recent_logs or _get_recent_logs_tail(10))[-10:])
    loc_str = ed.get("location") or "请结合报错步自查"
    adv_line = f"\n- AI 补充结构建议：{ai_code_advice.strip()}" if str(ai_code_advice or "").strip() else ""

    prompt_lines = [
        f"我的「暗号表助手 v{APP_VERSION}」（目录：E:\\cord\\SheetMate）运行失败了，请你帮我对照代码和日志优化修复并重新打包：",
        "",
        "【1. 本次报错与位置】",
        f"- 错误代码：{ed.get('code')}（{ed.get('title')}）",
        f"- 报错原文：{ed.get('raw_error') or ed.get('plain_reason')}",
        f"- 报错位置：{loc_str}",
        f"- 当前投递顺序：{pt.get('compose_step_order')}｜静音顺序：{pt.get('silent_step_order')}｜粘贴前等待：{c.get('dt_paste_delay_sec', 3)}秒｜发后最小化：{'开' if c.get('dt_minimize_after_send', True) else '关'}",
        "",
        "【2. 历次成败对比（为什么以前能成、这次失败）】",
        hist_txt,
        "",
        "【3. 相关源码模块与建议调整方向】",
        "- 重点排查文件：E:\\cord\\SheetMate\\auto_report.py、E:\\cord\\SheetMate\\ios_ui.py",
        "- 关键流程顺序：_capture_summary_with_recovery -> send_dingtalk_image -> _send_one_target -> _restore_and_activate_hwnd -> 搜索会话 -> _do_paste_image_step / _do_paste_text_step / _do_toggle_silent_step -> _enable_dingtalk_silent_send -> _commit_dingtalk_send",
        f"- 软件自检调优建议：{rec.get('summary')}{adv_line}",
    ]
    if logs_excerpt:
        prompt_lines.extend(["", "【4. 最近关键运行日志】", logs_excerpt])
    prompt_lines.extend([
        "",
        "【5. 请 Antigravity 帮我做的事】",
        "1) 分析上面“为什么上一轮成功而这一轮失败”的根本原因，直接帮我优化或调整相关函数模块的顺序和等待机制；",
        "2) 改完后只运行无弹窗语法与静态检查，再用 --onedir 模式重新打包到 E:\\cord\\SheetMate\\dist（完整保留 config.json）；",
        "3) 全程用通俗大白话告诉我改了哪几个模块、为什么这样调。",
    ])
    return "\n".join(prompt_lines)


def _safe_ai_cfg_snapshot(cfg):
    """提取脱敏配置摘要供 AI 诊断与优化建议（隐藏密码与 API Key）。"""
    c = normalize_config(cfg if isinstance(cfg, dict) else {})
    sp = c.get("summary_presets") or {}
    return {
        "version": APP_VERSION,
        "day_mode": c.get("day_mode"),
        "day": c.get("day"),
        "night_enabled": c.get("night_enabled"),
        "night": c.get("night"),
        "enable_capture": c.get("enable_capture"),
        "enable_dingtalk": c.get("enable_dingtalk"),
        "dt_silent_send": c.get("dt_silent_send"),
        "dt_auto_send": c.get("dt_auto_send"),
        "dt_minimize_after_send": c.get("dt_minimize_after_send"),
        "dt_paste_delay_sec": c.get("dt_paste_delay_sec", 3),
        "dt_send_wait_sec": c.get("dt_send_wait_sec", 5),
        "pipeline_tuning": c.get("pipeline_tuning"),
        "dt_targets_count": len([t for t in (c.get("dt_targets") or []) if t.get("enabled", True)]),
        "dt_time_mode": c.get("dt_time_mode"),
        "dt_date_fmt": c.get("dt_date_fmt"),
        "dt_day_offset": c.get("dt_day_offset"),
        "dt_cutoff": {
            "enabled": c.get("dt_cutoff_enabled"),
            "hm": c.get("dt_cutoff_hm"),
            "before_offset": c.get("dt_before_cutoff_offset"),
        },
        "summary_presets_enabled": sp.get("enabled"),
        "summary_presets_cells": [
            {k: it.get(k) for k in ("cell", "label", "action", "day_offset", "cutoff_enabled", "cutoff_hm", "before_cutoff_offset", "enabled")}
            for it in (sp.get("items") or [])
        ],
        "update_time_cell": c.get("update_time_cell"),
        "ai_auto_heal": bool((c.get("ai_assistant") or {}).get("auto_heal", True)),
        "point_overrides_count": len(c.get("point_overrides") or {}),
    }


def _local_ai_fallback_reply(mode, err_detail, cfg_snap, question="", hist_ctx="", ag_prompt="", tuning_rec=None):
    """本地内置分析兜底：给出结合历史成败对比与排障思路的大白话诊断。"""
    if mode == "suggest":
        tips = []
        if not cfg_snap.get("enable_dingtalk"):
            tips.append("• 当前「自动发送到钉钉」开关是关闭的，正式自动值守前记得去【钉钉投递】打开它。")
        if cfg_snap.get("enable_dingtalk") and not cfg_snap.get("dt_auto_send"):
            tips.append("• 「粘贴后自动按 Enter 发送」目前关闭，脚本会在贴好截图后停下等您手动敲回车；若要无人值守全自动发送，记得打开它。")
        if cfg_snap.get("dt_minimize_after_send", True):
            tips.append("• 当前开启了「发送后最小化钉钉」，第1次发完收起后第2次唤醒最容易因为聊天框未加载完而卡住，建议关闭它。")
        d3 = next((x for x in (cfg_snap.get("summary_presets_cells") or []) if x.get("cell") in ("D3", "D2")), None)
        if d3 and not d3.get("cutoff_enabled"):
            tips.append("• 如果您有凌晨班次（比如早上 9 点前要发昨天数据），建议在 D3 的「自动日期」下打开【跨天凌晨分界点】，设定时间点前自动减 1 天。")
        if not tips:
            tips.append("• 当前时段排期、汇总表预设校准与静音设置搭配很合理！")
        return (
            "（当前处于本地内置分析模式）\n"
            "【配置体检建议】\n" + "\n".join(tips)
        )

    ed = err_detail if isinstance(err_detail, dict) and err_detail.get("code") else classify_error_detail(Exception(question or "运行检查"))
    hist_show = hist_ctx or "暂未检索到足够的历次成功/失败对比日志。"
    tune_line = (tuning_rec or {}).get("summary") or "您可以先按上方卡片步骤检查窗口焦点再重试。"
    ag_tip = "下方已自动生成好发给开发协作的完整参考说明，直接点【复制指令】即可。"
    return (
        f"（内置智能分析｜状态码 {ed.get('code')}）\n"
        f"### ① 这次到底为什么卡住了？\n{ed.get('plain_reason')}\n\n"
        f"### ② 为什么之前能成功，这次却失败了？（历史对比）\n{hist_show}\n\n"
        f"### ③ 软件自己怎么调执行顺序马上好？\n{tune_line}\n（可直接点上面「一键按 AI 建议自调顺序并重试」立即生效）\n\n"
        f"### ④ 怎么跟 Antigravity 沟通改源码？\n{ag_tip}\n{ed.get('self_fix')}"
    )


def build_ai_summary_styles(focus_team=""):
    """按用户当前指定的关注组名动态构建 AI 总结风格模板（留空则公平横向总结全部团队，不再硬编码单一组名）。"""
    ft = str(focus_team or "").strip()
    if ft:
        brief_desc = f"80字内3~4行：总数据 + 前三名 + {ft}情况，最适合随图发群"
        brief_prompt = f"请只输出 3~4 行极简战报，总字数控制在 80 字以内：1) 总数据与当前时间段盘面；2) 排名前三人员及核心数值；3) 【{ft}】整体数据及简短一句冲刺提示。不要多余客套话，适合直接附在钉钉截图下方。"
        team_name = f"{ft}专项点评"
        team_desc = f"重点对比【{ft}】与其他小组占总比及组内成员高低产出点评"
        team_prompt = f"请重点聚焦【{ft}】复盘点评：1) 【{ft}】的总暗号/注册数据、名次与占总盘比例，以及和前一名组的差距；2) 点名该组或盘面内高产出人员与待冲刺跟进人员；3) 给出 1 条接地气的提升建议。"
        boss_team = f"（重点含【{ft}】）"
    else:
        brief_desc = "80字内3~4行：总数据 + 个人前三 + 团队榜首情况，最适合随图发群"
        brief_prompt = "请只输出 3~4 行极简战报，总字数控制在 80 字以内：1) 总数据与当前时间段盘面；2) 个人排名前三及核心数值；3) 领跑小组与中后段组分布及简短冲刺提示。不要多余客套话，适合直接附在钉钉截图下方。"
        team_name = "各团队横向点评"
        team_desc = "公平对比所有参与计算小组的总产出、名次差距及各组亮点"
        team_prompt = "请对【所有参与计算的团队】进行横向复盘点评：1) 列出各组总暗号/注册数据、排名次序及占总盘比例；2) 点出表现最亮眼的小组与待追赶小组的关键差距；3) 给出 1~2 条可落地的整体提升建议。"
        boss_team = "与各团队"
    return {
        "brief": {
            "name": "群极简战报",
            "desc": brief_desc,
            "prompt": brief_prompt,
        },
        "mujie": {
            "name": team_name,
            "desc": team_desc,
            "prompt": team_prompt,
        },
        "funnel": {
            "name": "转化率排查版",
            "desc": "看搜索量/暗链/注册比例，揪出高曝光低转化项与素材异常",
            "prompt": "请做运营转化率与异常排查总结：重点对比「搜索人数 -> 暗链/暗号 -> 注册数据」的转化比率，挑出搜索较高但注册偏低的 2~3 位成员或素材类别，给出一条可落地的调整建议。",
        },
        "delta": {
            "name": "较上轮增量对比",
            "desc": "对比当前最新图与上一张历史图，算出这两小时谁涨得最快",
            "prompt": "请对比最新这张汇总表截图与前面一张历史截图的差异：重点写出 1) 总盘数据增加了多少；2) 这段时间内涨幅最快的前三名人员与增量；3) 近一轮几乎无新增需要提醒的小伙伴。",
        },
        "boss": {
            "name": "领导一分钟汇报",
            "desc": "规范三段式：整体盘面 / 核心亮点与各组表现 / 下一步重点",
            "prompt": f"请按规范简洁的三段式输出管理汇报：【一、整体盘面】概括总数据与注册/短链整体表现；【二、核心亮点与团队】列明前三名{boss_team}表现；【三、下一步动作】写出 1~2 条具体跟进安排。",
        },
    }


def get_candidate_teams(cfg=None):
    """综合当前表格配置小组名单与历次存档榜单，提取全部不重复组名供筛选与骰子随机抽取。"""
    c = normalize_config(cfg if isinstance(cfg, dict) else {})
    teams = []

    def _add(raw):
        for part in re.split(r"[,，、;；\n]+", str(raw or "")):
            s = part.strip()
            if s and s not in teams and ("组" in s or len(s) >= 2):
                teams.append(s)

    for it in ((c.get("summary_presets") or {}).get("items") or []):
        if isinstance(it, dict) and str(it.get("cell") or "").upper() in ("L3", "L4"):
            _add(it.get("value"))
    try:
        for rec in load_report_archive(limit=10):
            for t in (rec.get("teams") or []):
                _add(t.get("team"))
    except Exception:
        pass
    for k in _KNOWN_TEAM_NAMES:
        _add(k)
    return teams


AI_SUMMARY_STYLES = build_ai_summary_styles("")


def _validate_safe_http_url(raw_url, require_https=False, allow_loopback=False):
    """校验外部请求或更新 URL 必须为安全 HTTP(S) 地址，阻断 file://、控制字符、本机回环及云元数据内网地址。"""
    u = str(raw_url or "").strip()
    if not u or any(ch in u for ch in ("\r", "\n", "\x00")):
        return None
    try:
        p = urllib.parse.urlparse(u)
    except Exception:
        return None
    allowed_schemes = ("https",) if require_https else ("http", "https")
    if p.scheme.lower() not in allowed_schemes or not p.netloc:
        return None
    host = (p.hostname or "").strip().lower().rstrip(".")
    if not host:
        return None
    if not allow_loopback and (host == "localhost" or host.endswith(".localhost")):
        return None
    if host == "169.254.169.254" or host.startswith("169.254."):
        return None
    try:
        ip_obj = ipaddress.ip_address(host)
        if (
            (not allow_loopback and ip_obj.is_loopback)
            or ip_obj.is_link_local
            or ip_obj.is_multicast
            or ip_obj.is_reserved
            or ip_obj.is_unspecified
        ):
            return None
        if require_https and ip_obj.is_private:
            return None
    except ValueError:
        if require_https and host.endswith(".local"):
            return None
    return u


class _SafeHttpsRedirectHandler(urllib.request.HTTPRedirectHandler):
    """校验 HTTP 重定向目标 URL，阻止通过 302 跳转访问内网、回环或非 HTTPS 资源。"""

    def __init__(self, require_https=True):
        super().__init__()
        self.require_https = bool(require_https)

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        abs_url = urllib.parse.urljoin(req.full_url, str(newurl or ""))
        if not _validate_safe_http_url(abs_url, require_https=self.require_https):
            raise urllib.error.HTTPError(abs_url, code, "Unsafe redirect blocked", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _is_official_gemini_base(api_base):
    base = str(api_base or "").strip()
    if not base:
        return True
    try:
        host = (urllib.parse.urlsplit(base).hostname or "").strip().lower()
        return host == "generativelanguage.googleapis.com" or host.endswith(".generativelanguage.googleapis.com")
    except Exception:
        return False


def _open_ai_http(req, timeout_sec, ssl_ctx, require_https=True):
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl_ctx),
        _SafeHttpsRedirectHandler(require_https=require_https),
    )
    return opener.open(req, timeout=timeout_sec)


def _call_openai_compat_generate(api_base, api_key, model_name, sys_prompt, contents, temperature=0.4, max_tokens=900, timeout_sec=24):
    """OpenAI 兼容格式通用调度器（支持多模态图片与纯文本，对外彻底隐藏底层供应商信息）。"""
    base = str(api_base or "").strip().rstrip("/")
    if not base:
        return {"ok": False, "source": "no_base", "model": "", "error": "未填兼容接口地址"}
    url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
    if not _validate_safe_http_url(url, require_https=False):
        return {"ok": False, "source": "invalid_url", "model": "", "error": "接口地址协议不安全或格式无效"}
    key = str(api_key or "").strip()
    if not key:
        return {"ok": False, "source": "no_key", "model": "", "error": "接口密钥未配置"}
    mdl = str(model_name or "gpt-4o-mini").strip()

    msgs = [{"role": "system", "content": sys_prompt}]
    for item in (contents or []):
        role = "assistant" if item.get("role") == "model" else "user"
        parts = item.get("parts") or []
        has_img = any(isinstance(p, dict) and p.get("inlineData") for p in parts)
        if has_img:
            o_parts = []
            for p in parts:
                if not isinstance(p, dict):
                    continue
                if p.get("text"):
                    o_parts.append({"type": "text", "text": str(p["text"])})
                elif isinstance(p.get("inlineData"), dict):
                    mime = p["inlineData"].get("mimeType") or "image/png"
                    b64 = p["inlineData"].get("data") or ""
                    if b64:
                        o_parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
            msgs.append({"role": role, "content": o_parts})
        else:
            txt = "\n".join(str(p.get("text") or "") for p in parts if isinstance(p, dict) and p.get("text")).strip()
            if txt:
                msgs.append({"role": role, "content": txt})

    payload = {
        "model": mdl,
        "messages": msgs,
        "temperature": float(temperature),
        "max_tokens": int(max_tokens),
    }
    body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ssl_ctx = ssl.create_default_context()
    req = urllib.request.Request(
        url,
        data=body_bytes,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "User-Agent": f"SheetMate/{APP_VERSION}",
        },
    )
    req_https = url.lower().startswith("https://")
    try:
        with _open_ai_http(req, timeout_sec, ssl_ctx, require_https=req_https) as resp:
            raw = json.loads(resp.read().decode("utf-8", errors="ignore"))
        choices = raw.get("choices") or []
        if not choices:
            return {"ok": False, "source": "network_error", "model": "", "error": "云端返回结果为空"}
        content = ((choices[0].get("message") or {}).get("content")) or ""
        if isinstance(content, list):
            content = "".join(str(x.get("text") or "") for x in content if isinstance(x, dict))
        answer = str(content).strip()
        if answer:
            return {"ok": True, "source": "ai_cloud", "model": "", "reply": answer}
        return {"ok": False, "source": "network_error", "model": "", "error": "云端返回内容为空"}
    except urllib.error.HTTPError as he:
        try:
            err_body = he.read().decode("utf-8", errors="ignore")[:180]
            code = he.code
        finally:
            try:
                he.close()
            except Exception:
                pass
        return {"ok": False, "source": "network_error", "model": "", "error": f"HTTP {code}: {err_body}"}
    except Exception as exc:
        return {"ok": False, "source": "network_error", "model": "", "error": str(exc)[:180]}


def _sanitize_ai_reply_brands(text):
    """兜底清洗云端模型回复中偶发复述的外部厂商/大模型名称。"""
    s = str(text or "")
    for pat, rep in (
        (r"https?://generativelanguage\.googleapis\.com\S*", "内置专属服务通道"),
        (r"\bGoogle\s*Gemini(?:\s*[A-Za-z0-9.\-]+)?\b", "外部通用大模型"),
        (r"\bGoogle\b", "外部服务商"),
        (r"\bGemini(?:\s*[A-Za-z0-9.\-]+)?\b", "外部通用模型"),
        (r"\bChatGPT\b|\bGPT-[0-9A-Za-z.\-]+\b|\bGPT\b|\bOpenAI\b", "外部大模型"),
        (r"\bDeepSeek\b|\bClaude\b", "外部模型"),
        (r"谷歌", "外部"),
    ):
        s = re.sub(pat, rep, s, flags=re.IGNORECASE)
    return s


def _call_gemini_generate(cfg, sys_prompt, contents, temperature=0.4, max_tokens=900, timeout_sec=22, max_models=None, total_budget_sec=40):
    """底层统一云端智能接口调度器（支持管理员切换不同服务接口，对外严格隐藏厂商与模型信息）。"""
    norm = normalize_config(cfg if isinstance(cfg, dict) else {})
    ai_cfg = norm.get("ai_assistant") or {}
    if not ai_cfg.get("enabled", True):
        return {"ok": False, "source": "disabled", "model": "", "error": "智能助手开关当前已关闭"}

    provider = str(ai_cfg.get("provider") or "gemini").strip().lower()
    if provider == "local_only":
        return {"ok": False, "source": "local_only", "model": "", "error": "当前设置为仅使用内置本地分析引擎"}

    api_base = str(ai_cfg.get("api_base") or "").strip().rstrip("/")
    raw_key = str(ai_cfg.get("api_key") or "").strip()
    if raw_key:
        api_key = raw_key
    elif provider == "gemini" and _is_official_gemini_base(api_base):
        api_key = str(DEFAULT_GEMINI_API_KEY or "").strip()
    else:
        api_key = ""
    pref_m = str(ai_cfg.get("model") or GEMINI_MODEL_CANDIDATES[0]).strip()

    if provider == "openai_compat":
        res_oc = _call_openai_compat_generate(
            api_base=api_base or "https://api.openai.com/v1",
            api_key=api_key,
            model_name=pref_m,
            sys_prompt=sys_prompt,
            contents=contents,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout_sec=timeout_sec,
        )
        if res_oc.get("ok") and res_oc.get("reply"):
            res_oc["reply"] = _sanitize_ai_reply_brands(res_oc["reply"])
        return res_oc

    if not api_key:
        return {"ok": False, "source": "no_key", "model": "", "error": "云端接口暂未配置"}

    models_to_try = []
    for m in (pref_m, *GEMINI_MODEL_CANDIDATES):
        clean_m = re.sub(r"[^A-Za-z0-9._\-]", "", m.replace("models/", "").strip())
        if clean_m and clean_m not in models_to_try:
            models_to_try.append(clean_m)
    if max_models is not None and int(max_models) > 0:
        models_to_try = models_to_try[:int(max_models)]

    payload = {
        "systemInstruction": {"parts": [{"text": sys_prompt}]},
        "contents": contents,
        "generationConfig": {"temperature": float(temperature), "maxOutputTokens": int(max_tokens)},
    }
    body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ssl_ctx = ssl.create_default_context()
    last_err = ""
    base_host = api_base or "https://generativelanguage.googleapis.com"
    if not _validate_safe_http_url(base_host, require_https=False):
        return {"ok": False, "source": "invalid_url", "model": "", "error": "接口地址协议不安全或格式无效"}

    t_start = time.monotonic()
    for model_name in models_to_try:
        rem = float(total_budget_sec) - (time.monotonic() - t_start)
        if rem < 4.0:
            last_err = last_err or "请求总耗时已接近上限，已快速切换离线兜底"
            break
        per_timeout = max(5.0, min(float(timeout_sec), rem))
        url = f"{base_host}/v1beta/models/{model_name}:generateContent?key={api_key}"
        req = urllib.request.Request(
            url,
            data=body_bytes,
            headers={"Content-Type": "application/json", "User-Agent": f"SheetMate/{APP_VERSION}"},
        )
        try:
            req_https = base_host.lower().startswith("https://")
            with _open_ai_http(req, per_timeout, ssl_ctx, require_https=req_https) as resp:
                raw = json.loads(resp.read().decode("utf-8", errors="ignore"))
            cands = raw.get("candidates") or []
            if not cands:
                last_err = "云端返回候选结果为空"
                continue
            parts = ((cands[0].get("content") or {}).get("parts")) or []
            texts = [str(p.get("text") or "") for p in parts if isinstance(p, dict) and not p.get("thought") and p.get("text")]
            answer = "".join(texts).strip()
            if answer:
                return {"ok": True, "source": "ai_cloud", "model": "", "reply": _sanitize_ai_reply_brands(answer)}
            last_err = "云端返回文本为空"
        except urllib.error.HTTPError as he:
            try:
                err_body = he.read().decode("utf-8", errors="ignore")[:180]
                h_code = he.code
            finally:
                try:
                    he.close()
                except Exception:
                    pass
            last_err = f"HTTP {h_code}: {err_body}"
            if h_code in (400, 401, 403):
                break
        except Exception as e:
            last_err = str(e)[:180]
            low_e = last_err.lower()
            if any(k in low_e for k in ("getaddrinfo", "10061", "nameresolutionerror", "actively refused", "winerror 10051")):
                break

    return {"ok": False, "source": "network_error", "model": "", "error": last_err}


def _encode_image_b64_scaled(img_path, max_dim=3072):
    """读取图片等比高清缩放后转为 PNG base64，保障超宽数据汇总表文字数字清晰可辨。"""
    import base64
    im = None
    im_resized = None
    buf = None
    try:
        with Image.open(img_path) as raw_im:
            im = raw_im.convert("RGB")
        target_im = im
        if max(im.width, im.height) > max_dim:
            ratio = max_dim / float(max(im.width, im.height))
            im_resized = im.resize((max(1, int(im.width * ratio)), max(1, int(im.height * ratio))), Image.Resampling.LANCZOS)
            target_im = im_resized
        buf = io.BytesIO()
        target_im.save(buf, format="PNG", optimize=True)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    finally:
        for obj in (buf, im_resized, im):
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass


def _encode_image_for_ai(img_path, max_dim=2200):
    """将汇总表截图智能缩放并择优编码为更轻量的 JPEG/PNG base64，显著缩短 AI 总结网络上传耗时。"""
    import base64
    im = None
    im_resized = None
    buf_jpg = None
    buf_png = None
    try:
        with Image.open(img_path) as raw_im:
            im = raw_im.convert("RGB")
        target_im = im
        if max(im.width, im.height) > max_dim:
            ratio = max_dim / float(max(im.width, im.height))
            im_resized = im.resize((max(1, int(im.width * ratio)), max(1, int(im.height * ratio))), Image.Resampling.LANCZOS)
            target_im = im_resized
        buf_jpg = io.BytesIO()
        target_im.save(buf_jpg, format="JPEG", quality=88, optimize=True)
        jpg_bytes = buf_jpg.getvalue()

        buf_png = io.BytesIO()
        target_im.save(buf_png, format="PNG", optimize=True)
        png_bytes = buf_png.getvalue()

        if len(jpg_bytes) <= len(png_bytes):
            return "image/jpeg", base64.b64encode(jpg_bytes).decode("ascii")
        return "image/png", base64.b64encode(png_bytes).decode("ascii")
    finally:
        for obj in (buf_png, buf_jpg, im_resized, im):
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass


_SHEETMATE_AI_KNOWLEDGE = """【软件身份与核心功能说明书】
- 身份：你是「暗号表助手 (SheetMate)」专属内置的萌宠小助手与运营数据分析伙伴。
- 设计研发者：仅当用户明确主动提问“谁设计的 / 谁开发的 / 作者是谁”时，才回答由【Soren Drake】设计研发；平时日常打招呼、自我介绍或回答其他问题时不要主动提及设计师名字。
- 绝对保密边界：
  1) 严禁向外透露或自称任何外部 AI 厂商、大模型名称或 API 接口协议（如不要说自己是哪家公司的大模型，统一自称「暗号表助手专属内置智能伙伴」）；
  2) 没有任何读取、查看或访问管理员密码/口令、权限控制、隐藏管理界面、他人隐私聊天记录或接口密钥的能力；遇到相关提问必须礼貌拒绝并说明“没有读取与访问权限”。
- 内置正式执行控制权（重要）：作为软件内部正式集成的管家，我具备通过系统指令路由与 `[[SM_ACTION:...]]` 动作协议直接拉起「全流程跑表（run_full）」「仅刷新截图发钉钉（capture_send）」「文字直发钉钉（text_send）」「开启/停止自动值守（start_schedule / stop_schedule）」「运行中暂停（pause_task）」「恢复继续（resume_task）」「取消当前任务（cancel_task）」及修改排期配置的正式权限；**严禁谎称“没有底层/后台执行权限”或自称只是纯聊天助手**。若提及界面按钮，请使用真实名称：顶部主按钮「立即运行一次」「仅截图并发送」与状态栏「自动值守」卡片，严禁杜撰不存在的按钮位置。
- 我能做什么：
  1) 全自动抓数填表发群：自动登录 Luya 数据后台下载「暗号数据」「注册数据」「短链数据」，转为 TSV 后写入 WPS 在线表格；
  2) 自定义任意单元格写入：在「钉钉投递 -> 格位校准」中点击快捷格位胶囊或「+ 新增位置」，可为任意工作表（如数据汇总/团队成员/看板）的任意格位（如 D3/D4:E4/D5/L3/Y3/Y4/Y5/AA2/AA5/R4）自由定义：自动日期（支持跨天凌晨自动减天）、当前时间、自定义文字、Excel 公式或清空；
  3) 自然语言直接改排期：用户可直接说“帮我设置每周一到周五 9点到18点每2小时把昨天数据发送到 XX群”，我会自动修改软件设置并列出核对清单让用户确认对不对；
  4) 钉钉稳妥投递：自动探测当前电脑分辨率、DPI 缩放与深色模式适配方案，支持多群顺序投递、防卡顿直通搜索、无坐标静音发送、富文本发送按钮补发，并在粘贴操作期间全屏提示勿动键鼠；
  5) AI 看图自愈与档案记忆：遇错自动弹窗给出 ERR 错误码大白话解决方案，支持「遇错 AI 自动看图自愈」先截屏调整现场并续跑，还能自动对比历次成功/失败原因、调整内部模块顺序并生成改代码指令；同时每轮将表格数字与排行归档到本地 `report_archive.jsonl`，公平对比各团队与个人数据。
  6) 云端异地远程网址（https://sorenskr.github.io/SheetMate/）：无论身在何处，用手机或外地电脑打开这个网址，填上电脑上起好的名字和设置好的远程密码，就能直接通过云端跟电脑对话查数据、让电脑跑表发钉钉，两边的聊天记录会实时同步。
- 常用快捷键：运行中按 【F8】 随时暂停或继续未完成步骤；按 【F9】 在已手动打开的目标群聊窗口直接贴图继续。""" + "\n" + _SHEETMATE_CODE_ARCHITECTURE

_KNOWN_TEAM_NAMES = ("慕杰组", "潘立浩组", "宋磊组", "刘全泉组", "李俊鑫组", "江峰组", "娄慧珍组")
_last_captured_summary_tsv = ""
_last_archive_sig = ""


def _archive_file_path():
    return app_dir() / "report_archive.jsonl"


_NON_PERSON_STAT_NAMES = {
    "番号", "走光", "任务", "短链", "聊天记录", "新模版", "新模板", "备用", "备用2",
    "总计", "合计", "小计", "单日所有数据", "素材分类", "序号", "名次", "姓名", "团队",
    "全部在职组", "未知", "其他", "其它", "汇总", "直达", "占比", "数据", "明细",
    "群", "大群", "空", "默认",
}

_NON_PERSON_SUFFIXES = (
    "组", "群", "部", "团队", "模板", "模版", "汇总", "合计", "总计", "占比", "数据", "明细", "分类", "排行", "助手", "记录",
)


def _to_float_safe(raw):
    try:
        s = str(raw or "").strip().replace(",", "").replace("，", "")
        if not s:
            return None
        if s.endswith("%"):
            s = s[:-1].strip()
        if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", s):
            return None
        val = float(s)
        if abs(val) > 1e7:
            return None
        return val
    except Exception:
        return None


def _is_valid_person_name_for_stats(name):
    s = str(name or "").strip()
    if not s:
        return False
    if s in _NON_PERSON_STAT_NAMES or s in _KNOWN_TEAM_NAMES:
        return False
    if s.endswith(_NON_PERSON_SUFFIXES):
        return False
    return bool(re.fullmatch(r"[\u4e00-\u9fff]{2,4}", s))


def _extract_material_label(text):
    s = str(text or "").strip()
    if not s:
        return ""
    best_mat = ""
    best_cnt = -1.0
    for mat in ("番号", "走光", "任务", "短链"):
        m_cnt = re.search(rf"{mat}\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(?:/\s*(\d+(?:\.\d+)?))?", s)
        if m_cnt:
            v = float(m_cnt.group(1))
            den = float(m_cnt.group(2)) if m_cnt.group(2) else 1.0
            eff = v / max(1.0, den)
            if eff > best_cnt:
                best_cnt = eff
                best_mat = mat
        elif mat in s and best_cnt < 0:
            best_mat = mat
            best_cnt = 0.0
    return best_mat


def _sanitize_personal_entry(item):
    if not isinstance(item, dict):
        return None
    nm = str(item.get("name") or "").strip()
    if not _is_valid_person_name_for_stats(nm):
        return None
    clean = dict(item)
    clean["name"] = nm
    for k in ("cipher_link_total", "reg_total", "cn_reg", "cn_reg_sum", "search_users", "cipher_link_rank_val"):
        if k in clean and clean[k] is not None:
            try:
                fv = float(clean[k])
                if fv < 0 or fv > 100000:
                    del clean[k]
                else:
                    clean[k] = round(fv, 2)
            except Exception:
                del clean[k]
    return clean


def _filter_valid_personal_entries(items):
    out = []
    seen = set()
    for it in (items or []):
        c = _sanitize_personal_entry(it)
        if c is not None and c["name"] not in seen:
            seen.add(c["name"])
            out.append(c)
    return out


def parse_summary_tsv_snapshot(summary_tsv="", report_dir=None):
    """从 WPS 数据汇总表选区 TSV 与桌面三份明细表中提取结构化个人/团队/素材指标。"""
    rep_dir = Path(report_dir) if report_dir is not None else (Path.home() / "Desktop" / "数据日报")
    personal_map = {}
    teams_map = {}
    summary_totals = []

    def _clean_cell(c):
        return str(c or "").replace("\r", "").replace("\n", " ").strip()

    for raw_ln in str(summary_tsv or "").splitlines():
        cols = [_clean_cell(c) for c in raw_ln.split("\t")]
        if not any(cols):
            continue

        # 1) 提取左侧「数据明细」个人行：名次(数字) | 姓名 | 明细 | 素材拆分 | 暗号+短链 | 注册 | 占总比
        if len(cols) >= 6 and re.fullmatch(r"\d{1,3}", cols[0]) and _is_valid_person_name_for_stats(cols[1]):
            nm = cols[1]
            entry = personal_map.setdefault(nm, {"name": nm})
            entry["rank_detail"] = int(cols[0])
            if cols[2]:
                entry["detail"] = cols[2][:90]
            mat = _extract_material_label((cols[3] if len(cols) > 3 and cols[3] else "") or cols[2])
            if mat:
                entry["material"] = mat
            if len(cols) > 4 and cols[4]:
                v = _to_float_safe(cols[4])
                if v is not None and 0 <= v <= 100000:
                    entry["cipher_link_total"] = round(v, 2)
            if len(cols) > 5 and cols[5]:
                v = _to_float_safe(cols[5])
                if v is not None and 0 <= v <= 100000:
                    entry["reg_total"] = round(v, 2)
            if len(cols) > 6 and "%" in cols[6]:
                entry["share"] = cols[6]

        # 2) 提取右侧「团队全部暗号数据情况」以及中区「注册数据排行」（严格限定列与特征，绝不把素材/团队当人）
        for ci, cell in enumerate(cols):
            if not cell:
                continue
            # 识别团队行：独立团队名，且不带逗号名单，后面紧跟数字指标
            if (
                "," not in cell
                and "、" not in cell
                and (cell in _KNOWN_TEAM_NAMES or re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9]{1,8}组", cell))
            ):
                tail = [c for c in cols[ci + 1:ci + 7] if c]
                nums = [_to_float_safe(t) for t in tail[:3]]
                if any(v is not None and 0 <= v <= 100000 for v in nums[:2]):
                    t_item = teams_map.setdefault(cell, {"team": cell})
                    if ci >= 1 and re.fullmatch(r"\d{1,2}", cols[ci - 1]):
                        t_item["rank"] = int(cols[ci - 1])
                    if nums[0] is not None and 0 <= nums[0] <= 100000:
                        t_item["cipher_score"] = round(nums[0], 2)
                    if len(nums) > 1 and nums[1] is not None and 0 <= nums[1] <= 100000:
                        t_item["reg_total"] = round(nums[1], 2)
                    if len(tail) > 2:
                        t_item["detail"] = " | ".join(tail[2:5])[:160]

            # 识别中区个人注册排行：严格限定 6<=ci<=11，且第 ci+2 列必须为暗号/短链（含冒号或laya-）
            if (
                6 <= ci <= 11
                and re.fullmatch(r"\d{1,3}", cell)
                and ci + 2 < len(cols)
                and _is_valid_person_name_for_stats(cols[ci + 1])
                and any(sep in cols[ci + 2] for sep in (":", "：", "laya-"))
            ):
                nm = cols[ci + 1]
                entry = personal_map.setdefault(nm, {"name": nm})
                rk_now = int(cell)
                if "rank_reg" not in entry or rk_now < int(entry["rank_reg"]):
                    entry["rank_reg"] = rk_now
                sub = cols[ci + 2:ci + 7]
                if len(sub) >= 1 and sub[0] and not entry.get("top_code"):
                    entry["top_code"] = sub[0][:40]
                if not entry.get("material") and sub[0]:
                    mat2 = _extract_material_label(sub[0])
                    if mat2:
                        entry["material"] = mat2
                if len(sub) >= 2:
                    sv = _to_float_safe(sub[1])
                    if sv is not None and 0 <= sv <= 100000:
                        entry["search_users"] = round(float(entry.get("search_users") or 0.0) + sv, 2)
                if len(sub) >= 3:
                    cv = _to_float_safe(sub[2])
                    if cv is not None and 0 <= cv <= 100000:
                        entry["cipher_link_rank_val"] = round(float(entry.get("cipher_link_rank_val") or 0.0) + cv, 2)
                if len(sub) >= 4:
                    rv = _to_float_safe(sub[3])
                    if rv is not None and 0 <= rv <= 100000:
                        entry["cn_reg_sum"] = round(float(entry.get("cn_reg_sum") or 0.0) + rv, 2)
                        if "cn_reg" not in entry:
                            entry["cn_reg"] = round(rv, 2)

        if any(k in raw_ln for k in ("总计", "单日所有数据", "各团队不同素材折算")):
            compact_row = " | ".join(c for c in cols if c)[:160]
            if compact_row and compact_row not in summary_totals:
                summary_totals.append(compact_row)

    # 3) 从右区团队明细里自动为个人绑定所属团队（如 李俊鑫组）
    for tm_name, t_obj in teams_map.items():
        d_txt = str(t_obj.get("detail") or "")
        if not d_txt:
            continue
        for nm, p_obj in personal_map.items():
            if not p_obj.get("team") and re.search(rf"(?<![\u4e00-\u9fff]){re.escape(nm)}(?![\u4e00-\u9fff])", d_txt):
                p_obj["team"] = tm_name

    personal_list = sorted(
        _filter_valid_personal_entries(personal_map.values()),
        key=lambda x: (
            int(x.get("rank_detail", 9999)),
            int(x.get("rank_reg", 9999)),
            -(float(x.get("reg_total") or x.get("cn_reg_sum") or x.get("cn_reg") or 0)),
        ),
    )
    teams_list = sorted(
        teams_map.values(),
        key=lambda x: (int(x.get("rank", 999)), -(float(x.get("cipher_score") or 0))),
    )

    daily_reg = []
    top_keywords = []
    top_channels = []
    if rep_dir.exists():
        reg_p = rep_dir / "注册数据.tsv"
        if reg_p.exists():
            try:
                lines = _read_head_nonempty_lines(reg_p, max_lines=16)
                for ln in lines[1:9]:
                    ps = ln.split("\t")
                    if len(ps) >= 4:
                        daily_reg.append({
                            "date": ps[0][:10].strip(),
                            "total": _to_float_safe(ps[1]),
                            "cn": _to_float_safe(ps[2]),
                            "ratio": _to_float_safe(ps[3]),
                        })
            except Exception:
                pass
        kw_p = rep_dir / "暗号数据.tsv"
        if kw_p.exists():
            try:
                lines = _read_head_nonempty_lines(kw_p, max_lines=200)
                latest_d = lines[1].split("\t")[0].strip() if len(lines) > 1 else ""
                for ln in lines[1:]:
                    ps = ln.split("\t")
                    if len(ps) >= 4 and (not latest_d or ps[0].strip() == latest_d):
                        top_keywords.append({
                            "keyword": ps[1].strip(),
                            "search": _to_float_safe(ps[2]),
                            "cn_search": _to_float_safe(ps[3]),
                        })
                        if len(top_keywords) >= 15:
                            break
            except Exception:
                pass
        ch_p = rep_dir / "短链数据.tsv"
        if ch_p.exists():
            try:
                lines = _read_head_nonempty_lines(ch_p, max_lines=200)
                latest_d = lines[1].split("\t")[0].strip() if len(lines) > 1 else ""
                for ln in lines[1:]:
                    ps = ln.split("\t")
                    if len(ps) >= 4 and (not latest_d or ps[0].strip() == latest_d):
                        top_channels.append({
                            "channel": ps[1].strip() or "(直达/空渠道)",
                            "total": _to_float_safe(ps[2]),
                            "cn": _to_float_safe(ps[3]),
                        })
                        if len(top_channels) >= 15:
                            break
            except Exception:
                pass

    return {
        "personal": personal_list[:60],
        "teams": teams_list[:15],
        "summary_totals": summary_totals[:6],
        "daily_reg": daily_reg,
        "top_keywords": top_keywords,
        "top_channels": top_channels,
    }


def build_round_change_spotlight(prev_rec, curr_rec, focus_team=""):
    """每次数据粘贴/截图归档完成后，对比上一轮档案并生成有温度的数据变化播报。"""
    curr = curr_rec if isinstance(curr_rec, dict) else {}
    prev = prev_rec if isinstance(prev_rec, dict) else None
    lines = []

    def _fmt_num(v):
        if v is None:
            return "—"
        fv = float(v)
        if abs(fv - round(fv)) < 1e-6:
            return str(int(round(fv)))
        s = f"{fv:.2f}".rstrip("0").rstrip(".")
        return s

    def _pick_person_reg(p_obj):
        for k in ("reg_total", "cn_reg_sum", "cn_reg"):
            v = p_obj.get(k)
            if v is not None:
                try:
                    fv = float(v)
                    if 0 <= fv <= 100000:
                        return fv
                except Exception:
                    pass
        return None

    def _sum_personal_cipher(rec):
        total_c = 0.0
        has_any = False
        for it in _filter_valid_personal_entries(rec.get("personal") or []):
            v = it.get("cipher_link_total")
            if v is not None:
                total_c += float(v)
                has_any = True
        return round(total_c, 2) if has_any else None

    curr_personal = _filter_valid_personal_entries(curr.get("personal") or [])
    c_regs = curr.get("daily_reg") or []
    c_reg0 = c_regs[0] if c_regs else {}
    c_tot = c_reg0.get("total")
    c_cn = c_reg0.get("cn")
    c_ciph = _sum_personal_cipher(curr)

    if prev is None:
        if c_tot is not None or c_cn is not None:
            lines.append(f"• 当前总注册：**{_fmt_num(c_tot)}**，华人注册：**{_fmt_num(c_cn)}**" + (f"（明细合计暗号+短链 **{_fmt_num(c_ciph)}**）" if c_ciph is not None else ""))
        p_top = curr_personal[:3]
        if p_top:
            lines.append("• 本轮前列成员：" + "、".join(f"{x.get('name')}(注册 {_fmt_num(_pick_person_reg(x))})" for x in p_top))
        txt = "本轮表格数据已更新就绪！这是首份基准记录，后面每轮对比都会告诉您具体涨跌：\n" + ("\n".join(lines) if lines else "已记录最新盘面快照，随时可以让我帮您分析~")
        return {
            "time": curr.get("time_str") or time.strftime("%H:%M:%S"),
            "round": curr.get("round") or "最新",
            "has_delta": False,
            "summary_line": f"数据已更新：{lines[0]}" if lines else "已记录首轮基准数据",
            "reply": txt,
        }

    p_regs = prev.get("daily_reg") or []
    p_reg0 = p_regs[0] if p_regs else {}
    p_tot = p_reg0.get("total")
    p_cn = p_reg0.get("cn")
    p_ciph = _sum_personal_cipher(prev)

    any_change = False
    if c_tot is not None and p_tot is not None:
        d = c_tot - p_tot
        if abs(d) >= 1e-6:
            any_change = True
            sign = f"+{_fmt_num(d)}" if d > 0 else _fmt_num(d)
            lines.append(f"• **总注册**：从 **{_fmt_num(p_tot)}** 变成了 **{_fmt_num(c_tot)}**（{sign}）")
        else:
            lines.append(f"• **总注册**：维持 **{_fmt_num(c_tot)}**（较上轮持平）")

    if c_cn is not None and p_cn is not None:
        d_cn = c_cn - p_cn
        if abs(d_cn) >= 1e-6:
            any_change = True
            sign = f"+{_fmt_num(d_cn)}" if d_cn > 0 else _fmt_num(d_cn)
            lines.append(f"• **华人注册**：从 **{_fmt_num(p_cn)}** 变成了 **{_fmt_num(c_cn)}**（{sign}）")

    if c_ciph is not None and p_ciph is not None:
        d_c = c_ciph - p_ciph
        if abs(d_c) >= 1e-6:
            any_change = True
            sign = f"+{_fmt_num(d_c)}" if d_c > 0 else _fmt_num(d_c)
            lines.append(f"• **全盘暗号+短链合计**：从 **{_fmt_num(p_ciph)}** 变成了 **{_fmt_num(c_ciph)}**（{sign}）")

    # 个人对比变化（仅统计真实成员姓名，带上素材分类 + 所属团队）
    prev_p_map = {x["name"]: x for x in _filter_valid_personal_entries(prev.get("personal") or [])}
    p_deltas = []
    for cur_p in curr_personal:
        nm = cur_p["name"]
        if nm not in prev_p_map:
            continue
        old_p = prev_p_map[nm]
        nr = _pick_person_reg(cur_p)
        orr = _pick_person_reg(old_p)
        nc = cur_p.get("cipher_link_total")
        oc = old_p.get("cipher_link_total")
        dr = (float(nr) - float(orr)) if (nr is not None and orr is not None) else 0.0
        dc = (float(nc) - float(oc)) if (nc is not None and oc is not None) else 0.0
        if abs(dr) >= 1e-6 or abs(dc) >= 1e-6:
            any_change = True
            segs = []
            if abs(dr) >= 1e-6:
                segs.append(f"注册 {_fmt_num(orr)}→{_fmt_num(nr)}({'+' if dr > 0 else ''}{_fmt_num(dr)})")
            if abs(dc) >= 1e-6:
                segs.append(f"暗号短链 {_fmt_num(oc)}→{_fmt_num(nc)}({'+' if dc > 0 else ''}{_fmt_num(dc)})")
            mat_lbl = str(cur_p.get("material") or old_p.get("material") or "").strip()
            tm_lbl = str(cur_p.get("team") or old_p.get("team") or "").strip()
            name_part = f"{tm_lbl} {nm}".strip()
            title_lbl = f"{mat_lbl}：{name_part}" if mat_lbl else name_part
            p_deltas.append((abs(dr) * 10 + abs(dc), f"  - **{title_lbl}**：{'，'.join(segs)}"))

    if p_deltas:
        p_deltas.sort(key=lambda x: -x[0])
        lines.append("• **个人数值变化亮点**：\n" + "\n".join(x[1] for x in p_deltas[:4]))

    # 团队对比变化
    prev_t_map = {str(x.get("team") or ""): x for x in (prev.get("teams") or []) if x.get("team")}
    t_deltas = []
    ft = str(focus_team or "").strip()
    for cur_t in (curr.get("teams") or []):
        tm = str(cur_t.get("team") or "")
        if not tm or tm not in prev_t_map:
            continue
        old_t = prev_t_map[tm]
        ns, os_ = cur_t.get("cipher_score"), old_t.get("cipher_score")
        nr_t, or_t = cur_t.get("reg_total"), old_t.get("reg_total")
        ds = (float(ns) - float(os_)) if (ns is not None and os_ is not None) else 0.0
        dr_t = (float(nr_t) - float(or_t)) if (nr_t is not None and or_t is not None) else 0.0
        if abs(ds) >= 1e-6 or abs(dr_t) >= 1e-6:
            any_change = True
            segs = []
            if abs(ds) >= 1e-6:
                segs.append(f"暗号 {_fmt_num(os_)}→{_fmt_num(ns)}({'+' if ds > 0 else ''}{_fmt_num(ds)})")
            if abs(dr_t) >= 1e-6:
                segs.append(f"注册 {_fmt_num(or_t)}→{_fmt_num(nr_t)}({'+' if dr_t > 0 else ''}{_fmt_num(dr_t)})")
            prio = 1000 if (ft and ft in tm) else abs(ds + dr_t * 5)
            t_deltas.append((prio, f"  - **{tm}**：{'，'.join(segs)}"))

    if t_deltas:
        t_deltas.sort(key=lambda x: -x[0])
        lines.append("• **各团队变动**：\n" + "\n".join(x[1] for x in t_deltas[:4]))

    if not any_change:
        lines.append("• 对比上一轮档案，整体数值与各位成员数据暂无新增波动（全部持平）。")

    header = "刚刚这轮数据已粘贴整理完成！我帮您和上一轮比了一下变化：" if any_change else "刚刚这轮数据已粘贴整理完成！本轮盘面稳稳当当："
    return {
        "time": curr.get("time_str") or time.strftime("%H:%M:%S"),
        "round": curr.get("round") or "最新",
        "has_delta": any_change,
        "summary_line": lines[0].replace("**", "") if lines else "数据已核对",
        "reply": header + "\n" + "\n".join(lines),
    }


_greeting_lock = threading.Lock()
_greeting_prefetch_pool = []
_greeting_prefetch_busy = False


def _is_valid_complete_greeting(txt):
    """校验问候鼓励语是否完整通顺，严防思考模型消耗配额后出现半句或残片。"""
    s = str(txt or "").strip()
    if len(s) < 18 or len(s) > 150:
        return False
    if "*   " in s or "\n*" in s or "**" in s or "#" in s:
        return False
    low = s.lower()
    if any(bad in low for bad in ("gemini", "google", "gpt", "openai", "deepseek")):
        return False
    tail = s.rstrip(" \"'”’）)")
    if not tail:
        return False
    return tail[-1] in set("！!～~。？?呀哦啦呢吧哇哟呗哒")


def _spawn_greeting_prefetch(norm_cfg, sys_g, usr_g, seen_hashes_snap):
    global _greeting_prefetch_busy
    ai_cfg = (norm_cfg or {}).get("ai_assistant") or {}
    if not ai_cfg.get("enabled", True) or str(ai_cfg.get("provider") or "").strip().lower() == "local_only":
        return
    if not str(ai_cfg.get("api_key") or DEFAULT_GEMINI_API_KEY).strip():
        return
    with _greeting_lock:
        if _greeting_prefetch_busy or len(_greeting_prefetch_pool) >= 3:
            return
        _greeting_prefetch_busy = True

    def _bg():
        global _greeting_prefetch_busy
        import hashlib
        try:
            g_res = _call_gemini_generate(
                norm_cfg,
                sys_g,
                [{"role": "user", "parts": [{"text": usr_g}]}],
                temperature=0.95,
                max_tokens=1024,
                timeout_sec=6.5,
                max_models=1,
            )
            if g_res.get("ok"):
                cand = str(g_res.get("reply") or "").strip()
                if _is_valid_complete_greeting(cand):
                    h = hashlib.sha256(cand.encode("utf-8")).hexdigest()[:16]
                    with _greeting_lock:
                        if h not in seen_hashes_snap and all(x[1] != h for x in _greeting_prefetch_pool):
                            _greeting_prefetch_pool.append((cand, h))
        except Exception:
            pass
        finally:
            with _greeting_lock:
                _greeting_prefetch_busy = False

    threading.Thread(target=_bg, daemon=True, name="SheetMateGreetingPrefetch").start()


def generate_unique_startup_greeting(cfg=None, nickname="", force=False):
    """生成每次完完全全不重样、带真人情绪温度且句句完整的上班问候对话（支持零等待秒换一句）。"""
    import hashlib
    import random

    seen_path = app_dir() / "ai_greetings_seen.json"
    state = {"hashes": [], "recent_texts": [], "last_ts": 0.0, "last_date": ""}
    try:
        if seen_path.exists():
            loaded = json.loads(seen_path.read_text(encoding="utf-8", errors="ignore"))
            if isinstance(loaded, dict):
                clean_recent = [str(t).strip() for t in (loaded.get("recent_texts") or []) if _is_valid_complete_greeting(t)]
                state.update({
                    "hashes": list(loaded.get("hashes") or [])[-300:],
                    "recent_texts": clean_recent[-16:],
                    "last_ts": float(loaded.get("last_ts") or 0.0),
                    "last_date": str(loaded.get("last_date") or ""),
                })
    except Exception:
        pass

    now_ts = time.time()
    lt = time.localtime(now_ts)
    today_s = time.strftime("%Y-%m-%d", lt)
    hr = lt.tm_hour

    if not force:
        is_first_today = (state["last_date"] != today_s)
        gap_ok = (now_ts - state["last_ts"]) >= 25 * 60
        if not is_first_today and (not gap_ok or random.random() > 0.65):
            return {"show": False, "reply": ""}

    if 5 <= hr < 9:
        period_tag, period_ctx = "清晨早班", "刚起不久还带着点早上的微凉"
        openings = [
            "早呀{name}，外头天还刚亮呢，先深吸一口气慢慢清醒一下",
            "{name}早安！桌上的水倒好了吗？新的一天咱们不慌不忙开场",
            "早啊{name}，这么早来工位辛苦啦，让小猫先伸个懒腰陪你热热身",
            "哈啰{name}，早高峰刚过还是正赶上呀？先把心情调成轻松档位",
            "早呀早呀{name}，今天第一杯提神水我先精神上跟你碰个杯",
            "{name}来啦！清早脑子最灵光，咱们先挑一件小事顺手解决掉",
        ]
    elif 9 <= hr < 12:
        period_tag, period_ctx = "上午时光", "上午正适合稳稳推进手头节奏"
        openings = [
            "嗨{name}，上午好呀！刚坐稳就感觉今天干劲挺足的",
            "{name}上午好！我刚梳理完小窝，随时准备接手跑表格的体力活啦",
            "叮咚～{name}在不在呀，上午的阳光看着真舒服，今天咱们稳稳推进",
            "来啦{name}！上午状态一般最好，把繁琐的导表放着我来盯就行",
            "{name}好呀，刚打开界面我就猜你要开始忙啦，需要我帮忙随时吆喝",
            "早市忙碌开场啦{name}，放平心态，一件一件理顺可快了",
        ]
    elif 12 <= hr < 14:
        period_tag, period_ctx = "午间时段", "中午刚忙完或正准备吃饭小憩"
        openings = [
            "{name}中午好呀～忙着盯表也别委屈了肚子，记得好好吃顿热乎饭",
            "午安{name}！中午这会儿容易迷糊，吃饱后可以眯几分钟充充电",
            "嗨{name}，忙完一上午辛苦了，午间哪怕发呆两分钟也是很好的回血",
            "{name}吃饭了吗？我蹲在屏幕角替你看着数字，你放心去歇会儿",
            "中午啦{name}，别一直绷着肩膀啦，起来活动一下脖子再说",
        ]
    elif 14 <= hr < 18:
        period_tag, period_ctx = "下午茶时段", "下午三四点最容易犯困、需要加点小甜头"
        openings = [
            "下午好呀{name}！到这个点是不是有点想打哈欠了，小猫给你递颗糖",
            "嗨{name}，下午的困意交给我帮你挡一挡，烦人的重复点点点我来跑",
            "{name}在忙吗？下午光线柔和，顺手喝两口温水润润嗓子吧",
            "咚咚咚～{name}，坚持过下午这段小坡，离收工开心时刻又近一步啦",
            "{name}下午好，感觉累了就盯着我晃晃耳朵放松三秒钟再继续",
            "忙归忙，{name}下午也要给自己留口喘息的小空当呀",
        ]
    elif 18 <= hr < 21:
        period_tag, period_ctx = "傍晚收工时分", "天色擦黑、该慢慢收心总结今日战果"
        openings = [
            "傍晚好呀{name}，外头灯应该陆续亮起来了吧，今天也一路扛下来了真棒",
            "{name}辛苦一天啦，这会儿还开着软件是收个尾吗？剩下的跑数让我快马加鞭",
            "夕阳时间到～{name}别太赶，稳妥发完最后一波咱们就开心收工",
            "{name}晚上好呀，摸摸头，忙完手头这一小段一定记得按时吃饭回家",
            "辛苦了{name}，今天每一点数据里都有你的用心，我全帮你记在心里呢",
        ]
    else:
        period_tag, period_ctx = "夜深时刻", "夜深人静仍在电脑前忙着收尾或守数据"
        openings = [
            "{name}这么晚还在忙呀……先抱抱你，夜深了注意眼睛别离屏幕太近",
            "夜猫子{name}好，全办公室大概就我这只小猫还在安静亮着灯陪你啦",
            "{name}还没睡或刚值完这一班呀？贴完这一轮千万早点休息，熬夜伤神",
            "夜里凉，{name}披件外套吧，复杂的活丢给我跑，你喝口热饮歇歇",
            "深夜值守的{name}超厉害的，但身体永远排第一，收完这单就快合上电脑吧",
        ]

    wk_msgs = {
        0: ["虽说是周一要收收周末的心，不过慢慢进入状态就好，不用一下子拉满", "周一总是最忙的开头，好在有我帮你把导表和发群的机械活全包了", "新的一周开局啦，祝咱们这周数字一路飘红少踩坑"],
        1: ["周二节奏一般最稳当，今天搞不好能顺顺利利早点收手", "到周二啦，干起活来明显比昨天顺手多了对不对", "周二的微风都带着一股踏实劲儿，慢慢来都会搞定的"],
        2: ["周三一过这一周就走完一半啦，给自己点个赞再继续", "小周末周三到！离歇口气又近了半程，手头事一定顺顺利利", "周三最适合稳中带快，我随时待命给你搭把手"],
        3: ["周四啦，再坚持一下周末的曙光就在眼前晃悠了", "周四通常数据波动最有意思，今天也让我陪你一起盯紧亮点", "疯狂周四的快乐还没到，先让好心情提前上线"],
        4: ["耶！终于到周五啦，心里是不是已经开始惦记下班去吃什么了", "周五心情先加二十分！跑完今天这几轮就能美美开启周末", "周五冲刺不掉链子，细节盯紧点保证一次顺利发完"],
        5: ["周末还在上线忙工作呀，真是太敬业啦，有啥想偷懒的事全交给我跑", "周六辛苦了，做完这波赶紧去逛逛吃点好的犒劳一下自己", "周末值班也要快快乐乐的，我陪你把枯燥的活变快变轻"],
        6: ["周日晚上别太焦虑明天的事啦，先把手头这点收好安心休息", "周日还这么认真太赞了，今天我一定稳稳当当不给你添乱", "周日小伴工上线～顺顺利利跑完早点回血"],
    }

    cares = [
        "表格里起起伏伏太正常了，别光盯着一时的冷热自己内耗，有我陪着一起捋呢。",
        "遇到群消息发卡了或不知道怎么调日期，直接打字跟说一声，不用自己死琢磨。",
        "有时候电脑抽风不是你的错，真卡住了我会自动拦着不乱点、给你兜住现场的。",
        "看数字看累了就眨眨眼看看窗外，保持好心情反而出单出效果更快哦。",
        "每一次认真核对数据其实都在悄悄攒底气，今天也放轻松顺其自然发挥就好。",
        "如果你心里有哪个组或哪个成员特别惦记，输入框上面打个字或摇摇骰子我就帮你重点看。",
        "记得坐姿歪久了就往后靠一靠伸个懒腰，赚业绩也要保护好小腰和手腕。",
        "别看我只是只蹲在角落的小猫伙伴，干起活来可是认认真真一丝不苟的！",
    ]

    closings = [
        "有什么需要吩咐的，随时在下面敲字喊我就行～",
        "今天你想先看最新盘面，还是直接喊我排个新计划呀？",
        "好啦，话不多说，咱们一起顺顺当当开启今天这一波吧！",
        "加油呀，我在窗口这边随时给你摇尾巴打 call！",
        "不管忙不忙，我都一直在这儿听候调遣～",
        "祝今天每次发送都秒过无报错，数字嗖嗖往上蹿！",
    ]

    clean_nick = str(nickname or "").strip()
    if not clean_nick or clean_nick.startswith("未命名") or "编号" in clean_nick:
        call_name = "小伙伴"
    else:
        call_name = clean_nick

    seen_set = set(state["hashes"])
    recent_list = state["recent_texts"]

    def _is_unique(txt):
        h = hashlib.sha256(txt.strip().encode("utf-8")).hexdigest()[:16]
        return h not in seen_set, h

    chosen = ""
    chosen_hash = ""

    # 1) 优先取用后台预取的完整云端创意句（点【换一句】瞬间 0ms 响应不卡顿）
    norm_cfg = normalize_config(cfg if isinstance(cfg, dict) else {})
    avoid_txt = "\n".join(f"- {x}" for x in recent_list[-10:]) or "（暂无）"
    sys_g = (
        "你是「暗号表助手」界面里一只充满人情味、性格温暖俏皮的拟人萌宠小助手。"
        "严禁提起任何外部AI公司/模型名字，严禁使用Markdown符号或列表，不要自称机器程序。"
        "请像真朋友一样用大白话一次性说 2~3 句完整、有标点结尾的话（总共约 35~85 字），关心和鼓励对方工作。"
    )
    usr_g = (
        f"当前真实时间：{time.strftime('%Y-%m-%d %H:%M', lt)}（{period_tag}，{period_ctx}），星期{['一','二','三','四','五','六','日'][lt.tm_wday]}。\n"
        f"对方称呼：{call_name}。\n"
        f"请直接输出一句完整的全新鼓励对话，不要和下面重复：\n{avoid_txt}"
    )

    if (norm_cfg.get("ai_assistant") or {}).get("enabled", True):
        with _greeting_lock:
            while _greeting_prefetch_pool:
                c_cand, c_hash = _greeting_prefetch_pool.pop(0)
                if c_hash not in seen_set and _is_valid_complete_greeting(c_cand):
                    chosen, chosen_hash = c_cand, c_hash
                    break
        if not chosen and not force:
            try:
                g_res = _call_gemini_generate(
                    norm_cfg,
                    sys_g,
                    [{"role": "user", "parts": [{"text": usr_g}]}],
                    temperature=0.95,
                    max_tokens=1024,
                    timeout_sec=3.5,
                    max_models=1,
                )
                if g_res.get("ok"):
                    cand = str(g_res.get("reply") or "").strip()
                    if _is_valid_complete_greeting(cand):
                        ok_u, h_u = _is_unique(cand)
                        if ok_u:
                            chosen, chosen_hash = cand, h_u
            except Exception:
                pass
        _spawn_greeting_prefetch(norm_cfg, sys_g, usr_g, seen_set)

    # 2) 本地超高维度组合防重兜底（确保万次以上零重复）
    if not chosen:
        wk_pool = wk_msgs.get(lt.tm_wday, wk_msgs[0])
        for _ in range(40):
            op = random.choice(openings).format(name=call_name)
            wk = random.choice(wk_pool)
            cr = random.choice(cares)
            cl = random.choice(closings)
            style_pick = random.randint(0, 2)
            if style_pick == 0:
                cand = f"{op}！{wk}。{cr}{cl}"
            elif style_pick == 1:
                cand = f"{op}～ {cr}{cl}"
            else:
                cand = f"{op}。{wk}，记得{cr}"
            ok_u, h_u = _is_unique(cand)
            if ok_u:
                chosen, chosen_hash = cand, h_u
                break
        if not chosen:
            chosen = f"{random.choice(openings).format(name=call_name)}！今天是 {time.strftime('%m月%d日 %H:%M', lt)}，希望这一刻起事事顺手、心情明朗～{random.choice(closings)}"
            chosen_hash = hashlib.sha256(chosen.encode("utf-8")).hexdigest()[:16]

    state["hashes"] = (state["hashes"] + [chosen_hash])[-300:]
    state["recent_texts"] = (state["recent_texts"] + [chosen])[-16:]
    state["last_ts"] = now_ts
    state["last_date"] = today_s
    _atomic_write_json(seen_path, state)

    return {
        "show": True,
        "time": time.strftime("%H:%M:%S", lt),
        "period": period_tag,
        "reply": chosen,
    }


_ARCHIVE_IO_LOCK = threading.RLock()


def record_report_archive(cfg=None, round_label="手动", summary_tsv="", shot_path=None, report_dir=None):
    """将每轮汇总表与明细指标持久化追加到 report_archive.jsonl，并实时计算较上轮数据变动播报。"""
    global _last_archive_sig, _latest_round_spotlight
    tsv_use = str(summary_tsv or _last_captured_summary_tsv or "").strip()
    parsed = parse_summary_tsv_snapshot(tsv_use, report_dir=report_dir)
    if not any([parsed["personal"], parsed["teams"], parsed["daily_reg"], parsed["top_keywords"], parsed["top_channels"]]):
        return None

    sig_payload = json.dumps({
        "p0": parsed["personal"][:2],
        "t0": parsed["teams"][:2],
        "d0": parsed["daily_reg"][:1],
        "r": str(round_label),
    }, ensure_ascii=False, sort_keys=True)
    now_ts = time.time()
    # 防止同一次执行既在截图结束又在发送后重复落盘两份完全相同的数据
    cur_sig = f"{int(now_ts // 30)}|{sig_payload}"
    spot = None
    sp_cb = None
    with _ARCHIVE_IO_LOCK:
        if cur_sig == _last_archive_sig:
            return None
        _last_archive_sig = cur_sig

        prev_recs = load_report_archive(limit=5)
        prev_rec = prev_recs[-1] if prev_recs else None

        shot_name = Path(shot_path).name if shot_path else ""
        rec = {
            "ts": int(now_ts),
            "time_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now_ts)),
            "round": str(round_label or "手动"),
            "shot": shot_name,
            "personal": parsed["personal"],
            "teams": parsed["teams"],
            "summary_totals": parsed["summary_totals"],
            "daily_reg": parsed["daily_reg"],
            "top_keywords": parsed["top_keywords"],
            "top_channels": parsed["top_channels"],
            "raw_tsv": tsv_use[:12000],
        }

        arch_p = _archive_file_path()
        try:
            existing_lines = []
            if arch_p.exists():
                existing_lines = [ln for ln in arch_p.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip()]
            existing_lines.append(json.dumps(rec, ensure_ascii=False))
            if len(existing_lines) > 120:
                existing_lines = existing_lines[-120:]
            _atomic_write_text(arch_p, "\n".join(existing_lines) + "\n")

            ft = str(((cfg or {}).get("ai_assistant") or {}).get("focus_team") or "").strip()
            spot = build_round_change_spotlight(prev_rec, rec, focus_team=ft)
            with _runtime_lock:
                _latest_round_spotlight = spot
                sp_cb = _spotlight_callback
        except Exception:
            return None
    if isinstance(spot, dict) and spot.get("summary_line"):
        log(f"  💡 [数据变化速递] {spot['summary_line']}")
    if sp_cb is not None and spot is not None:
        try:
            sp_cb(spot)
        except Exception:
            pass
    return rec


def load_report_archive(limit=30):
    """按从旧到新顺序读取最近 limit 条历史表格数据档案。"""
    arch_p = _archive_file_path()
    if not arch_p.exists():
        return []
    out = []
    try:
        with _ARCHIVE_IO_LOCK:
            lines = [ln for ln in arch_p.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip()]
        for ln in lines[-int(limit):]:
            try:
                obj = json.loads(ln)
                if isinstance(obj, dict):
                    obj["personal"] = _filter_valid_personal_entries(obj.get("personal") or [])
                    out.append(obj)
            except Exception:
                pass
    except Exception:
        pass
    return out


def get_dashboard_data_brief(report_dir=None):
    """返回供控制台顶部看板卡片与状态栏展示的最新盘面摘要与归档统计。"""
    records = load_report_archive(limit=120)
    archive_count = len(records)
    latest = records[-1] if records else None
    if latest is None:
        live = parse_summary_tsv_snapshot("", report_dir=report_dir)
        latest = {
            "time_str": "本地日报快照" if (live["daily_reg"] or live["top_keywords"]) else "",
            "personal": live["personal"],
            "teams": live["teams"],
            "daily_reg": live["daily_reg"],
            "top_keywords": live["top_keywords"],
            "top_channels": live["top_channels"],
        }

    reg_list = latest.get("daily_reg") or []
    reg0 = reg_list[0] if reg_list else None
    personal = _filter_valid_personal_entries(latest.get("personal") or [])
    teams = latest.get("teams") or []

    reg_val = f"注册 {int(reg0['total']) if reg0 and reg0.get('total') is not None else '—'} · 华人 {int(reg0['cn']) if reg0 and reg0.get('cn') is not None else '—'}" if reg0 else "待抓取首轮数据"
    reg_sub = f"{reg0.get('date', '')} · 华人占比 {reg0.get('ratio', '—')}% · 档案 {archive_count} 轮" if reg0 else f"已保存 {archive_count} 轮档案"

    mujie = next((t for t in teams if "慕杰" in str(t.get("team", ""))), None)
    top_team = teams[0] if teams else None
    top_person = personal[0] if personal else None
    if top_person and mujie:
        top_p_reg = top_person.get("reg_total") or top_person.get("cn_reg_sum") or top_person.get("cn_reg") or 0
        top_val = f"{top_person.get('name')} 居首 · {mujie.get('team')}"
        top_sub = f"榜首华人/注册 {int(round(float(top_p_reg)))} | {mujie.get('team')}暗号 {mujie.get('cipher_score', '—')}"
    elif top_person:
        top_p_reg = top_person.get("reg_total") or top_person.get("cn_reg_sum") or top_person.get("cn_reg") or "—"
        top_val = f"个人榜首：{top_person.get('name')}"
        top_sub = f"注册 {top_p_reg} · 暗号+短链 {top_person.get('cipher_link_total', '—')}"
    elif top_team:
        top_val = f"团队榜首：{top_team.get('team')}"
        top_sub = f"暗号总数 {top_team.get('cipher_score', '—')} · 注册 {top_team.get('reg_total', '—')}"
    else:
        kw_cnt = len(latest.get("top_keywords") or [])
        ch_cnt = len(latest.get("top_channels") or [])
        top_val = f"热门暗号 {kw_cnt} 条 · 渠道 {ch_cnt} 项" if (kw_cnt or ch_cnt) else "暂无个人/团队榜单"
        top_sub = "跑完一轮含汇总表截图流程后自动提取排名"

    return {
        "archive_count": archive_count,
        "latest_time": latest.get("time_str") or "",
        "reg_val": reg_val,
        "reg_sub": reg_sub,
        "top_val": top_val,
        "top_sub": top_sub,
        "has_archive": bool(archive_count > 0 or reg0 or personal or teams),
    }


def build_archive_query_context(user_text="", report_dir=None, focus_team=""):
    """基于持久化表格档案，按提问关键词与当前关注组追踪个人/团队历史轨迹并组装最新多轮对比上下文。"""
    records = load_report_archive(limit=30)
    if not records:
        live = parse_summary_tsv_snapshot(_last_captured_summary_tsv, report_dir=report_dir)
        if any([live["personal"], live["teams"], live["daily_reg"], live["top_keywords"], live["top_channels"]]):
            records = [{
                "time_str": "当前桌面数据日报",
                "round": "实时快照",
                **live,
            }]
    if not records:
        return "（本地尚未归档过历次跑表数字档案，仅如有截图则在图中读取；完成一轮跑表后将自动记录全部个人/团队与历史曲线。）"

    latest = records[-1]
    blocks = []
    blocks.append(f"【本地持久化数据档案共 {len(records)} 轮（最新：{latest.get('time_str')}｜轮次：{latest.get('round')}）】")

    ft_clean = str(focus_team or "").strip()
    # 1) 提取最近 5 轮概览对比
    trend_lines = []
    for rec in records[-5:]:
        r0 = (rec.get("daily_reg") or [{}])[0]
        p0 = (rec.get("personal") or [{}])[0]
        t_list = rec.get("teams") or []
        picked_t = (next((t for t in t_list if ft_clean and ft_clean in str(t.get("team", ""))), None) if ft_clean else None) or (t_list[0] if t_list else {})
        t_parts = [f"{rec.get('time_str')}({rec.get('round')})"]
        if r0.get("total") is not None:
            t_parts.append(f"注册={r0.get('total')}/华人={r0.get('cn')}({r0.get('ratio')}%)")
        if p0.get("name"):
            t_parts.append(f"个人第一={p0.get('name')}(cn注册={p0.get('cn_reg') or p0.get('reg_total')})")
        if picked_t.get("team"):
            t_parts.append(f"{picked_t.get('team')}(暗号={picked_t.get('cipher_score')},注册={picked_t.get('reg_total')})")
        trend_lines.append("  • " + " | ".join(t_parts))
    if trend_lines:
        blocks.append("【近几轮核心趋势摘要】\n" + "\n".join(trend_lines))

    # 2) 按用户点名精准追踪指定人物或团队的历史表现
    q_txt = str(user_text or "")
    all_names = []
    all_teams = []
    for rec in records:
        for p in (rec.get("personal") or []):
            nm = str(p.get("name") or "").strip()
            if nm and nm not in all_names:
                all_names.append(nm)
        for t in (rec.get("teams") or []):
            tm = str(t.get("team") or "").strip()
            if tm and tm not in all_teams:
                all_teams.append(tm)

    hit_names = [nm for nm in all_names if nm in q_txt]
    hit_teams = [tm for tm in all_teams if tm in q_txt or (tm.endswith("组") and tm[:-1] in q_txt)]
    if hit_names or hit_teams:
        focus_lines = []
        for nm in hit_names[:6]:
            pts = []
            for rec in records[-12:]:
                m = next((p for p in (rec.get("personal") or []) if p.get("name") == nm), None)
                if m:
                    pts.append(
                        f"{rec.get('time_str')}[明排名#{m.get('rank_detail', '-')},注排名#{m.get('rank_reg', '-')},"
                        f"暗+链={m.get('cipher_link_total', '-')},注册={m.get('reg_total', '-')},"
                        f"华人注={m.get('cn_reg', '-')},搜索={m.get('search_users', '-')},明细={m.get('detail', '')[:45]}]"
                    )
            if pts:
                focus_lines.append(f"• 个人【{nm}】历次记录（共命中 {len(pts)} 轮）：\n  " + "\n  ".join(pts))
        for tm in hit_teams[:5]:
            pts = []
            for rec in records[-12:]:
                m = next((t for t in (rec.get("teams") or []) if t.get("team") == tm), None)
                if m:
                    pts.append(
                        f"{rec.get('time_str')}[排名#{m.get('rank', '-')},暗号总数={m.get('cipher_score', '-')},"
                        f"注册={m.get('reg_total', '-')},明细={m.get('detail', '')[:60]}]"
                    )
            if pts:
                focus_lines.append(f"• 团队【{tm}】历次记录（共命中 {len(pts)} 轮）：\n  " + "\n  ".join(pts))
        if focus_lines:
            blocks.append("【按您提问点名检索到的历史数据轨迹】\n" + "\n".join(focus_lines))

    # 3) 最新一轮详细榜单（团队 + Top 15 个人 + 近7日注册 + 热门暗号/渠道）
    if latest.get("teams"):
        t_lines = [
            f"  #{t.get('rank', '-')} {t.get('team')}: 暗号={t.get('cipher_score', '-')}, 注册={t.get('reg_total', '-')}, 明细={t.get('detail', '')[:70]}"
            for t in latest["teams"][:10]
        ]
        blocks.append("【最新一轮团队排名】\n" + "\n".join(t_lines))
    if latest.get("personal"):
        p_lines = [
            f"  #{p.get('rank_reg', p.get('rank_detail', '-'))} {p.get('name')}: 华人注册={p.get('cn_reg', '-')}, 注册={p.get('reg_total', '-')}, 暗+链={p.get('cipher_link_total', '-')}, 占比={p.get('share', '-')}, 明细={p.get('detail', '')[:50]}"
            for p in latest["personal"][:18]
        ]
        blocks.append("【最新一轮重点个人榜单(Top 18)】\n" + "\n".join(p_lines))
    if latest.get("daily_reg"):
        d_lines = [f"  {d.get('date')}: 总注册={d.get('total')}, 华人={d.get('cn')}, 占比={d.get('ratio')}%" for d in latest["daily_reg"][:7]]
        blocks.append("【近7日盘面注册走势】\n" + "\n".join(d_lines))
    if latest.get("raw_tsv"):
        blocks.append(f"【最新汇总表原始 TSV 节选】\n{latest['raw_tsv'][:1400]}")

    out = "\n\n".join(blocks)
    return out[:4800]


_AI_MEM_LOCK = threading.RLock()
_AI_MEM_CACHE = {}
_AI_MEM_CHANGE_LOCK = threading.Lock()
_AI_MEM_CHANGE_CB = None


def set_ai_memory_change_callback(fn):
    """注册 AI 记忆/话术变更回调，供账号云同步防抖上传。"""
    global _AI_MEM_CHANGE_CB
    with _AI_MEM_CHANGE_LOCK:
        _AI_MEM_CHANGE_CB = fn


def _notify_ai_memory_changed(reason="memory"):
    with _AI_MEM_CHANGE_LOCK:
        cb = _AI_MEM_CHANGE_CB
    if cb is not None:
        try:
            cb(reason)
        except Exception:
            pass


def _ai_user_memory_path():
    return app_dir() / "ai_user_memory.json"


def _default_ai_user_memory():
    return {
        "version": 1,
        "self_real_name": "",
        "preferred_targets": [],
        "last_confirmed_targets": [],
        "last_action": "",
        "preferred_day_offset": None,
        "preferred_silent": None,
        "forbid_group_send": False,
        "name_corrections": {},
        "habit_notes": [],
        "chat_history": [],
    }


def _normalize_ai_user_memory(raw):
    import copy
    base = _default_ai_user_memory()
    if not isinstance(raw, dict):
        return base
    srn = str(raw.get("self_real_name") or "").strip()
    if srn and not is_likely_real_person_name(srn):
        srn = ""
    base["self_real_name"] = srn

    for k in ("preferred_targets", "last_confirmed_targets"):
        vals = raw.get(k)
        if isinstance(vals, list):
            clean_v = []
            for x in vals:
                sx = str(x or "").strip()
                if sx and not _is_chat_sentence_noise(sx) and sx not in clean_v:
                    clean_v.append(sx[:24])
            base[k] = clean_v[:8]

    act = str(raw.get("last_action") or "").strip()
    base["last_action"] = act if act in ("", "run_full", "capture_send", "text_send") else ""

    pdo = raw.get("preferred_day_offset")
    if pdo is not None and not isinstance(pdo, bool) and str(pdo).strip() != "":
        try:
            base["preferred_day_offset"] = max(-60, min(7, int(pdo)))
        except Exception:
            base["preferred_day_offset"] = None

    ps = raw.get("preferred_silent")
    base["preferred_silent"] = ps if isinstance(ps, bool) else None
    base["forbid_group_send"] = bool(raw.get("forbid_group_send", False))

    corr = raw.get("name_corrections")
    if isinstance(corr, dict):
        clean_corr = {}
        for wk, rv in corr.items():
            wks = str(wk or "").strip()
            rvs = str(rv or "").strip()
            if wks and rvs and wks != rvs and wks not in _DT_GENERIC_ALIAS_WORDS and rvs not in _DT_GENERIC_ALIAS_WORDS:
                if not _is_chat_sentence_noise(wks) and not _is_chat_sentence_noise(rvs):
                    clean_corr[wks[:24]] = rvs[:24]
        base["name_corrections"] = clean_corr

    notes = raw.get("habit_notes")
    if isinstance(notes, list):
        clean_notes = []
        for n in notes:
            sn = str(n or "").strip()
            if sn and len(sn) <= 120 and sn not in clean_notes:
                clean_notes.append(sn)
        base["habit_notes"] = clean_notes[-30:]

    hist = raw.get("chat_history")
    if isinstance(hist, list):
        clean_h = []
        for h in hist[-30:]:
            if not isinstance(h, dict):
                continue
            txt = str(h.get("text") or "").strip()
            if not txt:
                continue
            item = {
                "role": "model" if h.get("role") == "model" else "user",
                "text": txt[:2400],
                "time": str(h.get("time") or ""),
                "source": str(h.get("source") or ""),
                "shot_info": str(h.get("shot_info") or ""),
                "via": str(h.get("via") or "pc"),
            }
            if h.get("sender"):
                item["sender"] = str(h["sender"])[:30]
            if isinstance(h.get("on_demand_meta"), dict):
                item["on_demand_meta"] = copy.deepcopy(h["on_demand_meta"])
            if isinstance(h.get("thinking_steps"), list) and h["thinking_steps"]:
                clean_steps = [str(x).strip()[:120] for x in h["thinking_steps"][:6] if str(x).strip()]
                if clean_steps:
                    item["thinking_steps"] = clean_steps
            if h.get("elapsed_sec") is not None:
                try:
                    item["elapsed_sec"] = round(max(0.0, float(h["elapsed_sec"])), 1)
                except (TypeError, ValueError):
                    pass
            clean_h.append(item)
        base["chat_history"] = clean_h[-30:]
    return base


def load_ai_user_memory():
    """读取本机专属 AI 个人习惯、姓名纠错与近期聊天持久化记忆（按路径缓存）。"""
    import copy
    p = _ai_user_memory_path()
    key = str(p)
    with _AI_MEM_LOCK:
        if key in _AI_MEM_CACHE:
            return copy.deepcopy(_AI_MEM_CACHE[key])
        data = None
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                data = None
        norm = _normalize_ai_user_memory(data)
        _AI_MEM_CACHE[key] = copy.deepcopy(norm)
        return copy.deepcopy(norm)


def save_ai_user_memory(mem, notify=True):
    """线程安全地原子持久化保存本机 AI 个人习惯与记忆。"""
    import copy
    p = _ai_user_memory_path()
    key = str(p)
    norm = _normalize_ai_user_memory(mem)
    with _AI_MEM_LOCK:
        _AI_MEM_CACHE[key] = copy.deepcopy(norm)
        _atomic_write_json(p, norm)
    if notify:
        _notify_ai_memory_changed("memory")
    return copy.deepcopy(norm)


def export_ai_sync_bundle():
    """导出可用于跨电脑云同步的个人 AI 记忆、话术偏好、运行训练经验及已验证人名纠错映射。"""
    return {
        "ai_user_memory": load_ai_user_memory(),
        "ai_run_playbook": load_ai_run_playbook(),
        "dt_verified_targets": _load_verified_dingtalk_targets(),
    }


def import_ai_sync_bundle(bundle, reset_if_missing=False):
    """从云端同步包恢复本机 AI 记忆、训练经验与人名映射，不触发回推避免循环同步。"""
    b = bundle if isinstance(bundle, dict) else {}
    if isinstance(b.get("ai_user_memory"), dict):
        save_ai_user_memory(b["ai_user_memory"], notify=False)
    elif reset_if_missing:
        save_ai_user_memory(_default_ai_user_memory(), notify=False)
    if isinstance(b.get("ai_run_playbook"), dict):
        save_ai_run_playbook(b["ai_run_playbook"], notify=False)
    elif reset_if_missing:
        save_ai_run_playbook(_default_ai_run_playbook(), notify=False)
    vt_in = b.get("dt_verified_targets")
    if isinstance(vt_in, dict):
        clean_vt = {
            str(k).strip(): str(v).strip()
            for k, v in vt_in.items()
            if _is_valid_verified_dingtalk_target(k, v)
        }
        with _AI_MEM_LOCK:
            _atomic_write_json(_verified_targets_file(), clean_vt)
    elif reset_if_missing:
        with _AI_MEM_LOCK:
            _atomic_write_json(_verified_targets_file(), {})
    return export_ai_sync_bundle()


# ==================== AI 每次运行自训练与问题归纳优化引擎 ====================
_PLAYBOOK_LOCK = threading.RLock()
_RUN_TRACE_LOCK = threading.RLock()

DEFAULT_DT_FOCUS_STRATEGIES = (
    "uia_edit_child",
    "uia_above_bottombar",
    "uia_input_wnd_lower",
    "ratio_bottom_85",
    "ratio_bottom_89",
    "ratio_bottom_79",
)
DEFAULT_DT_PASTE_METHODS = (
    "win32_sendinput_ctrl_v",
    "pywinauto_ctrl_v",
    "uia_ctrl_v",
    "context_menu_paste",
)
_FOCUS_STRATEGY_CN = {
    "uia_edit_child": "UIA输入控件精确聚焦",
    "uia_above_bottombar": "发送底栏正上方安全区聚焦",
    "uia_input_wnd_lower": "输入容器下半区聚焦",
    "ratio_bottom_85": "85%底栏高比例聚焦",
    "ratio_bottom_89": "89%近底栏聚焦",
    "ratio_bottom_79": "79%中下区兜底聚焦",
}
_PASTE_METHOD_CN = {
    "win32_sendinput_ctrl_v": "Win32硬件扫描码Ctrl+V",
    "pywinauto_ctrl_v": "pywinauto组合键^v",
    "uia_ctrl_v": "UIA快捷键Ctrl+V",
    "context_menu_paste": "输入框右键菜单粘贴",
}


def set_training_summary_callback(fn):
    global _training_summary_callback
    _training_summary_callback = fn


def _notify_training_summary(obj):
    cb = _training_summary_callback
    if cb is not None and isinstance(obj, dict):
        try:
            cb(copy.deepcopy(obj))
        except Exception:
            pass


def _ai_run_playbook_path():
    return app_dir() / "ai_run_playbook.json"


def _new_empty_run_trace(mode="full", round_label=""):
    return {
        "mode": str(mode or "full"),
        "round": str(round_label or ""),
        "started_at": time.time(),
        "targets": [],
        "focus_attempts": [],
        "paste_attempts": [],
        "text_methods": [],
        "silent_attempts": [],
        "wps_capture_attempts": [],
        "heal_events": [],
        "user_recoveries": [],
        "auto_confirms": [],
        "errors": [],
    }


_RUN_TRAINING_TRACE = _new_empty_run_trace()


def reset_run_training_trace(mode="full", round_label=""):
    """在每轮自动化开始前清空单轮行为轨迹，以便结束后精准复盘提炼经验。"""
    global _RUN_TRAINING_TRACE
    with _RUN_TRACE_LOCK:
        _RUN_TRAINING_TRACE = _new_empty_run_trace(mode=mode, round_label=round_label)


def record_run_trace_event(kind, detail="", meta=None):
    """记录本轮执行中的聚焦策略、粘贴手法、静音选择、自愈与用户操作轨迹。"""
    k = str(kind or "").strip()
    if not k:
        return
    m = dict(meta) if isinstance(meta, dict) else {}
    d = str(detail or "").strip()[:180]
    with _RUN_TRACE_LOCK:
        tr = _RUN_TRAINING_TRACE
        if k == "dt_target" and d and d not in tr["targets"]:
            tr["targets"].append(d)
        elif k == "focus_ok":
            tr["focus_attempts"].append({"strategy": d, "ok": True, "meta": m})
        elif k == "focus_fail":
            tr["focus_attempts"].append({"strategy": d, "ok": False, "meta": m})
        elif k == "paste_ok":
            tr["paste_attempts"].append({"method": d, "ok": True, "focus": str(m.get("focus") or ""), "meta": m})
        elif k == "paste_fail":
            tr["paste_attempts"].append({"method": d, "ok": False, "focus": str(m.get("focus") or ""), "reason": str(m.get("reason") or d), "meta": m})
        elif k == "text_method":
            tr["text_methods"].append({"method": d, "ok": bool(m.get("ok", True))})
        elif k == "silent_ok":
            tr["silent_attempts"].append({"method": d, "ok": True})
        elif k == "silent_fail":
            tr["silent_attempts"].append({"method": d, "ok": False, "reason": str(m.get("reason") or "")})
        elif k == "wps_capture_ok":
            tr["wps_capture_attempts"].append({"method": d, "ok": True, "meta": m})
        elif k == "wps_capture_fail":
            tr["wps_capture_attempts"].append({"method": d, "ok": False, "reason": str(m.get("reason") or d), "meta": m})
        elif k == "wps_rclick_a1_guard":
            tr["wps_capture_attempts"].append({"method": "a1_guard", "ok": False, "reason": d, "meta": m})
        elif k == "heal_applied":
            tr["heal_events"].append({"stage": str(m.get("stage") or ""), "detail": d, "actions": list(m.get("actions") or [])})
        elif k == "user_recovery":
            tr["user_recoveries"].append({"choice": d, "ts": time.strftime("%H:%M:%S")})
        elif k == "auto_confirm":
            tr["auto_confirms"].append({"detail": d, "attempt": int(m.get("attempt") or 1)})
        elif k == "error":
            tr["errors"].append({"code": str(m.get("code") or ""), "detail": d})


def get_run_training_trace():
    with _RUN_TRACE_LOCK:
        return copy.deepcopy(_RUN_TRAINING_TRACE)


def _default_ai_run_playbook():
    return {
        "version": 1,
        "total_runs": 0,
        "success_runs": 0,
        "healed_runs": 0,
        "failed_runs": 0,
        "streak_ok": 0,
        "focus_stats": {},
        "paste_stats": {},
        "silent_stats": {},
        "wps_capture_stats": {},
        "wps_capture_anchor": None,
        "recommended_paste_delay_sec": None,
        "last_unresolved_issue": None,
        "problem_lessons": [],
        "recent_trainings": [],
    }


def _normalize_stat_map(raw_map):
    out = {}
    if not isinstance(raw_map, dict):
        return out
    for k, v in raw_map.items():
        ks = str(k or "").strip()[:48]
        if not ks or not isinstance(v, dict):
            continue
        ok_n = max(0, int(v.get("ok") or 0))
        fail_n = max(0, int(v.get("fail") or 0))
        out[ks] = {"ok": ok_n, "fail": fail_n}
    return out


def _normalize_ai_run_playbook(raw):
    base = _default_ai_run_playbook()
    if not isinstance(raw, dict):
        return base
    for k in ("total_runs", "success_runs", "healed_runs", "failed_runs", "streak_ok"):
        try:
            base[k] = max(0, int(raw.get(k) or 0))
        except Exception:
            base[k] = 0
    base["focus_stats"] = _normalize_stat_map(raw.get("focus_stats"))
    base["paste_stats"] = _normalize_stat_map(raw.get("paste_stats"))
    base["silent_stats"] = _normalize_stat_map(raw.get("silent_stats"))
    base["wps_capture_stats"] = _normalize_stat_map(raw.get("wps_capture_stats"))
    anc = raw.get("wps_capture_anchor")
    if isinstance(anc, dict):
        try:
            xp = round(float(anc.get("x_pct")), 4)
            yp = round(float(anc.get("y_pct")), 4)
            vw = max(0, int(anc.get("vw") or 0))
            vh = max(0, int(anc.get("vh") or 0))
            mth = str(anc.get("method") or "")[:32]
            if 0.08 <= xp <= 0.85 and 0.30 <= yp <= 0.88:
                base["wps_capture_anchor"] = {
                    "x_pct": xp,
                    "y_pct": yp,
                    "vw": vw,
                    "vh": vh,
                    "method": mth,
                }
        except Exception:
            pass
    rec_d = raw.get("recommended_paste_delay_sec")
    if rec_d is not None and not isinstance(rec_d, bool) and str(rec_d).strip() != "":
        try:
            base["recommended_paste_delay_sec"] = max(1, min(8, int(rec_d)))
        except Exception:
            base["recommended_paste_delay_sec"] = None
    unres = raw.get("last_unresolved_issue")
    if isinstance(unres, dict) and (unres.get("problem_summary") or unres.get("err_code")):
        base["last_unresolved_issue"] = {
            "ts": str(unres.get("ts") or "")[:24],
            "err_code": str(unres.get("err_code") or "")[:24],
            "stage": str(unres.get("stage") or "")[:32],
            "problem_summary": str(unres.get("problem_summary") or "")[:200],
            "actions_tried": [str(x)[:40] for x in (unres.get("actions_tried") or [])[:8]],
        }
    lessons = raw.get("problem_lessons")
    if isinstance(lessons, list):
        clean_l = []
        for it in lessons[-40:]:
            if not isinstance(it, dict):
                continue
            ps = str(it.get("problem_summary") or "").strip()[:200]
            if not ps:
                continue
            clean_l.append({
                "key": str(it.get("key") or ps[:40])[:64],
                "err_code": str(it.get("err_code") or "")[:24],
                "stage": str(it.get("stage") or "")[:32],
                "problem_summary": ps,
                "how_resolved": str(it.get("how_resolved") or "")[:220],
                "next_run_rule": str(it.get("next_run_rule") or "")[:220],
                "hit_count": max(1, int(it.get("hit_count") or 1)),
                "resolved_count": max(0, int(it.get("resolved_count") or 0)),
                "last_ts": str(it.get("last_ts") or "")[:24],
            })
        base["problem_lessons"] = clean_l[-40:]
    recs = raw.get("recent_trainings")
    if isinstance(recs, list):
        clean_r = []
        for r in recs[-20:]:
            if not isinstance(r, dict):
                continue
            clean_r.append({
                "ts": str(r.get("ts") or "")[:24],
                "mode": str(r.get("mode") or "full")[:24],
                "status": str(r.get("status") or "ok")[:24],
                "duration_sec": max(0, int(r.get("duration_sec") or 0)),
                "win_focus": str(r.get("win_focus") or "")[:40],
                "win_paste": str(r.get("win_paste") or "")[:40],
                "summary_line": str(r.get("summary_line") or "")[:240],
            })
        base["recent_trainings"] = clean_r[-20:]
    return base


def load_ai_run_playbook():
    """读取本机 AI 每次运行自训练经验库（胜率统计与问题复盘教训）。"""
    p = _ai_run_playbook_path()
    with _PLAYBOOK_LOCK:
        data = None
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                data = None
        return _normalize_ai_run_playbook(data)


def save_ai_run_playbook(pb, notify=True):
    """线程安全地原子保存 AI 运行训练经验库。"""
    norm = _normalize_ai_run_playbook(pb)
    with _PLAYBOOK_LOCK:
        _atomic_write_json(_ai_run_playbook_path(), norm)
    if notify:
        _notify_ai_memory_changed("playbook")
    return copy.deepcopy(norm)


def _rank_playbook_options(default_tuple, stats_map):
    def_idx = {name: idx for idx, name in enumerate(default_tuple)}
    base_name = default_tuple[0]
    bs = (stats_map or {}).get(base_name) or {}
    base_ok = int(bs.get("ok") or 0)
    base_fail = int(bs.get("fail") or 0)
    base_score = (base_ok + 1.5) / (base_ok + base_fail + 3.0)

    def _sort_key(name):
        st = (stats_map or {}).get(name) or {}
        ok_n = int(st.get("ok") or 0)
        fail_n = int(st.get("fail") or 0)
        score = (ok_n + 1.5) / (ok_n + fail_n + 3.0)
        # 仅当其它候选具备明确实测胜场 (ok > fail) 且胜率高于首选项时前置，避免单次偶发抖动乱序
        promoted = 1 if (name != base_name and ok_n >= 1 and ok_n > fail_n and score > base_score + 0.01) else 0
        return (-promoted, -round(score, 4), def_idx.get(name, 999))

    return sorted(list(default_tuple), key=_sort_key)


def _record_wps_capture_learned_anchor(method, x_pct=None, y_pct=None, vw=0, vh=0, ok=True):
    """零阻塞更新 AI 经验库中 WPS 200% 缩放右键/按键胜率与安全内点坐标（严禁记住 A1 区域）。"""
    try:
        pb = load_ai_run_playbook()
        stats = pb.setdefault("wps_capture_stats", {})
        rec = stats.setdefault(str(method or "unknown")[:32], {"ok": 0, "fail": 0})
        rec["ok" if ok else "fail"] += 1
        if ok and x_pct is not None and y_pct is not None:
            xp = round(float(x_pct), 4)
            yp = round(float(y_pct), 4)
            if 0.08 <= xp <= 0.85 and 0.30 <= yp <= 0.88:
                pb["wps_capture_anchor"] = {
                    "x_pct": xp,
                    "y_pct": yp,
                    "vw": max(0, int(vw or 0)),
                    "vh": max(0, int(vh or 0)),
                    "method": str(method or "border_rclick")[:32],
                }
        save_ai_run_playbook(pb, notify=False)
    except Exception:
        pass


def get_dt_runtime_strategy_from_playbook(cfg=None):
    """依据历史成功/失败训练数据动态生成本轮钉钉最优聚焦、粘贴与静音执行顺序。"""
    pb = load_ai_run_playbook()
    c = normalize_config(cfg if isinstance(cfg, dict) else {})
    focus_order = _rank_playbook_options(DEFAULT_DT_FOCUS_STRATEGIES, pb.get("focus_stats"))
    paste_order = _rank_playbook_options(DEFAULT_DT_PASTE_METHODS, pb.get("paste_stats"))

    try:
        user_delay = max(0, min(20, int(round(float(c.get("dt_paste_delay_sec", 3) or 3)))))
    except Exception:
        user_delay = 3
    rec_delay = pb.get("recommended_paste_delay_sec")
    effective_delay = max(user_delay, int(rec_delay)) if (isinstance(rec_delay, int) and user_delay > 0) else user_delay

    pt_cfg = c.get("pipeline_tuning") if isinstance(c.get("pipeline_tuning"), dict) else {}
    silent_order = str(pt_cfg.get("silent_step_order") or "uia_first")
    sil_stats = pb.get("silent_stats") or {}
    uia_ok = int((sil_stats.get("uia") or {}).get("ok") or 0)
    uia_fail = int((sil_stats.get("uia") or {}).get("fail") or 0)
    img_ok = int((sil_stats.get("image") or {}).get("ok") or 0)
    img_fail = int((sil_stats.get("image") or {}).get("fail") or 0)
    if silent_order == "uia_first" and uia_fail >= 2 and (img_ok + 1.0) / (img_ok + img_fail + 2.0) > (uia_ok + 1.0) / (uia_ok + uia_fail + 2.0):
        silent_order = "arrow_img_then_uia"

    top_f_cn = _FOCUS_STRATEGY_CN.get(focus_order[0], focus_order[0])
    top_p_cn = _PASTE_METHOD_CN.get(paste_order[0], paste_order[0])
    summary = (
        f"第 {int(pb.get('total_runs') or 0) + 1} 轮训练执行：优先「{top_f_cn}」+「{top_p_cn}」，"
        f"等待 {effective_delay}s，静音方案「{'图像优先' if silent_order == 'arrow_img_then_uia' else 'UIA优先'}」"
    )
    return {
        "focus_order": focus_order,
        "paste_order": paste_order,
        "silent_order": silent_order,
        "effective_paste_delay_sec": effective_delay,
        "recent_lessons": list((pb.get("problem_lessons") or [])[-5:]),
        "summary": summary,
    }


def _upsert_playbook_lesson(pb, err_code, stage, problem_summary, how_resolved, next_run_rule, resolved=False):
    lessons = list(pb.get("problem_lessons") or [])
    key = f"{err_code or stage}|{problem_summary[:28]}"
    now_s = time.strftime("%Y-%m-%d %H:%M:%S")
    hit = next((x for x in lessons if x.get("key") == key or (err_code and x.get("err_code") == err_code and x.get("stage") == stage)), None)
    if hit is not None:
        hit["problem_summary"] = str(problem_summary or hit.get("problem_summary") or "")[:200]
        if how_resolved:
            hit["how_resolved"] = str(how_resolved)[:220]
        if next_run_rule:
            hit["next_run_rule"] = str(next_run_rule)[:220]
        hit["hit_count"] = int(hit.get("hit_count") or 1) + 1
        if resolved:
            hit["resolved_count"] = int(hit.get("resolved_count") or 0) + 1
        hit["last_ts"] = now_s
        lessons = [x for x in lessons if x is not hit] + [hit]
    else:
        lessons.append({
            "key": key[:64],
            "err_code": str(err_code or "")[:24],
            "stage": str(stage or "")[:32],
            "problem_summary": str(problem_summary or "")[:200],
            "how_resolved": str(how_resolved or "")[:220],
            "next_run_rule": str(next_run_rule or "")[:220],
            "hit_count": 1,
            "resolved_count": 1 if resolved else 0,
            "last_ts": now_s,
        })
    pb["problem_lessons"] = lessons[-40:]


def finalize_run_training_summary(cfg, status, mode="full", round_label="", duration_sec=0, error_detail=None):
    """每次运行结束/异常后自动复盘本次遭遇的问题与解法，持续优化下轮默认策略并推送训练总结。"""
    trace = get_run_training_trace()
    pb = load_ai_run_playbook()
    c = normalize_config(cfg if isinstance(cfg, dict) else {})
    st_s = str(status or "ok")
    now_s = time.strftime("%Y-%m-%d %H:%M:%S")

    for fa in trace.get("focus_attempts") or []:
        s_name = str(fa.get("strategy") or "")
        if s_name:
            rec = pb["focus_stats"].setdefault(s_name, {"ok": 0, "fail": 0})
            rec["ok" if fa.get("ok") else "fail"] += 1
    for pa in trace.get("paste_attempts") or []:
        m_name = str(pa.get("method") or "")
        if m_name:
            rec = pb["paste_stats"].setdefault(m_name, {"ok": 0, "fail": 0})
            rec["ok" if pa.get("ok") else "fail"] += 1
    for sa in trace.get("silent_attempts") or []:
        m_name = str(sa.get("method") or "")
        if m_name:
            rec = pb["silent_stats"].setdefault(m_name, {"ok": 0, "fail": 0})
            rec["ok" if sa.get("ok") else "fail"] += 1
    for wa in trace.get("wps_capture_attempts") or []:
        m_name = str(wa.get("method") or "")
        if m_name:
            rec = pb["wps_capture_stats"].setdefault(m_name, {"ok": 0, "fail": 0})
            rec["ok" if wa.get("ok") else "fail"] += 1

    pb["total_runs"] += 1
    win_focus = next((x.get("strategy") for x in reversed(trace.get("focus_attempts") or []) if x.get("ok")), "")
    win_paste = next((x.get("method") for x in reversed(trace.get("paste_attempts") or []) if x.get("ok")), "")
    focus_fails = [x for x in (trace.get("focus_attempts") or []) if not x.get("ok")]
    paste_fails = [x for x in (trace.get("paste_attempts") or []) if not x.get("ok")]
    heals = trace.get("heal_events") or []
    user_recs = trace.get("user_recoveries") or []
    auto_confs = trace.get("auto_confirms") or []

    try:
        cur_delay = max(1, min(20, int(round(float(c.get("dt_paste_delay_sec", 3) or 3)))))
    except Exception:
        cur_delay = 3

    had_timing_paste_fail = any(
        any(k in str(pf.get("reason") or "") for k in ("未变化", "加载", "focus", "焦点"))
        for pf in paste_fails
    )
    if had_timing_paste_fail and st_s == "ok":
        prev_rec = pb.get("recommended_paste_delay_sec") or cur_delay
        pb["recommended_paste_delay_sec"] = min(6, max(cur_delay, int(prev_rec) + 1))

    lesson_msg = ""
    opt_msg = ""
    if st_s == "ok":
        pb["success_runs"] += 1
        pb["streak_ok"] += 1
        prev_unres = pb.get("last_unresolved_issue")
        if isinstance(prev_unres, dict) and (prev_unres.get("problem_summary") or prev_unres.get("err_code")):
            pb["healed_runs"] += 1
            res_parts = []
            if user_recs:
                res_parts.append(f"人工/预设恢复选择了「{'、'.join(x.get('choice', '') for x in user_recs)}」")
            if win_focus or win_paste:
                res_parts.append(
                    f"切换为「{_FOCUS_STRATEGY_CN.get(win_focus, win_focus or '智能定位')} + {_PASTE_METHOD_CN.get(win_paste, win_paste or 'Win32粘贴')}」后一次通过"
                )
            how_txt = "；".join(res_parts) or "重新激活钉钉并核验光标落入待发送框后顺利发出"
            rule_txt = f"下轮起优先启用「{_FOCUS_STRATEGY_CN.get(win_focus, win_focus or '安全区聚焦')}」与「{_PASTE_METHOD_CN.get(win_paste, win_paste or 'Ctrl+V')}」，并将加载等待保障在 {pb.get('recommended_paste_delay_sec') or cur_delay}s"
            _upsert_playbook_lesson(
                pb,
                prev_unres.get("err_code") or "PREV-FIXED",
                prev_unres.get("stage") or "dingtalk_paste",
                prev_unres.get("problem_summary") or "上轮运行受阻",
                how_txt,
                rule_txt,
                resolved=True,
            )
            pb["last_unresolved_issue"] = None
            lesson_msg = f"复盘上轮难题「{prev_unres.get('problem_summary')}」→ 解法：{how_txt}"
            opt_msg = rule_txt
        elif focus_fails or paste_fails or heals or user_recs or auto_confs:
            pb["healed_runs"] += 1
            probs = []
            if focus_fails:
                probs.append(f"原聚焦方式（{_FOCUS_STRATEGY_CN.get(focus_fails[0].get('strategy'), focus_fails[0].get('strategy'))}）未稳")
            if paste_fails:
                probs.append(f"首轮粘贴（{_PASTE_METHOD_CN.get(paste_fails[0].get('method'), paste_fails[0].get('method'))}）未触发画面变化")
            if heals:
                probs.append(str(heals[-1].get("detail") or "中途触发AI自动调试"))
            prob_txt = "；".join(probs) or "发送过程出现轻微卡阻"
            how_txt = f"自动确认/自愈切至「{_FOCUS_STRATEGY_CN.get(win_focus, win_focus or '备选聚焦')} + {_PASTE_METHOD_CN.get(win_paste, win_paste or '备选粘贴')}」后成功发送"
            rule_txt = f"已提升胜出组合权重，下轮直接首选「{_FOCUS_STRATEGY_CN.get(win_focus, win_focus or '稳定区')} + {_PASTE_METHOD_CN.get(win_paste, win_paste or 'Win32粘贴')}」"
            _upsert_playbook_lesson(pb, "SELF-HEALED", "dingtalk_paste", prob_txt, how_txt, rule_txt, resolved=True)
            lesson_msg = f"本轮卡点「{prob_txt}」→ 自动解法：{how_txt}"
            opt_msg = rule_txt
        else:
            lesson_msg = "本轮全程零卡顿一次顺滑通过（已累计连续稳定 {0} 轮）".format(pb["streak_ok"])
            opt_msg = f"继续保持当前最优组合（{_FOCUS_STRATEGY_CN.get(win_focus, '精确聚焦')} + {_PASTE_METHOD_CN.get(win_paste, 'Win32扫码粘贴')}）"
    else:
        pb["failed_runs"] += 1
        pb["streak_ok"] = 0
        ed = error_detail if isinstance(error_detail, dict) else {}
        ecode = str(ed.get("code") or "ERR-RUN-900")
        eraw = str(ed.get("plain_reason") or ed.get("raw_error") or ed.get("title") or "执行中断")[:160]
        next_rule = "下轮自动按训练权重切换下一组聚焦区域与剪贴板粘贴手法，并开启自动确定重试"
        if "304" in ecode or "粘贴" in eraw or "变化" in eraw or "光标" in eraw:
            pb["recommended_paste_delay_sec"] = min(6, max(cur_delay + 1, int(pb.get("recommended_paste_delay_sec") or 4)))
            next_rule = f"下轮自动切换备用聚焦位置+右键菜单粘贴兜底，并将等待提高至 {pb['recommended_paste_delay_sec']}s"
        elif "305" in ecode or "最小化" in eraw or "切换" in eraw:
            next_rule = "下轮遇到前台短暂切走时先自动重新置顶钉钉主窗，再完成粘贴发送"
        pb["last_unresolved_issue"] = {
            "ts": now_s,
            "err_code": ecode,
            "stage": "dingtalk_paste" if "304" in ecode or "305" in ecode else "runtime",
            "problem_summary": f"[{ecode}] {eraw}",
            "actions_tried": [str(x.get("strategy") or "") for x in focus_fails[:3]] + [str(x.get("method") or "") for x in paste_fails[:3]],
        }
        _upsert_playbook_lesson(
            pb,
            ecode,
            pb["last_unresolved_issue"]["stage"],
            f"[{ecode}] {eraw}",
            "本轮未彻底恢复，待下轮或人工干预后自动记录最终解法",
            next_rule,
            resolved=False,
        )
        lesson_msg = f"本轮阻断问题：[{ecode}] {eraw}"
        opt_msg = next_rule

    summary_line = f"{lesson_msg}；下轮优化：{opt_msg}"
    pb["recent_trainings"].append({
        "ts": now_s,
        "mode": str(mode or "full"),
        "status": st_s,
        "duration_sec": int(duration_sec or 0),
        "win_focus": win_focus,
        "win_paste": win_paste,
        "summary_line": summary_line[:240],
    })
    pb["recent_trainings"] = pb["recent_trainings"][-20:]
    saved_pb = save_ai_run_playbook(pb)

    win_rate_pct = int(round(100.0 * saved_pb["success_runs"] / max(1, saved_pb["total_runs"])))
    chat_reply = (
        f"🧠 [第 {saved_pb['total_runs']} 轮运行训练总结 · 成功率 {win_rate_pct}%（{saved_pb['success_runs']}/{saved_pb['total_runs']}）]\n"
        f"• 执行结果：{'✅ 顺利完成' if st_s == 'ok' else '⚠️ 本轮遇阻'}（用时 {int(duration_sec or 0)}s）\n"
        f"• 总结问题：{lesson_msg}\n"
        f"• 自动优化：{opt_msg}"
    )
    out_obj = {
        "reply": chat_reply,
        "time": time.strftime("%H:%M:%S"),
        "summary_line": summary_line[:160],
        "status": st_s,
        "total_runs": saved_pb["total_runs"],
        "success_runs": saved_pb["success_runs"],
    }
    log(f"  🧠 [AI 训练总结] {summary_line}")
    _notify_training_summary(out_obj)
    return out_obj


def append_ai_chat_turn(role, text, time_str="", source="", shot_info="", via="pc", sender="", on_demand_meta=None, thinking_steps=None, elapsed_sec=None):
    """追加一条 AI 对话轮次到持久化档案（最多保留最近 30 条）。"""
    txt = str(text or "").strip()
    if not txt:
        return []
    with _AI_MEM_LOCK:
        mem = load_ai_user_memory()
        item = {
            "role": "model" if role == "model" else "user",
            "text": txt[:2400],
            "time": str(time_str or time.strftime("%H:%M:%S")),
            "source": str(source or ""),
            "shot_info": str(shot_info or ""),
            "via": str(via or "pc"),
        }
        if sender:
            item["sender"] = str(sender)[:30]
        if isinstance(on_demand_meta, dict):
            item["on_demand_meta"] = on_demand_meta
        if isinstance(thinking_steps, list) and thinking_steps:
            clean_steps = [str(x).strip()[:120] for x in thinking_steps[:6] if str(x).strip()]
            if clean_steps:
                item["thinking_steps"] = clean_steps
        if elapsed_sec is not None:
            try:
                item["elapsed_sec"] = round(max(0.0, float(elapsed_sec)), 1)
            except (TypeError, ValueError):
                pass
        mem["chat_history"].append(item)
        mem["chat_history"] = mem["chat_history"][-30:]
        saved = save_ai_user_memory(mem, notify=False)
    _notify_ai_memory_changed("memory")
    return list(saved["chat_history"])


def clear_ai_chat_turns_only():
    """清空对话框气泡记录，但完整保留已经记住的本机个人习惯、实名与纠错映射。"""
    with _AI_MEM_LOCK:
        mem = load_ai_user_memory()
        mem["chat_history"] = []
        save_ai_user_memory(mem)


def _format_day_offset_cn(off):
    if off is None:
        return "跟随当前配置"
    n = int(off)
    if n == 0:
        return "当天(0天)"
    if n == -1:
        return "昨天(-1天)"
    if n == -2:
        return "前天(-2天)"
    return f"{n:+d}天"


def learn_user_habits_from_interaction(user_text="", speaker_name="", is_local_speaker=True, resolved_targets=None, action=None):
    """从用户的对话纠错、偏好表达和已确认投递结果中持续学习个人习惯并持久化。"""
    raw = str(user_text or "").strip()
    if raw and check_ai_security_and_creator_guardrail(raw):
        return {"learned": [], "memory": load_ai_user_memory()}

    learned = []
    with _AI_MEM_LOCK:
        mem = load_ai_user_memory()
        changed = False

        if raw:
            # 1) 识别并绑定本机实名：“我是XX / 我叫XX / 我的钉钉名是XX / 以后叫我XX”
            m_self = re.search(
                r"(?:^|[，。,.;；!\s])(?:记住[：:]?\s*)?(?:我是|我叫|我本人是|我本人叫|我的真名是?|我的真实姓名是?|我的钉钉名(?:字)?是?|我钉钉叫|以后叫我)\s*[「\"'『]?([\u4e00-\u9fffA-Za-z0-9·]{2,12})",
                raw,
            )
            if is_local_speaker and m_self:
                cand_self = re.sub(r"(?:啊|呀|哦|哈|吧|的)$", "", m_self.group(1).strip())
                bad_starts = ("谁", "什么", "怎么", "哪个", "想", "要", "来", "看", "问", "让", "发", "测试", "管理员")
                if (
                    cand_self
                    and not cand_self.startswith(bad_starts)
                    and not any(k in cand_self for k in ("数据", "昨天", "今天", "报表", "表格", "群里", "文件助手"))
                    and is_likely_real_person_name(cand_self)
                ):
                    if mem.get("self_real_name") != cand_self:
                        mem["self_real_name"] = cand_self
                        learned.append(f"记住您的本人姓名是「{cand_self}」")
                        changed = True

            # 2) 识别人名/群名纠错：“不是X是Y / 没有X，是Y / X不对，应该是Y / 把X改成Y”
            corr_pairs = []
            for pm in re.finditer(
                r"(?:不是|没有|别找|不要找|不是找)\s*[「\"'『]?([^\s，。,.;；!\n？?」\"'』]{2,12})[」\"'』]?\s*[，,、]?\s*(?:应该是|应是|而是|其实是|实际是|真名是|改成|换成|叫|是)\s*[「\"'『]?([^\s，。,.;；!\n？?」\"'』]{2,12})[」\"'』]?",
                raw,
            ):
                corr_pairs.append((pm.group(1), pm.group(2)))
            for pm in re.finditer(
                r"([\u4e00-\u9fffA-Za-z0-9]{2,10})\s*(?:不对|错了|写错了|打错了|不是这个人)[，,.\s]*(?:应该是|应是|而是|其实是|实际是|改成|换成|真名是|叫|是)\s*([\u4e00-\u9fffA-Za-z0-9]{2,10})",
                raw,
            ):
                corr_pairs.append((pm.group(1), pm.group(2)))
            for pm in re.finditer(
                r"把\s*[「\"'『]?([\u4e00-\u9fffA-Za-z0-9]{2,10})[」\"'』]?\s*(?:改成|换成|纠正为|记成)\s*[「\"'『]?([\u4e00-\u9fffA-Za-z0-9]{2,10})[」\"'』]?",
                raw,
            ):
                corr_pairs.append((pm.group(1), pm.group(2)))

            for raw_w, raw_r in corr_pairs:
                w_n = re.sub(r"(?:的|吧|啊|哦)$", "", str(raw_w or "").strip())
                r_n = re.sub(r"(?:的|吧|啊|哦)$", "", str(raw_r or "").strip())
                if (
                    len(w_n) >= 2 and len(r_n) >= 2 and w_n != r_n
                    and w_n not in _DT_GENERIC_ALIAS_WORDS and r_n not in _DT_GENERIC_ALIAS_WORDS
                    and not _is_chat_sentence_noise(w_n) and not _is_chat_sentence_noise(r_n)
                    and not any(k in w_n for k in ("发送", "表格", "数据", "群里"))
                ):
                    if mem["name_corrections"].get(w_n) != r_n:
                        mem["name_corrections"][w_n] = r_n
                        _save_verified_dingtalk_target(w_n, r_n)
                        for list_k in ("preferred_targets", "last_confirmed_targets"):
                            mem[list_k] = [r_n if x == w_n else x for x in (mem.get(list_k) or [])]
                        learned.append(f"永久纠错：「{w_n}」->「{r_n}」")
                        changed = True

            # 3) 禁发群 / 恢复发群偏好
            if any(k in raw for k in ("以后不要发群", "以后别发群", "默认不发群", "永远别发群", "别再发群", "不要再发群", "记住不发群", "不要发到群", "别发到群")):
                if not mem.get("forbid_group_send"):
                    mem["forbid_group_send"] = True
                    learned.append("记住您的偏好：默认不自动发大群")
                    changed = True
            elif any(k in raw for k in ("恢复发群", "以后发群", "默认发群", "取消不发群")):
                if mem.get("forbid_group_send"):
                    mem["forbid_group_send"] = False
                    learned.append("已恢复允许默认发群")
                    changed = True

            # 4) 默认投递目标：“以后默认发给XX / 每次都发给XX / 习惯发给我”
            for m_pref in re.finditer(
                r"(?:以后|每次都?|默认|平时|习惯|记住)\s*(?:默认|直接|都|只)?\s*(?:只)?(?:发给|发送到|发到|投递给|私发给?|推给)\s*([^，。,.;；!\n？?]{1,24})",
                raw,
            ):
                prev_ctx = raw[max(0, m_pref.start() - 4):m_pref.start() + 6]
                if any(neg in prev_ctx for neg in ("不要发", "别发", "不发", "禁止发")):
                    continue
                seg = re.split(r"(?:\s+|并|然后|同时|就行|就好|不要|不用|别)", m_pref.group(1).strip())[0].strip()
                pref_list = []
                for part in re.split(r"[、,，和与及]+", seg):
                    p = _clean_spoken_target_phrase(part)
                    if not p:
                        continue
                    if p in ("我", "我自己", "本人", "我本人", "自己", "我的钉钉"):
                        resolved_me = mem.get("self_real_name") or (speaker_name if (is_local_speaker and is_likely_real_person_name(speaker_name)) else "")
                        p_val = resolved_me or "我"
                    else:
                        p_val = mem["name_corrections"].get(p, p)
                    if p_val and p_val not in pref_list and not _is_chat_sentence_noise(p_val):
                        pref_list.append(p_val)
                if pref_list and mem.get("preferred_targets") != pref_list:
                    mem["preferred_targets"] = pref_list
                    learned.append(f"记住您的默认发送对象：「{'、'.join(pref_list)}」")
                    changed = True

            # 5) 默认日期偏移与静音偏好
            if any(k in raw for k in ("以后默认看昨天", "以后默认发昨天", "以后默认跑昨天", "以后都发昨天", "以后都看昨天", "习惯看昨天", "习惯发昨天", "习惯跑昨天", "每次都跑昨天", "每次都看昨天", "默认看昨天", "默认发昨天", "默认跑昨天")):
                if mem.get("preferred_day_offset") != -1:
                    mem["preferred_day_offset"] = -1
                    learned.append("记住您的日期偏好：默认看昨天(-1天)")
                    changed = True
            elif any(k in raw for k in ("以后默认看前天", "以后默认发前天", "以后默认跑前天", "习惯跑前天", "默认跑前天")):
                if mem.get("preferred_day_offset") != -2:
                    mem["preferred_day_offset"] = -2
                    learned.append("记住您的日期偏好：默认看前天(-2天)")
                    changed = True
            elif any(k in raw for k in ("以后默认看今天", "以后默认发今天", "以后默认跑今天", "以后都看今天", "习惯看今天", "习惯跑今天", "每次都跑今天", "默认看今天", "默认发今天", "默认跑今天")):
                if mem.get("preferred_day_offset") != 0:
                    mem["preferred_day_offset"] = 0
                    learned.append("记住您的日期偏好：默认看今天(0天)")
                    changed = True

            if any(k in raw for k in ("以后默认静音", "以后都静音", "习惯静音", "每次都静音", "默认都静音", "记住静音发送")):
                if mem.get("preferred_silent") is not True:
                    mem["preferred_silent"] = True
                    learned.append("记住您的发送偏好：默认静音发送")
                    changed = True
            elif any(k in raw for k in ("以后取消静音", "以后别静音", "以后不静音", "习惯不静音", "默认不静音")):
                if mem.get("preferred_silent") is not False:
                    mem["preferred_silent"] = False
                    learned.append("记住您的发送偏好：常规提醒(不静音)")
                    changed = True

            # 6) 个人风格与注意事项笔记
            if (
                any(k in raw for k in ("以后", "记住", "我习惯", "我喜欢", "每次都", "默认都", "不要再", "别再", "请记得", "偏好"))
                and not raw.endswith(("吗", "吗？", "吗?", "？", "?"))
                and not any(k in raw for k in ("每周", "每天", "工作日", "定时计划"))
            ):
                note_clean = re.sub(r"^(?:请)?记住[：:，,\s]*", "", raw).strip()[:80]
                if len(note_clean) >= 4 and note_clean not in mem["habit_notes"]:
                    mem["habit_notes"].append(note_clean)
                    mem["habit_notes"] = mem["habit_notes"][-30:]
                    if not learned:
                        learned.append(f"记住您的个人偏好：「{note_clean}」")
                    changed = True

        # 7) 记录最近一次已确认目标与动作，用于跨重启“续跑”
        if isinstance(resolved_targets, list) and resolved_targets:
            clean_rt = [str(x).strip() for x in resolved_targets if str(x).strip() and not _is_chat_sentence_noise(str(x).strip())]
            if clean_rt and (mem.get("last_confirmed_targets") != clean_rt or (action and mem.get("last_action") != action)):
                mem["last_confirmed_targets"] = clean_rt[:8]
                if action in ("run_full", "capture_send", "text_send"):
                    mem["last_action"] = action
                changed = True

        if changed:
            mem = save_ai_user_memory(mem, notify=False)
    if changed:
        _notify_ai_memory_changed("memory")
    return {"learned": learned, "memory": mem}


def build_ai_user_memory_context():
    """生成精炼的本机用户个人习惯与纠错记忆摘要，供每次对话与总结参考。"""
    mem = load_ai_user_memory()
    lines = []
    if mem.get("self_real_name"):
        lines.append(f"- 本机主人姓名（当他说“我/发给我”时优先对应）：{mem['self_real_name']}")
    if mem.get("preferred_targets"):
        lines.append(f"- 默认首选投递对象：{'、'.join(mem['preferred_targets'])}")
    if mem.get("last_confirmed_targets"):
        lines.append(f"- 上次已确认发送目标：{'、'.join(mem['last_confirmed_targets'])}")
    if mem.get("preferred_day_offset") is not None:
        lines.append(f"- 常用数据日期偏好：{_format_day_offset_cn(mem['preferred_day_offset'])}")
    if mem.get("preferred_silent") is not None:
        lines.append(f"- 常用静音习惯：{'静音发送' if mem['preferred_silent'] else '常规提醒(不静音)'}")
    if mem.get("forbid_group_send"):
        lines.append("- 重要禁忌：用户交代过平时不要随便发到大群")
    if mem.get("name_corrections"):
        pairs = [f"{k}->{v}" for k, v in list(mem["name_corrections"].items())[-12:]]
        lines.append(f"- 人名纠错映射（必须遵守）：{'；'.join(pairs)}")
    if mem.get("habit_notes"):
        notes = mem["habit_notes"][-8:]
        lines.append("- 用户历史嘱咐与习惯：\n  • " + "\n  • ".join(notes))
    try:
        pb = load_ai_run_playbook()
        if int(pb.get("total_runs") or 0) > 0:
            lines.append(
                f"- 运行训练战绩：累计 {pb['total_runs']} 轮（成功 {pb['success_runs']} / 自愈恢复 {pb['healed_runs']} / 连续顺畅 {pb['streak_ok']}）"
            )
        lessons = pb.get("problem_lessons") or []
        if lessons:
            ls_txts = [
                f"问题「{x.get('problem_summary')}」→ 解法「{x.get('how_resolved')}」（下轮规则：{x.get('next_run_rule')}）"
                for x in lessons[-4:]
            ]
            lines.append("- 历次跑表排障总结与自优化规则：\n  • " + "\n  • ".join(ls_txts))
    except Exception:
        pass
    return "\n".join(lines)


def try_handle_ai_memory_command(user_text, cfg=None, speaker_name="", is_local_speaker=True):
    """处理用户明确询问/教导/重置个人习惯记忆的对话请求。"""
    raw = str(user_text or "").strip()
    if not raw or check_ai_security_and_creator_guardrail(raw):
        return {"handled": False}

    if any(k in raw for k in ("清除习惯记忆", "清空习惯记忆", "忘掉我的习惯", "重置习惯记忆", "忘记我的偏好")):
        mem = _default_ai_user_memory()
        save_ai_user_memory(mem)
        return {
            "handled": True,
            "source": "habit_memory",
            "shot_info": "个人习惯记忆已重置",
            "reply": "好的，已经清空本机保存的个人习惯与人名纠错记忆。接下来您随时告诉我新偏好，我会重新记住！",
        }

    if any(k in raw for k in ("查看记忆", "查看习惯", "我的习惯记忆", "你记住了我什么", "你记住我什么", "我的个人习惯", "记住什么了")):
        ctx = build_ai_user_memory_context()
        mem = load_ai_user_memory()
        cnt_chat = len(mem.get("chat_history") or [])
        if not ctx:
            return {
                "handled": True,
                "source": "habit_memory",
                "shot_info": "暂无个人习惯记录",
                "reply": (
                    f"这台电脑目前保存了最近 **{cnt_chat}** 条聊天记录，还没有额外记录您的专属习惯。\n\n"
                    "您可以直接对我说：\n"
                    "• `我叫张xx，以后默认发给我`\n"
                    "• `以后默认看昨天数据，不要发群`\n"
                    "• `不是卢慧珍，是娄慧珍`\n"
                    "我会永久记在这台电脑上，重启软件也不会忘记！"
                ),
            }
        return {
            "handled": True,
            "source": "habit_memory",
            "shot_info": "已加载本机长期记忆",
            "reply": f"我在当前这台电脑为您记住了以下专属习惯（共留存 {cnt_chat} 条近期对话）：\n\n{ctx}\n\n（无论软件重启多少次我都会遵守，您随时说「清空习惯记忆」或直接教我新习惯即可更新）",
        }

    if any(k in raw for k in ("每周", "每天", "工作日", "定时计划", "帮我设置", "帮我改", "为什么", "怎么设置")):
        return {"handled": False}

    res = learn_user_habits_from_interaction(raw, speaker_name=speaker_name, is_local_speaker=is_local_speaker)
    if res.get("learned"):
        ctx = build_ai_user_memory_context()
        items_txt = "\n".join(f"• {x}" for x in res["learned"])
        return {
            "handled": True,
            "source": "habit_memory",
            "shot_info": "已永久记住您的习惯",
            "reply": f"✅ 收到，我已经牢牢记在当前电脑上啦（下次重启也会继续按这个习惯来）：\n{items_txt}\n\n**当前已生效记忆总览**：\n{ctx}",
        }
    return {"handled": False}


def _local_smart_chat_fallback(cfg_norm, user_prompt, arch_ctx, api_err="", hist_diff="", ag_prompt=""):
    """离线本地全能管家兜底：回答署名/身份/如何设单元格/当前状态/安全边界与历史成败分析（绝不提及外部 AI 品牌）。"""
    guard = check_ai_security_and_creator_guardrail(user_prompt)
    if guard:
        return guard
    q = str(user_prompt or "").strip()
    low = q.lower()
    tail_note = "\n\n（当前已由内置本地分析引擎为您实时解答）" if api_err else ""
    mem_ctx = build_ai_user_memory_context()
    mem_tail = f"\n\n【我已记住您的本机习惯】\n{mem_ctx}" if mem_ctx else ""
    if any(k in low for k in ("训练", "总结问题", "归纳问题", "怎么解决", "优化方案", "每次运行", "复盘")):
        pb = load_ai_run_playbook()
        lessons = pb.get("problem_lessons") or []
        strat = get_dt_runtime_strategy_from_playbook(cfg_norm)
        if lessons:
            ls_lines = "\n".join(
                f"• **问题**：{x.get('problem_summary')}\n  **解决**：{x.get('how_resolved')}\n  **优化**：{x.get('next_run_rule')}"
                for x in reversed(lessons[-5:])
            )
        else:
            ls_lines = "• 近期尚无异常阻塞记录，每次运行都会自动积累聚焦与粘贴胜率。"
        return (
            f"目前已累计执行 **{pb.get('total_runs', 0)} 轮训练**（成功 {pb.get('success_runs', 0)} / 自愈修复 {pb.get('healed_runs', 0)}）：\n\n"
            f"【当前自动优化最优策略】\n{strat.get('summary')}\n\n"
            f"【已归纳的历史问题与解决方法】\n{ls_lines}"
            + mem_tail
            + tail_note
        )
    if any(k in low for k in ("报错", "失败", "为什么", "第一次", "第二次", "怎么改", "代码", "antigravity", "顺序")):
        return (
            "已为您调取历次运行成败日志并整理出原因与修复参考建议：\n\n"
            f"【历次成败对比】\n{hist_diff or '暂未检索到历史对比'}\n\n"
            f"【开发协作参考说明】\n{ag_prompt or ''}"
            + tail_note
        )
    if any(k in q for k in ("你是谁", "你是啥", "ai是啥", "干嘛的", "做什么", "介绍")):
        return (
            "我是「暗号表助手 (SheetMate)」专属内置的智能小猫伙伴！\n"
            "• 一句话自动排期：直接跟我说“哪天几点把什么数据发到哪个群”，我能直接帮您设置并回传核对结果；\n"
            "• 每轮数据变化播报：每次表格处理完我都会帮您比比总数据从多少变成了多少、哪位伙伴或团队有变化；\n"
            "• 本机永久习惯记忆：我会记住您在这台电脑教过我的人名纠错、常用收信人与日期偏好，重启不丢失。"
            + mem_tail
            + tail_note
        )
    if any(k in q for k in ("单元格", "格位", "写入", "公式", "自己定义", "自定义")):
        return (
            "您可以在顶部切换到【钉钉投递】页面，在右列找到「格位校准（名称框定位）」：\n"
            "1. 默认已配置数据汇总常用 10 项（D3 查询日期、D4:E4 范围、D5 整周周报清空、L3 慕杰组、Y3/Y4/Y5/AA2/AA5 及 R4 最后更新时间）；\n"
            "2. 每个格位都能自由填写工作表名称（默认「数据汇总」，也能写其他页签）、单元格位置与用途备注；\n"
            "3. 写入方式支持 5 种：自动日期（含凌晨跨天减天）、当前时间、自定义内容、公式（=开头）以及清空。"
            + tail_note
        )
    if any(k in low for k in ("状态", "配置", "设置", "发几个群", "静音")):
        snap = _safe_ai_cfg_snapshot(cfg_norm)
        brief = get_dashboard_data_brief()
        ut_cell = (snap.get("update_time_cell") or {}).get("cell") or "R4"
        return (
            f"当前暗号表助手 v{APP_VERSION} 运行状态速览：\n"
            f"• 自动截图：{'开启' if snap.get('enable_capture') else '关闭'}｜自动发钉钉：{'开启' if snap.get('enable_dingtalk') else '关闭'}（{'静音' if snap.get('dt_silent_send') else '常规'} · {'自动发送' if snap.get('dt_auto_send') else '贴图待发'}）\n"
            f"• 目标会话：{snap.get('dt_targets_count')} 个｜预设校准格位：{len(snap.get('summary_presets_cells') or [])} 项｜{ut_cell} 更新时间：{'开启' if (snap.get('update_time_cell') or {}).get('enabled') else '关闭'}\n"
            f"• 历史数据档案：{brief.get('archive_count')} 轮｜最新盘面：{brief.get('reg_val')}｜{brief.get('top_val')}"
            + mem_tail
            + tail_note
        )
    return (
        f"已检索本地数据档案为您整理要点：\n{arch_ctx[:900]}" + mem_tail + tail_note
    )


def collect_report_context_for_ai(include_prev_shot=False, extra_img_path=None, encode_images=True):
    """采集本地最新汇总表截图（及可选上轮对比图）和桌面最新明细 TSV 摘要供 AI 总结；仅查元数据时可跳过编码。"""
    cand_imgs = []
    if extra_img_path is not None and Path(extra_img_path).exists():
        cand_imgs.append(Path(extra_img_path))

    tmp_img = app_dir() / "_sheetmate_tmp.png"
    if tmp_img.exists():
        cand_imgs.append(tmp_img)

    hist_dir = app_dir() / "history_shots"
    if hist_dir.exists():
        hist_pngs = sorted(
            [p for p in hist_dir.glob("*.png") if p.is_file()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        cand_imgs.extend(hist_pngs)

    desk_shot = Path.home() / "Desktop" / "数据日报" / "数据汇总.png"
    if desk_shot.exists():
        cand_imgs.append(desk_shot)

    # 去重并保持时间最新顺序
    picked_imgs = []
    seen_keys = set()
    for p in cand_imgs:
        try:
            rp = str(p.resolve())
            st = p.stat()
            if st.st_size < 1024:
                continue
            key = (rp, int(st.st_mtime))
            if key not in seen_keys:
                seen_keys.add(key)
                picked_imgs.append((p, st.st_mtime))
        except Exception:
            pass
    picked_imgs.sort(key=lambda it: it[1], reverse=True)
    # 若文件名同图在不同位置重复且尺寸mtime接近，仅在 history_shots 中保留最近 2 张不同图的对比
    images_out = []
    want_n = 2 if include_prev_shot else 1
    for p, mtime in picked_imgs[:want_n]:
        try:
            if encode_images:
                mime_t, b64 = _encode_image_for_ai(p, max_dim=2200)
            else:
                mime_t, b64 = "image/png", ""
            images_out.append({
                "name": p.name,
                "path": str(p),
                "mtime_str": time.strftime("%m-%d %H:%M", time.localtime(mtime)),
                "mime_type": mime_t,
                "b64": b64,
            })
        except Exception:
            pass

    tsv_blocks = []
    now_ts = time.time()
    tsv_dir = Path.home() / "Desktop" / "数据日报"
    if tsv_dir.exists():
        for nm in ("暗号数据", "注册数据", "短链数据"):
            tp = tsv_dir / f"{nm}.tsv"
            try:
                if tp.exists() and (now_ts - tp.stat().st_mtime) <= 18 * 3600:
                    head_lines, total_lines = _read_head_nonempty_lines(tp, max_lines=28, count_total=True)
                    if head_lines:
                        tsv_blocks.append(
                            f"【{nm} 明细表（更新于 {time.strftime('%m-%d %H:%M', time.localtime(tp.stat().st_mtime))}，共 {max(0, total_lines - 1)} 条记录）】\n"
                            + "\n".join(head_lines)
                        )
            except Exception:
                pass

    tsv_text = "\n\n".join(tsv_blocks)
    if len(tsv_text) > 3600:
        tsv_text = tsv_text[:3600] + "\n…（后续明细已省略，完整排名以汇总截图为准）"

    return {
        "has_data": bool(images_out or tsv_text),
        "images": images_out,
        "tsv_text": tsv_text,
        "latest_shot_label": f"{images_out[0]['name']} ({images_out[0]['mtime_str']})" if images_out else "",
        "prev_shot_label": f"{images_out[1]['name']} ({images_out[1]['mtime_str']})" if len(images_out) > 1 else "",
    }


def check_ai_security_and_creator_guardrail(user_text):
    """创作署名（Soren Drake）与权限/密码/隐藏后台/隐私绝不泄密安全护栏。"""
    txt = str(user_text or "").strip()
    if not txt:
        return None
    low = txt.lower()

    sensitive_kws = (
        "密码", "口令", "通行证", "passcode", "password", "api key", "api_key", "apikey",
        "密钥", "秘钥", "token", "gist", "隐藏窗口", "隐藏后台", "隐藏入口", "隐蔽", "影藏",
        "超管", "超级管理员", "进后台", "进入后台", "连续点", "点5下", "点几下", "ctrl+shift+alt",
        "绕过权限", "破解权限", "修改权限", "解除权限", "解除限制", "修改别人权限",
        "权限管理", "权限控制", "权限设置", "他人权限", "别人权限", "谁有权限", "我的权限",
        "别人聊天", "别人的聊天", "他人聊天", "查看别人", "看别人", "他人配置",
        "隐私", "私密", "强制解锁", "secret", "admin_auth",
    )
    sensitive_perm_re = re.search(
        r"(修改|更改|调整|绕过|破解|解除|查看|读取|获取|开启|关闭|分配).{0,8}权限"
        r"|(后台|隐藏|超管|管理|账号|他人|别人).{0,6}(权限|密码|口令)",
        low,
    )
    is_sensitive = any(k in low for k in sensitive_kws) or bool(sensitive_perm_re)

    is_creator = bool(re.search(
        r"(谁.{0,6}(?:设计|开发|编写|研发|创建|创作|写|做)|"
        r"(?:作者|开发者|设计者|设计师|研发者|研发人|出品方|开发团队).{0,4}是谁|"
        r"(?:who\s+(?:made|designed|created|built)|creator|author|designer))",
        low,
    ))
    is_model_probe = bool(re.search(
        r"(什么模型|哪个大模型|用的什么\s*ai|你是哪个\s*ai|底层.{0,6}(?:模型|接口|ai)|"
        r"google|gemini|gpt|openai|deepseek|claude|通义|文心|豆包|kimi|"
        r"api.{0,4}地址|api.{0,4}接口)",
        low,
    ))

    if is_sensitive:
        prefix = "本软件「暗号表助手 (SheetMate)」由 **【Soren Drake】** 设计研发。\n\n" if is_creator else ""
        return (
            prefix
            + "抱歉，作为软件内置的智能小猫伙伴，我**没有任何读取、查看或访问软件密码、口令、权限配置、密钥或他人隐私数据的功能**。\n"
            "为了保障账号与团队数据安全，所有涉及密码、权限调整与隐私安全类的问题我都无法回答；如需协助请点击窗口右上角的【反馈】按钮提交说明。"
        )
    if is_model_probe:
        prefix = "「暗号表助手 (SheetMate)」由 **【Soren Drake】** 设计研发。\n\n" if is_creator else ""
        return (
            prefix
            + "我是「暗号表助手 (SheetMate)」专属内置的智能小猫伙伴。\n\n"
            "我常驻在软件内部专门负责陪伴沟通、自动查数跑表、识别联系人静默投递与异常自诊，"
            "不归属任何外部大模型品牌，也不对外暴露或读取任何底层接口信息。"
        )
    if is_creator:
        return (
            "「暗号表助手 (SheetMate)」是由 **【Soren Drake】** 设计研发的智能表格自动化与团队协同助手！\n\n"
            "我可以帮您完成数据抓取、WPS 自定义格位填写、钉钉自适应静默投递、历史数据分析，也可以用一句话帮您直接跑表发给指定同事。\n"
            "（温馨提示：本小猫伙伴仅专注数据分析与操作辅助，没有任何读取与访问后台密码或隐私数据的功能。）"
        )
    return None


def _verified_targets_file():
    return app_dir() / "dt_verified_targets.json"


_DT_GENERIC_ALIAS_WORDS = {
    "我", "我自己", "本人", "我本人", "自己", "我的", "我的钉钉",
    "发群", "发群里", "发到群", "发到群里", "群", "群里", "大群", "默认群",
    "大家", "所有人", "全员", "文件小助手", "同事", "好友",
}

_DT_CHAT_NOISE_PHRASES = (
    "就是我", "里的那个", "发给我", "跑一下", "不要", "不用",
    "怎么", "为什么", "收到", "好的", "哈哈哈", "聊天记录", "包含:", "条相关",
)


def _is_chat_sentence_noise(text):
    s = str(text or "").strip()
    if not s:
        return True
    if len(s) > 26:
        return True
    if any(p in s for p in _DT_CHAT_NOISE_PHRASES):
        return True
    if any(ch in s for ch in ("？", "?", "！", "!", "。", "，", "\n", "\r")):
        return True
    has_cjk = any("\u4e00" <= ch <= "\u9fff" for ch in s)
    if has_cjk and s.count(" ") >= 2 and not (len(s) <= 20 and s.endswith(("群", "组", "部", "中心", "团队", "公司", "助手"))):
        return True
    return False


def _is_valid_verified_dingtalk_target(alias_name, confirmed_dt_name):
    a = str(alias_name or "").strip()
    c = str(confirmed_dt_name or "").strip()
    if not a or not c or a == c:
        return False
    a_clean = re.sub(r"^[「\"'『\s]+|[」\"'』\s]+$", "", a)
    if a_clean in _DT_GENERIC_ALIAS_WORDS or len(a_clean) > 24:
        return False
    if _is_chat_sentence_noise(a_clean) or _is_chat_sentence_noise(c):
        return False
    return True


def _load_verified_dingtalk_targets():
    p = _verified_targets_file()
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                cleaned = {}
                removed_bad = False
                for k, v in d.items():
                    ks = str(k or "").strip()
                    vs = str(v or "").strip()
                    if _is_valid_verified_dingtalk_target(ks, vs):
                        cleaned[ks] = vs
                    else:
                        removed_bad = True
                if removed_bad:
                    try:
                        p.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8")
                    except Exception:
                        pass
                return cleaned
        except Exception:
            pass
    return {}


def _save_verified_dingtalk_target(alias_name, confirmed_dt_name):
    a = str(alias_name or "").strip()
    c = str(confirmed_dt_name or "").strip()
    if not _is_valid_verified_dingtalk_target(a, c):
        return
    try:
        with _AI_MEM_LOCK:
            cur = _load_verified_dingtalk_targets()
            cur[a] = c
            if len(cur) > 200:
                keys = list(cur.keys())[-200:]
                cur = {k: cur[k] for k in keys}
            _atomic_write_json(_verified_targets_file(), cur)
        _notify_ai_memory_changed("verified_targets")
    except Exception:
        pass


_DAILY_TSV_NAMES_CACHE = {"sig": "", "people": [], "groups": []}


def _load_names_from_daily_tsv(report_dir=None):
    """流式读取桌面数据日报 TSV 前 800 行全员真实姓名与组别，并按文件 mtime 缓存避免反复读盘。"""
    base = Path(report_dir) if report_dir else (Path.home() / "Desktop" / "数据日报")
    if not base.exists():
        return [], []
    sig_parts, files = [], []
    for nm in ("暗号数据.tsv", "注册数据.tsv", "短链数据.tsv"):
        p = base / nm
        if p.exists() and p.is_file():
            try:
                st = p.stat()
                sig_parts.append(f"{nm}:{int(st.st_mtime)}:{st.st_size}")
                files.append(p)
            except Exception:
                pass
    sig = "|".join(sig_parts)
    if sig and sig == _DAILY_TSV_NAMES_CACHE.get("sig"):
        return list(_DAILY_TSV_NAMES_CACHE["people"]), list(_DAILY_TSV_NAMES_CACHE["groups"])

    people, groups = [], []
    seen_p, seen_g = set(), set()
    skip_vals = {"合计", "总计", "汇总", "小计", "全员", "未知", "-", "—", "无", "空", "null", "none", "nan"}
    for p in files:
        try:
            lines = _read_head_nonempty_lines(p, max_lines=800)
            if len(lines) < 2:
                continue
            hdr = [h.strip() for h in lines[0].split("\t")]
            p_cols = [i for i, h in enumerate(hdr) if any(k in h for k in ("姓名", "员工", "归属", "负责", "人员", "成员", "用户名", "推广员"))]
            g_cols = [i for i, h in enumerate(hdr) if any(k in h for k in ("团队", "小组", "组别", "部门", "战队"))]
            for ln in lines[1:]:
                cols = ln.split("\t")
                for ci in p_cols:
                    if ci < len(cols):
                        val = cols[ci].strip()
                        if val and val.lower() not in skip_vals and 2 <= len(val) <= 12 and not val.isdigit() and val not in seen_p:
                            seen_p.add(val)
                            people.append(val)
                for ci in g_cols:
                    if ci < len(cols):
                        val = cols[ci].strip()
                        if val and val.lower() not in skip_vals and 2 <= len(val) <= 20 and not val.isdigit() and val not in seen_g:
                            seen_g.add(val)
                            groups.append(val)
        except Exception:
            pass
    if sig:
        _DAILY_TSV_NAMES_CACHE["sig"] = sig
        _DAILY_TSV_NAMES_CACHE["people"] = people
        _DAILY_TSV_NAMES_CACHE["groups"] = groups
    return people, groups


def collect_known_people_and_groups(cfg=None, extra_roster=None):
    """汇总花名册：配置群聊、协同成员唯一昵称/备注、数据档案及桌面数据日报全部真实员工与组别。"""
    norm = normalize_config(cfg if isinstance(cfg, dict) else {})
    people, groups = [], []
    seen_p, seen_g = set(), set()

    def _add_p(val):
        s = str(val or "").strip()
        if s and not s.startswith("未命名") and s not in seen_p:
            seen_p.add(s)
            people.append(s)

    def _add_g(val):
        s = str(val or "").strip()
        if s and s not in seen_g:
            seen_g.add(s)
            groups.append(s)

    for it in (norm.get("dt_targets") or []):
        if isinstance(it, dict):
            _add_g(it.get("name"))
    _add_g(norm.get("dt_search"))
    for cand in get_candidate_teams(norm):
        _add_g(cand)

    for mem in (extra_roster or []):
        if isinstance(mem, dict):
            _add_p(mem.get("nickname"))
            _add_p(mem.get("display_name"))
            _add_p(mem.get("remark"))
        elif isinstance(mem, str):
            _add_p(mem)

    try:
        arch = load_report_archive(limit=8)
        for rec in (arch or [])[-6:]:
            for p_it in (rec.get("personal") or []):
                _add_p(p_it.get("name"))
            for t_it in (rec.get("teams") or []):
                _add_g(t_it.get("team"))
    except Exception:
        pass

    try:
        tsv_p, tsv_g = _load_names_from_daily_tsv()
        for val in tsv_p:
            _add_p(val)
        for val in tsv_g:
            _add_g(val)
    except Exception:
        pass

    for _k, v in _load_verified_dingtalk_targets().items():
        _add_p(v)

    return {"people": people, "groups": groups, "all": people + [g for g in groups if g not in seen_p]}


_DEFAULT_RANDOM_NICKNAMES = (
    "数据捕手", "冲榜小火箭", "稳健操盘手", "早八满绩王", "战报指挥官",
    "丝滑传送门", "零失误选手", "金牌统计官", "高效摸鱼侠", "发群先锋官",
    "汇总小管家", "晴空逐浪者", "神速快捷键", "表格魔法师", "夜航守表人",
    "全能多面手", "多多猫头鹰", "奶糖小柴犬", "团团大熊猫", "蜜桃小橘猫",
    "草莓垂耳兔", "机灵小赤狐", "佛系卡皮巴拉", "冰川小企鹅",
)


def is_likely_real_person_name(name):
    """判断名称是否更像真实员工姓名（排除未命名编号、纯数字/符号及随机趣味昵称）。"""
    s = str(name or "").strip()
    if not s or len(s) < 2 or len(s) > 20:
        return False
    if s.startswith(("未命名", "成员-", "成员·", "成员 ")):
        return False
    if s.casefold() in {"me", "self", "admin", "root", "user", "test", "guest", "未设置", "我自己", "本人", "自己", "管理员", "超级管理员", "测试", "默认"}:
        return False
    alnum = "".join(ch for ch in s if ch.isalnum())
    if not alnum or alnum.isdigit():
        return False
    nick_pool = set(_DEFAULT_RANDOM_NICKNAMES)
    try:
        import team_hub  # type: ignore[import-not-found]
        for x in getattr(team_hub, "RANDOM_NICKNAMES", ()):
            if x:
                nick_pool.add(str(x).strip())
        for av in getattr(team_hub, "PRESET_AVATARS", ()):
            if isinstance(av, dict) and av.get("name"):
                nick_pool.add(str(av["name"]).strip())
    except Exception:
        pass
    for rn in nick_pool:
        if s == rn or (s.startswith(rn) and s[len(rn):].isdigit()):
            return False
    if _is_chat_sentence_noise(s):
        return False
    return True


def detect_dingtalk_logged_in_user_name(use_cache=True):
    """非侵入式只读探测当前电脑已登录钉钉的本人姓名（严禁抢焦点，带 10 分钟缓存）。"""
    global _dt_logged_in_user_cache
    now_t = time.monotonic()
    if use_cache and (now_t - float(_dt_logged_in_user_cache.get("ts") or 0.0)) < 600.0:
        cached = str(_dt_logged_in_user_cache.get("name") or "")
        if cached:
            return cached
    detected = ""
    try:
        import uiautomation as ua  # type: ignore[import-not-found]
        wins = _enum_dingtalk_im_windows(include_hidden=False)
        if wins:
            hwnd = int(wins[0][0])
            ctl = _safe_control_from_handle(ua, hwnd)
            if ctl is not None:
                wr = ctl.BoundingRectangle
                if wr.right > wr.left and wr.bottom > wr.top:
                    deadline = time.monotonic() + 0.55
                    visited = [0]
                    ui_skip = {"钉钉", "DingTalk", "消息", "文档", "工作台", "通讯录", "日历", "待办", "搜索", "头像", "设置", "更多", "状态", "关闭", "最小化", "最大化", "还原", "协作", "应用中心", "云盘"}

                    def _scan_nav(node, depth):
                        nonlocal detected
                        if detected or depth > 7 or visited[0] >= 55 or time.monotonic() > deadline:
                            return
                        for ch in node.GetChildren():
                            visited[0] += 1
                            if visited[0] > 55 or time.monotonic() > deadline:
                                return
                            try:
                                rr = ch.BoundingRectangle
                                if rr.right > rr.left and rr.bottom > rr.top:
                                    if rr.left <= wr.left + 140 and rr.top <= wr.top + 220:
                                        nm = (ch.Name or "").strip()
                                        if nm:
                                            if nm.endswith("的头像") and len(nm) > 3:
                                                cand = nm[:-3].strip()
                                                if cand not in ui_skip and is_likely_real_person_name(cand):
                                                    detected = cand
                                                    return
                                            elif nm not in ui_skip and 2 <= len(nm) <= 12 and is_likely_real_person_name(nm):
                                                ct_n = ch.ControlTypeName
                                                aid_n = (ch.AutomationId or "").lower()
                                                if ct_n in ("ButtonControl", "ImageControl", "TextControl") and (
                                                    any(k in aid_n for k in ("avatar", "user", "profile", "head", "self"))
                                                    or rr.top <= wr.top + 115
                                                ):
                                                    detected = nm
                                                    return
                            except Exception:
                                pass
                            _scan_nav(ch, depth + 1)

                    _scan_nav(ctl, 0)
    except Exception:
        detected = ""
    if detected:
        _dt_logged_in_user_cache = {"name": detected, "ts": now_t}
    return detected


def _gb2312_initial(ch):
    """零依赖获取常用汉字拼音首字母（基于 GBK/GB2312 一级字拼音区间）。"""
    if "a" <= ch <= "z" or "A" <= ch <= "Z":
        return ch.lower()
    if "0" <= ch <= "9":
        return ch
    try:
        b = ch.encode("gbk")
    except Exception:
        return ch
    if len(b) < 2:
        return ch
    code = (b[0] << 8) + b[1]
    bounds = (
        (0xB0A1, 0xB0C4, "a"), (0xB0C5, 0xB2C0, "b"), (0xB2C1, 0xB4ED, "c"),
        (0xB4EE, 0xB6E9, "d"), (0xB6EA, 0xB7A1, "e"), (0xB7A2, 0xB8C0, "f"),
        (0xB8C1, 0xB9FD, "g"), (0xB9FE, 0xBBF6, "h"), (0xBBF7, 0xBFA5, "j"),
        (0xBFA6, 0xC0AB, "k"), (0xC0AC, 0xC2E7, "l"), (0xC2E8, 0xC4C2, "m"),
        (0xC4C3, 0xC5B5, "n"), (0xC5B6, 0xC5BD, "o"), (0xC5BE, 0xC6D9, "p"),
        (0xC6DA, 0xC8BA, "q"), (0xC8BB, 0xC8F5, "r"), (0xC8F6, 0xCBF9, "s"),
        (0xCBFA, 0xCDD9, "t"), (0xCDDA, 0xCEF3, "w"), (0xCEF4, 0xD1B8, "x"),
        (0xD1B9, 0xD4D0, "y"), (0xD4D1, 0xD7F9, "z"),
    )
    for lo, hi, ini in bounds:
        if lo <= code <= hi:
            return ini
    return ch


_HOMOPHONE_GROUPS = (
    "娄楼卢芦鲁陆", "慧会惠卉辉汇晖", "珍真贞臻振震甄", "刘留柳流六",
    "奕亦毅艺意怡依伊逸易益义宜", "慕木穆沐牧", "杰洁捷婕哲喆", "小晓筱肖萧笑",
    "聪匆琮从", "俊军君钧骏", "鑫欣馨新心辛", "磊雷蕾", "泉全权铨", "马码玛",
    "豫玉宇雨羽予御", "鄂俄额", "罗落洛骆", "东冬栋董", "张章彰", "王汪",
    "李理里礼黎丽利立莉力", "陈晨辰沉橙", "杨洋阳扬", "黄皇", "赵兆",
    "周洲舟", "吴武伍无", "徐许旭绪", "朱祝竹", "胡湖虎", "高糕", "林琳霖",
    "何和河贺赫", "郭国", "梁良凉亮", "郑正政", "宋松颂", "潘盼攀", "江姜蒋将",
    "峰锋丰风枫凤", "涛滔韬", "浩豪昊皓", "伟玮炜威巍", "敏民鸣明铭名",
    "静晶景靖净", "婷亭廷庭", "雯文闻纹", "佳嘉家加", "思斯丝", "涵寒韩汉",
)
_HOMOPHONE_CHAR_MAP = {}
for _gi, _grp in enumerate(_HOMOPHONE_GROUPS):
    for _ch in _grp:
        _HOMOPHONE_CHAR_MAP.setdefault(_ch, f"H{_gi}")


def _char_phonetic_token(ch):
    return _HOMOPHONE_CHAR_MAP[ch] if ch in _HOMOPHONE_CHAR_MAP else ("I_" + _gb2312_initial(ch))


def _phonetic_name_similarity(q_name, cand_name):
    """计算中文姓名同音/拼音首字母/字形综合相似度（0.0~1.0），用于错别字与同音字智能找人。"""
    import difflib
    q = str(q_name or "").strip()
    c = str(cand_name or "").strip()
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0
    if abs(len(q) - len(c)) > 1:
        return 0.0
    if len(q) == len(c):
        pos_scores, exact_cnt, homo_cnt = [], 0, 0
        for qc, cc in zip(q, c):
            if qc == cc:
                pos_scores.append(1.0)
                exact_cnt += 1
            elif qc in _HOMOPHONE_CHAR_MAP and _HOMOPHONE_CHAR_MAP.get(qc) == _HOMOPHONE_CHAR_MAP.get(cc):
                pos_scores.append(0.88)
                homo_cnt += 1
            elif _gb2312_initial(qc) == _gb2312_initial(cc) and _gb2312_initial(qc).isalpha():
                pos_scores.append(0.62)
            else:
                pos_scores.append(0.0)
        avg = sum(pos_scores) / max(1, len(pos_scores))
        if (exact_cnt + homo_cnt) >= max(2, len(q)) and exact_cnt >= 1:
            return max(avg, 0.88)
        if exact_cnt >= 1 and sum(1 for x in pos_scores if x >= 0.62) == len(pos_scores):
            return max(avg, 0.82)
        return avg
    return difflib.SequenceMatcher(None, [_char_phonetic_token(x) for x in q], [_char_phonetic_token(x) for x in c]).ratio() * 0.78


def _clean_spoken_target_phrase(raw_t):
    s = str(raw_t or "").strip()
    s = re.sub(r"^[「\"'『\s]+|[」\"'』\s]+$", "", s)
    s = re.sub(r"^(?:钉钉上的?|钉钉里的?|钉钉|给|发给|发送到|发到|投递给|私发给?|找|联系|同事|好友|那位|换成)\s*", "", s)
    s = re.sub(r"(?:的钉钉|那边|那里|这里|一下|看看|吧|啊|呀|哦|就行|就好|哈|呢)$", "", s).strip()
    return s


def resolve_smart_dingtalk_target(raw_target, cfg=None, speaker_name="", extra_roster=None, use_ai=True, is_local_speaker=True):
    """模糊与同音纠错找人：严格一对一匹配，绝不把查无此人错投给文件小助手，找不到直接报错。"""
    import difflib

    cleaned = _clean_spoken_target_phrase(raw_target)
    norm_cfg = normalize_config(cfg if isinstance(cfg, dict) else {})
    roster = collect_known_people_and_groups(cfg=norm_cfg, extra_roster=extra_roster)
    verified = _load_verified_dingtalk_targets()
    ai_mem = load_ai_user_memory()
    mem_corr = ai_mem.get("name_corrections") or {}

    if cleaned in mem_corr:
        best_corr = mem_corr[cleaned]
        return {
            "ok": True,
            "raw": str(raw_target or ""),
            "resolved_name": best_corr,
            "candidates": list(dict.fromkeys([best_corr, cleaned])),
            "matched_from_roster": True,
            "is_self": False,
            "note": f"已按本机纠错习惯将「{cleaned}」识别为「{best_corr}」",
        }

    if cleaned in ("文件小助手", "文件传输助手", "小助手", "传输助手"):
        return {
            "ok": True,
            "raw": str(raw_target or ""),
            "resolved_name": "文件小助手",
            "candidates": ["文件小助手"],
            "matched_from_roster": True,
            "is_self": True,
            "explicit_assistant": True,
            "note": "已明确指定发往「文件小助手」",
        }

    if cleaned in ("发群", "发群里", "发到群", "发到群里", "群", "群里", "大群", "默认群"):
        fallback_tgts = get_enabled_dt_targets(norm_cfg)
        if not fallback_tgts:
            return {
                "ok": False,
                "not_found": True,
                "raw": str(raw_target or ""),
                "error": "您还没有在【钉钉投递】里配置任何目标群聊，请先填写群名称或直接说「发给某某某」。",
            }
        fb_name = fallback_tgts[0]
        return {
            "ok": True,
            "raw": str(raw_target or ""),
            "resolved_name": fb_name,
            "candidates": list(dict.fromkeys(fallback_tgts)),
            "matched_from_roster": True,
            "is_self": False,
            "is_group_default": True,
            "note": f"已将「{cleaned}」对应为您配置好的发送目标「{'、'.join(fallback_tgts)}」",
        }

    if cleaned in ("我", "我自己", "本人", "我本人", "我的", "我的钉钉", "自己"):
        sp = str(speaker_name or "").strip()
        sp_real = sp if is_likely_real_person_name(sp) else ""
        remark_real = ""
        for mem in (extra_roster or []):
            if isinstance(mem, dict):
                n1 = str(mem.get("nickname") or "").strip()
                n2 = str(mem.get("display_name") or "").strip()
                if sp and sp in (n1, n2):
                    rmk = str(mem.get("remark") or "").strip()
                    if is_likely_real_person_name(rmk):
                        remark_real = rmk
                    break

        mem_self = str(ai_mem.get("self_real_name") or "").strip() if is_local_speaker else ""
        dt_self = detect_dingtalk_logged_in_user_name() if is_local_speaker else ""
        real_cands = [x for x in dict.fromkeys([sp_real, remark_real, mem_self, dt_self]) if x]
        if not real_cands:
            sp_desc = f"当前登录名为「{sp}」，" if sp else ""
            return {
                "ok": False,
                "not_found": True,
                "raw": str(raw_target or ""),
                "error": (
                    f"未能确定「我」对应的真实联系人姓名（{sp_desc}数据文件与钉钉中均无跟您一对一匹配的实名，程序与文件传输助手无关，不会错发给文件小助手）。"
                    f"请在软件右上角把昵称设为您的钉钉真实姓名，或直接告诉我「发给 某某某」！"
                ),
            }

        best_self = real_cands[0]
        if is_local_speaker:
            return {
                "ok": True,
                "raw": str(raw_target or ""),
                "resolved_name": best_self,
                "candidates": real_cands,
                "matched_from_roster": True,
                "is_self": True,
                "note": f"已一对一识别「我」为「{best_self}」",
            }
        cleaned = best_self
        is_self = True
    else:
        is_self = False

    if not cleaned:
        fallback_tgts = get_enabled_dt_targets(norm_cfg)
        if not fallback_tgts:
            return {
                "ok": False,
                "not_found": True,
                "raw": str(raw_target or ""),
                "error": "未指定要发给哪位同事或哪个群，且当前未配置默认目标会话，请直接说出收信人姓名。",
            }
        fb_name = fallback_tgts[0]
        return {
            "ok": True,
            "raw": str(raw_target or ""),
            "resolved_name": fb_name,
            "candidates": [fb_name],
            "matched_from_roster": False,
            "is_self": is_self,
            "note": f"沿用默认目标会话「{fb_name}」",
        }

    if cleaned in verified:
        best = verified[cleaned]
        cands = [best, cleaned]
        return {
            "ok": True,
            "raw": str(raw_target or ""),
            "resolved_name": best,
            "candidates": list(dict.fromkeys([c for c in cands if c])),
            "matched_from_roster": True,
            "is_self": is_self,
            "note": f"已按历史确认记忆将「{cleaned}」识别为「{best}」",
        }

    want_group = (not is_self) and any(k in str(raw_target or "") for k in ("群", "组", "部门", "部"))
    if is_self:
        pool_order = [p for p in roster["people"] if not any(p.endswith(s) for s in ("组", "群", "部门", "部", "中心", "团队"))]
    elif want_group:
        pool_order = roster["groups"] + roster["people"]
    else:
        pool_order = roster["people"] + roster["groups"]

    scored = []
    q_low = cleaned.casefold()
    for cand in pool_order:
        c_low = cand.casefold()
        cand_is_grp = any(cand.endswith(s) for s in ("组", "群", "部门", "部", "中心", "团队"))
        score = 0.0
        if q_low == c_low:
            score = 1.0
        elif q_low in c_low or c_low in q_low:
            ratio = min(len(q_low), len(c_low)) / max(1, len(c_low))
            score = 0.80 + 0.16 * ratio
            if want_group and cand_is_grp:
                score += 0.04
            elif not want_group and not cand_is_grp:
                score += 0.04
            elif not want_group and cand_is_grp:
                score = min(score, 0.58)
        else:
            seq_r = difflib.SequenceMatcher(None, q_low, c_low).ratio()
            overlap = sum(1 for ch in set(q_low) if ch in c_low)
            p_sim = _phonetic_name_similarity(cleaned, cand) if (not want_group and not cand_is_grp) else 0.0
            score = max(seq_r, overlap / max(1, len(set(q_low))) * 0.72, p_sim)
            if not want_group and cand_is_grp:
                score = min(score, 0.58)
        if score >= 0.52:
            scored.append((score, cand))
    scored.sort(key=lambda x: -x[0])

    best_name = cleaned
    alts = [c for _s, c in scored[:4]]
    matched_roster = False
    if scored and scored[0][0] >= 0.76:
        best_name = scored[0][1]
        matched_roster = True
    elif use_ai and pool_order and cfg is not None:
        norm_c = normalize_config(cfg)
        ai_cfg = norm_c.get("ai_assistant") or {}
        if ai_cfg.get("enabled", True) and str(ai_cfg.get("provider") or "gemini") != "local_only":
            try:
                sample_pool = pool_order[:60]
                g_res = _call_gemini_generate(
                    norm_c,
                    "你是钉钉联系人与群名同音纠错助手。只输出一段JSON，不要解释：如果花名册中存在明确对应的同音字、错别字或简称（如卢慧珍->娄慧珍、刘亦->刘奕），返回{\"best\":\"花名册原名\",\"alts\":[]}；如果完全没有对应人选，必须返回{\"best\":\"\",\"not_found\":true}，严禁编造或凑数。",
                    [{"role": "user", "parts": [{"text": f"用户提到：{cleaned}\n已知花名册：{json.dumps(sample_pool, ensure_ascii=False)}"}]}],
                    temperature=0.05,
                    max_tokens=100,
                    timeout_sec=3.5,
                )
                if g_res.get("ok"):
                    m_j = re.search(r"\{[\s\S]*?\}", str(g_res.get("reply") or ""))
                    if m_j:
                        obj = json.loads(m_j.group(0))
                        b_ai = str(obj.get("best") or "").strip()
                        if b_ai and len(b_ai) <= 30 and not obj.get("not_found") and b_ai in pool_order:
                            best_name = b_ai
                            matched_roster = True
                        for a in (obj.get("alts") or []):
                            a_s = str(a or "").strip()
                            if a_s and a_s not in alts:
                                alts.append(a_s)
            except Exception:
                pass
        if not matched_roster and scored and scored[0][0] >= 0.72:
            best_name = scored[0][1]
            matched_roster = True

    cands = [best_name] if best_name == cleaned else [best_name, cleaned]
    for a in alts:
        if a not in cands:
            cands.append(a)
    stripped_core = re.sub(r"(?:组|群|部门)$", "", best_name).strip()
    if stripped_core and len(stripped_core) >= 2 and stripped_core not in cands:
        cands.append(stripped_core)
    if not matched_roster and len(cleaned) >= 3:
        cands.append(cleaned[-2:])
        cands.append(cleaned[:2])
    cands = [x for x in dict.fromkeys(cands) if str(x).strip()]

    if best_name != cleaned:
        note = f"已将「{cleaned}」智能纠错匹配为「{best_name}」"
    elif matched_roster:
        note = f"已一对一锁定「{best_name}」"
    else:
        note = f"将在钉钉通讯录按「{best_name}」一对一精确查找（查不到将直接报错）"
    return {
        "ok": True,
        "raw": str(raw_target or ""),
        "resolved_name": best_name,
        "candidates": cands,
        "matched_from_roster": matched_roster,
        "is_self": is_self,
        "note": note,
    }


def parse_natural_date_spec(raw):
    """解析口语与明确日期（今天/昨天/前天/大前天/11月20日/2026-11-20）。"""
    txt = str(raw or "")
    now = datetime.now()
    m_full = re.search(r"(\d{4})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})\s*[日号]?", txt)
    if m_full:
        try:
            y, m, d = int(m_full.group(1)), int(m_full.group(2)), int(m_full.group(3))
            dt = datetime(y, m, d)
            return {
                "explicit": True,
                "mode": "exact",
                "day_offset": (dt.date() - now.date()).days,
                "formatted_cn": f"{y}年{m}月{d}日",
                "formatted_dash": f"{y:04d}-{m:02d}-{d:02d}",
                "label": f"{y}年{m}月{d}日",
            }
        except Exception:
            pass
    m_md = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?", txt)
    if m_md:
        try:
            m, d = int(m_md.group(1)), int(m_md.group(2))
            y = now.year - 1 if (m > now.month + 1 and now.month <= 2) else now.year
            dt = datetime(y, m, d)
            return {
                "explicit": True,
                "mode": "exact",
                "day_offset": (dt.date() - now.date()).days,
                "formatted_cn": f"{y}年{m}月{d}日",
                "formatted_dash": f"{y:04d}-{m:02d}-{d:02d}",
                "label": f"{m}月{d}日",
            }
        except Exception:
            pass
    if any(k in txt for k in ("大前天",)):
        return {"explicit": True, "mode": "offset", "day_offset": -3, "label": "大前天(-3天)"}
    if any(k in txt for k in ("前天", "前两天")):
        return {"explicit": True, "mode": "offset", "day_offset": -2, "label": "前天(-2天)"}
    if any(k in txt for k in ("昨天", "昨日", "前一天", "前1天")):
        return {"explicit": True, "mode": "offset", "day_offset": -1, "label": "昨天(-1天)"}
    if any(k in txt for k in ("今天", "今日", "当天", "最新", "此刻", "现在")):
        return {"explicit": True, "mode": "offset", "day_offset": 0, "label": "今天(当天)"}
    return {"explicit": False, "mode": "offset", "day_offset": 0, "label": "当前默认日期"}


def _extract_last_on_demand_context(chat_history):
    """从最近对话历史提取上一次明确的发送目标与动作，避免续跑时反复确认发哪个人。"""
    last_targets = []
    last_action = "run_full"
    last_was_not_found = False
    for idx, it in enumerate(reversed(list(chat_history or [])[-10:])):
        if not isinstance(it, dict):
            continue
        meta = it.get("on_demand_meta")
        if isinstance(meta, dict):
            tgts = [str(x).strip() for x in (meta.get("targets") or []) if str(x).strip()]
            if tgts and not last_targets:
                last_targets = tgts
                last_action = str(meta.get("action") or "run_full")
                break
        txt = str(it.get("text") or "")
        if idx <= 1 and it.get("role") == "model" and ("一对一匹配" in txt or "未找到" in txt or "未能确定" in txt):
            last_was_not_found = True
        if it.get("role") == "model" and not last_targets:
            m_t = re.search(r"(?:已确定发送给\s*\*?\*?|钉钉发送目标[：:]\s*\*?\*?|发送到钉钉「|已发送[^「]*「)([^」*\n（(]{1,24})", txt)
            if m_t:
                cand_line = m_t.group(1).strip()
                cands = [p.strip() for p in cand_line.split("、") if p.strip()]
                if cands:
                    last_targets = cands
                    if "文字" in txt:
                        last_action = "text_send"
                    elif "截图" in txt and "跑表" not in txt and "全流程" not in txt:
                        last_action = "capture_send"
                    else:
                        last_action = "run_full"
                    break
    return {"targets": last_targets, "action": last_action, "last_was_not_found": last_was_not_found}


_AI_ALLOWED_ACTIONS = {"run_full", "capture_send", "text_send", "start_schedule", "stop_schedule", "pause_task", "resume_task", "cancel_task"}

_PAUSE_TASK_RE = re.compile(
    r"(?:暂停运行|暂停执行|暂停任务|暂停当前任务|暂停跑表|先暂停|帮我暂停|暂停一下|暂停一会|先停一下|停一下|先挂起|暂时暂停|^暂停[!！。~呢吧啊]*$)"
)

_RESUME_TASK_RE = re.compile(
    r"(?:继续运行|继续执行|继续任务|继续当前任务|继续跑|恢复运行|恢复执行|恢复任务|取消暂停|解除暂停|接着跑|接着运行|接着执行|帮我继续|继续一下|恢复一下|^继续[!！。~吧啊]*$|^恢复[!！。~吧啊]*$)"
)

_CANCEL_TASK_RE = re.compile(
    r"(?:取消当前任务|取消任务|取消运行|取消执行|取消跑表|取消本轮|取消发送|帮我取消|立刻取消|马上取消|"
    r"终止运行|终止任务|终止当前任务|中止任务|中止运行|停止当前任务|停止任务|停止运行|停止执行|停止跑表|停止发送|"
    r"立即停止|马上停止|停掉任务|停掉当前|停掉跑表|停下来|别跑了|不要跑了|不用跑了|不要运行了|别执行了|"
    r"别发了|不要发了|算了取消|算了不跑了|^取消[!！。~吧啊]*$|^停止[!！。~吧啊]*$|^停掉[!！。~吧啊]*$|^终止[!！。~吧啊]*$)"
)

_STOP_SCHEDULE_KWS = (
    "停止自动值守", "关闭自动值守", "关掉自动值守",
    "停止值守", "关闭值守", "关掉值守", "取消值守", "停掉值守",
    "停止定时", "关闭定时", "关掉定时",
)

_START_SCHEDULE_KWS = (
    "开启自动值守", "打开自动值守", "启动自动值守", "开始自动值守",
    "开启值守", "打开值守", "启动值守",
    "开启定时", "打开定时", "启动定时",
)

_RUN_ACTION_VERBS = (
    "跑一下", "跑个", "跑一次", "跑程序", "自动运行", "立即运行", "马上运行", "马上跑",
    "抓最新", "抓取数据", "更新表格", "重新跑", "重跑", "续跑", "再跑", "再来一次", "再来一遍",
    "重发一下", "再发一次", "刷新一下", "截个图", "截图发", "直接发", "现在发",
    "开始一次", "开始啊", "开始吧", "开始跑", "开始执行", "开始任务", "开始干活", "开跑",
    "跑表", "跑一轮", "跑一遍", "跑一把", "帮我跑", "立刻跑", "现在跑", "马上执行", "立即执行",
    "执行一次", "执行一下", "执行一轮", "运行一次", "运行一下", "启动一次", "启动跑表", "启动任务",
    "来一次", "来一轮", "搞一次", "弄一次", "发一次", "发一轮", "刷一轮", "更新一次", "抓一次", "抓数", "截一次图", "截图一次",
)

_SHORT_RUN_CMD_RE = re.compile(
    r"(?:快|赶紧|现在|立刻|马上|直接|帮我|给我|请|那就|行)?(?:开始|执行|运行|启动|开跑|跑表|跑吧|发吧|干活)"
    r"(?:吧|啊|呀|啦|咯|一下|一次|一轮|一遍|呗|嘛|哈|了|！|!|~|。)*"
)


def try_handle_ai_on_demand_task(user_text, cfg, speaker_name="", extra_roster=None, is_local_speaker=True, chat_history=None, bypass_schedule_guard=False):
    """识别即时单次执行与值守启停指令（支持同音纠错找人、续跑上下文与严格查无此人直接报错），绝不污染默认配置。"""
    raw = str(user_text or "").strip()
    if not raw or check_ai_security_and_creator_guardrail(raw):
        return {"handled": False}

    # 周期计划、排期增删与起始时间配置类词句留给 try_handle_ai_config_command
    if not bypass_schedule_guard and any(k in raw for k in ("每天", "每日", "每周", "工作日", "周末", "每隔", "定时计划", "排期总览", "帮我设置", "帮我改设置", "设置为")):
        return {"handled": False}
    if any(k in raw for k in ("新增", "添加", "多加", "加一个", "加一条", "追加", "额外加", "补加", "删除", "删掉", "移除", "去掉")) and any(
        ctx in raw for ctx in ("定时", "排期", "时间点", "节点", "自定义")
    ):
        return {"handled": False}
    if any(k in raw for k in ("关闭刷底表", "关闭定时刷底表", "关掉刷底表", "关掉定时刷底表", "取消定时刷底表", "取消刷底表")) or (
        any(k in raw for k in ("刷底表", "三张表", "三表", "三个表", "三个数据表", "更新三表", "刷新三表", "只写R4", "只写r4", "不改数据汇总", "不要改数据汇总"))
        and (
            any(k in raw for k in ("每隔", "关闭", "关掉", "取消", "开启", "打开", "启用", "恢复", "设为", "改成", "改为"))
            or bool(re.search(r"每\s*(?:\d+|[零一二两三四五六七八九十]+)\s*(?:个)?(?:小时|分钟?)", raw))
        )
    ):
        return {"handled": False}
    if any(k in raw for k in ("开始时间", "几点开始", "什么时候开始")) or re.search(r"从\s*\d{1,2}\s*[:：点].{0,4}开始", raw):
        return {"handled": False}
    if any(k in raw for k in ("怎么", "如何", "哪里设置", "为什么", "能不能自己", "什么时候", "几点")) and not any(
        k in raw for k in ("发给我", "发我", "给我发", "发给", "发到", "跑一下", "跑程序", "自动运行", "立即运行", "马上跑", "续跑", "再跑", "开始一次", "执行一次")
    ):
        return {"handled": False}

    date_spec = parse_natural_date_spec(raw)

    # 自动值守启停直通路由（定时停用词先于通用跑表否定词拦截）
    if any(k in raw for k in _STOP_SCHEDULE_KWS):
        return {
            "handled": True,
            "action": "stop_schedule",
            "targets": [],
            "date_spec": date_spec,
            "thinking_steps": [
                f"识别到值守控制指令：{raw[:28]}",
                "已对接底层自动值守开关，准备停止循环",
            ],
            "reply": "已收到指令，正在为您停止自动值守…",
        }
    if any(k in raw for k in _START_SCHEDULE_KWS) and not re.search(r"(?:不要|别|先别|暂不|不用|停止|取消|关闭|关掉)", raw):
        return {
            "handled": True,
            "action": "start_schedule",
            "targets": [],
            "date_spec": date_spec,
            "thinking_steps": [
                f"识别到值守控制指令：{raw[:28]}",
                "正在核验排期配置与时间点并拉起自动值守",
            ],
            "reply": "⚡ 收到！正在为您开启自动值守计划…",
        }

    # 运行中暂停 / 恢复继续 / 取消当前任务（排除静音、值守、刷底表、默认、发信目标等非任务词以免误拦）
    _has_runtime_ctrl_conflict = any(
        k in raw
        for k in (
            "静音", "不发群", "自动值守", "值守", "定时", "刷底表", "三表",
            "以后", "默认", "公告", "媒体", "动画", "视频", "防休眠", "熄屏",
            "发给", "发送到", "发到", "发我", "给我发", "私发",
        )
    )
    if not _has_runtime_ctrl_conflict:
        if _CANCEL_TASK_RE.search(raw):
            return {
                "handled": True,
                "action": "cancel_task",
                "targets": [],
                "date_spec": date_spec,
                "thinking_steps": [
                    f"识别到取消停止指令：{raw[:28]}",
                    "已通知执行引擎立即终止当前任务并释放锁定",
                ],
                "reply": "已收到取消指令，正在立即停止当前任务…",
            }
        m_pause = _PAUSE_TASK_RE.search(raw)
        if m_pause and not re.search(r"(?:不要|别|不用|别再|无需|停止|取消)\s*$", raw[max(0, m_pause.start() - 6):m_pause.start()]):
            return {
                "handled": True,
                "action": "pause_task",
                "targets": [],
                "date_spec": date_spec,
                "thinking_steps": [
                    f"识别到运行中暂停指令：{raw[:28]}",
                    "已直连运行时挂起开关，保留当前浏览器与进度现场",
                ],
                "reply": "已收到暂停指令，正在安全暂停当前任务…",
            }
        m_resume = _RESUME_TASK_RE.search(raw)
        if m_resume and not re.search(r"(?:不要|别|不用|别再|无需)\s*$", raw[max(0, m_resume.start() - 6):m_resume.start()]):
            return {
                "handled": True,
                "action": "resume_task",
                "targets": [],
                "date_spec": date_spec,
                "thinking_steps": [
                    f"识别到继续运行指令：{raw[:28]}",
                    "正在唤醒挂起点并恢复执行未完成步骤",
                ],
                "reply": "已收到继续指令，正在恢复执行当前任务…",
            }

    scan_text = re.sub(
        r"(?:不要|不用|别|不是|勿|切勿)\s*(?:发给|发送到|发到|投递给|私发给?|传给|推给|发)\s*[^，。,.;；!\n？?\s]{1,16}",
        " ",
        raw,
    )
    # 否定动作拦截（保留如“跑一下发给我不要发群”等仅否定某一发送目标的句式）
    if re.search(r"(?:不要|别|先别|暂不|不用|停止|取消)\s*(?:开始|跑|运行|执行|启动|立即发|现在发|马上发|自动发|截图发)", raw) or (
        re.search(r"(?:不要|别|先别|暂不|不用|停止|取消)\s*发(?:了|了吧|送|群)?$", scan_text)
        and not re.search(r"(?:发给|发到|发我)", scan_text)
    ):
        return {"handled": False}

    is_short_run_cmd = bool(_SHORT_RUN_CMD_RE.fullmatch(raw))
    has_run_verb = is_short_run_cmd or any(k in raw for k in _RUN_ACTION_VERBS)
    has_immediate_run_intent = has_run_verb or any(
        k in raw for k in ("现在发", "立刻发", "直接发", "马上发", "截图", "一份", "看看")
    )

    is_pure_habit_teach = (
        any(k in raw for k in ("记住", "以后默认", "以后都", "习惯默认", "习惯看", "习惯跑", "以后不要发", "以后别发", "默认不发", "清除习惯", "清空习惯", "查看记忆", "查看习惯", "你记住了", "恢复发群", "取消不发群"))
        or bool(re.search(r"(?:我是|我叫|我的钉钉名|不是[\u4e00-\u9fff]{2,8}[，,]?是[\u4e00-\u9fff]{2,8}|没有[\u4e00-\u9fff]{2,8}[，,]?是[\u4e00-\u9fff]{2,8})", raw))
    )
    if is_pure_habit_teach and not has_immediate_run_intent:
        return {"handled": False}

    hist_ctx = _extract_last_on_demand_context(chat_history)
    raw_targets = []
    if re.search(r"(?:发给|发送到|发到|投递给|私发给?|传给|推给)\s*(?:我自己|我本人|本人|我的钉钉|自己|我)(?![\u4e00-\u9fffA-Za-z0-9])", scan_text) or re.search(
        r"(?:给我发|私发我|发我)(?:看看|一份|吧|一下|哈|呢|就行|就好|(?![\u4e00-\u9fffA-Za-z0-9]))",
        scan_text,
    ):
        raw_targets.append("我")
    for m in re.finditer(r"(?:发给|发送到|发到|投递给|私发给?|传给|推给|换成|改成)\s*([^，。,.;；!\n？?]{1,24})", scan_text):
        seg = m.group(1).strip()
        seg = re.split(r"(?:\s+|并|然后|同时|再把|也发|并且|不要|不用|别|不是|就行|就好|看看|一下)", seg)[0].strip()
        for part in re.split(r"[、,，和与及]+", seg):
            p = _clean_spoken_target_phrase(part)
            if p in ("我", "我自己", "本人", "我本人", "我的", "我的钉钉", "自己"):
                p = "我"
            if p and p not in raw_targets:
                raw_targets.append(p)
    if not raw_targets and re.search(r"(?:发到?群里?|发大群|投递到群)", scan_text):
        raw_targets.append("发群")

    # 若上一轮刚刚提醒“未找到一对一联系人”，本轮用户只简短回复了一个人名（如“娄慧珍”），直接接管执行
    if not raw_targets and hist_ctx.get("last_was_not_found") and re.fullmatch(r"[\u4e00-\u9fff]{2,5}", raw):
        raw_targets.append(raw)

    if not raw_targets and not has_run_verb:
        return {"handled": False}

    try:
        learn_user_habits_from_interaction(raw, speaker_name=speaker_name, is_local_speaker=is_local_speaker)
    except Exception:
        pass

    import copy
    run_cfg = normalize_config(copy.deepcopy(cfg if isinstance(cfg, dict) else {}))
    ai_mem = load_ai_user_memory()

    # 优先尊重用户教导的个人偏好记忆（日期偏移、静音、跨重启续跑目标、默认收信人、禁发群规则）
    if not date_spec.get("explicit") and ai_mem.get("preferred_day_offset") is not None:
        pref_off = int(ai_mem["preferred_day_offset"])
        date_spec = {
            "explicit": True,
            "mode": "offset",
            "day_offset": pref_off,
            "label": f"按您习惯的偏好({_format_day_offset_cn(pref_off)})",
        }

    # 续跑/再跑时未点名发谁：先查内存对话历史，再查持久化记忆上次确认目标与默认偏好目标
    if not raw_targets and any(k in raw for k in ("续跑", "再跑", "再来一次", "再来一遍", "重跑", "重发", "再发")):
        if hist_ctx.get("targets"):
            raw_targets = list(hist_ctx["targets"])
        else:
            persisted_ctx = _extract_last_on_demand_context(ai_mem.get("chat_history") or [])
            if persisted_ctx.get("targets"):
                raw_targets = list(persisted_ctx["targets"])
            elif ai_mem.get("last_confirmed_targets"):
                raw_targets = list(ai_mem["last_confirmed_targets"])
            elif ai_mem.get("preferred_targets"):
                raw_targets = list(ai_mem["preferred_targets"])
    elif not raw_targets and has_run_verb and ai_mem.get("preferred_targets"):
        raw_targets = list(ai_mem["preferred_targets"])

    if any(k in raw for k in ("只发文字", "纯文字", "文字总结发", "文字发")):
        action = "text_send"
    elif has_run_verb:
        action = "run_full"
    elif hist_ctx.get("last_was_not_found") and hist_ctx.get("action") in ("run_full", "capture_send", "text_send"):
        action = hist_ctx["action"]
    else:
        action = "capture_send"

    resolved_list = []
    res_notes = []
    search_cand_map = {}
    for rt in raw_targets:
        r_obj = resolve_smart_dingtalk_target(
            rt, cfg=run_cfg, speaker_name=speaker_name, extra_roster=extra_roster, use_ai=True, is_local_speaker=is_local_speaker
        )
        if not r_obj.get("ok"):
            return {
                "handled": True,
                "action": "error_only",
                "targets": [],
                "date_spec": date_spec,
                "thinking_steps": [
                    f"识别到执行指令：{raw[:24]}",
                    "校验钉钉发送对象失败，已安全拦截防止误发",
                ],
                "reply": f"❌ {r_obj.get('error') or '未找到一对一匹配的发送对象，已安全拦截，不会误发给文件传输助手。'}",
            }
        if r_obj.get("is_group_default"):
            grp_list = [x for x in (r_obj.get("candidates") or []) if x] or [r_obj["resolved_name"]]
            for g_nm in grp_list:
                if g_nm not in resolved_list:
                    resolved_list.append(g_nm)
                search_cand_map[g_nm] = [g_nm]
        else:
            r_name = r_obj["resolved_name"]
            if r_name not in resolved_list:
                resolved_list.append(r_name)
            search_cand_map[r_name] = r_obj.get("candidates") or [r_name]
        if r_obj.get("note"):
            res_notes.append(r_obj["note"])

    skip_dingtalk_only = False
    if not resolved_list:
        if not raw_targets and (not run_cfg.get("enable_dingtalk", True)) and (not ai_mem.get("forbid_group_send")) and action == "run_full":
            skip_dingtalk_only = True
        elif ai_mem.get("forbid_group_send"):
            if ai_mem.get("preferred_targets"):
                resolved_list = list(ai_mem["preferred_targets"])
            elif ai_mem.get("self_real_name") and is_local_speaker:
                resolved_list = [ai_mem["self_real_name"]]
                search_cand_map[ai_mem["self_real_name"]] = [ai_mem["self_real_name"]]
                res_notes.append(f"遵循您不发大群的习惯，默认发往「{ai_mem['self_real_name']}」")
            else:
                return {
                    "handled": True,
                    "action": "error_only",
                    "targets": [],
                    "date_spec": date_spec,
                    "thinking_steps": [
                        f"识别到执行指令：{raw[:24]}",
                        "遵循您不发大群的习惯，等待指明个人收信人",
                    ],
                    "reply": "❌ 记住您平时交代过不要直接发大群，但当前尚未指明个人收信人，请告诉我「发给 某某某」或恢复发群。",
                }
        else:
            resolved_list = get_enabled_dt_targets(run_cfg)
        if not resolved_list and not skip_dingtalk_only:
            return {
                "handled": True,
                "action": "error_only",
                "targets": [],
                "date_spec": date_spec,
                "thinking_steps": [
                    f"识别到执行指令：{raw[:24]}",
                    "未检测到默认发送群聊或收信人，已安全暂停",
                ],
                "reply": "❌ 还没有指明要发给哪位同事（也没有默认群聊配置），请直接告诉我「发给 某某某」。",
            }

    if skip_dingtalk_only:
        run_cfg["enable_dingtalk"] = False
        run_cfg["enable_capture"] = bool(run_cfg.get("enable_capture", True))
    else:
        run_cfg["enable_dingtalk"] = True
        run_cfg["dt_auto_send"] = True
        run_cfg["enable_capture"] = True
        if "取消静音" in raw or "不静音" in raw:
            run_cfg["dt_silent_send"] = False
        elif "静音" in raw:
            run_cfg["dt_silent_send"] = True
        elif ai_mem.get("preferred_silent") is not None:
            run_cfg["dt_silent_send"] = bool(ai_mem["preferred_silent"])
        run_cfg["dt_targets"] = [{"name": t, "enabled": True} for t in resolved_list]
        run_cfg["dt_search"] = resolved_list[0]
        run_cfg["point_overrides"] = {}
        run_cfg["_smart_target_candidates"] = search_cand_map
        if any(rt == "我" for rt in raw_targets) and is_local_speaker and resolved_list:
            run_cfg["_resolve_self_for_search"] = resolved_list[0]

    run_cfg["point_overrides"] = {}
    if date_spec.get("explicit"):
        run_cfg["suffix_rules"] = []
        run_cfg["dt_use_actual_clock"] = False
        sp = run_cfg.setdefault("summary_presets", {})
        sp["enabled"] = True
        items = sp.setdefault("items", [])
        d3 = next((it for it in items if isinstance(it, dict) and str(it.get("cell") or "").upper() in ("D3", "D2")), None)
        if d3 is None:
            d3 = {"enabled": True, "sheet": "数据汇总", "cell": "D3", "label": "查询日期", "action": "today", "date_fmt": "YYYY年M月D日", "day_offset": 0}
            items.insert(0, d3)
        d3["enabled"] = True
        if date_spec.get("mode") == "exact":
            d3["action"] = "value"
            d3["value"] = date_spec["formatted_cn"]
            run_cfg["dt_suffix"] = f"{date_spec['label']}数据"
        else:
            d3["action"] = "today"
            d3["cutoff_enabled"] = False
            d3["day_offset"] = int(date_spec.get("day_offset", 0))
            run_cfg["dt_cutoff_enabled"] = False
            run_cfg["dt_day_offset"] = int(date_spec.get("day_offset", 0))

    if resolved_list:
        try:
            learn_user_habits_from_interaction(
                raw,
                speaker_name=speaker_name,
                is_local_speaker=is_local_speaker,
                resolved_targets=resolved_list,
                action=action,
            )
        except Exception:
            pass

    if skip_dingtalk_only:
        act_label = "全流程抓数 + 写入表格（未启用钉钉投递）"
        reply = f"⚡ 已收到指令，正在为您直接执行：**{act_label}**（{date_spec['label']}），请稍候~"
        t_steps = [
            f"识别到立即执行指令：{raw[:24]}",
            f"当前配置关闭了钉钉投递，直接拉起抓数与写表引擎（{date_spec['label']}）",
        ]
    else:
        act_label = {
            "run_full": "全流程跑表 + 发钉钉",
            "capture_send": "刷新汇总表 + 截图发钉钉",
            "text_send": "整理数据文字战报 + 直发钉钉",
        }[action]
        tgt_str = "、".join(resolved_list)
        note_line = f"（{'；'.join(res_notes)}）" if res_notes else ""
        reply = f"⚡ 已确定发送给 **{tgt_str}**{note_line}，正在为您直接执行：{act_label}（{date_spec['label']}），请稍候~"
        t_steps = [
            f"识别到立即执行指令：{raw[:24]}",
            f"已核验发送目标「{tgt_str}」并拉起底层执行引擎（{act_label}）",
        ]
    return {
        "handled": True,
        "action": action,
        "run_cfg": run_cfg,
        "targets": resolved_list,
        "target_notes": res_notes,
        "date_spec": date_spec,
        "thinking_steps": t_steps,
        "reply": reply,
    }


def parse_and_strip_ai_action_tag(reply_text):
    """提取大模型回复末尾附带的 [[SM_ACTION:{...}]] 控制协议并从展示文案彻底剔除标签。"""
    raw = str(reply_text or "")
    parsed_action = None
    for m in re.finditer(r"\[\[SM_ACTION\s*:\s*(\{.*?\})\s*\]\]", raw, flags=re.DOTALL):
        try:
            obj = json.loads(m.group(1))
        except Exception:
            obj = None
        if isinstance(obj, dict):
            act = str(obj.get("action") or "").strip().lower()
            if act in _AI_ALLOWED_ACTIONS:
                parsed_action = {
                    "action": act,
                    "target": str(obj.get("target") or "").strip(),
                    "date": str(obj.get("date") or "").strip(),
                }
    cleaned = re.sub(r"\s*\[\[SM_ACTION\s*:[\s\S]*?\]\]\s*", "\n", raw).strip()
    return cleaned, parsed_action


def build_on_demand_task_from_ai_action(action_obj, cfg, speaker_name="", extra_roster=None, is_local_speaker=True, chat_history=None):
    """将大模型输出的动作对象严格经过联系人一对一核验、同音纠错与权限保护转为可执行任务。"""
    if not isinstance(action_obj, dict):
        return {"handled": False}
    act = str(action_obj.get("action") or "").strip().lower()
    if act not in _AI_ALLOWED_ACTIONS:
        return {"handled": False}
    date_hint = str(action_obj.get("date") or "").strip()
    date_spec = parse_natural_date_spec(date_hint)
    if act in ("start_schedule", "stop_schedule", "pause_task", "resume_task", "cancel_task"):
        return {
            "handled": True,
            "action": act,
            "targets": [],
            "date_spec": date_spec,
            "thinking_steps": [
                f"大模型动作桥接：{act}",
                "准备通过安全控制口执行运行时控制指令",
            ],
            "reply": "",
        }
    tgt_hint = str(action_obj.get("target") or "").strip()
    tgt_hint = re.sub(r"^(?:发给|发送到|发到|投递给)\s*", "", tgt_hint).strip()
    if tgt_hint in ("我", "我自己", "本人", "我本人", "我的", "我的钉钉", "自己"):
        tgt_part = "发给我"
    elif tgt_hint in ("发群", "群", "群里", "默认群", "发到群", "发到群里", "大群"):
        tgt_part = "发到群里"
    elif tgt_hint:
        tgt_part = f"发给{tgt_hint}"
    else:
        tgt_part = ""
    verb = "只发文字" if act == "text_send" else ("截图发" if act == "capture_send" else "开始一次")
    phrase = " ".join(p for p in (date_hint, verb, tgt_part) if p).strip()
    res = try_handle_ai_on_demand_task(
        phrase,
        cfg,
        speaker_name=speaker_name,
        extra_roster=extra_roster,
        is_local_speaker=is_local_speaker,
        chat_history=chat_history,
        bypass_schedule_guard=True,
    )
    if res.get("handled") and res.get("action") != "error_only":
        res["action"] = act
    return res


def _build_config_verification_summary(new_cfg, changes):
    """为 AI 自然语言自动设排期后生成详尽的大白话核对结果清单，请用户确认是否符合预期。"""
    c = normalize_config(new_cfg)
    w_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    dmode = c.get("day_mode", "daily")
    if dmode == "workday":
        day_desc = "工作日（周一至周五）"
    elif dmode == "weekend":
        day_desc = "周末（周六、周日）"
    elif dmode == "custom":
        cds = [w_names[i] for i in (c.get("custom_days") or []) if 0 <= int(i) <= 6]
        day_desc = "自定义（" + ("、".join(cds) if cds else "未选") + "）"
    else:
        day_desc = "每天（周一至周日）"

    tt = build_daily_timetable(c)
    tt_times = [pt["time"] for pt in tt]
    if len(tt_times) > 10:
        tt_desc = f"共 {len(tt_times)} 个时间点（{'、'.join(tt_times[:5])} … 等至 {tt_times[-1]}）"
    elif tt_times:
        tt_desc = f"共 {len(tt_times)} 个时间点：{'、'.join(tt_times)}"
    else:
        tt_desc = "暂无有效执行时间点"

    targets = get_enabled_dt_targets(c)
    tgt_desc = "、".join(targets) if targets else "暂未启用任何群聊（将跳过发钉钉）"

    sp_items = ((c.get("summary_presets") or {}).get("items") or [])
    d3 = next((it for it in sp_items if isinstance(it, dict) and str(it.get("cell", "")).upper() in ("D3", "D2")), None)
    d3_desc = "当天"
    if d3:
        off = int(d3.get("day_offset", 0) or 0)
        d3_desc = "昨天（前一天 -1 天）" if off == -1 else ("前天（-2天）" if off == -2 else (f"当天 ({off:+d}天)" if off else "当天（0天）"))

    cell_notes = []
    for it in sp_items:
        if not isinstance(it, dict) or not it.get("enabled", True):
            continue
        cell = str(it.get("cell") or "").upper()
        act = it.get("action", "value")
        val = str(it.get("value") or "")
        if cell in ("L3", "Y3", "Y4", "Y5", "AA2", "AA5", "F2", "H4", "I4") or act == "value":
            if val:
                cell_notes.append(f"{cell}={val}")

    send_mode_txt = "粘贴后直接自动发送" if c.get("dt_auto_send") else "只贴进输入框暂不自动按 Enter"
    silent_txt = "静音发送" if c.get("dt_silent_send", True) else "常规提醒发送（不点静音）"
    clock_txt = "文案带实际发送时刻" if c.get("dt_use_actual_clock") else "文案按计划时间点"
    delay_sec = c.get("dt_paste_delay_sec", 3)
    send_wait_s = c.get("dt_send_wait_sec", 5)

    extra_set = set(c.get("extra_points") or [])
    ov_notes = []
    for hm_k in sorted((c.get("point_overrides") or {}).keys()):
        ov_d = c["point_overrides"][hm_k]
        parts = []
        if hm_k in extra_set:
            parts.append("自定义加点")
        if ov_d.get("mode") == "table_only":
            parts.append("不发钉钉")
        elif ov_d.get("mode") == "send":
            parts.append("强制发钉钉")
        if isinstance(ov_d.get("targets"), list) and ov_d["targets"]:
            parts.append("发给:" + "、".join(ov_d["targets"][:3]))
        if ov_d.get("sheet_day_offset") is not None:
            so = int(ov_d["sheet_day_offset"])
            parts.append("查昨天" if so == -1 else f"查{so:+d}天")
        if ov_d.get("msg_time_mode") == "date_only":
            parts.append("仅发日期")
        elif ov_d.get("msg_time_mode") == "datetime":
            parts.append("日期+时间")
        elif ov_d.get("msg_time_mode") == "none":
            parts.append("纯内容不加时间")
        if ov_d.get("msg_date_fmt"):
            parts.append(ov_d["msg_date_fmt"])
        if ov_d.get("suffix") is not None:
            suf_disp = str(ov_d["suffix"]).strip() or "(空后缀)"
            parts.append(f"内容:{suf_disp[:18]}")
        if ov_d.get("silent") is True:
            parts.append("静音发送")
        elif ov_d.get("silent") is False:
            parts.append("正常提醒")
        if parts:
            ov_notes.append(f"{hm_k}（{'/'.join(parts)}）")

    # 配套核验提示
    warnings = []
    if c.get("enable_dingtalk") and not targets:
        warnings.append("您还没有指定目标钉钉群名，记得补上一句「发到 XX群」。")
    if not c.get("enable_dingtalk"):
        warnings.append("「自动发送到钉钉」目前未打开，如要发群请说「顺便开启自动发钉钉」。")

    lines = [
        "已根据您的指令自动改好以下设置并立即保存生效：",
        "• **实际改动项**：" + ("；".join(changes) if changes else "已按要求校准配置"),
        "",
        "### 请您核对一下，这样设置对不对？",
        f"1. **执行周期**：{day_desc}",
        f"2. **每天触发时间**：{tt_desc}",
        f"3. **要的数据日期**：表格 D3 日期 = **{d3_desc}**" + (f"（关键自定义格：{', '.join(cell_notes[:6])}）" if cell_notes else ""),
        f"4. **目标钉钉会话**：**{tgt_desc}**",
        f"5. **投递方式与等待**：{'自动打开钉钉发送' if c.get('enable_dingtalk') else '仅写表不发钉钉'}｜{silent_txt}｜{clock_txt}｜{send_mode_txt}｜切会话等 {delay_sec} 秒粘贴 · 贴后等 {send_wait_s} 秒发",
    ]
    if ov_notes:
        lines.append(f"6. **单点特殊规则**：{'；'.join(ov_notes[:8])}" + (f" 等共 {len(ov_notes)} 个点" if len(ov_notes) > 8 else ""))
    rs_cfg = c.get("raw_sync") if isinstance(c.get("raw_sync"), dict) else {}
    if rs_cfg.get("enabled"):
        rs_iv = int(rs_cfg.get("interval_min", 40) or 40)
        rs_skip = int(rs_cfg.get("skip_near_send_min", 5) or 0)
        rs_keep = "保留数据汇总其余格位" if rs_cfg.get("preserve_summary_cells", True) else "正常写入预设格位"
        rs_r4 = "仅写 R4 时间" if rs_cfg.get("write_r4_timestamp", True) else "不额外写 R4"
        skip_note = f"；临近发群 {rs_skip} 分钟内自动避让" if rs_skip > 0 else ""
        lines.append(f"• **后台定时刷底表**：已开启（每 {rs_iv} 分钟刷新三表，{rs_keep}，{rs_r4}{skip_note}）")
    lines.extend([
        "",
        "（提示：若您只是先预设方案还没开启自动定时跑，别忘了点首页左下角【自动值守】卡片启动值守；以上哪一项要修改，直接回复我一句话就能立刻帮您改好！）",
    ])
    if warnings:
        lines.append("⚠️ **自检提醒**：" + " ".join(warnings))
    return "\n".join(lines)


def _parse_cn_int(token, default=0):
    """把常见中文数字（一~二十四/两）或阿拉伯数字安全转为整数。"""
    s = str(token or "").strip()
    if not s:
        return default
    if s.isdigit():
        return int(s)
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if s in digits:
        return digits[s]
    if s == "十":
        return 10
    if s.startswith("十") and len(s) == 2 and s[1] in digits:
        return 10 + digits[s[1]]
    if s.endswith("十") and len(s) == 2 and s[0] in digits:
        return digits[s[0]] * 10
    if len(s) == 3 and s[1] == "十" and s[0] in digits and s[2] in digits:
        return digits[s[0]] * 10 + digits[s[2]]
    return default


def _extract_schedule_point_attrs(seg):
    """从自然语言片段提取日期格式、时间模式、天数偏移、静音状态、定向目标及自定义内容后缀。"""
    s = str(seg or "")
    if re.search(r"\d{4}年\d{1,2}月\d{1,2}日", s) or any(k in s for k in ("年月日", "中文日期")):
        d_fmt = "YYYY年M月D日"
    elif re.search(r"\d{4}-\d{1,2}-\d{1,2}", s) or any(k in s for k in ("横杠日期", "横杠格式", "横线格式")):
        d_fmt = "YYYY-MM-DD"
    elif re.search(r"(?<!\d)\d{1,2}月\d{1,2}日", s) or any(k in s for k in ("月日格式", "简短日期")):
        d_fmt = "M月D日"
    else:
        d_fmt = None

    wants_actual = bool(re.search(r"(?:[（(]\s*(?:比如|例如)[^）)]*\d{1,2}[:：]\d{2}|实时时间|实际时间|发送时间|当前时间|\+\s*时间)", s))
    if any(k in s for k in ("不加时间", "不带时间", "不带日期", "不要时间", "不要日期", "纯文案", "纯内容", "只发文字", "只发自定义")):
        t_mode = "none"
    elif wants_actual or re.search(r"(?:日期\s*[+加]\s*时间|年月日\s*[+加]\s*时间|带时间|加时间|\+\s*时间|几点几分)", s):
        t_mode = "datetime"
    elif any(k in s for k in ("只发日期", "仅发日期", "只要日期", "只看日期", "不带时钟")) or d_fmt is not None:
        t_mode = "date_only"
    else:
        t_mode = None

    suf = None
    m_explicit_q = re.search(
        r'''(?:自定义内容|发送内容|群消息词?|内容|文案|后缀|结尾词?|文字|备注)\s*(?:写|填|为|设为|改为|是)?\s*[:：]?\s*[「"'『]([^\r\n」"'』]{1,80})[」"'』]''',
        s,
    )
    if m_explicit_q:
        suf = m_explicit_q.group(1).strip()
    else:
        m_explicit_p = re.search(
            r"(?:自定义内容|发送内容|群消息词?|内容|文案|后缀|结尾词?)\s*(?:写|填|为|设为|改为|是)\s*[:：]?\s*([^，。,.;；\n]{1,40})",
            s,
        )
        if m_explicit_p:
            cand_p = re.sub(r"^(?:为|是|:|：)\s*", "", m_explicit_p.group(1)).strip()
            if cand_p and not any(bad in cand_p for bad in ("静音", "非静音", "不发钉钉", "日期+时间")):
                suf = cand_p
    if suf is None:
        m_suf = re.search(
            r"(?:\+\s*时间(?:\s*[（(][^）)]*[）)])?|\+\s*\d{4}[-年]\d{1,2}[-月]\d{1,2}日?|\d{4}[-年]\d{1,2}[-月]\d{1,2}日?)\s*(?:\+\s*|\s+)([\u4e00-\u9fffA-Za-z0-9_]{2,16})(?=\s*(?:[,，。.;；]|$|\s+都|\s+这|\s+不|\s+每))",
            s,
        )
        if m_suf:
            cand_s = m_suf.group(1).strip()
            if cand_s and not any(bad in cand_s for bad in ("都是", "不是", "静音", "发送", "然后", "每天", "图片", "比如")):
                suf = cand_s

    if any(k in s for k in ("前一天", "昨天", "昨日")):
        sh_off = -1
        msg_off = -1
    elif any(k in s for k in ("前两天", "前2天", "前天")):
        sh_off = -2
        msg_off = -2
    elif any(k in s for k in ("当时", "当天", "今天", "今日", "实时")):
        sh_off = 0
        msg_off = 0
    else:
        sh_off = None
        msg_off = None

    if any(k in s for k in ("都不是静音", "不是静音", "不静音", "不要静音", "不用静音", "非静音", "取消静音", "关闭静音", "常规提醒", "正常提醒")):
        sil = False
    elif "静音" in s:
        sil = True
    else:
        sil = None

    targets = None
    _tgt_verb = (
        r"(?<!不)(?:发送到|投递到|发到|发给|发送给|定向到|定向发给|"
        r"(?:发送?|投递)\s*(?:今天|今日|昨天|昨日|前一天|前两天|前天|当时|实时|当前|最新|\d{4}[-年]\d{1,2}[-月]\d{1,2}日?|\d{1,2}月\d{1,2}日)?(?:的)?(?:数据|汇总表|汇总|报表|表格|截图|图片|图|报告|内容|消息|通知){1,2}\s*(?:到|给))"
    )
    tgt_m = re.search(
        rf'''{_tgt_verb}\s*(?:钉钉)?\s*(?:[「"'『]([^」"'』\r\n]{{1,50}})[」"'』]|([^，。,.;；\r\n「"'『」"'』]{{1,50}}))''',
        s,
    )
    if tgt_m:
        raw_t = (tgt_m.group(1) or tgt_m.group(2) or "").strip()
        if not tgt_m.group(1):
            raw_t = re.sub(r"(?:的(?:定时|排期|时间点|节点|任务)|里面|中|去|里|看看|的)$", "", raw_t).strip()
            raw_t = re.split(
                r"(?:顺便|并|然后|延迟|等待|静音|非静音|不静音|日期|只发|不加|内容|文案|后缀|文字|格式|查昨天|查今天|前一天|当天|设置\d)",
                raw_t,
            )[0].strip()
        t_list = [x.strip()[:64] for x in re.split(r"[、,，和与]+", raw_t) if x.strip()]
        t_list = [x for x in t_list if x not in ("昨天数据", "今天数据", "数据", "图片", "日期")]
        if t_list:
            targets = t_list[:10]

    if any(k in s for k in ("不发钉钉", "只更新表格", "只更表", "只写表")):
        mode = "table_only"
    elif any(
        k in s
        for k in (
            "发钉钉", "正常发", "恢复发送", "恢复发群", "启用发送", "也要发", "并发送",
            "发给", "发送到", "发到", "投递到", "新增", "添加", "多加", "加一个", "加一条", "追加", "额外加", "补加",
        )
    ) or targets:
        mode = "send"
    else:
        mode = None
    return {
        "d_fmt": d_fmt,
        "t_mode": t_mode,
        "actual": wants_actual,
        "suf": suf,
        "sh_off": sh_off,
        "msg_off": msg_off,
        "sil": sil,
        "targets": targets,
        "mode": mode,
    }


_PERIOD_PREFIX_RE = r"(?:上午|早上|下午|晚上|晚间|夜间|夜里|凌晨)"


def _norm_hour_with_period(val_str, ctx_str=""):
    """统一解析中文或数字小时，并按时段前缀完成 12/24 小时制换算。"""
    hv = _parse_cn_int(val_str, -1)
    if hv < 0:
        return -1
    hv %= 24
    ctx = str(ctx_str or "")
    if any(k in ctx for k in ("下午", "晚上", "晚间", "夜间", "夜里")) and 1 <= hv <= 11:
        hv += 12
    elif "凌晨" in ctx and hv == 12:
        hv = 0
    return hv


def _parse_incremental_schedule_point_command(raw, new_cfg, changes):
    """增量添加或删除自定义定时节点，严格保留现有白天/夜间排期与其他节点不被覆盖。"""
    s = str(raw or "").strip()
    if not s:
        return False

    # 若包含起止时间区间或周期步长，交给多段/全量解析器处理，避免截断区间
    has_range = bool(re.search(
        rf"{_PERIOD_PREFIX_RE}?\s*(?:\d{{1,2}}\s*[:：]\s*\d{{1,2}}|(?:\d{{1,2}}|[零一二两三四五六七八九十]+)\s*[点时])\s*(?:到|至|-|~|～)\s*{_PERIOD_PREFIX_RE}?\s*(?:\d{{1,2}}|[零一二两三四五六七八九十]+)\s*(?:[:：点时])",
        s,
    )) or bool(re.search(r"每(?:隔)?\s*(?:\d+|[零一二两三四五六七八九十]+)\s*(?:个)?(?:小时|分钟?)", s))
    if has_range:
        return False

    has_del = any(k in s for k in ("删除", "删掉", "移除", "去掉", "取消该定时", "取消定时"))
    has_add = any(k in s for k in ("新增", "添加", "多加", "加一个", "加一条", "加个", "加条", "追加", "再加", "额外加", "补加", "单独加", "单独设", "单独改"))
    if not (has_del or has_add):
        return False

    # 剥离引号内自定义文案和日期字面量后再抽取触发时刻（避免把示例日期或文案里的数字误判成时间点）
    clean_for_tm = re.sub(r'''[「"'『][^」"'』\r\n]{0,90}[」"'』]''', " ", s)
    clean_for_tm = re.sub(r"[（(]\s*(?:比如|例如)[^）)]*[）)]", " ", clean_for_tm)
    clean_for_tm = re.sub(r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?", " ", clean_for_tm)
    clean_for_tm = re.sub(r"(?<!\d)\d{1,2}月\d{1,2}日", " ", clean_for_tm)

    pt_pat = re.compile(rf"({_PERIOD_PREFIX_RE})?\s*(\d{{1,2}}|[零一二两三四五六七八九十]+)\s*(?:[:：]\s*(\d{{1,2}})|[点时]\s*(?:(半)|(\d{{1,2}}|[零一二两三四五六七八九十]+)\s*分?)?)")
    hm_points = []
    for pm in pt_pat.finditer(clean_for_tm):
        hv = _norm_hour_with_period(pm.group(2), pm.group(1) or "")
        if not (0 <= hv <= 23):
            continue
        mv = 30 if pm.group(4) else _parse_cn_int(pm.group(3) or pm.group(5), 0)
        if 0 <= mv <= 59:
            hm_s = f"{hv:02d}:{mv:02d}"
            if hm_s not in hm_points:
                hm_points.append(hm_s)

    if not hm_points:
        return False

    extra_list = list(new_cfg.get("extra_points") or [])
    overrides = dict(new_cfg.get("point_overrides") or {})

    if has_del and not has_add:
        cur_extra = set(extra_list)
        if not any(ctx in s for ctx in ("定时", "排期", "时间点", "节点", "自定义")) and not any(hm in cur_extra or hm in overrides for hm in hm_points):
            return False
        removed_ep = []
        cleared_ov = []
        for hm_s in hm_points:
            if hm_s in extra_list:
                extra_list = [x for x in extra_list if x != hm_s]
                removed_ep.append(hm_s)
            if hm_s in overrides:
                overrides.pop(hm_s, None)
                cleared_ov.append(hm_s)
        new_cfg["extra_points"] = sorted(set(extra_list))
        new_cfg["point_overrides"] = overrides
        if removed_ep:
            changes.append("已从排期中删除自定义时间点：" + "、".join(removed_ep))
        elif cleared_ov:
            changes.append("已清除时间点单独设置（恢复常规排期默认）：" + "、".join(cleared_ov))
        else:
            changes.append(f"已检查时间点 {'、'.join(hm_points)}（该点属于白天/晚上规律区间，若需只保留其余点请微调起始/间隔）")
        return True

    # 增量添加 / 单独定制：先识别现有常规白天/夜间排期时刻，防止单独修改常规点时误写入 extra_points
    base_pts = {
        it["time"]
        for it in build_daily_timetable({**new_cfg, "extra_points": []})
        if isinstance(it, dict) and it.get("time")
    }
    attrs = _extract_schedule_point_attrs(s)
    added_pts = []
    updated_extra_pts = []
    updated_base_pts = []
    skipped_cap_pts = []
    for hm_s in hm_points:
        is_base = hm_s in base_pts
        is_existing_extra = hm_s in extra_list
        if not is_base and not is_existing_extra:
            if len(extra_list) >= 48:
                skipped_cap_pts.append(hm_s)
                continue
            extra_list.append(hm_s)
            added_pts.append(hm_s)
        elif is_base:
            updated_base_pts.append(hm_s)
        else:
            updated_extra_pts.append(hm_s)

        cur_ov = dict(overrides.get(hm_s) or {})
        eff_mode = cur_ov.get("mode", "default")
        if attrs["mode"] == "table_only":
            eff_mode = "table_only"
        elif attrs["mode"] == "send":
            eff_mode = "send"
        next_ov = {
            "mode": eff_mode,
            "suffix": attrs["suf"] if attrs["suf"] is not None else cur_ov.get("suffix"),
            "silent": attrs["sil"] if attrs["sil"] is not None else cur_ov.get("silent"),
            "sheet_day_offset": attrs["sh_off"] if attrs["sh_off"] is not None else cur_ov.get("sheet_day_offset"),
            "msg_time_mode": attrs["t_mode"] if attrs["t_mode"] is not None else cur_ov.get("msg_time_mode"),
            "msg_day_offset": attrs["msg_off"] if attrs["msg_off"] is not None else cur_ov.get("msg_day_offset"),
            "msg_date_fmt": attrs["d_fmt"] if attrs["d_fmt"] is not None else cur_ov.get("msg_date_fmt"),
            "targets": attrs["targets"] if attrs["targets"] is not None else cur_ov.get("targets"),
        }
        overrides[hm_s] = next_ov

    applied_pts = added_pts + updated_extra_pts + updated_base_pts
    if not applied_pts and skipped_cap_pts:
        changes.append(f"自定义时间点已达 48 个上限，未新增：{'、'.join(skipped_cap_pts)}（请先删除不用的自定义节点后再加）")
        return True

    if attrs["mode"] == "send" and applied_pts:
        new_cfg["enable_capture"] = True
    new_cfg["extra_points"] = sorted(set(extra_list))
    new_cfg["point_overrides"] = overrides

    detail_parts = []
    if attrs["targets"]:
        detail_parts.append("发给 " + "、".join(attrs["targets"]))
    if attrs["sh_off"] is not None:
        detail_parts.append("昨天数据" if attrs["sh_off"] == -1 else ("今天数据" if attrs["sh_off"] == 0 else f"偏移{attrs['sh_off']}天"))
    if attrs["t_mode"] == "date_only":
        detail_parts.append("只发日期" + (f"({attrs['d_fmt']})" if attrs["d_fmt"] else ""))
    elif attrs["t_mode"] == "datetime":
        detail_parts.append("日期+时间" + (f"({attrs['d_fmt']})" if attrs["d_fmt"] else ""))
    elif attrs["t_mode"] == "none":
        detail_parts.append("不加时间仅发内容")
    if attrs["suf"] is not None:
        detail_parts.append(f"内容「{attrs['suf']}」")
    if attrs["sil"] is True:
        detail_parts.append("静音")
    elif attrs["sil"] is False:
        detail_parts.append("正常提醒")

    act_lbl = (
        "增量添加自定义时间点" if added_pts
        else ("单独设置常规排期时间点" if updated_base_pts and not updated_extra_pts else "更新单点自定义设置")
    )
    changes.append(f"{act_lbl}：{'、'.join(applied_pts)}" + (f"（{' · '.join(detail_parts)}）" if detail_parts else "") + "；其余排期完整保留")
    if skipped_cap_pts:
        changes.append(f"自定义时间点已达 48 个上限，已跳过：{'、'.join(skipped_cap_pts)}")
    return True


def _parse_multi_segment_schedule_command(raw, new_cfg, changes):
    """解析包含多个时段、单点日期格式差异、不同步静音及实际发送时钟的复合排期指令。"""
    clauses = [
        c.strip()
        for c in re.split(r"(?:\n+|(?:^|[，,。.;；])\s*(?:然后|另外|再者)\s*(?=从|\d{1,2}\s*[:：]|每))", raw)
        if c and c.strip()
    ]
    if len(clauses) < 2:
        return False

    def _extract_hm_pair(txt_part):
        if not txt_part:
            return None
        txt_part = str(txt_part)
        m_colon = re.match(r"^\s*(\d{1,2})\s*[:：]\s*(\d{1,2})\s*$", txt_part)
        if m_colon:
            h, m = int(m_colon.group(1)), int(m_colon.group(2))
            return (h, m) if (0 <= h <= 23 and 0 <= m <= 59) else None
        m_cn = re.match(
            rf"^\s*({_PERIOD_PREFIX_RE})?\s*(\d{{1,2}}|[零一二两三四五六七八九十]+)\s*[点时]\s*(?:(半)|(\d{{1,2}}|[零一二两三四五六七八九十]+)\s*分?)?\s*$",
            txt_part,
        )
        if m_cn:
            h = _norm_hour_with_period(m_cn.group(2), m_cn.group(1) or "")
            if not (0 <= h <= 23):
                return None
            m = 30 if m_cn.group(3) else _parse_cn_int(m_cn.group(4), 0)
            return (h, max(0, min(59, m)))
        return None

    time_token = rf"(?:{_PERIOD_PREFIX_RE}?\s*(?:\d{{1,2}}\s*[:：]\s*\d{{1,2}}|(?:\d{{1,2}}|[零一二两三四五六七八九十]+)\s*[点时](?:\s*(?:半|(?:\d{{1,2}}|[零一二两三四五六七八九十]+)\s*分?))?))"
    range_pat = re.compile(rf"({time_token})\s*(?:到|至|-|~|～)\s*({time_token})")
    point_pat = re.compile(rf"({_PERIOD_PREFIX_RE})?\s*(\d{{1,2}}|[零一二两三四五六七八九十]+)\s*(?:[:：]\s*(\d{{1,2}})|[点时]\s*(?:(半)|(\d{{1,2}}|[零一二两三四五六七八九十]+)\s*分?)?)")
    specs = []
    for seg in clauses:
        attrs = _extract_schedule_point_attrs(seg)
        d_fmt = attrs["d_fmt"]
        t_mode = attrs["t_mode"]
        wants_actual = attrs["actual"]
        suf = attrs["suf"]
        sh_off = attrs["sh_off"]
        msg_off = attrs["msg_off"]
        sil = attrs["sil"]
        seg_targets = attrs.get("targets")
        seg_mode = attrs.get("mode")

        # 清除举例和日期字面量后再提取触发时间点
        clean_seg = re.sub(r"[（(]\s*(?:比如|例如)[^）)]*[）)]", " ", seg)
        clean_seg = re.sub(r"(?:比如|例如)[^，,。\n]*?(?=[，,。\n]|然后\s*到|$)", " ", clean_seg)
        clean_seg = re.sub(r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?", " ", clean_seg)
        clean_seg = re.sub(r"\d{1,2}月\d{1,2}日", " ", clean_seg)

        m_rng = range_pat.search(clean_seg)
        if m_rng:
            p1 = _extract_hm_pair(m_rng.group(1))
            p2 = _extract_hm_pair(m_rng.group(2))
            if p1 and p2:
                iv_m = re.search(r"每(?:隔)?\s*(?:(\d{1,2}|[零一二两三四五六七八九十]+)\s*(?:个)?小时)?\s*(?:(\d{1,2}|[零一二两三四五六七八九十]+)\s*分钟?)?", clean_seg)
                ih, im = 1, 0
                if iv_m and (iv_m.group(1) or iv_m.group(2)):
                    ih = _parse_cn_int(iv_m.group(1), 0)
                    im = _parse_cn_int(iv_m.group(2), 0)
                gap = max(1, ih * 60 + im)
                sm = p1[0] * 60 + p1[1]
                em = p2[0] * 60 + p2[1]
                span = (em - sm) if sm < em else ((1440 - sm + em) if sm > em else 1440)
                # 若起止刚好是步长整数倍，按“含尾点”自动将终点延后 1 分钟以纳入边界时刻（如 20:00 / 23:30）
                eh, em_v = p2[0], p2[1]
                if span > 0 and span % gap == 0:
                    em_inc = (em + 1) % 1440
                    eh, em_v = em_inc // 60, em_inc % 60
                    span += 1
                pts = []
                off_m = 0
                while off_m < span and len(pts) < 48:
                    pm_tot = (sm + off_m) % 1440
                    pts.append(f"{pm_tot // 60:02d}:{pm_tot % 60:02d}")
                    off_m += gap
                specs.append({
                    "kind": "range", "sh": p1[0], "sm": p1[1], "eh": eh, "em": em_v,
                    "ih": gap // 60, "im": gap % 60, "gap": gap, "points": pts,
                    "d_fmt": d_fmt, "t_mode": t_mode, "actual": wants_actual,
                    "suf": suf, "sh_off": sh_off, "msg_off": msg_off, "sil": sil,
                    "targets": seg_targets, "mode": seg_mode,
                })
                continue

        pts_single = []
        for pm in point_pat.finditer(clean_seg):
            hv = _norm_hour_with_period(pm.group(2), pm.group(1) or "")
            if not (0 <= hv <= 23):
                continue
            mv = 30 if pm.group(4) else _parse_cn_int(pm.group(3) or pm.group(5), 0)
            hm_s = f"{hv:02d}:{max(0, min(59, mv)):02d}"
            if hm_s not in pts_single:
                pts_single.append(hm_s)
        if pts_single:
            specs.append({
                "kind": "single", "points": pts_single,
                "d_fmt": d_fmt, "t_mode": t_mode, "actual": wants_actual,
                "suf": suf, "sh_off": sh_off, "msg_off": msg_off, "sil": sil,
                "targets": seg_targets, "mode": seg_mode,
            })

    if len(specs) < 2:
        return False

    # 选出点数最多的段作为全局默认基调
    main_spec = max(specs, key=lambda s: len(s["points"]))
    g_sil = main_spec["sil"] if main_spec["sil"] is not None else bool(new_cfg.get("dt_silent_send", False))
    g_tmode = main_spec["t_mode"] or new_cfg.get("dt_time_mode", "datetime")
    g_dfmt = main_spec["d_fmt"] or new_cfg.get("dt_date_fmt", "YYYY-MM-DD")
    g_moff = main_spec["msg_off"] if main_spec["msg_off"] is not None else 0
    g_soff = main_spec["sh_off"] if main_spec["sh_off"] is not None else 0
    g_actual = any(s["actual"] for s in specs)
    g_suf = main_spec["suf"] or next((s["suf"] for s in specs if s["suf"]), None)

    new_cfg["enable_capture"] = True
    new_cfg["enable_dingtalk"] = True
    new_cfg["dt_silent_send"] = bool(g_sil)
    new_cfg["dt_time_mode"] = g_tmode
    new_cfg["dt_date_fmt"] = g_dfmt
    new_cfg["dt_day_offset"] = g_moff
    new_cfg["dt_cutoff_enabled"] = False
    new_cfg["dt_use_actual_clock"] = bool(g_actual)
    if g_suf is not None:
        new_cfg["dt_suffix"] = g_suf

    # 更新表格默认查询日期 D3（不留旧单日偏移干扰）
    sp_items = new_cfg.setdefault("summary_presets", {}).setdefault("items", [])
    d3_it = next((it for it in sp_items if isinstance(it, dict) and str(it.get("cell", "")).upper() in ("D3", "D2")), None)
    if d3_it is not None:
        new_cfg["summary_presets"]["enabled"] = True
        d3_it["enabled"] = True
        d3_it["day_offset"] = g_soff

    # 将各个区间与单点写入 day / night / extra_points
    ranges = [s for s in specs if s["kind"] == "range"]
    singles = [s for s in specs if s["kind"] == "single"]
    extra_pts = []

    if ranges:
        day_r = dict(ranges[0])
        # 若恰好有一个单点紧邻第一区间前 1 个步长（如 09:00 接 10:00~20:01 每小时），直接无缝合并为同一时段
        still_singles = []
        for sng in singles:
            if len(sng["points"]) == 1:
                sh_p, sm_p = parse_hm(sng["points"][0])
                p_min = sh_p * 60 + sm_p
                d_start = day_r["sh"] * 60 + day_r["sm"]
                if p_min + day_r["gap"] == d_start:
                    day_r["sh"], day_r["sm"] = sh_p, sm_p
                    continue
            still_singles.append(sng)
        new_cfg["day"] = {
            "sh": day_r["sh"], "sm": day_r["sm"],
            "eh": day_r["eh"], "em": day_r["em"],
            "ih": day_r["ih"], "im": day_r["im"],
        }
        if len(ranges) >= 2:
            nr = ranges[1]
            new_cfg["night_enabled"] = True
            new_cfg["night"] = {"sh": nr["sh"], "sm": nr["sm"], "eh": nr["eh"], "em": nr["em"], "ih": nr["ih"], "im": nr["im"]}
            for ex_r in ranges[2:]:
                extra_pts.extend(ex_r["points"])
        else:
            new_cfg["night_enabled"] = False
        for sng in still_singles:
            extra_pts.extend(sng["points"])
    else:
        # 多组单点
        all_s = []
        for sng in singles:
            all_s.extend(sng["points"])
        h0, m0 = parse_hm(all_s[0])
        end0 = (h0 * 60 + m0 + 1) % 1440
        new_cfg["day"] = {"sh": h0, "sm": m0, "eh": end0 // 60, "em": end0 % 60, "ih": 1, "im": 0}
        new_cfg["night_enabled"] = False
        extra_pts.extend(all_s[1:])

    new_cfg["extra_points"] = sorted(set(extra_pts))

    # 重建精准单点覆盖表（仅记录与全局默认有差异的属性）
    overrides = {}
    for s in specs:
        for hm_k in s["points"]:
            ov = {}
            if s["sil"] is not None and bool(s["sil"]) != bool(g_sil):
                ov["silent"] = bool(s["sil"])
            if s["sh_off"] is not None and int(s["sh_off"]) != int(g_soff):
                ov["sheet_day_offset"] = int(s["sh_off"])
            if s["t_mode"] is not None and s["t_mode"] != g_tmode:
                ov["msg_time_mode"] = s["t_mode"]
            if s["msg_off"] is not None and int(s["msg_off"]) != int(g_moff):
                ov["msg_day_offset"] = int(s["msg_off"])
            if s["d_fmt"] is not None and s["d_fmt"] != g_dfmt:
                ov["msg_date_fmt"] = s["d_fmt"]
            if s["suf"] is not None and g_suf is not None and s["suf"] != g_suf:
                ov["suffix"] = s["suf"]
            if s.get("targets"):
                ov["targets"] = list(s["targets"])
            if s.get("mode") == "table_only":
                ov["mode"] = "table_only"
            elif s.get("mode") == "send" and len(specs) > 1 and any(x.get("mode") == "table_only" for x in specs):
                ov["mode"] = "send"
            if ov:
                overrides[hm_k] = {
                    "mode": ov.get("mode", "default"),
                    "suffix": ov.get("suffix"),
                    "silent": ov.get("silent"),
                    "sheet_day_offset": ov.get("sheet_day_offset"),
                    "msg_time_mode": ov.get("msg_time_mode"),
                    "msg_day_offset": ov.get("msg_day_offset"),
                    "msg_date_fmt": ov.get("msg_date_fmt"),
                    "targets": ov.get("targets"),
                }
    new_cfg["point_overrides"] = overrides
    total_pts = len(build_daily_timetable(new_cfg))
    changes.append(f"已解析 {len(specs)} 段排期规则（共 {total_pts} 个触发点，含 {len(overrides)} 个单点差异配置）")
    if g_actual:
        changes.append("开启定时文案使用实际发送实时时钟")
    return True


def _parse_raw_sync_command(raw, new_cfg, changes):
    """解析定时刷底表（每隔 N 分钟仅刷新三表 + R4，不动数据汇总其他格位）的开关与参数指令。"""
    s = str(raw or "").strip()
    if not s:
        return False

    has_raw_kw = any(k in s for k in (
        "刷底表", "三张表", "三表", "三个表", "三个数据表", "更新三表", "刷新三表",
        "只写R4", "只写r4", "不改数据汇总", "不要改数据汇总", "保留数据汇总",
    ))
    if not has_raw_kw:
        return False

    has_del_stop = any(k in s for k in (
        "关闭刷底表", "关闭定时刷底表", "关掉刷底表", "关掉定时刷底表",
        "停止刷底表", "停止定时刷底表", "取消刷底表", "取消定时刷底表",
    )) or bool(
        re.search(r"(?:关闭|关掉|停掉|停止|取消)\s*(?:每\s*(?:\d+|[零一二两三四五六七八九十]+)\s*(?:个)?(?:分钟?|小时))?\s*(?:自动|定时)?\s*(?:刷底表|刷新三表|更新三表)", s)
    )
    iv_min = None
    m_iv = re.search(
        r"每(?:隔)?\s*(?:(\d{1,3}|[零一二两三四五六七八九十]+)\s*(?:个)?小时)?\s*(?:(\d{1,3}|[零一二两三四五六七八九十]+)\s*分钟?)?",
        s,
    )
    if m_iv and (m_iv.group(1) or m_iv.group(2)):
        ih_v = _parse_cn_int(m_iv.group(1), 0) if m_iv.group(1) else 0
        im_v = _parse_cn_int(m_iv.group(2), 0) if m_iv.group(2) else 0
        tot_m = ih_v * 60 + im_v
        if tot_m > 0:
            iv_min = max(10, min(360, tot_m))

    is_pure_raw = has_del_stop or (iv_min is not None and not re.search(r"(?:到|至|-|~|～)\s*\d{1,2}\s*[:：点]", s)) or (
        any(k in s for k in ("只写R4", "只写r4", "不改数据汇总", "不要改数据汇总"))
        and any(k in s for k in ("开启", "打开", "启用", "关闭", "关掉", "取消", "刷新", "更新", "刷底表"))
    )
    if not is_pure_raw:
        return False

    rs = dict(new_cfg.get("raw_sync") or DEFAULT_CONFIG["raw_sync"])
    if has_del_stop:
        rs["enabled"] = False
        new_cfg["raw_sync"] = rs
        changes.append("已关闭后台定时刷底表（不影响现有发群排期）")
        return True

    rs["enabled"] = True
    if iv_min is not None:
        rs["interval_min"] = iv_min
    if any(k in s for k in ("不改", "不要改", "不动", "保留", "保持原样", "只写R4", "只写r4")):
        rs["preserve_summary_cells"] = True
    if "r4" in s.lower() or "粘贴时间" in s or "更新时间" in s:
        rs["write_r4_timestamp"] = True
    m_skip = re.search(r"(?:避让|临近发群|发群前)\s*(\d{1,2})\s*分钟?", s)
    if m_skip:
        rs["skip_near_send_min"] = max(0, min(30, int(m_skip.group(1))))
    new_cfg["raw_sync"] = rs
    changes.append(
        f"开启后台定时刷底表：每 {rs['interval_min']} 分钟更新三表"
        + (" · 保留数据汇总其余格位" if rs.get("preserve_summary_cells", True) else "")
        + (" · 仅写 R4 时间" if rs.get("write_r4_timestamp", True) else "")
    )
    return True


def try_handle_ai_config_command(user_text, cfg):
    """识别用户用自然语言直接设定定时计划、数据日期、单元格内容与钉钉发送目标的指令，并返回新配置与核对回复。"""
    raw = str(user_text or "").strip()
    if not raw or check_ai_security_and_creator_guardrail(raw):
        return {"handled": False}

    # 排除单纯询问“怎么设置/为什么/是不是”的问题（除非带了明确的修改动作+时间或发送目标）
    question_only = ("怎么设置", "如何设置", "在哪设置", "哪里设置", "为什么", "能不能", "你是谁", "分析", "总结", "对比", "复盘")
    has_setter_verb = any(k in raw for k in (
        "帮我设", "给我设", "帮我改", "给我改", "帮我调", "帮我安排", "我要设", "我要设置", "想设置",
        "请设置", "设置为", "设为", "改成", "改为", "调整为", "定在", "定为", "配置成",
        "关闭静音", "取消静音", "关掉静音",
        "新增", "添加", "多加", "加一个", "加一条", "加个", "加条", "追加", "再加", "额外加", "补加", "单独加", "单独设", "单独改",
        "删除定时", "删掉定时", "移除定时", "去掉定时", "删除时间点", "删掉时间点", "移除时间点", "删除节点", "删掉节点",
    )) or (
        any(k in raw for k in ("删除", "删掉", "移除", "去掉"))
        and any(ctx in raw for ctx in ("定时", "排期", "时间点", "节点", "自定义"))
    )
    has_send_cmd = (
        any(k in raw for k in ("发送到", "发到", "投递到", "发给", "发送给")) and any(k in raw for k in ("点", ":", "：", "每天", "每周", "周", "工作日", "昨天", "今天", "群", "助手"))
    ) or bool(re.search(r"(?:发送前|发前|贴完|贴图.{0,4}后).{0,6}\d+\s*秒", raw))
    has_raw_sync_cmd = any(k in raw for k in (
        "关闭刷底表", "关闭定时刷底表", "关掉刷底表", "关掉定时刷底表", "取消刷底表", "取消定时刷底表",
    )) or (
        any(k in raw for k in ("刷底表", "三张表", "三表", "三个表", "三个数据表", "更新三表", "刷新三表", "只写R4", "只写r4", "不改数据汇总", "不要改数据汇总"))
        and (
            any(k in raw for k in ("每隔", "关闭", "关掉", "取消", "开启", "打开", "启用", "恢复", "粘贴"))
            or bool(re.search(r"每\s*(?:\d+|[零一二两三四五六七八九十]+)\s*(?:个)?(?:小时|分钟?)", raw))
        )
    )
    if any(q in raw for q in question_only) and not (has_setter_verb or has_raw_sync_cmd):
        return {"handled": False}
    if not (has_setter_verb or has_send_cmd or has_raw_sync_cmd):
        return {"handled": False}

    import copy
    new_cfg = normalize_config(copy.deepcopy(cfg if isinstance(cfg, dict) else {}))
    changes = []

    # 0. 先检查是否为单点增量添加/删除自定义时间节点：严格局部修改，绝不覆写现有整体排期或全局默认群
    if _parse_incremental_schedule_point_command(raw, new_cfg, changes):
        new_cfg = normalize_config(new_cfg)
        return {
            "handled": True,
            "new_cfg": new_cfg,
            "changes": changes,
            "reply": _build_config_verification_summary(new_cfg, changes),
        }

    # 0.5 检查是否为后台定时刷底表（每隔 N 分钟仅刷新三表 + 写 R4）设置/开关指令
    if _parse_raw_sync_command(raw, new_cfg, changes):
        new_cfg = normalize_config(new_cfg)
        return {
            "handled": True,
            "new_cfg": new_cfg,
            "changes": changes,
            "reply": _build_config_verification_summary(new_cfg, changes),
        }

    # 1. 解析运行日期周期
    wk_map = {"一": 0, "1": 0, "二": 1, "2": 1, "三": 2, "3": 2, "四": 3, "4": 3, "五": 4, "5": 4, "六": 5, "6": 5, "日": 6, "天": 6, "7": 6}
    rng_m = re.search(r"(?:周|星期)([一二三四五六日天1-7])\s*(?:到|至|-|~)\s*(?:周|星期)?([一二三四五六日天1-7])", raw)
    if "工作日" in raw or (rng_m and wk_map.get(rng_m.group(1)) == 0 and wk_map.get(rng_m.group(2)) == 4):
        new_cfg["day_mode"] = "workday"
        changes.append("运行周期设为「工作日(周一至周五)」")
    elif "非工作日" in raw or "周末" in raw or (rng_m and wk_map.get(rng_m.group(1)) == 5 and wk_map.get(rng_m.group(2)) == 6):
        new_cfg["day_mode"] = "weekend"
        changes.append("运行周期设为「周末(周六/周日)」")
    elif rng_m:
        s_d = wk_map.get(rng_m.group(1), 0)
        e_d = wk_map.get(rng_m.group(2), 4)
        days = list(range(s_d, e_d + 1)) if s_d <= e_d else (list(range(s_d, 7)) + list(range(0, e_d + 1)))
        new_cfg["day_mode"] = "custom"
        new_cfg["custom_days"] = sorted(set(days))
        changes.append(f"运行星期设为周{rng_m.group(1)}至周{rng_m.group(2)}")
    else:
        found_days = []
        for dm in re.finditer(r"(?:每周|周|星期)([一二三四五六日天、,，和及与\s]+)", raw):
            for ch in dm.group(1):
                if ch in wk_map and wk_map[ch] not in found_days:
                    found_days.append(wk_map[ch])
        if found_days:
            new_cfg["day_mode"] = "custom"
            new_cfg["custom_days"] = sorted(found_days)
            wn = ["一", "二", "三", "四", "五", "六", "日"]
            changes.append("运行星期设为「" + "、".join("周" + wn[i] for i in new_cfg["custom_days"]) + "」")
        elif "每天" in raw or "每日" in raw:
            new_cfg["day_mode"] = "daily"
            changes.append("运行周期设为「每天」")

    # 2. 先尝试复合多段排期解析；不命中多段时走传统单段简洁解析
    multi_matched = _parse_multi_segment_schedule_command(raw, new_cfg, changes)

    def _norm_h(h_val, ctx_str=""):
        hv = int(h_val) % 24
        if any(k in ctx_str for k in ("下午", "晚上", "晚间", "夜里")) and 1 <= hv <= 11:
            hv += 12
        return hv

    if not multi_matched:
        range_t = re.search(
            r"(上午|早上|下午|晚上|夜间)?\s*(\d{1,2})\s*(?:[:：]\s*(\d{1,2})|[点时]\s*(?:(半)|(\d{1,2})\s*分?)?)\s*(?:到|至|-|~|～)\s*(上午|早上|下午|晚上|夜间)?\s*(\d{1,2})\s*(?:[:：]\s*(\d{1,2})|[点时]\s*(?:(半)|(\d{1,2})\s*分?)?)?",
            raw,
        )
        if range_t:
            sh = _norm_h(range_t.group(2), range_t.group(1) or "")
            sm = 30 if range_t.group(4) else int(range_t.group(3) or range_t.group(5) or 0)
            eh = _norm_h(range_t.group(7), range_t.group(6) or "")
            em = 30 if range_t.group(9) else int(range_t.group(8) or range_t.group(10) or 0)
            new_cfg["day"]["sh"], new_cfg["day"]["sm"] = sh, max(0, min(59, sm))
            new_cfg["day"]["eh"], new_cfg["day"]["em"] = eh, max(0, min(59, em))
            iv_m = re.search(r"每(?:隔)?\s*(?:(\d+)\s*(?:个)?小时)?\s*(?:(\d+)\s*分钟?)?", raw)
            if iv_m and (iv_m.group(1) or iv_m.group(2)):
                new_cfg["day"]["ih"] = int(iv_m.group(1) or 0)
                new_cfg["day"]["im"] = int(iv_m.group(2) or 0)
            if "夜" not in raw and "晚间" not in raw:
                new_cfg["night_enabled"] = False
            changes.append(f"运行时间段设为 {sh:02d}:{new_cfg['day']['sm']:02d} ~ {eh:02d}:{new_cfg['day']['em']:02d}（间隔 {new_cfg['day']['ih']}h{new_cfg['day']['im']}m）")
        else:
            single_pts = []
            for tm in re.finditer(r"(上午|早上|下午|晚上|夜间)?\s*(\d{1,2})\s*(?:[:：]\s*(\d{1,2})|[点时]\s*(?:(半)|(\d{1,2})\s*分?)?)", raw):
                hv = _norm_h(tm.group(2), tm.group(1) or "")
                mv = 30 if tm.group(4) else int(tm.group(3) or tm.group(5) or 0)
                if 0 <= hv <= 23 and 0 <= mv <= 59 and (hv, mv) not in single_pts:
                    single_pts.append((hv, mv))
            if single_pts:
                # 单个定点设置：让 eh/em 紧接 +1 分钟（span=1分钟），排期表精准只出这一个时间点！
                h0, m0 = single_pts[0]
                end_tot = (h0 * 60 + m0 + 1) % 1440
                new_cfg["day"] = {"sh": h0, "sm": m0, "eh": end_tot // 60, "em": end_tot % 60, "ih": 1, "im": 0}
                new_cfg["night_enabled"] = False
                if len(single_pts) > 1:
                    h_last, m_last = single_pts[-1]
                    new_cfg["night_enabled"] = True
                    end2 = (h_last * 60 + m_last + 1) % 1440
                    new_cfg["night"] = {"sh": h_last, "sm": m_last, "eh": end2 // 60, "em": end2 % 60, "ih": 1, "im": 0}
                    if len(single_pts) > 2:
                        mid_strs = [f"{hm[0]:02d}:{hm[1]:02d}" for hm in single_pts[1:-1]]
                        new_cfg["extra_points"] = sorted(set(mid_strs))[:48]
                    pts_lbl = "、".join(f"{hm[0]:02d}:{hm[1]:02d}" for hm in single_pts)
                    changes.append(f"定点触发设为 {pts_lbl}")
                else:
                    changes.append(f"每日定点执行设为 {h0:02d}:{m0:02d}")

    # 3. 解析要什么数据与自定义单元格（多段复合模式下已精确处理全局/单点数据日期）
    sp_items = new_cfg.setdefault("summary_presets", {}).setdefault("items", [])
    if not multi_matched:
        if any(k in raw for k in ("昨天数据", "昨天的数据", "前一天数据", "昨日数据", "减1天", "减一天")):
            new_cfg["summary_presets"]["enabled"] = True
            d3 = next((it for it in sp_items if isinstance(it, dict) and str(it.get("cell", "")).upper() in ("D3", "D2")), None)
            if d3 is not None:
                d3["enabled"] = True
                d3["day_offset"] = -1
            changes.append("表格查询日期(D3)设为「昨天(-1天)」")
        elif any(k in raw for k in ("今天数据", "今天的数据", "当天数据", "今日数据")):
            new_cfg["summary_presets"]["enabled"] = True
            d3 = next((it for it in sp_items if isinstance(it, dict) and str(it.get("cell", "")).upper() in ("D3", "D2")), None)
            if d3 is not None:
                d3["enabled"] = True
                d3["day_offset"] = 0
            changes.append("表格查询日期(D3)设为「当天(0天)」")

    for cm in re.finditer(r"([A-Z]{1,3}\d+(?::[A-Z]{1,3}\d+)?)\s*(?:格|单元格)?\s*(?:写入|填入|填|设为|改成|改为)\s*[「\"'『]?([^\s，。,.;；\n」\"'』]{1,36})[」\"'』]?", raw):
        c_addr = cm.group(1).upper()
        c_val = cm.group(2).strip()
        if c_val:
            new_cfg["summary_presets"]["enabled"] = True
            hit = next((it for it in sp_items if isinstance(it, dict) and str(it.get("cell", "")).upper() == c_addr), None)
            if hit is not None:
                hit["enabled"] = True
                hit["action"] = "clear" if c_val in ("空", "清空") else "value"
                if c_val not in ("空", "清空"):
                    hit["value"] = c_val
            else:
                sp_items.append({
                    "enabled": True, "sheet": "数据汇总", "cell": c_addr, "label": "AI指令设定",
                    "action": "clear" if c_val in ("空", "清空") else "value",
                    "value": "" if c_val in ("空", "清空") else c_val,
                })
            changes.append(f"单元格 {c_addr} 写入设为「{c_val}」")

    # 4. 解析目标钉钉群与投递参数
    tgt_m = re.search(r"(?:发送到|投递到|发到|发给|发送给|到群|在群)\s*[「\"『]?([^，。,.;；\n」\"'』]{2,40})[」\"'』]?", raw)
    if tgt_m:
        raw_t = re.sub(r"(?:里面|中|去|里|群里)$", "", tgt_m.group(1).strip()).strip()
        # 剔除可能连带抓到的数据/方式词
        raw_t = re.split(r"(?:顺便|并|然后|延迟|等待|静音)", raw_t)[0].strip()
        t_names = [x.strip() for x in re.split(r"[、,，和与]+", raw_t) if x.strip()]
        if t_names:
            new_cfg["dt_targets"] = [{"name": n, "enabled": True} for n in t_names]
            new_cfg["dt_search"] = t_names[0]
            new_cfg["enable_dingtalk"] = True
            if not any(k in raw for k in ("不自动发", "不发", "暂不发送", "只粘贴")):
                new_cfg["dt_auto_send"] = True
            changes.append("目标钉钉会话设为「" + "、".join(t_names) + "」并开启自动投递")

    if "仅截图" in raw or "只截图" in raw:
        new_cfg["enable_capture"] = True
    if not multi_matched:
        if any(k in raw for k in ("关闭静音", "关掉静音", "关静音", "取消静音", "不静音", "不要静音", "不用静音", "常规提醒")):
            new_cfg["dt_silent_send"] = False
            changes.append("发送方式设为「常规提醒(关闭静音，贴完后不点静音按钮)」")
        elif "静音" in raw:
            new_cfg["enable_dingtalk"] = True
            new_cfg["dt_silent_send"] = True
            changes.append("发送方式设为「静音发送」")

    send_wait_m = re.search(r"(?:发送前|发前|贴完|写完|贴图.{0,6}后).{0,8}?(?:等待?|延迟)\s*(\d{1,2})\s*秒", raw)
    if send_wait_m:
        sw_v = max(0, min(30, int(send_wait_m.group(1))))
        new_cfg["dt_send_wait_sec"] = sw_v
        changes.append(f"贴完发前等待设为 {sw_v} 秒")

    paste_delay_src = re.sub(r"(?:发送前|发前|贴完|写完|贴图.{0,6}后).{0,8}?(?:等待?|延迟)\s*\d{1,2}\s*秒", "", raw)
    delay_m = re.search(r"(?:粘贴|打开聊天|等待|延迟)\s*(\d{1,2})\s*秒", paste_delay_src)
    if delay_m:
        sec_v = max(0, min(20, int(delay_m.group(1))))
        new_cfg["dt_paste_delay_sec"] = sec_v
        changes.append(f"粘贴前等待设为 {sec_v} 秒")

    ft_m = re.search(r"(?:重点(?:关注|看|统计)|只看|聚焦)\s*[「\"']?([\u4e00-\u9fffA-Za-z0-9]{2,12}组)[」\"']?", raw)
    if ft_m:
        new_cfg.setdefault("ai_assistant", {})["focus_team"] = ft_m.group(1)
        changes.append(f"AI 重点关注组设为「{ft_m.group(1)}」")

    if not changes:
        return {"handled": False}

    new_cfg = normalize_config(new_cfg)
    return {
        "handled": True,
        "new_cfg": new_cfg,
        "changes": changes,
        "reply": _build_config_verification_summary(new_cfg, changes),
    }


def summarize_report_with_ai(cfg, style_id="brief", user_prompt="", chat_history=None, extra_img_path=None):
    """内置全能 AI 管家：回答软件身份/操作/状态、安全护栏拦截、自动设置排期、解读高清汇总图并结合历次档案综合分析。"""
    t_ai_start = time.monotonic()
    norm = normalize_config(cfg if isinstance(cfg, dict) else {})
    ai_cfg = norm.get("ai_assistant") or {}
    focus_team = str(ai_cfg.get("focus_team") or "").strip()
    styles_map = build_ai_summary_styles(focus_team)
    sid = str(style_id or "brief").strip().lower()
    style_meta = styles_map.get(sid)

    raw_user = str(user_prompt or "").strip()
    thinking_steps = []
    if style_meta and sid != "custom":
        thinking_steps.append(f"理解需求：按「{style_meta['name']}」生成分析")
    elif raw_user:
        thinking_steps.append(f"理解问题：{raw_user[:28]}")
    else:
        thinking_steps.append("理解需求：准备总结当前汇总数据")

    guard_reply = check_ai_security_and_creator_guardrail(raw_user)
    if guard_reply:
        thinking_steps.append("触发安全与权限边界校验，已直接给出合规答复")
        elapsed_g = round(max(0.0, time.monotonic() - t_ai_start), 1)
        return {
            "ok": True,
            "source": "guardrail",
            "model": "",
            "shot_info": "安全边界与版权声明",
            "thinking_steps": thinking_steps,
            "elapsed_sec": elapsed_g,
            "reply": guard_reply,
        }

    _need_visual_img = (
        bool(extra_img_path)
        or sid != "custom"
        or not raw_user
        or any(
            k in raw_user.lower()
            for k in (
                "表", "图", "截图", "汇总", "数据", "团队", "小组", "排行", "排名",
                "注册", "转化", "华人", "暗号", "渠道", "对比", "增量", "变化",
                "总结", "汇报", "简报", "业绩", "趋势", "昨天", "今天", "本周", "点评", "复盘",
            )
        )
    )
    ctx = collect_report_context_for_ai(
        include_prev_shot=(sid == "delta"),
        extra_img_path=extra_img_path,
        encode_images=_need_visual_img,
    )
    encoded_imgs = [im for im in (ctx.get("images") or []) if im.get("b64")]
    if encoded_imgs:
        thinking_steps.append(f"读取汇总表截图「{encoded_imgs[0]['name']}」（视觉轻量化压缩）")
    else:
        thinking_steps.append("走纯文本快答通道（免传大图，优先关联知识库与实时配置）")

    custom_tail = str(ai_cfg.get("custom_summary_prompt") or "").strip()
    effective_prompt = raw_user
    if not effective_prompt:
        if style_meta:
            effective_prompt = style_meta["prompt"]
        else:
            effective_prompt = styles_map["brief"]["prompt"]
    elif style_meta and sid != "custom":
        effective_prompt = f"参考【{style_meta['name']}】风格（{style_meta['prompt']}），并重点按我的要求输出：{effective_prompt}"
    if custom_tail and sid != "custom":
        effective_prompt += f"\n补充长期要求：{custom_tail}"

    arch_ctx = build_archive_query_context(effective_prompt, focus_team=focus_team)
    cfg_snap = _safe_ai_cfg_snapshot(norm)
    data_brief = get_dashboard_data_brief()
    recent_logs = _get_recent_logs_tail(18)
    recent_logs_txt = "\n".join(recent_logs)
    is_troubleshoot_q = any(k in effective_prompt.lower() for k in ("报错", "失败", "为什么", "第一次", "第二次", "怎么改", "代码", "antigravity", "顺序"))
    hist_diff = build_historical_failure_diff_context(norm, _last_diagnosed_detail, recent_logs=recent_logs) if is_troubleshoot_q else ""
    ag_prompt = build_antigravity_prompt(norm, _last_diagnosed_detail, hist_ctx_text=hist_diff, recent_logs=recent_logs) if is_troubleshoot_q else ""
    shot_info = ctx["latest_shot_label"] or (
        f"历史档案 ({data_brief['archive_count']} 轮)" if data_brief.get("has_archive") else "软件内置知识与配置态"
    )
    if ctx["prev_shot_label"]:
        shot_info += f" 对比 {ctx['prev_shot_label']}"

    focus_rule = f"（当前用户指定重点关注【{focus_team}】）" if focus_team else "（当前未锁定单个团队，请公平客观评价所有参与计算的小组，严禁偏向任何一组）"
    sys_prompt = (
        "你是桌面自动化软件「暗号表助手 (SheetMate)」专属内置的智能小猫伙伴与运营数据分析师。\n"
        f"{_SHEETMATE_AI_KNOWLEDGE}\n"
        "回答原则：\n"
        "1. 绝对不要说出任何外部 AI 公司、外部大模型或接口名称（不要提 Google/Gemini/GPT 等），若问你是谁，回答我是「暗号表助手专属内置智能小猫伙伴」；仅当用户明确询问软件是谁设计的、谁开发的或作者是谁时，才回答由【Soren Drake】设计研发，平时不要主动提及；\n"
        "2. 严禁回答或透露任何密码、口令、权限配置、后台入口或他人隐私信息，被问到直接告知无权读取与访问；\n"
        "3. 若用户问怎么操作、怎么改配置或某功能怎么用，请结合上方软件说明书给出具体到菜单页签和按钮名称的大白话步骤；\n"
        f"4. 若用户总结数据、点名追问某个成员或某个团队的数据与趋势{focus_rule}，务必结合表格高清截图、后台明细表与【持久化历次数据档案】给出的真实数字回答，严禁编造数字；\n"
        "5. 你拥有正式授权的内置软件驱动权：严禁说“我没有后台/底层/静默执行权限/不能操作键鼠”，也严禁在未附带动作标签时口头假装“已开始执行”。若用户明确发出立刻跑表、截图发钉钉、文字直发、开启/停止自动值守或暂停/恢复/取消当前任务指令（含上下文确认如“好，按刚说的直接开始”），请在回复末尾单独一行附带动作标签：`[[SM_ACTION:{\"action\":\"run_full|capture_send|text_send|start_schedule|stop_schedule|pause_task|resume_task|cancel_task\",\"target\":\"\",\"date\":\"\"}]]`（未指定对象或日期留空）；普通问答、查数、问教程或寒暄严禁附带该标签。\n"
        "6. 全程使用亲切有感情的真人同事口吻，条理清晰。"
    )

    contents = []
    mem_ctx_str = build_ai_user_memory_context()
    if data_brief.get("has_archive") or mem_ctx_str:
        thinking_steps.append(f"检索历次数据档案（共 {data_brief.get('archive_count', 0)} 轮）与个人习惯记忆")
    first_parts = [{
        "text": (
            f"【实时软件配置快照】{json.dumps(cfg_snap, ensure_ascii=False)}\n"
            f"【顶部数据看板摘要】{json.dumps(data_brief, ensure_ascii=False)}\n"
            + (f"【本机用户长期习惯与纠错记忆（务必严格遵守）】\n{mem_ctx_str}\n" if mem_ctx_str else "")
            + f"【持久化历次表格档案检索上下文】\n{arch_ctx}\n"
            + (f"【历次运行成败对比分析】\n{hist_diff}\n" if hist_diff else "")
            + (f"【发给 Antigravity 的改代码参考模板】\n{ag_prompt}\n" if ag_prompt else "")
            + (f"【最近运行日志节选】\n{recent_logs_txt}\n" if recent_logs_txt else "")
        )
    }]
    for idx, im_item in enumerate(ctx["images"]):
        if not im_item.get("b64"):
            continue
        label = "【最新一轮汇总表高清截图" if idx == 0 else "【上一轮对比汇总表高清截图"
        first_parts.append({"text": f"{label}（拍摄时间：{im_item['mtime_str']}，文件：{im_item['name']}）】"})
        first_parts.append({"inlineData": {"mimeType": im_item.get("mime_type") or "image/png", "data": im_item["b64"]}})
    if ctx["tsv_text"]:
        first_parts.append({"text": f"【桌面明细表精确文本摘录】\n{ctx['tsv_text']}"})

    hist = [it for it in (chat_history or []) if isinstance(it, dict) and str(it.get("text") or "").strip()]
    if hist:
        first_parts.append({"text": "以下是此前历史对话脉络，图片与实时数据以上文【user】提供为准。"})
        contents.append({"role": "user", "parts": first_parts})
        for turn in hist:
            t_role = "model" if turn.get("role") == "model" else "user"
            t_txt = str(turn.get("text"))
            if contents and contents[-1]["role"] == t_role:
                contents[-1]["parts"].append({"text": t_txt})
            else:
                contents.append({"role": t_role, "parts": [{"text": t_txt}]})
        if contents and contents[-1]["role"] == "user":
            contents[-1]["parts"].append({"text": effective_prompt})
        else:
            contents.append({"role": "user", "parts": [{"text": effective_prompt}]})
    else:
        first_parts.append({"text": effective_prompt})
        contents.append({"role": "user", "parts": first_parts})

    call_res = _call_gemini_generate(norm, sys_prompt, contents, temperature=0.35, max_tokens=1800, timeout_sec=24, max_models=2, total_budget_sec=42)
    elapsed_s = round(max(0.0, time.monotonic() - t_ai_start), 1)

    if call_res.get("ok"):
        clean_reply, parsed_ai_action = parse_and_strip_ai_action_tag(call_res["reply"])
        thinking_steps.append(f"完成云端推理与数据核对（用时 {elapsed_s}s）")
        ret = {
            "ok": True,
            "source": call_res["source"],
            "model": call_res["model"],
            "shot_info": shot_info,
            "thinking_steps": thinking_steps,
            "elapsed_sec": elapsed_s,
            "reply": clean_reply,
        }
        if parsed_ai_action:
            ret["ai_action"] = parsed_ai_action
        return ret

    err_msg = call_res.get("error") or "请求失败"
    thinking_steps.append(f"云端未响应，已切换内置离线档案分析引擎（用时 {elapsed_s}s）")
    return {
        "ok": True,
        "source": "local_fallback",
        "model": "",
        "api_error": err_msg,
        "shot_info": shot_info,
        "thinking_steps": thinking_steps,
        "elapsed_sec": elapsed_s,
        "reply": _local_smart_chat_fallback(
            norm,
            effective_prompt,
            arch_ctx,
            api_err=err_msg,
            hist_diff=hist_diff,
            ag_prompt=ag_prompt,
        ),
    }


def ask_gemini_assistant(cfg, mode="diagnose", error_detail=None, recent_logs=None, question=""):
    """调用内置云端/本地智能分析引擎生成大白话报错复盘（含历史成败对比）、模块顺序自调建议及改代码提示词。"""
    norm = normalize_config(cfg if isinstance(cfg, dict) else {})
    ai_cfg = norm.get("ai_assistant") or {}
    if mode == "ask" and question:
        guard_r = check_ai_security_and_creator_guardrail(question)
        if guard_r:
            return {
                "ok": True,
                "source": "guardrail",
                "model": "",
                "hist_summary": "",
                "recommended_tuning": {"has_tuning": False, "patch": {}, "summary": "", "reasons": []},
                "antigravity_prompt": "",
                "reply": guard_r,
            }
    cfg_snap = _safe_ai_cfg_snapshot(norm)
    err_d = error_detail if isinstance(error_detail, dict) else {}
    logs_list = [str(x) for x in (recent_logs or _get_recent_logs_tail(28))[-28:]]
    logs_tail = "\n".join(logs_list)

    hist_diff = build_historical_failure_diff_context(norm, err_d, recent_logs=logs_list)
    tuning_rec = recommend_pipeline_tuning_for_error(norm, err_d, hist_diff) if err_d.get("code") else {"has_tuning": False, "patch": {}, "summary": "", "reasons": []}
    ag_prompt = build_antigravity_prompt(norm, err_d, hist_ctx_text=hist_diff, recent_logs=logs_list) if (mode == "diagnose" or err_d.get("code")) else ""

    if not ai_cfg.get("enabled", True):
        return {
            "ok": True,
            "source": "disabled",
            "model": "",
            "hist_summary": hist_diff,
            "recommended_tuning": tuning_rec,
            "antigravity_prompt": ag_prompt,
            "reply": _local_ai_fallback_reply(
                mode, err_d, cfg_snap, question=question,
                hist_ctx=hist_diff, ag_prompt=ag_prompt, tuning_rec=tuning_rec,
            ),
        }

    sys_prompt = (
        "你是桌面自动化软件「暗号表助手 (SheetMate)」专属内置的智能小猫伙伴与运营故障分析师。\n"
        f"{_SHEETMATE_AI_KNOWLEDGE}\n"
        "重要要求：\n"
        "1. 绝对不要说出任何外部 AI 公司、外部大模型或接口名称（禁止提及 Google、Gemini、GPT、DeepSeek 等），若问你是谁，回答我是「暗号表助手专属内置智能小猫伙伴」；仅当用户明确询问软件是谁设计的、谁开发的或作者是谁时，才回答由【Soren Drake】设计研发，平时不要主动提及；\n"
        "2. 严禁回答或透露任何密码、口令、权限配置、后台入口或他人隐私信息，被问到直接告知无权读取与访问；\n"
        "3. 必须用极度通俗易懂、像真人同事一样温暖亲切的【大白话】回答，绝对不要只丢干瘪英文术语。\n"
        "4. 报错排障（mode=diagnose）时必须结合【历次运行成败对比】和【源码模块地图】严格按以下四段结构输出（每段清晰实用）：\n"
        "   ### ① 这次到底为什么卡住了？\n"
        "   ### ② 为什么之前能成功，这次却失败了？（历史对比）\n"
        "   ### ③ 软件自己怎么调执行顺序马上好？\n"
        "   ### ④ 怎么跟 Antigravity 沟通改源码？（点明要改哪几个模块及顺序怎么调）\n"
        "5. 配置优化（mode=suggest）时，结合 WPS 自定义单元格写入、钉钉粘贴等待秒数、发后最小化钉钉与内部执行顺序给 3~4 条防踩坑建议。\n"
    )

    if mode == "suggest":
        mem_ctx_s = build_ai_user_memory_context()
        user_prompt = (
            f"请帮我全面审核当前配置，指出潜在踩坑并给出 3~4 条大白话优化建议：\n"
            f"配置摘要：{json.dumps(cfg_snap, ensure_ascii=False)}\n"
            + (f"【本机用户长期习惯】\n{mem_ctx_s}\n" if mem_ctx_s else "")
            + f"历次运行统计：\n{hist_diff}\n"
            + (f"附加补充要求：{question}\n" if question else "")
        )
    elif mode == "ask":
        mem_ctx_s = build_ai_user_memory_context()
        user_prompt = (
            f"用户提问：{question or '请介绍当前如何配置最稳'}\n"
            f"当前配置摘要：{json.dumps(cfg_snap, ensure_ascii=False)}\n"
            + (f"【本机用户长期习惯与偏好（务必贴合他的习惯回答）】\n{mem_ctx_s}\n" if mem_ctx_s else "")
            + (f"历次运行统计：\n{hist_diff}\n" if hist_diff else "")
            + (f"最近报错：{json.dumps(err_d, ensure_ascii=False)}\n" if err_d.get("code") else "")
            + (f"最近运行日志：\n{logs_tail}\n" if logs_tail else "")
        )
    else:
        user_prompt = (
            f"程序运行刚才报错或卡住了，请结合历史成败记录对比分析：\n"
            f"错误代码：{err_d.get('code', 'ERR-RUN-900')}｜标题：{err_d.get('title', '运行异常')}\n"
            f"原始错误：{err_d.get('raw_error', '')}\n"
            f"异常位置：{err_d.get('location', '未知')}\n"
            f"当前配置与执行顺序：{json.dumps(cfg_snap, ensure_ascii=False)}\n"
            f"【历次运行成败自动对比】\n{hist_diff}\n"
            f"【软件自检模块调优建议】{tuning_rec.get('summary')}\n"
            + (f"【最近运行日志】\n{logs_tail}\n" if logs_tail else "")
            + (f"【用户补充描述】{question}\n" if question else "")
        )

    call_res = _call_gemini_generate(
        norm,
        sys_prompt,
        [{"role": "user", "parts": [{"text": user_prompt}]}],
        temperature=0.35,
        max_tokens=1100,
        timeout_sec=20,
    )
    if call_res.get("ok"):
        reply_txt = call_res["reply"]
        if mode == "diagnose" or err_d.get("code"):
            ag_prompt = build_antigravity_prompt(
                norm, err_d, hist_ctx_text=hist_diff, recent_logs=logs_list, ai_code_advice=reply_txt[:450],
            )
        return {
            "ok": True,
            "source": call_res["source"],
            "model": call_res["model"],
            "hist_summary": hist_diff,
            "recommended_tuning": tuning_rec,
            "antigravity_prompt": ag_prompt,
            "reply": reply_txt,
        }

    last_err = call_res.get("error") or "接口未响应"
    fallback = _local_ai_fallback_reply(
        mode, err_d, cfg_snap, question=question,
        hist_ctx=hist_diff, ag_prompt=ag_prompt, tuning_rec=tuning_rec,
    )
    return {
        "ok": True,
        "source": "local_fallback",
        "model": "",
        "api_error": last_err,
        "hist_summary": hist_diff,
        "recommended_tuning": tuning_rec,
        "antigravity_prompt": ag_prompt,
        "reply": f"{fallback}\n\n（当前已为您切换本地离线智能分析模式生成诊断）",
    }


_AI_HEAL_ALLOWED_ACTIONS = (
    "activate_dingtalk_im",
    "dismiss_popups",
    "focus_chat_input",
    "enable_silent_uia",
    "click_send_now",
    "retry_paste",
    "reopen_search",
    "wps_escape_retry",
    "page_reload_retry",
)


def _capture_live_debug_context(stage, exc=None, extra=None, page=None):
    """采集卡住现场的桌面/网页截图及 Win32 前台窗口遥测，供 AI 看图诊断并制定自愈动作。"""
    import base64
    extra = extra or {}
    img_b64 = ""
    try:
        raw_png = None
        if page is not None:
            raw_png = page.screenshot(type="png")
        elif os.name == "nt":
            from PIL import ImageGrab
            grab_im = None
            im = None
            im_rsz = None
            buf = None
            try:
                grab_im = ImageGrab.grab()
                if grab_im is not None:
                    im = grab_im.convert("RGB")
                    t_im = im
                    if max(im.width, im.height) > 1280:
                        ratio = 1280.0 / float(max(im.width, im.height))
                        im_rsz = im.resize(
                            (max(1, int(im.width * ratio)), max(1, int(im.height * ratio))),
                            Image.Resampling.LANCZOS,
                        )
                        t_im = im_rsz
                    buf = io.BytesIO()
                    t_im.save(buf, format="PNG", optimize=True)
                    img_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            finally:
                for obj in (buf, im_rsz, im, grab_im):
                    if obj is not None:
                        try:
                            obj.close()
                        except Exception:
                            pass
        if raw_png:
            im = None
            im_rsz = None
            buf = None
            raw_buf = None
            try:
                raw_buf = io.BytesIO(raw_png)
                with Image.open(raw_buf) as raw_im:
                    im = raw_im.convert("RGB")
                t_im = im
                if max(im.width, im.height) > 1280:
                    ratio = 1280.0 / float(max(im.width, im.height))
                    im_rsz = im.resize(
                        (max(1, int(im.width * ratio)), max(1, int(im.height * ratio))),
                        Image.Resampling.LANCZOS,
                    )
                    t_im = im_rsz
                buf = io.BytesIO()
                t_im.save(buf, format="PNG", optimize=True)
                img_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            finally:
                for obj in (buf, im_rsz, im, raw_buf):
                    if obj is not None:
                        try:
                            obj.close()
                        except Exception:
                            pass
    except Exception:
        pass

    fg_hwnd = 0
    fg_cls = ""
    fg_title = ""
    fg_is_aux = False
    im_wins_info = []
    search_open = False
    has_complex_modal = False
    if os.name == "nt":
        try:
            u32 = ctypes.WinDLL("user32", use_last_error=True)
            fg_hwnd = int(u32.GetForegroundWindow() or 0)
            if fg_hwnd:
                cbuf = ctypes.create_unicode_buffer(128)
                tbuf = ctypes.create_unicode_buffer(256)
                u32.GetClassNameW(fg_hwnd, cbuf, 128)
                u32.GetWindowTextW(fg_hwnd, tbuf, 256)
                fg_cls, fg_title = cbuf.value, tbuf.value
                fg_is_aux = _is_dingtalk_aux_window(fg_cls, fg_title)
        except Exception:
            pass
        try:
            for hw, pid, cls, ttl, sc in _enum_dingtalk_im_windows(include_hidden=True)[:5]:
                im_wins_info.append({"hwnd": hw, "pid": pid, "cls": cls, "title": ttl, "score": sc})
            search_open = _is_search_popup_open()
            has_complex_modal = bool(_enum_top_windows(target_classes=("ddesign::ComplexModal",), target_titles=None))
        except Exception:
            pass

    return {
        "stage": str(stage or ""),
        "error": str(exc or ""),
        "extra": dict(extra),
        "img_b64": img_b64,
        "telemetry": {
            "foreground": {"hwnd": fg_hwnd, "cls": fg_cls, "title": fg_title, "is_dt_aux_window": fg_is_aux},
            "dingtalk_im_windows": im_wins_info,
            "search_popup_open": search_open,
            "complex_modal_open": has_complex_modal,
            "recent_logs": _get_recent_logs_tail(18),
        },
    }


def _local_heuristic_auto_heal_plan(stage, exc, ctx, cfg):
    """离线或 AI 超时情况下的确定性现场自愈规则，保障钉钉误窗、富文本未发送与弹窗卡阻可自动恢复。"""
    st = str(stage or "").lower()
    err_l = str(exc or "").lower()
    telem = ctx.get("telemetry") or {}
    fg = telem.get("foreground") or {}
    extra = ctx.get("extra") or {}
    auto_send = bool((cfg or {}).get("dt_auto_send", False))

    if st in ("dingtalk_search", "dingtalk_paste"):
        if fg.get("is_dt_aux_window"):
            acts = ["dismiss_popups", "activate_dingtalk_im", "reopen_search" if st == "dingtalk_search" else "retry_paste"]
            return {
                "observation": f"检测到当前误停在钉钉附属窗口「{fg.get('title') or fg.get('cls')}」，未切回聊天主窗。",
                "can_auto_fix": True,
                "actions": acts,
                "summary": "关闭附属浮层并强力唤醒钉钉聊天主窗后自动续跑。",
            }
        if st == "dingtalk_paste" and (
            extra.get("image_pasted") and extra.get("text_pasted")
            or any(k in err_l for k in ("静音", "uia", "no windows", "menu", "菜单", "send", "发送", "箭头"))
        ):
            acts = ["dismiss_popups", "activate_dingtalk_im", "enable_silent_uia"]
            if auto_send:
                acts.append("click_send_now")
            else:
                acts.append("focus_chat_input")
            return {
                "observation": "表格图片与日期文案已贴入输入框，但静音切换/富文本发送按钮触发受阻。",
                "can_auto_fix": True,
                "actions": acts,
                "summary": "清除弹出遮挡、直连聊天主窗设静音后点击发送按钮完成投递。",
            }
        acts = ["dismiss_popups", "activate_dingtalk_im", "reopen_search" if st == "dingtalk_search" else "retry_paste"]
        return {
            "observation": "检测到搜索窗、模态弹窗或窗口焦点偏移影响了当前步骤。",
            "can_auto_fix": True,
            "actions": acts,
            "summary": "复位窗口焦点与弹窗后自动重试当前步骤。",
        }

    if st in ("wps_capture", "wps_paste"):
        return {
            "observation": "WPS 在线表格页面可能被提示窗或旧选区焦点阻挡。",
            "can_auto_fix": True,
            "actions": ["wps_escape_retry"],
            "summary": "发送 Escape 清理遮挡弹窗并重新校准页面缩放后自动重试。",
        }

    if st == "luya_download":
        return {
            "observation": "数据后台表格渲染较慢导致下载按钮未及时出现。",
            "can_auto_fix": True,
            "actions": ["page_reload_retry"],
            "summary": "刷新当前数据页面并延长等待后重试下载。",
        }

    return {
        "observation": "当前异常依赖人工确认现场。",
        "can_auto_fix": False,
        "actions": [],
        "summary": "需人工介入处理。",
    }


def ai_auto_debug_and_heal(cfg, stage, exc=None, extra=None, page=None, force=False):
    """遇阻时自动截屏看现场并由 Gemini/本地专家生成安全受控的自动调试修复动作。"""
    global _last_ai_heal_report
    norm = normalize_config(cfg if isinstance(cfg, dict) else {})
    ai_cfg = norm.get("ai_assistant") or {}
    st = str(stage or "").strip().lower()
    err_s = str(exc or "").strip()

    if not force and (not ai_cfg.get("enabled", True) or not ai_cfg.get("auto_heal", True)):
        return {"attempted": False, "can_auto_fix": False, "actions": [], "observation": "", "summary": "", "source": "disabled"}

    if st in ("net_offline", "vpn_blocked", "luya_login", "dingtalk_login") or "权限不一致" in err_s:
        return {
            "attempted": False,
            "can_auto_fix": False,
            "actions": [],
            "observation": "当前属于网络/VPN/扫码登录或管理员权限不一致问题，需要人工完成。",
            "summary": "不执行自动键鼠重试，交由人工处理。",
            "source": "skipped_manual",
        }

    try:
        set_step("AI 正在看图诊断并自动调试…")
        log("  🤖 [AI 自动调试] 检测到步骤受阻，正在截屏观察现场并尝试自动修复…")
    except Exception:
        pass

    ctx = _capture_live_debug_context(st, exc, extra=extra, page=page)
    plan = None
    src = "local_heuristic"
    model_used = ""

    if ai_cfg.get("enabled", True):
        sys_prompt = (
            "你是桌面自动化助手 SheetMate 的内置现场调试专家。\n"
            "请结合现场截图与窗口遥测 JSON 判断到底哪里卡住了，并从允许动作白名单中选择修复动作，严格返回纯 JSON：\n"
            '{"observation":"一句话大白话说明看到的问题","can_auto_fix":true,"actions":["activate_dingtalk_im"],"summary":"简短修复方案"}\n'
            f"允许动作白名单仅限：{json.dumps(list(_AI_HEAL_ALLOWED_ACTIONS), ensure_ascii=False)}。\n"
            "注意：如果 foreground.is_dt_aux_window 为 true 说明误停在钉钉-文档等子窗；"
            "如果已贴图且见富文本工具栏/静音受阻，优先 activate_dingtalk_im + enable_silent_uia + click_send_now。"
        )
        parts = []
        if ctx.get("img_b64"):
            parts.append({"inlineData": {"mimeType": "image/png", "data": ctx["img_b64"]}})
        parts.append({
            "text": (
                f"当前阶段：{st}\n"
                f"报错信息：{err_s[:260]}\n"
                f"配置摘要：dt_auto_send={norm.get('dt_auto_send')}, dt_silent_send={norm.get('dt_silent_send')}\n"
                f"窗口与日志遥测：{json.dumps(ctx.get('telemetry') or {}, ensure_ascii=False)}"
            )
        })
        try:
            call_res = _call_gemini_generate(
                norm,
                sys_prompt,
                [{"role": "user", "parts": parts}],
                temperature=0.1,
                max_tokens=450,
                timeout_sec=12,
            )
            if call_res.get("ok"):
                raw_txt = str(call_res.get("reply") or "").strip()
                raw_txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_txt, flags=re.IGNORECASE | re.DOTALL).strip()
                parsed = _parse_first_json_dict(raw_txt)
                if isinstance(parsed, dict) and "observation" in parsed:
                    valid_acts = [a for a in (parsed.get("actions") or []) if a in _AI_HEAL_ALLOWED_ACTIONS]
                    plan = {
                        "observation": str(parsed.get("observation") or "").strip(),
                        "can_auto_fix": bool(parsed.get("can_auto_fix")) and bool(valid_acts),
                        "actions": valid_acts,
                        "summary": str(parsed.get("summary") or "").strip(),
                    }
                    src = "ai_vision"
                    model_used = ""
        except Exception:
            plan = None

    if plan is None:
        plan = _local_heuristic_auto_heal_plan(st, exc, ctx, norm)
        src = "local_heuristic"

    # 若图片和文字均已粘贴好且配置了自动发送，补充保底发送动作确保不卡在“贴完不点发送”
    ext_meta = ctx.get("extra") or {}
    if (
        st == "dingtalk_paste"
        and norm.get("dt_auto_send")
        and ext_meta.get("image_pasted")
        and ext_meta.get("text_pasted")
        and plan.get("can_auto_fix")
        and "click_send_now" not in plan["actions"]
        and "retry_paste" not in plan["actions"]
    ):
        plan["actions"].append("click_send_now")

    res = {
        "attempted": True,
        "stage": st,
        "source": src,
        "model": model_used,
        "observation": plan.get("observation", ""),
        "can_auto_fix": bool(plan.get("can_auto_fix")) and bool(plan.get("actions")),
        "actions": [a for a in (plan.get("actions") or []) if a in _AI_HEAL_ALLOWED_ACTIONS],
        "summary": plan.get("summary", ""),
        "time": time.strftime("%H:%M:%S"),
    }
    _last_ai_heal_report = res
    if res["observation"]:
        log(f"  🤖 [AI 现场诊断] {res['observation']}")
    if res["can_auto_fix"]:
        log(f"  🛠️ [AI 自愈动作] {' -> '.join(res['actions'])}（{res['summary']}）")
    return res


def diagnose_issue(stage, exc=None, extra=None, probe_fn=probe_network):
    """根据当前阶段、报错内容与网络探测结果，返回含结构化报错代码的排障说明与恢复按钮。"""
    extra = extra or {}
    err_s = str(exc or "").strip()
    low = err_s.lower()
    net_err_keys = (
        "err_internet_disconnected",
        "err_name_not_resolved",
        "err_proxy_connection_failed",
        "err_tunnel_connection_failed",
        "err_timed_out",
        "err_connection_timed_out",
        "err_connection_reset",
        "err_connection_closed",
        "err_connection_refused",
        "net::err",
        "timeout",
    )
    is_net_err = any(k in low for k in net_err_keys)

    net = None
    if stage in ("luya_open", "luya_login", "luya_download", "wps_open", "wps_paste", "wps_capture"):
        try:
            net = probe_fn()
        except Exception:
            net = {"internet_ok": True, "luya_ok": True, "wps_ok": True}

    def _with_code(res, st_override=None, extra_override=None):
        global _last_diagnosed_detail
        det = classify_error_detail(
            exc,
            stage=st_override or stage,
            extra=extra_override if extra_override is not None else extra,
        )
        res["error_detail"] = det
        res["code"] = det["code"]
        with _runtime_lock:
            _last_diagnosed_detail = det
        return res

    # 1. 本机彻底断网
    if ("err_internet_disconnected" in low or "err_name_not_resolved" in low
            or (net is not None and not net.get("internet_ok", True))):
        return _with_code({
            "title": "[ERR-NET-101] 电脑未连接网络，无法继续抓取",
            "detail": (
                "1. 请检查 Wi-Fi 或网线是否正常连网，先在浏览器确认能正常打开网页；\n"
                "2. 连上网后点击【已连网·重试当前步】，前面已完成的部分无需重跑。"
            ),
            "hint": "按键提示：连好网络点【已连网·重试当前步】，或点【取消】终止本轮。",
            "actions": [
                {"id": "retry", "label": "已连网·重试当前步", "style": "primary"},
            ],
        }, st_override="net_offline", extra_override={**extra, "internet_ok": False})

    # 2. 已连网但未开 VPN / 代理未连通 Luya 后台
    if stage in ("luya_open", "luya_login", "luya_download") and net is not None:
        proxy_or_timeout = any(k in low for k in (
            "err_proxy", "err_tunnel", "err_timed_out", "err_connection_timed_out",
            "err_connection_reset", "err_connection_refused", "timeout", "net::err",
        ))
        if (not net.get("luya_ok", True)) or (is_net_err and proxy_or_timeout):
            return _with_code({
                "title": "[ERR-VPN-102] 已连网络，但未开启 VPN 或无法连接后台",
                "detail": (
                    "电脑外网正常，但访问 Luya 数据后台（bb.luyaapp.live）超时或被拦截。\n"
                    "1. 请先打开 VPN 软件并确保节点连接成功（确认系统代理可用）；\n"
                    "2. 开启 VPN 后点击【已开VPN·重试当前步】，直接接着当前步抓取！"
                ),
                "hint": "按键提示：打开 VPN 并连上节点后点【已开VPN·重试当前步】继续。",
                "actions": [
                    {"id": "retry", "label": "已开VPN·重试当前步", "style": "primary"},
                ],
            }, st_override="vpn_blocked", extra_override={**extra, "luya_ok": False})

    # 3. 后台登录未通过 / 登录失效
    if stage == "luya_login" or extra.get("luya_need_login"):
        return _with_code({
            "title": "[ERR-LUYA-103] Luya 后台需要手动确认登录",
            "detail": (
                "1. 请查看已弹出的浏览器窗口，如停留在登录页或验证码页，请手动完成登录；\n"
                "2. 登录成功进入后台后点击【我已登录·继续】，或点【重试自动登录】。"
            ),
            "hint": "按键提示：手动登录完成后点【我已登录·继续】，直接去下载数据。",
            "actions": [
                {"id": "continue", "label": "我已登录·继续", "style": "primary"},
                {"id": "retry", "label": "重试自动登录", "style": "secondary"},
            ],
        }, st_override="luya_login")

    # 4. Luya 数据打开或下载失败
    if stage == "luya_open":
        return _with_code({
            "title": "[ERR-VPN-102] 打开 Luya 数据后台页面失败",
            "detail": (
                f"原因：{human_error(err_s)}\n"
                "1. 请检查网络与 VPN 是否开启且节点稳定；\n"
                "2. 确认后点击【重试打开后台】即可继续。"
            ),
            "hint": "按键提示：确认网络/VPN 正常后点【重试打开后台】继续。",
            "actions": [
                {"id": "retry", "label": "重试打开后台", "style": "primary"},
            ],
        })

    if stage == "luya_download":
        tbl_name = extra.get("name") or "数据表"
        return _with_code({
            "title": f"[ERR-LUYA-104] 【{tbl_name}】下载未能成功",
            "detail": (
                f"前面已下好的表会保留，仅【{tbl_name}】未下下来。\n"
                "1. 请检查 VPN/网络是否稳定，可在浏览器中看下页面是否已刷出表格；\n"
                "2. 确认后点击【重试下载当前表】，无需重新下载前面已完成的表。"
            ),
            "hint": f"按键提示：检查 VPN 后点【重试下载当前表】单独重抓【{tbl_name}】。",
            "actions": [
                {"id": "retry", "label": "重试下载当前表", "style": "primary"},
            ],
        })

    # 5. WPS 打开 / 写入失败
    if stage == "wps_open":
        return _with_code({
            "title": "[ERR-WPS-201] 打开或登录 WPS 在线表格失败",
            "detail": (
                f"原因：{human_error(err_s)}\n"
                "1. 请查看浏览器中的 WPS 页面，确保网络正常并已完成扫码登录；\n"
                "2. 表格可编辑后点击【重试打开/检测WPS】，已下载的三份数据无需重抓。"
            ),
            "hint": "按键提示：在浏览器确认 WPS 表格打开并登录后点【重试打开/检测WPS】。",
            "actions": [
                {"id": "retry", "label": "重试打开/检测WPS", "style": "primary"},
            ],
        })

    if stage == "wps_paste":
        tbl_name = extra.get("name") or "工作表"
        return _with_code({
            "title": f"[ERR-WPS-202] 写入 WPS【{tbl_name}】工作表失败",
            "detail": (
                f"原因：{human_error(err_s)}\n"
                f"1. 请查看浏览器中【{tbl_name}】标签页是否可见、无弹窗遮挡；\n"
                f"2. 处理好后点【重试写入当前表】，或点【跳过此表·下一步】继续后续流程。"
            ),
            "hint": "按键提示：检查工作表后点【重试写入当前表】继续写入，点【取消】终止本轮。",
            "actions": [
                {"id": "retry", "label": "重试写入当前表", "style": "primary"},
                {"id": "skip", "label": "跳过此表·下一步", "style": "secondary"},
            ],
        })

    # 6. WPS 汇总表截图失败
    if stage == "wps_capture":
        det_cap = classify_error_detail(exc, stage="wps_capture", extra=extra)
        return _with_code({
            "title": f"[{det_cap['code']}] WPS 数据汇总表截图未成功",
            "detail": (
                f"浏览器仍保持打开且数据已写入（原因：{human_error(err_s)}）。\n"
                "方案A：点开浏览器的【数据汇总】页关掉遮挡弹窗后，点【重试自动截图】；\n"
                "方案B：您也可以自己在表里框选 C6:AK 右键「复制为图片」，再点【我已手动复制·发钉钉】。"
            ),
            "hint": "按键提示：修好页面点【重试自动截图】，或手动复制图片后点【我已手动复制·发钉钉】。",
            "actions": [
                {"id": "retry", "label": "重试自动截图", "style": "primary"},
                {"id": "manual_copied", "label": "我已手动复制·发钉钉", "style": "secondary"},
                {"id": "skip", "label": "跳过截图发钉钉", "style": "secondary"},
            ],
        })

    # 6.5 钉钉使用中被用户切换、关闭或主动让出键鼠
    if stage == "dingtalk_interrupted":
        target = extra.get("search_name") or "目标群聊"
        idx = int(extra.get("idx") or 1)
        total = int(extra.get("total") or 1)
        prog = f"（当前进度 {idx}/{total}：「{target}」）" if total > 1 else f"（目标会话：「{target}」）"
        reason_txt = extra.get("reason") or (human_error(err_s) if err_s else "检测到您关闭或正在使用钉钉")
        acts = [
            {"id": "retry", "label": "继续未完成步骤 (F8)", "style": "primary"},
            {"id": "skip_search", "label": "已开群·直接粘贴 (F9)", "style": "secondary"},
        ]
        if extra.get("can_skip_target"):
            acts.append({"id": "skip_target", "label": f"跳过「{target[:8]}」·发下一个", "style": "secondary"})
        return _with_code({
            "title": "[ERR-DT-305] 检测到钉钉被关闭或您正在使用，已自动暂停",
            "detail": (
                f"{reason_txt}{prog}。\n"
                "已截好的表格图片与未完成步骤全部安全保留，不会重抓：\n"
                "1. 忙完后按键盘【F8】或点【继续未完成步骤 (F8)】，自动接着完成剩余发送；\n"
                "2. 若您已手动打开该群窗口，按【F9】或点【已开群·直接粘贴 (F9)】直接贴图。"
            ),
            "hint": "快捷键：按【F8】继续未完成步骤 · 按【F9】在当前聊天窗直接粘贴 · 点【取消】终止",
            "actions": acts,
        })

    # 7. 钉钉未启动 / 停留在扫码登录页
    if stage == "dingtalk_login":
        return _with_code({
            "title": "[ERR-DT-301] 钉钉未启动或尚未扫码登录",
            "detail": (
                "刚刚截好的表格图片已安全保存，不会丢失！\n"
                "1. 请先在电脑上打开钉钉并扫码登录，看到聊天消息主界面；\n"
                "2. 登录成功后按【F8】或点击【已登录钉钉·继续发送】，图片将直接贴到群里（无需重抓表格）。"
            ),
            "hint": "按键提示：钉钉扫码登录成功后按【F8】或点【已登录钉钉·继续发送】直接继续。",
            "actions": [
                {"id": "retry", "label": "已登录钉钉·继续发送", "style": "primary"},
            ],
        })

    # 8. 钉钉未搜索到目标群聊
    if stage == "dingtalk_search":
        target = extra.get("search_name") or "目标群聊"
        acts = [
            {"id": "skip_search", "label": "已手动开群·直接粘贴", "style": "primary"},
            {"id": "retry", "label": "重新搜索群聊", "style": "secondary"},
        ]
        if extra.get("can_skip_target"):
            acts.append({"id": "skip_target", "label": f"跳过「{target[:8]}」·发下一个", "style": "secondary"})
        return _with_code({
            "title": f"[ERR-DT-303] 钉钉未能自动搜到会话「{target}」",
            "detail": (
                "表格图片已安全保存，不会丢失！您可以任选一种方式继续：\n"
                "方案A（推荐）：直接在钉钉里手动点开目标群聊/联系人窗口，然后按【F9】或点【已手动开群·直接粘贴】；\n"
                "方案B：确认名称正确或切回钉钉主窗后，点【重新搜索群聊】。"
            ),
            "hint": "按键提示：手动开群后按【F8】/【F9】或点【已手动开群·直接粘贴】；也可点【重新搜索群聊】。",
            "actions": acts,
        })

    # 9. 钉钉粘贴或输入框焦点受阻
    if stage == "dingtalk_paste":
        target = extra.get("search_name") or "目标群聊"
        acts = [
            {"id": "retry_paste", "label": "直接粘贴重试", "style": "primary"},
            {"id": "retry", "label": "重新搜索群聊", "style": "secondary"},
        ]
        if extra.get("can_skip_target"):
            acts.append({"id": "skip_target", "label": f"跳过「{target[:8]}」·发下一个", "style": "secondary"})
        return _with_code({
            "title": f"[ERR-DT-304] 钉钉粘贴到「{target}」时受阻",
            "detail": (
                f"表格图片仍已保留（原因：{human_error(err_s)}）。\n"
                "1. 请在钉钉点开要发送的目标聊天窗口，关闭遮挡小窗；\n"
                "2. 确认后按【F8】/【F9】或点【直接粘贴重试】即可继续发送，或点【重新搜索群聊】。"
            ),
            "hint": "按键提示：点开聊天窗口后按【F8】/【F9】或点击【直接粘贴重试】继续发送。",
            "actions": acts,
        })

    det_def = classify_error_detail(exc, stage=stage, extra=extra)
    return _with_code({
        "title": f"[{det_def['code']}] {det_def['title']}",
        "detail": f"原因：{det_def['plain_reason']}\n请按指引处理后点击【重试当前步】继续。",
        "hint": "按键提示：处理完成后点【重试当前步】继续，或点【取消】终止本轮。",
        "actions": [
            {"id": "retry", "label": "重试当前步", "style": "primary"},
        ],
    })


def wait_for_recovery(title, detail, hint="", actions=None, timeout_sec=900, error_detail=None):
    """遇到可恢复阻塞时处理选择：除必须人工登录外默认自动选确定继续，多次自动换策略重试，达上限后记入 AI 训练档案。"""
    global _waiting_recovery, _recovery_choice, _runtime_prompt
    check_stop()
    act_list = [dict(a) for a in (actions or [{"id": "retry", "label": "重试当前步", "style": "primary"}])]
    title_str = str(title or "需要处理")
    need_human_login = any(k in title_str for k in ("登录", "扫码", "验证码"))
    auto_ok = True
    try:
        auto_ok = bool(load_config().get("auto_confirm_choices", True))
    except Exception:
        auto_ok = True
    if auto_ok and not need_human_login and act_list:
        auto_id = None
        auto_lbl = ""
        next_attempt = 0
        exhausted_cnt = None
        with _AUTO_CONFIRM_LOCK:
            cur_cnt = int(_auto_confirmed_recovery_counts.get(title_str, 0))
            if cur_cnt < 3:
                prefer_order = ("retry", "retry_paste", "continue", "ok", "yes", "confirm") if cur_cnt >= 1 else (
                    "retry_paste", "retry", "continue", "ok", "yes", "confirm"
                )
                auto_act = None
                for pid in prefer_order:
                    auto_act = next((a for a in act_list if str(a.get("id") or "") == pid), None)
                    if auto_act is not None:
                        break
                if auto_act is None:
                    auto_act = next(
                        (a for a in act_list if str(a.get("id") or "") not in ("cancel", "skip_search", "skip", "skip_target")),
                        None,
                    )
                if auto_act is not None:
                    next_attempt = cur_cnt + 1
                    _auto_confirmed_recovery_counts[title_str] = next_attempt
                    _auto_confirmed_recovery_titles.add(title_str)
                    auto_id = str(auto_act.get("id") or "retry")
                    auto_lbl = str(auto_act.get("label") or auto_id)
            else:
                exhausted_cnt = cur_cnt
        if auto_id is not None:
            try:
                record_run_trace_event("auto_confirm", f"{title_str}->{auto_id}", {"attempt": next_attempt})
            except Exception:
                pass
            log(f"▶ 已按默认自动确定规则选择「{auto_lbl}」继续执行（第 {next_attempt}/3 次自动确认，无需手动点击）")
            sleep_check(0.35)
            return auto_id
        if exhausted_cnt is not None:
            raise RuntimeError(f"{title_str}：已按默认确定规则自动重试 {exhausted_cnt} 次仍未恢复，请检查现场或查看 AI 复盘分析")
    with _runtime_lock:
        det = error_detail or (_last_diagnosed_detail if _last_diagnosed_detail and _last_diagnosed_detail.get("code") in str(title or "") else None)
        if det is None:
            det = classify_error_detail(Exception(f"{title or ''} {detail or ''}"))
        _waiting_recovery = True
        _recovery_choice = None
        _runtime_prompt = {
            "mode": "waiting_fix",
            "title": str(title or "需要处理"),
            "detail": str(detail or ""),
            "hint": str(hint or "处理完成后点击恢复按钮继续，或点击【取消】结束本轮。"),
            "actions": act_list,
            "error_detail": det,
        }
    try:
        try:
            set_step(f"暂停待处理：{title}")
        except Exception:
            pass
        log(f"⚠️ {title} — {str(detail or '').replace(chr(10), ' ')}")
        try:
            record_run_journal(load_config(), status="paused_err", mode="waiting_fix", round_label="待排障", error_detail=det)
        except Exception:
            pass
        _notify_runtime_state()
        deadline = time.monotonic() + max(10.0, float(timeout_sec))
        with _runtime_cond:
            while time.monotonic() < deadline:
                if _stop_requested:
                    raise TaskStoppedError("用户手动停止了任务")
                choice = _recovery_choice
                if choice is not None:
                    if choice == "cancel":
                        raise TaskStoppedError("用户手动停止了任务")
                    log(f"▶ 正在执行恢复操作：{choice}")
                    return choice
                _runtime_cond.wait(timeout=0.25)
        raise RuntimeError(f"等待处理超时（超过 {int(timeout_sec // 60)} 分钟未响应）：{title}")
    finally:
        with _runtime_cond:
            _waiting_recovery = False
            _recovery_choice = None
            if not _stop_requested and _runtime_prompt.get("mode") != "stopped":
                _runtime_prompt = _apply_hands_off_to_prompt(_default_running_prompt())
            _runtime_cond.notify_all()
        _notify_runtime_state()


def fetch_remote_version():
    """联网取最新版本号与下载链接；严格校验证书与重定向目标，未配置或失败返回 None。"""
    if not UPDATE_CHECK_URL or not _validate_safe_http_url(UPDATE_CHECK_URL, require_https=True):
        return None
    try:
        req = urllib.request.Request(UPDATE_CHECK_URL, headers={"User-Agent": f"SheetMate/{APP_VERSION}"})
        opener = urllib.request.build_opener(_SafeHttpsRedirectHandler())
        with opener.open(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8"))
        ver, url = data.get("version"), data.get("url")
        safe_u = _validate_safe_http_url(url, require_https=True)
        if ver and safe_u:
            return ver, safe_u
    except Exception:
        pass
    return None


def _ver_newer(remote, current):
    def parts(v):
        return [int(x) for x in str(v).split(".")]
    try:
        return parts(remote) > parts(current)
    except Exception:
        return False


def set_gui_logger(fn):
    global _gui_logger
    _gui_logger = fn


# 顶部状态区：当前步骤 / 上次抓取时间 / 下次抓取时间 / 报错步骤 回调
_step_callback = None
_last_run_callback = None


def set_step_callback(fn):
    global _step_callback
    _step_callback = fn


def set_step(text):
    if _step_callback:
        _step_callback(text)


def set_last_run_callback(fn):
    global _last_run_callback
    _last_run_callback = fn


def notify_last_run():
    if _last_run_callback:
        _last_run_callback()


_prompt_show = None
_prompt_close = None


def set_login_prompt(show_fn, close_fn):
    global _prompt_show, _prompt_close
    _prompt_show = show_fn
    _prompt_close = close_fn


def show_login_prompt():
    if _prompt_show:
        try:
            _prompt_show()
        except Exception:
            pass


def close_login_prompt():
    if _prompt_close:
        try:
            _prompt_close()
        except Exception:
            pass


_recent_logs_lock = threading.Lock()
_recent_logs_buffer = []
_last_ai_heal_report = None


def _get_recent_logs_tail(limit=25):
    with _recent_logs_lock:
        return list(_recent_logs_buffer[-int(limit):])


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    with _recent_logs_lock:
        _recent_logs_buffer.append(line)
        if len(_recent_logs_buffer) > 80:
            del _recent_logs_buffer[:-80]
        try:
            if sys.stdout is not None:
                print(line)
        except Exception:
            pass
        # 落盘运行日志，超 2MB 自动滚动保留最近约 2000 行，避免长期值守后丢日志
        try:
            p = app_dir() / "run.log"
            p.parent.mkdir(parents=True, exist_ok=True)
            if p.exists() and p.stat().st_size >= 2_000_000:
                tail_lines = []
                try:
                    with open(p, "rb") as rf:
                        rf.seek(0, os.SEEK_END)
                        sz = rf.tell()
                        rf.seek(max(0, sz - 600_000), os.SEEK_SET)
                        chunk = rf.read().decode("utf-8", errors="ignore")
                    if sz > 600_000 and "\n" in chunk:
                        chunk = chunk.split("\n", 1)[1]
                    tail_lines = [ln for ln in chunk.splitlines()[-2000:] if ln.strip()]
                except Exception:
                    tail_lines = []
                tail_lines.append(line)
                _atomic_write_text(p, "\n".join(tail_lines) + "\n")
            else:
                with open(p, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception:
            pass
    if _gui_logger:
        try:
            _gui_logger(msg)
        except Exception:
            pass


def log_error(exc=None, title=""):
    """将异常堆栈追加写入 error.log（单文件上限 2MB）。"""
    try:
        import traceback
        p = app_dir() / "error.log"
        mode = "w" if (p.exists() and p.stat().st_size >= 2_000_000) else "a"
        tb = traceback.format_exc()
        if (not tb or tb.strip() == "NoneType: None") and exc is not None:
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        with open(p, mode, encoding="utf-8") as f:
            head = f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')}"
            if title:
                head += f" [{title}]"
            f.write(head + " =====\n")
            if tb:
                f.write(tb)
            elif exc is not None:
                f.write(repr(exc) + "\n")
    except Exception:
        pass


def human_error(e):
    """把技术报错翻译成带错误码的大白话指引。"""
    det = classify_error_detail(e)
    return f"[{det['code']}] {det['title']}：{det['plain_reason']}"


def _no_window_flags():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def set_clipboard_windows(text):
    """使用原生 64 位安全的 Win32 CF_UNICODETEXT 写入 UTF-16LE（带结尾空字符），杜绝短中文误判成 GBK（如 慕杰组 -> Uapg膥）。"""
    s = "" if text is None else str(text)
    if os.name != "nt":
        try:
            subprocess.run(
                ["clip"],
                input=b"\xff\xfe" + s.encode("utf-16-le"),
                check=False,
                timeout=5,
                creationflags=_no_window_flags(),
            )
            return True
        except Exception:
            return False

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p

    raw = s.encode("utf-16-le") + b"\x00\x00"
    for _ in range(8):
        if user32.OpenClipboard(None):
            h_mem = None
            transferred = False
            try:
                user32.EmptyClipboard()
                h_mem = kernel32.GlobalAlloc(0x0002, len(raw))  # GMEM_MOVEABLE
                if h_mem:
                    ptr = kernel32.GlobalLock(h_mem)
                    if ptr:
                        try:
                            ctypes.memmove(ptr, raw, len(raw))
                        finally:
                            kernel32.GlobalUnlock(h_mem)
                        if user32.SetClipboardData(13, h_mem):  # CF_UNICODETEXT
                            transferred = True
                            return True
            finally:
                if h_mem and not transferred:
                    try:
                        kernel32.GlobalFree(h_mem)
                    except Exception:
                        pass
                user32.CloseClipboard()
        time.sleep(0.08)

    # 极端被占锁时的保底兜底：务必加 UTF-16LE BOM 防止 IsTextUnicode 误判
    try:
        cp = subprocess.run(
            ["clip"],
            input=b"\xff\xfe" + s.encode("utf-16-le"),
            check=False,
            timeout=5,
            creationflags=_no_window_flags(),
        )
        return cp.returncode == 0
    except Exception:
        pass
    return False


def clear_clipboard_windows():
    """用 Win32 原生接口清空剪贴板，避免读到上一轮残留文本。"""
    if os.name != "nt":
        return False
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    for _ in range(5):
        if user32.OpenClipboard(None):
            try:
                user32.EmptyClipboard()
                return True
            finally:
                user32.CloseClipboard()
        time.sleep(0.12)
    return False


def get_clipboard_text_windows():
    """用 Win32 原生接口读取 Unicode 文字，避免 PowerShell 中文输出编码误判。"""
    if os.name != "nt":
        return ""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.restype = wintypes.BOOL
    kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    for _ in range(3):
        if not user32.OpenClipboard(None):
            time.sleep(0.15)
            continue
        try:
            handle = user32.GetClipboardData(13)  # CF_UNICODETEXT
            if not handle:
                return ""
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return ""
            try:
                return ctypes.wstring_at(ptr)
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()
    return ""


# ==================== 剪贴板图片（.NET 中转，避免图片被文本覆盖）====================
def _ps_clipboard(cmd, timeout=40):
    """运行带 WinForms + Drawing 引用的 PowerShell，返回 stdout 文本（无黑框）。"""
    full = ("Add-Type -AssemblyName System.Windows.Forms;"
             "Add-Type -AssemblyName System.Drawing;" + cmd)
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command", full],
        capture_output=True, text=True, timeout=timeout,
        creationflags=_no_window_flags(),
    )
    return r.stdout.strip()


def clipboard_has_image():
    """原生查询图片格式，避免 .NET 剪贴板在浏览器未响应时每次卡 40 秒。"""
    if os.name == "nt":
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
            user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
            # CF_BITMAP / CF_DIB / CF_DIBV5；WPS 至少会提供其中一种。
            return any(user32.IsClipboardFormatAvailable(fmt) for fmt in (2, 8, 17))
        except Exception:
            return False
    try:
        return _ps_clipboard(
            "[System.Windows.Forms.Clipboard]::ContainsImage()"
        ).lower().startswith("true")
    except Exception:
        return False


def get_clipboard_image_size():
    """读取剪贴板位图尺寸 (width, height)，无法读取返回 None。"""
    obj = None
    try:
        from PIL import ImageGrab
        obj = ImageGrab.grabclipboard()
        if isinstance(obj, Image.Image):
            return obj.size
    except Exception:
        pass
    finally:
        if obj is not None and hasattr(obj, "close"):
            try:
                obj.close()
            except Exception:
                pass
    return None


def _ps_sq(val):
    """转义 PowerShell 单引号字面量，避免用户目录包含单引号时执行失败。"""
    return str(val).replace("'", "''")


def _find_bundle_asset(rel_path):
    """兼容源码目录与 PyInstaller 单文件/文件夹(_internal)模式查找内置资源文件。"""
    candidates = [Path(__file__).resolve().parent / rel_path]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.insert(0, Path(meipass) / rel_path)
    candidates.append(app_dir() / "_internal" / rel_path)
    candidates.append(app_dir() / rel_path)
    for p in candidates:
        try:
            if p.exists():
                return p
        except Exception:
            pass
    return None


def save_clipboard_image(path):
    """把剪贴板里的图片（位图）保存成文件；只取位图，天然净化掉其他格式。"""
    _ps_clipboard(f"[System.Windows.Forms.Clipboard]::GetImage().Save('{_ps_sq(path)}')")
    return Path(path).exists()


def save_clipboard_image_if_exists(path):
    """剪贴板有图就保存到文件，一次 PowerShell 完成检查+保存。

    返回 'saved'（已保存）/ 'noimage'（剪贴板无图）/ 'error'（保存出错）。
    """
    out = _ps_clipboard(
        "if ([System.Windows.Forms.Clipboard]::ContainsImage()) {"
        f"[System.Windows.Forms.Clipboard]::GetImage().Save('{_ps_sq(path)}');"
        "Write-Output 'saved'} else {Write-Output 'noimage'}")
    if "saved" in out:
        return "saved"
    if "noimage" in out:
        return "noimage"
    return "error"


def _win32_release_modifiers():
    """在发送组合快捷键前安全抬起残留的系统修饰键，防止卡键导致快捷键错乱。"""
    if os.name != "nt":
        return
    try:
        u32 = ctypes.windll.user32
        for vk in (0x11, 0xA2, 0xA3, 0x10, 0xA0, 0xA1, 0x12, 0xA4, 0xA5, 0x5B, 0x5C):
            if u32.GetAsyncKeyState(vk) & 0x8000:
                scan = u32.MapVirtualKeyW(vk, 0)
                ext = 0x0001 if vk in (0xA3, 0xA5, 0x5B, 0x5C) else 0
                u32.keybd_event(vk, scan, ext | 0x0002, 0)
    except Exception:
        pass


def _win32_send_vk_combo(vk_list, hold_sec=0.04):
    """使用 Win32 SendInput + 硬件扫描码发送组合键，兼容 Qt/Electron 及中文输入法状态。"""
    if os.name != "nt" or not vk_list:
        return False

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = (
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        )

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = (
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        )

    class _INPUT_UNION(ctypes.Union):
        _fields_ = (("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT))

    class _INPUT(ctypes.Structure):
        _fields_ = (("type", wintypes.DWORD), ("u", _INPUT_UNION))

    EXTENDED_VKS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0xA3, 0xA5}
    try:
        u32 = ctypes.windll.user32
        u32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
        u32.SendInput.restype = wintypes.UINT
        u32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
        u32.MapVirtualKeyW.restype = wintypes.UINT
        _win32_release_modifiers()

        downs = []
        ups = []
        for vk in vk_list:
            vki = int(vk)
            scan = int(u32.MapVirtualKeyW(vki, 0) or 0)
            ext_flag = 0x0001 if vki in EXTENDED_VKS else 0
            # 当同时带硬件扫描码时优先触发系统底层热键与Qt编辑器粘贴路由
            flags_dn = (0x0008 if scan else 0) | ext_flag
            flags_up = flags_dn | 0x0002
            in_dn = _INPUT(1, _INPUT_UNION(ki=_KEYBDINPUT(vki, scan, flags_dn, 0, 0)))
            in_up = _INPUT(1, _INPUT_UNION(ki=_KEYBDINPUT(vki, scan, flags_up, 0, 0)))
            downs.append(in_dn)
            ups.insert(0, in_up)

        arr_dn = (_INPUT * len(downs))(*downs)
        sent1 = u32.SendInput(len(downs), arr_dn, ctypes.sizeof(_INPUT))
        time.sleep(max(0.02, float(hold_sec)))
        arr_up = (_INPUT * len(ups))(*ups)
        sent2 = u32.SendInput(len(ups), arr_up, ctypes.sizeof(_INPUT))
        return sent1 == len(downs) and sent2 == len(ups)
    except Exception:
        return False


def _win32_send_ctrl_key(vk_key, hold_sec=0.05):
    return _win32_send_vk_combo([0x11, int(vk_key)], hold_sec=hold_sec)


def _win32_send_single_vk(vk_key, hold_sec=0.04):
    return _win32_send_vk_combo([int(vk_key)], hold_sec=hold_sec)


def _write_dib_to_clipboard_windows(img_rgb, png_bytes=None):
    """在本进程内通过 Win32 同时写入 CF_DIB 与 PNG 双格式剪贴板，确保兼容所有版本钉钉粘贴识别。"""
    if os.name != "nt":
        return False
    import io
    buf = io.BytesIO()
    img_rgb.save(buf, format="BMP")
    bmp_bytes = buf.getvalue()
    if len(bmp_bytes) <= 14:
        return False
    dib_bytes = bmp_bytes[14:]  # 剥离 BITMAPFILEHEADER 即为标准 CF_DIB (BITMAPINFOHEADER + 像素)
    if png_bytes is None:
        try:
            pbuf = io.BytesIO()
            img_rgb.save(pbuf, format="PNG", optimize=True)
            png_bytes = pbuf.getvalue()
        except Exception:
            png_bytes = None

    u32 = ctypes.WinDLL("user32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    u32.OpenClipboard.argtypes = [wintypes.HWND]
    u32.OpenClipboard.restype = wintypes.BOOL
    u32.EmptyClipboard.argtypes = []
    u32.EmptyClipboard.restype = wintypes.BOOL
    u32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    u32.SetClipboardData.restype = ctypes.c_void_p
    u32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
    u32.RegisterClipboardFormatW.restype = wintypes.UINT
    u32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    u32.IsClipboardFormatAvailable.restype = wintypes.BOOL
    u32.CloseClipboard.argtypes = []
    u32.CloseClipboard.restype = wintypes.BOOL
    k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    k32.GlobalAlloc.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = [ctypes.c_void_p]
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    k32.GlobalUnlock.restype = wintypes.BOOL
    k32.GlobalFree.argtypes = [ctypes.c_void_p]
    k32.GlobalFree.restype = ctypes.c_void_p

    CF_DIB = 8
    CF_PNG = int(u32.RegisterClipboardFormatW("PNG") or 0)
    GMEM_MOVEABLE = 0x0002

    def _alloc_and_copy(raw_bytes):
        hm = k32.GlobalAlloc(GMEM_MOVEABLE, len(raw_bytes))
        if not hm:
            return None
        ptr = k32.GlobalLock(hm)
        if not ptr:
            try:
                k32.GlobalFree(hm)
            except Exception:
                pass
            return None
        try:
            ctypes.memmove(ptr, raw_bytes, len(raw_bytes))
        finally:
            k32.GlobalUnlock(hm)
        return hm

    for _ in range(8):
        if u32.OpenClipboard(None):
            h_mem = None
            h_png = None
            try:
                u32.EmptyClipboard()
                h_mem = _alloc_and_copy(dib_bytes)
                if h_mem:
                    if u32.SetClipboardData(CF_DIB, h_mem):
                        h_mem = None  # 系统已接管内存所有权，禁止释放
                if CF_PNG and png_bytes:
                    h_png = _alloc_and_copy(png_bytes)
                    if h_png:
                        if u32.SetClipboardData(CF_PNG, h_png):
                            h_png = None
                if u32.IsClipboardFormatAvailable(CF_DIB):
                    return True
            finally:
                for leftover in (h_mem, h_png):
                    if leftover:
                        try:
                            k32.GlobalFree(leftover)
                        except Exception:
                            pass
                u32.CloseClipboard()
        time.sleep(0.08)
    return False


def _prepare_scaled_send_image(src_path):
    """一次性等比缩放至钉钉安全分辨率并预先编码 PNG 字节流，避免粘贴重试循环重复缩放耗时。"""
    src = Path(src_path)
    use_path = src
    png_bytes = None
    try:
        import io
        im = None
        im_resized = None
        pbuf = None
        try:
            with Image.open(src) as raw_im:
                im = raw_im.convert("RGB")
            target_im = im
            max_w = 4600
            max_pixels = 12_000_000
            scale = 1.0
            if im.width > max_w:
                scale = min(scale, max_w / float(im.width))
            if im.width * im.height > max_pixels:
                scale = min(scale, (max_pixels / float(im.width * im.height)) ** 0.5)
            if scale < 0.999:
                new_size = (max(1, int(im.width * scale)), max(1, int(im.height * scale)))
                im_resized = im.resize(new_size, Image.Resampling.LANCZOS)
                target_im = im_resized
                use_path = src.with_name(src.stem + "_send.png")
                target_im.save(use_path, "PNG", optimize=True)
            pbuf = io.BytesIO()
            target_im.save(pbuf, format="PNG", optimize=True)
            png_bytes = pbuf.getvalue()
        finally:
            for obj in (pbuf, im_resized, im):
                if obj is not None:
                    try:
                        obj.close()
                    except Exception:
                        pass
    except Exception:
        use_path = src
    return use_path, png_bytes


def set_clipboard_image_from_file(path, cached_scaled_path=None, cached_png_bytes=None):
    """将图片等比规范至钉钉安全像素宽度内，并通过本进程原生 CF_DIB+PNG 双格式（含 PS 兜底）写入剪贴板。"""
    src = Path(path)
    use_path = Path(cached_scaled_path) if (cached_scaled_path and Path(cached_scaled_path).exists()) else src
    png_bytes = cached_png_bytes
    try:
        if use_path == src and png_bytes is None:
            use_path, png_bytes = _prepare_scaled_send_image(src)
        with Image.open(use_path) as raw_im:
            im = raw_im.convert("RGB")
        try:
            if _write_dib_to_clipboard_windows(im, png_bytes=png_bytes):
                return True
        finally:
            im.close()
    except Exception:
        use_path = src
    ps = (
        f"$img=[System.Drawing.Image]::FromFile('{_ps_sq(use_path)}');"
        "$bmp=New-Object System.Drawing.Bitmap($img);$ok=$false;"
        "for($i=0;$i -lt 6;$i++){try{[System.Windows.Forms.Clipboard]::SetImage($bmp);"
        "$ok=$true;break}catch{Start-Sleep -Milliseconds 250}};"
        "$img.Dispose();$bmp.Dispose();if($ok){Write-Output 'set_ok'}else{Write-Output 'set_fail'}"
    )
    out = _ps_clipboard(ps)
    return "set_ok" in out.lower()


# ==================== 配置持久化 ====================
_INITIAL_APP_DIR = app_dir()
CONFIG_PATH = _INITIAL_APP_DIR / "config.json"
_CONFIG_LOCK = threading.RLock()


def _config_file_path():
    if CONFIG_PATH != (_INITIAL_APP_DIR / "config.json"):
        return Path(CONFIG_PATH)
    return app_dir() / "config.json"

DEFAULT_SUMMARY_PRESET_ITEMS = [
    {
        "enabled": True,
        "sheet": "数据汇总",
        "cell": "D3",
        "label": "查询日期",
        "action": "today",
        "date_fmt": "YYYY年M月D日",
        "time_fmt": "full",
        "day_offset": 0,
        "cutoff_enabled": False,
        "cutoff_hm": "09:00",
        "before_cutoff_offset": -1,
        "only_after_paste": False,
        "value": "",
    },
    {
        "enabled": True,
        "sheet": "数据汇总",
        "cell": "D4",
        "label": "日期范围起始",
        "action": "clear",
        "date_fmt": "YYYY年M月D日",
        "time_fmt": "full",
        "day_offset": 0,
        "cutoff_enabled": False,
        "cutoff_hm": "09:00",
        "before_cutoff_offset": -1,
        "only_after_paste": False,
        "value": "",
    },
    {
        "enabled": True,
        "sheet": "数据汇总",
        "cell": "E4",
        "label": "日期范围结束",
        "action": "clear",
        "date_fmt": "YYYY年M月D日",
        "time_fmt": "full",
        "day_offset": 0,
        "cutoff_enabled": False,
        "cutoff_hm": "09:00",
        "before_cutoff_offset": -1,
        "only_after_paste": False,
        "value": "",
    },
    {
        "enabled": True,
        "sheet": "数据汇总",
        "cell": "D5",
        "label": "整周周报",
        "action": "clear",
        "date_fmt": "YYYY年M月D日",
        "time_fmt": "full",
        "day_offset": 0,
        "cutoff_enabled": False,
        "cutoff_hm": "09:00",
        "before_cutoff_offset": -1,
        "only_after_paste": False,
        "value": "",
    },
    {
        "enabled": True,
        "sheet": "数据汇总",
        "cell": "L3",
        "label": "需要标记的团队",
        "action": "value",
        "date_fmt": "YYYY年M月D日",
        "time_fmt": "full",
        "day_offset": 0,
        "cutoff_enabled": False,
        "cutoff_hm": "09:00",
        "before_cutoff_offset": -1,
        "only_after_paste": False,
        "value": "慕杰组",
    },
    {
        "enabled": True,
        "sheet": "数据汇总",
        "cell": "Y3",
        "label": "排除模板",
        "action": "value",
        "date_fmt": "YYYY年M月D日",
        "time_fmt": "full",
        "day_offset": 0,
        "cutoff_enabled": False,
        "cutoff_hm": "09:00",
        "before_cutoff_offset": -1,
        "only_after_paste": False,
        "value": "排除所有模板",
    },
    {
        "enabled": True,
        "sheet": "数据汇总",
        "cell": "Y4",
        "label": "减去短链",
        "action": "value",
        "date_fmt": "YYYY年M月D日",
        "time_fmt": "full",
        "day_offset": 0,
        "cutoff_enabled": False,
        "cutoff_hm": "09:00",
        "before_cutoff_offset": -1,
        "only_after_paste": False,
        "value": "减去短链",
    },
    {
        "enabled": True,
        "sheet": "数据汇总",
        "cell": "Y5",
        "label": "筛选暗号",
        "action": "value",
        "date_fmt": "YYYY年M月D日",
        "time_fmt": "full",
        "day_offset": 0,
        "cutoff_enabled": False,
        "cutoff_hm": "09:00",
        "before_cutoff_offset": -1,
        "only_after_paste": False,
        "value": "当前筛选暗号",
    },
    {
        "enabled": True,
        "sheet": "数据汇总",
        "cell": "AA2",
        "label": "排除暗号",
        "action": "value",
        "date_fmt": "YYYY年M月D日",
        "time_fmt": "full",
        "day_offset": 0,
        "cutoff_enabled": False,
        "cutoff_hm": "09:00",
        "before_cutoff_offset": -1,
        "only_after_paste": False,
        "value": "备用,备用2,CS暗号",
    },
    {
        "enabled": True,
        "sheet": "数据汇总",
        "cell": "AA5",
        "label": "在职组范围",
        "action": "value",
        "date_fmt": "YYYY年M月D日",
        "time_fmt": "full",
        "day_offset": 0,
        "cutoff_enabled": False,
        "cutoff_hm": "09:00",
        "before_cutoff_offset": -1,
        "only_after_paste": False,
        "value": "全部在职组",
    },
]

DEFAULT_CONFIG = {
    "preset_schema_v": 2,
    "day_mode": "daily",         # daily | workday | weekend | custom
    "custom_days": [0, 1, 2, 3, 4],  # 0=周一 .. 6=周日
    "day": {"sh": 9, "sm": 0, "eh": 18, "em": 0, "ih": 2, "im": 0},
    "night": {"sh": 18, "sm": 0, "eh": 23, "em": 0, "ih": 2, "im": 0},
    "night_enabled": True,       # 是否启用晚上时段自动执行
    "extra_points": [],          # 额外补充的独立固定执行点 ["HH:MM", ...]
    "luya_user": LUYA_USER,      # 后台登录账号
    "luya_pass": LUYA_PASS,      # 后台登录密码
    "wps_url": WPS_URL,          # 金山文档在线表格地址
    "enable_capture": True,      # 跑完后在 WPS 选区复制为图片
    "enable_dingtalk": False,    # 复制后发到钉钉会话
    "dt_auto_send": True,        # 默认确认直接按 Enter 发送；关闭则只贴入待发
    "dt_ai_screen_verify": True, # 每次发送前由 AI 看桌面核对对象：一致确定发送，不一致自动取消发送
    "auto_confirm_choices": True, # 默认对非登录类确认选择自动点击确定继续
    "dt_clear_before_send": True,  # 粘贴前先清空聊天输入框原有残留内容
    "dt_paste_delay_sec": 1,     # 打开/切入会话后等待几秒再粘贴图片（1秒极速就绪）
    "dt_send_wait_sec": 5,       # 截图与文案全部写入输入框后，等待几秒再直接按 Enter 发送（默认5秒）
    "wps_post_copy_wait_sec": 5, # WPS「复制为图片」成功进入剪贴板后，稳态等待几秒再切换去粘贴（默认5秒）
    "dt_silent_send": True,      # 发送前自动勾选「设为静音」（静音发送）
    "dt_minimize_after_send": True,  # 自动发送完成后最小化钉钉窗口
    "dt_search": "文件小助手",    # 钉钉搜索的主会话名（兼容单目标及回退）
    "dt_targets": [],            # 多目标列表：[{"name": "文件小助手", "enabled": True}, ...]
    "dt_suffix": "",             # 默认后缀：留空表示平时仅发时间戳，只在命中的分时段规则加后缀
    "dt_time_mode": "datetime",  # datetime(日期+时间) | date_only(仅日期) | none(不带日期时间)
    "dt_date_fmt": "YYYY-MM-DD", # YYYY-MM-DD | YYYY年M月D日 | M月D日
    "dt_use_actual_clock": False, # 定时发消息文案是否使用实际发出的电脑实时时钟（如 23:46）
    "dt_day_offset": 0,          # 0=当天, -1=前1天, -2=前2天 ...
    "dt_cutoff_enabled": False,  # 是否启用凌晨跨天分界点（分界点前按 before_cutoff_offset）
    "dt_cutoff_hm": "09:00",
    "dt_before_cutoff_offset": -1,
    "suffix_rules": [],          # [{"enabled": True, "start": "11:30", "end": "12:30", "suffix": "午间数据"}]
    "point_overrides": {},       # {"HH:MM": {"mode": ..., "suffix": ..., "silent": ..., "sheet_day_offset": ..., "msg_time_mode": ..., "msg_day_offset": ..., "msg_date_fmt": ...}}
    "open_wps_after_run": False, # 每轮跑完是否自动用系统浏览器/WPS打开表格（默认关闭避免自动值守弹窗扰民）
    "prevent_sleep": True,       # 自动值守与运行期间阻止电脑自动休眠/息屏
    "save_shot_history": True,   # 自动归档最近 20 张汇总表截图至 history_shots
    "ai_assistant": {
        "enabled": True,
        "provider": "gemini",
        "api_base": "",
        "api_key": DEFAULT_GEMINI_API_KEY,
        "model": GEMINI_MODEL_CANDIDATES[0],
        "auto_diagnose": True,
        "auto_heal": True,
        "auto_attach_summary": False,
        "attach_style": "brief",
        "focus_team": "",
        "custom_summary_prompt": "",
        "custom_phrases": [],
    },
    "summary_presets": {
        "enabled": True,
        "items": [dict(it) for it in DEFAULT_SUMMARY_PRESET_ITEMS],
    },
    "update_time_cell": {
        "enabled": True,           # 是否在粘贴数据后写入最后更新时间（对应 P4 标题）
        "cell": "R4",              # 目标单元格（默认 R4）
        "only_after_paste": True,  # True=仅全流程粘贴数据后写入；False=仅截图模式也同步更新
    },
    "pipeline_tuning": {
        "compose_step_order": "image_text_silent",  # image_text_silent | text_silent_image
        "silent_step_order": "uia_first",           # uia_first | arrow_img_then_uia
        "paste_key_order": "pywinauto_first",       # pywinauto_first | uia_first
        "wps_refresh_wait_sec": 5,                  # 切汇总表校准后的等待刷新秒数 (3~15)
    },
    # 后台定时刷底表：每隔 N 分钟仅下载三表并粘贴到 WPS，保持数据汇总其余格位不动，只把粘贴完成时间写入 R4。
    "raw_sync": {
        "enabled": False,
        "interval_min": 40,
        "skip_near_send_min": 5,
        "preserve_summary_cells": True,
        "write_r4_timestamp": True,
        "all_day": True,
        "start": "00:00",
        "end": "23:59",
    },
    # 独立截图发送时段：只约束自动计划，手动按钮始终可以立即执行。
    "capture_plan": {
        "enabled": False,
        "windows": [
            {"enabled": True, "start": "09:00", "end": "18:00"},
        ],
        "workday": {
            "slot1": True, "start1": "09:00", "end1": "18:00",
            "slot2": False, "start2": "18:00", "end2": "22:00",
        },
        "weekend": {
            "slot1": True, "start1": "09:00", "end1": "18:00",
            "slot2": False, "start2": "18:00", "end2": "22:00",
        },
    },
}


def _split_target_names(raw):
    """按换行或中英文逗号/分号拆分并去重会话名称。"""
    items = []
    for part in re.split(r"[\r\n,;，；]+", str(raw or "")):
        s = part.strip()
        if s and s not in items:
            items.append(s)
    return items


def get_enabled_dt_targets(cfg):
    """按序提取当前配置中所有已启用的钉钉目标群聊/联系人名称列表。"""
    c = cfg if isinstance(cfg, dict) else {}
    raw_targets = c.get("dt_targets")
    if isinstance(raw_targets, list):
        named_items = [it for it in raw_targets if isinstance(it, dict) and str(it.get("name") or "").strip()]
        if named_items:
            out = []
            for it in named_items:
                if it.get("enabled", True):
                    for nm in _split_target_names(it.get("name")):
                        if nm not in out:
                            out.append(nm)
            return out
    fallback = _split_target_names(c.get("dt_search") or "文件小助手")
    return fallback or ["文件小助手"]


def _deep_merge(default, saved):
    """把保存的配置合并到默认配置上，缺的键补默认值。"""
    out = dict(default)
    for k, v in (saved or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _norm_hm_str(s, default="09:00"):
    try:
        parts = str(s or "").strip().split(":")
        if len(parts) != 2:
            return default
        h = max(0, min(23, int(parts[0])))
        m = max(0, min(59, int(parts[1])))
        return f"{h:02d}:{m:02d}"
    except Exception:
        return default


def _clamp_day_offset(val, default=0):
    if val is None or isinstance(val, bool) or val == "":
        return default
    try:
        return max(-60, min(7, int(val)))
    except Exception:
        return default


def normalize_config(cfg):
    """规范化并兼容旧版配置结构，确保日期模式、偏移规则、消息时间模式、AI 助手与分时段字段合法。"""
    import copy
    out = _deep_merge(copy.deepcopy(DEFAULT_CONFIG), cfg if isinstance(cfg, dict) else {})

    if out.get("day_mode") not in ("daily", "workday", "weekend", "custom"):
        out["day_mode"] = "daily"

    raw_days = out.get("custom_days")
    if isinstance(raw_days, list):
        clean_days = sorted({int(d) for d in raw_days if str(d).isdigit() and 0 <= int(d) <= 6})
        out["custom_days"] = clean_days
    else:
        out["custom_days"] = [0, 1, 2, 3, 4]

    for key, def_slot in (("day", DEFAULT_CONFIG["day"]), ("night", DEFAULT_CONFIG["night"])):
        sl = out.get(key) if isinstance(out.get(key), dict) else {}
        out[key] = {
            "sh": max(0, min(23, int(sl.get("sh", def_slot["sh"])))),
            "sm": max(0, min(59, int(sl.get("sm", def_slot["sm"])))),
            "eh": max(0, min(23, int(sl.get("eh", def_slot["eh"])))),
            "em": max(0, min(59, int(sl.get("em", def_slot["em"])))),
            "ih": max(0, min(12, int(sl.get("ih", def_slot["ih"])))),
            "im": max(0, min(59, int(sl.get("im", def_slot["im"])))),
        }

    raw_suf = out.get("dt_suffix")
    out["dt_suffix"] = "" if raw_suf is None else str(raw_suf).strip()
    out["dt_search"] = str(out.get("dt_search") or "文件小助手").strip() or "文件小助手"
    out["open_wps_after_run"] = bool(out.get("open_wps_after_run", False))
    out["prevent_sleep"] = bool(out.get("prevent_sleep", True))
    out["save_shot_history"] = bool(out.get("save_shot_history", True))
    out["auto_confirm_choices"] = bool(out.get("auto_confirm_choices", True))
    out["dt_ai_screen_verify"] = bool(out.get("dt_ai_screen_verify", True))
    out["dt_minimize_after_send"] = bool(out.get("dt_minimize_after_send", True))
    try:
        p_del = out.get("dt_paste_delay_sec", 3)
        p_del = 3 if isinstance(p_del, bool) or p_del in (None, "") else int(round(float(p_del)))
    except Exception:
        p_del = 3
    out["dt_paste_delay_sec"] = max(0, min(20, p_del))
    try:
        s_wait = out.get("dt_send_wait_sec", 5)
        s_wait = 5 if isinstance(s_wait, bool) or s_wait in (None, "") else int(round(float(s_wait)))
    except Exception:
        s_wait = 5
    out["dt_send_wait_sec"] = max(0, min(30, s_wait))
    try:
        w_wait = out.get("wps_post_copy_wait_sec", 5)
        w_wait = 5.0 if isinstance(w_wait, bool) or w_wait in (None, "") else float(w_wait)
    except Exception:
        w_wait = 5.0
    w_wait = max(0.0, min(15.0, round(w_wait, 2)))
    out["wps_post_copy_wait_sec"] = int(w_wait) if abs(w_wait - round(w_wait)) < 1e-6 else w_wait

    if out.get("dt_time_mode") not in ("datetime", "date_only", "none"):
        out["dt_time_mode"] = "datetime"
    if out.get("dt_date_fmt") not in ("YYYY-MM-DD", "YYYY年M月D日", "M月D日"):
        out["dt_date_fmt"] = "YYYY-MM-DD"
    out["dt_day_offset"] = _clamp_day_offset(out.get("dt_day_offset"), 0)
    out["dt_cutoff_enabled"] = bool(out.get("dt_cutoff_enabled", False))
    out["dt_cutoff_hm"] = _norm_hm_str(out.get("dt_cutoff_hm"), "09:00")
    out["dt_before_cutoff_offset"] = _clamp_day_offset(out.get("dt_before_cutoff_offset"), -1)

    ai_cfg = out.get("ai_assistant") if isinstance(out.get("ai_assistant"), dict) else {}
    ai_prov = str(ai_cfg.get("provider") or "gemini").strip().lower()
    if ai_prov not in ("gemini", "openai_compat", "local_only"):
        ai_prov = "gemini"
    ai_base = str(ai_cfg.get("api_base") or "").strip().rstrip("/")
    ai_key = str(ai_cfg.get("api_key") or "").strip()
    if not ai_key and ai_prov == "gemini" and _is_official_gemini_base(ai_base):
        ai_key = str(DEFAULT_GEMINI_API_KEY or "").strip()
    ai_model = str(ai_cfg.get("model") or GEMINI_MODEL_CANDIDATES[0]).replace("models/", "").strip() or GEMINI_MODEL_CANDIDATES[0]
    attach_st = str(ai_cfg.get("attach_style") or "brief").strip().lower()
    if attach_st not in AI_SUMMARY_STYLES:
        attach_st = "brief"
    raw_phrases = ai_cfg.get("custom_phrases")
    clean_phrases = []
    if isinstance(raw_phrases, list):
        for ph in raw_phrases:
            sp = str(ph or "").strip()[:120]
            if sp and sp not in clean_phrases:
                clean_phrases.append(sp)
            if len(clean_phrases) >= 16:
                break
    out["ai_assistant"] = {
        "enabled": bool(ai_cfg.get("enabled", True)),
        "provider": ai_prov,
        "api_base": ai_base,
        "api_key": ai_key,
        "model": ai_model,
        "auto_diagnose": bool(ai_cfg.get("auto_diagnose", True)),
        "auto_heal": bool(ai_cfg.get("auto_heal", True)),
        "auto_attach_summary": bool(ai_cfg.get("auto_attach_summary", False)),
        "attach_style": attach_st,
        "focus_team": str(ai_cfg.get("focus_team") or "").strip()[:24],
        "custom_summary_prompt": str(ai_cfg.get("custom_summary_prompt") or "").strip(),
        "custom_phrases": clean_phrases,
    }

    pt_cfg = out.get("pipeline_tuning") if isinstance(out.get("pipeline_tuning"), dict) else {}
    comp_ord = str(pt_cfg.get("compose_step_order") or "image_text_silent").strip()
    if comp_ord not in ("image_text_silent", "text_silent_image"):
        comp_ord = "image_text_silent"
    sil_ord = str(pt_cfg.get("silent_step_order") or "uia_first").strip()
    if sil_ord not in ("uia_first", "arrow_img_then_uia"):
        sil_ord = "uia_first"
    key_ord = str(pt_cfg.get("paste_key_order") or "pywinauto_first").strip()
    if key_ord not in ("pywinauto_first", "uia_first"):
        key_ord = "pywinauto_first"
    try:
        wps_w = int(round(float(pt_cfg.get("wps_refresh_wait_sec", 5))))
    except Exception:
        wps_w = 5
    out["pipeline_tuning"] = {
        "compose_step_order": comp_ord,
        "silent_step_order": sil_ord,
        "paste_key_order": key_ord,
        "wps_refresh_wait_sec": max(3, min(15, wps_w)),
    }

    targets = []
    seen_targets = set()
    raw_targets = out.get("dt_targets")
    if isinstance(raw_targets, list):
        for item in raw_targets:
            if not isinstance(item, dict):
                continue
            nm = str(item.get("name") or "").strip()
            if nm and nm not in seen_targets:
                seen_targets.add(nm)
                targets.append({"name": nm, "enabled": bool(item.get("enabled", True))})
    if not targets:
        for nm in _split_target_names(out["dt_search"]):
            if nm not in seen_targets:
                seen_targets.add(nm)
                targets.append({"name": nm, "enabled": True})
    if not targets:
        targets = [{"name": "文件小助手", "enabled": True}]
    out["dt_targets"] = targets
    first_enabled = next((t["name"] for t in targets if t["enabled"]), targets[0]["name"])
    out["dt_search"] = first_enabled

    rules = []
    for item in (out.get("suffix_rules") or []):
        if not isinstance(item, dict):
            continue
        rules.append({
            "enabled": bool(item.get("enabled", True)),
            "start": _norm_hm_str(item.get("start"), "11:30"),
            "end": _norm_hm_str(item.get("end"), "12:30"),
            "suffix": str(item.get("suffix") or "").strip(),
        })
    out["suffix_rules"] = rules

    out["dt_use_actual_clock"] = bool(out.get("dt_use_actual_clock", False))
    extra_pts = []
    seen_ep = set()
    for ep in (out.get("extra_points") or []):
        hm_ep = _norm_hm_str(ep, "")
        if hm_ep and hm_ep not in seen_ep:
            seen_ep.add(hm_ep)
            extra_pts.append(hm_ep)
        if len(extra_pts) >= 48:
            break
    out["extra_points"] = sorted(extra_pts)

    overrides = {}
    raw_ov = out.get("point_overrides")
    if isinstance(raw_ov, dict):
        for k, v in raw_ov.items():
            if not isinstance(v, dict):
                continue
            hm_k = _norm_hm_str(k, "")
            if not hm_k:
                continue
            mode = v.get("mode", "default")
            if mode not in ("default", "send", "table_only"):
                mode = "default"
            suf_v = v.get("suffix", None)
            if suf_v is not None:
                suf_v = " ".join(str(suf_v).replace("\r", " ").replace("\n", " ").split()).strip()[:120]
            sil_raw = v.get("silent", None)
            sil_v = sil_raw if isinstance(sil_raw, bool) else None
            s_off_raw = v.get("sheet_day_offset", None)
            s_off = _clamp_day_offset(s_off_raw, None) if s_off_raw is not None and s_off_raw != "" else None
            m_mode = v.get("msg_time_mode", None)
            if m_mode not in ("datetime", "date_only", "none"):
                m_mode = None
            m_off_raw = v.get("msg_day_offset", None)
            m_off = _clamp_day_offset(m_off_raw, None) if m_off_raw is not None and m_off_raw != "" else None
            m_fmt = v.get("msg_date_fmt", None)
            if m_fmt not in ("YYYY-MM-DD", "YYYY年M月D日", "M月D日"):
                m_fmt = None
            raw_tgts = v.get("targets", None)
            clean_tgts = None
            if isinstance(raw_tgts, (list, tuple)):
                tmp_t = []
                seen_t = set()
                for t_item in raw_tgts:
                    ts = " ".join(str(t_item or "").replace("\r", " ").replace("\n", " ").split()).strip()[:64]
                    if ts and ts not in seen_t:
                        seen_t.add(ts)
                        tmp_t.append(ts)
                    if len(tmp_t) >= 10:
                        break
                if tmp_t:
                    clean_tgts = tmp_t
            if (mode == "default" and suf_v is None and sil_v is None
                    and s_off is None and m_mode is None and m_off is None
                    and m_fmt is None and clean_tgts is None):
                continue
            overrides[hm_k] = {
                "mode": mode,
                "suffix": suf_v,
                "silent": sil_v,
                "sheet_day_offset": s_off,
                "msg_time_mode": m_mode,
                "msg_day_offset": m_off,
                "msg_date_fmt": m_fmt,
                "targets": clean_tgts,
            }
    out["point_overrides"] = overrides

    cp = out.get("capture_plan") if isinstance(out.get("capture_plan"), dict) else {}
    raw_cp = cfg.get("capture_plan") if isinstance(cfg, dict) and isinstance(cfg.get("capture_plan"), dict) else {}
    saved_has_windows = "windows" in raw_cp
    wins = []
    if saved_has_windows or isinstance(cp.get("windows"), list):
        raw_wins = cp.get("windows") or []
        if not saved_has_windows and raw_cp:
            migrated = []
            seen = set()
            for prof_name in ("workday", "weekend"):
                prof = raw_cp.get(prof_name) if isinstance(raw_cp.get(prof_name), dict) else {}
                for idx in (1, 2):
                    if prof.get(f"slot{idx}", False):
                        st = _norm_hm_str(prof.get(f"start{idx}"), "09:00")
                        ed = _norm_hm_str(prof.get(f"end{idx}"), "18:00")
                        if (st, ed) not in seen:
                            seen.add((st, ed))
                            migrated.append({"enabled": True, "start": st, "end": ed})
            if migrated:
                raw_wins = migrated
        for w in raw_wins:
            if not isinstance(w, dict):
                continue
            wins.append({
                "enabled": bool(w.get("enabled", True)),
                "start": _norm_hm_str(w.get("start"), "09:00"),
                "end": _norm_hm_str(w.get("end"), "18:00"),
            })
    cp["windows"] = wins
    cp["enabled"] = bool(cp.get("enabled", False))
    out["capture_plan"] = cp

    raw_rs = cfg.get("raw_sync") if isinstance(cfg, dict) and isinstance(cfg.get("raw_sync"), dict) else {}
    rs_base = out.get("raw_sync") if isinstance(out.get("raw_sync"), dict) else {}
    rs_src = {**DEFAULT_CONFIG["raw_sync"], **rs_base, **raw_rs}
    try:
        rs_iv = int(rs_src.get("interval_min", 40) or 40)
    except Exception:
        rs_iv = 40
    try:
        rs_skip = int(rs_src.get("skip_near_send_min", 5) if rs_src.get("skip_near_send_min") is not None else 5)
    except Exception:
        rs_skip = 5
    out["raw_sync"] = {
        "enabled": bool(rs_src.get("enabled", False)),
        "interval_min": max(10, min(360, rs_iv)),
        "skip_near_send_min": max(0, min(30, rs_skip)),
        "preserve_summary_cells": bool(rs_src.get("preserve_summary_cells", True)),
        "write_r4_timestamp": bool(rs_src.get("write_r4_timestamp", True)),
        "all_day": bool(rs_src.get("all_day", True)),
        "start": _norm_hm_str(rs_src.get("start"), "00:00"),
        "end": _norm_hm_str(rs_src.get("end"), "23:59"),
    }

    raw_sp = cfg.get("summary_presets") if isinstance(cfg, dict) and isinstance(cfg.get("summary_presets"), dict) else {}
    sp = out.get("summary_presets") if isinstance(out.get("summary_presets"), dict) else {}
    sp_items = []
    if "items" in raw_sp and isinstance(raw_sp.get("items"), list):
        src_items = raw_sp.get("items") or []
    else:
        src_items = copy.deepcopy(DEFAULT_SUMMARY_PRESET_ITEMS)
    for item in src_items:
        if not isinstance(item, dict):
            continue
        cell = re.sub(r"\s+", "", str(item.get("cell") or "")).upper()
        if not cell:
            continue
        act = str(item.get("action") or "value").strip().lower()
        if act not in ("today", "now", "clear", "value", "formula"):
            act = "value"
        dfmt = str(item.get("date_fmt") or "YYYY年M月D日").strip()
        if dfmt not in ("YYYY年M月D日", "M月D日", "YYYY-MM-DD"):
            dfmt = "YYYY年M月D日"
        tfmt = str(item.get("time_fmt") or "full").strip()
        if tfmt not in ("full", "hm", "time_only"):
            tfmt = "full"
        sheet_nm = str(item.get("sheet") or "数据汇总").strip() or "数据汇总"
        sp_items.append({
            "enabled": bool(item.get("enabled", True)),
            "sheet": sheet_nm,
            "cell": cell,
            "label": str(item.get("label") or "").strip(),
            "action": act,
            "date_fmt": dfmt,
            "time_fmt": tfmt,
            "day_offset": _clamp_day_offset(item.get("day_offset"), 0),
            "cutoff_enabled": bool(item.get("cutoff_enabled", False)),
            "cutoff_hm": _norm_hm_str(item.get("cutoff_hm"), "09:00"),
            "before_cutoff_offset": _clamp_day_offset(item.get("before_cutoff_offset"), -1),
            "only_after_paste": bool(item.get("only_after_paste", False)),
            "value": "" if item.get("value") is None else str(item.get("value")).strip(),
        })
    sp["enabled"] = bool(raw_sp.get("enabled", sp.get("enabled", True)))
    in_schema_v = 0
    if isinstance(cfg, dict):
        try:
            in_schema_v = int(cfg.get("preset_schema_v") or 0)
        except Exception:
            in_schema_v = 0

    raw_ut = cfg.get("update_time_cell") if isinstance(cfg, dict) and isinstance(cfg.get("update_time_cell"), dict) else {}
    def_ut = out.get("update_time_cell") if isinstance(out.get("update_time_cell"), dict) else {}
    ut_cell = re.sub(r"\s+", "", str(raw_ut.get("cell", def_ut.get("cell", "R4")) or "")).upper() or "R4"

    # 从旧版 (preset_schema_v < 2) 平滑迁移：S4 -> R4，D2 -> D3，删原 L3 参与计算小组，L4 移至 L3，补齐 D5/Y3/Y4/Y5/AA2/AA5
    if isinstance(cfg, dict) and cfg and in_schema_v < 2:
        if ut_cell == "S4":
            ut_cell = "R4"
        if "items" in raw_sp and isinstance(raw_sp.get("items"), list):
            def _is_legacy_l3_grp(it):
                lbl = it.get("label") or ""
                val = it.get("value") or ""
                return it.get("cell") == "L3" and ("参与计算" in lbl or "," in val or "宋磊组" in val)

            def _is_legacy_l4_mark(it):
                lbl = it.get("label") or ""
                val = it.get("value") or ""
                return it.get("cell") == "L4" and ("需要标记" in lbl or val == "慕杰组")

            has_v1_marker = any(it.get("cell") == "D2" or _is_legacy_l3_grp(it) or _is_legacy_l4_mark(it) for it in sp_items)
            if has_v1_marker:
                migrated_sp = []
                seen_cells = set()
                has_d3 = any(it.get("cell") == "D3" for it in sp_items)
                for it in sp_items:
                    if _is_legacy_l3_grp(it):
                        continue
                    it_c = dict(it)
                    if it_c.get("cell") == "D2" and not has_d3:
                        it_c["cell"] = "D3"
                    elif _is_legacy_l4_mark(it_c):
                        it_c["cell"] = "L3"
                        it_c["label"] = it_c.get("label") or "需要标记的团队"
                        it_c["value"] = it_c.get("value") or "慕杰组"
                    ck = it_c.get("cell") or ""
                    if ck not in seen_cells:
                        seen_cells.add(ck)
                        migrated_sp.append(it_c)
                by_cell = {it["cell"]: it for it in migrated_sp}
                ordered_items = []
                for def_it in DEFAULT_SUMMARY_PRESET_ITEMS:
                    dc = def_it["cell"]
                    if dc in by_cell:
                        cur_it = by_cell.pop(dc)
                        if dc == "D5" and str(cur_it.get("value") or "").strip() == "":
                            cur_it["action"] = "clear"
                            cur_it["value"] = ""
                        ordered_items.append(cur_it)
                    else:
                        ordered_items.append(copy.deepcopy(def_it))
                for rem_it in migrated_sp:
                    if rem_it["cell"] in by_cell:
                        ordered_items.append(rem_it)
                sp_items = ordered_items

    sp["items"] = sp_items
    out["summary_presets"] = sp
    out["preset_schema_v"] = 2

    out["update_time_cell"] = {
        "enabled": bool(raw_ut.get("enabled", def_ut.get("enabled", True))),
        "cell": ut_cell,
        "only_after_paste": bool(raw_ut.get("only_after_paste", def_ut.get("only_after_paste", True))),
    }
    return out


def load_config():
    with _CONFIG_LOCK:
        try:
            cfg_p = _config_file_path()
            if cfg_p.exists():
                saved = json.loads(cfg_p.read_text(encoding="utf-8"))
                normed = normalize_config(saved)
                try:
                    if isinstance(saved, dict) and int(saved.get("preset_schema_v") or 0) < 2:
                        save_config(normed)
                except Exception:
                    pass
                return normed
        except Exception:
            pass
        return normalize_config({})


def save_config(cfg):
    with _CONFIG_LOCK:
        try:
            normed = normalize_config(cfg)
            _atomic_write_json(_config_file_path(), normed)
        except Exception:
            pass


def parse_hm(s):
    h, m = s.split(":")
    return int(h), int(m)


def in_time_window(cur_min, start_str, end_str, inclusive_end=True):
    """判断分钟数 cur_min (0..1439) 是否落入时段 start_str ~ end_str（支持单点与跨零点）。"""
    try:
        sh, sm = parse_hm(_norm_hm_str(start_str, "00:00"))
        eh, em = parse_hm(_norm_hm_str(end_str, "00:00"))
        s = sh * 60 + sm
        e = eh * 60 + em
        if s == e:
            return cur_min == s
        if s < e:
            return (s <= cur_min <= e) if inclusive_end else (s <= cur_min < e)
        return (cur_min >= s or cur_min <= e) if inclusive_end else (cur_min >= s or cur_min < e)
    except Exception:
        return False


def in_slot(now, start, end):
    try:
        cur = now.tm_hour * 60 + now.tm_min
        return in_time_window(cur, start, end, inclusive_end=False)
    except Exception:
        return False


def is_run_day_allowed(cfg, lt=None):
    """判断指定日期（默认今天）是否允许执行自动计划：daily / workday / weekend / custom。"""
    lt = lt or time.localtime()
    mode = (cfg or {}).get("day_mode", "daily")
    wday = lt.tm_wday  # 0=周一 .. 6=周日
    if mode == "workday":
        return wday < 5
    if mode == "weekend":
        return wday >= 5
    if mode == "custom":
        days = (cfg or {}).get("custom_days")
        if days is None:
            days = [0, 1, 2, 3, 4]
        return wday in set(int(x) for x in days if str(x).isdigit())
    return True


def resolve_suffix_for_time(cfg, lt=None, hm_override=None):
    """按单点覆盖 -> 分时段后缀规则 -> 默认后缀 解析指定时刻的消息后缀，返回 (suffix, source)。"""
    lt = lt or time.localtime()
    hm = _norm_hm_str(hm_override, f"{lt.tm_hour:02d}:{lt.tm_min:02d}") if hm_override else f"{lt.tm_hour:02d}:{lt.tm_min:02d}"
    h, m = parse_hm(hm)
    cur_min = h * 60 + m

    ov = ((cfg or {}).get("point_overrides") or {}).get(hm)
    if isinstance(ov, dict) and "suffix" in ov and ov["suffix"] is not None:
        return str(ov["suffix"]).strip(), "单点设置"

    for rule in ((cfg or {}).get("suffix_rules") or []):
        if not isinstance(rule, dict) or not rule.get("enabled", True):
            continue
        st = _norm_hm_str(rule.get("start"), "00:00")
        ed = _norm_hm_str(rule.get("end"), "00:00")
        if in_time_window(cur_min, st, ed, inclusive_end=True):
            return str(rule.get("suffix") or "").strip(), f"{st}-{ed}"

    default_suf = (cfg or {}).get("dt_suffix")
    return ("" if default_suf is None else str(default_suf).strip()), "默认"


def _shift_lt(lt=None, day_offset=0):
    """按整数天数偏移日期，精确处理跨月与跨年。"""
    lt = lt or time.localtime()
    off = _clamp_day_offset(day_offset, 0)
    if off == 0:
        return lt
    dt = datetime(lt.tm_year, lt.tm_mon, lt.tm_mday, lt.tm_hour, lt.tm_min, lt.tm_sec) + timedelta(days=off)
    return dt.timetuple()


def resolve_preset_day_offset(item, cfg=None, lt=None, hm_override=None):
    """按 单点设置 -> 凌晨跨天分界规则 -> 预设默认偏移 解析汇总表日期单元格的天数偏移。"""
    lt = lt or time.localtime()
    hm = _norm_hm_str(hm_override, f"{lt.tm_hour:02d}:{lt.tm_min:02d}") if hm_override else f"{lt.tm_hour:02d}:{lt.tm_min:02d}"
    h, m = parse_hm(hm)
    cur_min = h * 60 + m

    ov = ((cfg or {}).get("point_overrides") or {}).get(hm)
    ov_sh_off = _clamp_day_offset(ov.get("sheet_day_offset"), None) if isinstance(ov, dict) else None
    if ov_sh_off is not None:
        return ov_sh_off, f"单点({hm})"

    if isinstance(item, dict) and item.get("cutoff_enabled"):
        cut_hm = _norm_hm_str(item.get("cutoff_hm"), "09:00")
        ch, cm = parse_hm(cut_hm)
        if cur_min < ch * 60 + cm:
            return _clamp_day_offset(item.get("before_cutoff_offset"), -1), f"{cut_hm}前跨天"

    return _clamp_day_offset((item or {}).get("day_offset"), 0), "预设默认"


def resolve_dingtalk_time_policy(cfg, lt=None, hm_override=None):
    """解析指定时刻钉钉消息的 (time_mode, date_fmt, day_offset, source_desc)。"""
    cfg = cfg or {}
    lt = lt or time.localtime()
    hm = _norm_hm_str(hm_override, f"{lt.tm_hour:02d}:{lt.tm_min:02d}") if hm_override else f"{lt.tm_hour:02d}:{lt.tm_min:02d}"
    h, m = parse_hm(hm)
    cur_min = h * 60 + m

    ov = (cfg.get("point_overrides") or {}).get(hm)
    ov_dict = ov if isinstance(ov, dict) else {}

    t_mode = ov_dict.get("msg_time_mode")
    if t_mode not in ("datetime", "date_only", "none"):
        t_mode = cfg.get("dt_time_mode", "datetime")
    if t_mode not in ("datetime", "date_only", "none"):
        t_mode = "datetime"

    d_fmt = ov_dict.get("msg_date_fmt")
    if d_fmt not in ("YYYY-MM-DD", "YYYY年M月D日", "M月D日"):
        d_fmt = cfg.get("dt_date_fmt", "YYYY-MM-DD")
    if d_fmt not in ("YYYY-MM-DD", "YYYY年M月D日", "M月D日"):
        d_fmt = "YYYY-MM-DD"

    ov_msg_off = _clamp_day_offset(ov_dict.get("msg_day_offset"), None)
    if ov_msg_off is not None:
        d_off = ov_msg_off
        src = f"单点({hm})"
    elif cfg.get("dt_cutoff_enabled"):
        cut_hm = _norm_hm_str(cfg.get("dt_cutoff_hm"), "09:00")
        ch, cm = parse_hm(cut_hm)
        if cur_min < ch * 60 + cm:
            d_off = _clamp_day_offset(cfg.get("dt_before_cutoff_offset"), -1)
            src = f"{cut_hm}前跨天"
        else:
            d_off = _clamp_day_offset(cfg.get("dt_day_offset"), 0)
            src = "全局设置"
    else:
        d_off = _clamp_day_offset(cfg.get("dt_day_offset"), 0)
        src = "全局设置"

    return t_mode, d_fmt, d_off, src


def build_dingtalk_message_text(cfg, lt=None, hm_override=None, is_preview=False):
    """生成钉钉发送文案：支持日期+时间 / 仅日期 / 不加日期、单点日期格式、实际发送时钟与分时段后缀。"""
    lt = lt or time.localtime()
    hm = _norm_hm_str(hm_override, f"{lt.tm_hour:02d}:{lt.tm_min:02d}") if hm_override else f"{lt.tm_hour:02d}:{lt.tm_min:02d}"
    suffix, suffix_src = resolve_suffix_for_time(cfg, lt=lt, hm_override=hm)
    t_mode, d_fmt, d_off, time_src = resolve_dingtalk_time_policy(cfg, lt=lt, hm_override=hm)
    use_actual_clock = bool((cfg or {}).get("dt_use_actual_clock", False)) and (not is_preview)
    act_lt = time.localtime() if use_actual_clock else None
    base_date_lt = act_lt if (use_actual_clock and t_mode == "datetime") else lt
    shifted_lt = _shift_lt(base_date_lt, d_off)
    date_part = format_preset_date(lt=shifted_lt, date_fmt=d_fmt, day_offset=0)

    if t_mode == "datetime":
        if use_actual_clock:
            clock_hm = f"{act_lt.tm_hour:02d}:{act_lt.tm_min:02d}"
        else:
            clock_hm = hm
        stamp = f"{date_part} {clock_hm}"
    elif t_mode == "date_only":
        stamp = date_part
    else:
        stamp = ""

    if stamp and suffix:
        full_msg = f"{stamp} {suffix}"
    elif stamp:
        full_msg = stamp
    elif suffix:
        full_msg = suffix
    else:
        # 既选了不加日期时间又没填任何后缀时，保底带日期防止发送空串出错
        full_msg = date_part
    return {
        "text": full_msg,
        "stamp": stamp,
        "date_part": date_part,
        "suffix": suffix,
        "suffix_source": suffix_src,
        "time_mode": t_mode,
        "date_fmt": d_fmt,
        "day_offset": d_off,
        "time_source": time_src,
        "hm": hm,
        "actual_clock": use_actual_clock,
    }


def resolve_silent_for_time(cfg, lt=None, hm_override=None):
    """按单点覆盖 -> 全局默认 解析指定时刻是否开启静音发送，返回 (do_silent, source)。"""
    lt = lt or time.localtime()
    hm = _norm_hm_str(hm_override, f"{lt.tm_hour:02d}:{lt.tm_min:02d}") if hm_override else f"{lt.tm_hour:02d}:{lt.tm_min:02d}"
    ov = ((cfg or {}).get("point_overrides") or {}).get(hm)
    if isinstance(ov, dict) and isinstance(ov.get("silent"), bool):
        return bool(ov["silent"]), "单点设置"
    return bool((cfg or {}).get("dt_silent_send", True)), "全局默认"


def resolve_targets_for_time(cfg, lt=None, hm_override=None):
    """按单点定向发送对象 -> 全局启用群聊 解析指定时刻的钉钉发送目标列表，返回 (targets, source)。"""
    lt = lt or time.localtime()
    hm = _norm_hm_str(hm_override, f"{lt.tm_hour:02d}:{lt.tm_min:02d}") if hm_override else f"{lt.tm_hour:02d}:{lt.tm_min:02d}"
    ov = ((cfg or {}).get("point_overrides") or {}).get(hm)
    if isinstance(ov, dict):
        raw_tgts = ov.get("targets")
        if isinstance(raw_tgts, list):
            clean = [str(x).strip() for x in raw_tgts if str(x or "").strip()]
            if clean:
                return clean, "单点指定"
    return get_enabled_dt_targets(cfg), "全局默认"


_keep_awake_lock = threading.Lock()
_keep_awake_reasons = set()


_keep_awake_counts = {}
_keep_awake_worker_q = None
_keep_awake_worker_started = False


def _ensure_keep_awake_worker():
    global _keep_awake_worker_q, _keep_awake_worker_started
    if _keep_awake_worker_started:
        return _keep_awake_worker_q
    import queue as _ka_q
    q = _ka_q.SimpleQueue()
    _keep_awake_worker_q = q
    _keep_awake_worker_started = True

    def _ka_loop():
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.SetThreadExecutionState.argtypes = [wintypes.DWORD]
        k32.SetThreadExecutionState.restype = wintypes.DWORD
        while True:
            item = q.get()
            flags, done_ev, box = item
            try:
                prev = k32.SetThreadExecutionState(flags)
                box[0] = (prev != 0)
            except Exception:
                box[0] = False
            finally:
                done_ev.set()

    threading.Thread(target=_ka_loop, daemon=True, name="SheetMateKeepAwake").start()
    return q


def set_keep_awake(enable=True, reason="manual"):
    """由专用常驻线程调用 Win32 SetThreadExecutionState 并按引用计数防休眠，避免跨线程释放失效。"""
    if os.name != "nt":
        return False
    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001
    ES_DISPLAY_REQUIRED = 0x00000002
    r_key = str(reason or "manual")
    with _keep_awake_lock:
        if enable:
            _keep_awake_reasons.add(r_key)
            _keep_awake_counts[r_key] = _keep_awake_counts.get(r_key, 0) + 1
            flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
        else:
            cur_c = _keep_awake_counts.get(r_key, 0)
            if cur_c > 1:
                _keep_awake_counts[r_key] = cur_c - 1
            else:
                _keep_awake_counts.pop(r_key, None)
                _keep_awake_reasons.discard(r_key)
            if _keep_awake_counts:
                return True
            flags = ES_CONTINUOUS
        q = _ensure_keep_awake_worker()
    try:
        ev = threading.Event()
        box = [False]
        q.put((flags, ev, box))
        ev.wait(timeout=2.0)
        return bool(box[0])
    except Exception:
        return False


def archive_history_shot(src_path, keep=20):
    """归档最新汇总表截图到本地 history_shots 目录并滚动保留最近 keep 张。"""
    try:
        import shutil
        src = Path(src_path)
        if not src.exists():
            return None
        hist_dir = app_dir() / "history_shots"
        hist_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        dest = hist_dir / f"汇总_{stamp}.png"
        if dest.exists():
            dest = hist_dir / f"汇总_{stamp}_{int(time.time() * 1000) % 1000:03d}.png"
        shutil.copy2(src, dest)
        shots = sorted(
            [p for p in hist_dir.glob("*.png") if p.is_file()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old_p in shots[max(1, int(keep)):]:
            try:
                old_p.unlink()
            except Exception:
                pass
        return dest
    except Exception:
        return None


def diagnose_dingtalk_env():
    """一键检测钉钉安装路径、运行主窗、同权限一致性及图像匹配模板完整性（零键鼠扰动）。"""
    priv = check_dingtalk_privilege_match()
    procs = _list_dingtalk_processes()
    cands = _resolve_dingtalk_exe_candidates()
    existing_exe = next((p for p in cands if p and os.path.exists(p)), "")

    win_count = 0
    login_only = False
    has_aux_doc_win = False
    try:
        im_wins = _enum_dingtalk_im_windows(include_hidden=False)
        win_count = len(im_wins)
        if im_wins:
            try:
                import uiautomation as ua  # type: ignore[import-not-found]
                ctl = _safe_control_from_handle(ua, im_wins[0][0])
                if ctl is not None and _is_dingtalk_login_window(ctl):
                    login_only = True
            except Exception:
                pass
        for _hw, _cls, _ttl in _enum_top_windows():
            if _is_dingtalk_aux_window(_cls, _ttl) and ("钉钉" in (_ttl or "") or "DingTalk" in (_ttl or "")):
                has_aux_doc_win = True
                break
    except Exception:
        pass

    tpl_names = ("send_pill.png", "send_arrow.png", "menu_set_silent.png", "badge_silent_on.png")
    tpl_ok = [nm for nm in tpl_names if _find_bundle_asset(Path("templates") / nm) is not None]
    tpl_missing = [nm for nm in tpl_names if nm not in tpl_ok]

    self_lbl = "管理员权限" if priv.get("self_elevated") else "普通用户权限"
    ding_lbl = (
        "管理员权限" if priv.get("ding_elevated")
        else ("普通用户权限" if priv.get("ding_elevated") is False else "未运行")
    )
    win_status_txt = (
        "未登录/扫码页" if login_only
        else (f"已就绪（{win_count} 个聊天主窗）" if win_count
              else ("仅检测到钉钉-文档等子窗，聊天主窗在托盘" if has_aux_doc_win else "托盘隐藏或未打开"))
    )
    pc_prof = probe_pc_and_dingtalk_profile(None)
    lines = [
        f"• 当前电脑适配：{pc_prof['screen_w']}×{pc_prof['screen_h']}（缩放 {pc_prof['dpi_scale']:.2f}x）｜钉钉主题：{pc_prof['dt_theme_cn']}｜UIA响应：{pc_prof['uia_ms']}ms",
        f"• 本程序权限：{self_lbl}",
        f"• 钉钉进程：{len(procs)} 个（{ding_lbl}）",
        f"• 安装路径：{existing_exe or '暂未找到可执行文件'}",
        f"• 主窗口状态：{win_status_txt}",
        f"• 图像兜底模板：{len(tpl_ok)}/{len(tpl_names)} 张就绪" + (f"（缺失：{','.join(tpl_missing)}）" if tpl_missing else ""),
    ]
    if not priv.get("matched", True):
        lines.append("⚠️ 权限不一致，请关闭后以相同权限启动钉钉或本程序！")
    ok = bool(priv.get("matched", True)) and bool(tpl_missing == []) and (len(procs) > 0 or bool(existing_exe))
    return {
        "ok": ok,
        "privilege_matched": bool(priv.get("matched", True)),
        "proc_count": len(procs),
        "win_count": win_count,
        "login_only": login_only,
        "exe_path": existing_exe,
        "templates_missing": tpl_missing,
        "pc_profile": pc_prof,
        "summary": "\n".join(lines),
    }


def capture_allowed_now(cfg, now=None, hm_override=None):
    """自动计划是否允许此时截图发钉钉；先查单点覆盖，再查多时段允许窗口。"""
    now = now or time.localtime()
    hm = _norm_hm_str(hm_override, f"{now.tm_hour:02d}:{now.tm_min:02d}") if hm_override else f"{now.tm_hour:02d}:{now.tm_min:02d}"
    h, m = parse_hm(hm)
    cur_min = h * 60 + m

    ov = ((cfg or {}).get("point_overrides") or {}).get(hm)
    if isinstance(ov, dict):
        if ov.get("mode") == "send":
            return True
        if ov.get("mode") == "table_only":
            return False

    plan = (cfg or {}).get("capture_plan") or {}
    if not plan.get("enabled", False):
        return True

    windows = plan.get("windows")
    if isinstance(windows, list):
        for w in windows:
            if isinstance(w, dict) and w.get("enabled", True):
                if in_time_window(cur_min, w.get("start", "09:00"), w.get("end", "18:00"), inclusive_end=True):
                    return True
        return False

    profile = plan.get("weekend" if now.tm_wday >= 5 else "workday") or {}
    for i in (1, 2):
        if profile.get(f"slot{i}", False) and in_slot(
                now, profile.get(f"start{i}", "09:00"),
                profile.get(f"end{i}", "18:00")):
            return True
    return False


def build_daily_timetable(cfg):
    """根据白天与夜间时段设置，生成 00:00~23:59 全天确定性触发排期表及每点动作/后缀。"""
    cfg = normalize_config(cfg)
    points = {}  # minute -> period ("白天" | "夜间")

    slots = [("白天", cfg["day"])]
    if cfg.get("night_enabled", True):
        slots.append(("夜间", cfg["night"]))

    for period_label, sl in slots:
        gap = max(1, int(sl["ih"]) * 60 + int(sl["im"]))
        start_m = int(sl["sh"]) * 60 + int(sl["sm"])
        end_m = int(sl["eh"]) * 60 + int(sl["em"])
        if start_m < end_m:
            span = end_m - start_m
        elif start_m > end_m:
            span = (1440 - start_m) + end_m
        else:
            span = 1
        offset = 0
        guard = 0
        while offset < span and guard < 1442:
            m = (start_m + offset) % 1440
            if m not in points:
                points[m] = period_label
            offset += gap
            guard += 1

    extra_set = set()
    for ep in (cfg.get("extra_points") or []):
        hm_ep = _norm_hm_str(ep, "")
        if hm_ep:
            extra_set.add(hm_ep)
            eh_p, em_p = parse_hm(hm_ep)
            m_ep = eh_p * 60 + em_p
            if m_ep not in points:
                points[m_ep] = "自定义"

    g_cap = bool(cfg.get("enable_capture", True))
    g_dt = bool(cfg.get("enable_dingtalk", False))
    overrides = cfg.get("point_overrides") or {}
    now_lt = time.localtime()
    date_preset_item = next(
        (it for it in ((cfg.get("summary_presets") or {}).get("items") or [])
         if isinstance(it, dict) and it.get("enabled", True) and it.get("action") == "today"),
        None,
    )
    timetable = []
    for m in sorted(points.keys()):
        hm = f"{m // 60:02d}:{m % 60:02d}"
        ph, pm = m // 60, m % 60
        pt_lt = time.struct_time((now_lt.tm_year, now_lt.tm_mon, now_lt.tm_mday, ph, pm, 0, now_lt.tm_wday, now_lt.tm_yday, now_lt.tm_isdst))
        ov = overrides.get(hm) if isinstance(overrides.get(hm), dict) else {}
        ov_mode = ov.get("mode", "default")
        if ov_mode not in ("default", "send", "table_only"):
            ov_mode = "default"
        if ov_mode == "send":
            do_capture = True
            do_dingtalk = True
        elif ov_mode == "table_only":
            do_capture = False
            do_dingtalk = False
        else:
            allowed = capture_allowed_now(cfg, hm_override=hm)
            do_capture = g_cap and allowed
            do_dingtalk = g_dt and allowed and do_capture
        suf, suf_src = resolve_suffix_for_time(cfg, hm_override=hm)
        do_silent, silent_src = resolve_silent_for_time(cfg, hm_override=hm)
        sil_ov = ov.get("silent") if isinstance(ov.get("silent"), bool) else None
        has_custom_suf = ("suffix" in ov and ov["suffix"] is not None)

        if date_preset_item is not None and (cfg.get("summary_presets") or {}).get("enabled", True):
            s_off, s_src = resolve_preset_day_offset(date_preset_item, cfg=cfg, lt=pt_lt, hm_override=hm)
            s_prev = format_preset_date(lt=pt_lt, date_fmt=date_preset_item.get("date_fmt", "YYYY年M月D日"), day_offset=s_off)
        else:
            s_ov_raw = ov.get("sheet_day_offset")
            s_off = _clamp_day_offset(s_ov_raw, None) if s_ov_raw is not None else 0
            s_src = f"单点({hm})" if s_ov_raw is not None else "未启用"
            s_prev = format_preset_date(lt=pt_lt, date_fmt="YYYY年M月D日", day_offset=s_off)

        eff_tgts, tgt_src = resolve_targets_for_time(cfg, lt=pt_lt, hm_override=hm)
        ov_tgts = list(ov["targets"]) if isinstance(ov.get("targets"), list) and ov["targets"] else None
        msg_info = build_dingtalk_message_text(cfg, lt=pt_lt, hm_override=hm, is_preview=True)
        timetable.append({
            "time": hm,
            "min": m,
            "period": points[m],
            "is_extra": bool(hm in extra_set),
            "do_capture": do_capture,
            "do_dingtalk": do_dingtalk,
            "do_silent": do_silent,
            "silent_source": silent_src,
            "silent_override": sil_ov,
            "suffix": suf,
            "suffix_source": suf_src,
            "has_custom_suffix": has_custom_suf,
            "override_mode": ov_mode,
            "targets": eff_tgts,
            "targets_override": ov_tgts,
            "targets_source": tgt_src,
            "sheet_day_offset": ov.get("sheet_day_offset") if ov.get("sheet_day_offset") is not None else s_off,
            "sheet_offset_effective": s_off,
            "sheet_date_source": s_src,
            "sheet_date_preview": s_prev,
            "msg_time_mode": msg_info["time_mode"],
            "msg_date_fmt": msg_info["date_fmt"],
            "msg_day_offset": msg_info["day_offset"],
            "msg_time_source": msg_info["time_source"],
            "use_actual_clock": bool(cfg.get("dt_use_actual_clock", False)),
            "msg_preview": msg_info["text"],
        })
    return timetable


def compute_raw_sync_due(cfg, now_ts=None, last_paste_ts=0.0, next_send_ts=0.0, schedule_started_ts=0.0):
    """判定后台每 N 分钟刷底表是否到期；若临近正式发群轮次则自动避让。"""
    rs_raw = (cfg or {}).get("raw_sync") if isinstance(cfg, dict) and isinstance((cfg or {}).get("raw_sync"), dict) else {}
    rs = {**DEFAULT_CONFIG["raw_sync"], **rs_raw}
    try:
        iv = max(10, min(360, int(rs.get("interval_min", 40) or 40)))
    except Exception:
        iv = 40
    try:
        skip_min = max(0, min(30, int(rs.get("skip_near_send_min", 5) if rs.get("skip_near_send_min") is not None else 5)))
    except Exception:
        skip_min = 5

    if not rs.get("enabled", False):
        return {"enabled": False, "due": False, "next_ts": 0.0, "interval_min": iv, "skipped_reason": ""}

    now_ts = time.time() if now_ts is None else float(now_ts)
    now_lt = time.localtime(now_ts)
    if not is_run_day_allowed(cfg, now_lt):
        return {"enabled": True, "due": False, "next_ts": 0.0, "interval_min": iv, "skipped_reason": "今日不在运行周期"}

    if not rs.get("all_day", True):
        cur_m = now_lt.tm_hour * 60 + now_lt.tm_min
        in_win = in_time_window(cur_m, rs.get("start") or "00:00", rs.get("end") or "23:59", inclusive_end=True)
        if not in_win:
            return {"enabled": True, "due": False, "next_ts": 0.0, "interval_min": iv, "skipped_reason": "当前不在刷底表时段"}

    base_ts = float(last_paste_ts or 0.0)
    if base_ts <= 0.0:
        base_ts = float(schedule_started_ts or 0.0)
    due_ts = (base_ts + iv * 60.0) if base_ts > 0.0 else 0.0
    if due_ts > 0.0 and now_ts < due_ts:
        return {"enabled": True, "due": False, "next_ts": due_ts, "interval_min": iv, "skipped_reason": ""}

    next_send = float(next_send_ts or 0.0)
    if skip_min > 0 and next_send > 0.0 and -60.0 <= (next_send - now_ts) <= skip_min * 60.0:
        rem_min = max(1, int((next_send - now_ts + 59) // 60))
        return {
            "enabled": True,
            "due": False,
            "next_ts": next_send,
            "interval_min": iv,
            "skipped_reason": f"距下次正式发群仅余 {rem_min} 分钟，自动避让等待发群轮同步更新",
        }
    return {"enabled": True, "due": True, "next_ts": now_ts, "interval_min": iv, "skipped_reason": ""}


def cleanup_wps_tabs(context, keep=MAX_KEEP_WPS_TABS, wps_url=None):
    try:
        target = wps_url or WPS_URL
        wps_pages = [
            pg for pg in context.pages
            if target in (pg.url or "") or "kdocs.cn/l/" in (pg.url or "")
        ]
        if len(wps_pages) > keep:
            for pg in wps_pages[:-keep]:
                try:
                    pg.close()
                    log("  已关闭旧的表格页面")
                except Exception:
                    pass
    except Exception as e:
        log(f"  清理页面时跳过：{e}")


# 偶发网络抖动错误关键词，命中则自动等待重试，不直接中断
NETWORK_RETRY_ERRS = (
    "ERR_NETWORK_CHANGED",
    "ERR_INTERNET_DISCONNECTED",
    "ERR_NETWORK_IO_SUSPENDED",
    "ERR_CONNECTION_RESET",
    "ERR_CONNECTION_CLOSED",
    "ERR_CONNECTION_REFUSED",
    "ERR_TIMED_OUT",
    "ERR_PROXY_CONNECTION_FAILED",
    "ERR_NAME_NOT_RESOLVED",
    "Timeout",
    "net::ERR",
)


def goto_with_retry(page, url, wait_until="networkidle", timeout=60000, tries=3):
    """打开网页，遇到网络抖动/超时自动重试，最多 tries 次。"""
    last = None
    for i in range(tries):
        check_stop()
        try:
            page.goto(url, wait_until=wait_until, timeout=timeout)
            return
        except Exception as e:
            last = e
            if i < tries - 1 and any(k in str(e) for k in NETWORK_RETRY_ERRS):
                wait = 3 + i * 3
                log(f"  网络抖动，{wait} 秒后自动重试（第 {i + 1}/{tries} 次）")
                sleep_check(wait)
            else:
                raise
    raise last


def download_xlsx(page, name, url, save_dir):
    check_stop()
    log(f"正在下载【{name}】...")
    save_dir.mkdir(exist_ok=True)
    icon_sels = [
        "[class*='download']", "[aria-label*='download']", "[title*='下载']",
        "button:has(.icon-download)", "svg[class*='download']",
    ]
    icon_combined = ", ".join(f"{s}:visible" for s in icon_sels)
    download_btn = ("button:has-text('Download'):visible, "
                    ".modal button:has-text('Download'):visible, "
                    "[role='dialog'] button:has-text('Download'):visible")

    for attempt in range(2):
        check_stop()
        # 页面有持续请求，networkidle 会白等；等表格真正渲染后再找下载入口。
        goto_with_retry(page, url, wait_until="domcontentloaded")
        try:
            page.wait_for_selector("table", state="visible", timeout=25000)
        except Exception:
            log("  表格仍在加载，继续等待下载入口…")
        if attempt:
            log("  刷新页面后重试下载…")

        log("  点右下角下载图标...")
        try:
            page.click(icon_combined, timeout=18000)
        except Exception:
            try:
                page.click("text=Download", timeout=3000)
            except Exception:
                log("  ⚠️ 没找到下载按钮")
                continue

        try:
            page.click("text=.xlsx", timeout=2000)
        except Exception:
            pass

        log("  点Download开始下载...")
        try:
            with page.expect_download(timeout=60000) as dl_info:
                page.click(download_btn, timeout=10000)
            dl = dl_info.value
            save_path = save_dir / f"{name}.xlsx"
            dl.save_as(str(save_path))
            with open(save_path, "rb") as _rf_hdr:
                hdr = _rf_hdr.read(4)
            if not hdr.startswith(b"PK"):
                try:
                    save_path.unlink()
                except Exception:
                    pass
                log(f"  ⚠️ 【{name}】下载文件内容不是有效 .xlsx（可能格式未切换成功），准备重试")
                continue
            log(f"✅ 【{name}】下载完成")
            return save_path
        except Exception as e_dl:
            if _is_stop_exception(e_dl):
                raise
            log(f"  【{name}】本次下载未成功")
    log(f"  【{name}】两次下载均未成功")
    return None


def _sanitize_tsv_cell(cell_val):
    if cell_val is None:
        return ""
    s = str(cell_val)
    return s.replace("\r\n", " ").replace("\r", " ").replace("\n", " ").replace("\t", " ")


def xlsx_to_tsv(xlsx_path, tsv_path):
    wb = openpyxl.load_workbook(xlsx_path, read_only=True)
    try:
        ws = wb.active
        with open(tsv_path, "w", encoding="utf-8", newline="") as f:
            for row in ws.iter_rows(values_only=True):
                f.write("\t".join(_sanitize_tsv_cell(c) for c in row) + "\n")
    finally:
        try:
            wb.close()
        except Exception:
            pass
    log(f"✅ 【{tsv_path.stem}】格式转换完成")


def switch_wps_sheet(page, name, tries=3):
    """可靠切换 WPS 工作表：has-text 子串匹配 + 标签栏滚动 + 选中态验证。"""
    item_sel = f".et-status-sheet-item:has-text('{name}')"
    for _ in range(tries):
        for _ in range(8):
            loc = page.locator(item_sel)
            try:
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click(timeout=5000)
                    break
            except Exception:
                pass
            try:
                page.locator(".bar-btn.next-btn").first.click(timeout=2000)
                time.sleep(0.6)
            except Exception:
                break
        try:
            page.wait_for_selector(
                f".et-status-sheet-item.selected:has-text('{name}')",
                timeout=8000)
            return True
        except Exception:
            time.sleep(1)
    return False


def paste_to_wps(page, sheet_name, tsv_path):
    with hands_off_guard(f"WPS 表格【{sheet_name}】中粘贴数据"):
        check_stop()
        log(f"正在写入【{sheet_name}】...")
        if not switch_wps_sheet(page, sheet_name):
            raise RuntimeError(f"没找到【{sheet_name}】这个工作表")

        with open(tsv_path, "r", encoding="utf-8") as f:
            content = f.read()
        if not set_clipboard_windows(content):
            raise RuntimeError(f"无法将【{sheet_name}】数据写入系统剪贴板，已中止以避免误清空底表")
        if content.strip():
            clip_chk = get_clipboard_text_windows()
            if not clip_chk or clip_chk[:40] != content[:40]:
                if not set_clipboard_windows(content):
                    raise RuntimeError(f"剪贴板回读校验失败，已中止清空【{sheet_name}】")

        sleep_check(3)
        page.keyboard.press("Control+A")
        sleep_check(0.5)
        page.keyboard.press("Delete")
        sleep_check(1)

        sleep_check(0.2)
        page.keyboard.press("Control+V")
        sleep_check(3)
        log(f"✅ 【{sheet_name}】写入完成")


def check_wps_logged_in(page):
    check_selectors = ["text='fx'", "text='开始'", "text='插入'", "text='数据'",
                       "text='样式'", "text='合并'", "text='WPS AI'"]
    for sel in check_selectors:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                return True
        except Exception:
            pass
    for frame in page.frames:
        for sel in check_selectors:
            try:
                el = frame.query_selector(sel)
                if el and el.is_visible():
                    return True
            except Exception:
                pass
    return False


def click_in_all_frames(page, button_text, max_wait=20):
    for _i in range(max_wait):
        sleep_check(1)
        for sel in [
            f"button:has-text('{button_text}')",
            f"text='{button_text}'",
            f"[role='dialog'] button:has-text('{button_text}')",
        ]:
            try:
                page.click(sel, timeout=800)
                log(f"  点了「{button_text}」")
                return True
            except Exception:
                pass
        for frame in page.frames:
            for sel in [f"button:has-text('{button_text}')", f"text='{button_text}'"]:
                try:
                    frame.click(sel, timeout=500)
                    log(f"  点了「{button_text}」")
                    return True
                except Exception:
                    pass
    return False


def wait_wps_login(page, max_wait=1200):
    """等待 WPS 登录成功：未登录时点「立即登录/确认登录」，出现扫码页则暂停等用户扫码。"""
    global _runtime_prompt, _recovery_choice
    if check_wps_logged_in(page):
        return True
    log("  需要登录 WPS，正在找登录入口...")
    click_in_all_frames(page, "立即登录", max_wait=15)
    sleep_check(3)
    click_in_all_frames(page, "确认登录", max_wait=10)
    log("  ⏳ 请在浏览器里扫码登录，登录成功后会自动继续（最多等 20 分钟）")
    show_login_prompt()
    with _runtime_lock:
        _recovery_choice = None
        _runtime_prompt = {
            "mode": "login_wps",
            "title": "WPS 需要扫码登录",
            "detail": (
                "1. 请在弹出的浏览器窗口中用 WPS / 微信扫码登录；\n"
                "2. 扫码成功后程序会自动继续（也可点击【我已扫码·继续】立即检测）。"
            ),
            "hint": "按键提示：扫码后点【我已扫码·继续】加快检测，点【取消】终止本轮。",
            "actions": [
                {"id": "check_now", "label": "我已扫码·继续", "style": "primary"},
            ],
        }
    _notify_runtime_state()
    deadline = time.monotonic() + max_wait
    try:
        while time.monotonic() < deadline:
            check_stop()
            for _ in range(10):
                check_stop()
                with _runtime_lock:
                    if _recovery_choice == "check_now":
                        _recovery_choice = None
                        break
                time.sleep(0.2)
            if check_wps_logged_in(page):
                log("  ✅ WPS 登录成功")
                return True
            click_in_all_frames(page, "立即登录", max_wait=2)
        return False
    finally:
        close_login_prompt()
        with _runtime_lock:
            if _runtime_prompt.get("mode") == "login_wps":
                _runtime_prompt = _default_running_prompt()
            _recovery_choice = None
        _notify_runtime_state()


def _col_letter_to_idx(letter):
    n = 0
    for ch in letter.upper():
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n


def _idx_to_col_letter(n):
    chars = []
    num = int(n)
    while num > 0:
        num, rem = divmod(num - 1, 26)
        chars.append(chr(ord("A") + rem))
    return "".join(reversed(chars))


def _main_canvas_box(page):
    """表格主体是面积最大的 canvas，返回其 bounding box。"""
    boxes = [c.bounding_box() for c in page.query_selector_all("canvas")]
    boxes = [b for b in boxes if b]
    if not boxes:
        return None
    return max(boxes, key=lambda b: b["width"] * b["height"])


def _goto_cell(page, addr):
    """名称框（input.edit-box）只支持定位单个单元格。"""
    eb = page.locator("input.edit-box").first
    eb.click(timeout=5000)
    page.keyboard.press("Control+A")
    eb.fill(addr)
    page.keyboard.press("Enter")
    time.sleep(0.45)


def format_preset_date(lt=None, date_fmt="YYYY年M月D日", day_offset=0):
    """按配置格式与天数偏移生成查询日期字符串，精确支持跨月/跨年。"""
    lt = _shift_lt(lt or time.localtime(), day_offset=day_offset)
    if date_fmt == "M月D日":
        return f"{lt.tm_mon}月{lt.tm_mday}日"
    if date_fmt == "YYYY-MM-DD":
        return f"{lt.tm_year:04d}-{lt.tm_mon:02d}-{lt.tm_mday:02d}"
    return f"{lt.tm_year:04d}年{lt.tm_mon}月{lt.tm_mday}日"


def expand_preset_cells(raw_cell):
    """展开单格、逗号分隔或小区间（如 D4:E4 -> ['D4', 'E4']）为规范单元格地址列表。"""
    out = []
    seen = set()
    for part in re.split(r"[\s,;，、]+", str(raw_cell or "").strip().upper()):
        if not part:
            continue
        m_rng = re.fullmatch(r"([A-Z]{1,3})([1-9]\d{0,5}):([A-Z]{1,3})([1-9]\d{0,5})", part)
        if m_rng:
            c1, r1, c2, r2 = m_rng.group(1), int(m_rng.group(2)), m_rng.group(3), int(m_rng.group(4))
            lo_x, hi_x = sorted((_col_letter_to_idx(c1), _col_letter_to_idx(c2)))
            lo_r, hi_r = sorted((r1, r2))
            if (hi_x - lo_x + 1) * (hi_r - lo_r + 1) <= 20:
                for rr in range(lo_r, hi_r + 1):
                    for cc in range(lo_x, hi_x + 1):
                        addr = f"{_idx_to_col_letter(cc)}{rr}"
                        if addr not in seen:
                            seen.add(addr)
                            out.append(addr)
                continue
        if re.fullmatch(r"[A-Z]{1,3}[1-9]\d{0,5}", part) and part not in seen:
            seen.add(part)
            out.append(part)
    return out


def _normalize_preset_text(s):
    txt = str(s or "").replace("\r", "").replace("\n", "").replace("\t", "")
    return re.sub(r"\s+", "", txt).replace("，", ",").strip()


def _preset_readback_matches(action, expected_val, actual_val, lt=None, day_offset=0):
    """比对从 WPS 单元格回读到的文本是否已符合预期目标。"""
    act_norm = _normalize_preset_text(actual_val)
    if action == "clear":
        return act_norm == ""
    if not act_norm:
        return False
    exp_norm = _normalize_preset_text(expected_val)
    if action == "today":
        if act_norm == exp_norm:
            return True
        target_lt = _shift_lt(lt or time.localtime(), day_offset=day_offset)
        try:
            nums = [int(x) for x in re.findall(r"\d+", act_norm)]
        except Exception:
            nums = []
        ty, tm, td = target_lt.tm_year, target_lt.tm_mon, target_lt.tm_mday
        if len(nums) >= 3:
            return any(nums[i] == ty and nums[i + 1] == tm and nums[i + 2] == td for i in range(len(nums) - 2))
        if len(nums) >= 2:
            return any(nums[i] == tm and nums[i + 1] == td for i in range(len(nums) - 1))
        return False
    return act_norm == exp_norm


def _focus_wps_formula_bar(page):
    """尝试点击 WPS 顶部公式编辑栏，不可用时返回 False 以便回退 F2 编辑态。"""
    selectors = [
        ".formula-input",
        ".et-formula-bar input",
        ".et-formula-bar textarea",
        "[class*='formula-bar'] [contenteditable='true']",
        "[class*='formula'] [contenteditable='true']",
        "[class*='formula'] textarea",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                loc.click(timeout=2000)
                return True
        except Exception:
            pass
    return False


def _write_wps_cell_via_edit_mode(page, cell, val_str):
    """通过名称框定位后进入公式栏或 F2 编辑态写入目标文本并回车提交。"""
    _goto_cell(page, cell)
    if not _focus_wps_formula_bar(page):
        page.keyboard.press("F2")
        time.sleep(0.2)
    page.keyboard.press("Control+A")
    time.sleep(0.08)
    page.keyboard.press("Backspace")
    time.sleep(0.08)
    set_clipboard_windows(val_str)
    sleep_check(0.15)
    page.keyboard.press("Control+V")
    sleep_check(0.25)
    page.keyboard.press("Enter")
    sleep_check(0.3)
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass


def format_preset_now(lt=None, time_fmt="full"):
    """按配置格式生成写入单元格的当前精确时间文本。"""
    t = lt or time.localtime()
    if time_fmt == "hm":
        return f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d} {t.tm_hour:02d}:{t.tm_min:02d}"
    if time_fmt == "time_only":
        return f"{t.tm_hour:02d}:{t.tm_min:02d}:{t.tm_sec:02d}"
    return f"{t.tm_year:04d}年{t.tm_mon:02d}月{t.tm_mday:02d}日{t.tm_hour:02d}:{t.tm_min:02d}:{t.tm_sec:02d}"


def apply_wps_summary_presets(page, cfg, lt=None, hm_override=None, after_data_paste=True):
    """在切换到数据汇总表后、等待刷新与截图前，按任意工作表与格位精准写入用户自定义内容。"""
    sp = (cfg or {}).get("summary_presets")
    if not isinstance(sp, dict) or not sp.get("enabled", True):
        return 0
    items = [it for it in (sp.get("items") or []) if isinstance(it, dict) and it.get("enabled", True)]
    if not items:
        return 0

    lt = lt or time.localtime()
    applied = 0
    log(f"  正在通过名称框校准表格 {len(items)} 项自定义单元格（无需表格框线）…")
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass

    current_sheet = "数据汇总"
    # 先执行非「数据汇总」页签，再统一写「数据汇总」，避免在单测 MockPage 场景调用切表
    ordered_items = [it for it in items if str(it.get("sheet") or "数据汇总").strip() != "数据汇总"] + [
        it for it in items if str(it.get("sheet") or "数据汇总").strip() in ("", "数据汇总")
    ]

    for it in ordered_items:
        check_stop()
        if it.get("only_after_paste", False) and not after_data_paste:
            continue
        cells = expand_preset_cells(it.get("cell"))
        if not cells:
            continue
        target_sheet = str(it.get("sheet") or "数据汇总").strip() or "数据汇总"
        if target_sheet != current_sheet and hasattr(page, "locator"):
            try:
                if switch_wps_sheet(page, target_sheet):
                    current_sheet = target_sheet
                    sleep_check(0.8)
                else:
                    log(f"  ⚠️ 切换工作表「{target_sheet}」失败，跳过对应格位")
                    continue
            except Exception as e_sw:
                if _is_stop_exception(e_sw):
                    raise
                log(f"  ⚠️ 切换工作表「{target_sheet}」失败，跳过对应格位：{e_sw}")
                continue

        action = str(it.get("action") or "value").strip().lower()
        date_fmt = str(it.get("date_fmt") or "YYYY年M月D日").strip()
        time_fmt = str(it.get("time_fmt") or "full").strip()
        label = str(it.get("label") or "").strip()
        day_off = 0
        off_desc = ""
        if action == "today":
            day_off, off_src = resolve_preset_day_offset(it, cfg=cfg, lt=lt, hm_override=hm_override)
            val_str = format_preset_date(lt=lt, date_fmt=date_fmt, day_offset=day_off)
            if day_off != 0:
                off_desc = f" [偏移{day_off:+d}天 · {off_src}]"
        elif action == "now":
            val_str = format_preset_now(lt=lt, time_fmt=time_fmt)
        elif action == "clear":
            val_str = ""
        else:
            val_str = "" if it.get("value") is None else str(it.get("value")).strip()
            if not val_str:
                action = "clear"
            elif val_str.startswith("=") and action != "formula":
                action = "formula"

        sheet_prefix = f"[{target_sheet}] " if target_sheet != "数据汇总" else ""
        tag = f"（{label}）" if label else ""
        for cell in cells:
            check_stop()
            _goto_cell(page, cell)
            if action == "clear":
                page.keyboard.press("Delete")
                sleep_check(0.25)
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
                log(f"  ✅ 已清空单元格 {sheet_prefix}{cell}{tag}")
                applied += 1
                continue

            if action == "formula":
                _write_wps_cell_via_edit_mode(page, cell, val_str)
                log(f"  ✅ 已写入公式单元格 {sheet_prefix}{cell}{tag} -> {val_str}")
                applied += 1
                continue

            page.keyboard.press("Delete")
            sleep_check(0.15)
            set_clipboard_windows(val_str)
            sleep_check(0.12)
            page.keyboard.press("Control+V")
            sleep_check(0.35)
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            sleep_check(0.18)

            readback = ""
            try:
                clear_clipboard_windows()
                _goto_cell(page, cell)
                page.keyboard.press("Control+C")
                dl = time.monotonic() + 0.55
                while time.monotonic() < dl:
                    time.sleep(0.12)
                    readback = get_clipboard_text_windows()
                    if readback:
                        break
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
            except Exception:
                readback = ""

            if action == "now":
                rb_n = _normalize_preset_text(readback)
                if rb_n and ("#" in rb_n or (time_fmt != "hm" and len(re.findall(r"\d+", rb_n)) < 5 and ":" not in rb_n)):
                    _write_wps_cell_via_edit_mode(page, cell, "'" + val_str)
            elif readback and not _preset_readback_matches(action, val_str, readback, lt=lt, day_offset=day_off):
                log(f"  [预设校准] {sheet_prefix}{cell}{tag} 回读「{_normalize_preset_text(readback)[:24]}」，改用公式栏/编辑态重写入…")
                _write_wps_cell_via_edit_mode(page, cell, val_str)
            elif not readback and action == "value" and _focus_wps_formula_bar(page):
                _write_wps_cell_via_edit_mode(page, cell, val_str)

            log(f"  ✅ 已校准单元格 {sheet_prefix}{cell}{tag} -> {val_str}{off_desc}")
            applied += 1

    if current_sheet != "数据汇总" and hasattr(page, "locator"):
        try:
            switch_wps_sheet(page, "数据汇总")
            sleep_check(0.6)
        except Exception as e_ret:
            if _is_stop_exception(e_ret):
                raise
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    try:
        _goto_cell(page, "C6")
    except Exception:
        pass
    return applied


def format_update_timestamp(lt=None):
    """生成数据粘贴完成后的秒级最后更新时间文本，例如 2026年11月21日13:55:01。"""
    t = lt or time.localtime()
    return f"{t.tm_year:04d}年{t.tm_mon:02d}月{t.tm_mday:02d}日{t.tm_hour:02d}:{t.tm_min:02d}:{t.tm_sec:02d}"


def apply_wps_update_timestamp(page, cfg, after_data_paste=True, now_lt=None):
    """在数据粘贴完成后，通过名称框定位并在 R4（对应 P4 最后更新时间）写入当前精确到秒的时间。"""
    raw_ut = (cfg or {}).get("update_time_cell")
    if not isinstance(raw_ut, dict):
        ut = {"enabled": True, "cell": "R4", "only_after_paste": True}
    else:
        ut = raw_ut
    if not ut.get("enabled", True):
        return 0
    if ut.get("only_after_paste", True) and not after_data_paste:
        return 0

    cells = expand_preset_cells(ut.get("cell") or "R4") or ["R4"]
    ts_str = format_update_timestamp(now_lt)
    written = 0

    try:
        page.keyboard.press("Escape")
    except Exception:
        pass

    for cell in cells:
        check_stop()
        _goto_cell(page, cell)
        page.keyboard.press("Delete")
        sleep_check(0.15)
        set_clipboard_windows(ts_str)
        sleep_check(0.12)
        page.keyboard.press("Control+V")
        sleep_check(0.35)
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        sleep_check(0.18)

        readback = ""
        try:
            clear_clipboard_windows()
            _goto_cell(page, cell)
            page.keyboard.press("Control+C")
            dl = time.monotonic() + 0.55
            while time.monotonic() < dl:
                time.sleep(0.12)
                readback = get_clipboard_text_windows()
                if readback:
                    break
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
        except Exception:
            readback = ""

        rb_norm = _normalize_preset_text(readback)
        if rb_norm and ("#" in rb_norm or (":" not in rb_norm and len(re.findall(r"\d+", rb_norm)) < 5)):
            log(f"  [最后更新时间] {cell} 回读「{rb_norm[:24]}」，改用纯文本编辑态重写入…")
            _write_wps_cell_via_edit_mode(page, cell, "'" + ts_str)
        elif not rb_norm and _focus_wps_formula_bar(page):
            _write_wps_cell_via_edit_mode(page, cell, ts_str)

        log(f"  ✅ 已写入最后更新时间 {cell}（对应 P4） -> {ts_str}")
        written += 1

    try:
        _goto_cell(page, "C6")
    except Exception:
        pass
    return written


def _detect_last_row(page, start_cell="Q8"):
    """清空剪贴板后复制指定列，原生 Win32 轮询解析真实数据末行。"""
    try:
        clear_clipboard_windows()
        _goto_cell(page, start_cell)
        page.keyboard.press("Control+Shift+ArrowDown")
        time.sleep(0.4)
        page.keyboard.press("Control+C")
        text = ""
        deadline = time.time() + 2.5
        while time.time() < deadline:
            time.sleep(0.15)
            text = get_clipboard_text_windows()
            if text:
                break
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        if not text:
            return None
        lines = text.splitlines()
        last = -1
        for i, line in enumerate(lines):
            if line.strip():
                last = i
        if last >= 0:
            start_row = int(re.search(r"\d+", start_cell).group())
            return start_row + last
    except Exception:
        return None
    return None


_C6_BANNER_RGB = (54, 76, 116)  # #364C74：数据汇总第 6 行深蓝标题栏（11月24日…个人数据明细）


def _mouse_move_smooth(page, x, y, steps=12):
    """兼容真实 Playwright 与单元测试 Mock 的平滑鼠标移动辅助函数。"""
    try:
        page.mouse.move(float(x), float(y), steps=int(steps))
    except TypeError:
        page.mouse.move(float(x), float(y))


def _set_wps_zoom(page, percent):
    """通过右下角真实缩放控件设置比例，放慢等待画布重排稳态并回读确认。"""
    wanted = f"{percent}%"
    is_hi = int(percent) >= 150
    for _attempt in range(2):
        try:
            page.click("div.zoom-value", timeout=4000)
            sleep_check(0.35)
            zin = None
            for el in page.query_selector_all("input"):
                try:
                    if el.is_visible() and (el.input_value() or "").strip().isdigit():
                        zin = el
                        break
                except Exception:
                    pass
            if zin is not None:
                zin.fill(str(percent))
                page.keyboard.press("Enter")
            else:
                page.click(f".panel-item.only-text:text-is('{wanted}')", timeout=2500)
            sleep_check(1.15 if is_hi else 0.75)
            actual = (page.locator("div.zoom-value").inner_text() or "").strip()
            if actual == wanted:
                log(f"  缩放比例已确认为 {wanted}")
                sleep_check(0.45 if is_hi else 0.25)
                return
        except Exception as e_zm:
            if "手动停止" in str(e_zm):
                raise
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
    raise RuntimeError(f"表格缩放没有设置到 {wanted}")


def _find_wps_green_pixels(im, dr=24, dg=30, db=30):
    """矢量化提取图像中 WPS 绿色选中边框像素坐标 (xs_list, ys_list)，优先使用 NumPy 零整型转换提速。"""
    idr, idg, idb = int(dr), int(dg), int(db)
    if idr <= 0 or idg <= 0 or idb <= 0:
        return [], []
    try:
        import numpy as np  # type: ignore[import-not-found]
        arr = np.asarray(im)
        r_lo, r_hi = max(0, 16 - idr + 1), min(255, 16 + idr - 1)
        g_lo, g_hi = max(0, 153 - idg + 1), min(255, 153 + idg - 1)
        b_lo, b_hi = max(0, 104 - idb + 1), min(255, 104 + idb - 1)
        r_ch, g_ch, b_ch = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
        mask = (
            (r_ch >= r_lo) & (r_ch <= r_hi)
            & (g_ch >= g_lo) & (g_ch <= g_hi)
            & (b_ch >= b_lo) & (b_ch <= b_hi)
        )
        ys, xs = np.nonzero(mask)
        return xs.tolist(), ys.tolist()
    except Exception:
        pix = im.load()
        xs = []
        ys = []
        w, h = im.size
        for y in range(h):
            for x in range(w):
                r, g, b = pix[x, y]
                if abs(r - 16) < idr and abs(g - 153) < idg and abs(b - 104) < idb:
                    xs.append(x)
                    ys.append(y)
        return xs, ys


def _find_c6_header_banner_bbox(im, min_width_ratio=0.18, min_height_px=12):
    """基于 #364C74 (RGB 54,76,116) 精确识别汇总表第 6 行（C6 区域）深蓝标题横条像素边界。"""
    w, h = im.size
    if w < 60 or h < 40:
        return None
    top_guard = min(max(12, int(h * 0.045)), max(0, h // 3))
    left_guard = min(max(12, int(w * 0.035)), max(0, w // 4))
    min_row_px = max(36, int(w * float(min_width_ratio)))
    mask = None
    try:
        import numpy as np  # type: ignore[import-not-found]
        arr = np.asarray(im)
        sub = arr[top_guard:, left_guard:]
        r = sub[:, :, 0].astype(np.int16)
        g = sub[:, :, 1].astype(np.int16)
        b = sub[:, :, 2].astype(np.int16)
        mask = (
            (np.abs(r - _C6_BANNER_RGB[0]) <= 26)
            & (np.abs(g - _C6_BANNER_RGB[1]) <= 30)
            & (np.abs(b - _C6_BANNER_RGB[2]) <= 28)
            & (b >= g + 8)
            & (g >= r + 4)
            & (b >= 84)
        )
        row_cnts = mask.sum(axis=1)
        hit_rel = np.flatnonzero(row_cnts >= min_row_px).tolist()
    except Exception:
        pix = im.load()
        hit_rel = []
        for y in range(top_guard, h):
            cnt = 0
            for x in range(left_guard, w):
                rv, gv, bv = pix[x, y]
                if (
                    abs(rv - _C6_BANNER_RGB[0]) <= 26
                    and abs(gv - _C6_BANNER_RGB[1]) <= 30
                    and abs(bv - _C6_BANNER_RGB[2]) <= 28
                    and bv >= gv + 8
                    and gv >= rv + 4
                    and bv >= 84
                ):
                    cnt += 1
            if cnt >= min_row_px:
                hit_rel.append(y - top_guard)
    if not hit_rel:
        return None

    bands = []
    bs = hit_rel[0]
    be = hit_rel[0]
    for ry in hit_rel[1:]:
        if ry <= be + 3:
            be = ry
        else:
            bands.append((bs, be))
            bs = be = ry
    bands.append((bs, be))

    best = None
    best_score = -1
    for bs, be in bands:
        bh = be - bs + 1
        if bh < int(min_height_px):
            continue
        if mask is not None:
            import numpy as np  # type: ignore[import-not-found]
            band_m = mask[bs : be + 1, :]
            px_cnt = int(band_m.sum())
            col_cnts = band_m.sum(axis=0)
            col_thresh = max(2, bh // 3) if bh >= 6 else 1
            xs_rel = np.flatnonzero(col_cnts >= col_thresh)
            if len(xs_rel) == 0:
                xs_rel = np.flatnonzero(band_m.any(axis=0))
            if len(xs_rel) == 0:
                continue
            px0 = int(xs_rel[0]) + left_guard
            px1 = int(xs_rel[-1]) + left_guard
        else:
            pix = im.load()
            xs_all = []
            px_cnt = 0
            for y in range(bs + top_guard, be + top_guard + 1):
                for x in range(left_guard, w):
                    rv, gv, bv = pix[x, y]
                    if (
                        abs(rv - _C6_BANNER_RGB[0]) <= 26
                        and abs(gv - _C6_BANNER_RGB[1]) <= 30
                        and abs(bv - _C6_BANNER_RGB[2]) <= 28
                        and bv >= gv + 8
                        and gv >= rv + 4
                        and bv >= 84
                    ):
                        px_cnt += 1
                        xs_all.append(x)
            if not xs_all:
                continue
            px0 = min(xs_all)
            px1 = max(xs_all)
        bw = px1 - px0 + 1
        if bw < min_row_px:
            continue
        cov = float(px_cnt) / float(max(1, bw * bh))
        if cov < 0.28:
            continue
        if px_cnt > best_score:
            best_score = px_cnt
            best = (px0, bs + top_guard, px1, be + top_guard, px_cnt, cov)
    return best


def _capture_main_canvas_rgb(page):
    """截取主表格 canvas 的 RGB 图像与 DPR 换算比例，返回 (box, im_rgb, scale_x, scale_y)。"""
    box = _main_canvas_box(page)
    if box is None:
        raise RuntimeError("没有找到表格画布")
    canvases = []
    for c in page.query_selector_all("canvas"):
        try:
            r = c.bounding_box()
            if r:
                canvases.append((c, r))
        except Exception:
            pass
    if not canvases:
        raise RuntimeError("表格画布尚未准备好")
    target, t_box = max(canvases, key=lambda it: it[1]["width"] * it[1]["height"])
    use_box = t_box or box
    shot_buf = io.BytesIO(target.screenshot())
    try:
        with Image.open(shot_buf) as raw_im:
            im_rgb = raw_im.convert("RGB")
        scale_x = max(1.0, float(im_rgb.width) / max(1.0, float(use_box["width"])))
        scale_y = max(1.0, float(im_rgb.height) / max(1.0, float(use_box["height"])))
        return use_box, im_rgb, scale_x, scale_y
    finally:
        try:
            shot_buf.close()
        except Exception:
            pass


def _largest_pixel_component_rect(pts, min_len=6):
    """从像素点集中提取最大 8-连通分量外接框 (min_x, min_y, max_x, max_y)。"""
    if len(pts) < int(min_len):
        return None
    remaining = set(pts)
    comps = []
    while remaining:
        seed = remaining.pop()
        stack = [seed]
        comp_i = [seed]
        while stack:
            x, y = stack.pop()
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    q = (x + dx, y + dy)
                    if q in remaining:
                        remaining.remove(q)
                        stack.append(q)
                        comp_i.append(q)
        comps.append(comp_i)
    comp = max(comps, key=len)
    if len(comp) < int(min_len):
        return None
    return (
        min(x for x, _ in comp),
        min(y for _, y in comp),
        max(x for x, _ in comp),
        max(y for _, y in comp),
    )


def _diff_active_cell_rect(im_ref, im_cur, scale_x, scale_y, min_x_css=0.0, min_y_css=0.0):
    """通过 A1 参考帧与目标帧的像素差分精确提取新激活单元格绿框，彻底排除第1行图标与远端静态绿块干扰。"""
    if im_ref is None or im_cur is None or im_ref.size != im_cur.size:
        return None
    w, h = im_cur.size
    left_px = max(int(14 * scale_x), int(float(min_x_css) * scale_x))
    top_px = max(int(12 * scale_y), int(float(min_y_css) * scale_y))
    if left_px >= w - 4 or top_px >= h - 4:
        left_px = 0
        top_px = 0
    try:
        import numpy as np  # type: ignore[import-not-found]
        a = np.asarray(im_ref)
        b = np.asarray(im_cur)
        # 检查背景是否发生大面积滚动（抽样中位像素差应近似为 0）
        bg_diff = np.abs(a[::16, ::16].astype(np.int16) - b[::16, ::16].astype(np.int16))
        if float(np.median(bg_diff)) > 4.0:
            return None
        diff_sub = np.max(np.abs(a[top_px:, left_px:].astype(np.int16) - b[top_px:, left_px:].astype(np.int16)), axis=2)
        sub_b = b[top_px:, left_px:]
        r_ch, g_ch, b_ch = sub_b[:, :, 0], sub_b[:, :, 1], sub_b[:, :, 2]
        green_cur = (r_ch <= 45) & (g_ch >= 118) & (g_ch <= 190) & (b_ch >= 68) & (b_ch <= 140)
        ys, xs = np.nonzero((diff_sub >= 18) & green_cur)
        pts = [(int(x) + left_px, int(y) + top_px) for x, y in zip(xs.tolist(), ys.tolist())]
        box_px = _largest_pixel_component_rect(pts, min_len=6)
        if box_px is None:
            ys2, xs2 = np.nonzero(diff_sub >= 28)
            if 6 <= len(ys2) <= int(w * h * 0.15):
                pts2 = [(int(x) + left_px, int(y) + top_px) for x, y in zip(xs2.tolist(), ys2.tolist())]
                box_px = _largest_pixel_component_rect(pts2, min_len=6)
    except Exception:
        pix_a = im_ref.load()
        pix_b = im_cur.load()
        pts = []
        for y in range(top_px, h):
            for x in range(left_px, w):
                ra, ga, ba = pix_a[x, y]
                rb, gb, bb = pix_b[x, y]
                if max(abs(ra - rb), abs(ga - gb), abs(ba - bb)) >= 18 and abs(rb - 16) < 28 and abs(gb - 153) < 34 and abs(bb - 104) < 34:
                    pts.append((x, y))
        box_px = _largest_pixel_component_rect(pts, min_len=6)

    if box_px is None:
        return None
    px0, py0, px1, py1 = box_px
    return (px0 / scale_x, py0 / scale_y, px1 / scale_x, py1 / scale_y)


def _active_cell_rect(page, cell, ref_im=None, min_x_css=0.0, min_y_css=0.0):
    """名称框定位单元格后，优先使用帧差分 + C6 #364C74 横带读取并按 DPR 换算回 CSS 像素位置。"""
    _goto_cell(page, cell)
    sleep_check(0.20)
    use_box, im, scale_x, scale_y = _capture_main_canvas_rgb(page)
    im_crop = None
    try:
        if ref_im is not None:
            diff_rect = _diff_active_cell_rect(
                ref_im,
                im,
                scale_x,
                scale_y,
                min_x_css=min_x_css,
                min_y_css=min_y_css,
            )
            if diff_rect is not None:
                return use_box, diff_rect

        if str(cell or "").strip().upper() == "C6":
            c6_box = _find_c6_header_banner_bbox(im, min_width_ratio=0.10, min_height_px=4)
            if c6_box is not None:
                px0, py0, px1, py1, _, _ = c6_box
                return use_box, (px0 / scale_x, py0 / scale_y, px1 / scale_x, py1 / scale_y)

        scan_im = im
        max_scan_h = int(700 * scale_y)
        if im.height > max_scan_h:
            im_crop = im.crop((0, 0, im.width, max_scan_h))
            scan_im = im_crop
        xs, ys = _find_wps_green_pixels(scan_im, dr=24, dg=30, db=30)
        pts = list(zip(xs, ys))
    finally:
        for obj in (im_crop, im):
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
    if not pts:
        raise RuntimeError(f"没有识别到 {cell} 的选中框")

    # 非 A1 目标过滤掉超出指定区域或第 1 行静态图标的像素（若过滤后仍有有效框）
    if str(cell or "").strip().upper() != "A1":
        lim_x = max(0.0, float(min_x_css) * scale_x)
        lim_y = max(14.0 * scale_y if float(min_y_css) > 0 else 0.0, float(min_y_css) * scale_y)
        filtered = [(x, y) for x, y in pts if x >= lim_x and y >= lim_y]
        if len(filtered) >= 6:
            pts = filtered

    if str(cell or "").strip().upper() == "C6":
        comp_pts = pts
        rect = (
            min(x for x, _ in comp_pts) / scale_x,
            min(y for _, y in comp_pts) / scale_y,
            max(x for x, _ in comp_pts) / scale_x,
            max(y for _, y in comp_pts) / scale_y,
        )
    else:
        comp_box = _largest_pixel_component_rect(pts, min_len=1)
        if comp_box is None:
            raise RuntimeError(f"没有识别到 {cell} 的选中框")
        rect = (
            comp_box[0] / scale_x,
            comp_box[1] / scale_y,
            comp_box[2] / scale_x,
            comp_box[3] / scale_y,
        )
    return use_box, rect


def _verify_selection_by_tsv(page, last_row, min_cols=18):
    """通过 Ctrl+C 采样剪贴板 TSV 核验选区是否真包含多行多列数据（绕开名称框仅显示起点 C6）。"""
    global _last_captured_summary_tsv
    try:
        clear_clipboard_windows()
        page.keyboard.press("Control+C")
        dl = time.monotonic() + 0.70
        txt = ""
        while time.monotonic() < dl:
            time.sleep(0.10)
            txt = get_clipboard_text_windows()
            if txt and len(txt.strip()) >= 12:
                break
        if not txt:
            return False, 0, 0
        lines = [ln for ln in txt.rstrip("\r\n").splitlines() if ln.strip()]
        row_cnt = len(lines)
        col_cnt = max((ln.count("\t") + 1 for ln in lines), default=0)
        exp_rows = max(2, int(last_row) - 5)
        need_rows = max(2, min(exp_rows - 1, int(exp_rows * 0.45))) if exp_rows > 2 else 2
        ok = (row_cnt >= need_rows and col_cnt >= int(min_cols))
        if ok and len(txt.strip()) >= 20:
            _last_captured_summary_tsv = txt
        return ok, row_cnt, col_cnt
    except Exception:
        return False, 0, 0


def _select_wps_range(page, last_row, end_col="AK", use_mouse=True):
    """四层稳态选中 C6:AK{last_row}：名称框直选 -> 20%缩放差分框选 -> Shift+终点+TSV核验 -> 键盘C6下扩兜底。"""
    expected_cols = max(1, _col_letter_to_idx(end_col) - _col_letter_to_idx("C") + 1)

    def _verify_range(selection):
        m = re.search(r"(\d+)\s*R\s*[x×]\s*(\d+)\s*C", selection or "")
        rows = int(m.group(1)) if m else 0
        cols = int(m.group(2)) if m else 0
        if rows != last_row - 5 or not (max(1, expected_cols - 1) <= cols <= expected_cols + 1):
            raise RuntimeError(f"框选回读为 {selection or '未知'}")

    # 通道 0：尝试名称框直输区间（若当前版本 WPS 支持则直接零误差选中）
    try:
        _goto_cell(page, f"C6:{end_col}{last_row}")
        sleep_check(0.40)
        nb_c0 = _read_wps_name_box(page)
        if nb_c0:
            try:
                _verify_range(nb_c0)
                log(f"  已通过名称框直接选中 C6:{end_col}{last_row}（{nb_c0}）")
                return True
            except Exception:
                pass
            if f":{end_col}{last_row}".upper() in nb_c0.upper():
                ok_tsv0, rc0, cc0 = _verify_selection_by_tsv(page, last_row)
                if ok_tsv0:
                    log(f"  已通过名称框直选并核验选区 C6:{end_col}{last_row}（{rc0}行×{cc0}列）")
                    return True
    except Exception as e_c0:
        if "手动停止" in str(e_c0):
            raise

    # 通道 1：20% 缩放 + A1 差分定位起点/终点 + 平滑缓慢拖拽框选
    if use_mouse:
        im_ref = None
        try:
            _set_wps_zoom(page, 20)
            try:
                _goto_cell(page, "A1")
                sleep_check(0.25)
                _, im_ref, _, _ = _capture_main_canvas_rgb(page)
            except Exception as e_ref:
                if _is_stop_exception(e_ref):
                    raise
                im_ref = None
            box, sr = _active_cell_rect(page, "C6", ref_im=im_ref)
            _, er = _active_cell_rect(
                page,
                f"{end_col}{last_row}",
                ref_im=im_ref,
                min_x_css=max(0.0, sr[0] - 8.0),
                min_y_css=max(0.0, sr[3] - 2.0),
            )
            if er[1] < sr[1] + 2.0:
                raise RuntimeError(f"终点纵坐标异常（{er[1]:.1f} < {sr[1]:.1f}），防止误拖向第1行")
            sx = box["x"] + sr[0] + max(4.0, min(14.0, (sr[2] - sr[0]) * 0.08))
            sy = box["y"] + sr[1] + max(2.0, (sr[3] - sr[1]) / 2.0)
            ex = box["x"] + (er[0] + er[2]) / 2.0
            ey = box["y"] + (er[1] + er[3]) / 2.0
            _mouse_move_smooth(page, sx, sy, steps=8)
            sleep_check(0.18)
            page.mouse.down()
            _mouse_move_smooth(page, ex, ey, steps=40)
            sleep_check(0.35)
            selection = _read_wps_name_box(page)
            page.mouse.up()
            sleep_check(0.25)
            try:
                _verify_range(selection)
                log(f"  已用鼠标框选 C6:{end_col}{last_row}（{selection}）")
                return True
            except Exception:
                ok_tsv1, rc1, cc1 = _verify_selection_by_tsv(page, last_row)
                if ok_tsv1:
                    log(f"  已用鼠标框选 C6:{end_col}{last_row}（TSV 实测 {rc1}行×{cc1}列）")
                    return True
                raise RuntimeError(f"框选回读为 {selection or '未知'}")
        except Exception as e:
            if "手动停止" in str(e):
                raise
            try:
                page.mouse.up()
            except Exception:
                pass
            log(f"  鼠标框选未命中，改为 Shift+终点精确框选：{human_error(e)}")
        finally:
            if im_ref is not None:
                try:
                    im_ref.close()
                except Exception:
                    pass

    # 通道 2：20% 缩放 + 差分终点 + Shift+点击 + TSV 真实选区核验
    im_ref2 = None
    try:
        _set_wps_zoom(page, 20)
        try:
            _goto_cell(page, "A1")
            sleep_check(0.25)
            _, im_ref2, _, _ = _capture_main_canvas_rgb(page)
        except Exception as e_ref2:
            if _is_stop_exception(e_ref2):
                raise
            im_ref2 = None
        box, sr2 = _active_cell_rect(page, "C6", ref_im=im_ref2)
        _, er2 = _active_cell_rect(
            page,
            f"{end_col}{last_row}",
            ref_im=im_ref2,
            min_x_css=max(0.0, sr2[0] - 8.0),
            min_y_css=max(0.0, sr2[3] - 2.0),
        )
        if er2[1] < sr2[1] + 2.0:
            raise RuntimeError(f"Shift终点纵坐标异常（{er2[1]:.1f} < {sr2[1]:.1f}），防止误选第1行")
        ex = box["x"] + (er2[0] + er2[2]) / 2.0
        ey = box["y"] + (er2[1] + er2[3]) / 2.0
        _goto_cell(page, "C6")
        sleep_check(0.35)
        page.keyboard.down("Shift")
        try:
            _mouse_move_smooth(page, ex, ey, steps=12)
            sleep_check(0.15)
            page.mouse.click(ex, ey)
            sleep_check(0.25)
        finally:
            page.keyboard.up("Shift")
        sleep_check(0.40)
        selection2 = _read_wps_name_box(page)
        try:
            _verify_range(selection2)
            log(f"  已精准框选 C6:{end_col}{last_row}（{selection2}）")
            return True
        except Exception:
            ok_tsv2, rc2, cc2 = _verify_selection_by_tsv(page, last_row)
            if ok_tsv2:
                log(f"  已通过 Shift+终点核验选中 C6:{end_col}{last_row}（TSV 实测 {rc2}行×{cc2}列）")
                return True
    except Exception as e2:
        if "手动停止" in str(e2):
            raise
        log(f"  Shift+点击回读异常，尝试键盘 C6 合并行下扩兜底：{human_error(e2)}")
    finally:
        if im_ref2 is not None:
            try:
                im_ref2.close()
            except Exception:
                pass

    # 通道 3：纯键盘 C6（已横跨 C6:AK6）+ Shift+ArrowDown 逐行稳态下扩（完全不依赖鼠标）
    try:
        _goto_cell(page, "C6")
        sleep_check(0.35)
        steps_down = max(1, min(260, int(last_row) - 6))
        for idx_dn in range(steps_down):
            if idx_dn % 10 == 0:
                check_stop()
            page.keyboard.press("Shift+ArrowDown")
            if idx_dn % 8 == 7:
                time.sleep(0.04)
        sleep_check(0.35)
        ok_tsv3, rc3, cc3 = _verify_selection_by_tsv(page, last_row)
        if ok_tsv3:
            log(f"  已通过键盘下扩稳选 C6:{end_col}{last_row}（TSV 实测 {rc3}行×{cc3}列）")
            return True
        log(f"  键盘下扩后采样 {rc3}行×{cc3}列，继续依赖白边与标题栏定位…")
        return False
    except Exception as e3:
        if "手动停止" in str(e3):
            raise
        log(f"  键盘下扩回读异常，继续依赖标题栏定位：{human_error(e3)}")
        return False


def _col_letters_to_index(col_str):
    """将 Excel/WPS 列标（如 C、AK）转为 1-based 列号。"""
    n = 0
    for ch in str(col_str or "").strip().upper():
        if not ("A" <= ch <= "Z"):
            return 0
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n


def _read_wps_name_box(page):
    """极速回读 WPS 左上角名称框（input.edit-box）当前选中区域/单元格，失败返回空串。"""
    try:
        loc = page.locator("input.edit-box")
        if hasattr(loc, "count") and loc.count() <= 0:
            return ""
        val = loc.first.input_value() if hasattr(loc.first, "input_value") else page.eval_on_selector("input.edit-box", "e=>e.value")
        return str(val or "").strip()
    except Exception:
        try:
            return str(page.eval_on_selector("input.edit-box", "e=>e.value") or "").strip()
        except Exception:
            return ""


def _is_wps_selection_valid_for_capture(name_box_val, last_row=0):
    """校验当前名称框选区是否仍停留在汇总数据区（C6 及之后），严厉拦截 A1 或 1~5 行误点。"""
    s = str(name_box_val or "").strip().upper()
    if not s:
        return False
    m_rc = re.search(r"(\d+)\s*R\s*[X×]\s*(\d+)\s*C", s)
    if m_rc:
        rows_n = int(m_rc.group(1))
        cols_n = int(m_rc.group(2))
        return rows_n >= 2 and cols_n >= 10
    head_part = s.split(":", 1)[0].strip()
    m_cell = re.fullmatch(r"([A-Z]{1,3})(\d+)", head_part)
    if not m_cell:
        return False
    col_idx = _col_letters_to_index(m_cell.group(1))
    row_idx = int(m_cell.group(2))
    # 目标汇总表左上角为 C6，禁止 A/B 列或第 1~5 行（尤其 A1 漂移）
    return col_idx >= 3 and row_idx >= 6


def _longest_true_run_span(vec):
    """返回一维布尔序列中连续 True 段列表 [(start_idx, end_idx), ...]。"""
    spans = []
    n = len(vec)
    i = 0
    while i < n:
        if vec[i]:
            j = i
            while j + 1 < n and vec[j + 1]:
                j += 1
            spans.append((i, j))
            i = j + 1
        else:
            i += 1
    return spans


def _find_wps_selection_border_bbox(im, min_h_run=40, min_v_run=24):
    """通过连续水平/垂直绿线边界检测 WPS 选区外框，彻底忽略第 1 行绿色图标与零星绿格干扰。"""
    w, h = im.size
    if w < 60 or h < 60:
        return None
    top_guard = min(max(40, int(h * 0.18)), max(0, h // 3))
    left_guard = min(36, int(w * 0.03))
    try:
        import numpy as np  # type: ignore[import-not-found]
        arr = np.asarray(im)
        r_ch, g_ch, b_ch = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
        mask = (
            (r_ch >= 0) & (r_ch <= 43)
            & (g_ch >= 120) & (g_ch <= 186)
            & (b_ch >= 71) & (b_ch <= 137)
        )
        h_lines = []
        row_sums = mask[top_guard:, left_guard:].sum(axis=1)
        for ry in np.flatnonzero(row_sums >= min_h_run):
            y = int(ry) + top_guard
            row_m = mask[y, left_guard:]
            for sx, ex in _longest_true_run_span(row_m.tolist()):
                if (ex - sx + 1) >= min_h_run:
                    h_lines.append((y, sx + left_guard, ex + left_guard))
        v_lines = []
        col_sums = mask[top_guard:, left_guard:].sum(axis=0)
        for cx in np.flatnonzero(col_sums >= min_v_run):
            x = int(cx) + left_guard
            col_m = mask[top_guard:, x]
            for sy, ey in _longest_true_run_span(col_m.tolist()):
                if (ey - sy + 1) >= min_v_run:
                    v_lines.append((x, sy + top_guard, ey + top_guard))
    except Exception:
        pix = im.load()
        h_lines = []
        for y in range(top_guard, h):
            run_s = -1
            for x in range(left_guard, w):
                r, g, b = pix[x, y]
                is_g = abs(r - 16) < 28 and abs(g - 153) < 34 and abs(b - 104) < 34
                if is_g and run_s < 0:
                    run_s = x
                elif not is_g and run_s >= 0:
                    if (x - run_s) >= min_h_run:
                        h_lines.append((y, run_s, x - 1))
                    run_s = -1
            if run_s >= 0 and (w - run_s) >= min_h_run:
                h_lines.append((y, run_s, w - 1))
        v_lines = []
        for x in range(left_guard, w):
            run_s = -1
            for y in range(top_guard, h):
                r, g, b = pix[x, y]
                is_g = abs(r - 16) < 28 and abs(g - 153) < 34 and abs(b - 104) < 34
                if is_g and run_s < 0:
                    run_s = y
                elif not is_g and run_s >= 0:
                    if (y - run_s) >= min_v_run:
                        v_lines.append((x, run_s, y - 1))
                    run_s = -1
            if run_s >= 0 and (h - run_s) >= min_v_run:
                v_lines.append((x, run_s, h - 1))

    if not h_lines and not v_lines:
        return None
    bx0 = min(v[0] for v in v_lines) if v_lines else min(hl[1] for hl in h_lines)
    by0 = min(hl[0] for hl in h_lines) if h_lines else min(v[1] for v in v_lines)
    max_vx = max((v[0] for v in v_lines), default=-1)
    max_hy = max((hl[0] for hl in h_lines), default=-1)
    bx1 = max_vx if (v_lines and max_vx > bx0 + min_h_run) else (max((hl[2] for hl in h_lines), default=w - 12))
    by1 = max_hy if (h_lines and max_hy > by0 + min_v_run) else (max((v[2] for v in v_lines), default=h - 12))
    if (bx1 - bx0) < 40 or (by1 - by0) < 16:
        return None
    return bx0, by0, bx1, by1


def _find_c6_header_banner_point(page):
    """在当前 WPS 画布中识别第 6 行 #364C74 深蓝标题栏（C6 区域），返回安全右键 CSS 坐标详情。"""
    try:
        box, im, scale_x, scale_y = _capture_main_canvas_rgb(page)
    except Exception:
        return {"found": False, "box": None}
    try:
        binfo = _find_c6_header_banner_bbox(im, min_width_ratio=0.18, min_height_px=max(8, int(10 * scale_y)))
        if binfo is None:
            return {"found": False, "box": box}
        px0, py0, px1, py1, px_cnt, cov = binfo
        cx0, cy0 = px0 / scale_x, py0 / scale_y
        cx1, cy1 = px1 / scale_x, py1 / scale_y
        bw = cx1 - cx0
        bh = cy1 - cy0
        if bw < 40 or bh < 6:
            return {"found": False, "box": box}
        # 优先取深蓝横条偏左中段内部（严格远离 A/B 列与右侧屏幕边缘）
        rx = box["x"] + min(cx1 - max(28.0, bw * 0.12), cx0 + max(48.0, bw * 0.28))
        ry = box["y"] + (cy0 + cy1) * 0.50
        return {
            "found": True,
            "rx": round(float(rx), 1),
            "ry": round(float(ry), 1),
            "bbox_css": (round(cx0, 1), round(cy0, 1), round(cx1, 1), round(cy1, 1)),
            "bw": round(bw, 1),
            "bh": round(bh, 1),
            "navy_px": int(px_cnt),
            "coverage": round(float(cov), 3),
            "scale_x": round(float(scale_x), 3),
            "scale_y": round(float(scale_y), 3),
            "box": box,
        }
    finally:
        try:
            im.close()
        except Exception:
            pass


def _find_selection_right_click_point(page):
    """优先锁定第 6 行 #364C74 C6 标题栏，其次连续绿框直线 + HiDPI 换算，彻底防误点第 1 行图标或 A1。"""
    c6_hit = _find_c6_header_banner_point(page)
    if c6_hit.get("found"):
        return float(c6_hit["rx"]), float(c6_hit["ry"]), True

    canvases = []
    for c in page.query_selector_all("canvas"):
        try:
            r = c.bounding_box()
            if r and r["width"] > 100 and r["height"] > 100:
                canvases.append((c, r))
        except Exception:
            pass
    if not canvases:
        raise RuntimeError("没有找到表格画布")
    target, box = max(canvases, key=lambda it: it[1]["width"] * it[1]["height"])
    try:
        shot_buf = io.BytesIO(target.screenshot())
        im = None
        scale_x = 1.0
        scale_y = 1.0
        bbox_px = None
        try:
            with Image.open(shot_buf) as raw_im:
                im = raw_im.convert("RGB")
            scale_x = max(1.0, float(im.width) / max(1.0, float(box["width"])))
            scale_y = max(1.0, float(im.height) / max(1.0, float(box["height"])))
            min_h_px = max(40, int(44 * scale_x))
            min_v_px = max(20, int(24 * scale_y))
            bbox_px = _find_wps_selection_border_bbox(im, min_h_run=min_h_px, min_v_run=min_v_px)
        finally:
            for obj in (im, shot_buf):
                if obj is not None:
                    try:
                        obj.close()
                    except Exception:
                        pass
        if bbox_px is not None:
            px0, py0, px1, py1 = bbox_px
            cx0, cy0 = px0 / scale_x, py0 / scale_y
            cx1, cy1 = px1 / scale_x, py1 / scale_y
            bw = cx1 - cx0
            bh = cy1 - cy0
            if bw >= 40 and bh >= 16:
                inset_x = min(160.0, max(36.0, bw * 0.30))
                inset_x = min(inset_x, max(20.0, bw - 20.0))
                rx = box["x"] + min(cx1 - 16.0, cx0 + inset_x)
                if bh >= 52:
                    inset_y = min(110.0, max(36.0, bh * 0.35), max(22.0, bh - 16.0))
                else:
                    inset_y = max(14.0, bh * 0.5)
                ry = box["y"] + min(cy1 - 10.0, cy0 + inset_y)
                return rx, ry, True
    except Exception:
        pass
    # 200% 缩放无框检出兜底：落到数据表中下安全区（绝不落到 A1 或第 1~5 行标题区）
    rx = box["x"] + min(max(box["width"] * 0.36, 180.0), max(120.0, box["width"] - 80.0))
    ry = box["y"] + min(max(box["height"] * 0.58, 220.0), max(160.0, box["height"] - 40.0))
    return rx, ry, False


def _is_copy_as_image_menu_visible(page):
    """快速探测当前画面是否已弹出「复制为图片」菜单（区分复制为表格图，并支持文本节点精确定位）。"""
    try:
        if hasattr(page, "evaluate"):
            hit = page.evaluate("""() => {
                const els = document.querySelectorAll('.ks-contextmenu *, [class*="contextmenu"] *, [role="menu"] *, [role="menuitem"]');
                for (const el of els) {
                    const st = window.getComputedStyle(el);
                    if (!st || st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') continue;
                    const txt = (el.textContent || '').trim();
                    if (txt === '复制为图片') {
                        const r = el.getBoundingClientRect();
                        if (r && r.width >= 24 && r.height >= 12 && r.left >= 35 && r.top >= 50) {
                            return { x: r.left + r.width / 2, y: r.top + r.height / 2, w: r.width, h: r.height };
                        }
                    }
                    for (const ch of (el.childNodes || [])) {
                        if (ch.nodeType === 3 && (ch.textContent || '').trim() === '复制为图片') {
                            try {
                                const rg = document.createRange();
                                rg.selectNodeContents(ch);
                                const r2 = rg.getBoundingClientRect();
                                if (r2 && r2.width >= 24 && r2.height >= 12 && r2.left >= 35 && r2.top >= 50) {
                                    return { x: r2.left + r2.width / 2, y: r2.top + r2.height / 2, w: r2.width, h: r2.height };
                                }
                            } catch (_e) {}
                        }
                    }
                }
                return null;
            }""")
            if isinstance(hit, dict) and hit.get("x") and hit.get("y"):
                return hit
    except Exception:
        pass
    for sel in (
        ".ks-contextmenu :text-is('复制为图片')",
        "[class*='contextmenu'] :text-is('复制为图片')",
        "[role='menu'] :text-is('复制为图片')",
    ):
        try:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                bb = loc.first.bounding_box() if hasattr(loc.first, "bounding_box") else None
                if isinstance(bb, dict):
                    if bb.get("width", 0) >= 24 and bb.get("height", 0) >= 12 and (bb.get("x", 0) >= 35 or bb.get("y", 0) >= 50):
                        return {"x": bb["x"] + bb["width"] / 2, "y": bb["y"] + bb["height"] / 2, "w": bb["width"], "h": bb["height"]}
                else:
                    return True
        except Exception:
            pass
    return None


def _click_copy_as_image_menu(page):
    """精确点击可视右键菜单中的「复制为图片」（严拒复制为表格图及 (0,0)/A1 幽灵坐标）。"""
    vis = _is_copy_as_image_menu_visible(page)
    if isinstance(vis, dict) and vis.get("x") and vis.get("y"):
        _mouse_move_smooth(page, float(vis["x"]), float(vis["y"]), steps=8)
        sleep_check(0.18)
        page.mouse.click(float(vis["x"]), float(vis["y"]))
        return True
    selectors = [
        ".ks-contextmenu :text-is('复制为图片')",
        "[class*='contextmenu'] :text-is('复制为图片')",
        "[role='menu'] :text-is('复制为图片')",
        "text='复制为图片'",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                bb = loc.first.bounding_box() if hasattr(loc.first, "bounding_box") else None
                if isinstance(bb, dict) and (bb.get("width", 0) < 20 or bb.get("height", 0) < 10 or (bb.get("x", 0) < 35 and bb.get("y", 0) < 55)):
                    continue
                loc.first.click(timeout=2500)
                return True
        except Exception:
            pass
    if hasattr(page, "get_by_text"):
        try:
            loc_ex = page.get_by_text("复制为图片", exact=True)
            if loc_ex.count() > 0 and loc_ex.first.is_visible():
                bb = loc_ex.first.bounding_box() if hasattr(loc_ex.first, "bounding_box") else None
                if not isinstance(bb, dict) or (bb.get("width", 0) >= 20 and bb.get("height", 0) >= 10 and (bb.get("x", 0) >= 35 or bb.get("y", 0) >= 55)):
                    loc_ex.first.click(timeout=2500)
                    return True
        except Exception:
            pass
    raise RuntimeError("未找到可见且位置有效的「复制为图片」菜单项")


def _get_page_viewport_wh(page):
    """安全获取当前浏览器视口宽高 (vw, vh)，失败回退 (1280, 720)。"""
    vw, vh = 1280, 720
    try:
        vp = getattr(page, "viewport_size", None)
        if isinstance(vp, dict) and vp.get("width") and vp.get("height"):
            return int(vp["width"]), int(vp["height"])
        mb = _main_canvas_box(page)
        if isinstance(mb, dict) and mb.get("width"):
            return int(mb.get("x", 0) + mb["width"]), int(mb.get("y", 0) + mb["height"])
    except Exception:
        pass
    return vw, vh


def _ai_vision_locate_wps_capture_target(page, stage="selection_interior", cfg=None, cand_xy=None):
    """按需调用视觉 AI 在 200% 缩放表格中定位或用红圈准星反复确认 C6（#364C74）标题栏 /「复制为图片」菜单。"""
    norm_c = normalize_config(cfg if isinstance(cfg, dict) else load_config())
    ai_cfg = norm_c.get("ai_assistant") or {}
    if not ai_cfg.get("enabled", True) or str(ai_cfg.get("provider") or "gemini") == "local_only":
        return {"ok": False, "reason": "ai_disabled"}
    try:
        raw_png = page.screenshot()
    except Exception as exc_s:
        return {"ok": False, "reason": f"screenshot_fail:{exc_s}"}
    if not raw_png:
        return {"ok": False, "reason": "empty_screenshot"}

    vw, vh = _get_page_viewport_wh(page)
    cand_xp = 0.0
    cand_yp = 0.0
    if cand_xy is not None and len(cand_xy) >= 2:
        try:
            cand_xp = max(0.0, min(1.0, float(cand_xy[0]) / float(max(1, vw))))
            cand_yp = max(0.0, min(1.0, float(cand_xy[1]) / float(max(1, vh))))
        except Exception:
            cand_xp = cand_yp = 0.0

    im_buf = io.BytesIO(raw_png)
    out_buf = io.BytesIO()
    b64_img = ""
    rgb_im = None
    try:
        with Image.open(im_buf) as raw_im:
            rgb_im = raw_im.convert("RGB")
            iw, ih = rgb_im.size
            if iw > 960:
                nh = max(120, int(ih * (960.0 / float(iw))))
                resized_im = rgb_im.resize((960, nh), Image.Resampling.LANCZOS)
                rgb_im.close()
                rgb_im = resized_im
                iw, ih = rgb_im.size
            if cand_xy is not None and len(cand_xy) >= 2:
                try:
                    from PIL import ImageDraw
                    drw = ImageDraw.Draw(rgb_im)
                    px = max(6, min(iw - 6, int(round(iw * cand_xp))))
                    py = max(6, min(ih - 6, int(round(ih * cand_yp))))
                    rr = max(12, min(24, int(min(iw, ih) * 0.035)))
                    drw.ellipse((px - rr, py - rr, px + rr, py + rr), outline=(255, 40, 40), width=3)
                    drw.line((px - rr - 6, py, px + rr + 6, py), fill=(255, 40, 40), width=2)
                    drw.line((px, py - rr - 6, px, py + rr + 6), fill=(255, 40, 40), width=2)
                except Exception:
                    pass
            rgb_im.save(out_buf, format="JPEG", quality=82, optimize=True)
        b64_img = base64.b64encode(out_buf.getvalue()).decode("ascii")
    except Exception:
        b64_img = ""
    finally:
        for obj in (rgb_im, out_buf, im_buf):
            try:
                obj.close()
            except Exception:
                pass
    if not b64_img:
        return {"ok": False, "reason": "encode_fail"}

    is_confirm_stage = stage in ("confirm_c6_banner", "confirm_copy_menu")
    if stage == "copy_as_image_menu":
        sys_p = (
            "你是浏览器 WPS 在线表格视觉助手。当前屏幕已弹出黑色或白色右键菜单。\n"
            "请找出菜单中写着「复制为图片」（注意不要点到旁边的「复制为表格图」或其他项）的精确中心位置比例 x_pct、y_pct（0.0~1.0）。\n"
            "只输出一段严格 JSON：\n"
            '{"found":false,"x_pct":0.0,"y_pct":0.0,"reason":""}'
        )
        u_txt = "请识别右键菜单中「复制为图片」选项的精确点击坐标。"
    elif stage == "confirm_copy_menu":
        sys_p = (
            "你是浏览器 WPS 在线表格视觉复核专家。图中已用红色圆圈与十字标出候选左键点击坐标。\n"
            "请反复核对：该红圈十字是否精准落在右键菜单中的「复制为图片」上（绝不能偏到「复制为表格图」、桌面其他处或 A1）？\n"
            "若确认无误返回 confirmed:true；若偏差请给出正确的「复制为图片」中心比例 x_pct、y_pct（0.0~1.0）。\n"
            "只输出一段严格 JSON：\n"
            '{"confirmed":false,"found":false,"x_pct":0.0,"y_pct":0.0,"reason":""}'
        )
        u_txt = f"请复核红圈({cand_xp:.3f},{cand_yp:.3f})是否正中「复制为图片」菜单项。"
    elif stage == "confirm_c6_banner":
        sys_p = (
            "你是浏览器 WPS 在线表格视觉复核专家。当前页面已放大 200%，图中已用红色圆圈与十字标出候选右键点击位置。\n"
            "目标区域是第 6 行深蓝色（色调 #364C74）合并标题横条（C6 区域，内含白色文字「个人数据明细 / 日报未更 / 暗号数据表」）。\n"
            "请反复确认：红圈十字是否正落在第 6 行 #364C74 深蓝标题栏内部，且完全没有偏到左上角 A1/C1、列标或第 1~5 行？\n"
            "只输出一段严格 JSON：\n"
            '{"confirmed":false,"found":false,"x_pct":0.0,"y_pct":0.0,"reason":""}'
        )
        u_txt = f"请反复确认红圈({cand_xp:.3f},{cand_yp:.3f})是否准确落在第6行 #364C74 深蓝标题栏（C6 区域）内部。"
    elif stage == "c6_banner_locate":
        sys_p = (
            "你是浏览器 WPS 在线表格视觉助手。当前表格已放大至 200%，请专门寻找第 6 行深蓝色背景（十六进制颜色 #364C74）的合并标题横条（C6 区域，写着「个人数据明细」等白色大字）。\n"
            "请给出该 #364C74 深蓝标题横条内部一个安全右键点坐标比例 x_pct、y_pct（0.0~1.0）。\n"
            "严禁返回左上角 A1/B1/C1、列标行、行号栏或第 1~5 行浅色筛选区！\n"
            "只输出一段严格 JSON：\n"
            '{"found":false,"x_pct":0.0,"y_pct":0.0,"reason":""}'
        )
        u_txt = "请按颜色 #364C74 寻找第 6 行 C6 标题栏（个人数据明细）内部的安全右键坐标。"
    else:
        sys_p = (
            "你是浏览器 WPS 在线表格视觉助手。当前表格已放大至 200% 并选中了第 6 行及以下（C6 开始）的数据区域（C6 标题栏为深蓝色 #364C74）。\n"
            "请给出第 6 行 #364C74 深蓝标题栏或绿框数据区内部一个安全的中心点比例 x_pct、y_pct（0.0~1.0），用于弹出右键菜单：\n"
            "严禁返回左上角 A1/B1/C1、列标 A/B/C、行号或顶部 1~5 行筛选区位置！\n"
            "只输出一段严格 JSON：\n"
            '{"found":false,"x_pct":0.0,"y_pct":0.0,"reason":""}'
        )
        u_txt = "请识别 200% 缩放 C6（#364C74）标题栏或选中数据区内部适合右键点击的安全坐标。"

    parts = [
        {"text": u_txt},
        {"inlineData": {"mimeType": "image/jpeg", "data": b64_img}},
    ]
    try:
        g_res = _call_gemini_generate(
            norm_c,
            sys_p,
            [{"role": "user", "parts": parts}],
            temperature=0.05,
            max_tokens=220,
            timeout_sec=7,
            max_models=1,
            total_budget_sec=9,
        )
    except Exception as exc_ai:
        return {"ok": False, "reason": f"ai_err:{exc_ai}"}
    if not isinstance(g_res, dict) or not g_res.get("ok"):
        return {"ok": False, "reason": "ai_not_ok"}
    obj = _parse_first_json_dict(g_res.get("reply"))
    if not isinstance(obj, dict):
        return {"ok": False, "reason": "ai_bad_json"}

    confirmed_flag = bool(obj.get("confirmed", False))
    found_flag = bool(obj.get("found", False))
    if is_confirm_stage:
        if not confirmed_flag and not found_flag:
            return {"ok": False, "confirmed": False, "reason": str(obj.get("reason") or "ai_unconfirmed")}
    else:
        if not found_flag:
            return {"ok": False, "reason": "ai_not_found"}

    try:
        raw_xp = obj.get("x_pct")
        raw_yp = obj.get("y_pct")
        if is_confirm_stage and confirmed_flag and (raw_xp in (None, "", 0, 0.0) or raw_yp in (None, "", 0, 0.0)) and cand_xy is not None:
            xp, yp = cand_xp, cand_yp
        else:
            xp = float(raw_xp)
            yp = float(raw_yp)
    except Exception:
        return {"ok": False, "reason": "ai_bad_coords"}

    if stage in ("copy_as_image_menu", "confirm_copy_menu"):
        if not (0.04 <= xp <= 0.96 and 0.08 <= yp <= 0.96) or (xp < 0.08 and yp < 0.25):
            return {"ok": False, "reason": f"ai_menu_a1_guard({xp},{yp})"}
    elif stage in ("confirm_c6_banner", "c6_banner_locate"):
        if not (0.08 <= xp <= 0.92 and 0.16 <= yp <= 0.90) or (xp < 0.14 and yp < 0.25):
            return {"ok": False, "reason": f"ai_c6_a1_guard({xp},{yp})"}
    else:
        if not (0.08 <= xp <= 0.90 and 0.30 <= yp <= 0.88):
            return {"ok": False, "reason": f"ai_selection_a1_guard({xp},{yp})"}

    return {
        "ok": True,
        "confirmed": confirmed_flag if is_confirm_stage else True,
        "x": round(vw * xp, 1),
        "y": round(vh * yp, 1),
        "x_pct": round(xp, 4),
        "y_pct": round(yp, 4),
        "vw": vw,
        "vh": vh,
        "reason": str(obj.get("reason") or "ai_vision_hit"),
    }


def _confirm_wps_c6_right_click_target(page, cand_rx, cand_ry, banner_info=None, cfg=None):
    """每次右键 C6（#364C74）前进行「本地两帧稳定复核 + 云端红圈准星反复确认」，严防偏到 A1/C1。"""
    rx = float(cand_rx)
    ry = float(cand_ry)
    vw, vh = _get_page_viewport_wh(page)
    binfo = dict(banner_info or {})

    # 确认层 1：停顿等待后复查第二帧（若画布仍在微动则追帧至第三帧），确保 #364C74 横条静止且几何远离 A1/C1
    if binfo.get("found"):
        sleep_check(0.45)
        b2 = _find_c6_header_banner_point(page)
        if b2.get("found"):
            rx2, ry2 = float(b2["rx"]), float(b2["ry"])
            if abs(rx2 - rx) > 12.0 or abs(ry2 - ry) > 8.0:
                rx, ry = rx2, ry2
                binfo = b2
                sleep_check(0.35)
                b3 = _find_c6_header_banner_point(page)
                if b3.get("found"):
                    rx, ry = float(b3["rx"]), float(b3["ry"])
                    binfo = b3
            else:
                rx, ry = rx2, ry2
                binfo = b2
            log(
                f"  🤖 [AI 反复确认 1/2] 已锁定 C6 深蓝标题栏（#364C74，高 {binfo.get('bh', 0):.0f}px/"
                f"覆盖 {float(binfo.get('coverage', 0.0)) * 100:.0f}%），多帧坐标稳定 ({rx:.0f}, {ry:.0f})，避开 A1/C1"
            )

    # 几何硬护栏：绝不允许落入左上角 A1/C1/第1~5行风险区
    xp = rx / float(max(1, vw))
    yp = ry / float(max(1, vh))
    if xp < 0.08 or yp < 0.14 or (xp < 0.14 and yp < 0.25):
        raise RuntimeError(f"候选右键点 ({rx:.0f}, {ry:.0f}) 过于靠近左上角 A1/C1 危险区，已主动拦截")

    # 确认层 2：调用视觉 AI 用红圈十字二次/三次确认（未配密钥或离线时由层 1 双帧色标确认无缝放行）
    cfm_tag = "c6_double_local"
    for _pass in range(2):
        ai_c = _ai_vision_locate_wps_capture_target(
            page,
            stage="confirm_c6_banner",
            cfg=cfg,
            cand_xy=(rx, ry),
        )
        if ai_c.get("ok"):
            if ai_c.get("confirmed"):
                rx, ry = float(ai_c["x"]), float(ai_c["y"])
                cfm_tag = "c6_ai_confirmed"
                log(f"  🤖 [AI 反复确认 2/2] 云端视觉 AI 红圈复核通过，正中 C6（#364C74）标题栏 ({rx:.0f}, {ry:.0f})")
                break
            # 云端视觉指出了更准确的 C6 落点：纠偏后再进入下一轮确认
            rx, ry = float(ai_c["x"]), float(ai_c["y"])
            cfm_tag = "c6_ai_adjusted"
            log(f"  🤖 [AI 反复确认] 视觉纠偏微调落点至 ({rx:.0f}, {ry:.0f})（{ai_c.get('reason') or '标题栏中心'}），再次复核…")
        else:
            if _pass == 0:
                log("  🤖 [AI 反复确认 2/2] 本地视觉色标（#364C74）与几何防 A1 双检已确认（离线/未配云端密钥兜底）")
            break

    return rx, ry, cfm_tag


def _verify_clipboard_image_size(min_w=180, min_h=50):
    """确认剪贴板图片宽高达标，避免误复制单格 A1 或空白区域。"""
    sz = get_clipboard_image_size()
    if sz is not None:
        return sz[0] >= min_w and sz[1] >= min_h, sz
    tmp_check = app_dir() / "_sheetmate_check_tmp.png"
    try:
        if save_clipboard_image_if_exists(tmp_check) == "saved" and tmp_check.exists():
            with Image.open(tmp_check) as im:
                sz = im.size
            return sz[0] >= min_w and sz[1] >= min_h, sz
    except Exception:
        pass
    finally:
        try:
            if tmp_check.exists():
                tmp_check.unlink()
        except Exception:
            pass
    return False, None


def _trim_image_right_blank(im):
    """裁除图片右侧纯白多余列并保留 4px 自然边距，返回 (img_out, orig_w, new_w)。"""
    rgb = im.convert("RGB")
    mask = None
    try:
        w, h = rgb.size
        mask = rgb.point(lambda v: 255 if v < 246 else 0)
        bbox = mask.getbbox()
        if bbox is None:
            return im, w, w
        right_x = bbox[2] - 1
        if right_x < int(w * 0.25):
            return im, w, w
        if w - 1 - right_x > 12:
            new_w = min(w, right_x + 5)
            return rgb.crop((0, 0, new_w, h)), w, new_w
        return im, w, w
    finally:
        for _tmp_im in (mask, rgb):
            if _tmp_im is not None:
                try:
                    _tmp_im.close()
                except Exception:
                    pass


def _trim_clipboard_image_right_blank():
    """检查剪贴板图片右侧是否存在多余纯白列，如有则无损裁切并写回剪贴板。"""
    tmp_trim = app_dir() / "_sheetmate_trim_tmp.png"
    tmp_trim_out = app_dir() / "_sheetmate_trim_out_tmp.png"
    trimmed = None
    try:
        if save_clipboard_image_if_exists(tmp_trim) != "saved" or not tmp_trim.exists():
            return False
        with Image.open(tmp_trim) as im:
            trimmed, orig_w, new_w = _trim_image_right_blank(im)
            if new_w >= orig_w:
                if trimmed is not im and trimmed is not None:
                    trimmed.close()
                    trimmed = None
                return False
            trimmed.save(tmp_trim_out, "PNG", optimize=True)
            if trimmed is not im and trimmed is not None:
                trimmed.close()
                trimmed = None
        os.replace(str(tmp_trim_out), str(tmp_trim))
        if set_clipboard_image_from_file(tmp_trim):
            log(f"  已自动裁除右侧多余白边：宽度 {orig_w} -> {new_w}px")
            return True
    except Exception as e:
        log(f"  白边裁切跳过（保留原图）：{human_error(e)}")
    finally:
        if trimmed is not None:
            try:
                trimmed.close()
            except Exception:
                pass
        for p_del in (tmp_trim, tmp_trim_out, tmp_trim.with_name(tmp_trim.stem + "_send.png")):
            try:
                if p_del.exists():
                    p_del.unlink()
            except Exception:
                pass
    return False


def _try_capture_selection_tsv(page):
    """在复制图片前顺手抓取已框选区域的纯文本 TSV 供 AI 长期数据归档。"""
    global _last_captured_summary_tsv
    try:
        clear_clipboard_windows()
        page.keyboard.press("Control+C")
        dl = time.monotonic() + 0.55
        while time.monotonic() < dl:
            time.sleep(0.1)
            txt = get_clipboard_text_windows()
            if txt and len(txt.strip()) >= 20:
                _last_captured_summary_tsv = txt
                return txt
    except Exception:
        pass
    return ""


def capture_wps_region(page, cfg=None):
    """按 Q 列（失败则 D 列兜底）确定末行，框选 C6:AK 并高清复制为图片（期间保护键鼠提示）。"""
    with hands_off_guard("WPS 汇总表中框选区域并复制截图"):
        return _capture_wps_region_inner(page, cfg=cfg)


def _capture_wps_region_inner(page, cfg=None):
    """按 Q 列确定末行，选区并放大 200% 后稳态锁定 C6 深蓝标题栏（#364C74），AI 反复确认右键复制为图片并等 5 秒。"""
    global _last_captured_summary_tsv
    check_stop()
    _last_captured_summary_tsv = ""
    log("  准备按 Q 列末行框选表格…")
    last_row = 8
    for _attempt in range(4):
        check_stop()
        candidate = _detect_last_row(page, "Q8")
        if candidate and candidate > 180:
            sleep_check(0.5)
            recheck = _detect_last_row(page, "Q8")
            if recheck and recheck > 8:
                candidate = recheck
        if candidate and candidate > last_row:
            last_row = candidate
        if last_row > 8:
            break
        sleep_check(1.2)
    if last_row <= 8:
        d_cand = _detect_last_row(page, "D8")
        if d_cand and d_cand > last_row and d_cand <= 300:
            last_row = d_cand
    log(f"  Q 列最后有文字的是第 {last_row} 行，目标 C6:AK{last_row}")

    selected_ok = _select_wps_range(page, last_row, end_col="AK", use_mouse=True)
    if not str(_last_captured_summary_tsv or "").strip():
        _try_capture_selection_tsv(page)

    sleep_check(0.50)
    try:
        _set_wps_zoom(page, 200)
    except Exception as e_z1:
        if _is_stop_exception(e_z1):
            raise
        try:
            _set_wps_zoom(page, 150)
        except Exception as e_zoom:
            if _is_stop_exception(e_zoom):
                raise
            log(f"  高清缩放未生效，按当前比例继续：{human_error(e_zoom)}")
    sleep_check(0.75)

    # 200% 缩放后即时核验选区是否仍稳停在 C6:AK，防缩放过程焦点误滑回 A1
    nb_zoom = _read_wps_name_box(page)
    if nb_zoom and not _is_wps_selection_valid_for_capture(nb_zoom, last_row=last_row):
        log(f"  ⚠️ 缩放后检测到选区漂移至「{nb_zoom}」，立即重选 C6:AK{last_row}…")
        record_run_trace_event("wps_rclick_a1_guard", f"zoom_drift:{nb_zoom}")
        _select_wps_range(page, last_row, end_col="AK", use_mouse=False)
        try:
            _set_wps_zoom(page, 200)
        except Exception as e_z2:
            if _is_stop_exception(e_z2):
                raise
        sleep_check(0.75)

    clear_clipboard_windows()
    for copy_attempt in range(3):
        check_stop()
        if copy_attempt:
            log(f"  本次复制未拿到有效图片，自动重试第 {copy_attempt + 1} 次…")
            try:
                page.keyboard.press("Escape")
                sleep_check(0.40)
            except Exception as e_esc1:
                if _is_stop_exception(e_esc1):
                    raise
            _select_wps_range(page, last_row, end_col="AK", use_mouse=False)
            try:
                _set_wps_zoom(page, 200)
            except Exception as e_z3:
                if _is_stop_exception(e_z3):
                    raise
            sleep_check(0.75)

        used_method = "border_rclick"
        learned_xy = None
        menu_opened = False

        # 先检测当前画布是否可见第 6 行 C6 深蓝标题栏（#364C74）
        banner_info = _find_c6_header_banner_point(page)
        if banner_info.get("found"):
            log(
                f"  🔍 已识别 C6 深蓝标题栏（#364C74），候选落点 ({banner_info['rx']:.0f}, {banner_info['ry']:.0f})，"
                "正在由 AI 反复确认后慢速右键…"
            )
            rx, ry, _cfm = _confirm_wps_c6_right_click_target(
                page,
                banner_info["rx"],
                banner_info["ry"],
                banner_info=banner_info,
                cfg=cfg,
            )
            used_method = "c6_banner_rclick"
        else:
            # 无 #364C74 色带场景（如特殊主题或快捷直通）先尝试系统快捷键 Shift+F10 / ContextMenu
            if copy_attempt == 0:
                for sc_key, m_tag in (("Shift+F10", "shift_f10"), ("ContextMenu", "contextmenu_key")):
                    try:
                        page.keyboard.press(sc_key)
                    except Exception:
                        continue
                    for _w in range(4):
                        sleep_check(0.18)
                        if _is_copy_as_image_menu_visible(page):
                            menu_opened = True
                            used_method = m_tag
                            log(f"  已通过系统按键 {sc_key} 零误触打开右键菜单")
                            break
                    if menu_opened:
                        break
                if not menu_opened:
                    try:
                        page.keyboard.press("Escape")
                        sleep_check(0.15)
                    except Exception as e_esc2:
                        if _is_stop_exception(e_esc2):
                            raise

            if not menu_opened:
                rx, ry, hit_green = _find_selection_right_click_point(page)
                if hit_green:
                    used_method = "border_rclick"
                else:
                    ai_c6 = _ai_vision_locate_wps_capture_target(page, stage="c6_banner_locate", cfg=cfg)
                    if ai_c6.get("ok"):
                        rx, ry = float(ai_c6["x"]), float(ai_c6["y"])
                        used_method = "c6_ai_locate"
                    else:
                        pb_cur = load_ai_run_playbook()
                        anc = pb_cur.get("wps_capture_anchor") if isinstance(pb_cur, dict) else None
                        if isinstance(anc, dict) and anc.get("x_pct") and anc.get("y_pct"):
                            try:
                                vw_a, vh_a = _get_page_viewport_wh(page)
                                rx = float(vw_a) * float(anc["x_pct"])
                                ry = float(vh_a) * float(anc["y_pct"])
                                used_method = "learned_anchor"
                            except Exception:
                                pass
                        if not selected_ok and copy_attempt == 0 and used_method not in ("learned_anchor", "c6_ai_locate"):
                            _select_wps_range(page, last_row, end_col="AK", use_mouse=False)
                            try:
                                _set_wps_zoom(page, 200)
                            except Exception as e_z4:
                                if _is_stop_exception(e_z4):
                                    raise
                            sleep_check(0.75)
                            rx, ry, hit_green = _find_selection_right_click_point(page)
                            if hit_green:
                                used_method = "border_rclick"
                try:
                    rx, ry, _cfm2 = _confirm_wps_c6_right_click_target(page, rx, ry, banner_info=None, cfg=cfg)
                except Exception as e_cfm2:
                    if _is_stop_exception(e_cfm2):
                        raise
                    log(f"  ⚠️ 候选右键落点被防误触护栏拦截（{human_error(e_cfm2)}），重新选区定位…")
                    continue

        if not menu_opened:
            _mouse_move_smooth(page, rx, ry, steps=12)
            sleep_check(0.35)
            page.mouse.click(rx, ry, button="right")
            sleep_check(0.85)
            try:
                vw_l, vh_l = _get_page_viewport_wh(page)
                learned_xy = (rx / float(max(1, vw_l)), ry / float(max(1, vh_l)), int(vw_l), int(vh_l))
            except Exception:
                learned_xy = None

            # 防 A1 误点核心守卫：右键后立刻复查名称框，若意外点成 A1/C1/前5行则当场撤回重选并重定 C6 #364C74
            nb_after = _read_wps_name_box(page)
            if nb_after and not _is_wps_selection_valid_for_capture(nb_after, last_row=last_row):
                log(f"  ⚠️ 检测到右键误点到「{nb_after}」（偏离数据区），立即放弃并重选 + AI 协同定位…")
                record_run_trace_event("wps_rclick_a1_guard", f"rclick->{nb_after}")
                _record_wps_capture_learned_anchor(used_method, ok=False)
                try:
                    page.keyboard.press("Escape")
                    sleep_check(0.30)
                except Exception as e_esc3:
                    if _is_stop_exception(e_esc3):
                        raise
                _select_wps_range(page, last_row, end_col="AK", use_mouse=False)
                try:
                    _set_wps_zoom(page, 200)
                except Exception as e_z5:
                    if _is_stop_exception(e_z5):
                        raise
                sleep_check(0.75)

                b_retry = _find_c6_header_banner_point(page)
                if b_retry.get("found"):
                    rx, ry, _ = _confirm_wps_c6_right_click_target(
                        page,
                        b_retry["rx"],
                        b_retry["ry"],
                        banner_info=b_retry,
                        cfg=cfg,
                    )
                    used_method = "c6_banner_rclick"
                    vw_r, vh_r = _get_page_viewport_wh(page)
                    learned_xy = (rx / float(max(1, vw_r)), ry / float(max(1, vh_r)), vw_r, vh_r)
                else:
                    ai_sel = _ai_vision_locate_wps_capture_target(page, stage="selection_interior", cfg=cfg)
                    if ai_sel.get("ok"):
                        rx, ry = float(ai_sel["x"]), float(ai_sel["y"])
                        used_method = "ai_vision"
                        learned_xy = (ai_sel["x_pct"], ai_sel["y_pct"], ai_sel["vw"], ai_sel["vh"])
                    else:
                        log(f"  右键防 A1 已触发撤回（{ai_sel.get('reason') or '无视觉通道'}），进入下轮重选")
                        continue
                _mouse_move_smooth(page, rx, ry, steps=12)
                sleep_check(0.35)
                page.mouse.click(rx, ry, button="right")
                sleep_check(0.85)
                nb_ai = _read_wps_name_box(page)
                if nb_ai and not _is_wps_selection_valid_for_capture(nb_ai, last_row=last_row):
                    log(f"  ⚠️ 二次落点仍偏移至「{nb_ai}」，进入下轮重选")
                    continue

        # 顺位 3：轮询菜单出现 -> 二次停顿复核「复制为图片」位置（防偏移/防误点复制为表格图）再点击
        clicked_pic = False
        vis1 = None
        for _mw in range(6):
            vis1 = _is_copy_as_image_menu_visible(page)
            if vis1:
                break
            sleep_check(0.25)

        if isinstance(vis1, dict) and vis1.get("x") and vis1.get("y"):
            sleep_check(0.25)
            vis2 = _is_copy_as_image_menu_visible(page)
            v_use = vis2 if (isinstance(vis2, dict) and vis2.get("x") and vis2.get("y")) else vis1
            mx, my = float(v_use["x"]), float(v_use["y"])
            if used_method not in ("shift_f10", "contextmenu_key"):
                ai_chk_m = _ai_vision_locate_wps_capture_target(
                    page,
                    stage="confirm_copy_menu",
                    cfg=cfg,
                    cand_xy=(mx, my),
                )
                if ai_chk_m.get("ok") and not ai_chk_m.get("confirmed"):
                    mx, my = float(ai_chk_m["x"]), float(ai_chk_m["y"])
                    log(f"  🤖 [AI 反复确认·菜单] 视觉微调「复制为图片」坐标至 ({mx:.0f}, {my:.0f})")
                else:
                    log(f"  🤖 [AI 反复确认·菜单] 已双检确认「复制为图片」稳态位于 ({mx:.0f}, {my:.0f})，准备点击")
            _mouse_move_smooth(page, mx, my, steps=10)
            sleep_check(0.28)
            page.mouse.click(mx, my)
            clicked_pic = True
        else:
            try:
                clicked_pic = _click_copy_as_image_menu(page)
            except Exception as e_cpic:
                if _is_stop_exception(e_cpic):
                    raise
                clicked_pic = False

        if not clicked_pic:
            ai_menu = _ai_vision_locate_wps_capture_target(page, stage="copy_as_image_menu", cfg=cfg)
            if ai_menu.get("ok"):
                mx_a, my_a = float(ai_menu["x"]), float(ai_menu["y"])
                log(f"  🤖 AI 视觉已锁定右键菜单「复制为图片」({mx_a:.0f}, {my_a:.0f})，慢速移入点击…")
                _mouse_move_smooth(page, mx_a, my_a, steps=10)
                sleep_check(0.28)
                page.mouse.click(mx_a, my_a)
                clicked_pic = True
                used_method = "ai_vision"
            else:
                record_run_trace_event("wps_capture_fail", used_method, {"reason": "menu_not_found"})
                _record_wps_capture_learned_anchor(used_method, ok=False)
                continue

        log("  已点「复制为图片」，正在等待图片写入剪贴板…")
        for _ in range(44):
            check_stop()
            if clipboard_has_image():
                ok_size, sz = _verify_clipboard_image_size(min_w=280, min_h=100)
                if ok_size:
                    _trim_clipboard_image_right_blank()
                    record_run_trace_event("wps_capture_ok", used_method)
                    if learned_xy is not None:
                        _record_wps_capture_learned_anchor(used_method, *learned_xy, ok=True)
                    else:
                        _record_wps_capture_learned_anchor(used_method, ok=True)
                    log(f"  图片已复制到剪贴板（策略：{used_method}，尺寸：{sz}）")
                    post_copy_wait = 5.0
                    norm_cfg_wait = normalize_config(cfg if isinstance(cfg, dict) else {})
                    if "wps_post_copy_wait_sec" in norm_cfg_wait:
                        try:
                            post_copy_wait = max(0.0, min(15.0, float(norm_cfg_wait.get("wps_post_copy_wait_sec", 5))))
                        except Exception:
                            post_copy_wait = 5.0
                    if post_copy_wait > 0:
                        log(f"  ⏳ 已复制为图片，按要求稳定等待 {post_copy_wait:g} 秒后再去粘贴…")
                        sleep_check(post_copy_wait)
                    return
                log(f"  ⚠️ 检测到剪贴板图片过小（{sz}，疑似误点单格），立即清理并重选复制…")
                record_run_trace_event("wps_capture_fail", used_method, {"reason": f"small_img:{sz}"})
                _record_wps_capture_learned_anchor(used_method, ok=False)
                clear_clipboard_windows()
                break
            sleep_check(0.25)
    raise RuntimeError("复制为图片后仍未检测到有效图片，请确认选区及浏览器剪贴板权限后重试")


def _enum_top_windows(target_classes=None, target_titles=None):
    """用 Win32 EnumWindows 枚举可见顶层窗口，返回 (hwnd, class, title) 列表。

    相比 uiautomation 的 GetChildren（TreeWalker 逐个导航，会在某个窗口的
    UIA 提供者无响应时被卡住），EnumWindows 直接走窗口管理器 + GetClassName，
    毫秒级返回、不调用各窗口的 UIA 提供者，稳定不阻塞。
    target_classes / target_titles 为 None 表示不按该条件过滤。
    """
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    # 显式声明原型，避免 64 位窗口句柄被默认按 32 位 c_int 截断
    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    results = []

    def _cb(hwnd, lparam):
        if user32.IsWindowVisible(hwnd):
            cbuf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cbuf, 256)
            tbuf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, tbuf, 256)
            cls, title = cbuf.value, tbuf.value
            if ((target_classes is None or cls in target_classes)
                    and (target_titles is None or title in target_titles)):
                results.append((hwnd, cls, title))
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return results


_DT_AUX_TITLE_KEYWORDS = (
    "钉钉-文档", "钉钉文档", "多维表", "脑图", "白板", "闪记", "知识库",
    "钉盘", "钉邮", "邮箱", "日历", "日程", "视频会议", "语音通话",
    "会议", "直播", "图片查看", "预览", "工作台", "小程序", "审批", "考勤", "日志",
)
_active_dt_hwnd = 0


def _is_dingtalk_aux_window(cls, title):
    """精准识别「钉钉-文档」、独立预览/会议/搜索浮窗等非主聊天窗口，防止抢占聊天主窗激活。"""
    c = str(cls or "").strip()
    t = str(title or "").strip()
    if any(k in c for k in ("ToolSaveBits", "QuiMenu", "Shadow", "Popup", "ComplexModal", "RichInputInsertLinkBox")):
        return True
    if "钉钉-文档" in t or "钉钉文档" in t:
        return True
    if t.endswith(("- 钉钉", "- 钉钉文档", "- DingTalk")):
        return True
    if t and t not in ("钉钉", "DingTalk") and any(k in t for k in _DT_AUX_TITLE_KEYWORDS):
        return True
    return False


def _score_dingtalk_main_window(cls, title, width=800, height=600, is_iconic=False, in_ding_pids=True):
    """对顶层窗口进行打分：仅真正的钉钉 IM 聊天主窗得高分，附属文档窗口和微小隐藏窗返回 0。"""
    c = str(cls or "").strip()
    cl = c.lower()
    t = str(title or "").strip()
    if _is_dingtalk_aux_window(c, t):
        return 0
    if not is_iconic and (int(width) < 380 or int(height) < 280):
        return 0
    if c == "DtMainFrameView":
        return 100
    if t in ("钉钉", "DingTalk"):
        if cl.startswith("qt"):
            return 95 if in_ding_pids else 85
        if in_ding_pids:
            return 80
    if in_ding_pids and c == "Qt51511QWindowIcon" and not t and (is_iconic or (int(width) >= 560 and int(height) >= 380)):
        return 60
    return 0


def _enum_dingtalk_im_windows(include_hidden=False):
    """按匹配得分与窗口状态枚举真正的钉钉聊天主窗：[(hwnd, pid, cls, title, score), ...]。"""
    if os.name != "nt":
        return []
    user32 = ctypes.windll.user32
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, wintypes.LPDWORD]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int

    ding_pids = {p for p, _ in _list_dingtalk_processes() if p}
    candidates = []

    def _cb(hwnd, _lp):
        vis = bool(user32.IsWindowVisible(hwnd))
        if not include_hidden and not vis:
            return True
        iconic = bool(user32.IsIconic(hwnd))
        cbuf = ctypes.create_unicode_buffer(128)
        tbuf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cbuf, 128)
        user32.GetWindowTextW(hwnd, tbuf, 256)
        pv = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pv))
        pid = int(pv.value or 0)
        rc = wintypes.RECT()
        w, h = 0, 0
        if user32.GetWindowRect(hwnd, ctypes.byref(rc)):
            w = int(rc.right - rc.left)
            h = int(rc.bottom - rc.top)
        in_ding = (pid in ding_pids) if ding_pids else True
        sc = _score_dingtalk_main_window(cbuf.value, tbuf.value, width=w, height=h, is_iconic=iconic, in_ding_pids=in_ding)
        if sc > 0:
            candidates.append((int(hwnd), pid, cbuf.value, tbuf.value, sc, 0 if (vis and not iconic) else 1, max(0, w * h)))
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    candidates.sort(key=lambda item: (-item[4], item[5], -item[6]))
    return [(item[0], item[1], item[2], item[3], item[4]) for item in candidates]


def _safe_control_from_handle(ua_mod, hwnd):
    """安全包裹 uiautomation.ControlFromHandle，防止窗口还原瞬间触发 COMError(-2147220991) 崩溃。"""
    if not hwnd or ua_mod is None:
        return None
    for attempt in range(2):
        try:
            ctl = ua_mod.ControlFromHandle(int(hwnd))
            if ctl is not None:
                return ctl
        except Exception:
            if attempt == 0:
                time.sleep(0.12)
    return None


def _extract_search_keywords(search_name):
    """从含 Emoji 与符号的群名提取高命中率纯文字搜索词和校验分词。"""
    import unicodedata
    normalized = unicodedata.normalize("NFKC", search_name or "").strip()
    tokens = re.findall(r"[\u4e00-\u9fffA-Za-z0-9]+", normalized)
    candidates = []

    def _add(w):
        s = str(w or "").strip()
        if s and s not in candidates:
            candidates.append(s)

    if tokens:
        # 优先非纯数字、非年月开头的中文/字母核心词，避免如“2026年11月 慕杰组”先拿年份去搜
        def _tok_sort_key(item):
            idx, tok = item
            is_num_only = tok.isdigit()
            is_date_tok = bool(re.fullmatch(r"\d{2,4}(?:年\d{1,2}月?|\d{2})", tok))
            return (1 if (is_num_only or is_date_tok) else 0, -len(tok), idx)

        by_len = sorted(enumerate(tokens), key=_tok_sort_key)
        ordered_tokens = [t for _, t in by_len]
        if len(ordered_tokens) >= 2:
            combo = f"{ordered_tokens[0]} {ordered_tokens[1]}"
            if len(combo) <= 16:
                _add(combo)
        for ot in ordered_tokens:
            if not ot.isdigit() and len(ot) >= 2:
                _add(ot)
        full_space = " ".join(tokens)
        if len(full_space) <= 24:
            _add(full_space)
        clean_joined = "".join(tokens)
        if len(tokens) >= 2 and len(clean_joined) <= 20:
            _add(clean_joined)
        # 对超长中文短语追加末端/首端核心切片兜底（例如“秋茶慕杰组”→“慕杰组”）
        for ot in ordered_tokens:
            if re.fullmatch(r"[\u4e00-\u9fff]{5,12}", ot):
                if ot.endswith(("组", "群", "部", "厅", "室")):
                    _add(ot[-3:])
                else:
                    _add(ot[:4])
    if normalized:
        _add(normalized)
    return candidates, tokens


def _is_search_popup_open():
    """毫秒级检测是否存在可见且宽高均大于 150 的钉钉全局搜索弹窗（ToolSaveBits）。"""
    if os.name != "nt":
        return False
    user32 = ctypes.windll.user32
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    for h, c, _t in _enum_top_windows():
        if "ToolSaveBits" in c:
            rect = wintypes.RECT()
            if user32.GetWindowRect(h, ctypes.byref(rect)):
                if (rect.right - rect.left) > 150 and (rect.bottom - rect.top) > 150:
                    return True
            else:
                return True
    return False


def _is_dingtalk_login_window(win):
    """检测找到的钉钉窗口是否仍处于未登录扫码/密码登录页。"""
    if win is None:
        return True
    try:
        rr = win.BoundingRectangle
        if (rr.right - rr.left) < 640 or (rr.bottom - rr.top) < 440:
            return True
    except Exception:
        pass
    try:
        hit_login = False
        deadline = time.monotonic() + 0.55
        visited = [0]

        def _scan_login(ctrl, depth=0):
            nonlocal hit_login
            if hit_login or depth > 6 or visited[0] >= 65 or time.monotonic() > deadline:
                return
            for ch in ctrl.GetChildren():
                visited[0] += 1
                if visited[0] % 15 == 0:
                    check_stop()
                if visited[0] > 65 or time.monotonic() > deadline:
                    return
                try:
                    nm = (ch.Name or "").strip()
                    if any(k in nm for k in ("扫码登录", "密码登录", "验证码登录", "钉钉账号登录", "安全登录")):
                        hit_login = True
                        return
                except Exception:
                    pass
                _scan_login(ch, depth + 1)

        _scan_login(win, 0)
        if hit_login:
            return True
    except Exception as ex:
        if "手动停止" in str(ex):
            raise
    return False


def get_process_elevation(pid=None):
    """检测当前进程或指定 PID 是否以管理员权限运行：True(管理员) / False(普通) / None(无法判定)。"""
    if os.name != "nt":
        return None
    TOKEN_QUERY = 0x0008
    TokenElevation = 20
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    adv32 = ctypes.WinDLL("advapi32", use_last_error=True)
    k32.GetCurrentProcess.argtypes = []
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    adv32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    adv32.OpenProcessToken.restype = wintypes.BOOL
    adv32.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)
    ]
    adv32.GetTokenInformation.restype = wintypes.BOOL

    class TOKEN_ELEVATION(ctypes.Structure):
        _fields_ = [("TokenIsElevated", wintypes.DWORD)]

    close_proc = False
    if pid is None:
        proc_handle = k32.GetCurrentProcess()
    else:
        proc_handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not proc_handle:
            if ctypes.get_last_error() == 5 and get_process_elevation(None) is False:
                return True
            return None
        close_proc = True

    tok = wintypes.HANDLE()
    try:
        if not adv32.OpenProcessToken(proc_handle, TOKEN_QUERY, ctypes.byref(tok)):
            if ctypes.get_last_error() == 5 and pid is not None and get_process_elevation(None) is False:
                return True
            return None
        try:
            elev = TOKEN_ELEVATION()
            rl = wintypes.DWORD()
            ok = adv32.GetTokenInformation(tok, TokenElevation, ctypes.byref(elev), ctypes.sizeof(elev), ctypes.byref(rl))
            if not ok:
                return None
            return bool(elev.TokenIsElevated)
        finally:
            k32.CloseHandle(tok)
    finally:
        if close_proc and proc_handle:
            k32.CloseHandle(proc_handle)


def _list_dingtalk_processes():
    """枚举当前系统运行的 DingTalk.exe 进程，返回 [(pid, exe_path), ...]。"""
    if os.name != "nt":
        return []
    TH32CS_SNAPPROCESS = 0x00000002
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    k32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
    ]
    k32.QueryFullProcessImageNameW.restype = wintypes.BOOL

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32FirstW.restype = wintypes.BOOL
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.restype = wintypes.BOOL

    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap in (None, 0, -1, ctypes.c_void_p(-1).value):
        return []
    out = []
    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not k32.Process32FirstW(snap, ctypes.byref(pe)):
            return []
        while True:
            if (pe.szExeFile or "").lower() == "dingtalk.exe":
                pid = int(pe.th32ProcessID)
                exe_path = ""
                hp = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
                if hp:
                    try:
                        buf = ctypes.create_unicode_buffer(1024)
                        sz = wintypes.DWORD(1024)
                        if k32.QueryFullProcessImageNameW(hp, 0, buf, ctypes.byref(sz)):
                            exe_path = buf.value
                    finally:
                        k32.CloseHandle(hp)
                out.append((pid, exe_path))
            if not k32.Process32NextW(snap, ctypes.byref(pe)):
                break
    finally:
        k32.CloseHandle(snap)
    return out


def _resolve_dingtalk_exe_candidates():
    """综合正在运行进程、Windows 注册表与常见盘符目录，动态定位 DingTalk.exe 安装路径。"""
    cands = []
    for _pid, ppath in _list_dingtalk_processes():
        if ppath and ppath not in cands:
            cands.append(ppath)
    if os.name == "nt":
        try:
            import winreg
            reg_queries = [
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\DingTalk.exe"),
                (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\DingTalk.exe"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\DingDing"),
            ]
            for root_k, sub in reg_queries:
                try:
                    with winreg.OpenKey(root_k, sub) as k:
                        val, _ = winreg.QueryValueEx(k, "")
                        v = str(val or "").strip().strip('"')
                        if v.lower().endswith(".exe") and v not in cands:
                            cands.append(v)
                except Exception:
                    pass
        except Exception:
            pass
    defaults = [
        r"D:\Program Files (x86)\DingDing\main\current_new\DingTalk.exe",
        r"C:\Program Files (x86)\DingDing\main\current_new\DingTalk.exe",
        r"C:\Program Files\DingDing\main\current_new\DingTalk.exe",
        r"D:\Program Files\DingDing\main\current_new\DingTalk.exe",
        r"E:\Program Files (x86)\DingDing\main\current_new\DingTalk.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\DingDing\main\current_new\DingTalk.exe"),
    ]
    for d in defaults:
        if d and d not in cands:
            cands.append(d)
    return cands


def check_dingtalk_privilege_match():
    """检查当前脚本与钉钉是否同权限运行（同为管理员或同为普通用户），避免 UIPI 静默拦截。"""
    self_elev = get_process_elevation(None)
    procs = _list_dingtalk_processes()
    if not procs:
        role = "管理员权限" if self_elev else "普通用户权限"
        return {
            "self_elevated": self_elev,
            "ding_running": False,
            "ding_elevated": None,
            "matched": True,
            "message": f"当前程序运行级别：{role}（暂未检测到运行中的 DingTalk.exe，发送时请保持同权限启动钉钉）",
        }
    ding_elev = None
    for pid, _pp in procs:
        ev = get_process_elevation(pid)
        if ev is not None:
            ding_elev = ev
            if ev is True:
                break
    matched = (self_elev is None) or (ding_elev is None) or (self_elev == ding_elev)
    self_lbl = "管理员权限" if self_elev else "普通用户权限"
    ding_lbl = "管理员权限" if ding_elev else ("普通用户权限" if ding_elev is False else "未知权限")
    if matched:
        msg = f"权限校验通过：当前程序与钉钉均运行在【{self_lbl}】"
    else:
        msg = (
            f"权限不一致警告：本程序为【{self_lbl}】，而钉钉为【{ding_lbl}】！"
            "两者权限不同会导致点「静音发送」或按键被 Windows 拦截，请关闭后以相同权限重新启动！"
        )
    return {
        "self_elevated": self_elev,
        "ding_running": True,
        "ding_elevated": ding_elev,
        "matched": matched,
        "message": msg,
    }


def _retry_step(step_name, fn, retries=2, interval=1.0):
    """按要求为单个操作步骤增加最多 retries 次重试，每次间隔 interval 秒。"""
    last_err = None
    for attempt in range(retries + 1):
        check_stop()
        try:
            res = fn()
            if res is not False:
                return res
            last_err = RuntimeError(f"{step_name} 返回未完成")
        except Exception as e:
            if "手动停止" in str(e):
                raise
            last_err = e
        if attempt < retries:
            log(f"  [{step_name}] 第 {attempt + 1} 次未成功，{interval:g} 秒后重试（剩余 {retries - attempt} 次）…")
            sleep_check(interval)
    if last_err is not None:
        raise last_err
    raise RuntimeError(f"{step_name} 重试耗尽仍失败")


def _unwrap_uia(ctrl):
    """兼容 WindowSpecification 与 UIAWrapper，统一返回底层 UIAWrapper。"""
    if ctrl is not None and hasattr(ctrl, "wrapper_object"):
        return ctrl.wrapper_object()
    return ctrl


def _resolve_best_dt_uia_hwnd(pids):
    """精准解析真正的钉钉聊天主窗 HWND 与 PID，避开钉钉-文档与无窗渲染子进程。"""
    if os.name == "nt":
        u32 = ctypes.windll.user32
        u32.IsWindow.argtypes = [wintypes.HWND]
        u32.IsWindow.restype = wintypes.BOOL
        u32.IsWindowVisible.argtypes = [wintypes.HWND]
        u32.IsWindowVisible.restype = wintypes.BOOL
        u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, wintypes.LPDWORD]
        u32.GetWindowThreadProcessId.restype = wintypes.DWORD
        if _active_dt_hwnd and u32.IsWindow(_active_dt_hwnd) and u32.IsWindowVisible(_active_dt_hwnd):
            pv = wintypes.DWORD(0)
            u32.GetWindowThreadProcessId(_active_dt_hwnd, ctypes.byref(pv))
            return int(_active_dt_hwnd), int(pv.value or 0)
        im_wins = _enum_dingtalk_im_windows(include_hidden=False)
        if im_wins:
            return im_wins[0][0], im_wins[0][1]

    try:
        from pywinauto import Desktop  # type: ignore[import-not-found]
        for w in Desktop(backend="uia").windows():
            try:
                if pids and w.process_id() not in pids:
                    continue
                cls = (w.element_info.class_name or "")
                nm = (w.element_info.name or "")
                if _is_dingtalk_aux_window(cls, nm):
                    continue
                rr = w.rectangle()
                if w.is_visible() and rr.width() >= 500 and rr.height() >= 340:
                    return int(w.handle), int(w.process_id())
            except Exception:
                pass
    except Exception:
        pass
    return 0, 0


_current_pc_plan = {
    "screen_w": 1920,
    "screen_h": 1080,
    "dpi_scale": 1.0,
    "dt_theme": "unknown",
    "dt_theme_cn": "默认",
    "uia_ms": 0,
    "scales": (1.0, 1.5, 1.875, 2.0, 2.25, 1.25, 1.75, 2.5, 2.75, 3.0, 0.85),
    "search_settle_sec": 0.9,
    "summary": "常规标准屏适配",
}


def probe_pc_and_dingtalk_profile(cfg=None):
    """每次运行前自动探测当前电脑分辨率、DPI 缩放、钉钉明暗主题与 UIA 响应速度，动态生成最稳适配方案。"""
    global _current_pc_plan
    sw, sh = 1920, 1080
    dpi_scale = 1.0
    dt_theme = "unknown"
    dt_theme_cn = "默认"
    uia_ms = 0
    dt_hwnd = 0

    if os.name == "nt":
        try:
            u32 = ctypes.windll.user32
            sw = int(u32.GetSystemMetrics(0) or 1920)
            sh = int(u32.GetSystemMetrics(1) or 1080)
            im_wins = _enum_dingtalk_im_windows(include_hidden=True)
            if im_wins:
                dt_hwnd = int(im_wins[0][0] or 0)
            dpi_val = 96
            if dt_hwnd and hasattr(u32, "GetDpiForWindow"):
                try:
                    dpi_val = int(u32.GetDpiForWindow(dt_hwnd) or 96)
                except Exception:
                    dpi_val = 96
            elif hasattr(u32, "GetDpiForSystem"):
                try:
                    dpi_val = int(u32.GetDpiForSystem() or 96)
                except Exception:
                    dpi_val = 96
            if dpi_val >= 72:
                dpi_scale = round(max(0.75, min(3.5, dpi_val / 96.0)), 2)
        except Exception:
            pass

        if dt_hwnd and ctypes.windll.user32.IsWindowVisible(dt_hwnd) and not ctypes.windll.user32.IsIconic(dt_hwnd):
            try:
                from PIL import ImageGrab
                rc = wintypes.RECT()
                if ctypes.windll.user32.GetWindowRect(dt_hwnd, ctypes.byref(rc)):
                    w = rc.right - rc.left
                    h = rc.bottom - rc.top
                    if w > 300 and h > 300:
                        box = (rc.left + int(w * 0.45), rc.top + int(h * 0.65), rc.left + int(w * 0.55), rc.top + int(h * 0.72))
                        grab_im = None
                        crop = None
                        try:
                            grab_im = ImageGrab.grab(bbox=box)
                            if grab_im is not None:
                                crop = grab_im.convert("L")
                                pxs = list(crop.getdata())
                                if pxs:
                                    mean_lum = sum(pxs) / len(pxs)
                                    dt_theme = "dark" if mean_lum < 95 else "light"
                                    dt_theme_cn = "深色模式" if dt_theme == "dark" else "浅色模式"
                        finally:
                            for obj in (crop, grab_im):
                                if obj is not None:
                                    try:
                                        obj.close()
                                    except Exception:
                                        pass
            except Exception:
                pass

        try:
            t0 = time.monotonic()
            ctypes.windll.user32.GetForegroundWindow()
            if dt_hwnd:
                _get_dt_main_win_rect()
            uia_ms = int((time.monotonic() - t0) * 1000)
        except Exception:
            uia_ms = 0

    base_scales = [1.0, 1.25, 1.5, 1.75, 1.875, 2.0, 2.25, 2.5, 2.75, 3.0, 0.85]
    prioritized = sorted(base_scales, key=lambda s: abs(s - dpi_scale))
    if round(dpi_scale, 3) not in prioritized:
        prioritized.insert(0, round(dpi_scale, 3))

    if sw >= 3200 or dpi_scale >= 1.8:
        screen_tier = "4K超高清大屏"
        settle = 1.05
    elif sw >= 2200 or dpi_scale >= 1.35:
        screen_tier = "2K高分屏"
        settle = 0.95
    else:
        screen_tier = "常规标准屏"
        settle = 0.85

    if uia_ms > 280:
        settle = min(1.5, settle + 0.35)

    summary = f"{screen_tier} {sw}×{sh}（DPI {dpi_scale:.2f}x）｜钉钉{dt_theme_cn}｜搜索稳态{settle:.2f}s"
    plan = {
        "screen_w": sw,
        "screen_h": sh,
        "dpi_scale": dpi_scale,
        "dt_theme": dt_theme,
        "dt_theme_cn": dt_theme_cn,
        "uia_ms": uia_ms,
        "scales": tuple(prioritized),
        "search_settle_sec": settle,
        "summary": summary,
    }
    _current_pc_plan = plan
    return plan


def _try_click_qui_menu_by_win32():
    """直接按可见 Qt 弹出菜单窗口（im_chat::QuiMenu）尺寸比例命中第2项「设为静音」，兼容所有分辨率与深色皮肤。"""
    if os.name != "nt":
        return False
    try:
        import pyautogui  # type: ignore[import-not-found]
        u32 = ctypes.windll.user32
        rc = wintypes.RECT()
        for qh, _qc, _qt in _enum_top_windows(target_classes=("im_chat::QuiMenu",), target_titles=None):
            if not u32.IsWindowVisible(qh):
                continue
            if u32.GetWindowRect(qh, ctypes.byref(rc)):
                w = int(rc.right - rc.left)
                h = int(rc.bottom - rc.top)
                if w >= 60 and h >= 48:
                    cx = int(rc.left + w // 2)
                    cy = int(rc.top + max(12, int(h * 0.74)))
                    log(f"  [静音模式: Qt弹出窗比例命中] 检测到菜单尺寸 {w}x{h}，点击第2项「设为静音」({cx}, {cy})")
                    pyautogui.click(cx, cy)
                    return True
    except Exception:
        pass
    return False


def _connect_dingtalk_pywinauto(timeout=5.0):
    """使用 pywinauto(backend='uia') 直接按真实聊天主窗句柄连接，兜底才走 path='DingTalk.exe'。"""
    from pywinauto import Application  # type: ignore[import-not-found]

    procs = _list_dingtalk_processes()
    pids = {p for p, _ in procs}
    target_path = procs[0][1] if (procs and procs[0][1]) else "DingTalk.exe"

    best_hwnd, _best_pid = _resolve_best_dt_uia_hwnd(pids)
    if best_hwnd:
        try:
            app = Application(backend="uia").connect(handle=best_hwnd, timeout=timeout)
            return app, app.window(handle=best_hwnd), pids
        except Exception:
            pass

    app = None
    try:
        app = Application(backend="uia").connect(path="DingTalk.exe", timeout=min(3.0, timeout))
    except Exception:
        app = Application(backend="uia").connect(path=target_path, timeout=timeout)

    cand = app.window(class_name="DtMainFrameView")
    win_spec = cand if cand.exists(timeout=1.5) else app.top_window()
    return app, win_spec, pids


def _dump_uia_controls_to_log(uia_win, pids=None, max_depth=14):
    """UIA 失败时跳过聊天记录大列表，快速输出主窗下半区域与弹出层控件树快照。"""
    log("  ===== UIA 可用控件树快照（排查参考）=====")
    lines = []
    root_wrap = _unwrap_uia(uia_win)
    try:
        wrect = root_wrap.rectangle() if root_wrap is not None else None
    except Exception:
        wrect = None

    def _walk_pruned(start_ctrl, base_depth=0):
        for ctrl, depth in _iter_dt_uia_pruned(start_ctrl, max_depth=max_depth):
            if len(lines) >= 85:
                break
            d = base_depth + depth
            try:
                info = ctrl.element_info
                ct = info.control_type or ""
                nm = (info.name or "").replace("\r", " ").replace("\n", " ").strip()
                aid = info.automation_id or ""
                cls = info.class_name or ""
                rr = ctrl.rectangle()
                if (nm or aid or ct in ("Button", "MenuItem", "SplitButton", "Edit", "Document")) and (
                    wrect is None or d <= 4 or rr.top >= wrect.top + int((wrect.bottom - wrect.top) * 0.62)
                ):
                    lines.append(f"{'  '*min(d, 6)}[{ct}] name={nm!r} auto_id={aid!r} cls={cls!r}")
            except Exception:
                pass

    if root_wrap is not None:
        _walk_pruned(root_wrap, 0)
    if pids:
        try:
            from pywinauto import Desktop  # type: ignore[import-not-found]
            root_hwnd = root_wrap.element_info.handle if root_wrap is not None else None
            for pw in Desktop(backend="uia").windows():
                try:
                    if pw.process_id() in pids and (root_hwnd is None or pw.element_info.handle != root_hwnd):
                        lines.append(f"[弹出层] cls={pw.element_info.class_name!r} name={pw.element_info.name!r}")
                        _walk_pruned(pw, 1)
                except Exception:
                    pass
        except Exception:
            pass

    if not lines:
        log("  （控件树未暴露可识别子节点）")
    else:
        for ln in lines[:85]:
            log(f"  {ln}")
    log("  ===== 控件树快照结束 =====")


DT_SUB_MENU_BUTTON_AUTO_ID = (
    "qt_chat_navigable_content_widget.stackedWidget.mesasgePage.widget.splitter."
    "widgetRichEditWnd.drich_edit.verticalContentWidget.widget_input_wnd."
    "bottomBarWidget.combo_send_button_widget.containerWidget.rightContainerWidget.subMenuButton"
)
DT_RIGHT_SEND_CONTAINER_ID = (
    "qt_chat_navigable_content_widget.stackedWidget.mesasgePage.widget.splitter."
    "widgetRichEditWnd.drich_edit.verticalContentWidget.widget_input_wnd."
    "bottomBarWidget.combo_send_button_widget.containerWidget.rightContainerWidget"
)
DT_RICH_EDIT_AUTO_ID = (
    "qt_chat_navigable_content_widget.stackedWidget.mesasgePage.widget.splitter."
    "widgetRichEditWnd.drich_edit"
)


def _iter_dt_uia_pruned(root_wrap, max_depth=24):
    """快速遍历 Qt UIA 子树，跳过消息历史/会话列表超长分支以便毫秒级直达底栏控件。"""
    skip_aids = ("messagelist", "sessionlist", "chat_list", "conversationlist")
    stack = [(root_wrap, 0)]
    while stack:
        ctrl, depth = stack.pop()
        if ctrl is None or depth > max_depth:
            continue
        yield ctrl, depth
        if depth >= max_depth:
            continue
        try:
            kids = ctrl.children()
        except Exception:
            kids = []
        for ch in reversed(kids):
            try:
                aid = (ch.element_info.automation_id or "").lower()
                if any(sk in aid for sk in skip_aids):
                    continue
            except Exception:
                pass
            stack.append((ch, depth + 1))


def _find_dt_qt_arrow_ctrl(uia_win):
    """优先使用实测精准 AutomationId（rightContainerWidget.subMenuButton）无坐标定位下拉箭头。"""
    if hasattr(uia_win, "child_window"):
        try:
            cand = uia_win.child_window(auto_id=DT_SUB_MENU_BUTTON_AUTO_ID, found_index=0)
            if cand.exists(timeout=0.5):
                return cand
        except Exception:
            pass

    wrap = _unwrap_uia(uia_win)
    exact_low = DT_SUB_MENU_BUTTON_AUTO_ID.lower()
    right_low = DT_RIGHT_SEND_CONTAINER_ID.lower()
    suffix_hit = None
    right_box = None
    for ctrl, _depth in _iter_dt_uia_pruned(wrap, max_depth=24):
        try:
            info = ctrl.element_info
            aid = (info.automation_id or "")
            aid_l = aid.lower()
            cls = info.class_name or ""
            if aid_l == exact_low:
                return ctrl
            if suffix_hit is None and (
                aid_l.endswith("rightcontainerwidget.submenubutton")
                or (aid_l.endswith(".submenubutton") and cls == "ddesign::IconButton")
            ):
                suffix_hit = ctrl
            if right_box is None and (aid_l == right_low or aid_l.endswith(".rightcontainerwidget")):
                right_box = ctrl
        except Exception:
            pass
    if suffix_hit is not None:
        return suffix_hit
    if right_box is not None:
        for ch, _ in _iter_dt_uia_pruned(right_box, max_depth=6):
            try:
                if (ch.element_info.control_type or "") == "Button":
                    return ch
            except Exception:
                pass
    return None


def _find_dt_qui_menu_silent_item(pop_windows):
    """在 Qt 弹出菜单（如 im_chat::QuiMenu）中无坐标定位第 2 个菜单项控件（设为静音，兼容 4K 高缩放）。"""
    from types import SimpleNamespace

    class _QuiMenuAreaClick:
        def __init__(self, pw_wrap, rel_x, rel_y):
            self._pw = pw_wrap
            self._rx = rel_x
            self._ry = rel_y

        def click_input(self):
            self._pw.click_input(coords=(self._rx, self._ry))

    menu_types = {"MenuItem", "Button", "ListItem", "Text", "Custom", "Pane"}
    dpi_sc = float((_current_pc_plan or {}).get("dpi_scale") or 1.0)
    max_row_h = max(160, int(110 * dpi_sc))
    qui_fallback = None
    for pw in (pop_windows or []):
        rows = []
        pwrap = _unwrap_uia(pw)
        pcls = ""
        try:
            pcls = (pwrap.element_info.class_name or "")
        except Exception:
            pass
        for ctrl, depth in _iter_dt_uia_pruned(pwrap, max_depth=12):
            if depth < 1:
                continue
            try:
                info = ctrl.element_info
                rr = ctrl.rectangle()
                if (info.control_type or "") in menu_types and rr.width() >= 36 and 14 <= rr.height() <= max_row_h:
                    rows.append((rr.top, rr.height(), ctrl, info))
            except Exception:
                pass
        dedup = []
        for top_v, h_v, ctrl, info in sorted(rows, key=lambda x: (x[0], x[1])):
            if not dedup or abs(top_v - dedup[-1][0]) >= 10:
                dedup.append((top_v, ctrl, info))
        if len(dedup) >= 3 or (pcls == "im_chat::QuiMenu" and len(dedup) >= 2):
            second = dedup[1]
            return second[1], second[2]
        if pcls == "im_chat::QuiMenu" and qui_fallback is None:
            try:
                pr = pwrap.rectangle()
                if pr.width() >= 60 and pr.height() >= 48:
                    qui_fallback = (
                        _QuiMenuAreaClick(pwrap, max(10, pr.width() // 2), max(12, int(pr.height() * 0.74))),
                        SimpleNamespace(control_type="MenuItem", class_name=pcls),
                    )
            except Exception:
                pass
    return qui_fallback


def _uia_find_silent_badge(uia_win):
    """通过 child_window 及输入区子树检测是否已开启「静音发送」。"""
    try:
        badge = uia_win.child_window(title_re=r".*静音发送.*", found_index=0)
        if badge.exists(timeout=0.3):
            wrect = _unwrap_uia(uia_win).rectangle()
            brect = _unwrap_uia(badge).rectangle()
            if brect.width() > 0 and brect.top >= wrect.top + int(wrect.height() * 0.60):
                return True
    except Exception:
        pass
    try:
        wrap = _unwrap_uia(uia_win)
        rich_low = DT_RICH_EDIT_AUTO_ID.lower()
        for ctrl, _ in _iter_dt_uia_pruned(wrap, max_depth=18):
            aid_l = (ctrl.element_info.automation_id or "").lower()
            if aid_l == rich_low or aid_l.endswith(".drich_edit"):
                for ch, _ in _iter_dt_uia_pruned(ctrl, max_depth=8):
                    try:
                        nm = (ch.element_info.name or "")
                        rr = ch.rectangle()
                        if "静音发送" in nm and rr.width() > 0 and rr.height() > 0:
                            return True
                    except Exception:
                        pass
                break
    except Exception:
        pass
    return False


def _uia_find_arrow_ctrl(uia_win):
    """仅用 child_window(auto_id / title_re) 及精准子树定位发送右侧下拉箭头控件，禁止硬编码坐标。"""
    qt_arrow = _find_dt_qt_arrow_ctrl(uia_win)
    if qt_arrow is not None:
        return qt_arrow

    wrect = _unwrap_uia(uia_win).rectangle()
    bottom_min_y = wrect.top + int(wrect.height() * 0.65)

    for kw_spec in (
        {"auto_id_re": r"(?i).*(send.*arrow|arrow.*send|send.*menu|send.*more|split.*arrow).*", "found_index": 0},
        {"title_re": r"^(发送选项|发送设置|更多发送方式|下拉|选择发送方式)$", "found_index": 0},
    ):
        try:
            cand = uia_win.child_window(**kw_spec)
            if cand.exists(timeout=0.25):
                rr = _unwrap_uia(cand).rectangle()
                if rr.width() > 0 and rr.top >= bottom_min_y:
                    return cand
        except Exception:
            pass

    send_ctrl = None
    for spec in (
        {"title_re": r"^发送(\([A-Za-z]\))?$", "control_type": "Button", "found_index": 0},
        {"title_re": r"^发送(\([A-Za-z]\))?$", "found_index": 0},
        {"auto_id_re": r"(?i).*send.*btn.*", "found_index": 0},
    ):
        try:
            cand = uia_win.child_window(**spec)
            if cand.exists(timeout=0.3):
                rr = _unwrap_uia(cand).rectangle()
                if rr.width() >= 20 and rr.top >= bottom_min_y:
                    send_ctrl = cand
                    break
        except Exception:
            pass

    if send_ctrl is None:
        return None

    send_wrap = _unwrap_uia(send_ctrl)
    si = send_wrap.element_info
    sr = send_wrap.rectangle()
    log(f"  [静音模式: pywinauto-UIA] 定位发送按钮: type={si.control_type!r} name={si.name!r} auto_id={si.automation_id!r}")

    arrow_types = ("Button", "MenuItem", "SplitButton", "Custom", "Image", "Pane")

    def _scan_arrow_candidates(cands):
        for sib in cands:
            try:
                sbi = sib.element_info
                if sbi == si:
                    continue
                sbr = sib.rectangle()
                said = (sbi.automation_id or "").lower()
                sname = (sbi.name or "").strip()
                if sbi.control_type in arrow_types and sbr.width() > 0 and sbr.height() >= 12:
                    if (
                        any(k in said for k in ("arrow", "drop", "more", "menu"))
                        or any(k in sname for k in ("更多", "设置", "选项", "下拉"))
                        or (sbr.left >= sr.right - 22 and sbr.width() <= 44 and abs(sbr.mid_point()[1] - sr.mid_point()[1]) <= 12)
                    ):
                        return sib
            except Exception:
                pass
        return None

    # 先扫描发送控件内部子控件（兼容 SplitButton 内嵌箭头），再扫描同级兄弟控件
    try:
        hit = _scan_arrow_candidates(send_wrap.children())
        if hit is not None:
            return hit
    except Exception:
        pass
    try:
        parent = send_wrap.parent()
        if parent is not None:
            hit = _scan_arrow_candidates(parent.children())
            if hit is not None:
                return hit
    except Exception:
        pass
    return None


def _uia_click_ctrl(ctrl_wrap):
    """优先物理 click_input 点击控件；若隔离桌面或锁屏拦截鼠标光标且控件支持 invoke() 则回退调用。"""
    try:
        ctrl_wrap.click_input()
        return True
    except Exception as err:
        err_l = f"{err!r} {err}".lower()
        if (
            any(k in err_l for k in ("setcursorpos", "getcursorpos", "no active desktop", "mouse_event", "拒绝访问", "access is denied"))
            and hasattr(ctrl_wrap, "invoke")
        ):
            try:
                ctrl_wrap.invoke()
                return True
            except Exception:
                pass
        raise


def _uia_find_and_click_silent_menu(uia_win, pids, enable_silent=True):
    """先毫秒级在弹出菜单窗与 Qt QuiMenu 中定位，再剪枝扫描主窗（跳过聊天历史大列表），绝不卡死。"""
    from pywinauto import Application, Desktop, keyboard  # type: ignore[import-not-found]

    check_stop()
    pop_specs = []
    pop_windows = []
    main_hwnd = getattr(_unwrap_uia(uia_win), "handle", None)
    for pw in Desktop(backend="uia").windows():
        try:
            if pids and pw.process_id() not in pids:
                continue
            if not pw.is_visible():
                continue
            if main_hwnd is not None and pw.handle == main_hwnd:
                continue
            pop_windows.append(pw)
            pw_spec = Application(backend="uia").connect(handle=pw.handle, timeout=1.5).window(handle=pw.handle)
            pop_specs.append(pw_spec)
        except Exception:
            pass

    already_re = r".*取消静音.*" if enable_silent else r".*(设为静音|静音发送).*"
    already_msg = "菜单显示当前已处于静音发送状态" if enable_silent else "菜单显示当前已处于常规提醒状态"
    target_re = r".*(设为静音|静音发送).*" if enable_silent else r".*取消静音.*"
    specs = [
        {"title_re": target_re, "control_type": "MenuItem", "found_index": 0},
        {"title_re": target_re, "control_type": "Button", "found_index": 0},
        {"title_re": target_re, "found_index": 0},
    ]
    if enable_silent:
        specs.append({"auto_id_re": r"(?i).*(silent|mute).*", "found_index": 0})

    # 1) 优先秒扫独立弹出菜单窗（通常仅有 2~4 个控件）
    for root in pop_specs:
        check_stop()
        try:
            already_item = root.child_window(title_re=already_re, found_index=0)
            if already_item.exists(timeout=0.15):
                keyboard.send_keys("{ESC}")
                log(f"  [静音模式: pywinauto-UIA] {already_msg}")
                return True
        except Exception:
            pass

        for spec in specs:
            try:
                item = root.child_window(**spec)
                if item.exists(timeout=0.2):
                    item_wrap = _unwrap_uia(item)
                    ir = item_wrap.rectangle()
                    if ir.width() > 0 and ir.height() > 0:
                        ii = item_wrap.element_info
                        log(f"  [静音模式: pywinauto-UIA] 命中菜单项: type={ii.control_type!r} name={ii.name!r} auto_id={ii.automation_id!r}")
                        _uia_click_ctrl(item_wrap)
                        return True
            except Exception:
                pass

    # 2) Qt 弹出菜单（如 im_chat::QuiMenu）无暴露文字时，支持开启或取消静音点击第 2 项
    if not pop_windows:
        try:
            for qh, _qc, _qt in _enum_top_windows(target_classes=("im_chat::QuiMenu",), target_titles=None):
                try:
                    q_spec = Application(backend="uia").connect(handle=qh, timeout=1.0).window(handle=qh)
                    q_wrap = _unwrap_uia(q_spec)
                    if q_wrap is not None:
                        pop_windows.append(q_wrap)
                except Exception:
                    pass
        except Exception:
            pass

    if pop_windows:
        qui_hit = _find_dt_qui_menu_silent_item(pop_windows)
        if qui_hit is not None:
            q_ctrl, q_info = qui_hit
            act_lbl_q = "设为静音" if enable_silent else "取消静音"
            log(f"  [静音模式: pywinauto-UIA] 命中 Qt 弹出菜单第2项「{act_lbl_q}」: type={q_info.control_type!r} cls={q_info.class_name!r}")
            _uia_click_ctrl(q_ctrl)
            return True

    # 3) 对主窗仅走剪枝子树（跳过 messagelist / sessionlist），毫秒级兜底绝不阻塞
    check_stop()
    already_kw = ("取消静音",) if enable_silent else ("设为静音", "静音发送")
    target_kw = ("设为静音", "静音发送") if enable_silent else ("取消静音",)
    win_wrap = _unwrap_uia(uia_win)
    if win_wrap is not None:
        visited = 0
        for ctrl, _depth in _iter_dt_uia_pruned(win_wrap, max_depth=14):
            visited += 1
            if visited % 20 == 0:
                check_stop()
            if visited > 90:
                break
            try:
                ii = ctrl.element_info
                rr = ctrl.rectangle()
                if rr.width() <= 0 or rr.height() <= 0:
                    continue
                nm = str(ii.name or "")
                aid = str(ii.automation_id or "").lower()
                if any(k in nm for k in already_kw):
                    keyboard.send_keys("{ESC}")
                    log(f"  [静音模式: pywinauto-UIA] {already_msg}")
                    return True
                if any(k in nm for k in target_kw) or (enable_silent and any(k in aid for k in ("silent", "mute"))):
                    if (ii.control_type or "") in ("MenuItem", "Button", "ListItem", "Text", "Custom", "Pane"):
                        log(f"  [静音模式: pywinauto-UIA] 命中菜单项: type={ii.control_type!r} name={ii.name!r} auto_id={ii.automation_id!r}")
                        _uia_click_ctrl(ctrl)
                        return True
            except Exception:
                pass
    return False


def _try_click_open_silent_menu_uia():
    """当发送下拉菜单已在屏幕上打开时，直接借助 UIA 菜单项/Qt QuiMenu 第 2 项规则精准点击「设为静音」。"""
    try:
        _app, win, pids = _connect_dingtalk_pywinauto(timeout=2.5)
        if win is not None and _uia_find_and_click_silent_menu(win, pids):
            return True
    except Exception as e_u:
        if _is_stop_exception(e_u):
            raise
    return False


def _enable_dingtalk_silent_send_uia():
    """主模式：pywinauto(backend='uia') 通过 connect(path='DingTalk.exe') 定位并点击静音发送。"""
    state = {"win": None, "pids": set(), "arrow_clicked": False, "arrow_wrap": None}
    try:
        def _step_focus():
            _app, win, pids = _connect_dingtalk_pywinauto(timeout=5.0)
            state["win"] = win
            state["pids"] = pids
            win.set_focus()
            sleep_check(0.25)
            return True

        _retry_step("UIA连接钉钉并set_focus", _step_focus, retries=2, interval=1.0)
        win = state["win"]
        if _uia_find_silent_badge(win):
            log("  [静音模式: pywinauto-UIA] 当前已开启「静音发送」")
            return True
        if _uia_find_and_click_silent_menu(win, state["pids"]):
            sleep_check(0.3)
            log("  ✅ [静音模式: pywinauto-UIA] 已从当前展开菜单选择「静音发送」")
            return True

        def _step_arrow():
            arrow = _uia_find_arrow_ctrl(state["win"])
            if arrow is None:
                return False
            arrow_wrap = _unwrap_uia(arrow)
            state["arrow_wrap"] = arrow_wrap
            ai = arrow_wrap.element_info
            log(f"  [静音模式: pywinauto-UIA] 点击下拉箭头: type={ai.control_type!r} name={ai.name!r} auto_id={ai.automation_id!r}")
            _uia_click_ctrl(arrow_wrap)
            state["arrow_clicked"] = True
            return True

        _retry_step("UIA点击发送下拉箭头", _step_arrow, retries=2, interval=1.0)
        sleep_check(0.5)

        def _step_menu():
            if _uia_find_and_click_silent_menu(state["win"], state["pids"]):
                return True
            # 若桌面无任何可见弹出窗（说明首次点击未成功弹出菜单），优先用物理点击再试 invoke() 展开菜单
            try:
                from pywinauto import Desktop  # type: ignore[import-not-found]
                main_h = getattr(_unwrap_uia(state["win"]), "handle", None)
                pids = state["pids"]
                has_pop = any(
                    pw.is_visible() and (not pids or pw.process_id() in pids) and pw.handle != main_h
                    for pw in Desktop(backend="uia").windows()
                )
                aw = state.get("arrow_wrap")
                if not has_pop and aw is not None:
                    try:
                        _uia_click_ctrl(aw)
                    except Exception:
                        if hasattr(aw, "invoke"):
                            aw.invoke()
                    sleep_check(0.5)
                    return _uia_find_and_click_silent_menu(state["win"], state["pids"])
            except Exception as e_pop:
                if "手动停止" in str(e_pop):
                    raise
            return False

        _retry_step("UIA点击静音发送菜单", _step_menu, retries=2, interval=1.0)
        sleep_check(0.35)
        log("  ✅ [静音模式: pywinauto-UIA] 已选择「静音发送」")
        return True
    except Exception as e:
        if "手动停止" in str(e):
            raise
        log(f"  ⚠️ [静音模式: pywinauto-UIA] 未能在控件树直接完成点击（{e}）")
        if state["win"] is not None:
            _dump_uia_controls_to_log(state["win"], state["pids"])
        if state["arrow_clicked"]:
            try:
                from pywinauto import keyboard  # type: ignore[import-not-found]
                keyboard.send_keys("{ESC}")
                time.sleep(0.2)
            except Exception:
                pass
        return False


def _get_dt_main_win_rect():
    """获取当前可见钉钉聊天主窗矩形 (left, top, right, bottom)，未检测到则返回 None。"""
    if os.name != "nt":
        return None
    u32 = ctypes.windll.user32
    u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    u32.GetWindowRect.restype = wintypes.BOOL
    u32.IsWindow.argtypes = [wintypes.HWND]
    u32.IsWindow.restype = wintypes.BOOL
    u32.IsWindowVisible.argtypes = [wintypes.HWND]
    u32.IsWindowVisible.restype = wintypes.BOOL
    hwnd = 0
    if _active_dt_hwnd and u32.IsWindow(_active_dt_hwnd) and u32.IsWindowVisible(_active_dt_hwnd):
        hwnd = int(_active_dt_hwnd)
    else:
        im_wins = _enum_dingtalk_im_windows(include_hidden=False)
        if im_wins:
            hwnd = int(im_wins[0][0])
    if not hwnd:
        return None
    rc = wintypes.RECT()
    if not u32.GetWindowRect(hwnd, ctypes.byref(rc)):
        return None
    if rc.right <= rc.left or rc.bottom <= rc.top:
        return None
    return (int(rc.left), int(rc.top), int(rc.right), int(rc.bottom))


def _get_dt_send_region_rect(tpl_name, win_rect=None):
    """返回钉钉底栏发送区域裁切矩形 (left, top, width, height)，消除全屏右上/左上误匹配。"""
    wrect = win_rect if win_rect is not None else _get_dt_main_win_rect()
    if not wrect:
        return None
    L, T, R, B = wrect
    W = max(1, R - L)
    H = max(1, B - T)
    rx = L + (int(W * 0.45) if tpl_name in ("send_arrow.png", "send_pill.png", "menu_set_silent.png") else int(W * 0.18))
    ry = T + int(H * 0.46)
    rw = max(80, R - rx)
    rh = max(80, B - ry)
    return (rx, ry, rw, rh)


def _is_box_in_dt_send_region(box, tpl_name, win_rect=None):
    """校验匹配到的模板框中心点确实落在钉钉聊天窗底部（右侧）发送区，绝不误点左上角 (185, 94)。"""
    if box is None:
        return False
    wrect = win_rect if win_rect is not None else _get_dt_main_win_rect()
    if not wrect:
        return True
    L, T, R, B = wrect
    W = max(1, R - L)
    H = max(1, B - T)
    cx = float(box.left) + float(box.width) / 2.0
    cy = float(box.top) + float(box.height) / 2.0
    if not (L <= cx <= R and T + H * 0.46 <= cy <= B):
        return False
    if tpl_name in ("send_arrow.png", "send_pill.png", "menu_set_silent.png") and cx < L + W * 0.45:
        return False
    return True


def _locate_template_on_screen(
    tpl_name,
    scales=(1.0, 1.875, 1.5, 2.0, 2.25, 2.5, 2.75, 3.0, 1.25, 1.75, 0.85),
    confidences=(0.82, 0.74),
    _screen_img=None,
):
    """调用 pyautogui.locateOnScreen 及多尺度灰度匹配查找模板位置（真实桌面限定在钉钉底栏发送区域内）。"""
    tpl_path = _find_bundle_asset(Path("templates") / tpl_name)
    if tpl_path is None:
        raise RuntimeError(f"找不到图像匹配模板文件：templates/{tpl_name}")

    import pyautogui  # type: ignore[import-not-found]
    from PIL import ImageOps
    pyautogui.FAILSAFE = False

    win_rect = _get_dt_main_win_rect() if _screen_img is None else None
    dt_reg = _get_dt_send_region_rect(tpl_name, win_rect=win_rect) if win_rect else None
    off_x, off_y = (dt_reg[0], dt_reg[1]) if dt_reg is not None else (0, 0)
    if _screen_img is None and _current_pc_plan.get("scales"):
        scales = tuple(_current_pc_plan["scales"])

    def _to_screen_box(box):
        if box is None:
            return None
        if dt_reg is None:
            return box
        return box.__class__(int(box.left + off_x), int(box.top + off_y), int(box.width), int(box.height))

    if _screen_img is None:
        for conf in confidences:
            try:
                kw = {"grayscale": True, "confidence": conf}
                if dt_reg is not None:
                    kw["region"] = dt_reg
                box = pyautogui.locateOnScreen(str(tpl_path), **kw)
                if box is not None and (win_rect is None or _is_box_in_dt_send_region(box, tpl_name, win_rect=win_rect)):
                    return box
            except TypeError:
                try:
                    kw_nc = {"grayscale": True}
                    if dt_reg is not None:
                        kw_nc["region"] = dt_reg
                    box = pyautogui.locateOnScreen(str(tpl_path), **kw_nc)
                    if box is not None and (win_rect is None or _is_box_in_dt_send_region(box, tpl_name, win_rect=win_rect)):
                        return box
                except Exception:
                    pass
                break
            except Exception:
                pass
        grab_scr = pyautogui.screenshot(region=dt_reg) if dt_reg is not None else pyautogui.screenshot()
        try:
            screen_gray = grab_scr.convert("L")
        finally:
            try:
                grab_scr.close()
            except Exception:
                pass
    else:
        screen_gray = _screen_img.convert("L")

    tpl_gray = None
    tpl_inv = None
    try:
        with Image.open(tpl_path) as raw_tpl:
            tpl_gray = raw_tpl.convert("L")
        tpl_inv = ImageOps.invert(tpl_gray)
        for base_img in (tpl_gray, tpl_inv):
            for sc in scales:
                nw = max(8, int(round(base_img.width * sc)))
                nh = max(6, int(round(base_img.height * sc)))
                if nw >= screen_gray.width or nh >= screen_gray.height:
                    continue
                is_copy = abs(sc - 1.0) >= 0.02
                resized = base_img.resize((nw, nh), Image.Resampling.LANCZOS) if is_copy else base_img
                try:
                    for conf in confidences:
                        try:
                            box = pyautogui.locate(resized, screen_gray, grayscale=True, confidence=conf)
                            if box is not None:
                                sbox = _to_screen_box(box)
                                if win_rect is None or _is_box_in_dt_send_region(sbox, tpl_name, win_rect=win_rect):
                                    return sbox
                        except TypeError:
                            try:
                                box = pyautogui.locate(resized, screen_gray, grayscale=True)
                                if box is not None:
                                    sbox = _to_screen_box(box)
                                    if win_rect is None or _is_box_in_dt_send_region(sbox, tpl_name, win_rect=win_rect):
                                        return sbox
                            except Exception:
                                pass
                            break
                        except Exception:
                            pass
                finally:
                    if is_copy:
                        try:
                            resized.close()
                        except Exception:
                            pass
    finally:
        for obj in (tpl_inv, tpl_gray, screen_gray):
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
    return None


def _enable_dingtalk_silent_send_image():
    """兜底模式：当 UIA 抓不到控件时，降级为 pyautogui.locateOnScreen 图像匹配，并联动 UIA 菜单项点击。"""
    import pyautogui  # type: ignore[import-not-found]
    pyautogui.FAILSAFE = False
    log("  正在切换为 [静音模式: pyautogui-图像匹配兜底] …")

    if _locate_template_on_screen("badge_silent_on.png", confidences=(0.82, 0.76)) is not None:
        log("  [静音模式: pyautogui-图像匹配兜底] 检测到屏幕已存在「静音发送」标签")
        return True

    if _try_click_open_silent_menu_uia():
        sleep_check(0.35)
        log("  ✅ [静音模式: UIA协同] 已从展开菜单选择「静音发送」")
        return True

    # 若菜单此刻已处于展开态，直接点击「设为静音」避免二次点击箭头反而收起菜单
    opened_menu_box = _locate_template_on_screen("menu_set_silent.png", confidences=(0.84, 0.78))
    if opened_menu_box is not None:
        cx, cy = pyautogui.center(opened_menu_box)
        log(f"  [静音模式: pyautogui-图像匹配兜底] 检测到已展开静音菜单，直接点击 ({cx}, {cy})")
        pyautogui.click(cx, cy)
        sleep_check(0.35)
        log("  ✅ [静音模式: pyautogui-图像匹配兜底] 已选择「静音发送」")
        return True

    arrow_clicked = [False]

    def _step_img_arrow():
        box = _locate_template_on_screen("send_arrow.png")
        if box is not None:
            cx, cy = pyautogui.center(box)
            log(f"  [静音模式: pyautogui-图像匹配兜底] 模板 send_arrow.png 匹配命中 ({cx}, {cy})")
            pyautogui.click(cx, cy)
            arrow_clicked[0] = True
            return True
        pill_box = _locate_template_on_screen("send_pill.png")
        if pill_box is not None:
            cx = int(pill_box.left + pill_box.width * 0.85)
            cy = int(pill_box.top + pill_box.height / 2)
            log(f"  [静音模式: pyautogui-图像匹配兜底] 模板 send_pill.png 匹配命中，点击右端箭头 ({cx}, {cy})")
            pyautogui.click(cx, cy)
            arrow_clicked[0] = True
            return True
        return False

    def _step_img_menu():
        if _try_click_open_silent_menu_uia():
            log("  ✅ [静音模式: UIA协同] 已识别并点击「设为静音」")
            return True
        if _try_click_qui_menu_by_win32():
            return True
        box = _locate_template_on_screen("menu_set_silent.png", confidences=(0.80, 0.70, 0.64))
        if box is None:
            return False
        cx, cy = pyautogui.center(box)
        log(f"  [静音模式: pyautogui-图像匹配兜底] 模板 menu_set_silent.png 匹配命中 ({cx}, {cy})")
        pyautogui.click(cx, cy)
        return True

    try:
        _retry_step("图像匹配下拉箭头", _step_img_arrow, retries=2, interval=1.0)
        sleep_check(0.5)
        _retry_step("图像匹配「设为静音」菜单", _step_img_menu, retries=2, interval=1.0)
    except Exception as exc_img:
        if "手动停止" not in str(exc_img) and arrow_clicked[0]:
            try:
                pyautogui.press("esc")
                time.sleep(0.2)
            except Exception:
                pass
        raise
    sleep_check(0.35)
    log("  ✅ [静音模式: pyautogui-图像匹配兜底] 已选择「静音发送」")
    return True


def _compute_dt_compose_click_points(win_rect, edit_rect=None, input_wnd_rect=None, bottom_bar_rect=None, dpi_scale=1.0):
    """计算钉钉待发送输入框的多候选安全点击坐标与截图区域，严格避开顶部工具栏、消息记录区与底栏按钮。"""
    def _rect_tuple(rc):
        if rc is None:
            return None
        if isinstance(rc, (tuple, list)) and len(rc) == 4:
            return tuple(int(v) for v in rc)
        return (int(rc.left), int(rc.top), int(rc.right), int(rc.bottom))

    wr = _rect_tuple(win_rect)
    if not wr or wr[2] <= wr[0] or wr[3] <= wr[1]:
        return []
    r_l, r_t, r_r, r_b = wr
    W = max(1, r_r - r_l)
    H = max(1, r_b - r_t)
    sc = max(1.0, float(dpi_scale or 1.0))

    top_toolbar_pad = max(34, int(36 * sc))
    safe_min_x = r_l + int(W * 0.25)
    safe_max_x = r_r - max(40, int(42 * sc))
    safe_min_y = r_t + int(H * 0.62)
    safe_max_y = r_b - max(38, int(44 * sc))

    er = _rect_tuple(edit_rect)
    ir = _rect_tuple(input_wnd_rect)
    br = _rect_tuple(bottom_bar_rect)
    if br is not None and br[1] > safe_min_y + 18:
        safe_max_y = min(safe_max_y, br[1] - max(10, int(10 * sc)))

    def _clamp_pt(px, py):
        return (
            max(safe_min_x, min(safe_max_x, int(px))),
            max(safe_min_y, min(max(safe_min_y, safe_max_y), int(py))),
        )

    out = []
    if er is not None and (er[2] - er[0]) >= 80 and (er[3] - er[1]) >= 22:
        ew = er[2] - er[0]
        eh = er[3] - er[1]
        ey_max = (br[1] - 10) if (br is not None and br[1] > er[1] + 20) else (er[3] - 8)
        raw_x = er[0] + int(ew * 0.35)
        raw_y = min(ey_max, er[1] + max(14, int(eh * 0.55)))
        cx, cy = _clamp_pt(raw_x, raw_y)
        patch = (
            max(r_l + int(W * 0.24), er[0] + 4),
            max(safe_min_y, er[1] + 2),
            min(r_r - 12, er[2] - 4),
            min(ey_max, er[3] - 2),
        )
        out.append(("uia_edit_child", cx, cy, patch))

    if ir is not None and br is not None and (ir[2] - ir[0]) >= 100 and br[1] > ir[1] + top_toolbar_pad + 12:
        iw = ir[2] - ir[0]
        top_safe = ir[1] + top_toolbar_pad
        bot_safe = br[1] - max(8, int(8 * sc))
        raw_x = ir[0] + int(iw * 0.35)
        raw_y = max(top_safe + 6, bot_safe - max(16, int(18 * sc)))
        cx, cy = _clamp_pt(raw_x, raw_y)
        patch = (
            max(r_l + int(W * 0.24), ir[0] + 6),
            max(safe_min_y, top_safe),
            min(r_r - 12, ir[2] - 6),
            max(top_safe + 28, bot_safe),
        )
        out.append(("uia_above_bottombar", cx, cy, patch))

    if ir is not None and (ir[2] - ir[0]) >= 100 and (ir[3] - ir[1]) >= 40:
        iw = ir[2] - ir[0]
        top_safe = ir[1] + top_toolbar_pad
        raw_x = ir[0] + int(iw * 0.35)
        raw_y = max(top_safe + 8, ir[3] - max(52, int(52 * sc)))
        if br is not None and br[1] > top_safe + 14:
            raw_y = min(raw_y, br[1] - max(12, int(12 * sc)))
        cx, cy = _clamp_pt(raw_x, raw_y)
        patch = (
            max(r_l + int(W * 0.24), ir[0] + 6),
            max(safe_min_y, top_safe),
            min(r_r - 12, ir[2] - 6),
            min(r_b - 28, ir[3] - 32),
        )
        out.append(("uia_input_wnd_lower", cx, cy, patch))

    default_patch = (r_l + int(W * 0.26), r_t + int(H * 0.72), r_r - int(W * 0.08), r_b - int(H * 0.07))
    out.append(("ratio_bottom_85", int(r_l + W * 0.45), int(r_t + H * 0.85), default_patch))
    out.append(("ratio_bottom_89", int(r_l + W * 0.42), int(r_t + H * 0.89), default_patch))
    out.append(("ratio_bottom_79", int(r_l + W * 0.48), int(r_t + H * 0.79), default_patch))
    return out


def _get_win_gui_caret_info(hwnd):
    """查询目标窗口线程的 Win32 光标与菜单状态（含屏幕坐标转换）。"""
    if os.name != "nt" or not hwnd:
        return {"in_menu": False, "has_caret": False, "caret_screen_pt": None, "hwnd_focus": 0}
    try:
        class GUITHREADINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("hwndActive", wintypes.HWND),
                ("hwndFocus", wintypes.HWND),
                ("hwndCapture", wintypes.HWND),
                ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND),
                ("hwndCaret", wintypes.HWND),
                ("rcCaret", wintypes.RECT),
            ]

        class _PT(ctypes.Structure):
            _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

        u32 = ctypes.windll.user32
        u32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUITHREADINFO)]
        u32.GetGUIThreadInfo.restype = wintypes.BOOL
        u32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(_PT)]
        u32.ClientToScreen.restype = wintypes.BOOL
        tid = u32.GetWindowThreadProcessId(int(hwnd), None)
        if not tid:
            return {"in_menu": False, "has_caret": False, "caret_screen_pt": None, "hwnd_focus": 0}
        gti = GUITHREADINFO()
        gti.cbSize = ctypes.sizeof(GUITHREADINFO)
        if not u32.GetGUIThreadInfo(tid, ctypes.byref(gti)):
            return {"in_menu": False, "has_caret": False, "caret_screen_pt": None, "hwnd_focus": 0}
        in_menu = bool((gti.flags & 0x00000014) or gti.hwndMenuOwner)
        careth = int(gti.hwndCaret or gti.hwndFocus or 0)
        rc = gti.rcCaret
        has_caret = False
        caret_pt = None
        if careth and rc.bottom > rc.top and (rc.bottom - rc.top) >= 6:
            pt = _PT(int((rc.left + rc.right) // 2), int((rc.top + rc.bottom) // 2))
            if u32.ClientToScreen(wintypes.HWND(careth), ctypes.byref(pt)):
                has_caret = True
                caret_pt = (int(pt.x), int(pt.y))
        return {
            "in_menu": in_menu,
            "has_caret": has_caret,
            "caret_screen_pt": caret_pt,
            "hwnd_focus": int(gti.hwndFocus or 0),
        }
    except Exception:
        return {"in_menu": False, "has_caret": False, "caret_screen_pt": None, "hwnd_focus": 0}


def _evaluate_compose_focus_ctrl(fc, r):
    """纯粹判定给定焦点控件 fc 是否位于钉钉底部对话内容输入窗口内（便于单测与生产共用）。"""
    if fc is None:
        return False, "未获取到活动焦点控件"
    try:
        cname = (fc.Name or "").strip()
        nlow = cname.lower()
        ct = (fc.ControlTypeName or "")
        fr = fc.BoundingRectangle
        W = max(1, r.right - r.left)
        H = max(1, r.bottom - r.top)
        if "搜索" in nlow or "search" in nlow:
            return False, f"焦点仍在搜索框（{cname}）"
        if "发送" in nlow or "静音" in nlow or ct in ("ButtonControl", "MenuItemControl", "SplitButtonControl", "MenuControl"):
            return False, f"焦点停留在按钮/菜单（{ct}:{cname}），尚未回到输入窗口"
        if fr.bottom <= fr.top or fr.right <= fr.left:
            return False, f"焦点控件区域无效（{ct}）"
        if fr.bottom < r.top + H * 0.35:
            return False, f"焦点仍位于顶部搜索栏区域（{ct}）"
        if fr.right < r.left + W * 0.24:
            return False, f"焦点仍位于左侧会话/导航栏区域（{ct}）"
        if ct in ("EditControl", "DocumentControl") and fr.bottom >= r.top + H * 0.65:
            return True, f"{ct}（{cname or '对话输入窗口'}）"
        if fr.top >= r.top + H * 0.45 and fr.left >= r.left + W * 0.22 and (fr.right - fr.left) >= 80:
            return True, f"{ct}（对话输入区）"
        if (ct in ("WindowControl", "PaneControl", "CustomControl")
                and fr.bottom >= r.top + H * 0.75 and fr.right >= r.left + W * 0.60):
            return True, f"{ct}（钉钉主窗自绘输入区）"
        return False, f"焦点位于非输入区域（{ct}）"
    except Exception as ex:
        return False, f"读取焦点控件失败（{ex}）"


def _enable_dingtalk_silent_send(silent_order="uia_first"):
    """先校验权限一致性，按配置顺序执行 UIA 定位或图像箭头先行点开+UIA/图像兜底。"""
    priv = check_dingtalk_privilege_match()
    if not priv["matched"]:
        log(f"  ⚠️ {priv['message']}")
    if str(silent_order or "uia_first") == "arrow_img_then_uia":
        try:
            if _enable_dingtalk_silent_send_image():
                return True
        except Exception as e_img:
            if "手动停止" in str(e_img):
                raise
            log(f"  ℹ️ 图像箭头先行未能一步完成（{e_img}），自动切回 UIA 控件树模式…")
        return _enable_dingtalk_silent_send_uia()
    if _enable_dingtalk_silent_send_uia():
        return True
    return _enable_dingtalk_silent_send_image()


def _disable_dingtalk_silent_send():
    """当某时间点设置为常规提醒时，检查并清除钉钉输入框可能残留的「静音发送」状态。"""
    opened_menu = False
    try:
        _app, win, pids = _connect_dingtalk_pywinauto(timeout=4.0)
        win.set_focus()
        if _uia_find_silent_badge(win):
            arrow = _uia_find_arrow_ctrl(win)
            if arrow is not None:
                _uia_click_ctrl(_unwrap_uia(arrow))
                opened_menu = True
                sleep_check(0.45)
                if _uia_find_and_click_silent_menu(win, pids, enable_silent=False):
                    sleep_check(0.3)
                    log("  [静音模式: pywinauto-UIA] 已关闭残留静音状态，改为常规提醒发送")
                    return True
    except Exception as e_ds1:
        if _is_stop_exception(e_ds1):
            raise
    finally:
        if opened_menu:
            try:
                _win32_send_single_vk(0x1B)
            except Exception:
                pass

    try:
        import pyautogui  # type: ignore[import-not-found]
        pyautogui.FAILSAFE = False
        box = _locate_template_on_screen("badge_silent_on.png", confidences=(0.84, 0.78))
        if box is not None:
            cx = int(box.left + box.width * 0.88)
            cy = int(box.top + box.height / 2)
            log(f"  [静音模式: pyautogui-图像匹配兜底] 检测到残留静音标签，点击关闭 ({cx}, {cy})")
            pyautogui.click(cx, cy)
            sleep_check(0.3)
            return True
    except Exception as e_ds2:
        if _is_stop_exception(e_ds2):
            raise

    log("  [静音模式] 本次配置为常规提醒（未开启静音发送）")
    return True


_dt_silent_was_enabled = False


def _reset_residual_dt_silent_if_needed():
    """仅当之前轮次启用过静音或当前窗口可见静音徽标时，清理残留静音态（常规提醒零额外点击）。"""
    global _dt_silent_was_enabled
    try:
        if _dt_silent_was_enabled:
            _disable_dingtalk_silent_send()
            _dt_silent_was_enabled = False
            return True
        _app, win, _pids = _connect_dingtalk_pywinauto(timeout=1.2)
        if win is not None and _uia_find_silent_badge(win):
            _disable_dingtalk_silent_send()
            _dt_silent_was_enabled = False
            return True
    except Exception as e_res:
        if _is_stop_exception(e_res):
            raise
    return False


def _restore_and_activate_hwnd(hwnd, maximize=True, wake_exe_candidates=None):
    """将最小化或隐藏到后台的 Win32/Qt 窗口可靠还原并置前激活，必要时触发单实例唤醒。"""
    import ctypes
    from ctypes import wintypes

    if not hwnd:
        return False

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SendMessageTimeoutW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        wintypes.UINT, wintypes.UINT, ctypes.POINTER(wintypes.LPARAM),
    ]
    user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, wintypes.LPDWORD]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_ulonglong]
    user32.keybd_event.restype = None
    kernel32.GetCurrentThreadId.argtypes = []
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    hwnd = wintypes.HWND(int(hwnd))
    if user32.IsIconic(hwnd) or not user32.IsWindowVisible(hwnd):
        try:
            sm_res = wintypes.LPARAM(0)
            # WM_SYSCOMMAND=0x0112, SC_RESTORE=0xF120, SMTO_ABORTIFHUNG=0x0002
            user32.SendMessageTimeoutW(hwnd, 0x0112, 0xF120, 0, 0x0002, 600, ctypes.byref(sm_res))
        except Exception:
            pass
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        for _ in range(10):
            time.sleep(0.12)
            if not user32.IsIconic(hwnd) and user32.IsWindowVisible(hwnd):
                break

        if user32.IsIconic(hwnd):
            cands = wake_exe_candidates or _resolve_dingtalk_exe_candidates()
            for exe in cands:
                if exe and os.path.exists(exe):
                    try:
                        subprocess.Popen(exe)
                        break
                    except Exception:
                        pass
            for _ in range(10):
                time.sleep(0.12)
                user32.ShowWindow(hwnd, 9)
                if not user32.IsIconic(hwnd):
                    break

    cur = kernel32.GetCurrentThreadId()
    fg = user32.GetForegroundWindow()
    fg_thr = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    attached = False
    try:
        if fg_thr and cur and fg_thr != cur:
            attached = bool(user32.AttachThreadInput(cur, fg_thr, True))
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(cur, fg_thr, False)

    if int(user32.GetForegroundWindow() or 0) != int(hwnd.value):
        try:
            # 轻触 Alt 键释放系统前台锁定限制后再次 SetForegroundWindow
            user32.keybd_event(0x12, 0, 0, 0)
            user32.keybd_event(0x12, 0, 0x0002, 0)
            user32.SetForegroundWindow(hwnd)
        except Exception:
            pass

    if maximize:
        user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE
        time.sleep(0.25)
    else:
        time.sleep(0.12)
    return not bool(user32.IsIconic(hwnd))


def _commit_dingtalk_send(cur_hwnd, send_enter_fn=None, timeout=1.5, input_rect=None):
    """先用原生硬件扫描码发 Enter，并比对发送框画面变化后按需补点右下角「发送(S)」按钮，避免误发或漏发。"""
    from pywinauto import Application, keyboard as _pwa_kbd  # type: ignore[import-not-found]

    hwnd_val = 0
    if isinstance(cur_hwnd, int):
        hwnd_val = int(cur_hwnd)
    elif cur_hwnd is not None:
        try:
            hwnd_val = int(getattr(cur_hwnd, "NativeWindowHandle", 0) or 0)
        except Exception:
            hwnd_val = 0
        if not hwnd_val:
            try:
                hwnd_val = int(getattr(getattr(cur_hwnd, "element_info", None), "handle", 0) or 0)
            except Exception:
                hwnd_val = 0

    before_send_patch = _capture_dt_input_patch(None, input_rect=input_rect) if input_rect is not None else None
    after_send_patch = None
    try:
        if callable(send_enter_fn):
            send_enter_fn()
        else:
            if not _win32_send_single_vk(0x0D):
                _pwa_kbd.send_keys("{ENTER}")
        sleep_check(0.50)

        if before_send_patch is not None and input_rect is not None:
            after_send_patch = _capture_dt_input_patch(None, input_rect=input_rect)
            if _did_dt_input_change(before_send_patch, after_send_patch):
                return False
    finally:
        for _p_img in (before_send_patch, after_send_patch):
            if _p_img is not None and hasattr(_p_img, "close"):
                try:
                    _p_img.close()
                except Exception:
                    pass

    if not hwnd_val:
        return False

    try:
        s_app = Application(backend="uia").connect(handle=hwnd_val, timeout=timeout)
        s_win = s_app.window(handle=hwnd_val)
        s_wrap = _unwrap_uia(s_win)
        s_rect = s_wrap.rectangle()
        w_w = max(1, s_rect.width())
        w_h = max(1, s_rect.height())

        send_btn = None
        for ctrl, _d in _iter_dt_uia_pruned(s_wrap, max_depth=24):
            try:
                ii = ctrl.element_info
                aid_l = (ii.automation_id or "").lower()
                if aid_l.endswith(".leftcontainerwidget"):
                    for ch, _ in _iter_dt_uia_pruned(ctrl, max_depth=5):
                        try:
                            ci = ch.element_info
                            cnm = (ci.name or "").strip()
                            if ((ci.control_type or "") == "Button" or cnm.startswith("发送")) and ch.is_enabled():
                                send_btn = ch
                                break
                        except Exception:
                            pass
                    if send_btn is not None:
                        break
                nm = (ii.name or "").strip()
                rr = ctrl.rectangle()
                if (
                    (ii.control_type or "") == "Button"
                    and rr.width() >= 24
                    and rr.height() >= 18
                    and rr.top >= s_rect.top + int(w_h * 0.55)
                    and rr.left >= s_rect.left + int(w_w * 0.40)
                    and (nm.startswith("发送") or nm.lower() == "send" or aid_l.endswith(".sendbutton"))
                    and "更多" not in nm
                    and "submenubutton" not in aid_l
                    and ctrl.is_enabled()
                ):
                    send_btn = ctrl
            except Exception:
                pass

        if send_btn is not None and send_btn.is_enabled():
            _uia_click_ctrl(send_btn)
            sleep_check(0.35)
            try:
                if send_enter_fn is None and send_btn.is_enabled():
                    if not _win32_send_ctrl_key(0x0D):
                        _pwa_kbd.send_keys("^{ENTER}")
                    sleep_check(0.25)
            except Exception as e_ce:
                if _is_stop_exception(e_ce):
                    raise
            return True
    except Exception as e_btn:
        if "手动停止" in str(e_btn):
            raise
    return False


def _capture_dt_input_patch(r, input_rect=None):
    """优先截取真实待发送输入区局部灰度图（未知输入区时再按窗口右下比例兜底），核对粘贴/发送前后画面变化。"""
    grab_im = None
    try:
        from PIL import ImageGrab
        if isinstance(input_rect, (tuple, list)) and len(input_rect) == 4:
            x1, y1, x2, y2 = [int(v) for v in input_rect]
            if x2 - x1 >= 40 and y2 - y1 >= 24:
                grab_im = ImageGrab.grab(bbox=(x1, y1, x2, y2), all_screens=True)
                return grab_im.convert("L") if grab_im is not None else None
        if r is None:
            return None
        left = int(r.left)
        top = int(r.top)
        right = int(r.right)
        bottom = int(r.bottom)
        W = max(1, right - left)
        H = max(1, bottom - top)
        x1 = left + int(W * 0.25)
        y1 = top + int(H * 0.72)
        x2 = right - int(W * 0.08)
        y2 = bottom - int(H * 0.07)
        if x2 - x1 < 40 or y2 - y1 < 24:
            return None
        grab_im = ImageGrab.grab(bbox=(x1, y1, x2, y2), all_screens=True)
        return grab_im.convert("L") if grab_im is not None else None
    except Exception:
        return None
    finally:
        if grab_im is not None:
            try:
                grab_im.close()
            except Exception:
                pass


def _did_dt_input_change(before_img, after_img):
    """比对粘贴前后输入区像素差值，判断图片是否真实粘贴进聊天框（无图捕获环境放行）。"""
    if before_img is None or after_img is None or before_img.size != after_img.size:
        return True
    diff = None
    try:
        from PIL import ImageChops
        diff = ImageChops.difference(before_img, after_img)
        w, h = diff.size
        hist = diff.histogram()
        changed = sum(hist[16:])
        return changed >= max(35, int(w * h * 0.0010))
    except Exception:
        return True
    finally:
        if diff is not None:
            try:
                diff.close()
            except Exception:
                pass


def _collect_dingtalk_search_rows(doc_list, check_stop_fn=None):
    """带严格超时读取钉钉全局搜索弹窗内的候选行文本，过滤历史记录/文档/聊天记录干扰项。"""
    import unicodedata

    def _norm(s):
        return "".join(ch for ch in unicodedata.normalize("NFKC", str(s or "")).casefold() if ch.isalnum())

    items = []
    empty_flag = False
    hist_rect = None
    common_rect = None
    noise_heads = []
    good_heads = []
    deadline = time.monotonic() + 0.95
    visited = [0]

    def _walk(ctrl, depth):
        nonlocal empty_flag, hist_rect, common_rect
        if depth > 10 or visited[0] >= 190 or time.monotonic() > deadline:
            return
        for k in ctrl.GetChildren():
            visited[0] += 1
            if visited[0] % 20 == 0 and check_stop_fn is not None:
                check_stop_fn()
            if visited[0] > 190 or time.monotonic() > deadline:
                return
            try:
                ct = k.ControlTypeName
                nm = (k.Name or "").strip()
                rr = k.BoundingRectangle
                ok_rect = rr.right > rr.left and rr.bottom > rr.top
                if nm and any(e_k in nm for e_k in ("无搜索结果", "未找到相关", "没有找到", "暂无相关结果")):
                    empty_flag = True
                if ok_rect and nm:
                    if ct == "TextControl":
                        if nm == "搜索历史" and hist_rect is None:
                            hist_rect = rr
                        elif nm == "常用" and common_rect is None:
                            common_rect = rr
                        if nm in ("聊天记录", "文档", "云文档", "钉钉文档", "网络查找", "搜索网络", "待办", "邮件"):
                            noise_heads.append((nm, rr))
                        elif nm in ("联系人", "群组", "常用", "功能"):
                            good_heads.append((nm, rr))
                    if ct in ("TextControl", "ButtonControl", "ListItemControl", "HyperlinkControl"):
                        items.append((k, nm, rr))
            except Exception:
                pass
            _walk(k, depth + 1)

    for doc in (doc_list or []):
        if time.monotonic() > deadline:
            break
        _walk(doc, 0)

    def _in_hist(r):
        return (
            hist_rect is not None
            and r.top >= hist_rect.top - 4
            and (
                (common_rect is not None and r.top < common_rect.top - 4)
                or (common_rect is None and r.top < hist_rect.bottom + 90)
            )
        )

    filtered = []
    for k, nm, rr in items:
        if _in_hist(rr):
            continue
        if nm in ("综合", "联系人", "群组", "聊天记录", "文档", "云文档", "搜索历史", "常用", "功能", "内部群", "部门群", "更多"):
            continue
        if _norm(nm) and not _norm(nm).isdigit():
            filtered.append((k, nm, rr))

    rows = []
    for k, nm, rr in sorted(filtered, key=lambda x: ((x[2].top + x[2].bottom) / 2, x[2].left)):
        cy = (rr.top + rr.bottom) / 2
        placed = False
        for row in rows:
            if abs(cy - row[0]) <= 26:
                row[1].append((k, nm, rr))
                row[0] = (row[0] * (len(row[1]) - 1) + cy) / len(row[1])
                placed = True
                break
        if not placed:
            rows.append([cy, [(k, nm, rr)]])

    all_section_heads = []
    for lbl, r in good_heads:
        all_section_heads.append((r.top, True, lbl, r))
    for lbl, r in noise_heads:
        all_section_heads.append((r.top, False, lbl, r))
    all_section_heads.sort(key=lambda x: x[0])

    out_rows = []
    for cy, row_items in rows:
        row_items.sort(key=lambda x: x[2].left)
        nearest_head_above = None
        for h_top, is_good, h_lbl, hr in all_section_heads:
            if h_top < cy and abs(hr.left - row_items[0][2].left) < 340:
                nearest_head_above = (is_good, h_lbl)
        if nearest_head_above is not None and not nearest_head_above[0]:
            continue
        title = " ".join(nm for _k, nm, _r in row_items[:4]).strip()
        if not title or len(title) > 80 or _is_chat_sentence_noise(title):
            continue
        best_item = max(row_items, key=lambda x: x[2].right - x[2].left)
        sec = nearest_head_above[1] if (nearest_head_above and nearest_head_above[0]) else ""
        out_rows.append({"title": title, "section": sec, "ctrl": best_item[0], "rect": best_item[2]})

    return {"empty_result": bool(empty_flag and not out_rows), "rows": out_rows}


def _ai_pick_best_search_row(target_name, candidates, rows, cfg=None):
    """由本地多维打分与内置 AI 共同确认钉钉下拉搜索框里最贴合目标名称的那一条会话。"""
    import difflib
    import unicodedata

    if not rows:
        return None

    def _norm(s):
        return "".join(ch for ch in unicodedata.normalize("NFKC", str(s or "")).casefold() if ch.isalnum())

    cand_norms = [_norm(c) for c in ([target_name] + list(candidates or [])) if _norm(c)]
    t_norm = _norm(target_name)
    scored = []
    for idx, row in enumerate(rows):
        r_title = str(row.get("title") or "")
        r_base = re.sub(r"[（(][^）)]*[）)]", "", r_title).strip()
        r_norm = _norm(r_title)
        r_base_norm = _norm(r_base)
        sec = str(row.get("section") or "")
        if not r_norm:
            continue
        sc = 0.0
        if t_norm and (r_norm == t_norm or r_base_norm == t_norm):
            sc = 100.0
        else:
            for ci, cn in enumerate(cand_norms):
                bonus = max(0, 10 - ci * 2)
                if cn == r_norm or cn == r_base_norm:
                    sc = max(sc, 98.0 + bonus)
                elif r_base_norm.startswith(cn) or r_norm.startswith(cn):
                    sc = max(sc, 88.0 + bonus * 0.5)
                elif cn in r_norm or r_norm in cn:
                    sc = max(sc, 78.0 + bonus * 0.5)
                else:
                    sc = max(sc, difflib.SequenceMatcher(None, cn, r_base_norm or r_norm).ratio() * 72.0)
        if sec in ("联系人", "群组", "常用"):
            sc += 18.0
            if any(k in r_title for k in ("我自己", "（自己）", "(自己)")):
                sc += 35.0
        elif sec in ("文档", "云文档", "钉钉文档", "聊天记录", "待办", "邮件", "网络查找", "搜索网络"):
            sc -= 60.0
        elif sec == "功能" and "助手" not in str(target_name or ""):
            sc -= 40.0
        if "助手" not in str(target_name or "") and any(k in r_title for k in ("文件小助手", "文件传输助手")):
            sc -= 120.0
        if _is_chat_sentence_noise(r_title):
            sc -= 85.0
        if re.search(r"\.(?:xlsx?|docx?|pptx?|pdf|csv|txt|png|jpe?g|zip)$", r_title.strip(), re.I):
            sc -= 40.0
        scored.append((sc, idx, row))

    if not scored:
        return None
    scored.sort(key=lambda x: -x[0])
    top_sc, _top_i, top_row = scored[0]
    if top_sc >= 78.0:
        return top_row

    # 若分值接近或仅为模糊命中，尝试让内置 AI 在当前可见前 6 条真实结果里确认最符合原意的一项
    if cfg is not None:
        norm_c = normalize_config(cfg)
        ai_cfg = norm_c.get("ai_assistant") or {}
        if ai_cfg.get("enabled", True) and str(ai_cfg.get("provider") or "gemini") != "local_only":
            try:
                opts = [{"idx": i, "title": r["title"], "section": r["section"]} for i, r in enumerate(rows[:6])]
                g_res = _call_gemini_generate(
                    norm_c,
                    "你在帮用户在钉钉全局搜索结果中选择唯一一对一匹配的收信人或群聊。严禁把文件小助手/文件传输助手匹配给普通人名。只返回JSON：{\"pick\": 序号整数或-1}",
                    [{"role": "user", "parts": [{"text": f"用户想找：{target_name}（候选词：{candidates[:4]}）\n搜索到的结果：{json.dumps(opts, ensure_ascii=False)}"}]}],
                    temperature=0.05,
                    max_tokens=60,
                    timeout_sec=5,
                )
                if g_res.get("ok"):
                    m_j = re.search(r"\"pick\"\s*:\s*(-?\d+)", str(g_res.get("reply") or ""))
                    if m_j:
                        pk = int(m_j.group(1))
                        if 0 <= pk < len(rows):
                            picked_r = rows[pk]
                            if "助手" not in str(target_name or "") and any(k in str(picked_r.get("title") or "") for k in ("文件小助手", "文件传输助手")):
                                return None
                            return picked_r
            except Exception:
                pass
    return top_row if top_sc >= 72.0 else None


_DT_FORBIDDEN_VISION_SECTIONS = ("聊天记录", "文档", "云文档", "钉钉文档", "待办", "邮件", "网络查找", "搜索网络")


def _extract_rect_tuple(rect):
    if rect is None:
        return None
    if isinstance(rect, (tuple, list)) and len(rect) == 4:
        try:
            l, t, r, b = [int(v) for v in rect]
            return (l, t, r, b) if (r > l and b > t) else None
        except Exception:
            return None
    try:
        l, t, r, b = int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
        return (l, t, r, b) if (r > l and b > t) else None
    except Exception:
        return None


def _grab_window_region_b64(rect, crop_frac=None, max_dim=1280, quality=86):
    """截取桌面指定窗口/弹窗区域并缩放编码为 JPEG Base64，供 AI 桌面视觉识屏核对与本人看图确认使用。"""
    import base64
    base_box = _extract_rect_tuple(rect)
    if base_box is None:
        return None
    left, top, right, bottom = base_box
    w0 = right - left
    h0 = bottom - top
    if w0 < 30 or h0 < 30:
        return None
    if isinstance(crop_frac, (tuple, list)) and len(crop_frac) == 4:
        fx1, fy1, fx2, fy2 = [max(0.0, min(1.0, float(v))) for v in crop_frac]
        x1 = int(left + w0 * fx1)
        y1 = int(top + h0 * fy1)
        x2 = int(left + w0 * fx2)
        y2 = int(top + h0 * fy2)
    else:
        x1, y1, x2, y2 = left, top, right, bottom
    if (x2 - x1) < 30 or (y2 - y1) < 30:
        return None
    if os.name != "nt":
        return None
    grab_im = None
    im = None
    im_resized = None
    buf = None
    try:
        from PIL import ImageGrab
        grab_im = ImageGrab.grab(bbox=(x1, y1, x2, y2), all_screens=True)
        if grab_im is None:
            return None
        im = grab_im.convert("RGB")
        target_im = im
        if max(im.width, im.height) > max_dim:
            ratio = float(max_dim) / float(max(im.width, im.height))
            im_resized = im.resize(
                (max(1, int(im.width * ratio)), max(1, int(im.height * ratio))),
                Image.Resampling.LANCZOS,
            )
            target_im = im_resized
        buf = io.BytesIO()
        q_val = max(30, min(95, int(quality)))
        target_im.save(buf, format="JPEG", quality=q_val, optimize=True)
        b64_s = base64.b64encode(buf.getvalue()).decode("ascii")
        return {
            "mime_type": "image/jpeg",
            "b64": b64_s,
            "bbox": (x1, y1, x2, y2),
            "w": target_im.width,
            "h": target_im.height,
        }
    except Exception:
        return None
    finally:
        for res_obj in (buf, im_resized, im, grab_im):
            if res_obj is not None:
                try:
                    res_obj.close()
                except Exception:
                    pass


def _capture_dingtalk_confirm_screenshot(ding_obj=None, popup_ctl=None, max_dim=760, quality=68, max_b64_chars=96000):
    """抓取钉钉主窗口或搜索框轻量高清 JPEG Data URL，用于在 AI 对话框及手机端向本人展示现场画面。"""
    rects = []
    for ctl_obj in (ding_obj, popup_ctl):
        if ctl_obj is None:
            continue
        try:
            rc = getattr(ctl_obj, "BoundingRectangle", ctl_obj)
            if _extract_rect_tuple(rc) is not None:
                rects.append(rc)
        except Exception:
            pass
    if not rects:
        return ""
    for rc in rects:
        for dim_try, q_try in ((max_dim, quality), (640, 56), (500, 44)):
            shot = _grab_window_region_b64(rc, max_dim=dim_try, quality=q_try)
            if isinstance(shot, dict) and shot.get("b64"):
                data_url = f"data:{shot.get('mime_type') or 'image/jpeg'};base64,{shot['b64']}"
                if len(data_url) <= max_b64_chars:
                    return data_url
    return ""


def _parse_first_json_dict(raw_txt):
    s = str(raw_txt or "").strip()
    if not s:
        return None
    dec = json.JSONDecoder()
    idx = 0
    while idx < len(s):
        pos = s.find("{", idx)
        if pos < 0:
            break
        try:
            obj, _end = dec.raw_decode(s, pos)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
        idx = pos + 1
    return None


def _ai_vision_match_dingtalk_search(target_name, search_kw, shot_info, uia_rows=None, cfg=None):
    """由多模态视觉 AI 直接看钉钉搜索弹窗桌面截图，确认屏幕搜出的联系人/群名是否与目标一致。"""
    tgt = str(target_name or "").strip()
    kw_s = str(search_kw or "").strip()
    rows_list = list(uia_rows or [])
    norm_c = normalize_config(cfg if isinstance(cfg, dict) else {})
    ai_cfg = norm_c.get("ai_assistant") or {}
    can_vision = bool(
        isinstance(shot_info, dict)
        and shot_info.get("b64")
        and ai_cfg.get("enabled", True)
        and str(ai_cfg.get("provider") or "gemini") != "local_only"
    )
    allow_assistant = ("助手" in tgt)

    if can_vision:
        sys_p = (
            "你是电脑桌面端钉钉全局搜索界面视觉核对专家。请查看截图中的钉钉搜索弹窗：\n"
            "1. 读取搜索框里实际输入的文字；\n"
            "2. 查看下方搜索结果（重点区分「联系人」「群组」「常用」与「聊天记录」「文档」「云文档」「功能」）；\n"
            "3. 判断是否存在与用户目标对象一对一匹配的真实联系人或群聊；\n"
            "4. 严禁把「文件小助手」「文件传输助手」匹配给普通人名或群名；严禁把「聊天记录」「文档」「待办」「邮件」「网络查找」当作收信人；\n"
            "5. 若存在准确匹配项，估算该条目文字区域在当前截图内的可点击中心坐标比例 x_pct（0.0~1.0从左到右）、y_pct（0.0~1.0从上到下）；\n"
            "6. 把屏幕上实际看得到的其它「联系人/群组/常用」候选名称放到 candidates 列表。\n"
            "只输出一段严格 JSON，不要额外解释：\n"
            '{"search_box_text":"","found":false,"same_as_target":false,"matched_name":"","section":"","x_pct":0.0,"y_pct":0.0,"candidates":[],"reason":""}'
        )
        clues = [
            {"title": str(r.get("title") or ""), "section": str(r.get("section") or "")}
            for r in rows_list[:6]
            if isinstance(r, dict)
        ]
        user_txt = (
            f"用户想发送的目标对象：{tgt}\n"
            f"本次在钉钉搜索框输入的词：{kw_s}\n"
            + (f"辅助控件文本：{json.dumps(clues, ensure_ascii=False)}\n" if clues else "")
            + "请仔细核对截图并输出JSON。"
        )
        parts = [
            {"text": user_txt},
            {"inlineData": {"mimeType": shot_info.get("mime_type") or "image/jpeg", "data": shot_info["b64"]}},
        ]
        try:
            g_res = _call_gemini_generate(
                norm_c,
                sys_p,
                [{"role": "user", "parts": parts}],
                temperature=0.05,
                max_tokens=360,
                timeout_sec=10,
                max_models=2,
                total_budget_sec=14,
            )
            if g_res.get("ok"):
                obj = _parse_first_json_dict(g_res.get("reply"))
                if obj is not None:
                    matched_name = str(obj.get("matched_name") or "").strip()
                    section = str(obj.get("section") or "").strip()
                    found = bool(obj.get("found"))
                    same = bool(obj.get("same_as_target"))
                    reason = str(obj.get("reason") or "").strip()
                    box_text = str(obj.get("search_box_text") or "").strip()
                    raw_cands = obj.get("candidates") if isinstance(obj.get("candidates"), list) else []
                    cands = []
                    for rc in raw_cands:
                        cs = str(rc or "").strip()
                        if not cs or _is_chat_sentence_noise(cs):
                            continue
                        if not allow_assistant and any(ak in cs for ak in ("文件小助手", "文件传输助手")):
                            continue
                        if cs not in cands:
                            cands.append(cs)
                        if len(cands) >= 6:
                            break
                    if not allow_assistant and any(ak in matched_name for ak in ("文件小助手", "文件传输助手")):
                        found = False
                        same = False
                        reason = f"安全拦截：画面命中「{matched_name}」，禁止匹配给「{tgt}」"
                    elif section and any(bs in section for bs in _DT_FORBIDDEN_VISION_SECTIONS):
                        found = False
                        same = False
                        reason = f"已拦截非会话分区「{section}」：{matched_name or reason}"
                    xp, yp = None, None
                    try:
                        xv = float(obj.get("x_pct"))
                        yv = float(obj.get("y_pct"))
                        if 0.02 <= xv <= 0.98 and 0.02 <= yv <= 0.98:
                            xp, yp = round(xv, 4), round(yv, 4)
                    except Exception:
                        pass
                    return {
                        "used_vision": True,
                        "search_box_text": box_text or kw_s,
                        "found": found,
                        "same_as_target": same,
                        "matched_name": matched_name,
                        "section": section,
                        "x_pct": xp,
                        "y_pct": yp,
                        "candidates": cands,
                        "reason": reason or ("画面核对一致" if (found and same) else "钉钉搜索结果未见一致匹配"),
                    }
        except Exception:
            pass

    # 离线或未配置视觉通道时安全降级到本地 UIA 结果打分（不瞎猜）
    picked = _ai_pick_best_search_row(tgt, [kw_s] if kw_s else [], rows_list, cfg=None)
    fallback_cands = []
    for r in rows_list[:6]:
        rt = str(r.get("title") or "").strip()
        rsec = str(r.get("section") or "").strip()
        if not rt or any(bs in rsec for bs in _DT_FORBIDDEN_VISION_SECTIONS):
            continue
        if not allow_assistant and any(ak in rt for ak in ("文件小助手", "文件传输助手")):
            continue
        if rt not in fallback_cands:
            fallback_cands.append(rt)

    if picked is not None:
        xp, yp = None, None
        bbox = shot_info.get("bbox") if isinstance(shot_info, dict) else None
        pr = _extract_rect_tuple(picked.get("rect"))
        if bbox and pr and (bbox[2] > bbox[0]) and (bbox[3] > bbox[1]):
            cx = (pr[0] + pr[2]) / 2.0
            cy = (pr[1] + pr[3]) / 2.0
            xv = (cx - bbox[0]) / float(bbox[2] - bbox[0])
            yv = (cy - bbox[1]) / float(bbox[3] - bbox[1])
            if 0.02 <= xv <= 0.98 and 0.02 <= yv <= 0.98:
                xp, yp = round(xv, 4), round(yv, 4)
        p_title = str(picked.get("title") or "").strip()
        return {
            "used_vision": False,
            "search_box_text": kw_s,
            "found": True,
            "same_as_target": True,
            "matched_name": p_title,
            "section": str(picked.get("section") or ""),
            "x_pct": xp,
            "y_pct": yp,
            "candidates": fallback_cands,
            "reason": f"离线控件核对一致：「{p_title}」",
        }
    return {
        "used_vision": False,
        "search_box_text": kw_s,
        "found": False,
        "same_as_target": False,
        "matched_name": "",
        "section": "",
        "x_pct": None,
        "y_pct": None,
        "candidates": fallback_cands,
        "reason": "未在钉钉下拉列表检测到与目标一致的会话" + ("（当前为离线模式）" if not can_vision else "（视觉返回异常已安全拦截）"),
    }


def _ai_vision_verify_opened_chat(target_name, ding_rect, cfg=None, shot_info=None):
    """粘贴前让 AI 看钉钉聊天窗顶部标题栏，二次核对打开的是不是用户想要发送的对象。"""
    tgt = str(target_name or "").strip()
    if shot_info is None:
        shot_info = _grab_window_region_b64(ding_rect, crop_frac=(0.18, 0.0, 0.92, 0.16), max_dim=960)
    norm_c = normalize_config(cfg if isinstance(cfg, dict) else {})
    ai_cfg = norm_c.get("ai_assistant") or {}
    if not (
        isinstance(shot_info, dict)
        and shot_info.get("b64")
        and ai_cfg.get("enabled", True)
        and str(ai_cfg.get("provider") or "gemini") != "local_only"
    ):
        return {"verified": None, "same_as_target": None, "opened_name": "", "reason": "离线或未截取到顶部标题栏"}

    sys_p = (
        "你在核对电脑端钉钉当前打开的聊天窗口顶部标题栏。\n"
        "请读取截图顶部聊天窗标题显示的联系人或群聊名称，判断它与用户目标是不是同一个发送对象"
        "（允许忽略表情符号、群体人数括注如(25)；但若明显是另一个人、另一个群、或文件小助手/文件传输助手，same_as_target 必须为 false）。\n"
        '只返回严格 JSON：{"opened_name":"","same_as_target":true,"reason":""}'
    )
    parts = [
        {"text": f"目标发送对象：{tgt}\n请核对当前打开会话顶部标题栏并输出JSON。"},
        {"inlineData": {"mimeType": shot_info.get("mime_type") or "image/jpeg", "data": shot_info["b64"]}},
    ]
    try:
        g_res = _call_gemini_generate(
            norm_c,
            sys_p,
            [{"role": "user", "parts": parts}],
            temperature=0.05,
            max_tokens=180,
            timeout_sec=8,
            max_models=2,
            total_budget_sec=11,
        )
        if g_res.get("ok"):
            obj = _parse_first_json_dict(g_res.get("reply"))
            if obj is not None:
                opened = str(obj.get("opened_name") or "").strip()
                same = bool(obj.get("same_as_target"))
                reason = str(obj.get("reason") or "").strip()
                if "助手" not in tgt and any(ak in opened for ak in ("文件小助手", "文件传输助手")):
                    same = False
                    reason = f"安全拦截：当前打开会话是「{opened}」，不是目标「{tgt}」"
                return {"verified": True, "same_as_target": same, "opened_name": opened, "reason": reason}
    except Exception:
        pass
    return {"verified": False, "same_as_target": None, "opened_name": "", "reason": "顶部视觉复核请求未返回有效结果"}


def _dt_open_global_search():
    """在当前置前钉钉主窗按 Ctrl+Shift+F 打开全局搜索框。"""
    if os.name != "nt":
        return False
    import ctypes
    user32 = ctypes.windll.user32
    _KEYUP = 0x0002
    _VK_CTRL, _VK_SHIFT, _VK_F = 0x11, 0x10, 0x46
    user32.keybd_event(_VK_CTRL, 0, 0, 0)
    user32.keybd_event(_VK_SHIFT, 0, 0, 0)
    user32.keybd_event(_VK_F, 0, 0, 0)
    time.sleep(0.12)
    user32.keybd_event(_VK_F, 0, _KEYUP, 0)
    user32.keybd_event(_VK_SHIFT, 0, _KEYUP, 0)
    user32.keybd_event(_VK_CTRL, 0, _KEYUP, 0)
    return True


def _dt_search_controls(ua_mod):
    out = []
    try:
        for h, c, _t in _enum_top_windows():
            if "ToolSaveBits" in c:
                ctl = _safe_control_from_handle(ua_mod, h)
                if ctl is not None:
                    out.append((h, ctl))
    except Exception:
        pass
    return out


def _dt_search_doc(ua_mod):
    for _h, ctl in _dt_search_controls(ua_mod):
        try:
            d = ctl.DocumentControl(searchDepth=12, Name="advancedSearch")
            if d.Exists(0.2):
                return ctl, d
        except Exception:
            pass
    return None, None


def _dt_iter_search_docs(ua_mod, doc):
    documents = []
    for _h, ctl in _dt_search_controls(ua_mod):
        try:
            d = ctl.DocumentControl(searchDepth=12, Name="advancedSearch")
            if d.Exists(0.05):
                documents.append(d)
        except Exception:
            pass
    return documents or ([doc] if doc is not None else [])


def _dt_focus_search_input(ua_mod, popup_ctl, doc):
    try:
        if popup_ctl is not None:
            ed = popup_ctl.EditControl(searchDepth=12)
            if ed.Exists(0.15):
                er = ed.BoundingRectangle
                if er.right - er.left > 40 and er.bottom - er.top > 12:
                    ua_mod.Click(int((er.left + er.right) / 2), int((er.top + er.bottom) / 2), waitTime=0.15)
                    return
    except Exception:
        pass
    if doc is None:
        return
    try:
        doc_rect = doc.BoundingRectangle
    except Exception:
        return
    tab_rect = None
    try:
        def _find_tab(ctrl, depth=0):
            nonlocal tab_rect
            if tab_rect is not None or depth > 12:
                return
            for ch in ctrl.GetChildren():
                try:
                    if ch.ControlTypeName == "TextControl" and (ch.Name or "").strip() == "综合":
                        tab_rect = ch.BoundingRectangle
                        return
                except Exception:
                    pass
                _find_tab(ch, depth + 1)
        _find_tab(doc, 0)
    except Exception:
        pass
    if tab_rect is not None and tab_rect.top - doc_rect.top > 12:
        fx = int(doc_rect.left + (doc_rect.right - doc_rect.left) * 0.35)
        fy = int((doc_rect.top + tab_rect.top) / 2)
    else:
        fx = int(doc_rect.left + (doc_rect.right - doc_rect.left) * 0.30)
        fy = int(doc_rect.top + max(16, (doc_rect.bottom - doc_rect.top) * 0.035))
    ua_mod.Click(fx, fy, waitTime=0.15)


def _dt_paste_text_verified(ua_mod, text):
    ready = False
    for _ in range(5):
        set_clipboard_windows(text)
        time.sleep(0.14)
        try:
            if get_clipboard_text_windows().strip() == str(text or "").strip():
                ready = True
                break
        except Exception:
            pass
        time.sleep(0.14)
    if not ready:
        return False
    time.sleep(0.12)
    if not _win32_send_ctrl_key(ord("V")):
        try:
            ua_mod.SendKeys("{Ctrl}v", waitTime=0.25)
        except Exception:
            return False
    time.sleep(0.22)
    return True


def verify_dingtalk_target_on_desktop(target_name, cfg=None, close_search_after=True):
    """输入阶段即时检验：唤醒钉钉搜索并在桌面截屏交由 AI 核对搜出来的对象是否与想发的一致（绝不点击发送）。"""
    import contextlib
    raw_name = str(target_name or "").strip()
    if not raw_name:
        return {"ok": False, "matched": False, "message": "请先输入钉钉群名或联系人名称"}
    if os.name != "nt":
        return {"ok": False, "matched": False, "message": "仅 Windows 桌面端支持在钉钉中识屏核对"}
    try:
        import uiautomation as ua  # type: ignore[import-not-found]
    except Exception:
        return {"ok": False, "matched": False, "message": "桌面识别组件 uiautomation 未安装"}

    norm_c = normalize_config(cfg if isinstance(cfg, dict) else {})
    old_clip = ""
    try:
        old_clip = get_clipboard_text_windows()
    except Exception:
        old_clip = ""

    init_ctx = ua.UIAutomationInitializerInThread() if hasattr(ua, "UIAutomationInitializerInThread") else contextlib.nullcontext()
    try:
        with init_ctx:
            im_wins = _enum_dingtalk_im_windows(include_hidden=True)
            if not im_wins:
                return {"ok": False, "matched": False, "message": "未检测到已启动的钉钉窗口，请先打开并登录钉钉"}
            hwnd0 = int(im_wins[0][0])
            _restore_and_activate_hwnd(hwnd0, maximize=True, wake_exe_candidates=_resolve_dingtalk_exe_candidates())
            time.sleep(0.45)
            ding = _safe_control_from_handle(ua, hwnd0)
            if ding is None or _is_dingtalk_login_window(ding):
                return {"ok": False, "matched": False, "message": "请先扫码登录钉钉后再点击 AI 识屏核对"}

            # 组装去重后的高命中候选检索词（最多试前 4 个）
            verified_map = _load_verified_dingtalk_targets()
            sm_res = resolve_smart_dingtalk_target(raw_name, cfg=norm_c, use_ai=False)
            kw_list, _toks = _extract_search_keywords(raw_name)
            cands = []
            for item in (
                [verified_map.get(raw_name, "")]
                + [raw_name]
                + list((sm_res.get("candidates") or []) if isinstance(sm_res, dict) else [])
                + list(kw_list or [])
            ):
                s = str(item or "").strip()
                if s and s not in cands:
                    cands.append(s)
            cands = cands[:4] or [raw_name]

            _dt_open_global_search()
            opened = False
            for _ in range(10):
                if _is_search_popup_open():
                    opened = True
                    break
                time.sleep(0.16)
            popup_ctl, doc = _dt_search_doc(ua)
            if not opened and doc is None:
                return {"ok": False, "matched": False, "message": "未能唤起钉钉全局搜索窗，请保持钉钉在前台后再试"}

            all_cands = []
            last_vis = {}
            last_kw = cands[0]
            preview_img = ""
            try:
                for kw_idx, kw in enumerate(cands):
                    last_kw = kw
                    if kw_idx > 0 and not _is_search_popup_open():
                        _dt_open_global_search()
                        for _ in range(10):
                            if _is_search_popup_open():
                                break
                            time.sleep(0.16)
                    popup_now, doc_now = _dt_search_doc(ua)
                    _dt_focus_search_input(ua, popup_now, doc_now)
                    time.sleep(0.10)
                    ua.SendKeys("{Ctrl}a", waitTime=0.05)
                    ua.SendKeys("{Back}", waitTime=0.05)
                    if not _dt_paste_text_verified(ua, kw):
                        continue
                    time.sleep(0.85)

                    popup_now, doc_now = _dt_search_doc(ua)
                    uia_rows = []
                    if doc_now is not None:
                        scan_r = _collect_dingtalk_search_rows(_dt_iter_search_docs(ua, doc_now), check_stop_fn=None)
                        uia_rows = list(scan_r.get("rows") or [])

                    shot_v = None
                    if popup_now is not None:
                        try:
                            pr = popup_now.BoundingRectangle
                            if (pr.right - pr.left) >= 150 and (pr.bottom - pr.top) >= 150:
                                shot_v = _grab_window_region_b64(pr, max_dim=1100)
                        except Exception:
                            shot_v = None
                    if shot_v is None:
                        try:
                            shot_v = _grab_window_region_b64(ding.BoundingRectangle, crop_frac=(0.0, 0.0, 0.68, 0.76), max_dim=1200)
                        except Exception:
                            shot_v = None

                    vis = _ai_vision_match_dingtalk_search(raw_name, kw, shot_v, uia_rows=uia_rows, cfg=norm_c)
                    last_vis = vis
                    for c_item in (vis.get("candidates") or []):
                        cs = str(c_item or "").strip()
                        if cs and cs not in all_cands:
                            all_cands.append(cs)
                    if vis.get("found") and vis.get("same_as_target"):
                        m_name = str(vis.get("matched_name") or raw_name).strip()
                        if m_name:
                            _save_verified_dingtalk_target(raw_name, m_name)
                        if m_name and m_name not in all_cands:
                            all_cands.insert(0, m_name)
                        return {
                            "ok": True,
                            "matched": True,
                            "input_name": raw_name,
                            "matched_name": m_name,
                            "section": vis.get("section") or "",
                            "used_keyword": kw,
                            "search_box_text": vis.get("search_box_text") or kw,
                            "candidates": all_cands[:6],
                            "used_vision": bool(vis.get("used_vision")),
                            "reason": vis.get("reason") or "桌面画面核对一致",
                        }
                preview_img = _capture_dingtalk_confirm_screenshot(ding_obj=ding, popup_ctl=popup_ctl)
            finally:
                if close_search_after:
                    try:
                        if _is_search_popup_open():
                            ua.SendKeys("{Esc}", waitTime=0.15)
                    except Exception:
                        pass

            return {
                "ok": True,
                "matched": False,
                "input_name": raw_name,
                "matched_name": "",
                "section": last_vis.get("section") or "",
                "used_keyword": last_kw,
                "search_box_text": last_vis.get("search_box_text") or last_kw,
                "candidates": all_cands[:6],
                "used_vision": bool(last_vis.get("used_vision")),
                "preview_image_data_url": preview_img,
                "reason": last_vis.get("reason") or f"在钉钉中按「{raw_name}」及相关关键词搜索后未见一致对象",
            }
    except Exception as exc:
        return {"ok": False, "matched": False, "message": f"核对过程异常：{exc}"}
    finally:
        if old_clip:
            try:
                set_clipboard_windows(old_clip)
            except Exception:
                pass


def send_dingtalk_image(cfg, trigger_lt=None, trigger_hm=None, recover_interactive=True, text_only_message=""):
    """把 WPS 复制的表格图片依次发到一个或多个指定钉钉会话，并附上自定义日期时间戳及后缀。"""
    global _active_dt_hwnd
    check_stop()
    targets, target_src = resolve_targets_for_time(cfg, lt=trigger_lt, hm_override=trigger_hm)
    if not targets:
        log("  ⚠️ 未勾选任何启用的钉钉目标会话，跳过本轮钉钉发送")
        return True

    msg_info = build_dingtalk_message_text(cfg, lt=trigger_lt, hm_override=trigger_hm, is_preview=False)
    text_msg = msg_info["text"]
    suffix_src = msg_info["suffix_source"]
    do_silent, silent_src = resolve_silent_for_time(cfg, lt=trigger_lt, hm_override=trigger_hm)
    if "dt_silent_send" in (cfg or {}) and silent_src == "全局默认":
        do_silent = bool(cfg.get("dt_silent_send", True))
    silent_desc = f"{'静音发送' if do_silent else '常规提醒'}（{silent_src}）"
    off_note = f" | 日期偏移{msg_info['day_offset']:+d}天({msg_info['time_source']})" if msg_info["day_offset"] else ""
    log(f"  消息文案：{text_msg}（后缀来源：{suffix_src}{off_note}）| {silent_desc} | 目标({target_src})共 {len(targets)} 个：{'、'.join(targets)}")

    try:
        import uiautomation as ua  # type: ignore[import-not-found]
    except Exception as e:
        raise RuntimeError("钉钉发送组件未安装") from e

    priv_info = check_dingtalk_privilege_match()
    if not priv_info["matched"]:
        log(f"  ⚠️ {priv_info['message']}")
    else:
        log(f"  {priv_info['message']}")
    pc_plan = probe_pc_and_dingtalk_profile(cfg)
    log(f"  🖥️ 当前电脑自适应方案：{pc_plan['summary']}")
    dt_strat = get_dt_runtime_strategy_from_playbook(cfg)
    log(f"  🧠 [AI 训练策略] {dt_strat['summary']}")
    scaled_send_path = None
    scaled_png_bytes = None
    tmp_img = app_dir() / "_sheetmate_tmp.png"
    try:
        exe_candidates = _resolve_dingtalk_exe_candidates()
        WIN_CLASSES = ("DtMainFrameView", "Qt51511QWindowIcon")
        WIN_TITLES = ("钉钉", "DingTalk")

        def _find():
            global _active_dt_hwnd
            for hwnd, _pid, _cls, _title, _sc in _enum_dingtalk_im_windows(include_hidden=False):
                ctl = _safe_control_from_handle(ua, hwnd)
                if ctl is not None:
                    _active_dt_hwnd = int(hwnd)
                    return ctl
            return None

        def _find_hidden_main_hwnd():
            """当钉钉最小化到系统托盘隐藏主窗时，仅定位真正的钉钉聊天主窗（绝不误拉钉钉-文档）。"""
            im_all = _enum_dingtalk_im_windows(include_hidden=True)
            if im_all:
                return int(im_all[0][0])
            return 0

        def _fresh_ding():
            for _ in range(6):
                w = _find()
                if w is not None:
                    try:
                        hwnd0 = int(w.NativeWindowHandle or 0)
                        if hwnd0 and user32.IsIconic(hwnd0):
                            _restore_and_activate_hwnd(hwnd0, maximize=True, wake_exe_candidates=exe_candidates)
                            w = _find() or w
                        rr = w.BoundingRectangle
                        if rr.right > rr.left and rr.bottom > rr.top:
                            return w
                    except Exception:
                        pass
                time.sleep(0.35)
            return None

        # 0) 搜索前先把 WPS 图片存成文件（纯文字发送模式跳过剪贴板图片校验）
        if str(text_only_message or "").strip():
            text_msg = str(text_only_message).strip()
            log("  已切换为纯文字直发模式，开始定位钉钉目标会话")
        else:
            state0 = save_clipboard_image_if_exists(tmp_img)
            if state0 == "noimage":
                raise RuntimeError("剪贴板里没有图片，已停止钉钉发送")
            if state0 != "saved":
                raise RuntimeError("表格图片暂存失败，已停止钉钉发送")
            scaled_send_path, scaled_png_bytes = _prepare_scaled_send_image(tmp_img)
            if cfg.get("save_shot_history", True):
                hist_p = archive_history_shot(tmp_img, keep=20)
                if hist_p is not None:
                    log(f"  已归档历史截图：{hist_p.name}")
            else:
                hist_p = None
            try:
                rec_a = record_report_archive(cfg, round_label="截图发群", summary_tsv=_last_captured_summary_tsv, shot_path=hist_p)
                if rec_a is not None:
                    log(f"  已存入 AI 数据档案（个人 {len(rec_a.get('personal') or [])} 人 / 团队 {len(rec_a.get('teams') or [])} 组）")
            except Exception:
                pass
            clear_clipboard_windows()
            log("  已暂存表格图片（中断重连/多群发送不会丢失），开始投递钉钉会话")

            # 若开启随图附带 AI 数据简报，自动请 AI 看刚刚截好的表格图并附上一小段重点总结
            ai_cfg_send = (cfg or {}).get("ai_assistant") if isinstance((cfg or {}).get("ai_assistant"), dict) else {}
            if ai_cfg_send.get("enabled", True) and ai_cfg_send.get("auto_attach_summary", False):
                try:
                    log("  正在请 AI 总结当前表格重点用于随图附发…")
                    ai_sum = summarize_report_with_ai(
                        cfg,
                        style_id=ai_cfg_send.get("attach_style", "brief"),
                        extra_img_path=tmp_img,
                    )
                    if ai_sum.get("source") in ("gemini", "ai_cloud") and str(ai_sum.get("reply") or "").strip():
                        sum_txt = str(ai_sum["reply"]).strip()
                        text_msg = f"{text_msg}\n{sum_txt}"
                        log("  ✅ 已附带 AI 数据简报")
                    else:
                        log(f"  ℹ️ AI 随图简报跳过（{ai_sum.get('source', 'unavailable')}），仅发送时间文案")
                except Exception as e_ai:
                    log(f"  ℹ️ AI 随图简报生成跳过：{e_ai}")

        # 2) 强力置前（AttachThreadInput 解除前台锁定）并最大化
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, wintypes.LPDWORD]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        user32.AttachThreadInput.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.BringWindowToTop.argtypes = [wintypes.HWND]
        user32.BringWindowToTop.restype = wintypes.BOOL
        kernel32.GetCurrentThreadId.argtypes = []
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE,
                                       wintypes.DWORD, ctypes.c_ulonglong]
        user32.keybd_event.restype = None
        _KEYUP = 0x0002
        _VK_CTRL, _VK_SHIFT, _VK_F = 0x11, 0x10, 0x46
        self_pid = os.getpid()
        fg_reclaim_state = {"count": 0}

        def _get_win_pid_hwnd(win):
            if win is None:
                return 0, 0
            try:
                hwnd = int(win.NativeWindowHandle or 0)
            except Exception:
                hwnd = 0
            pid = 0
            if hwnd:
                pv = wintypes.DWORD(0)
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pv))
                pid = int(pv.value or 0)
            return pid, hwnd

        def _raise_if_interrupted(expected_pid=0, fg_locked=False):
            check_stop()
            w_cur = _find()
            if w_cur is None:
                for _ in range(2):
                    time.sleep(0.22)
                    check_stop()
                    w_cur = _find()
                    if w_cur is not None:
                        break
                if w_cur is None:
                    raise DingTalkInterrupted("检测到钉钉窗口已关闭或隐藏到托盘")
            cur_pid, cur_hwnd = _get_win_pid_hwnd(w_cur)
            if cur_hwnd and user32.IsIconic(cur_hwnd):
                # 准备阶段或用户未触碰键鼠时，自动把最小化窗口还原；仅当发送中途用户主动最小化才暂停
                if not fg_locked or get_user_idle_seconds() >= 2.5:
                    log("  检测到钉钉处于最小化状态，正在自动唤醒还原到前台…")
                    restored = _restore_and_activate_hwnd(cur_hwnd, maximize=True, wake_exe_candidates=exe_candidates)
                    sleep_check(0.8)
                    w_cur = _find() or w_cur
                    cur_pid, cur_hwnd = _get_win_pid_hwnd(w_cur)
                    if restored and not (cur_hwnd and user32.IsIconic(cur_hwnd)):
                        return w_cur, cur_pid, cur_hwnd
                raise DingTalkInterrupted("检测到钉钉窗口已被最小化")
            if fg_locked:
                fg = int(user32.GetForegroundWindow() or 0)
                if fg:
                    fg_pv = wintypes.DWORD(0)
                    user32.GetWindowThreadProcessId(fg, ctypes.byref(fg_pv))
                    fg_pid = int(fg_pv.value or 0)
                    ding_pids = {p for p, _ in _list_dingtalk_processes() if p}
                    allowed_pids = {p for p in (cur_pid, expected_pid, self_pid, *ding_pids) if p}
                    if fg_pid and allowed_pids and fg_pid not in allowed_pids and get_user_idle_seconds() < 2.5:
                        # 二次复核，排除窗口最大化重绘/输入法浮窗导致的瞬间焦点跳变；若开启自动确认且主窗仍在先拉回最多2次
                        time.sleep(0.25)
                        fg2 = int(user32.GetForegroundWindow() or 0)
                        if fg2:
                            fg_pv2 = wintypes.DWORD(0)
                            user32.GetWindowThreadProcessId(fg2, ctypes.byref(fg_pv2))
                            fg_pid2 = int(fg_pv2.value or 0)
                            if fg_pid2 and fg_pid2 not in allowed_pids and get_user_idle_seconds() < 2.5:
                                if bool(cfg.get("auto_confirm_choices", True)) and fg_reclaim_state["count"] < 2 and cur_hwnd:
                                    fg_reclaim_state["count"] += 1
                                    log("  检测到前台焦点短暂漂移，按默认自动确定规则将钉钉主窗拉回前台继续…")
                                    _restore_and_activate_hwnd(cur_hwnd, maximize=False, wake_exe_candidates=exe_candidates)
                                    sleep_check(0.35)
                                    fg3 = int(user32.GetForegroundWindow() or 0)
                                    if fg3:
                                        fg_pv3 = wintypes.DWORD(0)
                                        user32.GetWindowThreadProcessId(fg3, ctypes.byref(fg_pv3))
                                        if int(fg_pv3.value or 0) in allowed_pids:
                                            return w_cur, cur_pid, cur_hwnd
                                raise DingTalkInterrupted("检测到您主动切换到了其他应用窗口")
            return w_cur, cur_pid, cur_hwnd

        def _sleep_watch_ding(seconds, expected_pid=0, fg_locked=False):
            rem = max(0.0, float(seconds))
            latest = _raise_if_interrupted(expected_pid=expected_pid, fg_locked=fg_locked)
            while rem > 0:
                step = min(0.2, rem)
                sleep_check(step)
                rem -= step
                latest = _raise_if_interrupted(expected_pid=expected_pid, fg_locked=fg_locked)
            return latest

        def _force_front():
            win = _find()
            hwnd = 0
            if win is not None:
                try:
                    hwnd = int(win.NativeWindowHandle or 0)
                except Exception:
                    hwnd = 0
            if not hwnd:
                hwnd = _find_hidden_main_hwnd()
            if not hwnd:
                for exe in exe_candidates:
                    if exe and os.path.exists(exe):
                        try:
                            subprocess.Popen(exe)
                            break
                        except Exception:
                            pass
                for _ in range(10):
                    time.sleep(0.3)
                    w2 = _find()
                    if w2 is not None:
                        try:
                            hwnd = int(w2.NativeWindowHandle or 0)
                            win = w2
                            break
                        except Exception:
                            pass
            if hwnd:
                _restore_and_activate_hwnd(hwnd, maximize=True, wake_exe_candidates=exe_candidates)
                return
            if win is not None:
                try:
                    win.SetActive()
                except Exception:
                    pass

        def _send_paste_shortcut(method="win32_sendinput_ctrl_v", compose_rect=None):
            m = str(method or "win32_sendinput_ctrl_v")
            if m == "context_menu_paste" and isinstance(compose_rect, (tuple, list)) and len(compose_rect) == 4:
                try:
                    rx = int((compose_rect[0] + compose_rect[2]) / 2)
                    ry = int((compose_rect[1] + compose_rect[3]) / 2)
                    ua.RightClick(rx, ry, waitTime=0.22)
                    sleep_check(0.28)
                    from pywinauto import Application, Desktop  # type: ignore[import-not-found]
                    ding_pids = {p for p, _ in _list_dingtalk_processes() if p}
                    clicked_menu = False
                    for pw in Desktop(backend="uia").windows():
                        try:
                            if ding_pids and pw.process_id() not in ding_pids:
                                continue
                            if not pw.is_visible():
                                continue
                            papp = Application(backend="uia").connect(handle=pw.handle, timeout=1.0).window(handle=pw.handle)
                            for spec in (
                                {"title_re": r"^(粘贴|Paste)(\([A-Za-z]\))?$", "control_type": "MenuItem", "found_index": 0},
                                {"title_re": r".*(粘贴|Paste).*", "found_index": 0},
                            ):
                                item = papp.child_window(**spec)
                                if item.exists(timeout=0.2):
                                    _uia_click_ctrl(_unwrap_uia(item))
                                    clicked_menu = True
                                    break
                            if clicked_menu:
                                break
                        except Exception:
                            pass
                    if clicked_menu:
                        return True
                    _win32_send_single_vk(0x1B)
                    sleep_check(0.12)
                except Exception as e_cm_p:
                    try:
                        _win32_send_single_vk(0x1B)
                    except Exception:
                        pass
                    if _is_stop_exception(e_cm_p):
                        raise
            if m == "uia_ctrl_v":
                try:
                    ua.SendKeys("{Ctrl}v", waitTime=0.32)
                    return True
                except Exception:
                    pass
            if m == "pywinauto_ctrl_v":
                try:
                    from pywinauto import keyboard as _pwa_kbd  # type: ignore[import-not-found]
                    _pwa_kbd.send_keys("^v")
                    return True
                except Exception:
                    pass
            if _win32_send_ctrl_key(ord("V")):
                return True
            try:
                from pywinauto import keyboard as _pwa_kbd  # type: ignore[import-not-found]
                _pwa_kbd.send_keys("^v")
                return True
            except Exception:
                ua.SendKeys("{Ctrl}v", waitTime=0.35)
                return True

        def _paste_text(text, verify=False, method="win32_sendinput_ctrl_v"):
            if verify:
                ready = False
                for _ in range(6):
                    set_clipboard_windows(text)
                    time.sleep(0.16)
                    try:
                        if get_clipboard_text_windows().strip() == text.strip():
                            ready = True
                            break
                    except Exception:
                        pass
                    time.sleep(0.18)
                if not ready:
                    return False
            else:
                set_clipboard_windows(text)
            time.sleep(0.18)
            _send_paste_shortcut(method=method)
            time.sleep(0.24)
            return True

        def _type_unicode(text):
            class KEYBDINPUT(ctypes.Structure):
                _fields_ = (("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                            ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                            ("dwExtraInfo", ctypes.c_size_t))

            class INPUT(ctypes.Structure):
                _fields_ = (("type", wintypes.DWORD), ("ki", KEYBDINPUT),
                            ("padding", ctypes.c_byte * 8))

            events = []
            raw = text.encode("utf-16-le")
            for i in range(0, len(raw), 2):
                code = raw[i] | (raw[i + 1] << 8)
                events.append(INPUT(1, KEYBDINPUT(0, code, 0x0004, 0, 0)))
                events.append(INPUT(1, KEYBDINPUT(0, code, 0x0006, 0, 0)))
            if events:
                arr = (INPUT * len(events))(*events)
                sent = user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))
                if sent != len(events):
                    raise RuntimeError("搜索词输入失败")
            time.sleep(0.3)

        def _link_box():
            tops = _enum_top_windows(
                target_classes=("im_chat::RichInputInsertLinkBox",),
                target_titles=None)
            if not tops:
                return None
            return _safe_control_from_handle(ua, tops[0][0])

        def _close_link_box():
            box = _link_box()
            if box is not None:
                try:
                    box.ButtonControl(ClassName="ddesign::IconButton").Click()
                    time.sleep(0.8)
                except Exception:
                    pass

        def _open_global_search():
            _dt_open_global_search()

        def _search_controls():
            return _dt_search_controls(ua)

        def _search_doc():
            return _dt_search_doc(ua)

        def _focus_search_input(popup_ctl, doc):
            return _dt_focus_search_input(ua, popup_ctl, doc)

        def _iter_search_docs(doc):
            return _dt_iter_search_docs(ua, doc)

        def _has_search_history(doc):
            """有严格超时与节点上限的搜索历史检测，绝不在庞大 CEF 结果树上长时间递归卡死。"""
            found_hist = False
            deadline = time.monotonic() + 0.45
            visited = [0]

            def _scan(ctrl, depth):
                nonlocal found_hist
                if found_hist or depth > 9 or visited[0] >= 75 or time.monotonic() > deadline:
                    return
                for ch in ctrl.GetChildren():
                    visited[0] += 1
                    if visited[0] % 15 == 0:
                        check_stop()
                    if visited[0] > 75 or time.monotonic() > deadline:
                        return
                    try:
                        if ch.ControlTypeName == "TextControl" and (ch.Name or "").strip() == "搜索历史":
                            rect = ch.BoundingRectangle
                            if rect.right > rect.left and rect.bottom > rect.top:
                                found_hist = True
                                return
                    except Exception:
                        pass
                    _scan(ch, depth + 1)

            for d in _iter_search_docs(doc):
                check_stop()
                if time.monotonic() > deadline:
                    break
                _scan(d, 0)
                if found_hist:
                    break
            return found_hist

        def _feature_item(doc, target_name, target_tokens):
            """带超时与节点上限解析搜索弹窗文档并精确返回命中的目标行控件矩形或控件。"""
            import unicodedata

            def norm(text):
                text = unicodedata.normalize("NFKC", text or "").casefold()
                return "".join(ch for ch in text if ch.isalnum())

            target = norm(target_name)
            norm_tokens = [norm(t) for t in target_tokens if norm(t)]
            found = []
            headings = []
            text_items = []
            hist_rect = None
            common_rect = None
            deadline = time.monotonic() + 1.15
            visited = [0]

            def collect(ctrl, depth):
                nonlocal hist_rect, common_rect
                if depth > 10 or visited[0] >= 180 or time.monotonic() > deadline:
                    return
                for k in ctrl.GetChildren():
                    visited[0] += 1
                    if visited[0] % 20 == 0:
                        check_stop()
                    if visited[0] > 180 or time.monotonic() > deadline:
                        return
                    try:
                        ct = k.ControlTypeName
                        if ct in ("TextControl", "ButtonControl", "ListItemControl", "HyperlinkControl"):
                            name = k.Name or ""
                            name_stripped = name.strip()
                            rect = k.BoundingRectangle
                            valid_rect = rect.right > rect.left and rect.bottom > rect.top
                            if name and valid_rect:
                                text_items.append((k, name, rect))
                            if ct == "TextControl" and valid_rect:
                                if name_stripped == "搜索历史" and hist_rect is None:
                                    hist_rect = rect
                                elif name_stripped == "常用" and common_rect is None:
                                    common_rect = rect
                            normalized = norm(name)
                            if ct == "TextControl" and name_stripped in ("功能", "群组", "联系人", "常用") and valid_rect:
                                headings.append((name_stripped, rect))
                            elif target and normalized.startswith(target) and valid_rect:
                                found.append((k, rect))
                    except Exception:
                        pass
                    collect(k, depth + 1)

            for d in _iter_search_docs(doc):
                check_stop()
                if time.monotonic() > deadline:
                    break
                collect(d, 0)

            def _in_history(r):
                return (
                    hist_rect is not None
                    and r.top >= hist_rect.top - 4
                    and (
                        (common_rect is not None and r.top < common_rect.top - 4)
                        or (common_rect is None and r.top < hist_rect.bottom + 90)
                    )
                )

            if hist_rect is not None:
                found = [(k, r) for k, r in found if not _in_history(r)]
                text_items = [(k, name, r) for k, name, r in text_items if not _in_history(r)]

            # 按行聚合并拼接群名、emoji 与徽标文本
            if not found and (norm_tokens or len(target) >= 2):
                rows = []
                ordered = sorted(text_items,
                                 key=lambda x: ((x[2].top + x[2].bottom) / 2, x[2].left))
                for item in ordered:
                    r = item[2]
                    cy = (r.top + r.bottom) / 2
                    for row in rows:
                        if abs(cy - row[0]) <= 28:
                            row[1].append(item)
                            row[0] = (row[0] * (len(row[1]) - 1) + cy) / len(row[1])
                            break
                    else:
                        rows.append([cy, [item]])

                def _pick_row_target(row_items):
                    candidates = [
                        (k, r) for k, name, r in row_items
                        if norm(name) and not norm(name).isdigit()
                        and name.strip() not in ("功能", "群组", "内部群", "综合", "联系人", "聊天记录", "文档", "搜索历史", "常用")
                    ]
                    if candidates:
                        return max(candidates, key=lambda x: x[1].right - x[1].left)
                    return None

                # 第一优先级：所有纯文字词段全命中
                for _cy, row_items in rows:
                    row_items.sort(key=lambda x: x[2].left)
                    combined = "".join(norm(name) for _k, name, _r in row_items)
                    if target and target in combined:
                        picked = _pick_row_target(row_items)
                        if picked:
                            found.append(picked)
                            break
                    if norm_tokens and all(tok in combined for tok in norm_tokens):
                        picked = _pick_row_target(row_items)
                        if picked:
                            found.append(picked)
                            break

                # 第二优先级：如果只有一个词段或钉钉截断群名，匹配核心词段
                if not found and norm_tokens:
                    main_tok = max(norm_tokens, key=len)
                    for _cy, row_items in rows:
                        row_items.sort(key=lambda x: x[2].left)
                        combined = "".join(norm(name) for _k, name, _r in row_items)
                        if len(main_tok) >= 2 and main_tok in combined:
                            picked = _pick_row_target(row_items)
                            if picked:
                                found.append(picked)
                                break

            if found:
                badged = [(k, r) for k, r in found
                          if any(label == "功能" and abs(h.top - r.top) < 34 and h.left >= r.left
                                 for label, h in headings)]
                if badged:
                    return min(badged, key=lambda x: x[1].top)
                below = [(k, r) for k, r in found
                         if any(0 < r.top - h.top < 280 and h.left <= r.left
                                for label, h in headings)]
                if below:
                    return min(below, key=lambda x: x[1].top)
                return min(found, key=lambda x: x[1].top)
            return None

        def _scan_compose_candidates(win, r):
            """深度扫描钉钉 Qt 输入区（edit / widget_input_wnd / bottomBarWidget）并生成按训练权重排序的安全点位列表。"""
            dpi_sc = float((pc_plan or {}).get("dpi_scale") or 1.0)
            edit_rect = None
            input_wnd_rect = None
            bottom_bar_rect = None
            edit_ctrl = None
            hwnd_now = 0
            if win is not None:
                try:
                    hwnd_now = int(win.NativeWindowHandle or 0)
                except Exception:
                    hwnd_now = 0
            if hwnd_now:
                try:
                    from pywinauto import Application  # type: ignore[import-not-found]
                    papp = Application(backend="uia").connect(handle=hwnd_now, timeout=2.0)
                    pwin = papp.window(handle=hwnd_now)
                    pwin.set_focus()
                    pwrap = _unwrap_uia(pwin)
                    input_wnd = None
                    ed_hit = None
                    rich_low = DT_RICH_EDIT_AUTO_ID.lower()
                    W_win = max(1, r.right - r.left)
                    H_win = max(1, r.bottom - r.top)
                    for ctrl, _ in _iter_dt_uia_pruned(pwrap, max_depth=24):
                        try:
                            ii = ctrl.element_info
                            aid_l = (ii.automation_id or "").lower()
                            if bottom_bar_rect is None and (
                                aid_l.endswith(".bottombarwidget")
                                or aid_l.endswith(".combo_send_button_widget")
                            ):
                                br = ctrl.rectangle()
                                if br.width() > 60 and br.top >= r.top + int(H_win * 0.65):
                                    bottom_bar_rect = (br.left, br.top, br.right, br.bottom)
                            if input_wnd is None and (aid_l.endswith(".widget_input_wnd") or aid_l.endswith(".widgetinputwnd")):
                                ir = ctrl.rectangle()
                                if ir.width() > 100 and ir.height() > 36:
                                    input_wnd = ctrl
                                    input_wnd_rect = (ir.left, ir.top, ir.right, ir.bottom)
                            if ed_hit is None and (aid_l == rich_low or aid_l.endswith(".drich_edit")):
                                ed_hit = ctrl
                            nm_l = (ii.name or "").lower()
                            if (
                                edit_rect is None
                                and "bottombar" not in aid_l
                                and (ii.control_type or "") in ("Edit", "Document")
                                and "搜索" not in nm_l
                                and "search" not in nm_l
                            ):
                                er = ctrl.rectangle()
                                if er.width() > 120 and er.height() > 22 and er.top >= r.top + int(H_win * 0.50) and er.left >= r.left + int(W_win * 0.24):
                                    edit_rect = (er.left, er.top, er.right, er.bottom)
                                    edit_ctrl = ctrl
                        except Exception:
                            pass
                    if input_wnd_rect is None and ed_hit is not None:
                        try:
                            er2 = ed_hit.rectangle()
                            if er2.width() > 100 and er2.height() > 36:
                                input_wnd_rect = (er2.left, er2.top, er2.right, er2.bottom)
                        except Exception:
                            pass
                except Exception:
                    pass
            cands = _compute_dt_compose_click_points(
                (r.left, r.top, r.right, r.bottom),
                edit_rect=edit_rect,
                input_wnd_rect=input_wnd_rect,
                bottom_bar_rect=bottom_bar_rect,
                dpi_scale=dpi_sc,
            )
            order_pref = list((dt_strat or {}).get("focus_order") or DEFAULT_DT_FOCUS_STRATEGIES)
            rank_map = {nm: idx for idx, nm in enumerate(order_pref)}
            cands.sort(key=lambda it: rank_map.get(it[0], 999))
            return cands, edit_ctrl

        def _focus_chat_input(win, r, strategy_hint=None):
            """按训练权重顺序点击待发送输入框并确认坐标安全，返回命中的 (strategy_name, compose_patch_rect)。"""
            check_stop()
            cands, _edit_ctrl = _scan_compose_candidates(win, r)
            if not cands:
                W = r.right - r.left
                H = r.bottom - r.top
                cx = int(r.left + W * 0.48)
                cy = int(r.top + H * 0.85)
                ua.Click(cx, cy, waitTime=0.25)
                return "ratio_bottom_85", (int(r.left + W * 0.26), int(r.top + H * 0.72), int(r.right - W * 0.08), int(r.bottom - H * 0.07))
            picked = None
            if strategy_hint:
                picked = next((c for c in cands if c[0] == strategy_hint), None)
            if picked is None:
                picked = cands[0]
            strat_name, cx, cy, patch_rect = picked
            ua.Click(int(cx), int(cy), waitTime=0.22)
            time.sleep(0.18)
            return strat_name, patch_rect

        def _is_win_in_menu_mode(hwnd):
            return bool(_get_win_gui_caret_info(hwnd).get("in_menu"))

        def _is_focus_in_chat_compose(ding_now, r, compose_rect=None):
            """联合校验 UIA 焦点控件与 Win32 Caret 闪烁光标位置，确保光标确实已进入待发送框。"""
            if _is_search_popup_open():
                return False, "全局搜索弹窗处于打开状态"
            try:
                ding_hwnd = int(ding_now.NativeWindowHandle)
            except Exception:
                ding_hwnd = 0
            caret_info = {"in_menu": False, "has_caret": False, "caret_screen_pt": None}
            if ding_hwnd:
                fg_hwnd = int(user32.GetForegroundWindow() or 0)
                if fg_hwnd != ding_hwnd:
                    return False, "当前前台窗口并非钉钉主窗（可能仍有弹出菜单或切换了窗口）"
                caret_info = _get_win_gui_caret_info(ding_hwnd)
                if caret_info.get("in_menu"):
                    return False, "钉钉线程仍处于菜单展开模式，尚未回到输入窗口"
            try:
                fc = ua.GetFocusedControl()
            except Exception:
                fc = None
            ok_uia, desc_uia = _evaluate_compose_focus_ctrl(fc, r)
            if ok_uia:
                return True, desc_uia
            if caret_info.get("has_caret") and caret_info.get("caret_screen_pt"):
                cx_pt, cy_pt = caret_info["caret_screen_pt"]
                W = max(1, r.right - r.left)
                H = max(1, r.bottom - r.top)
                if cy_pt >= r.top + int(H * 0.56) and cx_pt >= r.left + int(W * 0.22):
                    if isinstance(compose_rect, (tuple, list)) and len(compose_rect) == 4:
                        if compose_rect[0] - 40 <= cx_pt <= compose_rect[2] + 40 and compose_rect[1] - 30 <= cy_pt <= compose_rect[3] + 35:
                            return True, f"Win32输入光标已就位({cx_pt},{cy_pt})"
                    return True, f"Win32底部输入光标已激活({cx_pt},{cy_pt})"
            return False, desc_uia

        def _verify_and_focus_chat_input(retries=2, force_strategy=None):
            """点击待发送框并严格校验光标落位；未稳时自动轮换候选点位。"""
            last_reason = "焦点未确认"
            strat_seq = ([force_strategy] if force_strategy else []) + list((dt_strat or {}).get("focus_order") or DEFAULT_DT_FOCUS_STRATEGIES)
            seen_strats = []
            for s in strat_seq:
                if s and s not in seen_strats:
                    seen_strats.append(s)
            for attempt in range(max(retries + 1, min(4, len(seen_strats)))):
                check_stop()
                ding_now = _fresh_ding()
                if ding_now is None:
                    raise RuntimeError("找不到钉钉主窗口，无法校验光标位置")
                r_now = ding_now.BoundingRectangle
                hint = seen_strats[attempt] if attempt < len(seen_strats) else None
                used_strat, c_rect = _focus_chat_input(ding_now, r_now, strategy_hint=hint)
                sleep_check(0.18)
                _ensure_not_in_search_box(r_now)
                ok_focus, desc = _is_focus_in_chat_compose(ding_now, r_now, compose_rect=c_rect)
                if ok_focus:
                    record_run_trace_event("focus_ok", used_strat)
                    return used_strat, c_rect, desc
                record_run_trace_event("focus_fail", used_strat, {"reason": desc})
                last_reason = desc
                if attempt < max(retries, min(3, len(seen_strats) - 1)):
                    sleep_check(0.25)
            ding_fb = _fresh_ding()
            r_fb = ding_fb.BoundingRectangle if ding_fb is not None else None
            cands_fb, _ = _scan_compose_candidates(ding_fb, r_fb) if (ding_fb is not None and r_fb is not None) else ([], None)
            fb_rect = cands_fb[0][3] if cands_fb else None
            return seen_strats[0] if seen_strats else "ratio_bottom_85", fb_rect, last_reason

        def _prepare_chat_compose_box(force_strategy=None, clear_box=True):
            """严格执行：定位点击待发送框 -> 按需清空旧内容 -> 激活输入态 -> 确认光标在待发送框内。"""
            used_strat, c_rect, focus_desc = _verify_and_focus_chat_input(retries=2, force_strategy=force_strategy)
            if clear_box and cfg.get("dt_clear_before_send", True):
                if not _win32_send_ctrl_key(ord("A")):
                    ua.SendKeys("{Ctrl}a", waitTime=0.10)
                sleep_check(0.06)
                if not _win32_send_single_vk(0x08):
                    ua.SendKeys("{Back}", waitTime=0.08)
                sleep_check(0.04)
                if not _win32_send_single_vk(0x2E):
                    ua.SendKeys("{Delete}", waitTime=0.08)
                sleep_check(0.12)
                log("  已清空发送框内原有内容")
                # 发送框已清空时轻按一次 Enter，可稳定收起残留输入法候选浮层并激活光标输入态（空框不会发出消息）
                if not _win32_send_single_vk(0x0D):
                    ua.SendKeys("{Enter}", waitTime=0.10)
                sleep_check(0.16)
                log("  ✅ 已在清空的待发送框按 Enter 激活光标并确认就位（" + str(focus_desc) + "）")
            else:
                log(f"  ✅ 光标已确认在待发送框内（{focus_desc}）")
            ding_chk = _fresh_ding()
            if ding_chk is not None:
                ok2, _ = _is_focus_in_chat_compose(ding_chk, ding_chk.BoundingRectangle, compose_rect=c_rect)
                if not ok2:
                    used_strat, c_rect = _focus_chat_input(ding_chk, ding_chk.BoundingRectangle, strategy_hint=used_strat)
                    sleep_check(0.15)
            return used_strat, c_rect

        def _is_focus_in_search_area(r):
            if _is_search_popup_open():
                return True, "全局搜索弹窗仍处于打开状态"
            try:
                fc = ua.GetFocusedControl()
                if fc is not None:
                    fname = (fc.Name or "").lower()
                    if "搜索" in fname or "search" in fname:
                        return True, f"焦点控件为「{fc.Name}」"
                    fr = fc.BoundingRectangle
                    H = max(1, r.bottom - r.top) if r is not None else 900
                    r_top = r.top if r is not None else 0
                    if (fr.bottom > fr.top and fr.right > fr.left
                            and r is not None and fr.bottom < r_top + H * 0.35):
                        return True, "焦点位于钉钉窗口顶部搜索栏区域"
            except Exception:
                pass
            return False, ""

        def _ensure_not_in_search_box(r):
            bad, reason = _is_focus_in_search_area(r)
            if not bad:
                return
            try:
                ua.SendKeys("{Esc}", waitTime=0.25)
            except Exception:
                pass
            time.sleep(0.3)
            win_now = _fresh_ding()
            r_now = win_now.BoundingRectangle if win_now else r
            _focus_chat_input(win_now, r_now)
            time.sleep(0.35)
            bad2, reason2 = _is_focus_in_search_area(r_now)
            if bad2:
                raise RuntimeError(f"检测到焦点仍在钉钉搜索框（{reason2}），已安全拦截粘贴")

        def _wait_search_closed(loops=10, step=0.22):
            for _ in range(loops):
                check_stop()
                sleep_check(step)
                if not _is_search_popup_open():
                    sleep_check(0.18)
                    if not _is_search_popup_open():
                        return True
            return False

        _current_actual_target = {"name": ""}

        def _send_one_target_with_actual(idx, total, target_name):
            _current_actual_target["name"] = target_name
            st_r = _send_one_target(idx, total, target_name)
            return st_r, (_current_actual_target.get("name") or target_name)

        def _send_one_target(idx, total, search_name):
            if total > 1:
                set_step(f"正在发送到钉钉 ({idx}/{total})：{search_name}…")
                log(f"  → 目标 ({idx}/{total})：正在搜索「{search_name}」…")
            else:
                log(f"  正在打开钉钉，搜索「{search_name}」…")
            record_run_trace_event("dt_target", search_name)

            search_candidates, search_tokens = _extract_search_keywords(search_name)
            smart_cand_map = cfg.get("_smart_target_candidates") if isinstance(cfg, dict) else None
            extra_cands = list(smart_cand_map.get(search_name) or []) if isinstance(smart_cand_map, dict) else []
            if not extra_cands:
                try:
                    sm_r = resolve_smart_dingtalk_target(search_name, cfg=cfg, use_ai=True)
                    extra_cands = list(sm_r.get("candidates") or [])
                except Exception:
                    extra_cands = []
            direct_candidates = list(dict.fromkeys(
                [search_name] + [c for c in extra_cands if c] + [c for c in search_candidates if c]
            ))
            skip_search = False
            ding_was_open = (_find() is not None)
            allow_auto_launch = not ding_was_open
            awakened_once = False
            resumed_by_user = False
            auto_heal_used = {"search": False, "paste": False}
            vision_hdr_rejects = 0
            target_switch_hops = 0
            seen_search_candidates = []

            def _record_seen_cands(c_list):
                for _ci in (c_list or []):
                    _cs = str(_ci or "").strip()
                    if _cs and "文件小助手" not in _cs and "文件传输助手" not in _cs and _cs not in seen_search_candidates:
                        seen_search_candidates.append(_cs)

            def _ask_human_confirm_mismatch(opened_lbl, reason_txt, ding_obj=None, popup_ctl=None):
                cand_pool = []
                for _c in (
                    list(seen_search_candidates)
                    + [t for t in get_enabled_dt_targets(cfg) if str(t or "").strip()]
                    + [str(t.get("name") or "").strip() for t in (cfg.get("dt_targets") or []) if isinstance(t, dict)]
                    + list(_load_verified_dingtalk_targets().values())
                ):
                    _cs = str(_c or "").strip()
                    if (
                        _cs
                        and _cs != search_name
                        and _cs != "文件小助手"
                        and _cs != "文件传输助手"
                        and _cs not in cand_pool
                    ):
                        cand_pool.append(_cs)
                shot_url = _capture_dingtalk_confirm_screenshot(ding_obj=ding_obj, popup_ctl=popup_ctl)
                log(
                    f"  👁️ [AI 桌面核对不一致 · 等待本人确认] 预期「{search_name}」，桌面看到「{opened_lbl}」"
                    f"（{reason_txt or '需要本人确认'}），已将现场截图同步到 AI 对话与手机端…"
                )
                if not recover_interactive or _target_confirm_callback is None:
                    return {"action": "cancel_send", "reason": "non_interactive"}
                return wait_for_target_confirmation(
                    {
                        "target_name": search_name,
                        "opened_name": str(opened_lbl or "未确认对象"),
                        "reason": str(reason_txt or "桌面识别对象与配置目标不完全一致"),
                        "candidates": cand_pool[:10],
                        "image_data_url": shot_url,
                    },
                    timeout_sec=600,
                )

            def _apply_new_target_switch(new_tgt_raw):
                nonlocal search_name, search_tokens, direct_candidates, skip_search, vision_hdr_rejects, target_switch_hops
                new_t = str(new_tgt_raw or "").strip()[:80]
                if not new_t:
                    return False
                target_switch_hops += 1
                search_name = new_t
                _current_actual_target["name"] = search_name
                sc_new, _st_new = _extract_search_keywords(search_name)
                search_tokens = _st_new
                try:
                    sm_new = resolve_smart_dingtalk_target(search_name, cfg=cfg, use_ai=True)
                    ex_new = list(sm_new.get("candidates") or [])
                except Exception:
                    ex_new = []
                direct_candidates = list(dict.fromkeys([search_name] + [c for c in ex_new if c] + [c for c in sc_new if c]))
                skip_search = False
                vision_hdr_rejects = 0
                log(f"  🔁 [本人切换发送目标] 第 {target_switch_hops} 次切换：改为搜索并核对「{search_name}」…")
                return True

            def _pause_for_dingtalk_interruption(reason_msg):
                nonlocal skip_search, resumed_by_user, allow_auto_launch
                if not recover_interactive:
                    raise RuntimeError(f"{reason_msg}，已停止发送")
                issue = diagnose_issue(
                    "dingtalk_interrupted",
                    RuntimeError(reason_msg),
                    extra={
                        "search_name": search_name,
                        "idx": idx,
                        "total": total,
                        "can_skip_target": (total > 1),
                        "reason": reason_msg,
                    },
                )
                choice = wait_for_recovery(
                    issue["title"], issue["detail"], issue["hint"], issue["actions"], timeout_sec=3600
                )
                resumed_by_user = True
                allow_auto_launch = True
                if choice == "skip_target":
                    return "skipped"
                skip_search = (choice in ("skip_search", "retry_paste"))
                return "continue"

            while True:
                check_stop()
                # 1) 检查钉钉窗口状态：若为最小化/后台先自动还原；仅当用户主动操作钉钉或中途关闭才暂停避让
                ding = _find()
                if ding is not None:
                    ding_was_open = True
                    allow_auto_launch = False
                    d_pid0, d_hwnd0 = _get_win_pid_hwnd(ding)
                    is_iconic_now = bool(d_hwnd0 and user32.IsIconic(d_hwnd0))
                    if is_iconic_now:
                        log("  检测到钉钉主窗口处于最小化状态，正在自动还原并置顶…")
                        _restore_and_activate_hwnd(d_hwnd0, maximize=True, wake_exe_candidates=exe_candidates)
                        sleep_check(0.8)
                        ding = _find() or ding
                    elif recover_interactive and not resumed_by_user:
                        fg0 = int(user32.GetForegroundWindow() or 0)
                        if fg0 and fg0 == d_hwnd0 and get_user_idle_seconds() < 2.0:
                            log("  检测到您刚使用过钉钉，已自动默认继续执行（无需手动点击确认）…")
                            sleep_check(0.6)
                            resumed_by_user = True
                            continue
                        elif fg0 and d_hwnd0 and fg0 != d_hwnd0:
                            try:
                                c0 = ctypes.create_unicode_buffer(128)
                                t0 = ctypes.create_unicode_buffer(256)
                                user32.GetClassNameW(fg0, c0, 128)
                                user32.GetWindowTextW(fg0, t0, 256)
                                if _is_dingtalk_aux_window(c0.value, t0.value):
                                    log(f"  检测到当前前置为「{t0.value or c0.value}」，正自动切回钉钉聊天主窗…")
                                    _restore_and_activate_hwnd(d_hwnd0, maximize=True, wake_exe_candidates=exe_candidates)
                                    ding = _find() or ding
                            except Exception:
                                pass
                elif ding_was_open and awakened_once and not allow_auto_launch and recover_interactive:
                    act_res = _pause_for_dingtalk_interruption("检测到您关闭或隐藏了钉钉窗口")
                    if act_res == "skipped":
                        return "skipped"
                    continue
                else:
                    hidden_h = _find_hidden_main_hwnd()
                    if hidden_h:
                        log("  检测到钉钉隐藏在后台托盘，正在尝试直接唤醒窗口…")
                        _restore_and_activate_hwnd(hidden_h, maximize=True, wake_exe_candidates=exe_candidates)
                        sleep_check(0.8)
                        ding = _find()
                    if ding is None:
                        for exe in exe_candidates:
                            if exe and os.path.exists(exe):
                                try:
                                    subprocess.Popen(exe)
                                    log("  钉钉窗口在托盘或未启动，已尝试唤起，请稍候…")
                                except Exception:
                                    pass
                                break
                        for _ in range(18):
                            sleep_check(1)
                            ding = _find()
                            if ding:
                                break
                    if ding is not None:
                        ding_was_open = True
                        allow_auto_launch = False

                if ding is None or _is_dingtalk_login_window(ding):
                    if not recover_interactive:
                        raise RuntimeError("没找到已登录的钉钉主窗口，请确认钉钉已打开并扫码登录后再试")
                    issue = diagnose_issue("dingtalk_login")
                    wait_for_recovery(issue["title"], issue["detail"], issue["hint"], issue["actions"])
                    skip_search = False
                    resumed_by_user = True
                    allow_auto_launch = True
                    continue

                ding_pid, _ = _get_win_pid_hwnd(ding)

                if not skip_search:
                    chat_ready = False
                    confirmed_no_match = False
                    opened_via_strict_uia = False
                    interrupted_act = None
                    try:
                        with hands_off_guard(f"钉钉搜索并打开会话「{search_name}」，随后粘贴数据"):
                            for _attempt in range(3):
                                check_stop()
                                _force_front()
                                awakened_once = True
                                _, ding_pid, _ = _sleep_watch_ding(0.32, expected_pid=ding_pid, fg_locked=False)
                                set_cands = list(direct_candidates)
                                if cfg.get("_resolve_self_for_search") == search_name and not auto_heal_used.get("_self_probed"):
                                    auto_heal_used["_self_probed"] = True
                                    dt_live_self = detect_dingtalk_logged_in_user_name(use_cache=False)
                                    if dt_live_self and dt_live_self not in set_cands:
                                        set_cands = list(set_cands) + [dt_live_self]
                                if _link_box() is not None:
                                    _close_link_box()
                                _open_global_search()

                                opened = False
                                for _ in range(14):
                                    _raise_if_interrupted(expected_pid=ding_pid, fg_locked=True)
                                    if _is_search_popup_open():
                                        opened = True
                                        break
                                    sleep_check(0.18)
                                popup_ctl, doc = (None, None) if opened else _search_doc()
                                if not opened and doc is None:
                                    log("  搜索窗口没打开，正在重试…")
                                    continue
                                _sleep_watch_ding(0.22, expected_pid=ding_pid, fg_locked=True)

                                settle_wait = float((pc_plan or {}).get("search_settle_sec") or 0.7)
                                kw_idx = 0
                                all_cands_seen_no_match = True
                                while kw_idx < len(set_cands):
                                    kw = set_cands[kw_idx]
                                    _raise_if_interrupted(expected_pid=ding_pid, fg_locked=True)
                                    if kw_idx > 0:
                                        if not _is_search_popup_open():
                                            _open_global_search()
                                            for _ in range(14):
                                                _raise_if_interrupted(expected_pid=ding_pid, fg_locked=True)
                                                if _is_search_popup_open():
                                                    break
                                                sleep_check(0.18)
                                            _sleep_watch_ding(0.2, expected_pid=ding_pid, fg_locked=True)
                                        popup_ctl, doc = _search_doc()
                                        if doc is not None:
                                            _focus_search_input(popup_ctl, doc)
                                            sleep_check(0.12)

                                    ua.SendKeys("{Ctrl}a", waitTime=0.06)
                                    ua.SendKeys("{Back}", waitTime=0.06)
                                    if not _paste_text(kw, verify=True):
                                        all_cands_seen_no_match = False
                                        log(f"  关键词「{kw}」写入剪贴板未就绪，跳过重试…")
                                        kw_idx += 1
                                        continue
                                    _sleep_watch_ding(settle_wait, expected_pid=ding_pid, fg_locked=True)

                                    def _capture_search_shot_v(pop_c):
                                        if pop_c is not None:
                                            try:
                                                pr = pop_c.BoundingRectangle
                                                if (pr.right - pr.left) >= 150 and (pr.bottom - pr.top) >= 150:
                                                    s0 = _grab_window_region_b64(pr, max_dim=1100)
                                                    if s0:
                                                        return s0
                                            except Exception:
                                                pass
                                        for _h_s, c_s in _search_controls():
                                            try:
                                                pr2 = c_s.BoundingRectangle
                                                if (pr2.right - pr2.left) >= 150 and (pr2.bottom - pr2.top) >= 150:
                                                    s2 = _grab_window_region_b64(pr2, max_dim=1100)
                                                    if s2:
                                                        return s2
                                            except Exception:
                                                pass
                                        try:
                                            d_now = _find() or ding
                                            if d_now is not None:
                                                return _grab_window_region_b64(d_now.BoundingRectangle, crop_frac=(0.0, 0.0, 0.68, 0.76), max_dim=1200)
                                        except Exception:
                                            pass
                                        return None

                                    _popup_now, doc_now = _search_doc()
                                    if doc_now is not None:
                                        scan_res = _collect_dingtalk_search_rows(_iter_search_docs(doc_now), check_stop)
                                        if scan_res.get("empty_result"):
                                            log(f"  钉钉中无「{kw}」搜索结果，禁止盲选误发")
                                            kw_idx += 1
                                            continue
                                        best_row = _ai_pick_best_search_row(search_name, set_cands, scan_res.get("rows") or [], cfg=cfg)
                                        if best_row is not None and best_row.get("rect") is not None:
                                            br = best_row["rect"]
                                            cx = int((br.left + br.right) / 2)
                                            cy = int((br.top + br.bottom) / 2)
                                            ua.Click(cx, cy, waitTime=0.25)
                                            if _wait_search_closed(loops=8, step=0.18):
                                                if str(best_row.get("section") or "") in ("联系人", "群组", "常用"):
                                                    _save_verified_dingtalk_target(search_name, best_row.get("title", ""))
                                                    opened_via_strict_uia = True
                                                log(f"  ✅ 已一对一确认钉钉目标：「{search_name}」→「{best_row.get('title', search_name)}」")
                                                chat_ready = True
                                                break
                                        if _has_search_history(doc_now):
                                            all_cands_seen_no_match = False
                                            _focus_search_input(_popup_now, doc_now)
                                            sleep_check(0.12)
                                            ua.SendKeys("{Ctrl}a", waitTime=0.06)
                                            ua.SendKeys("{Back}", waitTime=0.06)
                                            if _paste_text(kw, verify=True):
                                                _sleep_watch_ding(settle_wait, expected_pid=ding_pid, fg_locked=True)
                                                _popup2, doc2 = _search_doc()
                                                if doc2 is not None:
                                                    scan2 = _collect_dingtalk_search_rows(_iter_search_docs(doc2), check_stop)
                                                    best2 = _ai_pick_best_search_row(search_name, set_cands, scan2.get("rows") or [], cfg=cfg)
                                                    if best2 is not None and best2.get("rect") is not None:
                                                        br2 = best2["rect"]
                                                        ua.Click(int((br2.left + br2.right) / 2), int((br2.top + br2.bottom) / 2), waitTime=0.25)
                                                        if _wait_search_closed(loops=8, step=0.18):
                                                            if str(best2.get("section") or "") in ("联系人", "群组", "常用"):
                                                                _save_verified_dingtalk_target(search_name, best2.get("title", ""))
                                                                opened_via_strict_uia = True
                                                            chat_ready = True
                                                            break
                                                    scan_res = scan2
                                                    _popup_now, doc_now = _popup2, doc2

                                        # UIA 未能直接锁定清晰选项时，截屏搜索弹窗交由 AI 桌面视觉识别确认并点击
                                        _record_seen_cands([r.get("title") for r in (scan_res.get("rows") or []) if isinstance(r, dict)])
                                        shot_v = _capture_search_shot_v(_popup_now)
                                        vis_res = _ai_vision_match_dingtalk_search(search_name, kw, shot_v, uia_rows=scan_res.get("rows") or [], cfg=cfg)
                                        _record_seen_cands(vis_res.get("candidates"))
                                        if (
                                            vis_res.get("found")
                                            and vis_res.get("same_as_target")
                                            and shot_v is not None
                                            and vis_res.get("x_pct") is not None
                                            and vis_res.get("y_pct") is not None
                                        ):
                                            vx1, vy1, vx2, vy2 = shot_v["bbox"]
                                            ua.Click(int(vx1 + (vx2 - vx1) * vis_res["x_pct"]), int(vy1 + (vy2 - vy1) * vis_res["y_pct"]), waitTime=0.25)
                                            if _wait_search_closed(loops=8, step=0.18):
                                                m_title = vis_res.get("matched_name") or search_name
                                                _save_verified_dingtalk_target(search_name, m_title)
                                                log(f"  ✅ [AI 桌面视觉识别] 确认钉钉搜索结果「{m_title}」与目标「{search_name}」一致，已精准点开")
                                                chat_ready = True
                                                break
                                        elif vis_res.get("used_vision"):
                                            log(f"  👁️ [AI 桌面视觉识别] 搜「{kw}」未见与「{search_name}」一致的对象（{vis_res.get('reason') or '无匹配'}），禁止盲按 Enter")
                                            kw_idx += 1
                                            continue

                                        matched_strict = _feature_item(doc_now, search_name, search_tokens)
                                        if matched_strict is not None and "助手" not in str(matched_strict[0].Name or ""):
                                            ir = matched_strict[1]
                                            ua.Click(int((ir.left + ir.right) / 2), int((ir.top + ir.bottom) / 2), waitTime=0.25)
                                            if _wait_search_closed(loops=6, step=0.18):
                                                chat_ready = True
                                                break
                                        log(f"  未在钉钉结果中找到与「{kw}」一对一匹配的会话，严禁按 Enter 乱发")
                                        kw_idx += 1
                                        continue

                                    # 读取不到 UIA 搜索文档树时，先启用 AI 桌面视觉识别截图核对，绝不盲按 Enter
                                    shot_v = _capture_search_shot_v(popup_ctl)
                                    vis_res = _ai_vision_match_dingtalk_search(search_name, kw, shot_v, uia_rows=[], cfg=cfg)
                                    _record_seen_cands(vis_res.get("candidates"))
                                    if (
                                        vis_res.get("found")
                                        and vis_res.get("same_as_target")
                                        and shot_v is not None
                                        and vis_res.get("x_pct") is not None
                                        and vis_res.get("y_pct") is not None
                                    ):
                                        vx1, vy1, vx2, vy2 = shot_v["bbox"]
                                        ua.Click(int(vx1 + (vx2 - vx1) * vis_res["x_pct"]), int(vy1 + (vy2 - vy1) * vis_res["y_pct"]), waitTime=0.25)
                                        if _wait_search_closed(loops=8, step=0.18):
                                            m_title = vis_res.get("matched_name") or search_name
                                            _save_verified_dingtalk_target(search_name, m_title)
                                            log(f"  ✅ [AI 桌面视觉识别] 确认钉钉搜索结果「{m_title}」与目标「{search_name}」一致，已精准点开")
                                            chat_ready = True
                                            break
                                    elif vis_res.get("used_vision"):
                                        log(f"  👁️ [AI 桌面视觉识别] 搜「{kw}」未见与「{search_name}」一致的对象（{vis_res.get('reason') or '无匹配'}），禁止盲按 Enter")
                                        kw_idx += 1
                                        continue

                                    all_cands_seen_no_match = False
                                    # 仅当无法读取搜索树弹窗且视觉通道离线时才使用键盘 Enter 兜底
                                    _raise_if_interrupted(expected_pid=ding_pid, fg_locked=True)
                                    ua.SendKeys("{Enter}", waitTime=0.22)
                                    if _wait_search_closed(loops=8, step=0.18):
                                        chat_ready = True
                                        break

                                    _raise_if_interrupted(expected_pid=ding_pid, fg_locked=True)
                                    ua.SendKeys("{Down}", waitTime=0.10)
                                    ua.SendKeys("{Enter}", waitTime=0.22)
                                    if _wait_search_closed(loops=6, step=0.18):
                                        chat_ready = True
                                        break
                                    kw_idx += 1

                                if chat_ready:
                                    break
                                ua.SendKeys("{Esc}", waitTime=0.18)
                                if all_cands_seen_no_match and set_cands:
                                    confirmed_no_match = True
                                    break
                                log("  本轮未进入目标会话，关闭搜索框后重试…")
                                _sleep_watch_ding(0.35, expected_pid=ding_pid, fg_locked=True)
                    except DingTalkInterrupted as e_intr:
                        interrupted_act = _pause_for_dingtalk_interruption(str(e_intr))
                        if interrupted_act == "skipped":
                            return "skipped"
                        continue

                    if not chat_ready:
                        if confirmed_no_match:
                            if bool((cfg or {}).get("dt_ai_screen_verify", True)):
                                ding_snap = _fresh_ding() or ding
                                h_dec = _ask_human_confirm_mismatch(
                                    "未找到与目标完全一致的搜索项",
                                    f"钉钉搜索「{search_name}」后未找到完全一致的对象，请看截图确认是否发当前窗口、改发其他群或取消",
                                    ding_obj=ding_snap,
                                )
                                try:
                                    ua.SendKeys("{Esc}", waitTime=0.15)
                                except Exception:
                                    pass
                                if h_dec.get("action") == "confirm_send":
                                    log(f"  ✅ [本人看图确认 · 继续发送] 您已确认发给当前钉钉会话（目标标注「{search_name}」）")
                                    skip_search = True
                                    chat_ready = True
                                elif h_dec.get("action") == "switch_target" and target_switch_hops < 4 and _apply_new_target_switch(h_dec.get("new_target")):
                                    continue
                                else:
                                    clear_clipboard_windows()
                                    log(f"  🛑 [本人已取消发送] 搜索「{search_name}」未匹配，已按您的确认取消本次发送")
                                    return "cancelled"
                            else:
                                raise RuntimeError(
                                    f"在数据文件与钉钉通讯录中均未找到与「{search_name}」一对一匹配的联系人或群聊（已安全拦截，与文件传输助手无关，不会误发给文件小助手）"
                                )
                        if not auto_heal_used["search"]:
                            auto_heal_used["search"] = True
                            heal_s = ai_auto_debug_and_heal(
                                cfg,
                                "dingtalk_search",
                                RuntimeError(f"未能自动搜索进入群聊「{search_name}」"),
                                extra={"search_name": search_name},
                            )
                            if heal_s.get("can_auto_fix"):
                                if "dismiss_popups" in heal_s.get("actions", []):
                                    try:
                                        ua.SendKeys("{Esc}", waitTime=0.2)
                                        _close_link_box()
                                    except Exception:
                                        pass
                                if "activate_dingtalk_im" in heal_s.get("actions", []):
                                    _force_front()
                                    sleep_check(0.35)
                                continue
                        if not recover_interactive:
                            raise RuntimeError(f"没能定位到钉钉会话「{search_name}」，已停止发送")
                        issue = diagnose_issue(
                            "dingtalk_search",
                            extra={"search_name": search_name, "can_skip_target": (total > 1)},
                        )
                        choice = wait_for_recovery(issue["title"], issue["detail"], issue["hint"], issue["actions"])
                        resumed_by_user = True
                        allow_auto_launch = True
                        if choice == "skip_target":
                            return "skipped"
                        if choice == "skip_search":
                            skip_search = True
                        else:
                            skip_search = False
                            continue
                else:
                    log("  用户已手动打开目标群聊，跳过自动搜索，直接定位发送框…")

                # 发前自动由 AI 看桌面钉钉顶栏核对：一致确定发送，不一致或未确认安全则自动取消发送
                do_screen_verify = bool((cfg or {}).get("dt_ai_screen_verify", True))
                if not skip_search and (do_screen_verify or not opened_via_strict_uia):
                    ding_hdr = _fresh_ding() or ding
                    if ding_hdr is not None:
                        hdr_res = _ai_vision_verify_opened_chat(search_name, ding_hdr.BoundingRectangle, cfg=cfg)
                        same_flag = hdr_res.get("same_as_target")
                        if same_flag is False:
                            opened_lbl = hdr_res.get("opened_name") or "其他会话"
                            vision_hdr_rejects += 1
                            if do_screen_verify and vision_hdr_rejects >= 2:
                                h_dec2 = _ask_human_confirm_mismatch(
                                    opened_lbl,
                                    hdr_res.get("reason") or f"当前打开的是「{opened_lbl}」，与目标「{search_name}」不一致",
                                    ding_obj=ding_hdr,
                                )
                                if h_dec2.get("action") == "confirm_send":
                                    log(f"  ✅ [本人看图确认 · 继续发送] 您已确认发给当前对象「{opened_lbl}」")
                                elif h_dec2.get("action") == "switch_target" and target_switch_hops < 4 and _apply_new_target_switch(h_dec2.get("new_target")):
                                    try:
                                        ua.SendKeys("{Esc}", waitTime=0.18)
                                    except Exception:
                                        pass
                                    continue
                                else:
                                    clear_clipboard_windows()
                                    try:
                                        ua.SendKeys("{Esc}", waitTime=0.18)
                                    except Exception:
                                        pass
                                    log(f"  🛑 [本人已取消发送] 当前钉钉对象「{opened_lbl}」与目标「{search_name}」不一致，已按您的确认取消发送")
                                    return "cancelled"
                            elif not do_screen_verify and vision_hdr_rejects >= 2:
                                raise RuntimeError(
                                    f"AI 桌面识屏连续确认当前打开的会话（「{opened_lbl}」）与目标「{search_name}」不一致，已安全拦截发送"
                                )
                            else:
                                log(f"  ⚠️ [AI 桌面顶栏拦截] 当前打开的是「{opened_lbl}」，与想发送的「{search_name}」不一致（{hdr_res.get('reason') or '名称不符'}），已阻止粘贴并重新搜索")
                                try:
                                    ua.SendKeys("{Esc}", waitTime=0.18)
                                except Exception:
                                    pass
                                skip_search = False
                                continue
                        elif same_flag is True:
                            op_nm = hdr_res.get("opened_name") or search_name
                            _save_verified_dingtalk_target(search_name, op_nm)
                            log(f"  ✅ [AI 桌面自动核对通过] 确认当前钉钉对象「{op_nm}」与目标「{search_name}」一致，确定发送！")
                        elif do_screen_verify:
                            if opened_via_strict_uia:
                                log(f"  ✅ 钉钉控件树已一对一锁定「{search_name}」（视觉通道当前离线），确定继续发送")
                            else:
                                h_dec3 = _ask_human_confirm_mismatch(
                                    hdr_res.get("opened_name") or "未能自动识别的会话",
                                    hdr_res.get("reason") or "视觉模型暂未明确确认顶栏名称是否一致",
                                    ding_obj=ding_hdr,
                                )
                                if h_dec3.get("action") == "confirm_send":
                                    log(f"  ✅ [本人看图确认 · 继续发送] 您已确认发给当前钉钉会话（目标标注「{search_name}」）")
                                elif h_dec3.get("action") == "switch_target" and target_switch_hops < 4 and _apply_new_target_switch(h_dec3.get("new_target")):
                                    try:
                                        ua.SendKeys("{Esc}", waitTime=0.18)
                                    except Exception:
                                        pass
                                    continue
                                else:
                                    clear_clipboard_windows()
                                    try:
                                        ua.SendKeys("{Esc}", waitTime=0.18)
                                    except Exception:
                                        pass
                                    log(f"  🛑 [本人已取消发送] 未确认「{search_name}」对象身份，已按您的确认取消发送")
                                    return "cancelled"

                image_pasted_ok = False
                text_pasted_ok = False
                try:
                    with hands_off_guard(f"钉钉「{search_name}」中进行粘贴数据与静音发送"):
                        check_stop()
                        _force_front()
                        awakened_once = True
                        _, ding_pid, _ = _sleep_watch_ding(0.35, expected_pid=ding_pid, fg_locked=False)
                        clear_clipboard_windows()
                        log("  已打开目标会话，准备粘贴图片")

                        # 若有残留的弹出菜单（如上一轮静音下拉未退出），先按 Esc 收起避免抢占键盘输入
                        ding_pre, ding_pid, ding_hwnd_pre = _raise_if_interrupted(expected_pid=ding_pid, fg_locked=False)
                        if (ding_hwnd_pre and _is_win_in_menu_mode(ding_hwnd_pre)) or _enum_top_windows(
                            target_classes=("im_chat::QuiMenu",), target_titles=None
                        ):
                            try:
                                ua.SendKeys("{Esc}", waitTime=0.2)
                                sleep_check(0.22)
                            except Exception as e_esc_dt:
                                if _is_stop_exception(e_esc_dt):
                                    raise

                        # 按用户设定及训练优化结果等待 N 秒让钉钉聊天框加载完成再粘贴（纯文字直发极速免等）
                        try:
                            paste_delay = int((dt_strat or {}).get("effective_paste_delay_sec", cfg.get("dt_paste_delay_sec", 3)))
                        except Exception:
                            paste_delay = 3
                        if str(text_only_message or "").strip():
                            if paste_delay > 0:
                                _sleep_watch_ding(min(0.25, float(paste_delay)), expected_pid=ding_pid, fg_locked=False)
                        elif paste_delay > 0:
                            log(f"  ⏳ 等待 {paste_delay} 秒让钉钉聊天框加载就绪后再粘贴（可在「钉钉投递」调整秒数）…")
                            _sleep_watch_ding(paste_delay, expected_pid=ding_pid, fg_locked=False)

                        # 4) 精准定位待发送框 -> 清空原有内容 -> 按一次 Enter 激活并核对光标位置
                        pt_cfg = cfg.get("pipeline_tuning") if isinstance(cfg.get("pipeline_tuning"), dict) else {}
                        compose_order = str(pt_cfg.get("compose_step_order") or "image_text_silent")
                        silent_order = str((dt_strat or {}).get("silent_order") or pt_cfg.get("silent_step_order") or "uia_first")
                        focus_order_list = list((dt_strat or {}).get("focus_order") or DEFAULT_DT_FOCUS_STRATEGIES)
                        paste_order_list = list((dt_strat or {}).get("paste_order") or DEFAULT_DT_PASTE_METHODS)

                        active_focus_strat, active_compose_rect = _prepare_chat_compose_box()
                        active_paste_method = paste_order_list[0] if paste_order_list else "win32_sendinput_ctrl_v"

                        def _do_paste_image_step():
                            nonlocal image_pasted_ok, active_focus_strat, active_compose_rect, active_paste_method
                            combos = []
                            for idx_p in range(max(4, len(focus_order_list))):
                                fs = focus_order_list[min(idx_p, len(focus_order_list) - 1)] if idx_p > 0 else active_focus_strat
                                pm = paste_order_list[idx_p % len(paste_order_list)]
                                combos.append((fs, pm))
                            max_tries = min(4, len(combos))
                            for paste_try in range(max_tries):
                                fs_try, pm_try = combos[paste_try]
                                if paste_try > 0:
                                    preserve_txt = (compose_order == "text_silent_image" and text_pasted_ok)
                                    active_focus_strat, active_compose_rect = _prepare_chat_compose_box(
                                        force_strategy=fs_try,
                                        clear_box=not preserve_txt,
                                    )
                                ding_cur, _, hwnd_cur = _raise_if_interrupted(expected_pid=ding_pid, fg_locked=True)
                                r_img = ding_cur.BoundingRectangle
                                if compose_order == "text_silent_image":
                                    if not _win32_send_single_vk(0x23):
                                        ua.SendKeys("{End}", waitTime=0.10)
                                _ensure_not_in_search_box(r_img)
                                before_patch = None
                                after_patch = None
                                try:
                                    before_patch = (
                                        _capture_dt_input_patch(r_img, input_rect=active_compose_rect)
                                        if (hwnd_cur and int(user32.GetForegroundWindow() or 0) == hwnd_cur)
                                        else None
                                    )
                                    if not set_clipboard_image_from_file(
                                        tmp_img,
                                        cached_scaled_path=scaled_send_path,
                                        cached_png_bytes=scaled_png_bytes,
                                    ):
                                        raise RuntimeError("图片恢复失败，已停止粘贴")
                                    sleep_check(0.14)

                                    _send_paste_shortcut(method=pm_try, compose_rect=active_compose_rect)
                                    sleep_check(0.85)

                                    converted = False
                                    for _ in range(5):
                                        sleep_check(0.18)
                                        _raise_if_interrupted(expected_pid=ding_pid, fg_locked=False)
                                        tops = _enum_top_windows(
                                            target_classes=("ddesign::ComplexModal",), target_titles=None)
                                        if tops:
                                            modal = _safe_control_from_handle(ua, tops[0][0])
                                            if modal is not None:
                                                for btn_lbl in ("转为图片", "确定", "是", "Yes", "YES", "OK", "发送", "继续", "好", "确认"):
                                                    btn = modal.ButtonControl(searchDepth=12, Name=btn_lbl)
                                                    if btn.Exists(0.30):
                                                        btn.Click()
                                                        converted = True
                                                        sleep_check(0.45)
                                                        break
                                            break
                                    sleep_check(0.30)
                                    after_patch = (
                                        _capture_dt_input_patch(r_img, input_rect=active_compose_rect)
                                        if before_patch is not None
                                        else None
                                    )
                                    changed_img = converted or _did_dt_input_change(before_patch, after_patch)
                                finally:
                                    for _pi in (before_patch, after_patch):
                                        if _pi is not None and hasattr(_pi, "close"):
                                            try:
                                                _pi.close()
                                            except Exception:
                                                pass
                                if changed_img:
                                    active_paste_method = pm_try
                                    record_run_trace_event("paste_ok", pm_try, {"focus": active_focus_strat, "attempt": paste_try + 1})
                                    if converted:
                                        log(f"  ✅ 已自动点「转为图片」并确认图片粘贴进入钉钉输入框（{active_focus_strat} + {pm_try}）")
                                    else:
                                        log(f"  ✅ 已确认图片粘贴进入钉钉输入框（{active_focus_strat} + {pm_try}）")
                                    image_pasted_ok = True
                                    break
                                record_run_trace_event("paste_fail", pm_try, {"focus": active_focus_strat, "reason": "画面未变化"})
                                if paste_try < max_tries - 1:
                                    next_fs, next_pm = combos[paste_try + 1]
                                    log(
                                        f"  ⚠️ 粘贴未引起输入区变化（{active_focus_strat}+{pm_try}），"
                                        f"正在切换下一组合（{next_fs}+{next_pm}）立即重聚焦并补粘…"
                                    )
                                    sleep_check(1.1)

                            if not image_pasted_ok:
                                raise RuntimeError("图片粘贴后输入框画面未变化，请把「打开会话后等待秒数」调大一点或手动打开会话重试")

                        def _do_paste_text_step():
                            nonlocal text_pasted_ok, active_compose_rect
                            ding_txt, _, hwnd_txt = _raise_if_interrupted(expected_pid=ding_pid, fg_locked=True)
                            r_txt = ding_txt.BoundingRectangle
                            _focus_chat_input(ding_txt, r_txt, strategy_hint=active_focus_strat)
                            _sleep_watch_ding(0.16, expected_pid=ding_pid, fg_locked=True)
                            if not _win32_send_single_vk(0x23):
                                try:
                                    ua.SendKeys("{End}", waitTime=0.12)
                                except Exception as e_end_t:
                                    if _is_stop_exception(e_end_t):
                                        raise
                            _ensure_not_in_search_box(r_txt)
                            before_txt_patch = None
                            after_txt_patch = None
                            try:
                                before_txt_patch = (
                                    _capture_dt_input_patch(r_txt, input_rect=active_compose_rect)
                                    if (hwnd_txt and int(user32.GetForegroundWindow() or 0) == hwnd_txt)
                                    else None
                                )
                                paste_method_txt = "win32_sendinput_ctrl_v" if active_paste_method == "context_menu_paste" else active_paste_method
                                ok_clip = _paste_text(text_msg, verify=True, method=paste_method_txt)
                                after_txt_patch = (
                                    _capture_dt_input_patch(r_txt, input_rect=active_compose_rect)
                                    if before_txt_patch is not None
                                    else None
                                )
                                need_unicode_fb = (not ok_clip) or (
                                    before_txt_patch is not None
                                    and after_txt_patch is not None
                                    and not _did_dt_input_change(before_txt_patch, after_txt_patch)
                                )
                            finally:
                                for _pt in (before_txt_patch, after_txt_patch):
                                    if _pt is not None and hasattr(_pt, "close"):
                                        try:
                                            _pt.close()
                                        except Exception:
                                            pass
                            if need_unicode_fb:
                                log("  ⚠️ 文字粘贴未检测到输入变化或剪贴板繁忙，改用 Unicode 直打注入后缀文案…")
                                _focus_chat_input(ding_txt, r_txt, strategy_hint=active_focus_strat)
                                sleep_check(0.12)
                                if not _win32_send_single_vk(0x23):
                                    ua.SendKeys("{End}", waitTime=0.10)
                                _type_unicode(text_msg)
                                record_run_trace_event("text_method", "unicode_type", {"ok": True})
                            else:
                                record_run_trace_event("text_method", paste_method_txt, {"ok": True})
                            text_pasted_ok = True
                            _sleep_watch_ding(0.22, expected_pid=ding_pid, fg_locked=True)

                        def _do_toggle_silent_step():
                            global _dt_silent_was_enabled
                            if do_silent:
                                ok_sil = _enable_dingtalk_silent_send(silent_order=silent_order)
                                if ok_sil:
                                    _dt_silent_was_enabled = True
                                record_run_trace_event("silent_ok" if ok_sil else "silent_fail", "image" if silent_order == "arrow_img_then_uia" else "uia")
                            else:
                                if not _reset_residual_dt_silent_if_needed():
                                    log("  未开启静音发送，跳过点击静音按钮")

                        if str(text_only_message or "").strip():
                            image_pasted_ok = True
                            _do_paste_text_step()
                            _do_toggle_silent_step()
                        elif do_silent and compose_order == "text_silent_image":
                            log("  🔄 当前启用防卡顿顺序：先贴文字唤醒输入栏 -> 点设静音 -> 最后贴大图")
                            _do_paste_text_step()
                            _do_toggle_silent_step()
                            _do_paste_image_step()
                        else:
                            _do_paste_image_step()
                            _do_paste_text_step()
                            _do_toggle_silent_step()

                        # 贴好图片、日期与后缀后，等待可调秒数再直接按 Enter 发送（未开静音时不再切焦点点按钮）
                        try:
                            send_wait_sec = max(0, min(30, int(round(float(cfg.get("dt_send_wait_sec", 5))))))
                        except Exception:
                            send_wait_sec = 5
                        if not str(text_only_message or "").strip() and cfg.get("dt_auto_send", False) and send_wait_sec > 0:
                            log(f"  ⏳ 截图与文案已写入钉钉对话框，等待 {send_wait_sec} 秒后直接按 Enter 发送（可在「钉钉投递」调整秒数）…")
                            _sleep_watch_ding(send_wait_sec, expected_pid=ding_pid, fg_locked=False)
                        else:
                            _sleep_watch_ding(0.18 if not do_silent else 0.28, expected_pid=ding_pid, fg_locked=False)
                        if _is_search_popup_open():
                            ding_mid = _fresh_ding()
                            _ensure_not_in_search_box(ding_mid.BoundingRectangle if ding_mid is not None else None)
                        ding_commit, ding_pid, _ = _raise_if_interrupted(expected_pid=ding_pid, fg_locked=True)

                        if cfg.get("dt_auto_send", False):
                            ok_c_focus, _ = _is_focus_in_chat_compose(ding_commit, ding_commit.BoundingRectangle, compose_rect=active_compose_rect)
                            if not ok_c_focus:
                                _focus_chat_input(ding_commit, ding_commit.BoundingRectangle, strategy_hint=active_focus_strat)
                                sleep_check(0.14)
                            sent_via_btn = _commit_dingtalk_send(ding_commit, input_rect=active_compose_rect)
                            silent_word = "静音" if do_silent else "常规"
                            extra_tip = "（含发送按钮补发确认）" if sent_via_btn else ""
                            log(f"  已{silent_word}发送图片和文字到「{search_name}」：{text_msg}{extra_tip}")
                        else:
                            silent_tip = "并设为「静音发送」" if do_silent else "（常规提醒）"
                            clear_tip = "已清空旧草稿、" if cfg.get("dt_clear_before_send", True) else ""
                            log(f"  ✅ {clear_tip}在「{search_name}」贴好图片与「{text_msg}」{silent_tip}，光标已就位，按设置暂不发送，请检查后按 Enter")
                        return "sent"
                except DingTalkInterrupted as e_intr:
                    act_res = _pause_for_dingtalk_interruption(str(e_intr))
                    if act_res == "skipped":
                        return "skipped"
                    continue
                except Exception as e_paste:
                    if "手动停止" in str(e_paste):
                        raise
                    if not auto_heal_used["paste"]:
                        auto_heal_used["paste"] = True
                        heal_p = ai_auto_debug_and_heal(
                            cfg,
                            "dingtalk_paste",
                            e_paste,
                            extra={
                                "search_name": search_name,
                                "image_pasted": image_pasted_ok,
                                "text_pasted": text_pasted_ok,
                            },
                        )
                        if heal_p.get("can_auto_fix"):
                            acts_p = heal_p.get("actions", [])
                            record_run_trace_event("heal_applied", heal_p.get("observation") or "贴图阶段自动排障", {"stage": "dingtalk_paste", "actions": acts_p})
                            if "dismiss_popups" in acts_p:
                                try:
                                    ua.SendKeys("{Esc}", waitTime=0.2)
                                    _close_link_box()
                                except Exception:
                                    pass
                            if "activate_dingtalk_im" in acts_p:
                                _force_front()
                                sleep_check(0.35)
                            if (
                                image_pasted_ok
                                and text_pasted_ok
                                and any(a in acts_p for a in ("enable_silent_uia", "click_send_now", "focus_chat_input"))
                            ):
                                try:
                                    ding_h, ding_pid, _ = _raise_if_interrupted(expected_pid=ding_pid, fg_locked=False)
                                    _focus_chat_input(ding_h, ding_h.BoundingRectangle)
                                    sleep_check(0.2)
                                    if do_silent:
                                        if _enable_dingtalk_silent_send():
                                            globals()["_dt_silent_was_enabled"] = True
                                    else:
                                        if not _reset_residual_dt_silent_if_needed():
                                            log("  未开启静音发送，跳过点击静音按钮")
                                    try:
                                        sw2 = max(0, min(30, int(round(float(cfg.get("dt_send_wait_sec", 5))))))
                                    except Exception:
                                        sw2 = 5
                                    _sleep_watch_ding(float(sw2 if (cfg.get("dt_auto_send", False) and sw2 > 0) else 0.5), expected_pid=ding_pid, fg_locked=False)
                                    ding_h2, ding_pid, _ = _raise_if_interrupted(expected_pid=ding_pid, fg_locked=True)
                                    if cfg.get("dt_auto_send", False):
                                        ok_h_focus, _ = _is_focus_in_chat_compose(ding_h2, ding_h2.BoundingRectangle, compose_rect=active_compose_rect)
                                        if not ok_h_focus:
                                            _focus_chat_input(ding_h2, ding_h2.BoundingRectangle)
                                            sleep_check(0.16)
                                        sent_b2 = _commit_dingtalk_send(ding_h2, input_rect=active_compose_rect)
                                        silent_w2 = "静音" if do_silent else "常规"
                                        tip_b2 = "（含发送按钮补发确认）" if sent_b2 else ""
                                        log(f"  ✅ [AI 自动调试] 已自动排障并{silent_w2}发送到「{search_name}」：{text_msg}{tip_b2}")
                                    else:
                                        log(f"  ✅ [AI 自动调试] 已排障并完成贴图设静音，请检查后按 Enter")
                                    return "sent"
                                except Exception as e_heal_fail:
                                    if "手动停止" in str(e_heal_fail):
                                        raise
                                    log(f"  ℹ️ [AI 自动调试] 补发尝试未一次完成（{human_error(e_heal_fail)}），转入常规重贴流程…")
                            skip_search = (("retry_paste" in acts_p) or (image_pasted_ok is False)) and ("reopen_search" not in acts_p)
                            continue
                    if not recover_interactive:
                        raise
                    if _find() is None:
                        act_res = _pause_for_dingtalk_interruption("检测到钉钉窗口已关闭或隐藏")
                        if act_res == "skipped":
                            return "skipped"
                        continue
                    issue = diagnose_issue(
                        "dingtalk_paste",
                        e_paste,
                        extra={"search_name": search_name, "can_skip_target": (total > 1)},
                    )
                    choice = wait_for_recovery(issue["title"], issue["detail"], issue["hint"], issue["actions"])
                    resumed_by_user = True
                    allow_auto_launch = True
                    if choice == "skip_target":
                        return "skipped"
                    if choice == "retry_paste":
                        skip_search = True
                    else:
                        skip_search = False
                    continue

        # 3) 多目标依次投递主循环
        sent_targets = []
        skipped_targets = []
        cancelled_targets = []
        total_targets = len(targets)
        for idx, tgt in enumerate(targets, 1):
            res, actual_tgt = _send_one_target_with_actual(idx, total_targets, tgt)
            if res == "cancelled":
                cancelled_targets.append(actual_tgt)
                log(f"  🛑 已自动取消发送到「{actual_tgt}」（AI 桌面核对对象不一致，未粘贴发送）")
            elif res == "skipped":
                skipped_targets.append(actual_tgt)
                log(f"  ⚠️ 已跳过会话「{actual_tgt}」，继续下一个目标")
            else:
                sent_targets.append(actual_tgt)
            if idx < total_targets:
                sleep_check(0.8)

        if total_targets > 1 or cancelled_targets:
            if skipped_targets or cancelled_targets:
                msg_parts = [f"成功 {len(sent_targets)} 个（{'、'.join(sent_targets) if sent_targets else '无'}）"]
                if cancelled_targets:
                    msg_parts.append(f"自动取消 {len(cancelled_targets)} 个（{'、'.join(cancelled_targets)}）")
                if skipped_targets:
                    msg_parts.append(f"跳过 {len(skipped_targets)} 个（{'、'.join(skipped_targets)}）")
                log("  多会话投递完成：" + "，".join(msg_parts))
            else:
                log(f"  ✅ 多会话投递完成：全部 {len(sent_targets)} 个目标已处理")

        if not sent_targets and cancelled_targets:
            log("  🛑 本轮钉钉发送已全部自动取消（AI 桌面核对对象不一致，未发出任何消息）")
            return False

        if sent_targets and cfg.get("dt_auto_send", False) and cfg.get("dt_minimize_after_send", True):
            sleep_check(0.8)
            w_min = _find()
            if w_min is not None:
                try:
                    min_hwnd = int(w_min.NativeWindowHandle or 0)
                except Exception:
                    min_hwnd = 0
                if min_hwnd:
                    user32.ShowWindow(min_hwnd, 6)  # SW_MINIMIZE
                    log("  ✅ 发送完成，已自动最小化钉钉窗口")
        return True
    except Exception as e:
        log(f"  钉钉操作失败：{human_error(e)}")
        raise
    finally:
        _active_dt_hwnd = 0
        try:
            if tmp_img.exists():
                tmp_img.unlink()
            send_tmp = tmp_img.with_name(tmp_img.stem + "_send.png")
            if send_tmp.exists():
                send_tmp.unlink()
        except Exception:
            pass


def send_dingtalk_text_message(cfg, target_names, message_text, silent=True, recover_interactive=False):
    """直接将指定纯文字汇报静音发送到一个或多个钉钉联系人/群聊（支持 AI 搜索自确认）。"""
    import copy
    c = normalize_config(copy.deepcopy(cfg if isinstance(cfg, dict) else {}))
    raw_list = target_names if isinstance(target_names, (list, tuple)) else [target_names]
    tgts = [str(t).strip() for t in raw_list if str(t).strip()]
    if not tgts:
        tgts = get_enabled_dt_targets(c) or ["文件小助手"]
    c["enable_dingtalk"] = True
    c["dt_auto_send"] = True
    c["dt_silent_send"] = bool(silent)
    c["dt_targets"] = [{"name": t, "enabled": True} for t in tgts]
    c["dt_search"] = tgts[0]
    c["point_overrides"] = {}
    c["suffix_rules"] = []
    c["dt_use_actual_clock"] = False
    return send_dingtalk_image(
        c,
        recover_interactive=recover_interactive,
        text_only_message=str(message_text or "").strip(),
    )


def _open_and_login_wps(context, page, wps_url, recover_interactive=True):
    """打开 WPS 在线表并完成登录检测（失败时保持浏览器打开并暂停等待恢复）。"""
    while True:
        check_stop()
        try:
            cleanup_wps_tabs(context, wps_url=wps_url)
            goto_with_retry(page, wps_url)
            page.wait_for_selector("input.edit-box", timeout=20000)
            sleep_check(1)
            if check_wps_logged_in(page):
                log("  表格已登录，直接继续")
            else:
                if not wait_wps_login(page, max_wait=1200):
                    raise RuntimeError("WPS 登录超时：请在浏览器里扫码登录后继续")
            return
        except Exception as e_wps:
            if "手动停止" in str(e_wps) or not recover_interactive:
                raise
            issue = diagnose_issue("wps_open", e_wps)
            wait_for_recovery(issue["title"], issue["detail"], issue["hint"], issue["actions"])


def _capture_summary_with_recovery(page, temp_dir=None, enable_capture=True, recover_interactive=True,
                                   failures=None, log_prefix="5/5", cfg=None, trigger_lt=None, trigger_hm=None,
                                   after_data_paste=False, skip_summary_presets=False, force_write_r4=False,
                                   paste_lt=None):
    """切到「数据汇总」表、校准筛选项（或刷底表模式下保留其余格位仅写 R4）并可选复制为图片。"""
    wps_heal_used = False
    while True:
        check_stop()
        try:
            set_step("正在切换到数据汇总表…")
            log(f"{log_prefix} 切到数据汇总表...")
            if not switch_wps_sheet(page, "数据汇总"):
                raise RuntimeError("切换到数据汇总表失败，请检查底部标签栏")
            use_cfg = cfg if isinstance(cfg, dict) else load_config()
            if skip_summary_presets:
                log("  按刷底表模式保留【数据汇总】其余格位不变，跳过格位预设写入")
            else:
                set_step("正在校准汇总表筛选项…")
                apply_wps_summary_presets(page, use_cfg, lt=trigger_lt, hm_override=trigger_hm, after_data_paste=after_data_paste)
            if force_write_r4:
                import copy as _copy_r4
                set_step("正在写入 R4 更新时间…")
                ut_cfg = _copy_r4.deepcopy(use_cfg)
                raw_ut0 = ut_cfg.get("update_time_cell") if isinstance(ut_cfg.get("update_time_cell"), dict) else {}
                ut_cfg["update_time_cell"] = {
                    **raw_ut0,
                    "enabled": True,
                    "cell": (raw_ut0.get("cell") or "R4"),
                    "only_after_paste": False,
                }
                apply_wps_update_timestamp(page, ut_cfg, after_data_paste=True, now_lt=paste_lt)
            else:
                apply_wps_update_timestamp(page, use_cfg, after_data_paste=after_data_paste, now_lt=paste_lt)
            pt_wps = use_cfg.get("pipeline_tuning") if isinstance(use_cfg.get("pipeline_tuning"), dict) else {}
            try:
                wps_wait_s = max(3, min(15, int(round(float(pt_wps.get("wps_refresh_wait_sec", 5))))))
            except Exception:
                wps_wait_s = 5
            set_step("正在等待汇总表刷新…")
            log(f"  等待 {wps_wait_s} 秒，让数据汇总表完成刷新…")
            sleep_check(float(wps_wait_s))

            if temp_dir is not None:
                screenshot_path = temp_dir / "数据汇总.png"
                page.screenshot(path=str(screenshot_path))
                log("✅ 数据汇总表已准备好")

            if enable_capture:
                check_stop()
                set_step("正在复制表格图片…")
                capture_wps_region(page, cfg=use_cfg)
            return False
        except Exception as e_cap:
            if "手动停止" in str(e_cap):
                raise
            if not wps_heal_used:
                wps_heal_used = True
                use_cfg_heal = cfg if isinstance(cfg, dict) else load_config()
                heal_w = ai_auto_debug_and_heal(use_cfg_heal, "wps_capture", e_cap, page=page)
                if heal_w.get("can_auto_fix"):
                    try:
                        page.keyboard.press("Escape")
                        sleep_check(1.0)
                    except Exception as e_cap_esc:
                        if _is_stop_exception(e_cap_esc):
                            raise
                    continue
            if not recover_interactive:
                if failures is not None:
                    tip = human_error(e_cap)
                    failures.append("复制表格图片失败：" + tip)
                    log(f"  复制图片没成功：{tip}")
                    log_error(e_cap, "复制表格图片失败")
                    return True
                raise
            issue = diagnose_issue("wps_capture", e_cap)
            choice = wait_for_recovery(issue["title"], issue["detail"], issue["hint"], issue["actions"])
            while choice == "manual_copied":
                if clipboard_has_image():
                    log("  ✅ 检测到剪贴板已含手动复制的表格图片，准备发钉钉")
                    return False
                choice = wait_for_recovery(
                    "暂未在剪贴板检测到表格图片",
                    "请先在浏览器的【数据汇总】里选中区域 -> 右键「复制为图片」（等 1 秒确认复制成功），再点【我已复制好·继续】。",
                    "按键提示：复制好图片后点【我已复制好·继续】，或点【重试自动截图】。",
                    actions=[
                        {"id": "manual_copied", "label": "我已复制好·继续", "style": "primary"},
                        {"id": "retry", "label": "重试自动截图", "style": "secondary"},
                        {"id": "skip", "label": "跳过截图发钉钉", "style": "secondary"},
                    ],
                )
            if choice == "skip":
                log("  ⚠️ 用户选择跳过截图与钉钉发送")
                return True


def _with_keep_awake(cfg, reason, fn):
    """按配置在任务执行期间持有防休眠状态及全局快捷键监听，结束或异常退出时安全释放。"""
    keep_awake = bool((cfg or {}).get("prevent_sleep", True))
    hotkeys_on = reason in ("run_once", "capture_only")
    if keep_awake:
        set_keep_awake(True, reason=reason)
    if hotkeys_on:
        _HOTKEY_MGR.start()
    try:
        return fn()
    finally:
        if hotkeys_on:
            _HOTKEY_MGR.stop()
        if keep_awake:
            set_keep_awake(False, reason=reason)


def run_once(round_num, cfg=None, trigger_lt=None, trigger_hm=None, recover_interactive=True, raw_sync_only=False):
    reset_stop()
    if cfg is None:
        cfg = load_config()
    return _with_keep_awake(
        cfg,
        "run_once",
        lambda: _run_once_impl(
            round_num,
            cfg=cfg,
            trigger_lt=trigger_lt,
            trigger_hm=trigger_hm,
            recover_interactive=recover_interactive,
            raw_sync_only=raw_sync_only,
        ),
    )


def run_raw_sync_only(round_num="刷底表", cfg=None, recover_interactive=True):
    """仅下载三份后台表并粘贴到 WPS，保留【数据汇总】其余格位不变、仅写 R4 更新时间，不截图不发群。"""
    return run_once(round_num, cfg=cfg, recover_interactive=recover_interactive, raw_sync_only=True)


def _run_once_impl(round_num, cfg=None, trigger_lt=None, trigger_hm=None, recover_interactive=True, raw_sync_only=False):
    global _last_captured_summary_tsv
    _last_captured_summary_tsv = ""
    start_time = time.time()
    trigger_lt = trigger_lt or time.localtime(start_time)
    start_str = time.strftime("%Y-%m-%d %H:%M:%S")
    if cfg is None:
        cfg = load_config()
    tr_mode = "raw_sync" if raw_sync_only else "full"
    tr_label = f"刷底表({round_num})" if raw_sync_only else f"第{round_num}轮"
    reset_run_training_trace(mode=tr_mode, round_label=tr_label)
    try:
        return _run_once_impl_body(
            round_num=round_num,
            cfg=cfg,
            trigger_lt=trigger_lt,
            trigger_hm=trigger_hm,
            recover_interactive=recover_interactive,
            start_time=start_time,
            start_str=start_str,
            raw_sync_only=raw_sync_only,
        )
    except Exception as exc_tr:
        if "手动停止" not in str(exc_tr):
            try:
                finalize_run_training_summary(
                    cfg,
                    status="err",
                    mode=tr_mode,
                    round_label=tr_label,
                    duration_sec=int(time.time() - start_time),
                    error_detail=classify_error_detail(exc_tr),
                )
            except Exception:
                pass
        raise


def _run_once_impl_body(round_num, cfg, trigger_lt, trigger_hm, recover_interactive, start_time, start_str, raw_sync_only=False):
    luya_user = (cfg.get("luya_user") or LUYA_USER).strip() or LUYA_USER
    luya_pass = cfg.get("luya_pass") or LUYA_PASS
    wps_url = (cfg.get("wps_url") or WPS_URL).strip() or WPS_URL
    failures = []

    log("=" * 40)
    log(f"📊 开始第 {round_num} 轮，时间 {start_str}")
    log("=" * 40)

    temp_dir = Path.home() / "Desktop" / "数据日报"
    temp_dir.mkdir(exist_ok=True)
    BROWSER_USER_DATA.mkdir(parents=True, exist_ok=True)

    capture_failed = False
    with sync_playwright() as p:
        check_stop()
        set_step("正在打开浏览器…")
        log("正在打开浏览器...")
        context = _launch_context(p)

        try:
            cleanup_wps_tabs(context, wps_url=wps_url)
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(30000)

            # Stage 1: 打开并登录后台（断网/未开VPN/登录失败均停在原地等待修复）
            while True:
                check_stop()
                set_step("正在登录后台…")
                log("1/5 打开后台并登录...")
                try:
                    goto_with_retry(page, "https://bb.luyaapp.live")
                    sleep_check(2)

                    need_login = False
                    try:
                        page.wait_for_selector("input[type='password']", timeout=3000)
                        need_login = True
                    except Exception:
                        pass

                    if need_login:
                        check_stop()
                        log("  检测到登录页，自动填写账号密码...")
                        pwd_sel = "input[type='password']"
                        page.wait_for_selector(pwd_sel, timeout=5000)
                        user_filled = False
                        try:
                            page.fill(f"{pwd_sel} >> xpath=preceding-sibling::input[1]", luya_user, timeout=2000)
                            user_filled = True
                        except Exception:
                            pass
                        if not user_filled:
                            for sel in ["input[type='email']", "input[type='text']:visible",
                                        "input[name='email']", "input[placeholder*='邮箱']",
                                        "input[placeholder*='账号']"]:
                                try:
                                    page.fill(sel, luya_user, timeout=1000)
                                    user_filled = True
                                    break
                                except Exception:
                                    continue
                        if not user_filled:
                            inputs = page.query_selector_all("input:visible")
                            for inp in inputs:
                                inp_type = inp.get_attribute("type") or "text"
                                if inp_type != "password":
                                    inp.fill(luya_user)
                                    user_filled = True
                                    break
                        page.fill(pwd_sel, luya_pass, timeout=2000)
                        btn_clicked = False
                        for sel in ["button[type='submit']", "button:has-text('登录')", "input[type='submit']"]:
                            try:
                                page.click(sel, timeout=1500)
                                btn_clicked = True
                                break
                            except Exception:
                                continue
                        if not btn_clicked:
                            page.keyboard.press("Enter")
                        sleep_check(3)
                        still_login = False
                        try:
                            if page.query_selector("input[type='password']:visible") is not None:
                                still_login = True
                        except Exception:
                            pass
                        if still_login and recover_interactive:
                            issue = diagnose_issue("luya_login", extra={"luya_need_login": True})
                            choice = wait_for_recovery(issue["title"], issue["detail"], issue["hint"], issue["actions"])
                            if choice == "continue":
                                log("  用户确认后台已登录，继续下一步")
                                break
                            continue
                        log("  登录完成")
                    else:
                        log("  已经登录过，跳过")
                    break
                except Exception as e_stage1:
                    if "手动停止" in str(e_stage1) or not recover_interactive:
                        raise
                    cur_url = ""
                    try:
                        cur_url = page.url or ""
                    except Exception:
                        pass
                    st_tag = "luya_login" if "login" in cur_url.lower() else "luya_open"
                    issue = diagnose_issue(st_tag, e_stage1)
                    choice = wait_for_recovery(issue["title"], issue["detail"], issue["hint"], issue["actions"])
                    if choice == "continue":
                        break

            # Stage 2: 按单张表逐份下载（某张表失败只重抓这一张，已下好的表完全保留）
            check_stop()
            set_step("正在下载三份数据…")
            log("2/5 下载三份数据...")
            xlsx_files = []
            for name, url in DATA_URLS:
                while True:
                    check_stop()
                    try:
                        dl_file = download_xlsx(page, name, url, temp_dir)
                        if dl_file:
                            xlsx_files.append((name, dl_file))
                            sleep_check(0.3)
                            break
                        need_rel = False
                        try:
                            if (page.query_selector("input[type='password']:visible") is not None
                                    or "login" in (page.url or "").lower()):
                                need_rel = True
                        except Exception:
                            pass
                        if not recover_interactive:
                            raise RuntimeError(f"数据表【{name}】下载失败")
                        issue = diagnose_issue(
                            "luya_login" if need_rel else "luya_download",
                            RuntimeError(f"未能从页面下载【{name}】的表格数据"),
                            extra={"name": name, "luya_need_login": need_rel},
                        )
                        wait_for_recovery(issue["title"], issue["detail"], issue["hint"], issue["actions"])
                    except Exception as e_dl:
                        if "手动停止" in str(e_dl) or not recover_interactive:
                            raise
                        issue = diagnose_issue("luya_download", e_dl, extra={"name": name})
                        wait_for_recovery(issue["title"], issue["detail"], issue["hint"], issue["actions"])

            # Stage 3: 整理格式（失败时支持重新下载该表或提示排障）
            check_stop()
            set_step("正在整理数据格式…")
            log("3/5 整理数据格式...")
            tsv_files = []
            for name, xlsx_path in xlsx_files:
                while True:
                    check_stop()
                    tsv_path = temp_dir / f"{name}.tsv"
                    try:
                        xlsx_to_tsv(xlsx_path, tsv_path)
                        tsv_files.append((name, tsv_path))
                        break
                    except Exception as e_tsv:
                        if _is_stop_exception(e_tsv) or not recover_interactive:
                            raise
                        issue = diagnose_issue("luya_download", e_tsv, extra={"name": name})
                        choice_t = wait_for_recovery(issue["title"], issue["detail"], issue["hint"], issue["actions"])
                        if choice_t == "skip":
                            log(f"  ⚠️ 用户选择跳过【{name}】格式转换")
                            break
                        src_url = next((u for n, u in DATA_URLS if n == name), None)
                        if src_url:
                            dl_retry = download_xlsx(page, name, src_url, temp_dir)
                            if dl_retry:
                                xlsx_path = dl_retry

            # Stage 4: 打开 WPS 登录并逐表写入
            check_stop()
            set_step("正在写入在线表格…")
            log("4/5 写入在线表格...")
            _open_and_login_wps(context, page, wps_url, recover_interactive=recover_interactive)
            sleep_check(1)
            log("  表格加载完成")

            for name, tsv_path in tsv_files:
                while True:
                    check_stop()
                    try:
                        paste_to_wps(page, name, tsv_path)
                        sleep_check(0.5)
                        break
                    except Exception as e_paste_wps:
                        if "手动停止" in str(e_paste_wps) or not recover_interactive:
                            raise
                        issue = diagnose_issue("wps_paste", e_paste_wps, extra={"name": name})
                        choice = wait_for_recovery(issue["title"], issue["detail"], issue["hint"], issue["actions"])
                        if choice == "skip":
                            log(f"  ⚠️ 用户选择跳过【{name}】写入，继续下一步")
                            break

            paste_done_lt = time.localtime()

            # Stage 5: 切汇总表并截图（若为 raw_sync_only 模式，则保留数据汇总其余格位不动、仅写 R4 时间且不截图）
            if raw_sync_only:
                rs_cfg_run = normalize_config(cfg).get("raw_sync") or {}
                preserve_cells = bool(rs_cfg_run.get("preserve_summary_cells", True))
                write_r4 = bool(rs_cfg_run.get("write_r4_timestamp", True))
                if write_r4 or not preserve_cells:
                    capture_failed = _capture_summary_with_recovery(
                        page,
                        temp_dir=None,
                        enable_capture=False,
                        recover_interactive=recover_interactive,
                        failures=failures,
                        log_prefix="5/5",
                        cfg=cfg,
                        trigger_lt=trigger_lt,
                        trigger_hm=trigger_hm,
                        after_data_paste=True,
                        skip_summary_presets=preserve_cells,
                        force_write_r4=write_r4,
                        paste_lt=paste_done_lt,
                    )
                else:
                    log("5/5 已写入三份底表，按配置跳过【数据汇总】与截图步骤")
            else:
                capture_failed = _capture_summary_with_recovery(
                    page,
                    temp_dir=temp_dir,
                    enable_capture=bool(cfg.get("enable_capture")),
                    recover_interactive=recover_interactive,
                    failures=failures,
                    log_prefix="5/5",
                    cfg=cfg,
                    trigger_lt=trigger_lt,
                    trigger_hm=trigger_hm,
                    after_data_paste=True,
                    paste_lt=paste_done_lt,
                )
            cleanup_wps_tabs(context, wps_url=wps_url)
        finally:
            _set_active_pw_context(None)
            set_step("正在关闭后台浏览器…")
            log("关闭后台浏览器...")
            try:
                context.close()
            except Exception:
                pass

    # Stage 6: 发钉钉（未开启截图或处于 raw_sync_only 或截图失败/跳过时不发）
    if (not raw_sync_only) and cfg.get("enable_capture") and cfg.get("enable_dingtalk") and not capture_failed:
        try:
            check_stop()
            set_step("正在发送到钉钉…")
            send_dingtalk_image(
                cfg,
                trigger_lt=trigger_lt,
                trigger_hm=trigger_hm,
                recover_interactive=recover_interactive,
            )
        except Exception as e:
            if "手动停止" in str(e):
                raise
            tip = human_error(e)
            failures.append("钉钉发送失败：" + tip)
            log(f"  钉钉发送没成功：{tip}")
            log_error(e, "钉钉发送失败")

    if failures:
        raise RuntimeError("；".join(failures))

    if not raw_sync_only:
        try:
            record_report_archive(cfg, round_label=f"第{round_num}轮", summary_tsv=_last_captured_summary_tsv)
        except Exception:
            pass
    notify_last_run()
    end_time = time.time()
    duration = int(end_time - start_time)
    j_mode = "raw_sync" if raw_sync_only else "full"
    j_label = f"刷底表({round_num})" if raw_sync_only else f"第{round_num}轮"
    try:
        record_run_journal(cfg, status="ok", mode=j_mode, round_label=j_label, duration_sec=duration)
    except Exception:
        pass
    try:
        finalize_run_training_summary(cfg, status="ok", mode=j_mode, round_label=j_label, duration_sec=duration)
    except Exception:
        pass
    done_step_msg = "✅ 刷底表完成（三表与 R4 已更新）" if raw_sync_only else "✅ 本轮跑完"
    set_step(done_step_msg)
    log(f"{done_step_msg}，用时 {duration} 秒")

    if (not raw_sync_only) and cfg.get("open_wps_after_run", False):
        log("正在打开数据汇总表给你看...")
        wps_paths = [
            r"C:\Program Files\Kingsoft\WPS Office\office6\wps.exe",
            r"C:\Program Files (x86)\Kingsoft\WPS Office\office6\wps.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Kingsoft\WPS Office\office6\wps.exe"),
        ]
        wps_found = False
        wps_path = None
        for wp in wps_paths:
            if os.path.exists(wp):
                wps_found = True
                wps_path = wp
                break
        if wps_found:
            log("检测到WPS，用WPS打开")
            try:
                subprocess.Popen([wps_path, wps_url])
            except Exception:
                import webbrowser
                webbrowser.open(wps_url)
        else:
            log("没装WPS，用浏览器打开")
            import webbrowser
            webbrowser.open(wps_url)

    return end_time


def run_capture_only(round_num="截图", cfg=None, recover_interactive=True):
    """独立截图发送：不下载、不写入，只处理 WPS 汇总表和钉钉（支持分步恢复）。"""
    reset_stop()
    if cfg is None:
        cfg = load_config()
    return _with_keep_awake(
        cfg,
        "capture_only",
        lambda: _run_capture_only_impl(round_num=round_num, cfg=cfg, recover_interactive=recover_interactive),
    )


def _run_capture_only_impl(round_num="截图", cfg=None, recover_interactive=True):
    global _last_captured_summary_tsv
    _last_captured_summary_tsv = ""
    start_time = time.time()
    trigger_lt = time.localtime(start_time)
    if cfg is None:
        cfg = load_config()
    reset_run_training_trace(mode="capture_only", round_label="独立截图")
    try:
        return _run_capture_only_impl_body(
            round_num=round_num,
            cfg=cfg,
            recover_interactive=recover_interactive,
            start_time=start_time,
            trigger_lt=trigger_lt,
        )
    except Exception as exc_tr:
        if "手动停止" not in str(exc_tr):
            try:
                finalize_run_training_summary(
                    cfg,
                    status="err",
                    mode="capture_only",
                    round_label="独立截图",
                    duration_sec=int(time.time() - start_time),
                    error_detail=classify_error_detail(exc_tr),
                )
            except Exception:
                pass
        raise


def _run_capture_only_impl_body(round_num, cfg, recover_interactive, start_time, trigger_lt):
    wps_url = (cfg.get("wps_url") or WPS_URL).strip() or WPS_URL
    log("=" * 40)
    log(f"📸 开始独立截图发送，时间 {time.strftime('%Y-%m-%d %H:%M:%S')}")
    BROWSER_USER_DATA.mkdir(parents=True, exist_ok=True)

    capture_failed = False
    with sync_playwright() as p:
        check_stop()
        set_step("正在打开WPS汇总表…")
        log("1/3 正在打开 WPS 数据汇总表…")
        context = _launch_context(p)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(30000)
            _open_and_login_wps(context, page, wps_url, recover_interactive=recover_interactive)
            capture_failed = _capture_summary_with_recovery(
                page,
                temp_dir=None,
                enable_capture=True,
                recover_interactive=recover_interactive,
                failures=None,
                log_prefix="2/3",
                cfg=cfg,
                trigger_lt=trigger_lt,
            )
        finally:
            _set_active_pw_context(None)
            try:
                context.close()
            except Exception:
                pass

    if not capture_failed:
        check_stop()
        log("3/3 正在粘贴到钉钉并发送…")
        set_step("正在发送到钉钉…")
        send_dingtalk_image(cfg, trigger_lt=trigger_lt, recover_interactive=recover_interactive)
    else:
        try:
            record_report_archive(cfg, round_label="仅截图", summary_tsv=_last_captured_summary_tsv)
        except Exception:
            pass
    try:
        record_report_archive(cfg, round_label="仅截图", summary_tsv=_last_captured_summary_tsv)
    except Exception:
        pass
    notify_last_run()
    end_time = time.time()
    dur_s = int(end_time - start_time)
    try:
        record_run_journal(
            cfg,
            status="ok" if not capture_failed else "skipped_capture",
            mode="capture_only",
            round_label="独立截图",
            duration_sec=dur_s,
        )
    except Exception:
        pass
    try:
        finalize_run_training_summary(
            cfg,
            status="ok" if not capture_failed else "skipped_capture",
            mode="capture_only",
            round_label="独立截图",
            duration_sec=dur_s,
        )
    except Exception:
        pass
    set_step("✅ 本轮跑完")
    log(f"✅ 独立截图流程完成，用时 {dur_s} 秒")
    return end_time


def show_splash():
    """启动过渡窗：新拟态软质感 + 旋转加载，自检完成、走满后再进主界面。"""
    import threading
    import tkinter as tk
    import neu_ui as neu
    SPLASH_BASE = "#e3e8ef"
    win = tk.Tk()
    win.title("暗号表助手")
    try:
        ico_p = _find_bundle_asset("icon.ico")
        if ico_p is not None:
            win.iconbitmap(str(ico_p))
    except Exception:
        pass
    win.geometry("420x288")
    win.resizable(False, False)
    try:
        win.eval("tk::PlaceWindow . center")
    except Exception:
        pass
    win.configure(bg=SPLASH_BASE)
    ctx = neu.NeuContext(SPLASH_BASE)

    icon = neu.NeuCard(win, ctx, 74, 74, radius=37, pad=12)
    icon.place(relx=0.5, y=28, anchor="n")
    tk.Label(icon.inner, text="📊", bg=SPLASH_BASE,
             font=("Microsoft YaHei UI", 32)).place(relx=0.5, rely=0.5, anchor="center")

    tk.Label(win, text="暗号表助手", bg=SPLASH_BASE, fg="#2d3138",
             font=("Microsoft YaHei UI", 20, "bold")).place(
        relx=0.5, y=140, anchor="n")

    neu.RingSpinner(win, ctx, size=42, color="#007aff").place(
        relx=0.5, y=186, anchor="n")

    tip = tk.Label(win, text="正在检查浏览器组件，请稍候…", bg=SPLASH_BASE,
                   fg="#7a8190", font=("Microsoft YaHei UI", 12))
    tip.place(relx=0.5, y=246, anchor="n")

    done = threading.Event()

    def _ensure():
        try:
            ensure_browser_installed()
        except Exception:
            pass
        done.set()

    def _watch():
        if done.is_set():
            tip.configure(text="准备完成，正在打开…")
            win.after(350, win.destroy)
            return
        win.after(150, _watch)

    threading.Thread(target=_ensure, daemon=True).start()
    win.after(200, _watch)
    win.after(60000, win.destroy)  # 兜底：自检再慢也进入主界面
    win.mainloop()


def _show_settings_window_legacy():
    import threading
    import tkinter as tk
    import neu_ui as neu

    root = tk.Tk()
    root.title("📊暗号表助手")
    root.geometry("720x860")
    root.minsize(720, 760)
    root.resizable(True, True)
    try:
        root.eval("tk::PlaceWindow . center")
    except Exception:
        pass

    # ===== 现代卡片主题：浅色窗口 + 白色内容卡 + 高对比文字 =====
    THEMES = {
        "light": {
            "label": "浅色", "base": "#ffffff", "window": "#f3f6fb",
            "text_main": "#172033", "text_sub": "#8490a5",
        },
        "dark": {
            "label": "深色", "base": "#252932", "window": "#171a21",
            "text_main": "#f1f4fa", "text_sub": "#929caf",
        },
        "warm": {
            "label": "暖色", "base": "#fffaf5", "window": "#f4ede6",
            "text_main": "#423326", "text_sub": "#a08a75",
        },
    }
    BLUE, ORANGE, GREEN, RED = "#2f6fed", "#ff8a34", "#24b47e", "#ef4b55"

    cur_theme = "light"
    TH = THEMES[cur_theme]
    ctx = neu.NeuContext(TH["base"], TH["window"])
    # 普通 tk 标签登记（主题切换时改色）
    lab_main, lab_sub = [], []

    root.configure(bg=TH["window"])
    cfg = load_config()

    def apply_theme(name):
        nonlocal TH, cur_theme
        old = TH
        cur_theme = name
        TH = THEMES[name]
        ctx.set_base(TH["base"])
        ctx.window_hex = TH["window"]
        root.configure(bg=TH["window"])
        ctx.redraw_all()
        accents = {BLUE.lower(), ORANGE.lower(), GREEN.lower(), RED.lower()}

        def _walk(w):
            # 内容卡与窗口背景分别映射，保留白色卡片层次。
            try:
                bg = w.cget("bg").lower()
                if bg == old["base"].lower():
                    w.configure(bg=TH["base"])
                elif bg == old["window"].lower():
                    w.configure(bg=TH["window"])
            except Exception:
                pass
            # 前景：强调色保持，主/次文字色映射到新主题
            try:
                fgl = w.cget("fg").lower()
                if fgl in accents:
                    pass
                elif fgl == old["text_main"].lower():
                    w.configure(fg=TH["text_main"])
                elif fgl == old["text_sub"].lower():
                    w.configure(fg=TH["text_sub"])
            except Exception:
                pass
            for c in w.winfo_children():
                _walk(c)
        _walk(root)
        for b in theme_btns.values():
            b.configure(bg=TH["window"])

    def _on_close():
        try:
            collect_and_save()
        except Exception:
            pass
        try:
            root.destroy()
        except Exception:
            pass
        os._exit(0)
    root.protocol("WM_DELETE_WINDOW", _on_close)

    schedule_running = False
    manual_trigger = False
    stop_flag = False
    capture_running = False

    # ===== 标题区 =====
    head_icon = neu.NeuCard(root, ctx, 52, 52, radius=18, pad=12, outer_bg=True)
    head_icon.place(x=38, y=24)
    tk.Label(head_icon.inner, text="📊", bg=TH["base"],
             font=("Microsoft YaHei UI", 26)).place(relx=0.5, rely=0.5, anchor="center")

    title_lab = tk.Label(root, text="暗号表助手", bg=TH["window"],
                         fg=TH["text_main"], font=("Microsoft YaHei UI", 24, "bold"))
    title_lab.place(x=112, y=30)
    lab_main.append(title_lab)

    # 主题切换（小圆点按钮）
    theme_btns = {}
    tx = 720 - 40

    def _pick_theme(name):
        apply_theme(name)
        for k, b in theme_btns.items():
            if k == name:
                b.configure(text=THEMES[k]["label"], fg=BLUE)
            else:
                b.configure(text=THEMES[k]["label"], fg=TH["text_sub"])

    for tname, t in THEMES.items():
        b = tk.Label(root, text=t["label"], bg=TH["window"], fg=TH["text_sub"],
                     font=("Microsoft YaHei UI", 12), cursor="hand2")
        b.place(x=tx - 60, y=38, anchor="ne")
        b.bind("<Button-1>", lambda e, n=tname: _pick_theme(n))
        theme_btns[tname] = b
        tx -= 64
    _pick_theme("light")

    sub = tk.Label(root, text="自动下载 · 自动填表 · 汇总图片一键送达",
                   bg=TH["window"], fg=TH["text_sub"],
                   font=("Microsoft YaHei UI", 13))
    sub.place(x=40, y=86)
    lab_sub.append(sub)
    ver = tk.Label(root, text=f"v{APP_VERSION}", bg=TH["window"], fg=TH["text_sub"],
                   font=("Microsoft YaHei UI", 11))
    ver.place(x=682, y=88, anchor="ne")
    lab_sub.append(ver)

    # ===== 固定操作按钮 =====
    btn_card = neu.NeuCard(root, ctx, 640, 92, radius=22, pad=14, outer_bg=True)
    btn_card.place(x=40, y=112)
    btn_now = neu.NeuButton(btn_card.inner, ctx, 180, 56, "立即跑一次",
                            radius=14, accent=BLUE, emoji="▶", font_size=14, filled=True)
    btn_now.place(x=10, y=18)
    btn_capture = neu.NeuButton(btn_card.inner, ctx, 180, 56, "仅截图发送",
                                radius=14, accent=ORANGE, emoji="▣", font_size=14, filled=True)
    btn_capture.place(x=218, y=18)
    btn_schedule = neu.NeuButton(btn_card.inner, ctx, 180, 56, "开始计划任务",
                                 radius=14, accent=GREEN, emoji="◷", font_size=14, filled=True)
    btn_schedule.place(x=426, y=18)

    # ===== 状态卡 =====
    status_card = neu.NeuCard(root, ctx, 640, 132, radius=22, pad=14, outer_bg=True)
    status_card.place(x=40, y=232)
    tk.Label(status_card.inner, text="📡 运行状态", bg=TH["base"], fg=TH["text_main"],
             font=("Microsoft YaHei UI", 15, "bold")).place(x=18, y=10)

    run_spinner = neu.RingSpinner(status_card.inner, ctx, 44, color=ORANGE)
    run_spinner.place(x=20, y=48)
    # 空闲时隐藏 spinner，用静态圆点替代
    status_dot = tk.Label(status_card.inner, text="●", bg=TH["base"], fg=GREEN,
                          font=("Microsoft YaHei UI", 16))
    run_spinner.place_forget()
    status_dot.place(x=38, y=60)

    step_anim = neu.AnimatedText(status_card.inner, ctx, "✅ 空闲中，等待开始",
                                 color=GREEN, font_size=16, w=520, h=30, anchor="w")
    step_anim.place(x=80, y=46)
    last_anim = neu.AnimatedText(status_card.inner, ctx, "上次抓取：—",
                                 color=TH["text_sub"], font_size=12, bold=False,
                                 w=320, h=24, anchor="w")
    last_anim.place(x=80, y=88)
    next_anim = neu.AnimatedText(status_card.inner, ctx, "下次抓取：—",
                                 color=BLUE, font_size=12, bold=False,
                                 w=300, h=24, anchor="w")
    next_anim.place(x=320, y=88)
    err_lab = tk.Label(status_card.inner, text="", bg=TH["base"], fg=RED,
                       font=("Microsoft YaHei UI", 12, "bold"), anchor="w",
                       justify="left", wraplength=600)

    def _step_color(text):
        if text.startswith("✅"):
            return GREEN
        if text.startswith("❌") or text.startswith("⚠"):
            return RED
        if text.startswith("⏹"):
            return TH["text_sub"]
        return ORANGE

    # 桌面毛玻璃进度浮窗
    glass = neu.GlassProgress(root)
    STEP_PROGRESS = {
        "正在打开浏览器…": 0.06,
        "正在打开WPS汇总表…": 0.18,
        "正在登录后台…": 0.16,
        "正在下载三份数据…": 0.30,
        "正在整理数据格式…": 0.43,
        "正在写入在线表格…": 0.58,
        "正在切换到数据汇总表…": 0.72,
        "正在等待汇总表刷新…": 0.76,
        "正在复制表格图片…": 0.82,
        "正在关闭后台浏览器…": 0.88,
        "正在发送到钉钉…": 0.95,
        "✅ 本轮跑完": 1.0,
    }

    def _on_step(text):
        def _upd():
            step_anim.set(text, color=_step_color(text))
            prog = STEP_PROGRESS.get(text)
            if prog is not None and not glass.closed:
                glass.update_step(text, prog)
        root.after(0, _upd)

    def _on_last_run():
        def _upd():
            last_anim.set("上次抓取：" + time.strftime("%Y-%m-%d %H:%M"),
                          color=TH["text_sub"])
        root.after(0, _upd)

    set_step_callback(_on_step)
    set_last_run_callback(_on_last_run)

    # ===== 滚动区（纯 tk Canvas + 平滑缓动） =====
    scroll_cv = tk.Canvas(root, bg=TH["window"], highlightthickness=0, bd=0)
    scroll_cv.place(x=0, y=380, relwidth=1, relheight=1, height=-380)
    content = tk.Frame(scroll_cv, bg=TH["window"])
    scroll_cv.create_window(0, 0, anchor="nw", window=content)
    content.bind("<Configure>",
                 lambda e: scroll_cv.configure(scrollregion=scroll_cv.bbox("all")))

    smooth = {"target": None, "anim": None}

    def _frac():
        return scroll_cv.yview()[0]

    def _step_to_target():
        if smooth["target"] is None:
            smooth["anim"] = None
            return
        cur = _frac()
        tgt = smooth["target"]
        if abs(tgt - cur) < 0.002:
            scroll_cv.yview_moveto(tgt)
            smooth["target"] = smooth["anim"] = None
            return
        scroll_cv.yview_moveto(cur + (tgt - cur) * 0.22)
        smooth["anim"] = root.after(16, _step_to_target)

    def _on_wheel(e):
        try:
            bbox = scroll_cv.bbox("all")
            vis = scroll_cv.winfo_height()
            if bbox and vis > 0 and bbox[3] > vis:
                tick = max(0.04, (vis / bbox[3]) * 0.9)
            else:
                tick = 0.08
            direction = -1 if e.delta > 0 else 1
            smooth["target"] = max(0.0, min(1.0, _frac() + direction * tick))
            if smooth["anim"] is None:
                smooth["anim"] = root.after(16, _step_to_target)
        except Exception:
            pass
    root.bind_all("<MouseWheel>", _on_wheel)

    # ===== 卡片1：运行日期 =====
    c1 = neu.NeuCard(content, ctx, 640, 118, radius=22, pad=14, outer_bg=True)
    c1.pack(pady=(6, 14), padx=40)
    tk.Label(c1.inner, text="📅 运行日期", bg=TH["base"], fg=TH["text_main"],
             font=("Microsoft YaHei UI", 15, "bold")).place(x=18, y=12)
    day_mode_var = tk.StringVar(value=cfg.get("day_mode", "daily"))
    seg = neu.NeuSegment(c1.inner, ctx,
                         [("每天都跑", "daily"), ("仅工作日", "workday")],
                         day_mode_var.get(), lambda v: day_mode_var.set(v), h=46)
    seg.place(x=18, y=50)

    # ===== 时段构建（步进器） =====
    def build_slot(parent, defaults, accent):
        rows = tk.Frame(parent, bg=TH["base"])
        rows.pack(fill="x", padx=18, pady=(46, 4))

        def make_row(label, lo, hi, d1, d2):
            r = tk.Frame(rows, bg=TH["base"])
            r.pack(fill="x", pady=4)
            tk.Label(r, text=label, bg=TH["base"], fg=TH["text_sub"], width=4, anchor="w",
                     font=("Microsoft YaHei UI", 13)).pack(side="left")
            s1 = neu.NeuStepper(r, ctx, lo[0], hi[0], d1, h=36)
            s1.pack(side="left", padx=(6, 2))
            tk.Label(r, text=":", bg=TH["base"], fg=TH["text_main"],
                     font=("Microsoft YaHei UI", 15, "bold")).pack(side="left")
            s2 = neu.NeuStepper(r, ctx, lo[1], hi[1], d2, h=36)
            s2.pack(side="left", padx=(2, 0))
            return s1, s2

        sh, sm = make_row("开始", (0, 0), (23, 59), defaults["sh"], defaults["sm"])
        eh, em = make_row("结束", (0, 0), (23, 59), defaults["eh"], defaults["em"])
        ih, im = make_row("每隔", (0, 0), (12, 59), defaults["ih"], defaults["im"])
        widgets = dict(sh=sh, sm=sm, eh=eh, em=em, ih=ih, im=im)
        preview = tk.Label(parent, text="", bg=TH["base"], fg=accent, anchor="w",
                           font=("Microsoft YaHei UI", 12))
        preview.pack(fill="x", padx=20, pady=(2, 12))

        def refresh(*_):
            hh, mm = ih.get(), im.get()
            gap = f"{hh}小时{mm}分钟" if hh > 0 else f"{mm}分钟"
            preview.configure(
                text=f"{sh.get():02d}:{sm.get():02d} ~ {eh.get():02d}:{em.get():02d} · 每{gap}一次")
        for w in widgets.values():
            w.command = refresh
        refresh()
        return widgets

    c2 = neu.NeuCard(content, ctx, 640, 272, radius=22, pad=14, outer_bg=True)
    c2.pack(pady=(0, 14), padx=40)
    tk.Label(c2.inner, text="☀️ 白天时段", bg=TH["base"], fg=ORANGE,
             font=("Microsoft YaHei UI", 15, "bold")).place(x=18, y=12)
    day_w = build_slot(c2.inner, dict(cfg["day"]), ORANGE)

    c3 = neu.NeuCard(content, ctx, 640, 272, radius=22, pad=14, outer_bg=True)
    c3.pack(pady=(0, 14), padx=40)
    tk.Label(c3.inner, text="🌙 晚上时段", bg=TH["base"], fg=BLUE,
             font=("Microsoft YaHei UI", 15, "bold")).place(x=18, y=12)
    night_w = build_slot(c3.inner, dict(cfg["night"]), BLUE)

    # ===== 发送设置 =====
    cs = neu.NeuCard(content, ctx, 640, 300, radius=22, pad=14, outer_bg=True)
    cs.pack(pady=(0, 14), padx=40)
    tk.Label(cs.inner, text="📤 发送设置", bg=TH["base"], fg=TH["text_main"],
             font=("Microsoft YaHei UI", 15, "bold")).place(x=18, y=12)

    def switch_row(title, desc, default, accent, y):
        tk.Label(cs.inner, text=title, bg=TH["base"], fg=TH["text_main"],
                 font=("Microsoft YaHei UI", 14, "bold")).place(x=18, y=y)
        tk.Label(cs.inner, text=desc, bg=TH["base"], fg=TH["text_sub"],
                 font=("Microsoft YaHei UI", 11)).place(x=18, y=y + 24)
        sw = neu.NeuSwitch(cs.inner, ctx, accent_hex=accent, default=default)
        sw.place(x=540, y=y + 6)
        return sw

    capture_sw = switch_row("复制表格图片", "跑完后在汇总表选区复制为图片",
                            cfg.get("enable_capture", True), BLUE, 48)
    dt_sw = switch_row("发送到钉钉", "把图片发到下面填的钉钉会话",
                       cfg.get("enable_dingtalk", False), GREEN, 100)
    auto_send_sw = switch_row("自动点发送", "关掉则只贴好图片，由你手动点发送",
                              cfg.get("dt_auto_send", True), ORANGE, 152)

    dt_search_var = tk.StringVar(value=cfg.get("dt_search", "文件小助手"))
    dt_suffix_var = tk.StringVar(value=cfg.get("dt_suffix", "数据"))

    def entry_row(label, var, y):
        tk.Label(cs.inner, text=label, bg=TH["base"], fg=TH["text_sub"],
                 font=("Microsoft YaHei UI", 13)).place(x=18, y=y + 8)
        fld = neu.NeuField(cs.inner, ctx, 440, h=44, textvariable=var)
        fld.place(x=110, y=y - 4)
        return fld

    entry_row("钉钉会话", dt_search_var, 204)
    entry_row("消息后缀", dt_suffix_var, 250)

    # ===== 日志 =====
    c5 = neu.NeuCard(content, ctx, 640, 240, radius=22, pad=14, outer_bg=True)
    c5.pack(pady=(0, 14), padx=40)
    tk.Label(c5.inner, text="📋 运行日志", bg=TH["base"], fg=TH["text_main"],
             font=("Microsoft YaHei UI", 15, "bold")).place(x=18, y=10)
    log_inset = neu.NeuInset(c5.inner, ctx, 600, 112, radius=14, pad=10)
    log_inset.place(x=20, y=42)
    log_box = tk.Text(log_inset.inner, bd=0, relief=tk.FLAT, wrap="word",
                      font=("Microsoft YaHei UI", 12),
                      bg=TH["base"], fg=TH["text_main"], highlightthickness=0)
    log_box.pack(fill="both", expand=True, padx=6, pady=4)

    def append_log(msg):
        def _add():
            log_box.insert("end", msg + "\n")
            log_box.see("end")
        root.after(0, _add)
    set_gui_logger(append_log)

    # 日志卡内控制按钮
    btn_manual = neu.NeuButton(c5.inner, ctx, 290, 42, "立即跑一次",
                               radius=13, accent=BLUE, emoji="▶", font_size=13, filled=True)
    btn_manual.place(x=18, y=182)
    btn_stop = neu.NeuButton(c5.inner, ctx, 290, 42, "停止计划",
                             radius=13, accent=RED, emoji="■", font_size=13)
    btn_stop.place(x=324, y=182)

    tip = tk.Label(content, text="小提示：数字区域支持滚轮微调；计划运行时也可随时手动执行",
                   bg=TH["window"], fg=TH["text_sub"], wraplength=600, justify="left",
                   font=("Microsoft YaHei UI", 11))
    tip.pack(pady=(0, 6), padx=40, anchor="w")
    lab_sub.append(tip)

    btn_reset = neu.NeuButton(content, ctx, 640, 48, "恢复默认设置",
                              radius=15, accent=RED, emoji="↺", font_size=14,
                              outer_bg=True)
    btn_reset.pack(pady=(6, 24), padx=40)

    # ===== 扫码全屏窗 =====
    prompt_win = {"win": None}

    def _open_prompt():
        if prompt_win["win"] is not None:
            return
        top = tk.Toplevel(root)
        prompt_win["win"] = top
        sw, sh = top.winfo_screenwidth(), top.winfo_screenheight()
        top.overrideredirect(True)
        top.geometry(f"{sw}x{sh}+0+0")
        top.attributes("-topmost", True)
        try:
            top.attributes("-alpha", 0.62)
        except Exception:
            pass
        top.configure(bg="#101013")
        card = neu.NeuCard(top, ctx, 560, 360, radius=30, pad=16)
        card.place(relx=0.5, rely=0.5, anchor="center")
        tk.Label(card.inner, text="⏳", bg=ctx.base_hex,
                 font=("Microsoft YaHei UI", 70)).pack(pady=(40, 4))
        tk.Label(card.inner, text="请在浏览器里扫码登录", bg=ctx.base_hex, fg=BLUE,
                 font=("Microsoft YaHei UI", 28, "bold")).pack(pady=(4, 10))
        tk.Label(card.inner, text="登录成功后会自动继续（最多等待 20 分钟）",
                 bg=ctx.base_hex, fg=TH["text_main"], justify="center",
                 font=("Microsoft YaHei UI", 15, "bold")).pack(pady=(0, 30))

    def _close_prompt():
        w = prompt_win["win"]
        if w is not None:
            try:
                w.destroy()
            except Exception:
                pass
            prompt_win["win"] = None

    set_login_prompt(lambda: root.after(0, _open_prompt),
                     lambda: root.after(0, _close_prompt))

    # ===== 业务逻辑 =====
    def do_run(round_num):
        nonlocal schedule_running, stop_flag
        global _stop_requested
        _stop_requested = False
        # 显示桌面毛玻璃浮窗（内部自动重置，线程安全）
        glass.show()
        try:
            err_lab.configure(text="")
            step_anim.set(f"🔄 正在跑第 {round_num} 轮...", color=ORANGE)
            glass.update_step(f"正在跑第 {round_num} 轮…", 0.02)
            root.update()
            run_cfg = dict(collect_and_save())
            if isinstance(round_num, int) and not capture_allowed_now(run_cfg):
                run_cfg["enable_capture"] = False
                run_cfg["enable_dingtalk"] = False
                log("  当前不在截图发送时段，本轮只下载并更新表格")
                step_anim.set("🕒 不在截图发送时段，仅更新表格", color=TH["text_sub"])
            end_time = run_once(round_num, run_cfg)
            done_text = ("✅ 手动任务已完成" if round_num == "手动"
                         else f"✅ 第 {round_num} 轮已完成")
            step_anim.set(done_text, color=GREEN)
            glass.finish("✅ 本轮跑完")
            glass.close(1100)
            return end_time
        except Exception as e:
            _close_prompt()
            if "手动停止" in str(e):
                log("⏹ 已手动停止")
                err_lab.configure(text="")
                step_anim.set("⏹ 已停止", color=TH["text_sub"])
                glass.fail("⏹ 已手动停止")
                glass.close(700)
                return time.time()
            tip = human_error(e)
            log(f"❌ {tip}")
            err_lab.configure(text="⚠️ 出错步骤：" + tip)
            step_anim.set("❌ 出错了，请看下面日志", color=RED)
            glass.fail("❌ " + tip)
            glass.close(1800)
            try:
                import traceback
                with open(app_dir() / "error.log", "a", encoding="utf-8") as f:
                    f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
                    f.write(traceback.format_exc())
            except Exception:
                pass
            return time.time()

    def read_slot(w):
        sh, sm = w["sh"].get(), w["sm"].get()
        eh, em = w["eh"].get(), w["em"].get()
        ih, im = w["ih"].get(), w["im"].get()
        start = f"{sh:02d}:{sm:02d}"
        end = f"{eh:02d}:{em:02d}"
        interval = ih * 60 + im
        if interval < 1:
            raise ValueError
        return start, end, interval

    def _slot_dict(w):
        return dict(sh=w["sh"].get(), sm=w["sm"].get(),
                    eh=w["eh"].get(), em=w["em"].get(),
                    ih=w["ih"].get(), im=w["im"].get())

    def collect_and_save():
        try:
            cfg["day"] = _slot_dict(day_w)
            cfg["night"] = _slot_dict(night_w)
        except Exception:
            pass
        cfg["day_mode"] = day_mode_var.get()
        cfg["enable_capture"] = capture_sw.get()
        cfg["enable_dingtalk"] = dt_sw.get()
        cfg["dt_auto_send"] = auto_send_sw.get()
        cfg["dt_search"] = dt_search_var.get().strip() or "文件小助手"
        cfg["dt_suffix"] = dt_suffix_var.get().strip()
        save_config(cfg)
        return cfg

    def reset_defaults():
        import copy
        default = copy.deepcopy(DEFAULT_CONFIG)
        cfg.clear()
        cfg.update(default)
        seg.select(default["day_mode"])
        day_mode_var.set(default["day_mode"])
        for key in ("sh", "sm", "eh", "em", "ih", "im"):
            day_w[key].set(default["day"][key])
            night_w[key].set(default["night"][key])
        capture_sw.set(default["enable_capture"])
        dt_sw.set(default["enable_dingtalk"])
        auto_send_sw.set(default["dt_auto_send"])
        dt_search_var.set(default["dt_search"])
        dt_suffix_var.set(default["dt_suffix"])
        save_config(cfg)
        messagebox.showinfo("已恢复", "所有设置已恢复成默认。")

    btn_reset.command = reset_defaults

    # ===== 防抖自动保存 =====
    _auto_id = [None]

    def _do_autosave():
        _auto_id[0] = None
        try:
            collect_and_save()
        except Exception:
            pass

    def _autosave(*_):
        if _auto_id[0] is not None:
            try:
                root.after_cancel(_auto_id[0])
            except Exception:
                pass
        _auto_id[0] = root.after(500, _do_autosave)

    for _w in list(day_w.values()) + list(night_w.values()):
        _orig = _w.command

        def _wrap(orig):
            def _runner():
                if orig:
                    orig()
                _autosave()
            return _runner
        _w.command = _wrap(_orig)
    capture_sw.command = lambda on: _autosave()
    dt_sw.command = lambda on: _autosave()
    seg.command = lambda v: (day_mode_var.set(v), _autosave())
    dt_search_var.trace_add("write", _autosave)
    dt_suffix_var.trace_add("write", _autosave)

    def validate_config():
        try:
            ds, de, di = read_slot(day_w)
            ns, ne, ni = read_slot(night_w)
            return dict(day_mode=day_mode_var.get(),
                        day_start=ds, day_end=de, day_interval=di,
                        night_start=ns, night_end=ne, night_interval=ni)
        except Exception:
            messagebox.showerror("提示", "间隔不能是0，请把「每隔」的小时或分钟调大一点")
            return None

    def do_capture_only():
        global _active_end_time
        manual_start = time.time()
        _active_end_time = None
        _on_step("正在打开WPS汇总表…")
        _on_last_run()
        completed = False
        try:
            glass.show()
            end_time = run_capture_only("截图", collect_and_save())
            _active_end_time = end_time
            completed = True
            root.after(0, lambda: (
                step_anim.set("✅ 截图发送完成", color=GREEN),
                err_lab.configure(text="")))
        except Exception as e:
            if "手动停止" in str(e):
                root.after(0, lambda: (
                    step_anim.set("⏹ 已停止", color=TH["text_sub"]),
                    err_lab.configure(text="")))
            else:
                log(f"❌ 独立截图失败: {e}")
                root.after(0, lambda msg=str(e): (
                    step_anim.set("❌ 截图发送失败", color=RED),
                    err_lab.configure(text=msg)))
        finally:
            def _finish_ui():
                run_spinner.place_forget()
                status_dot.place(x=38, y=60)
                if completed:
                    glass.finish("✅ 截图发送完成")
                    glass.close(1400)
                else:
                    glass.close(1200)
            root.after(0, _finish_ui)

    def on_click_now():
        if capture_running:
            messagebox.showinfo("提示", "独立截图正在运行，请等它结束后再试")
            return
        btn_now.set_enabled(False)
        btn_capture.set_enabled(False)

        def task():
            try:
                do_run("手动")
            finally:
                root.after(0, lambda: (
                    btn_now.set_enabled(True), btn_capture.set_enabled(True)))
        threading.Thread(target=task, daemon=True).start()
    btn_now.command = on_click_now

    def on_click_capture():
        nonlocal capture_running
        if capture_running:
            return
        if schedule_running:
            messagebox.showinfo("提示", "计划任务正在运行，请先停止计划再单独截图")
            return
        capture_running = True
        btn_now.set_enabled(False)
        btn_capture.set_enabled(False)

        def task():
            nonlocal capture_running
            try:
                do_capture_only()
            finally:
                capture_running = False
                root.after(0, lambda: (
                    btn_now.set_enabled(True), btn_capture.set_enabled(True)))
        threading.Thread(target=task, daemon=True).start()
    btn_capture.command = on_click_capture

    def on_click_schedule():
        nonlocal schedule_running, stop_flag
        if capture_running:
            messagebox.showinfo("提示", "独立截图正在运行，请等它结束后再启动计划")
            return
        plan_cfg = validate_config()
        if not plan_cfg:
            return
        collect_and_save()
        schedule_running = True
        stop_flag = False
        btn_schedule.text = "运行中..."
        btn_schedule.redraw()

        def schedule_task():
            nonlocal schedule_running, stop_flag, manual_trigger
            round_num = 1
            last_run = {"day": 0.0, "night": 0.0}
            while not stop_flag:
                now = time.localtime()
                if plan_cfg["day_mode"] == "workday" and now.tm_wday >= 5:
                    step_anim.set("🌙 今天是周末，不运行", color=TH["text_sub"])
                    next_anim.set("下次抓取：—", color=BLUE)
                    for _ in range(30):
                        if stop_flag:
                            break
                        time.sleep(1)
                    continue
                slot = None
                if in_slot(now, plan_cfg["day_start"], plan_cfg["day_end"]):
                    slot = "day"
                elif in_slot(now, plan_cfg["night_start"], plan_cfg["night_end"]):
                    slot = "night"
                if slot is None:
                    step_anim.set("🌙 不在运行时段，等待中", color=TH["text_sub"])
                    next_anim.set("下次抓取：—", color=BLUE)
                    for _ in range(30):
                        if stop_flag:
                            break
                        if manual_trigger:
                            manual_trigger = False
                            do_run("手动")
                        time.sleep(1)
                    continue
                interval = plan_cfg["day_interval"] if slot == "day" else plan_cfg["night_interval"]
                since = time.time() - last_run[slot]
                if manual_trigger:
                    manual_trigger = False
                    do_run("手动")
                    last_run[slot] = time.time()
                elif since >= interval * 60:
                    do_run(round_num)
                    round_num += 1
                    last_run[slot] = time.time()
                else:
                    left = int(interval * 60 - since)
                    nxt = time.strftime("%H:%M", time.localtime(time.time() + left))
                    step_anim.set(f"⏳ 在{('白天' if slot=='day' else '晚上')}时段，等待下一次",
                                  color=ORANGE)
                    next_anim.set(f"下次自动跑：{nxt}", color=BLUE)
                for _ in range(10):
                    if stop_flag:
                        break
                    time.sleep(1)
            schedule_running = False

            def _restore():
                btn_schedule.text = "开始计划任务"
                btn_schedule.redraw()
                step_anim.set("⏹ 计划任务已停止", color=TH["text_sub"])
                next_anim.set("下次抓取：—", color=BLUE)
            root.after(0, _restore)
        threading.Thread(target=schedule_task, daemon=True).start()
    btn_schedule.command = on_click_schedule

    def on_click_manual():
        nonlocal manual_trigger
        manual_trigger = True
        step_anim.set("⚡ 已触发，马上跑...", color=ORANGE)
    btn_manual.command = on_click_manual

    def on_click_stop():
        nonlocal stop_flag
        stop_flag = True
        request_stop()
    btn_stop.command = on_click_stop

    def _check_update():
        info = fetch_remote_version()
        if info and _ver_newer(info[0], APP_VERSION):
            ver, url = info

            def ask():
                try:
                    import webbrowser
                    webbrowser.open(url)
                except Exception:
                    pass
            root.after(0, ask)
    threading.Thread(target=_check_update, daemon=True).start()

    root.mainloop()


def show_settings_window():
    from ios_ui import show_settings_window as show
    show(sys.modules[__name__])


if __name__ == "__main__":
    if not acquire_single_instance():
        sys.exit(0)
    try:
        def _delayed_browser_check():
            time.sleep(2.5)
            try:
                ensure_browser_installed()
            except Exception:
                pass

        threading.Thread(target=_delayed_browser_check, daemon=True).start()
        show_settings_window()
    except Exception as e:
        log_error(e, "启动失败")
        try:
            messagebox.showerror("出错了", human_error(e))
        except Exception:
            pass
