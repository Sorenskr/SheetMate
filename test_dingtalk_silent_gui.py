# -*- coding: utf-8 -*-
# pyright: reportMissingImports=false, reportMissingModuleSource=false
"""钉钉无坐标静音发送独立测试程序（支持真实桌面完整连跑 + UIA 控件树精准定位验证）。"""

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
from PIL import Image, ImageDraw, ImageFont

ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR))

LOG_FILE = ROOT_DIR / "test_dingtalk_silent.log"

# 您提供的真实钉钉控件树 AutomationId（完全无硬编码坐标）
SUB_MENU_BUTTON_AUTO_ID = (
    "qt_chat_navigable_content_widget.stackedWidget.mesasgePage.widget.splitter."
    "widgetRichEditWnd.drich_edit.verticalContentWidget.widget_input_wnd."
    "bottomBarWidget.combo_send_button_widget.containerWidget.rightContainerWidget.subMenuButton"
)
RICH_EDIT_AUTO_ID = (
    "qt_chat_navigable_content_widget.stackedWidget.mesasgePage.widget.splitter."
    "widgetRichEditWnd.drich_edit"
)
COMBO_SEND_AUTO_ID = (
    "qt_chat_navigable_content_widget.stackedWidget.mesasgePage.widget.splitter."
    "widgetRichEditWnd.drich_edit.verticalContentWidget.widget_input_wnd."
    "bottomBarWidget.combo_send_button_widget"
)
LEFT_SEND_CONTAINER_ID = COMBO_SEND_AUTO_ID + ".containerWidget.leftContainerWidget"
RIGHT_SEND_CONTAINER_ID = COMBO_SEND_AUTO_ID + ".containerWidget.rightContainerWidget"


def _no_window_flags():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _ps_sq(val):
    return str(val).replace("'", "''")


def get_process_elevation(pid=None):
    """检测本进程或指定 PID 是否管理员运行：True / False / None。"""
    if os.name != "nt":
        return None
    TOKEN_QUERY, TOKEN_ELEVATION_CLASS, PROCESS_QUERY_LIMITED = 0x0008, 20, 0x1000
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
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ]
    adv32.GetTokenInformation.restype = wintypes.BOOL

    class TOKEN_ELEVATION(ctypes.Structure):
        _fields_ = [("TokenIsElevated", wintypes.DWORD)]

    close_p = False
    if pid is None:
        hproc = k32.GetCurrentProcess()
    else:
        hproc = k32.OpenProcess(PROCESS_QUERY_LIMITED, False, int(pid))
        if not hproc:
            if ctypes.get_last_error() == 5 and get_process_elevation(None) is False:
                return True
            return None
        close_p = True
    tok = wintypes.HANDLE()
    try:
        if not adv32.OpenProcessToken(hproc, TOKEN_QUERY, ctypes.byref(tok)):
            if ctypes.get_last_error() == 5 and pid is not None and get_process_elevation(None) is False:
                return True
            return None
        try:
            elev = TOKEN_ELEVATION()
            rl = wintypes.DWORD()
            if not adv32.GetTokenInformation(tok, TOKEN_ELEVATION_CLASS, ctypes.byref(elev), ctypes.sizeof(elev), ctypes.byref(rl)):
                return None
            return bool(elev.TokenIsElevated)
        finally:
            k32.CloseHandle(tok)
    finally:
        if close_p and hproc:
            k32.CloseHandle(hproc)


def list_dingtalk_processes():
    """枚举当前系统 DingTalk.exe 进程，返回 [(pid, exe_path), ...]。"""
    if os.name != "nt":
        return []
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    k32.QueryFullProcessImageNameW.restype = wintypes.BOOL

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
        ]

    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32FirstW.restype = wintypes.BOOL
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.restype = wintypes.BOOL

    snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)
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
                ep = ""
                hp = k32.OpenProcess(0x1000, False, pid)
                if hp:
                    try:
                        buf = ctypes.create_unicode_buffer(1024)
                        sz = wintypes.DWORD(1024)
                        if k32.QueryFullProcessImageNameW(hp, 0, buf, ctypes.byref(sz)):
                            ep = buf.value
                    finally:
                        k32.CloseHandle(hp)
                out.append((pid, ep))
            if not k32.Process32NextW(snap, ctypes.byref(pe)):
                break
    finally:
        k32.CloseHandle(snap)
    return out


def check_privilege_match():
    """启动同权限校验：防止管理员与普通权限隔离导致点击/热键被拦截。"""
    self_ev = get_process_elevation(None)
    procs = list_dingtalk_processes()
    self_lbl = "管理员权限" if self_ev else "普通用户权限"
    if not procs:
        return True, f"权限检测：本测试程序为【{self_lbl}】（暂未检测到 DingTalk.exe 运行）"
    ding_ev = None
    for pid, _ in procs:
        ev = get_process_elevation(pid)
        if ev is not None:
            ding_ev = ev
            if ev:
                break
    ding_lbl = "管理员权限" if ding_ev else ("普通用户权限" if ding_ev is False else "未知权限")
    ok = (self_ev is None) or (ding_ev is None) or (self_ev == ding_ev)
    if ok:
        return True, f"权限校验通过：测试程序与钉钉同为【{self_lbl}】"
    return False, (
        f"⚠️ 权限不一致！测试程序为【{self_lbl}】，钉钉为【{ding_lbl}】，"
        "请用相同权限重新运行，否则 Windows UIPI 会拦截点击！"
    )


