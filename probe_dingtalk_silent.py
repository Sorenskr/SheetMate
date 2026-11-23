# -*- coding: utf-8 -*-
# pyright: reportMissingImports=false, reportMissingModuleSource=false
"""探测本机钉钉主窗口及「静音发送」下拉菜单的 UIA 控件树（control_type / name / automation_id / class_name）。"""

import ctypes
from ctypes import wintypes
import os
import subprocess
import time
from pathlib import Path

OUT_LOG = Path(__file__).resolve().parent / "probe_dingtalk_controls.log"
TPL_DIR = Path(__file__).resolve().parent / "templates"


def is_process_elevated(pid=None):
    """返回 True（管理员权限）、False（普通用户权限）或 None（无法判定）。"""
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
            if ctypes.get_last_error() == 5 and is_process_elevated(None) is False:
                return True
            return None
        close_proc = True

    tok = wintypes.HANDLE()
    try:
        if not adv32.OpenProcessToken(proc_handle, TOKEN_QUERY, ctypes.byref(tok)):
            if ctypes.get_last_error() == 5 and pid is not None and is_process_elevated(None) is False:
                return True
            return None
        try:
            elev = TOKEN_ELEVATION()
            ret_len = wintypes.DWORD()
            ok = adv32.GetTokenInformation(
                tok, TokenElevation, ctypes.byref(elev), ctypes.sizeof(elev), ctypes.byref(ret_len)
            )
            if not ok:
                return None
            return bool(elev.TokenIsElevated)
        finally:
            k32.CloseHandle(tok)
    finally:
        if close_proc and proc_handle:
            k32.CloseHandle(proc_handle)


def list_dingtalk_processes():
    """枚举当前正在运行的 DingTalk.exe 进程，返回 [(pid, exe_path), ...]。"""
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
                exe_path = "DingTalk.exe"
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


def check_same_privilege(procs):
    """检测脚本与 DingTalk.exe 是否运行在同一权限级别（避免 UIPI 静默拦截）。"""
    my_elev = is_process_elevated(None)
    my_lbl = "管理员权限" if my_elev else ("普通用户权限" if my_elev is False else "未知权限")
    if not procs:
        return True, f"[权限检测] 当前脚本={my_lbl}（暂未检测到运行中的 DingTalk.exe）"

    ding_elev = None
    details = []
    for pid, ppath in procs:
        ev = is_process_elevated(pid)
        if ev is not None:
            ding_elev = ev
        details.append(f"  PID={pid}, elevated={ev}, path={ppath}")

    ding_lbl = "管理员权限" if ding_elev else ("普通用户权限" if ding_elev is False else "未知权限")
    matched = (my_elev is None) or (ding_elev is None) or (my_elev == ding_elev)
    if matched:
        head = f"[权限检测] 校验通过：脚本与钉钉同为【{my_lbl}】"
    else:
        head = (
            f"⚠️ [权限不一致警告] 当前脚本为【{my_lbl}】，而 DingTalk.exe 为【{ding_lbl}】！\n"
            "  → 权限不一致会导致 pywinauto 无法读取/点击钉钉控件，请以与钉钉相同的权限重新运行脚本或重启钉钉！"
        )
    return matched, "\n".join([head] + details)