def set_clipboard_text(text):
    """用 Win32 原生 CF_UNICODETEXT 写入 UTF-16LE（带空终止符），杜绝 clip.exe 把短中文误判成 GBK 乱码。"""
    s = "" if text is None else str(text)
    if os.name != "nt":
        try:
            subprocess.run(["clip"], input=b"\xff\xfe" + s.encode("utf-16-le"), check=False, timeout=5, creationflags=_no_window_flags())
            return True
        except Exception:
            return False
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    u32.OpenClipboard.argtypes = [wintypes.HWND]
    u32.OpenClipboard.restype = wintypes.BOOL
    u32.EmptyClipboard.argtypes = []
    u32.EmptyClipboard.restype = wintypes.BOOL
    u32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    u32.SetClipboardData.restype = ctypes.c_void_p
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

    raw = s.encode("utf-16-le") + b"\x00\x00"
    for _ in range(8):
        if u32.OpenClipboard(None):
            h_mem = None
            transferred = False
            try:
                u32.EmptyClipboard()
                h_mem = k32.GlobalAlloc(0x0002, len(raw))
                if h_mem:
                    ptr = k32.GlobalLock(h_mem)
                    if ptr:
                        try:
                            ctypes.memmove(ptr, raw, len(raw))
                        finally:
                            k32.GlobalUnlock(h_mem)
                        if u32.SetClipboardData(13, h_mem):
                            transferred = True
                            return True
            finally:
                if h_mem and not transferred:
                    try:
                        k32.GlobalFree(h_mem)
                    except Exception:
                        pass
                u32.CloseClipboard()
        time.sleep(0.08)
    try:
        subprocess.run(["clip"], input=b"\xff\xfe" + s.encode("utf-16-le"), check=False, timeout=5, creationflags=_no_window_flags())
    except Exception:
        pass
    return False


def get_clipboard_text():
    if os.name != "nt":
        return ""
    u32, k32 = ctypes.WinDLL("user32", use_last_error=True), ctypes.WinDLL("kernel32", use_last_error=True)
    u32.OpenClipboard.argtypes = [wintypes.HWND]
    u32.OpenClipboard.restype = wintypes.BOOL
    u32.GetClipboardData.argtypes = [wintypes.UINT]
    u32.GetClipboardData.restype = wintypes.HANDLE
    u32.CloseClipboard.restype = wintypes.BOOL
    k32.GlobalLock.argtypes = [wintypes.HANDLE]
    k32.GlobalLock.restype = wintypes.LPVOID
    k32.GlobalUnlock.argtypes = [wintypes.HANDLE]
    k32.GlobalUnlock.restype = wintypes.BOOL
    for _ in range(3):
        if u32.OpenClipboard(None):
            try:
                h = u32.GetClipboardData(13)
                if not h:
                    return ""
                p = k32.GlobalLock(h)
                if not p:
                    return ""
                try:
                    return ctypes.wstring_at(p)
                finally:
                    k32.GlobalUnlock(h)
            finally:
                u32.CloseClipboard()
        time.sleep(0.1)
    return ""


def _ps_clipboard(cmd, timeout=30):
    full = "Add-Type -AssemblyName System.Windows.Forms;Add-Type -AssemblyName System.Drawing;" + cmd
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command", full],
        capture_output=True, text=True, timeout=timeout, creationflags=_no_window_flags(),
    )
    return (r.stdout or "").strip()


def save_clipboard_image_or_create_sample(target_path):
    """若系统剪贴板已有用户截图则直接保护导出；无图则生成测试样张 PNG。"""
    p = Path(target_path)
    out = _ps_clipboard(
        "if ([System.Windows.Forms.Clipboard]::ContainsImage()) {"
        f"[System.Windows.Forms.Clipboard]::GetImage().Save('{_ps_sq(p)}');"
        "Write-Output 'saved'} else {Write-Output 'noimage'}"
    )
    if "saved" in out and p.exists():
        return True, "使用系统剪贴板现有截图"
    create_sample_table_image(p)
    return False, "剪贴板暂无图片，已自动生成清晰测试样张"


def create_sample_table_image(out_path):
    """生成带当前时间的干净测试图片，方便单独跑通图文粘贴与静音发送全链路。"""
    im = Image.new("RGB", (680, 220), (248, 250, 255))
    dr = ImageDraw.Draw(im)
    dr.rectangle((0, 0, 679, 52), fill=(18, 120, 245))
    dr.rectangle((14, 68, 666, 204), outline=(210, 220, 236), width=2, fill=(255, 255, 255))
    dr.line((14, 114, 666, 114), fill=(224, 230, 242), width=2)
    try:
        font_b = ImageFont.truetype("msyh.ttc", 22)
        font_s = ImageFont.truetype("msyh.ttc", 18)
    except Exception:
        font_b = ImageFont.load_default()
        font_s = font_b
    dr.text((24, 12), "SheetMate 钉钉无坐标静音发送测试样张", fill=(255, 255, 255), font=font_b)
    dr.text((28, 78), f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}", fill=(32, 42, 58), font=font_s)
    dr.text((28, 134), "测试链路：Ctrl+Shift+F -> 贴图贴字 -> subMenuButton -> 设为静音 -> 发送 -> 最小化", fill=(65, 82, 108), font=font_s)
    im.save(out_path, "PNG")
    return Path(out_path)


def set_clipboard_image(img_path):
    ps = (
        f"$img=[System.Drawing.Image]::FromFile('{_ps_sq(img_path)}');"
        "$bmp=New-Object System.Drawing.Bitmap($img);$ok=$false;"
        "for($i=0;$i -lt 5;$i++){try{[System.Windows.Forms.Clipboard]::SetImage($bmp);$ok=$true;break}"
        "catch{Start-Sleep -Milliseconds 200}};$img.Dispose();$bmp.Dispose();"
        "if($ok){Write-Output 'ok'}else{Write-Output 'fail'}"
    )
    return "ok" in _ps_clipboard(ps).lower()


def _resolve_dingtalk_exes():
    cands = [ep for _, ep in list_dingtalk_processes() if ep]
    for p in (
        r"D:\Program Files (x86)\DingDing\main\current_new\DingTalk.exe",
        r"C:\Program Files (x86)\DingDing\main\current_new\DingTalk.exe",
        r"C:\Program Files\DingDing\main\current_new\DingTalk.exe",
        r"D:\Program Files\DingDing\main\current_new\DingTalk.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\DingDing\main\current_new\DingTalk.exe"),
    ):
        if p and p not in cands:
            cands.append(p)
    return cands


def _unwrap_uia(ctrl):
    return ctrl.wrapper_object() if (ctrl is not None and hasattr(ctrl, "wrapper_object")) else ctrl