def connect_dingtalk_uia(lines):
    """按要求用 pywinauto(backend='uia') 通过 connect(path='DingTalk.exe') 连接钉钉，不使用窗口标题。"""
    from pywinauto import Application, Desktop

    procs = list_dingtalk_processes()
    matched, priv_msg = check_same_privilege(procs)
    print(priv_msg)
    lines.extend(priv_msg.splitlines())
    if not matched:
        lines.append("⚠️ 请先统一权限后再试，以下仍尝试探测当前桌面可见控件…")

    exe_path = procs[0][1] if procs else "DingTalk.exe"
    desk = Desktop(backend="uia")
    pids_set = {p for p, _ in procs}
    ui_win = None

    def _find_ui_win(pid_filter):
        for w in desk.windows():
            try:
                if pid_filter and w.process_id() not in pid_filter:
                    continue
                cls = (w.element_info.class_name or "")
                if cls in ("DtMainFrameView", "Qt51511QWindowIcon"):
                    return w
                if w.is_visible() and w.rectangle().width() >= 500:
                    return w
            except Exception:
                pass
        return None

    ui_win = _find_ui_win(pids_set)

    # 如果所有进程都没有可见主窗，尝试通过进程可执行文件唤起托盘钉钉
    if ui_win is None and os.path.exists(exe_path):
        lines.append(f"[唤起] 未见主窗口，正在唤起 {exe_path} ...")
        subprocess.Popen([exe_path])
        for _ in range(20):
            time.sleep(0.5)
            procs = list_dingtalk_processes()
            pids_set = {p for p, _ in procs}
            ui_win = _find_ui_win(pids_set)
            if ui_win is not None:
                break

    # 首先显式使用 connect(path="DingTalk.exe") 连接；若多进程环境下命中无窗子 PID，切换绑定 UI 主窗 PID
    app = None
    try:
        app = Application(backend="uia").connect(path="DingTalk.exe", timeout=5)
        if ui_win is not None and app.process != ui_win.process_id():
            lines.append(
                f"[连接说明] connect(path='DingTalk.exe') 首个命中 PID={app.process}，"
                f"检测到拥有主窗的 UI 进程 PID={ui_win.process_id()}，自动切换绑定该 UI 进程"
            )
            app = Application(backend="uia").connect(process=ui_win.process_id(), timeout=5)
        else:
            cand = app.window(class_name="DtMainFrameView")
            if not cand.exists(timeout=1):
                top_cand = app.top_window()
                if not top_cand.exists(timeout=1):
                    raise RuntimeError("connect(path='DingTalk.exe') 命中无可见窗口子进程")
    except Exception as e:
        lines.append(f"[连接说明] connect(path='DingTalk.exe') 需要切换到 UI 主进程：{e}")
        if ui_win is not None:
            app = Application(backend="uia").connect(process=ui_win.process_id(), timeout=5)
            lines.append(f"[连接说明] 已绑定 DingTalk.exe UI 主窗 PID={ui_win.process_id()}")
        elif procs:
            app = Application(backend="uia").connect(process=procs[0][0], timeout=5)
        else:
            raise RuntimeError("本机未运行 DingTalk.exe")

    if ui_win is None:
        cand = app.window(class_name="DtMainFrameView")
        ui_win = cand if cand.exists(timeout=1.5) else app.top_window()
    if hasattr(ui_win, "wrapper_object"):
        ui_win = ui_win.wrapper_object()
    return app, ui_win, pids_set


def dump_tree(ctrl, lines, max_depth=18, depth=0, focus_bottom=False, win_rect=None):
    if depth > max_depth:
        return
    try:
        info = ctrl.element_info
        ctype = info.control_type or ""
        name = (info.name or "").replace("\r", " ").replace("\n", " ")
        aid = info.automation_id or ""
        cls = info.class_name or ""
        rr = ctrl.rectangle()
        keep = True
        if focus_bottom and win_rect is not None and depth >= 4:
            # 对发送区重点聚焦，但父容器全保留以保证树完整
            keep = (rr.top >= win_rect.top + (win_rect.bottom - win_rect.top) * 0.55) or (rr.width() >= 300)
        if keep:
            indent = "  " * depth
            lines.append(
                f"{indent}[{ctype}] name={name!r} auto_id={aid!r} cls={cls!r} rect=({rr.left},{rr.top},{rr.right},{rr.bottom})"
            )
    except Exception as e:
        lines.append(f"{'  '*depth}<err: {e}>")
        return

    try:
        children = ctrl.children()
    except Exception:
        children = []
    for ch in children:
        dump_tree(ch, lines, max_depth=max_depth, depth=depth + 1, focus_bottom=focus_bottom, win_rect=win_rect)


def find_first(ctrl, pred):
    try:
        if pred(ctrl):
            return ctrl
    except Exception:
        pass
    try:
        for ch in ctrl.children():
            res = find_first(ch, pred)
            if res is not None:
                return res
    except Exception:
        pass
    return None


def _describe_ctrl(ctrl):
    info = ctrl.element_info
    rr = ctrl.rectangle()
    return info, rr, (
        f"type={info.control_type!r} name={info.name!r} "
        f"auto_id={info.automation_id!r} cls={info.class_name!r} "
        f"rect=({rr.left},{rr.top},{rr.right},{rr.bottom})"
    )


def _pick_arrow_from_candidates(cands, send_info, send_rect):
    for sib in cands:
        try:
            sbi = sib.element_info
            if sbi == send_info:
                continue
            sbr = sib.rectangle()
            if sbi.control_type not in ("Button", "MenuItem", "SplitButton", "Custom", "Image", "Pane"):
                continue
            if sbr.width() <= 0 or sbr.height() < 12:
                continue
            sname = (sbi.name or "").strip()
            said = (sbi.automation_id or "").lower()
            if (
                any(k in said for k in ("arrow", "drop", "more", "menu"))
                or any(k in sname for k in ("更多", "设置", "选项", "下拉"))
                or (sbr.left >= send_rect.right - 26 and sbr.width() <= 45 and sbr.height() >= 14)
            ):
                return sib
        except Exception:
            pass
    return None