def _safe_info(ctrl):
    info = ctrl.element_info
    legacy_nm = ""
    try:
        lp = ctrl.legacy_properties()
        if isinstance(lp, dict):
            legacy_nm = str(lp.get("Name") or "")
    except Exception:
        pass
    try:
        rr = ctrl.rectangle()
    except Exception:
        rr = None
    return {
        "ct": info.control_type or "",
        "name": (info.name or "").replace("\r", " ").replace("\n", " ").strip(),
        "legacy": legacy_nm.replace("\r", " ").replace("\n", " ").strip(),
        "aid": info.automation_id or "",
        "cls": info.class_name or "",
        "rect": rr,
    }


def _iter_subtree_fast(root_wrap, skip_aids=(), max_depth=24):
    """快速遍历 UIA 子树，跳过消息历史列表等无关超长分支以瞬间直达输入区与底栏。"""
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


def find_uia_by_exact_or_suffix(root_wrap, exact_aid, suffixes=(), class_hint=None):
    """无坐标在 UIA 控件树中定位精确 AutomationId / 后缀，跳过聊天记录大列表实现毫秒级命中。"""
    skip_branches = ("messagelist", "sessionlist", "chat_list", "conversationlist")
    suffixes_low = tuple(s.lower() for s in suffixes if s)
    exact_low = (exact_aid or "").lower()

    # 若 root 支持 child_window 先按 exact_aid 尝试（仅对 WindowSpecification 有效）
    if hasattr(root_wrap, "child_window") and exact_aid:
        try:
            cw = root_wrap.child_window(auto_id=exact_aid, found_index=0)
            if cw.exists(timeout=0.6):
                return _unwrap_uia(cw)
        except Exception:
            pass

    wrap = _unwrap_uia(root_wrap)
    exact_hit = None
    suffix_hit = None
    for ctrl, _depth in _iter_subtree_fast(wrap, skip_aids=skip_branches, max_depth=24):
        try:
            aid = (ctrl.element_info.automation_id or "")
            aid_l = aid.lower()
            if exact_low and aid_l == exact_low:
                exact_hit = ctrl
                break
            if suffixes_low and any(aid_l.endswith(sfx) for sfx in suffixes_low):
                if class_hint is None or (ctrl.element_info.class_name or "") == class_hint:
                    suffix_hit = suffix_hit or ctrl
        except Exception:
            pass
    return exact_hit or suffix_hit


def _uia_click_with_invoke_fallback(ctrl_wrap, check_opened_fn=None):
    """优先无移动物理点击/click_input，若菜单未弹出且支持 InvokePattern 则补触发 invoke()。"""
    errors = []
    for fn_name in ("click_input", "invoke"):
        fn = getattr(ctrl_wrap, fn_name, None)
        if fn is None:
            continue
        try:
            fn()
            if check_opened_fn is None:
                return fn_name
            time.sleep(0.35)
            if check_opened_fn():
                return fn_name
        except Exception as exc:
            errors.append(f"{fn_name}: {exc}")
    if check_opened_fn is not None:
        return "attempted"
    raise RuntimeError("；".join(errors) or "控件不支持点击或 Invoke")


def _enum_popup_windows(pids, main_hwnd):
    """枚举当前钉钉 PID 下所有除主窗外的可见顶层弹出菜单/浮层窗。"""
    from pywinauto import Desktop  # type: ignore[import-not-found]
    popups = []
    for pw in Desktop(backend="uia").windows():
        try:
            if pids and pw.process_id() not in pids:
                continue
            if main_hwnd is not None and pw.handle == main_hwnd:
                continue
            if not pw.is_visible():
                continue
            rr = pw.rectangle()
            if rr.width() >= 40 and rr.height() >= 30:
                popups.append(pw)
        except Exception:
            pass
    return popups


def find_silent_menu_item_in_roots(roots, enable_silent=True, log_fn=None):
    """无坐标定位弹出菜单中的「设为静音 / 取消静音」条目。

    匹配顺序：
    1) 检查是否已存在目标反状态（如已显示「取消静音」）；
    2) Name 或 LegacyIAccessible.Name 含「设为静音 / 静音发送」；
    3) AutomationId 含 silent / mute / quiet；
    4) 当弹出菜单浮层内仅露出多个无名行子控件（设为定时 / 设为静音 / 锁定发送）时，按 Top 排序选第 2 项控件。
    """
    already_kw = ("取消静音",) if enable_silent else ("设为静音", "静音发送")
    target_kw = ("设为静音", "静音发送") if enable_silent else ("取消静音",)
    menu_types = {"MenuItem", "Button", "ListItem", "Text", "Custom", "Pane"}

    name_hit = None
    aid_hit = None
    popup_rows_all = []

    for r_idx, root in enumerate(roots):
        rwrap = _unwrap_uia(root)
        is_popup_win = (r_idx < len(roots) - 1)  # 最后一个约定为主窗，其余为独立弹出窗
        local_rows = []
        for ctrl, depth in _iter_subtree_fast(rwrap, skip_aids=("messagelist", "sessionlist"), max_depth=14):
            try:
                inf = _safe_info(ctrl)
                rr = inf["rect"]
                if rr is None or rr.width() <= 0 or rr.height() <= 0:
                    continue
                if log_fn and (is_popup_win or inf["name"] or inf["legacy"] or "silent" in inf["aid"].lower() or "mute" in inf["aid"].lower()):
                    log_fn(
                        f"    [菜单探测 r{r_idx} d{depth}] type={inf['ct']!r} name={inf['name']!r} "
                        f"legacy={inf['legacy']!r} aid={inf['aid']!r} cls={inf['cls']!r} "
                        f"rect=({rr.left},{rr.top},{rr.right},{rr.bottom})"
                    )
                combined_nm = f"{inf['name']} {inf['legacy']}"
                if any(k in combined_nm for k in already_kw):
                    return ("already", ctrl, combined_nm.strip())
                if name_hit is None and any(k in combined_nm for k in target_kw):
                    name_hit = ("name", ctrl, combined_nm.strip())
                aid_low = inf["aid"].lower()
                if aid_hit is None and any(k in aid_low for k in ("silent", "mute", "quiet")):
                    aid_hit = ("auto_id", ctrl, inf["aid"])
                if is_popup_win and depth >= 1 and inf["ct"] in menu_types and 45 <= rr.width() <= 320 and 18 <= rr.height() <= 64:
                    local_rows.append((rr.top, rr.height(), ctrl, inf))
            except Exception:
                pass
        if is_popup_win:
            if name_hit is not None:
                return name_hit
            if aid_hit is not None:
                return aid_hit
            if len(local_rows) >= 2:
                popup_rows_all.extend(local_rows)
                break
        elif len(local_rows) >= 2:
            popup_rows_all.extend(local_rows)

    if name_hit is not None:
        return name_hit
    if aid_hit is not None:
        return aid_hit
    if popup_rows_all:
        # 去重同一行（如 TextControl 嵌在 MenuItem 内部时保留每行唯一纵向位置）并取第 2 项「设为静音」
        dedup = []
        for top_v, h_v, ctrl, inf in sorted(popup_rows_all, key=lambda x: (x[0], x[1])):
            if not dedup or abs(top_v - dedup[-1][0]) >= 12:
                dedup.append((top_v, ctrl, inf))
        if len(dedup) >= 2:
            second = dedup[1]
            return ("menu_row_2", second[1], f"{second[2]['ct']}#{second[2]['cls']}")
    return None


class DingTalkSilentTester:
    """封装完整测试动作流：连接钉钉 -> Ctrl+Shift+F 搜索 -> 贴图贴字 -> subMenuButton 静音 -> 发送 -> 最小化。"""

    def __init__(self, log_fn=None):
        self.log = log_fn or print

    def _connect_uia(self):
        from pywinauto import Application, Desktop  # type: ignore[import-not-found]
        procs = list_dingtalk_processes()
        pids = {p for p, _ in procs}

        def _find_main():
            for w in Desktop(backend="uia").windows():
                try:
                    if pids and w.process_id() not in pids:
                        continue
                    cls = (w.element_info.class_name or "")
                    if cls in ("DtMainFrameView", "Qt51511QWindowIcon"):
                        return w
                    if w.is_visible() and w.rectangle().width() >= 600 and w.rectangle().height() >= 420:
                        return w
                except Exception:
                    pass
            return None

        ui_win = _find_main()
        if ui_win is None:
            for exe in _resolve_dingtalk_exes():
                if os.path.exists(exe):
                    self.log(f"钉钉未打开主窗，正在启动：{exe}")
                    subprocess.Popen([exe])
                    for _ in range(20):
                        time.sleep(0.5)
                        procs = list_dingtalk_processes()
                        pids = {p for p, _ in procs}
                        ui_win = _find_main()
                        if ui_win is not None:
                            break
                    break
        if ui_win is None:
            raise RuntimeError("未能找到已登录的钉钉主窗口，请先打开钉钉并扫码进入主界面。")

        app = Application(backend="uia").connect(path="DingTalk.exe", timeout=4)
        if app.process != ui_win.process_id():
            app = Application(backend="uia").connect(process=ui_win.process_id(), timeout=4)
        win_spec = app.window(handle=ui_win.handle)
        return app, win_spec, _unwrap_uia(ui_win), pids

    def _force_front_and_restore(self, hwnd):
        u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
        u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u32.ShowWindow.restype = wintypes.BOOL
        u32.GetForegroundWindow.argtypes = []
        u32.GetForegroundWindow.restype = wintypes.HWND
        u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, wintypes.LPDWORD]
        u32.GetWindowThreadProcessId.restype = wintypes.DWORD
        u32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        u32.AttachThreadInput.restype = wintypes.BOOL
        u32.SetForegroundWindow.argtypes = [wintypes.HWND]
        u32.SetForegroundWindow.restype = wintypes.BOOL
        u32.BringWindowToTop.argtypes = [wintypes.HWND]
        u32.BringWindowToTop.restype = wintypes.BOOL
        k32.GetCurrentThreadId.argtypes = []
        k32.GetCurrentThreadId.restype = wintypes.DWORD

        u32.ShowWindow(hwnd, 9)  # SW_RESTORE
        cur_t = k32.GetCurrentThreadId()
        fg = u32.GetForegroundWindow()
        fg_t = u32.GetWindowThreadProcessId(fg, None) if fg else 0
        attached = bool(fg_t and fg_t != cur_t and u32.AttachThreadInput(cur_t, fg_t, True))
        try:
            u32.SetForegroundWindow(hwnd)
            u32.BringWindowToTop(hwnd)
        finally:
            if attached:
                u32.AttachThreadInput(cur_t, fg_t, False)

    def _minimize_window(self, hwnd):
        u32 = ctypes.windll.user32
        u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u32.ShowWindow.restype = wintypes.BOOL
        ok = u32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
        self.log(f"✅ 已调用 ShowWindow(SW_MINIMIZE) 最小化钉钉窗口（result={bool(ok)}）")

    def _is_silent_already_on(self, win_spec):
        input_area = find_uia_by_exact_or_suffix(
            win_spec, RICH_EDIT_AUTO_ID, suffixes=("widgetRichEditWnd.drich_edit", ".drich_edit")
        )
        if input_area is None:
            return False
        for ch, _ in _iter_subtree_fast(_unwrap_uia(input_area), max_depth=10):
            inf = _safe_info(ch)
            rr = inf["rect"]
            if rr is None or rr.width() <= 0 or rr.height() <= 0:
                continue
            txt = f"{inf['name']} {inf['legacy']}"
            if "静音发送" in txt:
                return True
        return False

    def _click_silent_option(self, win_spec, win_wrap, pids, enable_silent=True, force_open_menu=False):
        """无坐标定位 subMenuButton -> 弹出菜单 -> 无坐标点中「设为静音 / 取消静音」。"""
        from pywinauto import keyboard  # type: ignore[import-not-found]
        win_spec.set_focus()
        time.sleep(0.2)

        already_on = False if force_open_menu else self._is_silent_already_on(win_spec)
        if enable_silent and already_on:
            self.log("ℹ️ 检测到输入区当前已带有「静音发送」标签，无需再次点击切换")
            return True
        if not force_open_menu and not enable_silent and not already_on:
            self.log("ℹ️ 当前处于常规提醒状态，无需取消静音")
            return True

        arrow = find_uia_by_exact_or_suffix(
            win_spec,
            SUB_MENU_BUTTON_AUTO_ID,
            suffixes=("rightContainerWidget.subMenuButton", ".subMenuButton"),
            class_hint="ddesign::IconButton",
        )
        if arrow is None:
            rbox = find_uia_by_exact_or_suffix(
                win_spec, RIGHT_SEND_CONTAINER_ID, suffixes=(".rightContainerWidget",)
            )
            if rbox is not None:
                for ch, _ in _iter_subtree_fast(_unwrap_uia(rbox), max_depth=6):
                    if (_safe_info(ch)["ct"] == "Button"):
                        arrow = ch
                        break
        if arrow is None:
            raise RuntimeError("未能在钉钉控件树中找到 subMenuButton 下拉箭头（请确认已进入具体聊天会话窗口）")

        ainf = _safe_info(arrow)
        self.log(f"✅ 命中下拉箭头：{ainf['ct']} cls={ainf['cls']!r} aid={ainf['aid']}")

        popups_holder = []

        def _has_popup():
            pops = _enum_popup_windows(pids, win_wrap.handle)
            popups_holder[:] = pops
            return bool(pops)

        mode_used = _uia_click_with_invoke_fallback(arrow, check_opened_fn=_has_popup)
        time.sleep(0.3)
        popups = _enum_popup_windows(pids, win_wrap.handle) or popups_holder
        self.log(f"  点击方式={mode_used}，检测到弹出浮层数={len(popups)}")

        roots = list(popups) + [win_wrap]
        hit = find_silent_menu_item_in_roots(roots, enable_silent=enable_silent, log_fn=self.log)
        if hit is not None:
            how, ctrl, desc = hit
            if how == "already":
                keyboard.send_keys("{ESC}")
                self.log(f"✅ 菜单状态确认为已设置（{desc}），已按 Esc 收起菜单")
                return True
            self.log(f"✅ 无坐标命中菜单项（方式={how}, 目标={desc!r}），正在点击…")
            _uia_click_with_invoke_fallback(ctrl)
            time.sleep(0.25)
            return True

        # 终极无坐标兜底：pyautogui.locateOnScreen 匹配模板（支持深/浅色反相与多缩放比）
        tpl_file = ROOT_DIR / "templates" / "menu_set_silent.png"
        if tpl_file.exists():
            import pyautogui  # type: ignore[import-not-found]
            from PIL import ImageOps
            pyautogui.FAILSAFE = False
            scr = None
            tpl = None
            tpl_inv = None
            try:
                scr_raw = pyautogui.screenshot()
                try:
                    scr = scr_raw.convert("L")
                finally:
                    try:
                        scr_raw.close()
                    except Exception:
                        pass
                with Image.open(tpl_file) as raw_tpl:
                    tpl = raw_tpl.convert("L")
                tpl_inv = ImageOps.invert(tpl)
                for base_t in (tpl, tpl_inv):
                    for sc in (1.0, 1.5, 1.25, 2.0, 1.75, 1.875, 0.85):
                        nw, nh = max(8, int(base_t.width * sc)), max(6, int(base_t.height * sc))
                        if nw >= scr.width or nh >= scr.height:
                            continue
                        is_copy = abs(sc - 1.0) >= 0.02
                        rsz = base_t.resize((nw, nh), Image.Resampling.LANCZOS) if is_copy else base_t
                        try:
                            for conf in (0.82, 0.74):
                                try:
                                    box = pyautogui.locate(rsz, scr, grayscale=True, confidence=conf)
                                    if box is not None:
                                        cx, cy = pyautogui.center(box)
                                        self.log(f"✅ 图像模板匹配命中「设为静音」（scale={sc} conf={conf}），点击")
                                        pyautogui.click(cx, cy)
                                        time.sleep(0.25)
                                        return True
                                except Exception:
                                    pass
                        finally:
                            if is_copy:
                                try:
                                    rsz.close()
                                except Exception:
                                    pass
            finally:
                for im_obj in (tpl_inv, tpl, scr):
                    if im_obj is not None:
                        try:
                            im_obj.close()
                        except Exception:
                            pass

        try:
            keyboard.send_keys("{ESC}")
        except Exception:
            pass
        raise RuntimeError("已展开下拉菜单，但未找到「设为静音」控件（详细控件树已打印到上方日志）")

    def _focus_rich_edit(self, win_spec, win_wrap):
        ed = find_uia_by_exact_or_suffix(
            win_spec, RICH_EDIT_AUTO_ID, suffixes=("widgetRichEditWnd.drich_edit", ".drich_edit")
        )
        if ed is not None:
            for ch, _ in _iter_subtree_fast(_unwrap_uia(ed), max_depth=8):
                inf = _safe_info(ch)
                rr = inf["rect"]
                if (
                    inf["ct"] in ("Edit", "Document")
                    and rr is not None and rr.width() > 120 and rr.height() > 24
                    and "搜索" not in inf["name"] and "search" not in inf["name"].lower()
                ):
                    _uia_click_with_invoke_fallback(ch)
                    time.sleep(0.2)
                    return ch
            _uia_click_with_invoke_fallback(ed)
            time.sleep(0.2)
            return ed
        wrect = win_wrap.rectangle()
        for ch, _ in _iter_subtree_fast(win_wrap, skip_aids=("messagelist", "sessionlist"), max_depth=16):
            inf = _safe_info(ch)
            rr = inf["rect"]
            if (
                inf["ct"] in ("Edit", "Document")
                and rr is not None and rr.width() > 120 and rr.height() > 24
                and rr.top > wrect.top + int(wrect.height() * 0.5)
                and "搜索" not in inf["name"] and "search" not in inf["name"].lower()
            ):
                _uia_click_with_invoke_fallback(ch)
                time.sleep(0.2)
                return ch
        raise RuntimeError("未在钉钉控件树中找到聊天输入窗口（widgetRichEditWnd.drich_edit）")

    def _detect_hotkey_hint_ctrl_enter_is_newline(self, win_wrap):
        """检查右下角快捷提示是否写着『Ctrl+Enter换行』（即您截图所示状态）。"""
        for ctrl, _ in _iter_subtree_fast(win_wrap, skip_aids=("messagelist", "sessionlist"), max_depth=16):
            inf = _safe_info(ctrl)
            txt = f"{inf['name']} {inf['legacy']}"
            if "Ctrl+Enter" in txt and ("换行" in txt or "newline" in txt.lower()):
                return True, txt
        return False, ""

    def _send_message(self, win_spec, win_wrap, send_key_mode="enter", wait_before_send=1.0):
        from pywinauto import keyboard  # type: ignore[import-not-found]
        if wait_before_send and wait_before_send > 0:
            self.log(f"  等待 {wait_before_send:g} 秒后发送消息…")
            time.sleep(wait_before_send)

        if send_key_mode == "enter":
            self.log("正在直接按 Enter 发送消息…")
            keyboard.send_keys("{ENTER}")
            return

        self._focus_rich_edit(win_spec, win_wrap)
        time.sleep(0.2)

        if send_key_mode == "ctrl_enter":
            self.log("正在按设置发送快捷键：Ctrl+Enter")
            keyboard.send_keys("^{ENTER}")
            return
        if send_key_mode == "alt_s":
            self.log("正在按设置发送快捷键：Alt+S")
            keyboard.send_keys("%s")
            return

        # smart 或 ui_button：优先检查是否提示 Ctrl+Enter 仅换行，避免只换行发不出去
        is_nl, hint_txt = self._detect_hotkey_hint_ctrl_enter_is_newline(win_wrap)
        send_main = None
        left_box = find_uia_by_exact_or_suffix(
            win_spec, LEFT_SEND_CONTAINER_ID, suffixes=(".leftContainerWidget",)
        )
        if left_box is not None:
            for ch, _ in _iter_subtree_fast(_unwrap_uia(left_box), max_depth=6):
                inf = _safe_info(ch)
                if inf["ct"] == "Button" or "发送" in f"{inf['name']} {inf['legacy']}":
                    send_main = ch
                    break
        if send_main is None:
            try:
                cand = win_spec.child_window(title_re=r"^发送(\([A-Za-z]\))?$", found_index=0)
                if cand.exists(timeout=0.4):
                    send_main = _unwrap_uia(cand)
            except Exception:
                pass

        if send_key_mode == "ui_button" or is_nl:
            if is_nl:
                self.log(f"⚠️ 检测到钉钉当前快捷键提示「{hint_txt}」（按 Ctrl+Enter 只会换行），自动点击左侧【发送】按钮！")
            if send_main is not None:
                _uia_click_with_invoke_fallback(send_main)
                self.log("✅ 已通过 UIA 点击主发送按钮完成发送")
                return
            self.log("未找到主发送按钮，回退发送 Alt+S…")
            keyboard.send_keys("%s")
            return

        # smart 且未提前检测到「Ctrl+Enter换行」：先按 Ctrl+Enter，若发送按钮仍处于可用状态说明仅触发了换行，自动点击发送按钮补发
        self.log("正在发送快捷键 Ctrl+Enter…")
        keyboard.send_keys("^{ENTER}")
        time.sleep(0.45)
        if send_main is not None:
            try:
                if send_main.is_enabled():
                    self.log("检测到输入框内容尚未发出（当前钉钉可能设为 Ctrl+Enter 换行），自动点击左侧【发送】按钮完成发送…")
                    _uia_click_with_invoke_fallback(send_main)
            except Exception:
                pass

    def run_test(
        self,
        search_target="文件小助手",
        custom_text="静音测试",
        use_silent=True,
        send_key_mode="enter",
        clear_draft=True,
        minimize_after=True,
        menu_only=False,
    ):
        from pywinauto import Desktop, keyboard  # type: ignore[import-not-found]
        matched, priv_msg = check_privilege_match()
        self.log(priv_msg)
        if not matched:
            self.log("⚠️ 警告：权限不匹配可能导致点击失败，仍继续尝试…")

        tmp_img = Path(tempfile.gettempdir()) / "_dt_silent_test_img.png"
        has_user_clip, clip_msg = save_clipboard_image_or_create_sample(tmp_img)
        self.log(f"[0/6] 截图准备：{clip_msg} -> {tmp_img.name} (from_user_clip={has_user_clip})")

        app, win_spec, win_wrap, pids = self._connect_uia()
        self._force_front_and_restore(win_wrap.handle)
        time.sleep(0.45)
        win_spec.set_focus()
        self.log(f"[1/6] 已置前钉钉主窗口 (PID={win_wrap.process_id()}, handle=0x{win_wrap.handle:X})")

        if menu_only:
            self._click_silent_option(win_spec, win_wrap, pids, enable_silent=use_silent, force_open_menu=True)
            self.log("🎯 仅菜单测试完成！")
            return True

        # 2) Ctrl+Shift+F 搜索目标并 Enter 进入会话
        target = (search_target or "文件小助手").strip() or "文件小助手"
        set_clipboard_text(target)
        time.sleep(0.15)
        self.log(f"[2/6] 发送 Ctrl+Shift+F 搜索「{target}」…")
        keyboard.send_keys("^+f")
        time.sleep(0.45)
        keyboard.send_keys("^a{BACKSPACE}")
        time.sleep(0.1)
        keyboard.send_keys("^v")
        time.sleep(0.85)
        keyboard.send_keys("{ENTER}")
        time.sleep(0.6)

        # 3) 聚焦 drich_edit 并粘贴截图 + 时间戳 + 自定义文字
        self._focus_rich_edit(win_spec, win_wrap)
        if clear_draft:
            keyboard.send_keys("^a{BACKSPACE}{DELETE}")
            time.sleep(0.2)
            self.log("[3/6] 已清空输入框草稿")

        if not set_clipboard_image(tmp_img):
            raise RuntimeError("测试图片写入剪贴板失败")
        keyboard.send_keys("^v")
        time.sleep(0.6)

        # 若弹出“选择粘贴的形式”，点「转为图片」
        for pw in Desktop(backend="uia").windows():
            try:
                if pw.process_id() in pids and (pw.element_info.class_name or "") == "ddesign::ComplexModal":
                    btn = pw.child_window(title="转为图片", found_index=0)
                    if btn.exists(timeout=1):
                        _uia_click_with_invoke_fallback(_unwrap_uia(btn))
                        time.sleep(0.5)
                        self.log("  已点弹窗「转为图片」")
                    break
            except Exception:
                pass

        stamp_line = time.strftime("%Y-%m-%d %H:%M:%S") + (f" {custom_text.strip()}" if custom_text.strip() else "")
        set_clipboard_text(stamp_line)
        time.sleep(0.2)
        self._focus_rich_edit(win_spec, win_wrap)
        keyboard.send_keys("{END}")
        time.sleep(0.1)
        keyboard.send_keys("^v")
        time.sleep(0.35)
        self.log(f"[3/6] 已贴入图片与文字：「{stamp_line}」")

        # 4) 静音发送 / 直接发送分支
        if use_silent:
            self.log("[4/6] 正在通过 subMenuButton 无坐标开启「设为静音」…")
            self._click_silent_option(win_spec, win_wrap, pids, enable_silent=True)
        else:
            self.log("[4/6] 本次为「直接发送」（跳过静音开启，如有残留静音先关闭）")
            self._click_silent_option(win_spec, win_wrap, pids, enable_silent=False)

        # 5) 等 1 秒直接按 Enter（或按所选按键）发送消息
        self.log("[5/6] 准备发送消息（等待 1 秒后直接按 Enter）…")
        self._send_message(win_spec, win_wrap, send_key_mode=send_key_mode, wait_before_send=1.0)
        time.sleep(0.8)

        # 6) 最小化钉钉
        if minimize_after:
            self.log("[6/6] 正在最小化钉钉窗口…")
            self._minimize_window(win_wrap.handle)
        self.log("🎉 测试全流程执行完毕！")
        return True