def main():
    lines = [f"=== 钉钉 UIA 控件树探测报告 ({time.strftime('%Y-%m-%d %H:%M:%S')}) ==="]
    try:
        from pywinauto import Desktop, keyboard
    except Exception as e:
        OUT_LOG.write_text(f"pywinauto 导入失败: {e}\n", encoding="utf-8")
        raise

    try:
        _app, win, pids_set = connect_dingtalk_uia(lines)
        win.set_focus()
        time.sleep(0.4)
        wrect = win.rectangle()
        lines.append(
            f"\n[主窗口信息] cls={win.element_info.class_name!r}, "
            f"rect=({wrect.left},{wrect.top},{wrect.right},{wrect.bottom}), pid={win.process_id()}"
        )

        # 打印完整主窗口下半部分与全局核心控件树
        lines.append("\n--- 1. 主窗口控件树（重点含底部发送区与工具栏）---")
        dump_tree(win, lines, max_depth=22, focus_bottom=True, win_rect=wrect)

        # 在聊天编辑区查找（不含搜索框），若有“发送”按钮或下拉控件则直接探测菜单
        send_ctrl = find_first(
            win,
            lambda c: "发送" in (c.element_info.name or "") and c.rectangle().top >= wrect.top + wrect.height() * 0.78,
        )
        if send_ctrl is not None:
            si, sr, sdesc = _describe_ctrl(send_ctrl)
            lines.append(f"\n[命中发送控件] {sdesc}")

            child_cands = []
            try:
                child_cands = send_ctrl.children()
                lines.append(f"[发送控件内部子控件列表（共 {len(child_cands)} 个）]:")
                for idx, ch in enumerate(child_cands):
                    _, _, cdesc = _describe_ctrl(ch)
                    lines.append(f"  [child-{idx}] {cdesc}")
            except Exception as ec:
                lines.append(f"[获取发送按钮内部子控件异常]: {ec}")

            sib_cands = []
            try:
                parent = send_ctrl.parent()
                sib_cands = parent.children() if parent is not None else []
                lines.append(f"[发送控件同级兄弟控件列表（共 {len(sib_cands)} 个）]:")
                for idx, sib in enumerate(sib_cands):
                    _, _, sbdesc = _describe_ctrl(sib)
                    lines.append(f"  [sib-{idx}] {sbdesc}")
            except Exception as ep:
                lines.append(f"[获取发送按钮兄弟控件异常]: {ep}")

            # 优先从发送控件子级查找，再从兄弟控件查找下拉箭头
            arrow_ctrl = _pick_arrow_from_candidates(child_cands, si, sr)
            if arrow_ctrl is None:
                arrow_ctrl = _pick_arrow_from_candidates(sib_cands, si, sr)

            if arrow_ctrl is not None:
                ai, _ar, adesc = _describe_ctrl(arrow_ctrl)
                lines.append(f"\n[尝试点击下拉箭头] {adesc}")
                arrow_ctrl.click_input()
                time.sleep(0.5)
                lines.append("\n--- 2. 点击下拉箭头 0.5s 后所有可见弹出窗/菜单控件树 ---")
                desk = Desktop(backend="uia")
                for pw in desk.windows():
                    try:
                        if pw.process_id() in pids_set and pw.element_info.handle != win.element_info.handle:
                            lines.append(f"[弹出窗] cls={pw.element_info.class_name!r} name={pw.element_info.name!r}")
                            dump_tree(pw, lines, max_depth=16)
                    except Exception:
                        pass
                # 同时扫描主窗内可能新挂出的菜单层
                dump_tree(win, lines, max_depth=16, focus_bottom=True, win_rect=wrect)
                keyboard.send_keys("{ESC}")
            else:
                lines.append("\n[提示] 命中『发送』控件，但未在其子级或同级找到明显的下拉箭头控件。")
        else:
            lines.append("\n[提示] 当前主窗口底部未扫描到『发送』控件（可能停留在登录页、未点开具体会话，或发送区为自绘）。")
    except Exception as exc:
        lines.append(f"\n❌ [探测中止] {type(exc).__name__}: {exc}")
        lines.append(
            "  排查建议：1) 请在您本人当前登录的 Windows 桌面中打开钉钉并点进任意群聊窗口；\n"
            "  2) 确保运行本脚本与钉钉处于同一权限（同为普通用户或同为管理员，不可在隔离桌面或跨权限运行）；\n"
            "  3) 重新运行本脚本后查看生成的 probe_dingtalk_controls.log。"
        )
        print(f"⚠️ 探测未完成：{exc}")
    finally:
        OUT_LOG.write_text("\n".join(lines), encoding="utf-8")
        print(f"PROBE_DONE: {OUT_LOG} ({len(lines)} lines)")


if __name__ == "__main__":
    main()