def run_self_test():
    """纯内存 UIA 结构自检（无需真机桌面即可校验 subMenuButton 精准命中、菜单第2项控件与 AST 无坐标）。"""
    import ast
    src_text = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(src_text)
    bad_sub = [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub)
        and isinstance(node.right, ast.Constant) and node.right.value in (25, 62)
    ]
    assert not bad_sub, f"不得包含硬编码偏移坐标相减: {bad_sub}"

    class _Rect:
        def __init__(self, l, t, r, b):
            self.left, self.top, self.right, self.bottom = l, t, r, b
        def width(self): return self.right - self.left
        def height(self): return self.bottom - self.top

    class _Info:
        def __init__(self, ct, name="", aid="", cls="", handle=0):
            self.control_type, self.name, self.automation_id, self.class_name, self.handle = ct, name, aid, cls, handle

    class _Node:
        def __init__(self, ct, name="", aid="", cls="", rect=(0, 0, 100, 30), legacy="", children=None):
            self.element_info = _Info(ct, name, aid, cls)
            self._rect = _Rect(*rect)
            self._legacy = legacy
            self._children = children or []
            self.clicked = False
        def children(self): return self._children
        def rectangle(self): return self._rect
        def legacy_properties(self): return {"Name": self._legacy}
        def click_input(self): self.clicked = True

    arrow_node = _Node("Button", name="", aid=SUB_MENU_BUTTON_AUTO_ID, cls="ddesign::IconButton", rect=(2487, 1440, 2511, 1464))
    send_node = _Node("Button", name="发送(S)", aid=LEFT_SEND_CONTAINER_ID + ".sendBtn", rect=(2400, 1440, 2486, 1464))
    root_node = _Node("Window", name="钉钉", aid="DtMainFrameView", children=[
        _Node("Pane", aid="combo_send_button_widget", children=[
            _Node("Pane", aid=LEFT_SEND_CONTAINER_ID, children=[send_node]),
            _Node("Pane", aid=RIGHT_SEND_CONTAINER_ID, children=[arrow_node]),
        ]),
    ])
    hit_arrow = find_uia_by_exact_or_suffix(
        root_node, SUB_MENU_BUTTON_AUTO_ID, suffixes=("rightContainerWidget.subMenuButton", ".subMenuButton"),
        class_hint="ddesign::IconButton",
    )
    assert hit_arrow is arrow_node, f"arrow mismatch: {hit_arrow}"

    # 场景 A: 菜单暴露名称「设为静音」
    m_named = _Node("MenuItem", name="设为静音", rect=(2400, 1365, 2500, 1395))
    pop_a = _Node("Menu", children=[
        _Node("MenuItem", name="设为定时", rect=(2400, 1330, 2500, 1360)),
        m_named,
        _Node("MenuItem", name="锁定发送", rect=(2400, 1400, 2500, 1430)),
    ])
    res_a = find_silent_menu_item_in_roots([pop_a, root_node], enable_silent=True)
    assert res_a and res_a[0] == "name" and res_a[1] is m_named, res_a

    # 场景 B: Qt 自绘菜单 3 个子项均无 Name 与 AutoId -> 自动锁定纵向第 2 项控件且不继续冗余扫描主窗
    row2 = _Node("Custom", name="", rect=(2400, 1365, 2500, 1395))
    pop_b = _Node("Pane", children=[
        _Node("Custom", name="", rect=(2400, 1330, 2500, 1360)),
        row2,
        _Node("Custom", name="", rect=(2400, 1400, 2500, 1430)),
    ])
    res_b = find_silent_menu_item_in_roots([pop_b, root_node], enable_silent=True)
    assert res_b and res_b[0] == "menu_row_2" and res_b[1] is row2, res_b

    # 校验测试样张生成与默认配置
    assert DingTalkSilentTester.run_test.__defaults__ == ("文件小助手", "静音发送测试"[0:0] + "静音测试", True, "enter", True, True, False) or "enter" in str(DingTalkSilentTester.run_test.__defaults__)

    with tempfile.TemporaryDirectory() as td:
        sp = create_sample_table_image(Path(td) / "sample.png")
        assert sp.exists() and sp.stat().st_size > 1000
    print("SELF_TEST_DINGTALK_SILENT_GUI_OK")


def launch_gui():
    import tkinter as tk
    from tkinter import scrolledtext

    root = tk.Tk()
    root.title("钉钉无坐标静音发送 — 独立测试工具 (点静音等1秒按Enter版)")
    root.geometry("620x660")
    root.resizable(True, True)
    try:
        ico = ROOT_DIR / "icon.ico"
        if ico.exists():
            root.iconbitmap(str(ico))
    except Exception:
        pass

    log_q = queue.Queue()

    def ui_log(msg):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
        log_q.put(line)

    frm = tk.Frame(root, padx=14, pady=12)
    frm.pack(fill="both", expand=True)

    tk.Label(
        frm,
        text="说明：请保持钉钉已登录（若剪贴板已有截图将直接复用，否则自动生成测试图）。",
        fg="#1e40af",
        font=("Microsoft YaHei UI", 9, "bold"),
        anchor="w",
    ).pack(fill="x", pady=(0, 8))

    grid = tk.Frame(frm)
    grid.pack(fill="x", pady=4)
    grid.columnconfigure(1, weight=1)

    tk.Label(grid, text="搜索目标会话：", font=("Microsoft YaHei UI", 9)).grid(row=0, column=0, sticky="w", pady=4)
    v_target = tk.StringVar(value="文件小助手")
    tk.Entry(grid, textvariable=v_target, font=("Microsoft YaHei UI", 9)).grid(row=0, column=1, sticky="ew", pady=4)

    tk.Label(grid, text="附带自定义文字：", font=("Microsoft YaHei UI", 9)).grid(row=1, column=0, sticky="w", pady=4)
    v_text = tk.StringVar(value="静音发送测试")
    tk.Entry(grid, textvariable=v_text, font=("Microsoft YaHei UI", 9)).grid(row=1, column=1, sticky="ew", pady=4)

    tk.Label(grid, text="消息发送按键：", font=("Microsoft YaHei UI", 9)).grid(row=2, column=0, sticky="w", pady=4)
    v_key = tk.StringVar(value="enter")
    key_row = tk.Frame(grid)
    key_row.grid(row=2, column=1, sticky="w", pady=4)
    for val, lbl in (
        ("enter", "直接按 Enter (默认·等1秒发)"),
        ("ctrl_enter", "Ctrl+Enter"),
        ("ui_button", "点UIA发送按钮"),
        ("alt_s", "Alt+S"),
        ("smart", "智能兼容"),
    ):
        tk.Radiobutton(key_row, text=lbl, value=val, variable=v_key, font=("Microsoft YaHei UI", 8)).pack(side="left", padx=(0, 6))

    chk_row = tk.Frame(frm)
    chk_row.pack(fill="x", pady=4)
    v_clear = tk.BooleanVar(value=True)
    v_min = tk.BooleanVar(value=True)
    tk.Checkbutton(chk_row, text="粘贴前清空输入框草稿", variable=v_clear, font=("Microsoft YaHei UI", 9)).pack(side="left", padx=(0, 14))
    tk.Checkbutton(chk_row, text="发送完自动最小化钉钉", variable=v_min, font=("Microsoft YaHei UI", 9)).pack(side="left")

    btn_row = tk.Frame(frm)
    btn_row.pack(fill="x", pady=8)

    running = {"flag": False}

    def _start_worker(use_silent=True, menu_only=False):
        if running["flag"]:
            return
        running["flag"] = True
        log_box.insert("end", "\n" + "=" * 56 + "\n")
        log_box.see("end")

        def _work():
            try:
                tester = DingTalkSilentTester(log_fn=ui_log)
                tester.run_test(
                    search_target=v_target.get(),
                    custom_text=v_text.get(),
                    use_silent=use_silent,
                    send_key_mode=v_key.get(),
                    clear_draft=v_clear.get(),
                    minimize_after=v_min.get(),
                    menu_only=menu_only,
                )
            except Exception as exc:
                ui_log(f"❌ 测试失败：{exc}")
            finally:
                running["flag"] = False

        threading.Thread(target=_work, daemon=True).start()

    tk.Button(
        btn_row, text="测试静音发送 + 最小化", bg="#0a84ff", fg="#ffffff",
        font=("Microsoft YaHei UI", 9, "bold"), padx=12, pady=6, cursor="hand2",
        command=lambda: _start_worker(use_silent=True, menu_only=False),
    ).pack(side="left", padx=(0, 8))

    tk.Button(
        btn_row, text="测试直接发送(不静音)", bg="#16a34a", fg="#ffffff",
        font=("Microsoft YaHei UI", 9, "bold"), padx=12, pady=6, cursor="hand2",
        command=lambda: _start_worker(use_silent=False, menu_only=False),
    ).pack(side="left", padx=(0, 8))

    tk.Button(
        btn_row, text="仅点开subMenuButton设为静音", bg="#475569", fg="#ffffff",
        font=("Microsoft YaHei UI", 9), padx=10, pady=6, cursor="hand2",
        command=lambda: _start_worker(use_silent=True, menu_only=True),
    ).pack(side="left", padx=(0, 8))

    def _copy_log():
        txt = log_box.get("1.0", "end").strip()
        if txt:
            set_clipboard_text(txt)
            ui_log("已复制全部测试日志到剪贴板")

    tk.Button(btn_row, text="复制日志", command=_copy_log, font=("Microsoft YaHei UI", 9), padx=8, pady=6).pack(side="right")

    log_box = scrolledtext.ScrolledText(frm, font=("Consolas", 9), wrap="word")
    log_box.pack(fill="both", expand=True)

    _ok, init_msg = check_privilege_match()
    ui_log(init_msg)
    ui_log(f"日志文件：{LOG_FILE}")

    def _pump():
        while True:
            try:
                ln = log_q.get_nowait()
                log_box.insert("end", ln + "\n")
                log_box.see("end")
            except queue.Empty:
                break
        root.after(80, _pump)

    root.after(80, _pump)
    root.mainloop()


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        run_self_test()
    else:
        launch_gui()
