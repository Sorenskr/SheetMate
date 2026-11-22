# -*- coding: utf-8 -*-
# pyright: reportMissingImports=false, reportMissingModuleSource=false
"""SheetMate v2.8.7 多端协同控制中心界面、动态昼夜光效与纯云端异地调度后端。"""

import copy
import ctypes
from ctypes import wintypes
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

MAX_UI_BODY_BYTES = 2 * 1024 * 1024


def _safe_int(val, default=0):
    try:
        return int(val)
    except (TypeError, ValueError):
        return default

try:
    import team_hub
except Exception:
    team_hub = None

try:
    import mobile_bridge
except Exception:
    mobile_bridge = None

STEP_PROGRESS = {
    "正在打开浏览器…": 0.06,
    "正在打开WPS汇总表…": 0.16,
    "正在登录后台…": 0.18,
    "正在下载三份数据…": 0.32,
    "正在整理数据格式…": 0.45,
    "正在写入在线表格…": 0.60,
    "正在切换到数据汇总表…": 0.72,
    "正在校准汇总表筛选项…": 0.74,
    "正在等待汇总表刷新…": 0.76,
    "正在复制表格图片…": 0.84,
    "正在关闭后台浏览器…": 0.89,
    "正在发送到钉钉…": 0.95,
    "✅ 本轮跑完": 1.0,
}

_DECORATIVE_SYMBOL_RE = re.compile(
    r"[\u2600-\u27BF\U0001F300-\U0001FAFF\u23F0-\u23FA\u25A0-\u25FF\uFE0F]+"
)


def _strip_symbols(text):
    """剥离界面展示文案中的粗糙 Windows Emoji，仅保留干净文字。"""
    return _DECORATIVE_SYMBOL_RE.sub("", str(text or "")).strip()


def _find_app_shell_browser():
    """查找可作为独立 --app 窗口启动的 Chromium 内核浏览器。"""
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path, "edge_or_chrome"

    pw_base = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or (
        Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ms-playwright"
    ))
    if pw_base.exists():
        for d in sorted(pw_base.glob("chromium-*"), reverse=True):
            exe = d / "chrome-win64" / "chrome.exe"
            if exe.exists():
                return str(exe), "chromium"
    return None, None


def _apply_square_rounded_win32(wx, wy, win_w, win_h=None):
    """为桌面 --app 窗口应用 Windows 11 原生四角大圆角，若旧记忆尺寸过小则扩至宽屏尺寸。"""
    if win_h is None:
        win_h = win_w
    if os.name != "nt":
        return
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wintypes.UINT,
        ]
        dwmapi.DwmSetWindowAttribute.argtypes = [
            wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
        ]
        dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long

        def _scan():
            styled = False
            resized = False
            for _ in range(28):
                found_hwnd = 0

                def _cb(hwnd, _lp):
                    nonlocal found_hwnd
                    if not user32.IsWindowVisible(hwnd):
                        return True
                    tbuf = ctypes.create_unicode_buffer(256)
                    cbuf = ctypes.create_unicode_buffer(128)
                    user32.GetWindowTextW(hwnd, tbuf, 256)
                    user32.GetClassNameW(hwnd, cbuf, 128)
                    title = tbuf.value or ""
                    cls = cbuf.value or ""
                    if "暗号表助手" in title and cls.startswith("Chrome_WidgetWin"):
                        found_hwnd = int(hwnd)
                        return False
                    return True

                user32.EnumWindows(WNDENUMPROC(_cb), 0)
                if found_hwnd:
                    if not styled:
                        try:
                            # DWMWA_WINDOW_CORNER_PREFERENCE = 33, DWMWCP_ROUND = 2
                            pref = ctypes.c_int(2)
                            dwmapi.DwmSetWindowAttribute(found_hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
                            styled = True
                        except Exception:
                            pass
                    if not resized:
                        try:
                            rc = wintypes.RECT()
                            if user32.GetWindowRect(found_hwnd, ctypes.byref(rc)):
                                cur_w = rc.right - rc.left
                                cur_h = rc.bottom - rc.top
                                if cur_w < win_w - 24 or cur_h < win_h - 24:
                                    # SWP_NOZORDER=0x0004 | SWP_NOACTIVATE=0x0010
                                    user32.SetWindowPos(found_hwnd, 0, int(wx), int(wy), int(win_w), int(win_h), 0x0014)
                            resized = True
                        except Exception:
                            pass
                    if styled and resized:
                        break
                time.sleep(0.25)

        threading.Thread(target=_scan, daemon=True).start()
    except Exception:
        pass


class _SheetMateController:
    """线程安全的配置、日志、结构化错误诊断、AI 助手与自动值守调度控制器。"""

    def __init__(self, app, glass, tk_root):
        self.app = app
        self.glass = glass
        self.tk_root = tk_root
        self._lock = threading.RLock()
        self._task_lock = threading.Lock()
        self._run_epoch = 0

        self.cfg = app.load_config()
        self.logs = []
        self._log_seq = 0

        self.job_running = False
        self.capture_running = False
        self.schedule_running = False
        self.stop_flag = False
        self.need_login_prompt = False
        self.runtime_state = app.get_runtime_state() if hasattr(app, "get_runtime_state") else {}

        self.status_level = "idle"
        self.step_text = "空闲中，等待开始"
        self.progress = 0.0
        self.error_text = ""
        self.error_detail = None
        self.ai_busy = False
        self.ai_result = None
        self._last_auto_diag_sig = ""
        self.ai_chat_history = []
        self._chat_seq = 0
        self.latest_spotlight = None
        self._spotlight_seq = 0
        self._startup_greeted = False
        self._startup_greeting_data = None
        self.last_run_str = "—"
        self.next_run_str = "—"
        self.next_run_ts = 0.0
        self.last_heartbeat = time.time()
        self.close_requested_at = 0.0

        self.update_info = None
        self._brief_cache = (0.0, {})
        self._run_started_at = 0.0
        self._run_mode = "full"
        self._run_trigger_type = "manual"
        self._holds_team_lock = False
        self._queue_auto_starting = False
        self._last_sched_busy_log = 0.0

        base_dir = self.app.app_dir() if hasattr(self.app, "app_dir") else Path.cwd()
        self.hub = team_hub.TeamHubManager(base_dir, app=self.app) if team_hub is not None else None
        self._refresh_operator_name()
        self.mobile_bridge = mobile_bridge.MobileBridgeManager(self) if mobile_bridge is not None else None
        if self.mobile_bridge is not None:
            try:
                self.mobile_bridge.start()
            except Exception:
                pass
            try:
                mobile_bridge.export_static_mobile_web(Path(base_dir) / "mobile_web.html")
                if (Path(base_dir) / ".git").exists():
                    mobile_bridge.export_github_pages_site(Path(base_dir) / "docs")
            except Exception:
                pass

        app.set_gui_logger(self._on_log)
        app.set_step_callback(self._on_step)
        app.set_last_run_callback(self._on_last_run)
        app.set_login_prompt(self._on_login_prompt_show, self._on_login_prompt_close)
        if hasattr(app, "set_runtime_callback"):
            app.set_runtime_callback(self._on_runtime_state)
        if hasattr(app, "set_spotlight_callback"):
            app.set_spotlight_callback(self._on_data_spotlight)
        self.attach_glass(glass, tk_root)

        threading.Thread(target=self._check_privilege_startup, daemon=True).start()
        threading.Thread(target=self._check_update_worker, daemon=True).start()
        threading.Thread(target=self._hub_background_loop, daemon=True).start()

    def _refresh_operator_name(self):
        if self.hub is None or not hasattr(self.app, "set_active_operator_name"):
            return
        try:
            prof = self.hub.get_profile()
            self.app.set_active_operator_name(prof.get("display_name") or prof.get("member_code") or "当前成员")
        except Exception:
            pass

    def attach_glass(self, glass, tk_root):
        with self._lock:
            self.glass = glass
            self.tk_root = tk_root
        if glass is not None and hasattr(glass, "set_callbacks"):
            glass.set_callbacks(
                on_pause=self.pause_current_task,
                on_resume=self.resume_current_task,
                on_cancel=self.stop_current_task,
            )

    def _check_privilege_startup(self):
        try:
            if hasattr(self.app, "check_dingtalk_privilege_match"):
                priv = self.app.check_dingtalk_privilege_match()
                msg = priv.get("message", "")
                if not priv.get("matched", True):
                    self._on_log(f"⚠️ {msg}")
                    det = self.app.classify_error_detail(RuntimeError(msg)) if hasattr(self.app, "classify_error_detail") else None
                    with self._lock:
                        self.error_text = _strip_symbols(msg)
                        self.error_detail = det
                elif msg:
                    self._on_log(msg)
        except Exception:
            pass

    def _check_update_worker(self):
        try:
            info = self.app.fetch_remote_version()
            if info and self.app._ver_newer(info[0], self.app.APP_VERSION):
                with self._lock:
                    self.update_info = {"version": info[0], "url": info[1]}
        except Exception:
            pass

    def _on_log(self, msg):
        raw = str(msg).strip()
        level = "info"
        if raw.startswith("✅"):
            level = "ok"
        elif raw.startswith("❌"):
            level = "err"
        elif raw.startswith("⚠️") or "警告" in raw or "未成功" in raw or "未命中" in raw or "尚未" in raw:
            level = "warn"
        elif raw.startswith("====") or raw.startswith("📊") or raw.startswith("📸"):
            level = "head"
        clean_msg = _strip_symbols(raw) or raw
        if re.fullmatch(r"=+", clean_msg):
            return
        with self._lock:
            self._log_seq += 1
            self.logs.append({
                "id": self._log_seq,
                "time": time.strftime("%H:%M:%S"),
                "level": level,
                "text": clean_msg,
            })
            if len(self.logs) > 600:
                self.logs = self.logs[-500:]

    def _on_step(self, text):
        raw = str(text)
        clean_text = _strip_symbols(raw) or raw
        with self._lock:
            if self.status_level == "stopped" and not (self.job_running or self.capture_running) and not raw.startswith(("⏹", "✅")):
                return
            self.step_text = clean_text
            prog = STEP_PROGRESS.get(raw) or STEP_PROGRESS.get(clean_text)
            if prog is not None:
                self.progress = prog
            if self.glass is not None and not self.glass.closed:
                self.glass.update_step(clean_text, prog if prog is not None else self.progress)
            if raw.startswith("✅"):
                self.status_level = "ok"
            elif raw.startswith("❌"):
                self.status_level = "err"
            elif raw.startswith("⏹"):
                self.status_level = "stopped"
            elif (self.runtime_state or {}).get("paused") or (self.runtime_state or {}).get("waiting_recovery") or raw.startswith("暂停"):
                self.status_level = "paused"
            elif self.job_running or self.capture_running:
                self.status_level = "running"

    def _on_last_run(self):
        with self._lock:
            self.last_run_str = time.strftime("%m-%d %H:%M")

    def _on_login_prompt_show(self):
        with self._lock:
            self.need_login_prompt = True
        self._on_log("请在弹出的浏览器中扫码登录 WPS，完成后将自动继续")

    def _on_login_prompt_close(self):
        with self._lock:
            self.need_login_prompt = False

    def _on_runtime_state(self, rt):
        auto_det = None
        with self._lock:
            self.runtime_state = rt if isinstance(rt, dict) else {}
            prompt = self.runtime_state.get("prompt") or {}
            mode = prompt.get("mode", "running")
            p_det = prompt.get("error_detail") if isinstance(prompt, dict) else None
            if self.runtime_state.get("paused") or mode == "waiting_fix":
                self.status_level = "paused"
                if self.runtime_state.get("paused") and mode == "paused":
                    self.step_text = "任务已暂停（点击继续可接着执行）"
                if mode == "waiting_fix" and isinstance(p_det, dict) and p_det.get("code"):
                    self.error_detail = dict(p_det)
                    auto_det = dict(p_det)
            elif self.job_running or self.capture_running:
                if self.status_level == "paused":
                    self.status_level = "running"
        if self.glass is not None and hasattr(self.glass, "set_runtime_prompt") and not self.glass.closed:
            self.glass.set_runtime_prompt(self.runtime_state)
        if auto_det is not None:
            self._maybe_auto_ai_diagnose(auto_det)

    def _maybe_auto_ai_diagnose(self, err_detail):
        if not isinstance(err_detail, dict) or not err_detail.get("code"):
            return
        with self._lock:
            sig = f"{err_detail.get('code')}|{err_detail.get('raw_error', '')}|{self._run_epoch}"
            if sig == self._last_auto_diag_sig:
                return
            self._last_auto_diag_sig = sig
            recent_logs = [f"[{it['time']}] {it['text']}" for it in self.logs[-15:]]
        if self.hub is not None:
            try:
                self.hub.record_member_error(err_detail, recent_logs=recent_logs)
            except Exception:
                pass
        self._trigger_auto_ai_diagnose(err_detail)

    def _on_data_spotlight(self, spot):
        if not isinstance(spot, dict):
            return
        reply_txt = str(spot.get("reply") or "").strip()
        if not reply_txt:
            return
        with self._lock:
            self._spotlight_seq += 1
            item = dict(spot)
            item["seq"] = self._spotlight_seq
            self.latest_spotlight = item
            self.ai_chat_history.append({
                "role": "model",
                "text": reply_txt,
                "time": item.get("time") or time.strftime("%H:%M:%S"),
                "model": "",
                "source": "data_spotlight",
                "shot_info": item.get("summary_line") or "本轮数据变动播报",
                "via": "pc",
            })
            if len(self.ai_chat_history) > 25:
                self.ai_chat_history = self.ai_chat_history[-25:]
            self._chat_seq += 1
        if spot.get("summary_line"):
            self._on_log(f"数据变化播报：{spot['summary_line']}")

    @staticmethod
    def _sanitize_config_for_client(cfg_dict):
        out = copy.deepcopy(cfg_dict) if isinstance(cfg_dict, dict) else {}
        ai = out.get("ai_assistant")
        if isinstance(ai, dict):
            for k in ("api_key", "api_base", "provider", "model"):
                ai.pop(k, None)
        return out

    def _merge_server_ai_secrets(self, incoming_cfg, old_cfg=None):
        out = copy.deepcopy(incoming_cfg) if isinstance(incoming_cfg, dict) else {}
        base = old_cfg if isinstance(old_cfg, dict) else self.cfg
        old_ai = base.get("ai_assistant") if isinstance(base.get("ai_assistant"), dict) else {}
        new_ai = out.get("ai_assistant")
        if not isinstance(new_ai, dict):
            new_ai = {}
            out["ai_assistant"] = new_ai
        for k in ("api_key", "api_base", "provider", "model"):
            if k not in new_ai or (k == "api_key" and not str(new_ai.get(k) or "").strip()):
                if old_ai.get(k) not in (None, ""):
                    new_ai[k] = old_ai[k]
        return out

    def get_public_config(self):
        return self._sanitize_config_for_client(self.get_config())

    def _build_runtime_ai_cfg(self, base_cfg=None):
        out = copy.deepcopy(base_cfg) if isinstance(base_cfg, dict) else self.get_config()
        if self.hub is not None and hasattr(self.hub, "resolve_ai_api_for_me"):
            try:
                resolved = self.hub.resolve_ai_api_for_me()
                if isinstance(resolved, dict):
                    ai = out.setdefault("ai_assistant", {})
                    if resolved.get("provider"):
                        ai["provider"] = resolved["provider"]
                    if resolved.get("api_base") is not None:
                        ai["api_base"] = resolved["api_base"]
                    if resolved.get("api_key"):
                        ai["api_key"] = resolved["api_key"]
                    if resolved.get("model") is not None:
                        ai["model"] = resolved["model"]
            except Exception:
                pass
        return out

    def get_startup_greeting(self, force=False):
        if not hasattr(self.app, "generate_unique_startup_greeting"):
            return {"ok": True, "show": False, "reply": "", "time": time.strftime("%H:%M")}
        with self._lock:
            if not force and self._startup_greeted and self._startup_greeting_data is not None:
                return dict(self._startup_greeting_data)
        nick = ""
        if self.hub is not None:
            try:
                prof = self.hub.get_profile() or {}
                nick = str(prof.get("nickname") or "").strip()
            except Exception:
                pass
        rt_cfg = self._build_runtime_ai_cfg()
        res = self.app.generate_unique_startup_greeting(rt_cfg, nickname=nick, force=bool(force))
        out = {"ok": True, **(res if isinstance(res, dict) else {})}
        if out.get("show") and str(out.get("reply") or "").strip():
            with self._lock:
                self._startup_greeted = True
                self._startup_greeting_data = dict(out)
        return out

    def _trigger_auto_ai_diagnose(self, err_detail):
        cfg_snap = self._build_runtime_ai_cfg()
        ai_cfg = cfg_snap.get("ai_assistant") if isinstance(cfg_snap.get("ai_assistant"), dict) else {}
        if not ai_cfg.get("enabled", True) or not ai_cfg.get("auto_diagnose", True):
            return
        with self._lock:
            recent = [f"[{it['time']}] {it['text']}" for it in self.logs[-25:]]
            self.ai_busy = True

        def _worker():
            try:
                if hasattr(self.app, "ask_gemini_assistant"):
                    res = dict(self.app.ask_gemini_assistant(
                        cfg_snap,
                        mode="diagnose",
                        error_detail=err_detail,
                        recent_logs=recent,
                    ))
                    res["model"] = ""
                else:
                    res = {"ok": False, "reply": "当前版本未启用内置 AI 诊断模块"}
                with self._lock:
                    self.ai_result = {
                        "mode": "diagnose",
                        "code": (err_detail or {}).get("code", ""),
                        "time": time.strftime("%H:%M:%S"),
                        **res,
                    }
            except Exception as exc:
                with self._lock:
                    self.ai_result = {
                        "mode": "diagnose",
                        "ok": False,
                        "source": "error",
                        "reply": f"AI 诊断遇到异常：{exc}",
                    }
            finally:
                with self._lock:
                    self.ai_busy = False

        threading.Thread(target=_worker, daemon=True).start()

    def ask_ai(self, mode="diagnose", question="", err_override=None):
        if self.hub is not None and mode == "ask":
            perms = self.hub.get_my_permissions()
            if not perms.get("can_use_ai", True):
                return {"ok": False, "reply": "当前对话功能已采用团队统一设置，如需开启请点击右上角【反馈】提交说明。"}
        cfg_snap = self._build_runtime_ai_cfg()
        with self._lock:
            recent = [f"[{it['time']}] {it['text']}" for it in self.logs[-25:]]
            det = err_override or self.error_detail
            if not det and self.error_text and hasattr(self.app, "classify_error_detail"):
                det = self.app.classify_error_detail(RuntimeError(self.error_text))
            self.ai_busy = True
        try:
            if mode == "test":
                res = self.app.ask_gemini_assistant(
                    cfg_snap,
                    mode="ask",
                    question="请用两句亲切简短的中文打招呼，并确认当前内置智能服务已连通可用。",
                )
            else:
                res = self.app.ask_gemini_assistant(
                    cfg_snap,
                    mode=mode,
                    error_detail=det,
                    recent_logs=recent,
                    question=str(question or "").strip(),
                )
            safe_res = dict(res) if isinstance(res, dict) else {"ok": False, "reply": "未返回结果"}
            safe_res["model"] = ""
            wrapped = {
                "mode": mode,
                "code": (det or {}).get("code", ""),
                "time": time.strftime("%H:%M:%S"),
                **safe_res,
            }
            if self.hub is not None and mode != "test":
                try:
                    u_txt = str(question or "").strip() or (f"诊断错误 {wrapped['code']}" if wrapped.get("code") else "请求 AI 排查")
                    self.hub.record_member_ai_chat("user", u_txt, {"mode": mode})
                    if wrapped.get("reply"):
                        self.hub.record_member_ai_chat("model", wrapped["reply"], {"mode": mode, "source": wrapped.get("source", "")})
                except Exception:
                    pass
            with self._lock:
                self.ai_result = wrapped
            return wrapped
        finally:
            with self._lock:
                self.ai_busy = False

    def trigger_ai_auto_heal(self):
        cfg_snap = self._build_runtime_ai_cfg()
        with self._lock:
            det = self.error_detail
            rt = dict(self.runtime_state or {})
            prompt = rt.get("prompt") or {}
            prompt_det = prompt.get("error_detail") if isinstance(prompt, dict) else None
            active_det = prompt_det or det
            err_str = (active_det or {}).get("raw_error") or self.error_text or (prompt.get("detail") if isinstance(prompt, dict) else "") or ""
            code = (active_det or {}).get("code", "")
            stage_map = {
                "ERR-NET-101": "net_offline",
                "ERR-VPN-102": "vpn_blocked",
                "ERR-LUYA-103": "luya_login",
                "ERR-LUYA-104": "luya_download",
                "ERR-WPS-201": "wps_open",
                "ERR-WPS-202": "wps_paste",
                "ERR-WPS-204": "wps_capture",
                "ERR-DT-301": "dingtalk_login",
                "ERR-DT-303": "dingtalk_search",
                "ERR-DT-304": "dingtalk_paste",
                "ERR-DT-305": "dingtalk_paste",
            }
            stage = stage_map.get(code, "dingtalk_paste")
            waiting = bool(rt.get("waiting_recovery"))
            paused = bool(rt.get("paused"))
            self.ai_busy = True
        try:
            if not hasattr(self.app, "ai_auto_debug_and_heal"):
                return {"ok": False, "reply": "当前版本不支持 AI 自动看图调试"}
            res = self.app.ai_auto_debug_and_heal(cfg_snap, stage=stage, exc=RuntimeError(err_str), force=True)
            acts = res.get("actions") or []
            resumed = False
            resume_msg = ""
            if res.get("can_auto_fix") and (waiting or paused):
                act_id = "continue"
                if "retry_paste" in acts or "enable_silent_uia" in acts or "click_send_now" in acts:
                    act_id = "retry_paste"
                elif "reopen_search" in acts or "wps_escape_retry" in acts:
                    act_id = "retry"
                resumed, resume_msg = self.resume_current_task(act_id)
            reply_lines = []
            if res.get("observation"):
                reply_lines.append(f"【现场画面诊断】{res['observation']}")
            if acts:
                reply_lines.append(f"【自动调试动作】{' -> '.join(acts)}")
            if res.get("summary"):
                reply_lines.append(f"【方案说明】{res['summary']}")
            if resumed:
                reply_lines.append(f"【续跑状态】已自动恢复执行（{resume_msg}）")
            elif not res.get("can_auto_fix") and res.get("observation"):
                reply_lines.append("【提示】该场景需您按卡片提示手动确认后再继续。")
            wrapped = {
                "ok": True,
                "mode": "auto_heal",
                "code": code,
                "time": time.strftime("%H:%M:%S"),
                "source": res.get("source", "local_heuristic"),
                "model": "",
                "can_auto_fix": bool(res.get("can_auto_fix")),
                "actions": acts,
                "resumed": resumed,
                "reply": "\n".join(reply_lines) or "已完成现场检查。",
            }
            with self._lock:
                self.ai_result = wrapped
            return wrapped
        finally:
            with self._lock:
                self.ai_busy = False

    def apply_ai_tuning(self, patch=None, resume_now=True):
        cfg_snap = self.get_config()
        with self._lock:
            det = self.error_detail
            rt = dict(self.runtime_state or {})
            prompt = rt.get("prompt") or {}
            prompt_det = prompt.get("error_detail") if isinstance(prompt, dict) else None
            active_det = prompt_det or det
            ai_res = dict(self.ai_result or {})
            waiting = bool(rt.get("waiting_recovery"))
            paused = bool(rt.get("paused"))
        use_patch = dict(patch) if isinstance(patch, dict) else {}
        if not use_patch:
            rec_obj = ai_res.get("recommended_tuning") if isinstance(ai_res.get("recommended_tuning"), dict) else {}
            if isinstance(rec_obj.get("patch"), dict) and rec_obj.get("patch"):
                use_patch = dict(rec_obj["patch"])
            elif hasattr(self.app, "recommend_pipeline_tuning_for_error"):
                calc = self.app.recommend_pipeline_tuning_for_error(cfg_snap, active_det)
                use_patch = dict(calc.get("patch") or {})
        if not use_patch:
            return {"ok": False, "message": "当前已是最稳妥的执行顺序与等待配置，无需额外调整"}
        if hasattr(self.app, "apply_recommended_tuning"):
            new_cfg = self.app.apply_recommended_tuning(cfg_snap, use_patch)
        else:
            new_cfg = cfg_snap
        saved = self.save_config(new_cfg)
        self._on_log("🔧 已按 AI 建议自动调整内部模块执行顺序与等待参数")
        resumed = False
        resume_msg = ""
        if resume_now and (waiting or paused):
            code = str((active_det or {}).get("code") or "")
            act_id = "retry_paste" if code == "ERR-DT-304" else ("retry" if code in ("ERR-DT-303", "ERR-WPS-203", "ERR-WPS-204") else "continue")
            resumed, resume_msg = self.resume_current_task(act_id)
        return {
            "ok": True,
            "config": saved,
            "applied": use_patch,
            "resumed": resumed,
            "message": f"已自调模块顺序并立即重试（{resume_msg}）" if resumed else "已自调内部模块顺序与等待秒数，下次运行立即生效",
        }

    def _append_chat_notice(self, text, source="task_done", shot_info="远程指令回执"):
        txt = str(text or "").strip()
        if not txt:
            return
        with self._lock:
            self.ai_chat_history.append({
                "role": "model",
                "text": txt,
                "time": time.strftime("%H:%M:%S"),
                "model": "",
                "source": source,
                "shot_info": shot_info,
                "via": "pc",
            })
            if len(self.ai_chat_history) > 25:
                self.ai_chat_history = self.ai_chat_history[-25:]
            self._chat_seq += 1

    def verify_mobile_member_login(self, nickname):
        q = str(nickname or "").strip()
        if not q:
            return {"ok": False, "message": "请输入您在电脑上设置的名字"}
        if self.hub is not None:
            prof = self.hub.get_profile()
            my_nick = str(prof.get("nickname") or "").strip()
            hit = self.hub.find_member_by_nickname(q) if hasattr(self.hub, "find_member_by_nickname") else None
            if hit is not None:
                return {"ok": True, "profile": hit}
            if not my_nick or my_nick.startswith("未命名"):
                return {
                    "ok": False,
                    "message": "电脑端尚未起名！请先在电脑右上角【设置昵称】设置一个唯一名字，再用相同名字登录手机端。",
                }
            return {
                "ok": False,
                "message": f"没有找到名字「{q}」，请核对是否与电脑上设置的名字完全一致。",
            }
        return {"ok": True, "profile": {"nickname": q, "display_name": q, "avatar": ""}}

    def get_mobile_bridge_info(self):
        if self.mobile_bridge is None:
            return {"ok": False, "message": "手机网页服务未加载"}
        return self.mobile_bridge.get_full_info()

    def control_mobile_tunnel(self, body):
        if self.mobile_bridge is None:
            return {"ok": False, "message": "手机网页服务未加载"}
        b = body if isinstance(body, dict) else {}
        pin_res = None
        if "pin" in b:
            pin_res = self.mobile_bridge.set_pin(b.get("pin") or "")
        if b.get("action") == "pin_only":
            info = self.mobile_bridge.get_full_info()
            if isinstance(pin_res, dict):
                info.update({k: v for k, v in pin_res.items() if k in ("ok", "message", "remote_password")})
            return info
        if b.get("enable"):
            res = self.mobile_bridge.start_tunnel()
            info = self.mobile_bridge.get_full_info()
            info.update({k: v for k, v in res.items() if k in ("ok", "message", "kind")})
            return info
        res = self.mobile_bridge.stop_tunnel()
        info = self.mobile_bridge.get_full_info()
        info.update({k: v for k, v in res.items() if k in ("ok", "message")})
        return info

    def execute_ai_on_demand_task(self, cmd_res, speaker_name=""):
        if not isinstance(cmd_res, dict) or not cmd_res.get("handled"):
            return {"ok": False, "message": "无效指令"}
        if self.hub is not None:
            perms = self.hub.get_my_permissions()
            if not perms.get("can_run", True):
                return {"ok": False, "message": "当前团队设置暂未开放本机启动运行权限"}
        with self._lock:
            if self.job_running or self.capture_running:
                return {"ok": False, "message": "电脑当前正在运行上一轮任务，请稍候完成后再试"}
            if not self._task_lock.acquire(blocking=False):
                return {"ok": False, "message": "电脑当前正在执行任务，请稍候"}

        action = cmd_res.get("action") or "capture_send"
        run_cfg = cmd_res.get("run_cfg") or self.get_config()
        targets = list(cmd_res.get("targets") or [])
        date_lbl = (cmd_res.get("date_spec") or {}).get("label") or "当前日期"

        if self.hub is not None:
            lres = self.hub.acquire_run_lock(mode_label="AI即时指令", step_text=f"准备执行并发送到 {'、'.join(targets) or '钉钉'}…")
            if not lres.get("ok"):
                try:
                    self._task_lock.release()
                except Exception:
                    pass
                holder = lres.get("locked_by") or {}
                return {"ok": False, "message": f"「{holder.get('display_name') or '同事'}」正在使用表格，为防混乱已为您暂缓"}

        def _done_cb(ok_flag, err_tip=""):
            tgt_txt = "、".join(targets) if targets else "指定会话"
            if ok_flag:
                self._append_chat_notice(
                    f"✅ 您的即时指令已完成！已按【{date_lbl}】处理好内容并发送到钉钉「{tgt_txt}」。",
                    source="task_done",
                    shot_info=f"已完成 -> {tgt_txt}",
                )
            else:
                self._append_chat_notice(
                    f"⚠️ 您的即时指令（目标：{tgt_txt}）未能全部完成：{err_tip or '遇到执行中断，请查看运行日志'}",
                    source="task_done",
                    shot_info="指令未完成",
                )

        if action == "text_send":
            rt_ai = self._build_runtime_ai_cfg(run_cfg)
            sum_res = self.app.summarize_report_with_ai(
                rt_ai,
                style_id="brief",
                user_prompt=f"请帮我提取与{date_lbl}最相关的关键表格与团队/个人数据简报，准备直接作为钉钉文字发送",
            )
            msg_txt = str((sum_res or {}).get("reply") or "").strip() or f"{date_lbl} 数据已为您整理完毕。"
            with self._lock:
                self._run_epoch += 1
                ep = self._run_epoch
                self.capture_running = True
                self._holds_team_lock = True
                self._run_started_at = time.time()
                self._run_mode = "text_send"
                self._run_trigger_type = "manual"
                self.status_level = "running"
                self.progress = 0.35
                self.step_text = f"正在将整理结果发给钉钉 {'、'.join(targets)}…"
            threading.Thread(
                target=self._text_send_worker,
                args=(ep, run_cfg, targets, msg_txt, _done_cb),
                daemon=True,
            ).start()
            return {"ok": True}

        with self._lock:
            self._run_epoch += 1
            ep = self._run_epoch
            self._holds_team_lock = True
            self._run_started_at = time.time()
            self._run_trigger_type = "manual"
            self.error_text = ""
            self.error_detail = None
            self.ai_result = None
            self.status_level = "running"
            self.progress = 0.04
            if action == "run_full":
                self.job_running = True
                self._run_mode = "full"
                self.step_text = f"AI 正在启动全流程跑表（目标：{'、'.join(targets)}）…"
                worker_fn = self._run_once_worker
                worker_args = ("即时", True, run_cfg, None, None, ep, _done_cb)
            else:
                self.capture_running = True
                self._run_mode = "capture_only"
                self.step_text = f"AI 正在按{date_lbl}截图发钉钉（{'、'.join(targets)}）…"
                worker_fn = self._capture_only_worker
                worker_args = (ep, run_cfg, _done_cb)
        threading.Thread(target=worker_fn, args=worker_args, daemon=True).start()
        return {"ok": True}

    def summarize_ai(self, style_id="brief", prompt="", action="send", source_device="pc", speaker_name=""):
        if action == "clear":
            with self._lock:
                self.ai_chat_history = []
                self._chat_seq += 1
            return {"ok": True, "cleared": True, "history": [], "chat_seq": self._chat_seq}

        if self.hub is not None:
            perms = self.hub.get_my_permissions()
            if not perms.get("can_use_ai", True):
                return {"ok": False, "reply": "当前智能分析已采用团队统一设置，如需开启请点击右上角【反馈】提交说明。"}

        cfg_snap = self._build_runtime_ai_cfg()
        ai_cfg = cfg_snap.get("ai_assistant") if isinstance(cfg_snap.get("ai_assistant"), dict) else {}
        focus_team = str(ai_cfg.get("focus_team") or "").strip()
        if hasattr(self.app, "build_ai_summary_styles"):
            styles = self.app.build_ai_summary_styles(focus_team)
        else:
            styles = getattr(self.app, "AI_SUMMARY_STYLES", {})
        sid = str(style_id or "brief").strip().lower()
        clean_prompt = str(prompt or "").strip()
        display_user = clean_prompt or (styles.get(sid, {}).get("name") and f"请按【{styles[sid]['name']}】帮我总结当前数据") or "总结当前数据"

        eff_speaker = str(speaker_name or "").strip()
        roster = []
        if self.hub is not None:
            try:
                if not eff_speaker:
                    eff_speaker = str((self.hub.get_profile() or {}).get("nickname") or "").strip()
                if hasattr(self.hub, "get_member_roster_names"):
                    roster = self.hub.get_member_roster_names()
            except Exception:
                pass
        if source_device == "mobile" and clean_prompt:
            self._on_log(f"手机网页端「{eff_speaker or '远程用户'}」对话指令：{display_user}")

        with self._lock:
            hist_snap = list(self.ai_chat_history[-10:])

        on_demand_res = (
            self.app.try_handle_ai_on_demand_task(
                clean_prompt,
                cfg_snap,
                speaker_name=eff_speaker,
                extra_roster=roster,
            )
            if (clean_prompt and hasattr(self.app, "try_handle_ai_on_demand_task"))
            else {"handled": False}
        )
        if on_demand_res.get("handled"):
            exec_r = self.execute_ai_on_demand_task(on_demand_res, speaker_name=eff_speaker)
            if exec_r.get("ok"):
                res = {
                    "ok": True,
                    "source": "on_demand_task",
                    "model": "",
                    "shot_info": f"已派发任务 · {'、'.join(on_demand_res.get('targets') or [])}",
                    "reply": on_demand_res["reply"],
                }
            else:
                res = {
                    "ok": True,
                    "source": "on_demand_blocked",
                    "model": "",
                    "shot_info": "任务暂缓",
                    "reply": f"我已理解您想做的事，但暂时未能启动：{exec_r.get('message', '当前有任务运行中')}",
                }
        else:
            cmd_res = (
                self.app.try_handle_ai_config_command(clean_prompt, cfg_snap)
                if (clean_prompt and hasattr(self.app, "try_handle_ai_config_command"))
                else {"handled": False}
            )
            if cmd_res.get("handled"):
                save_out = self.save_config_checked(cmd_res["new_cfg"])
                if save_out.get("ok"):
                    res = {
                        "ok": True,
                        "source": "config_setter",
                        "model": "",
                        "shot_info": "已修改配置 · 请核对下方确认",
                        "reply": cmd_res["reply"],
                        "updated_config": save_out["config"],
                        "updated_timetable": save_out["timetable"],
                        "ai_meta": save_out.get("ai_meta"),
                    }
                else:
                    res = {
                        "ok": True,
                        "source": "guardrail",
                        "model": "",
                        "shot_info": "团队标准设置保护",
                        "reply": f"⚠️ {save_out.get('message', '当前该项采用团队标准设置，暂无需手动调整。')}\n\n如确需调整，可点击右上角【反馈】说明您的排期需求。",
                    }
            else:
                res = dict(self.app.summarize_report_with_ai(
                    cfg_snap,
                    style_id=sid,
                    user_prompt=clean_prompt,
                    chat_history=hist_snap,
                ))
                res["model"] = ""
        reply_txt = str(res.get("reply") or "").strip()
        if self.hub is not None and reply_txt:
            try:
                self.hub.record_member_ai_chat("user", display_user, {"style_id": sid, "via": source_device})
                self.hub.record_member_ai_chat("model", reply_txt, {"style_id": sid, "source": res.get("source", "")})
            except Exception:
                pass
        if reply_txt:
            with self._lock:
                self.ai_chat_history.append({
                    "role": "user",
                    "text": display_user,
                    "time": time.strftime("%H:%M:%S"),
                    "via": source_device or "pc",
                    "sender": eff_speaker,
                })
                self.ai_chat_history.append({
                    "role": "model",
                    "text": reply_txt,
                    "time": time.strftime("%H:%M:%S"),
                    "model": "",
                    "source": res.get("source", ""),
                    "shot_info": res.get("shot_info", ""),
                    "via": "pc",
                })
                if len(self.ai_chat_history) > 25:
                    self.ai_chat_history = self.ai_chat_history[-25:]
                self._chat_seq += 1
                res["history"] = list(self.ai_chat_history)
                res["chat_seq"] = self._chat_seq
        return res

    def get_config(self):
        with self._lock:
            return copy.deepcopy(self.cfg)

    def get_timetable(self):
        with self._lock:
            return self.app.build_daily_timetable(self.cfg)

    def get_data_brief(self, force=False):
        now = time.monotonic()
        with self._lock:
            ts0, cached = self._brief_cache
            if not force and cached and (now - ts0) < 5.0:
                return cached
        brief = {}
        try:
            if hasattr(self.app, "get_dashboard_data_brief"):
                brief = self.app.get_dashboard_data_brief()
        except Exception:
            brief = {}
        with self._lock:
            self._brief_cache = (now, brief)
        return brief

    def get_ai_meta(self):
        cfg_snap = self.get_config()
        ai_cfg = cfg_snap.get("ai_assistant") if isinstance(cfg_snap.get("ai_assistant"), dict) else {}
        focus_team = str(ai_cfg.get("focus_team") or "").strip()
        ctx = self.app.collect_report_context_for_ai(include_prev_shot=False, encode_images=False) if hasattr(self.app, "collect_report_context_for_ai") else {}
        if hasattr(self.app, "build_ai_summary_styles"):
            styles = self.app.build_ai_summary_styles(focus_team)
        else:
            styles = getattr(self.app, "AI_SUMMARY_STYLES", {})
        candidates = self.app.get_candidate_teams(cfg_snap) if hasattr(self.app, "get_candidate_teams") else []
        brief = self.get_data_brief(force=True)
        with self._lock:
            hist = list(self.ai_chat_history)
        return {
            "styles": [{"id": k, "name": v.get("name", k), "desc": v.get("desc", "")} for k, v in styles.items()],
            "focus_team": focus_team,
            "candidate_teams": candidates,
            "latest_shot_label": ctx.get("latest_shot_label", ""),
            "has_data": bool(ctx.get("has_data") or brief.get("has_archive")),
            "data_brief": brief,
            "history": hist,
        }

    def _apply_global_logic_to_cfg(self, cfg_dict):
        out = copy.deepcopy(cfg_dict) if isinstance(cfg_dict, dict) else {}
        if self.hub is None:
            return out
        gl = self.hub.get_effective_global_overrides()
        if isinstance(gl.get("enforce_paste_delay_sec"), int):
            out["dt_paste_delay_sec"] = gl["enforce_paste_delay_sec"]
        if gl.get("enforce_compose_order"):
            out.setdefault("pipeline_tuning", {})["compose_step_order"] = gl["enforce_compose_order"]
        if gl.get("enforce_silent_order"):
            out.setdefault("pipeline_tuning", {})["silent_step_order"] = gl["enforce_silent_order"]
        if isinstance(gl.get("enforce_minimize_after_send"), bool):
            out["dt_minimize_after_send"] = gl["enforce_minimize_after_send"]
        return out

    def save_config(self, new_cfg):
        with self._lock:
            merged_in = self._merge_server_ai_secrets(new_cfg, self.cfg)
            normed = self.app.normalize_config(merged_in)
            self.app.save_config(normed)
            self.cfg = self.app.load_config()
            saved = copy.deepcopy(self.cfg)
        self._sync_snapshot_safe(cfg=saved)
        return saved

    def save_config_checked(self, new_cfg):
        with self._lock:
            old_norm = self.app.normalize_config(self.cfg)
            merged_in = self._merge_server_ai_secrets(new_cfg, old_norm)
            new_norm = self.app.normalize_config(merged_in)
        if self.hub is not None and team_hub is not None:
            perms = self.hub.get_my_permissions()
            allowed, denied, safe_cfg = team_hub.check_config_edit_permission(old_norm, new_norm, perms)
            if not allowed:
                saved = self.save_config(safe_cfg)
                return {
                    "ok": False,
                    "denied": denied,
                    "message": f"当前该项采用团队标准设置，暂无需手动调整：{'、'.join(denied)}",
                    "config": self._sanitize_config_for_client(saved),
                    "timetable": self.get_timetable(),
                    "ai_meta": self.get_ai_meta(),
                }
        saved = self.save_config(new_norm)
        return {
            "ok": True,
            "message": "设置已保存",
            "config": self._sanitize_config_for_client(saved),
            "timetable": self.get_timetable(),
            "ai_meta": self.get_ai_meta(),
        }

    def reset_config(self):
        with self._lock:
            default = self.app.normalize_config({})
            self.app.save_config(default)
            self.cfg = self.app.load_config()
            saved = copy.deepcopy(self.cfg)
        self._sync_snapshot_safe(cfg=saved)
        return saved

    def clear_logs(self):
        with self._lock:
            self.logs.clear()

    def _sync_snapshot_safe(self, cfg=None):
        if self.hub is None:
            return
        try:
            with self._lock:
                c = self._sanitize_config_for_client(cfg if isinstance(cfg, dict) else self.cfg)
                lvl = self.status_level
                st_txt = self.step_text
                l_run = self.last_run_str
                n_run = self.next_run_str
                rec_logs = [f"[{it['time']}] {it['text']}" for it in self.logs[-25:]]
                ed = dict(self.error_detail) if isinstance(self.error_detail, dict) else None
            self.hub.sync_member_snapshot(
                cfg=c,
                status_level=lvl,
                step_text=st_txt,
                last_run_str=l_run,
                next_run_str=n_run,
                recent_logs=rec_logs,
                error_detail=ed,
            )
        except Exception:
            pass

    def _hub_background_loop(self):
        time.sleep(1.2)
        self._sync_snapshot_safe()
        tick = 0
        while True:
            try:
                tick += 1
                if self.hub is not None:
                    with self._lock:
                        busy = bool(self.job_running or self.capture_running)
                        holds = bool(self._holds_team_lock)
                        st_txt = self.step_text
                    if (busy or holds) and tick % 2 == 0:
                        self.hub.heartbeat_run_lock(st_txt)
                    if tick % 3 == 0:
                        self._sync_snapshot_safe()
                    if not busy and not self._queue_auto_starting:
                        qt = self.hub.poll_queue_turn()
                        if qt.get("in_queue") and qt.get("my_turn"):
                            self._queue_auto_starting = True
                            try:
                                rtype = qt.get("run_type") or "full"
                                self.hub.leave_run_queue()
                                self._on_log("排队已轮到您，正在自动接棒开始执行…")
                                if rtype == "capture":
                                    self.start_capture_only()
                                else:
                                    self.start_run_now()
                            finally:
                                self._queue_auto_starting = False
            except Exception:
                pass
            time.sleep(2.5)

    def get_profile_payload(self):
        if self.hub is None or team_hub is None:
            return {"ok": False, "profile": {}, "presets": [], "sync_settings": {}}
        return {
            "ok": True,
            "profile": self.hub.get_profile(),
            "presets": team_hub.PRESET_AVATARS,
            "sync_settings": self.hub.get_sync_settings(),
        }

    def update_profile_payload(self, body):
        if self.hub is None:
            return {"ok": False, "message": "协同模块未加载"}
        b = body if isinstance(body, dict) else {}
        res = self.hub.update_profile(
            nickname=b.get("nickname"),
            avatar=b.get("avatar"),
            skip_name=bool(b.get("skip_name")),
        )
        if isinstance(res, dict) and not res.get("ok", True):
            return {
                "ok": False,
                "duplicate": bool(res.get("duplicate")),
                "profile": res.get("profile") or self.hub.get_profile(),
                "message": res.get("message") or "这个名字已经被其他同事占用啦，请换一个",
            }
        prof = (res.get("profile") if isinstance(res, dict) else None) or res
        self._refresh_operator_name()
        self._sync_snapshot_safe()
        return {
            "ok": True,
            "profile": prof,
            "message": (res.get("message") if isinstance(res, dict) else None) or f"已保存您的显示名字：{prof.get('display_name', '')}",
        }

    def get_random_nickname_payload(self, exclude=""):
        if self.hub is not None and hasattr(self.hub, "pick_unique_random_nickname"):
            return {"ok": True, "nickname": self.hub.pick_unique_random_nickname(exclude=exclude)}
        if team_hub is None:
            return {"ok": True, "nickname": "数据小助手"}
        return {"ok": True, "nickname": team_hub.pick_random_nickname(exclude=exclude)}

    def update_sync_config_payload(self, body):
        if self.hub is None:
            return {"ok": False, "message": "协同模块未加载"}
        return self.hub.admin_update_sync_settings(body if isinstance(body, dict) else {})

    def handle_queue_action(self, body):
        if self.hub is None:
            return {"ok": False, "message": "协同模块未加载"}
        b = body if isinstance(body, dict) else {}
        act = str(b.get("action") or "join")
        if act == "leave":
            st = self.hub.leave_run_queue()
            return {"ok": True, "message": "已取消排队等待", **st}
        rtype = str(b.get("run_type") or "full")
        st = self.hub.join_run_queue(run_type=rtype)
        pos = st.get("position", 1)
        return {"ok": True, "message": f"已加入自动排队（当前第 {pos} 位），前人完成后会自动开跑", **st}

    def submit_feedback_payload(self, body):
        if self.hub is None:
            return {"ok": False, "message": "协同模块未加载"}
        b = body if isinstance(body, dict) else {}
        content = str(b.get("content") or "").strip()
        if not content:
            return {"ok": False, "message": "请先填写具体反馈内容或遇到的问题"}
        category = str(b.get("category") or "问题建议").strip()
        with self._lock:
            ed = dict(self.error_detail) if isinstance(self.error_detail, dict) else None
            cfg_snap = copy.deepcopy(self.cfg)
        return self.hub.submit_feedback(category=category, content=content, cfg=cfg_snap, error_detail=ed)

    def admin_login(self, body):
        if self.hub is None:
            return {"ok": False, "message": "协同模块未加载"}
        b = body if isinstance(body, dict) else {}
        return self.hub.verify_admin(b.get("passcode", ""), b.get("password", ""))

    def admin_is_authed(self, tok):
        if self.hub is None:
            return False
        return self.hub.is_admin_token_valid(tok)

    def admin_dashboard_payload(self, start_time="", end_time="", user_id=""):
        if self.hub is None:
            return {"ok": False, "message": "协同模块未加载"}
        return self.hub.get_admin_dashboard(start_time=start_time, end_time=end_time, user_id=user_id)

    def admin_unlock_payload(self, body):
        if self.hub is None:
            return {"ok": False, "message": "协同模块未加载"}
        b = body if isinstance(body, dict) else {}
        return self.hub.force_unlock_all(clear_queue=bool(b.get("clear_queue")))

    def admin_member_payload(self, body):
        if self.hub is None:
            return {"ok": False, "message": "协同模块未加载"}
        b = body if isinstance(body, dict) else {}
        res = self.hub.admin_update_member(
            target_uid=b.get("user_id", ""),
            remark=b.get("remark"),
            permissions=b.get("permissions"),
            new_config=b.get("config"),
            ai_api_override=b.get("ai_api_override"),
        )
        # 若编辑对象恰好就是本机当前账号且下发了新配置，立即应用到本地
        prof = self.hub.get_profile()
        if res.get("ok") and b.get("user_id") == prof.get("user_id") and isinstance(b.get("config"), dict):
            self.save_config(b["config"])
        return res

    def admin_test_ai_payload(self, body):
        b = body if isinstance(body, dict) else {}
        ai_patch = b.get("ai_api") if isinstance(b.get("ai_api"), dict) else {}
        if not ai_patch and self.hub is not None:
            st = self.hub._state if hasattr(self.hub, "_state") else {}
            ai_patch = (st.get("global_logic") or {}).get("global_ai_api") or {}
        prov = str(ai_patch.get("provider") or "gemini").strip().lower()
        if prov == "local_only":
            return {"ok": True, "source": "local_only", "reply": "当前模式已设为【内置基础模式】，无需外部接口即可稳定提供问答、排障与变化分析。"}
        test_cfg = self.get_config()
        ai_d = dict(test_cfg.get("ai_assistant") or {})
        ai_d["provider"] = prov
        ai_d["api_base"] = str(ai_patch.get("api_base") or "").strip()
        if str(ai_patch.get("api_key") or "").strip():
            ai_d["api_key"] = str(ai_patch["api_key"]).strip()
        ai_d["model"] = str(ai_patch.get("model") or "").strip()
        test_cfg["ai_assistant"] = ai_d
        if hasattr(self.app, "_call_gemini_generate"):
            norm = self.app.normalize_config(test_cfg)
            res = self.app._call_gemini_generate(
                norm,
                "你是智能接口连通性检测员，请用简短两句亲切中文回复并确认云端服务连通正常。",
                [{"role": "user", "parts": [{"text": "请回复一句简短问候，确认智能接口已连通可用。"}]}],
                temperature=0.3,
                max_tokens=120,
                timeout_sec=18,
            )
            if res.get("ok"):
                return {"ok": True, "source": res.get("source", "ai_cloud"), "reply": res.get("reply", "接口连通成功！")}
            return {"ok": False, "source": "error", "reply": f"连通测试失败：{res.get('error', '接口未返回有效响应')}"}
        return {"ok": False, "reply": "未找到接口测试函数"}

    def admin_global_logic_payload(self, body):
        if self.hub is None:
            return {"ok": False, "message": "协同模块未加载"}
        return self.hub.admin_update_global_logic(body if isinstance(body, dict) else {})

    def admin_server_toggle_payload(self, body):
        if self.hub is None:
            return {"ok": False, "message": "协同模块未加载"}
        b = body if isinstance(body, dict) else {}
        if b.get("enable", True):
            port = max(1024, min(65535, _safe_int(b.get("port"), 18790)))
            res = self.hub.start_center_server(port=port)
            self.hub.admin_update_sync_settings({"server_port": port, "server_autostart": True})
            return res
        self.hub.admin_update_sync_settings({"server_autostart": False})
        self.hub.stop_center_server()
        return {"ok": True, "server_running": False, "message": "已关闭联机服务端"}

    def admin_feedback_resolve_payload(self, body):
        if self.hub is None:
            return {"ok": False, "message": "协同模块未加载"}
        b = body if isinstance(body, dict) else {}
        return self.hub.admin_resolve_feedback(b.get("id", ""), status=b.get("status", "resolved"), reply=b.get("reply", ""))

    def admin_credentials_payload(self, body):
        if self.hub is None:
            return {"ok": False, "message": "协同模块未加载"}
        b = body if isinstance(body, dict) else {}
        return self.hub.update_admin_credentials(b.get("passcode", ""), b.get("password", ""))

    def get_state(self, since=0):
        remote_pulled = False
        if self.hub is not None:
            try:
                pending_cfg = self.hub.pull_remote_config_if_updated(self.cfg)
                if isinstance(pending_cfg, dict):
                    self.save_config(pending_cfg)
                    remote_pulled = True
                    self._on_log("已接收并自动应用最新团队标准配置")
            except Exception:
                pass
        with self._lock:
            self.last_heartbeat = time.time()
            delta = [item for item in self.logs if item["id"] > since]
            rt = self.runtime_state or {}
            prompt = rt.get("prompt") or {}
            hk_mgr = getattr(self.app, "_HOTKEY_MGR", None)
            native_hk = []
            if hk_mgr is not None and getattr(hk_mgr, "_thread", None) is not None and hk_mgr._thread.is_alive():
                try:
                    f8_lbl, f9_lbl = hk_mgr.active_labels()
                    if f8_lbl == "F8":
                        native_hk.append("F8")
                    if f9_lbl == "F9":
                        native_hk.append("F9")
                except Exception:
                    pass
            state_out = {
                "version": self.app.APP_VERSION,
                "job_running": self.job_running,
                "capture_running": self.capture_running,
                "busy": self.job_running or self.capture_running,
                "paused": bool(rt.get("paused")),
                "waiting_recovery": bool(rt.get("waiting_recovery")),
                "hands_off": bool(rt.get("hands_off")),
                "hands_off_text": str(rt.get("hands_off_text") or ""),
                "recovery_prompt": prompt,
                "native_hotkeys": native_hk,
                "schedule_running": self.schedule_running,
                "need_login_prompt": self.need_login_prompt,
                "status_level": self.status_level,
                "step_text": self.step_text,
                "progress": self.progress,
                "error_text": self.error_text,
                "error_detail": self.error_detail,
                "ai_busy": self.ai_busy,
                "ai_result": self.ai_result,
                "last_run_str": self.last_run_str,
                "next_run_str": self.next_run_str,
                "next_run_ts": self.next_run_ts,
                "server_now": time.time(),
                "log_seq": self._log_seq,
                "logs": delta,
                "update_info": self.update_info,
                "data_brief": self.get_data_brief(),
                "remote_config_pulled": remote_pulled,
                "spotlight_seq": self._spotlight_seq,
                "latest_spotlight": self.latest_spotlight,
                "chat_seq": self._chat_seq,
                "ai_history": list(self.ai_chat_history),
                "mobile_info": self.mobile_bridge.get_brief_info() if self.mobile_bridge is not None else {"running": False},
            }
        if self.hub is not None:
            try:
                state_out["member_profile"] = self.hub.get_profile()
                state_out["permissions"] = self.hub.get_my_permissions()
                state_out["global_logic"] = self.hub.get_effective_global_overrides()
                state_out["lock_status"] = self.hub.get_lock_status()
                state_out["queue_status"] = self.hub.poll_queue_turn()
                state_out["sync_settings"] = self.hub.get_sync_settings()
            except Exception:
                pass
        return state_out

    def start_run_now(self):
        self._refresh_operator_name()
        if self.hub is not None:
            perms = self.hub.get_my_permissions()
            if not perms.get("can_run", True):
                return {"ok": False, "message": "当前团队设置暂未开放本机启动运行，如有需要请点右上角【反馈】提交说明"}
        with self._lock:
            if self.job_running or self.capture_running:
                return {"ok": False, "message": "当前已有任务正在运行，请稍候"}
            if not self._task_lock.acquire(blocking=False):
                return {"ok": False, "message": "当前已有任务正在运行，请稍候"}
        if self.hub is not None:
            lres = self.hub.acquire_run_lock(mode_label="全流程", step_text="准备开始手动任务…")
            if not lres.get("ok"):
                try:
                    self._task_lock.release()
                except Exception:
                    pass
                holder = lres.get("locked_by") or {}
                hname = holder.get("display_name") or "其他成员"
                return {
                    "ok": False,
                    "locked": True,
                    "locked_by": holder,
                    "queue": lres.get("queue", []),
                    "message": f"「{hname}」正在运行表格，为防撞车混乱已为您暂缓",
                }
        with self._lock:
            self._run_epoch += 1
            ep = self._run_epoch
            self.job_running = True
            self._holds_team_lock = True
            self._run_started_at = time.time()
            self._run_mode = "full"
            self._run_trigger_type = "manual"
            self.error_text = ""
            self.error_detail = None
            self.ai_result = None
            self.status_level = "running"
            self.progress = 0.02
            self.step_text = "准备开始手动任务…"
        threading.Thread(target=self._run_once_worker, args=("手动", True, None, None, None, ep), daemon=True).start()
        return {"ok": True, "message": "已开始全流程任务"}

    def start_capture_only(self):
        self._refresh_operator_name()
        if self.hub is not None:
            perms = self.hub.get_my_permissions()
            if not perms.get("can_run", True):
                return {"ok": False, "message": "当前团队设置暂未开放本机启动运行，如有需要请点右上角【反馈】提交说明"}
        with self._lock:
            if self.job_running or self.capture_running:
                return {"ok": False, "message": "当前已有任务正在运行，请稍候"}
            if not self._task_lock.acquire(blocking=False):
                return {"ok": False, "message": "当前已有任务正在运行，请稍候"}
        if self.hub is not None:
            lres = self.hub.acquire_run_lock(mode_label="仅截图发送", step_text="正在打开WPS汇总表…")
            if not lres.get("ok"):
                try:
                    self._task_lock.release()
                except Exception:
                    pass
                holder = lres.get("locked_by") or {}
                hname = holder.get("display_name") or "其他成员"
                return {
                    "ok": False,
                    "locked": True,
                    "locked_by": holder,
                    "queue": lres.get("queue", []),
                    "message": f"「{hname}」正在操作表格，为防混乱已自动避让",
                }
        with self._lock:
            self._run_epoch += 1
            ep = self._run_epoch
            self.capture_running = True
            self._holds_team_lock = True
            self._run_started_at = time.time()
            self._run_mode = "capture_only"
            self._run_trigger_type = "manual"
            self.error_text = ""
            self.error_detail = None
            self.ai_result = None
            self.status_level = "running"
            self.progress = 0.04
            self.step_text = "正在打开WPS汇总表…"
        threading.Thread(target=self._capture_only_worker, args=(ep,), daemon=True).start()
        return {"ok": True, "message": "已开始仅截图发送"}

    def _run_once_worker(self, round_name, owns_lock=True, run_cfg_override=None, trigger_lt=None, trigger_hm=None, run_epoch=None, on_done=None):
        with self._lock:
            my_epoch = self._run_epoch if run_epoch is None else run_epoch
            started_ts = self._run_started_at or time.time()
            trig_type = self._run_trigger_type or ("schedule" if isinstance(round_name, int) else "manual")
        if self.glass is not None:
            self.glass.show()
            self.glass.update_step(f"正在运行第 {round_name} 轮…", 0.02)
        run_cfg = None
        ok_cb = False
        err_cb = ""
        try:
            if run_cfg_override is not None:
                run_cfg = self._build_runtime_ai_cfg(self._apply_global_logic_to_cfg(run_cfg_override))
            else:
                run_cfg = self._build_runtime_ai_cfg(self._apply_global_logic_to_cfg(self.get_config()))
                if isinstance(round_name, int) and not self.app.capture_allowed_now(run_cfg):
                    run_cfg["enable_capture"] = False
                    run_cfg["enable_dingtalk"] = False
            if isinstance(round_name, int) and not run_cfg.get("enable_capture") and not run_cfg.get("enable_dingtalk"):
                self.app.log("  当前时间点按排期设置为仅更新表格（不截图发钉钉）")
                self._on_step("按排期仅更新表格")
            self.app.run_once(round_name, run_cfg, trigger_lt=trigger_lt, trigger_hm=trigger_hm)
            ok_cb = True
            done_text = "手动任务已完成" if not isinstance(round_name, int) else f"第 {round_name} 轮已完成"
            with self._lock:
                if self._run_epoch == my_epoch:
                    self.status_level = "ok"
                    self.step_text = done_text
                    self.progress = 1.0
                    self.error_text = ""
                    self.error_detail = None
            if self.hub is not None and self._run_epoch == my_epoch:
                try:
                    self.hub.record_run_event(mode="full", trigger_type=trig_type, status="ok", duration_sec=max(1, int(time.time() - started_ts)))
                except Exception:
                    pass
            if self.glass is not None and self._run_epoch == my_epoch:
                self.glass.finish("本轮已完成")
                self.glass.close(1100)
        except Exception as exc:
            err_cb = _strip_symbols(self.app.human_error(exc))
            if "手动停止" in str(exc):
                self.app.log("已手动停止")
                with self._lock:
                    if self._run_epoch == my_epoch:
                        self.status_level = "stopped"
                        self.step_text = "已手动停止"
                        self.error_text = ""
                        self.error_detail = None
                if self.hub is not None and self._run_epoch == my_epoch:
                    try:
                        self.hub.record_run_event(mode="full", trigger_type=trig_type, status="stopped", duration_sec=max(1, int(time.time() - started_ts)))
                    except Exception:
                        pass
                if self.glass is not None and self._run_epoch == my_epoch:
                    self.glass.fail("已手动停止")
                    self.glass.close(700)
            else:
                self.app.log_error(exc, f"执行失败-{round_name}")
                tip = _strip_symbols(self.app.human_error(exc))
                det = self.app.classify_error_detail(exc) if hasattr(self.app, "classify_error_detail") else None
                self.app.log(f"❌ 本轮失败：{tip}")
                with self._lock:
                    if self._run_epoch == my_epoch:
                        self.status_level = "err"
                        self.step_text = f"[{det['code']}] 运行失败" if det else "运行失败，请查看详情"
                        self.error_text = tip
                        self.error_detail = det
                if self.glass is not None and self._run_epoch == my_epoch:
                    self.glass.fail(tip)
                    self.glass.close(1800)
                if self.hub is not None and self._run_epoch == my_epoch:
                    try:
                        self.hub.record_run_event(
                            mode="full",
                            trigger_type=trig_type,
                            status="err",
                            err_code=(det or {}).get("code", "ERR-RUN-900"),
                            duration_sec=max(1, int(time.time() - started_ts)),
                        )
                    except Exception:
                        pass
                if det is not None and self._run_epoch == my_epoch:
                    try:
                        if run_cfg is not None:
                            self.app.record_run_journal(run_cfg, status="err", mode="full", round_label=f"第{round_name}轮", error_detail=det)
                    except Exception:
                        pass
                    self._maybe_auto_ai_diagnose(det)
        finally:
            rel_hub = False
            with self._lock:
                if self._run_epoch == my_epoch:
                    self.job_running = False
                    self.need_login_prompt = False
                    if hasattr(self.app, "get_runtime_state"):
                        self.runtime_state = self.app.get_runtime_state()
                    if self._holds_team_lock:
                        self._holds_team_lock = False
                        rel_hub = True
                    if owns_lock:
                        try:
                            self._task_lock.release()
                        except Exception:
                            pass
            if rel_hub and self.hub is not None:
                try:
                    self.hub.release_run_lock()
                except Exception:
                    pass
                self._sync_snapshot_safe()
            if on_done is not None:
                try:
                    on_done(ok_cb, err_cb)
                except Exception:
                    pass

    def _capture_only_worker(self, run_epoch=None, run_cfg_override=None, on_done=None):
        with self._lock:
            my_epoch = self._run_epoch if run_epoch is None else run_epoch
            started_ts = self._run_started_at or time.time()
        if self.glass is not None:
            self.glass.show()
            self.glass.update_step("正在打开WPS汇总表…", 0.05)
        run_cfg = None
        ok_cb = False
        err_cb = ""
        try:
            base_cfg = run_cfg_override if isinstance(run_cfg_override, dict) else self.get_config()
            run_cfg = self._build_runtime_ai_cfg(self._apply_global_logic_to_cfg(base_cfg))
            self.app.run_capture_only("截图", run_cfg)
            ok_cb = True
            with self._lock:
                if self._run_epoch == my_epoch:
                    self.status_level = "ok"
                    self.step_text = "截图发送已完成"
                    self.progress = 1.0
                    self.error_text = ""
                    self.error_detail = None
            if self.hub is not None and self._run_epoch == my_epoch:
                try:
                    self.hub.record_run_event(mode="capture_only", trigger_type="manual", status="ok", duration_sec=max(1, int(time.time() - started_ts)))
                except Exception:
                    pass
            if self.glass is not None and self._run_epoch == my_epoch:
                self.glass.finish("截图发送已完成")
                self.glass.close(1400)
        except Exception as exc:
            err_cb = _strip_symbols(self.app.human_error(exc))
            if "手动停止" in str(exc):
                self.app.log("已手动停止")
                with self._lock:
                    if self._run_epoch == my_epoch:
                        self.status_level = "stopped"
                        self.step_text = "已手动停止"
                        self.error_text = ""
                        self.error_detail = None
                if self.hub is not None and self._run_epoch == my_epoch:
                    try:
                        self.hub.record_run_event(mode="capture_only", trigger_type="manual", status="stopped", duration_sec=max(1, int(time.time() - started_ts)))
                    except Exception:
                        pass
                if self.glass is not None and self._run_epoch == my_epoch:
                    self.glass.fail("已手动停止")
                    self.glass.close(700)
            else:
                self.app.log_error(exc, "独立截图失败")
                tip = _strip_symbols(self.app.human_error(exc))
                det = self.app.classify_error_detail(exc) if hasattr(self.app, "classify_error_detail") else None
                self.app.log(f"❌ 独立截图失败：{tip}")
                with self._lock:
                    if self._run_epoch == my_epoch:
                        self.status_level = "err"
                        self.step_text = f"[{det['code']}] 截图失败" if det else "截图发送失败"
                        self.error_text = tip
                        self.error_detail = det
                if self.glass is not None and self._run_epoch == my_epoch:
                    self.glass.fail(tip)
                    self.glass.close(1800)
                if self.hub is not None and self._run_epoch == my_epoch:
                    try:
                        self.hub.record_run_event(
                            mode="capture_only",
                            trigger_type="manual",
                            status="err",
                            err_code=(det or {}).get("code", "ERR-RUN-900"),
                            duration_sec=max(1, int(time.time() - started_ts)),
                        )
                    except Exception:
                        pass
                if det is not None and self._run_epoch == my_epoch:
                    try:
                        if run_cfg is not None:
                            self.app.record_run_journal(run_cfg, status="err", mode="capture_only", round_label="独立截图", error_detail=det)
                    except Exception:
                        pass
                    self._maybe_auto_ai_diagnose(det)
        finally:
            rel_hub = False
            with self._lock:
                if self._run_epoch == my_epoch:
                    self.capture_running = False
                    self.need_login_prompt = False
                    if hasattr(self.app, "get_runtime_state"):
                        self.runtime_state = self.app.get_runtime_state()
                    if self._holds_team_lock:
                        self._holds_team_lock = False
                        rel_hub = True
                    try:
                        self._task_lock.release()
                    except Exception:
                        pass
            if rel_hub and self.hub is not None:
                try:
                    self.hub.release_run_lock()
                except Exception:
                    pass
                self._sync_snapshot_safe()
            if on_done is not None:
                try:
                    on_done(ok_cb, err_cb)
                except Exception:
                    pass

    def _text_send_worker(self, run_epoch, run_cfg, targets, message_text, on_done=None):
        ok_cb = False
        err_cb = ""
        try:
            self.app.send_dingtalk_text_message(
                run_cfg,
                targets,
                message_text,
                silent=bool((run_cfg or {}).get("dt_silent_send", True)),
                recover_interactive=False,
            )
            ok_cb = True
            with self._lock:
                if self._run_epoch == run_epoch:
                    self.status_level = "ok"
                    self.step_text = f"已发文字结果到 {'、'.join(targets)}"
                    self.progress = 1.0
        except Exception as exc:
            err_cb = _strip_symbols(self.app.human_error(exc))
            with self._lock:
                if self._run_epoch == run_epoch:
                    self.status_level = "err"
                    self.step_text = f"发送文字到钉钉失败：{err_cb}"
        finally:
            rel_hub = False
            with self._lock:
                if self._run_epoch == run_epoch:
                    self.capture_running = False
                    if self._holds_team_lock:
                        self._holds_team_lock = False
                        rel_hub = True
                    try:
                        self._task_lock.release()
                    except Exception:
                        pass
            if rel_hub and self.hub is not None:
                try:
                    self.hub.release_run_lock()
                except Exception:
                    pass
                self._sync_snapshot_safe()
            if on_done is not None:
                try:
                    on_done(ok_cb, err_cb)
                except Exception:
                    pass

    def pause_current_task(self):
        with self._lock:
            if not (self.job_running or self.capture_running):
                return False, "当前没有正在运行的任务"
        if hasattr(self.app, "request_pause"):
            ok, msg = self.app.request_pause()
            if ok:
                with self._lock:
                    self.status_level = "paused"
                    self.step_text = "任务已暂停（按 F8 或点继续恢复）"
                    if hasattr(self.app, "get_runtime_state"):
                        self.runtime_state = self.app.get_runtime_state()
                if self.glass is not None and hasattr(self.glass, "set_runtime_prompt") and not self.glass.closed:
                    self.glass.set_runtime_prompt(self.runtime_state)
            return ok, msg
        return False, "当前版本不支持暂停"

    def resume_current_task(self, action="continue"):
        with self._lock:
            if not (self.job_running or self.capture_running):
                return False, "当前没有等待恢复的任务"
        if hasattr(self.app, "request_resume"):
            ok, msg = self.app.request_resume(action)
            if ok:
                with self._lock:
                    if hasattr(self.app, "get_runtime_state"):
                        self.runtime_state = self.app.get_runtime_state()
                    if not (self.runtime_state or {}).get("paused"):
                        self.status_level = "running"
                if self.glass is not None and hasattr(self.glass, "set_runtime_prompt") and not self.glass.closed:
                    self.glass.set_runtime_prompt(self.runtime_state)
            return ok, msg
        return False, "当前版本不支持恢复"

    def stop_current_task(self):
        self.app.request_stop()
        rel_hub = False
        was_running = False
        with self._lock:
            was_running = bool(self.job_running or self.capture_running)
            rmode = self._run_mode
            rtrig = self._run_trigger_type
            rstart = self._run_started_at or time.time()
            self._run_epoch += 1
            self.job_running = False
            self.capture_running = False
            self.need_login_prompt = False
            if self._holds_team_lock:
                self._holds_team_lock = False
                rel_hub = True
            if self.schedule_running:
                self.stop_flag = True
            self.status_level = "stopped"
            self.step_text = "已手动取消"
            self.progress = 0.0
            self.error_text = ""
            self.error_detail = None
            self._task_lock = threading.Lock()
            if hasattr(self.app, "get_runtime_state"):
                self.runtime_state = self.app.get_runtime_state()
        if rel_hub and self.hub is not None:
            try:
                if was_running:
                    self.hub.record_run_event(mode=rmode, trigger_type=rtrig, status="stopped", duration_sec=max(1, int(time.time() - rstart)))
                self.hub.release_run_lock()
            except Exception:
                pass
        if self.glass is not None and not self.glass.closed:
            self.glass.fail("已手动取消")
            self.glass.close(160)
        return True, "已立即取消本轮任务"

    def trigger_hotkey(self, slot="f8"):
        with self._lock:
            if not (self.job_running or self.capture_running):
                return False, "当前没有运行中的任务"
        if hasattr(self.app, "trigger_hotkey_action"):
            return self.app.trigger_hotkey_action(slot)
        return False, "当前版本不支持快捷键"

    def toggle_schedule(self, enable=None):
        with self._lock:
            want = (not self.schedule_running) if enable is None else bool(enable)
            if not want:
                if self.schedule_running:
                    self.stop_flag = True
                    self.app.request_stop()
                    return True, "正在停止自动值守…", False
                return True, "自动值守未开启", False

            if self.schedule_running:
                return True, "自动值守已在运行", True
            if self.job_running or self.capture_running:
                return False, "请等当前任务结束后再开启自动值守", False

            cfg = self.get_config()
            if cfg.get("day_mode") == "custom" and not (cfg.get("custom_days") or []):
                return False, "请至少勾选一天运行星期", False
            day_gap = cfg["day"]["ih"] * 60 + cfg["day"]["im"]
            night_gap = cfg["night"]["ih"] * 60 + cfg["night"]["im"]
            if day_gap < 1 or (cfg.get("night_enabled", True) and night_gap < 1):
                return False, "白天或夜间的执行间隔不能为 0 分钟", False
            if not self.app.build_daily_timetable(cfg):
                return False, "当前未生成任何执行时间点，请检查时段配置", False

            self.schedule_running = True
            self.stop_flag = False
            self.error_text = ""
            self.error_detail = None
        threading.Thread(target=self._schedule_loop, daemon=True).start()
        return True, "已开启自动值守", True

    def _compute_next_point(self, cfg, now_ts, fired_keys):
        """基于全天排期表与运行日期规则，找出当前应立即触发或下一个即将触发的时间点。"""
        from datetime import datetime, time as dtime, timedelta
        timetable = self.app.build_daily_timetable(cfg)
        if not timetable:
            return None
        base_date = datetime.fromtimestamp(now_ts).date()
        for day_off in range(0, 8):
            target_date = base_date + timedelta(days=day_off)
            day_lt = time.localtime(datetime.combine(target_date, dtime(12, 0)).timestamp())
            if not self.app.is_run_day_allowed(cfg, day_lt):
                continue
            day_str = target_date.strftime("%Y-%m-%d")
            for pt in timetable:
                ph, pm = self.app.parse_hm(pt["time"])
                pt_ts = datetime.combine(target_date, dtime(ph, pm)).timestamp()
                date_key = f"{day_str} {pt['time']}"
                if date_key in fired_keys:
                    continue
                if day_off == 0 and 0 <= (now_ts - pt_ts) <= 90:
                    return pt_ts, date_key, pt, True, day_off
                if pt_ts > now_ts:
                    return pt_ts, date_key, pt, False, day_off
        return None

    def _schedule_loop(self):
        WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
        round_num = 1
        fired_keys = set()
        try:
            while not self.stop_flag:
                cfg = self.get_config()
                if hasattr(self.app, "set_keep_awake"):
                    self.app.set_keep_awake(bool(cfg.get("prevent_sleep", True)), reason="schedule")
                now_ts = time.time()
                now_lt = time.localtime(now_ts)
                today_prefix = time.strftime("%Y-%m-%d", now_lt)
                fired_keys = {k for k in fired_keys if k.startswith(today_prefix)}

                found = self._compute_next_point(cfg, now_ts, fired_keys)
                if found is None:
                    with self._lock:
                        if not self.job_running and not self.capture_running:
                            self.status_level = "idle"
                            self.step_text = "未选择任何有效运行日期或时段"
                        self.next_run_ts = 0.0
                        self.next_run_str = "—"
                    for _ in range(5):
                        if self.stop_flag:
                            break
                        time.sleep(1)
                    continue

                pt_ts, date_key, pt, ready, day_off = found
                if ready and not self.job_running and not self.capture_running:
                    can_run = True if self.hub is None else self.hub.get_my_permissions().get("can_run", True)
                    if not can_run:
                        if time.time() - self._last_sched_busy_log > 45:
                            self._on_log("当前团队设置暂不执行本机定时触发，已自动跳过本轮")
                            self._last_sched_busy_log = time.time()
                    elif self._task_lock.acquire(blocking=False):
                        hub_ok = True
                        if self.hub is not None:
                            lres = self.hub.acquire_run_lock(mode_label=f"定时 {pt['time']}", step_text=f"定时第 {round_num} 轮准备中…")
                            if not lres.get("ok"):
                                hub_ok = False
                                try:
                                    self._task_lock.release()
                                except Exception:
                                    pass
                                holder = (lres.get("locked_by") or {}).get("display_name") or "其他成员"
                                if time.time() - self._last_sched_busy_log > 15:
                                    self._on_log(f"定时点 {pt['time']} 遇「{holder}」正在占用表格，自动避让等待其释放…")
                                    self._last_sched_busy_log = time.time()
                        if hub_ok:
                            fired_keys.add(date_key)
                            run_cfg = self.get_config()
                            run_cfg["enable_capture"] = bool(pt.get("do_capture", True))
                            run_cfg["enable_dingtalk"] = bool(pt.get("do_dingtalk", False))
                            run_cfg["dt_silent_send"] = bool(pt.get("do_silent", run_cfg.get("dt_silent_send", True)))

                            nxt_after = self._compute_next_point(cfg, now_ts, fired_keys)
                            with self._lock:
                                self.job_running = True
                                self._holds_team_lock = True
                                self._run_started_at = time.time()
                                self._run_mode = "full"
                                self._run_trigger_type = "schedule"
                                self.status_level = "running"
                                self.progress = 0.02
                                self.error_text = ""
                                self.error_detail = None
                                if nxt_after:
                                    n_ts, _, n_pt, _, n_off = nxt_after
                                    n_lt = time.localtime(n_ts)
                                    self.next_run_ts = n_ts
                                    self.next_run_str = n_pt["time"] if n_off == 0 else f"{WEEKDAYS[n_lt.tm_wday]} {n_pt['time']}"
                            self._run_once_worker(
                                round_num,
                                owns_lock=True,
                                run_cfg_override=run_cfg,
                                trigger_lt=time.localtime(pt_ts),
                                trigger_hm=pt["time"],
                            )
                            round_num += 1
                            continue

                pt_lt = time.localtime(pt_ts)
                day_ok = self.app.is_run_day_allowed(cfg, now_lt)
                with self._lock:
                    if not self.job_running and not self.capture_running:
                        self.status_level = "idle"
                        if not day_ok:
                            self.step_text = "今日不在运行周期，静默休眠中"
                        elif day_off == 0:
                            self.step_text = f"自动值守中（下一轮 {pt['time']}）"
                        else:
                            self.step_text = f"今日排期已结束（下轮 {WEEKDAYS[pt_lt.tm_wday]} {pt['time']}）"
                    self.next_run_ts = pt_ts
                    self.next_run_str = pt["time"] if day_off == 0 else f"{WEEKDAYS[pt_lt.tm_wday]} {pt['time']}"

                for _ in range(5):
                    if self.stop_flag:
                        break
                    time.sleep(1)
        finally:
            if hasattr(self.app, "set_keep_awake"):
                self.app.set_keep_awake(False, reason="schedule")

        with self._lock:
            self.schedule_running = False
            self.next_run_ts = 0.0
            self.next_run_str = "—"
            if not self.job_running and not self.capture_running:
                self.status_level = "stopped"
                self.step_text = "自动值守已停止"


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN" data-theme="light">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
<title>暗号表助手</title>
<link rel="icon" type="image/png" href="/icon.png?v=2.8.7" />
<link rel="shortcut icon" href="/favicon.ico?v=2.8.7" />
<style>
  :root {
    --bg: #EAF0F8;
    --bg-grad: radial-gradient(920px 520px at 14% -8%, rgba(0, 122, 255, 0.14), transparent 62%),
               radial-gradient(780px 460px at 88% 4%, rgba(88, 86, 214, 0.11), transparent 64%),
               radial-gradient(740px 460px at 50% 108%, rgba(50, 173, 230, 0.10), transparent 62%);
    --shell-bg: rgba(255, 255, 255, 0.44);
    --nav: rgba(247, 249, 254, 0.80);
    --tabbar: rgba(250, 251, 255, 0.86);
    --card: rgba(255, 255, 255, 0.93);
    --card-solid: #FFFFFF;
    --card-press: #F1F4FA;
    --sep: rgba(60, 60, 67, 0.11);
    --sep-strong: rgba(60, 60, 67, 0.22);
    --text: #111827;
    --text-2: #374151;
    --sub: #6B7280;
    --sub-2: #9CA3AF;
    --fill: rgba(118, 130, 152, 0.12);
    --fill-2: rgba(118, 130, 152, 0.08);
    --pill-blue: rgba(0, 122, 255, 0.11);
    --pill-indigo: rgba(88, 86, 214, 0.12);
    --switch-off: rgba(120, 120, 128, 0.22);
    --shadow: 0 4px 18px rgba(15, 32, 68, 0.055), inset 0 1px 0 rgba(255, 255, 255, 0.7);
    --shadow-hover: 0 10px 26px rgba(0, 92, 230, 0.10), inset 0 1px 0 rgba(255, 255, 255, 0.9);
    --shadow-lg: 0 18px 46px rgba(15, 32, 68, 0.16);
    --sheet-bg: rgba(255, 255, 255, 0.94);
    --blue: #007AFF;
    --green: #28CD41;
    --orange: #FF9500;
    --red: #FF3B30;
    --indigo: #5856D6;
    --teal: #32ADE6;
    --purple: #AF52DE;
    --pink: #FF2D55;
    --spring: cubic-bezier(0.28, 0.84, 0.32, 1.15);
    --ease: cubic-bezier(0.32, 0.72, 0, 1);
  }
  [data-theme="dark"] {
    --bg: #080B12;
    --bg-grad: radial-gradient(900px 480px at 14% -8%, rgba(10, 132, 255, 0.22), transparent 62%),
               radial-gradient(760px 440px at 88% 4%, rgba(94, 92, 230, 0.20), transparent 64%),
               radial-gradient(700px 460px at 50% 108%, rgba(50, 173, 230, 0.12), transparent 62%);
    --shell-bg: rgba(20, 24, 34, 0.54);
    --nav: rgba(18, 22, 32, 0.84);
    --tabbar: rgba(24, 28, 38, 0.88);
    --card: rgba(26, 31, 44, 0.91);
    --card-solid: #1E222D;
    --card-press: #2A3040;
    --sep: rgba(148, 163, 184, 0.16);
    --sep-strong: rgba(148, 163, 184, 0.30);
    --text: #F8FAFC;
    --text-2: #E2E8F0;
    --sub: #94A3B8;
    --sub-2: #64748B;
    --fill: rgba(148, 163, 184, 0.16);
    --fill-2: rgba(148, 163, 184, 0.10);
    --pill-blue: rgba(10, 132, 255, 0.20);
    --pill-indigo: rgba(94, 92, 230, 0.22);
    --switch-off: rgba(148, 163, 184, 0.28);
    --shadow: 0 6px 24px rgba(0, 0, 0, 0.42), inset 0 1px 0 rgba(255, 255, 255, 0.06);
    --shadow-hover: 0 12px 32px rgba(10, 132, 255, 0.18), inset 0 1px 0 rgba(255, 255, 255, 0.10);
    --shadow-lg: 0 22px 52px rgba(0, 0, 0, 0.65);
    --sheet-bg: rgba(28, 32, 44, 0.95);
    --blue: #0A84FF;
    --green: #30D158;
    --orange: #FF9F0A;
    --red: #FF453A;
    --indigo: #5E5CE6;
    --teal: #64D2FF;
    --purple: #BF5AF2;
    --pink: #FF375F;
  }

  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html, body {
    margin: 0;
    padding: 0;
    height: 100%;
    background: var(--bg);
    background-image: var(--bg-grad);
    background-attachment: fixed;
    color: var(--text);
    font-family: "SF Pro Text", "SF Pro Display", -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei UI", sans-serif;
    font-size: 13.5px;
    line-height: 1.45;
    -webkit-font-smoothing: antialiased;
    overflow: hidden;
    user-select: none;
  }

  ::-webkit-scrollbar { width: 5px; height: 5px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: rgba(142, 142, 147, 0.30); border-radius: 99px; }

  /* 桌面宽屏圆角玻璃舞台壳体（自适应 880px ~ 1420px） */
  .shell {
    position: relative;
    z-index: 1;
    width: 100%;
    max-width: 1420px;
    margin: 0 auto;
    height: 100vh;
    max-height: 100vh;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    background: var(--shell-bg);
    border-radius: 24px;
    box-shadow: inset 0 0 0 0.5px rgba(255, 255, 255, 0.45);
  }
  [data-theme="dark"] .shell {
    box-shadow: inset 0 0 0 0.5px rgba(255, 255, 255, 0.08);
  }

  /* 顶部毛玻璃导航 + 灵动岛（绝对固定于顶部，不随下方内容滚动） */
  .top-header {
    position: relative;
    flex-shrink: 0;
    z-index: 60;
    padding: 12px 18px 10px;
    background: var(--nav);
    backdrop-filter: saturate(190%) blur(28px);
    -webkit-backdrop-filter: saturate(190%) blur(28px);
    border-bottom: 0.5px solid var(--sep);
    border-radius: 26px 26px 0 0;
    box-shadow: 0 4px 16px rgba(15, 32, 68, 0.045);
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  /* 下方独立滚动内容舞台 */
  .scroll-stage {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    overflow-x: hidden;
    overscroll-behavior: contain;
    display: flex;
    flex-direction: column;
  }

  .nav-bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }
  .large-title-wrap {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-shrink: 0;
  }
  .nav-app-icon {
    width: 38px;
    height: 38px;
    border-radius: 10px;
    flex-shrink: 0;
    display: block;
    background: transparent;
    filter: drop-shadow(0 4px 10px rgba(0, 122, 255, 0.24));
    transition: transform 0.32s var(--spring);
  }
  .nav-app-icon:hover { transform: scale(1.08) rotate(-3deg); }
  .large-title {
    margin: 0;
    font-size: 20px;
    font-weight: 800;
    letter-spacing: -0.5px;
    background: linear-gradient(120deg, var(--text) 35%, var(--blue) 100%);
    -webkit-background-clip: text;
    background-clip: text;
    color: var(--text);
  }
  .ver-pill {
    font-size: 11px;
    font-weight: 700;
    padding: 2px 9px;
    border-radius: 999px;
    background: var(--pill-blue);
    color: var(--blue);
    font-variant-numeric: tabular-nums;
  }

  .header-center-island {
    flex: 1;
    display: flex;
    justify-content: center;
    min-width: 0;
  }
  .island {
    background: rgba(14, 17, 26, 0.94);
    color: #FFFFFF;
    border-radius: 999px;
    padding: 6px 14px;
    min-width: 240px;
    max-width: 460px;
    width: 100%;
    box-shadow: 0 8px 22px rgba(0, 0, 0, 0.20), inset 0 0 0 0.5px rgba(255, 255, 255, 0.16);
    transition: all 0.36s var(--ease);
  }
  .island.expanded {
    border-radius: 18px;
    padding: 10px 14px;
    max-width: 100%;
    box-shadow: 0 12px 30px rgba(0, 0, 0, 0.26), inset 0 0 0 0.5px rgba(255, 255, 255, 0.18);
  }
  .island-top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
  }
  .island-left {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
    flex: 1;
  }
  .island-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: #30D158;
    box-shadow: 0 0 0 3px rgba(48, 209, 88, 0.24);
    flex-shrink: 0;
    transition: background 0.25s;
  }
  .island-dot.err { background: #FF453A; box-shadow: 0 0 0 3px rgba(255, 69, 58, 0.28); }
  .island-dot.stopped { background: #8E8E93; box-shadow: none; }
  .island-dot.paused { background: #FF9F0A; box-shadow: 0 0 0 3px rgba(255, 159, 10, 0.30); }
  .island-dot.sched { background: #30D158; animation: dot-breathe 1.8s ease-in-out infinite; }
  @keyframes dot-breathe {
    0%, 100% { box-shadow: 0 0 0 2px rgba(48, 209, 88, 0.22); }
    50% { box-shadow: 0 0 0 6px rgba(48, 209, 88, 0.44); }
  }

  .ios-spinner {
    position: relative;
    width: 14px;
    height: 14px;
    flex-shrink: 0;
    display: none;
  }
  .island.busy .ios-spinner { display: block; }
  .island.busy .island-dot { display: none; }
  .island.paused .ios-spinner, .island.recovery .ios-spinner { display: none !important; }
  .island.paused .island-dot, .island.recovery .island-dot { display: block !important; }
  .ios-spinner div {
    position: absolute;
    left: 46%;
    top: 4%;
    width: 10%;
    height: 28%;
    background: #FFFFFF;
    border-radius: 50px;
    transform-origin: center 165%;
    animation: ios-spin 0.9s linear infinite;
  }
  @keyframes ios-spin { 0% { opacity: 1; } 100% { opacity: 0.16; } }

  .island-text {
    font-size: 12.5px;
    font-weight: 600;
    color: rgba(255, 255, 255, 0.96);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .island-pill-tag {
    font-size: 11px;
    font-weight: 600;
    color: rgba(255, 255, 255, 0.72);
    font-variant-numeric: tabular-nums;
    flex-shrink: 0;
  }
  .island-ctrls {
    display: none;
    align-items: center;
    gap: 5px;
    flex-shrink: 0;
  }
  .island.busy .island-ctrls { display: inline-flex; }
  .island.busy .island-pill-tag { display: none; }
  .island-ctrl-btn {
    border: none;
    color: #FFFFFF;
    font-family: inherit;
    font-size: 11px;
    font-weight: 600;
    padding: 4px 9px;
    border-radius: 999px;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: 3px;
    transition: transform 0.16s var(--spring), filter 0.16s;
  }
  .island-ctrl-btn:hover { filter: brightness(1.08); }
  .island-ctrl-btn:active { transform: scale(0.92); }
  .island-pause { background: #FF9F0A; }
  .island-resume { background: #30D158; }
  .island-stop { background: #FF453A; }

  .island-prog {
    display: none;
    align-items: center;
    gap: 8px;
    margin-top: 8px;
  }
  .island.busy .island-prog { display: flex; }
  .island-track {
    flex: 1;
    height: 4.5px;
    border-radius: 999px;
    background: rgba(255, 255, 255, 0.18);
    overflow: hidden;
  }
  .island-fill {
    height: 100%;
    width: 0%;
    border-radius: 999px;
    background: linear-gradient(90deg, #0A84FF, #30D158);
    transition: width 0.35s var(--ease);
  }
  .island-pct {
    font-size: 11px;
    font-weight: 600;
    color: rgba(255, 255, 255, 0.85);
    min-width: 30px;
    text-align: right;
    font-variant-numeric: tabular-nums;
  }
  .island-hint {
    display: none;
    margin-top: 6px;
    font-size: 11px;
    line-height: 1.35;
    color: rgba(255, 255, 255, 0.72);
  }
  .island.busy .island-hint { display: block; }

  .island-recovery {
    display: none;
    margin-top: 8px;
    padding: 9px 11px;
    border-radius: 13px;
    background: rgba(255, 159, 10, 0.18);
    border: 0.5px solid rgba(255, 159, 10, 0.40);
  }
  .island-recovery.show { display: block; animation: card-rise 0.25s var(--ease); }
  .island-recovery.login-mode {
    background: rgba(10, 132, 255, 0.22);
    border-color: rgba(10, 132, 255, 0.42);
  }
  .rec-title { font-size: 12.5px; font-weight: 700; color: #FFD180; margin-bottom: 4px; }
  .island-recovery.login-mode .rec-title { color: #8AC8FF; }
  .rec-detail { font-size: 11.5px; line-height: 1.45; color: rgba(255, 255, 255, 0.92); white-space: pre-line; }
  .rec-actions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
  .rec-btn {
    border: none;
    border-radius: 999px;
    font-family: inherit;
    font-size: 11.5px;
    font-weight: 600;
    padding: 5px 12px;
    cursor: pointer;
    color: #FFFFFF;
    transition: transform 0.16s var(--spring);
  }
  .rec-btn:active { transform: scale(0.93); }
  .rec-btn.primary { background: #0A84FF; }
  .rec-btn.secondary { background: rgba(255, 255, 255, 0.18); }

  .island-alert {
    display: none;
    margin-top: 7px;
    padding: 6px 10px;
    border-radius: 10px;
    font-size: 11.5px;
    font-weight: 500;
    line-height: 1.35;
    word-break: break-word;
  }
  .island-alert.err { display: block; background: rgba(255, 69, 58, 0.22); color: #FF8A84; }
  .island-alert.login { display: block; background: rgba(10, 132, 255, 0.24); color: #7CC4FF; }

  .nav-actions {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-shrink: 0;
  }
  .nav-circle-btn {
    position: relative;
    width: 34px;
    height: 34px;
    border: none;
    border-radius: 50%;
    background: var(--fill);
    color: var(--text);
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    transition: transform 0.2s var(--spring), background 0.2s;
  }
  .nav-circle-btn:hover { background: var(--fill-2); transform: scale(1.06); }
  .nav-circle-btn:active { transform: scale(0.9); }
  .nav-circle-btn svg { width: 16px; height: 16px; }
  .update-dot {
    position: absolute;
    top: 3px;
    right: 3px;
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--red);
    display: none;
  }

  /* 结构化报错大白话排障卡片（置顶可见） */
  .error-diag-card {
    display: none;
    margin: 12px 18px 0;
    padding: 13px 15px;
    border-radius: 18px;
    background: linear-gradient(135deg, rgba(255, 59, 48, 0.08), rgba(255, 149, 0, 0.06));
    border: 1px solid rgba(255, 59, 48, 0.28);
    box-shadow: 0 8px 24px rgba(255, 59, 48, 0.08);
    animation: card-rise 0.3s var(--ease);
  }
  .error-diag-card.show { display: block; }
  [data-theme="dark"] .error-diag-card {
    background: linear-gradient(135deg, rgba(255, 69, 58, 0.14), rgba(255, 159, 10, 0.10));
    border-color: rgba(255, 69, 58, 0.38);
  }
  .diag-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    margin-bottom: 8px;
    flex-wrap: wrap;
  }
  .diag-code-wrap {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
  }
  .diag-code-badge {
    font-family: "SF Mono", "Cascadia Mono", "Consolas", monospace;
    font-size: 11.5px;
    font-weight: 800;
    padding: 3px 9px;
    border-radius: 999px;
    background: var(--red);
    color: #FFFFFF;
    letter-spacing: 0.3px;
    flex-shrink: 0;
  }
  .diag-title {
    font-size: 14px;
    font-weight: 700;
    color: var(--text);
  }
  .diag-cols {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
  }
  .diag-box {
    background: var(--card);
    border-radius: 12px;
    padding: 9px 11px;
    border: 0.5px solid var(--sep);
    user-select: text;
  }
  .diag-box-lbl {
    font-size: 11px;
    font-weight: 700;
    color: var(--sub);
    margin-bottom: 3px;
  }
  .diag-box-txt {
    font-size: 12.5px;
    color: var(--text);
    white-space: pre-line;
    line-height: 1.45;
  }
  .diag-btns {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
    margin-top: 9px;
  }
  .diag-ai-panel {
    display: none;
    margin-top: 9px;
    padding: 10px 12px;
    border-radius: 12px;
    background: rgba(0, 122, 255, 0.08);
    border: 0.5px solid rgba(0, 122, 255, 0.24);
    font-size: 12.5px;
    line-height: 1.55;
    color: var(--text);
    white-space: pre-wrap;
    user-select: text;
  }
  .diag-ai-panel.show { display: block; animation: card-rise 0.25s var(--ease); }
  .md-h3 { font-weight: 700; font-size: 13px; color: var(--blue); margin: 6px 0 2px; }
  .md-h3:first-child { margin-top: 0; }
  .md-quote {
    margin: 4px 0;
    padding: 4px 9px;
    border-left: 3px solid var(--blue);
    background: rgba(0, 122, 255, 0.07);
    border-radius: 0 8px 8px 0;
    color: var(--text-2);
  }
  .md-hr { height: 0.5px; background: var(--sep-strong); margin: 7px 0; }
  .ag-prompt-box {
    display: none;
    margin-top: 9px;
    padding: 10px 12px;
    border-radius: 12px;
    background: var(--card);
    border: 0.5px dashed rgba(88, 86, 214, 0.45);
  }
  .ag-prompt-box.show { display: block; animation: card-rise 0.25s var(--ease); }
  .ag-prompt-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    margin-bottom: 6px;
  }
  .ag-prompt-title { font-size: 11.5px; font-weight: 700; color: var(--indigo); }
  .ag-prompt-pre {
    width: 100%;
    min-height: 112px;
    max-height: 220px;
    overflow-y: auto;
    font-family: "SF Mono", "Cascadia Mono", "Consolas", "Microsoft YaHei UI", monospace;
    font-size: 11.5px;
    line-height: 1.48;
    color: var(--text);
    background: var(--fill-2);
    border: none;
    border-radius: 9px;
    padding: 8px 10px;
    resize: vertical;
    outline: none;
    user-select: text;
    white-space: pre-wrap;
    word-break: break-word;
  }

  /* 主内容区域 & 双列 Bento 网格 */
  .main-content {
    flex: 1;
    padding: 14px 18px 104px;
  }
  .tab-pane {
    display: none;
    flex-direction: column;
    gap: 14px;
  }
  .tab-pane.active { display: flex; }

  .bento-grid {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    gap: 14px;
    align-items: start;
  }
  .bento-grid.home-split {
    grid-template-columns: minmax(0, 1.02fr) minmax(0, 1.18fr);
  }
  .kpi-ribbon-4 {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 10px;
  }
  .kpi-card {
    background: var(--card);
    border: 0.5px solid var(--sep);
    border-radius: 16px;
    padding: 11px 13px;
    box-shadow: var(--shadow);
    cursor: pointer;
    display: flex;
    flex-direction: column;
    gap: 5px;
    min-width: 0;
    position: relative;
    overflow: hidden;
    transition: transform 0.22s var(--spring), box-shadow 0.22s var(--ease);
  }
  .kpi-card:hover { transform: translateY(-2px); box-shadow: var(--shadow-hover); }
  .kpi-card:active { transform: scale(0.97); }
  .kpi-card::after {
    content: "";
    position: absolute;
    left: 0;
    top: 10px;
    bottom: 10px;
    width: 3px;
    border-radius: 0 3px 3px 0;
    background: var(--blue);
  }
  .kpi-card.k-purple::after { background: var(--purple); }
  .kpi-card.k-teal::after { background: var(--teal); }
  .kpi-card.k-green::after { background: var(--green); }
  .kpi-card .mini-stat-val { font-size: 14px; font-weight: 800; }
  .bento-col {
    display: flex;
    flex-direction: column;
    gap: 14px;
    min-width: 0;
  }
  @media (max-width: 900px) {
    .kpi-ribbon-4 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .bento-grid.home-split { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); }
  }
  @media (max-width: 680px) {
    .bento-grid, .bento-grid.home-split { grid-template-columns: 1fr; }
    .diag-cols { grid-template-columns: 1fr; }
  }

  .tab-pane.active .bento-col > * {
    animation: card-rise 0.34s var(--ease) both;
  }
  .tab-pane.active .bento-col > *:nth-child(2) { animation-delay: 0.04s; }
  .tab-pane.active .bento-col > *:nth-child(3) { animation-delay: 0.08s; }
  .tab-pane.active .bento-col > *:nth-child(4) { animation-delay: 0.12s; }
  @keyframes card-rise {
    from { opacity: 0; transform: translateY(10px) scale(0.99); }
    to { opacity: 1; transform: translateY(0) scale(1); }
  }

  /* 交互卡片基础聚光灯与微抬升 */
  .ios-group, .cc-tile, .mini-stat-card, .ai-chat-card {
    --mx: 50%;
    --my: 50%;
    position: relative;
    transition: transform 0.24s var(--spring), box-shadow 0.24s var(--ease), border-color 0.2s, background 0.2s;
  }
  .ios-group:hover, .cc-tile:hover, .mini-stat-card:hover, .ai-chat-card:hover {
    box-shadow: var(--shadow-hover);
  }
  .cc-tile:hover, .mini-stat-card:hover {
    transform: translateY(-2px);
  }
  .cc-tile::before, .mini-stat-card::before {
    content: "";
    position: absolute;
    inset: 0;
    border-radius: inherit;
    background: radial-gradient(240px circle at var(--mx) var(--my), rgba(10, 132, 255, 0.11), transparent 65%);
    opacity: 0;
    transition: opacity 0.22s;
    pointer-events: none;
  }
  .cc-tile:hover::before, .mini-stat-card:hover::before { opacity: 1; }

  /* 主运行按钮 + Shimmer 光泽扫过 */
  .hero-action {
    position: relative;
    overflow: hidden;
    border: none;
    width: 100%;
    text-align: left;
    border-radius: 20px;
    padding: 17px 18px;
    background: linear-gradient(135deg, #007AFF 0%, #1B64F2 50%, #5856D6 100%);
    color: #FFFFFF;
    box-shadow: 0 12px 28px rgba(0, 122, 255, 0.30), inset 0 1px 0 rgba(255, 255, 255, 0.30);
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 14px;
    cursor: pointer;
    transition: transform 0.22s var(--spring), box-shadow 0.22s var(--ease), opacity 0.2s;
  }
  .hero-action::after {
    content: "";
    position: absolute;
    top: -60%;
    left: -50%;
    width: 45%;
    height: 220%;
    background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.24), transparent);
    transform: rotate(24deg) translateX(-140%);
    transition: transform 0.7s ease;
    pointer-events: none;
  }
  .hero-action:hover:not(:disabled) {
    transform: translateY(-2px);
    box-shadow: 0 16px 34px rgba(0, 122, 255, 0.36), inset 0 1px 0 rgba(255, 255, 255, 0.38);
  }
  .hero-action:hover:not(:disabled)::after {
    transform: rotate(24deg) translateX(460%);
  }
  .hero-action:active:not(:disabled) { transform: scale(0.975); }
  .hero-action:disabled { opacity: 0.55; cursor: not-allowed; }
  .hero-copy { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
  .hero-title { font-size: 17.5px; font-weight: 800; letter-spacing: -0.3px; }
  .hero-sub { font-size: 12px; color: rgba(255, 255, 255, 0.88); }
  .hero-play {
    width: 42px;
    height: 42px;
    border-radius: 50%;
    background: rgba(255, 255, 255, 0.24);
    backdrop-filter: blur(8px);
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    transition: transform 0.24s var(--spring);
  }
  .hero-action:hover:not(:disabled) .hero-play { transform: scale(1.1); }
  .hero-play svg { width: 18px; height: 18px; color: #FFFFFF; margin-left: 2px; }

  .tile-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 11px;
  }
  .cc-tile {
    border: 0.5px solid var(--sep);
    background: var(--card);
    color: var(--text);
    border-radius: 18px;
    padding: 13px 13px 12px;
    box-shadow: var(--shadow);
    cursor: pointer;
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 8px;
    text-align: left;
  }
  .cc-tile:active:not(:disabled) { transform: scale(0.96); }
  .cc-tile:disabled { opacity: 0.5; cursor: not-allowed; }
  .tile-ico {
    width: 34px;
    height: 34px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    color: #FFFFFF;
  }
  .tile-ico svg { width: 17px; height: 17px; }
  .tile-ico.orange { background: linear-gradient(135deg, #FFA726, #FF7A00); }
  .tile-ico.green { background: linear-gradient(135deg, #34D058, #229A42); }
  .tile-title { font-size: 14px; font-weight: 700; }
  .tile-sub { font-size: 11.5px; color: var(--sub); line-height: 1.3; }
  .cc-tile.schedule-on {
    background: linear-gradient(145deg, #34C759 0%, #248A3D 100%);
    color: #FFFFFF;
    border-color: transparent;
    box-shadow: 0 10px 26px rgba(52, 199, 89, 0.34);
  }
  .cc-tile.schedule-on .tile-ico { background: rgba(255, 255, 255, 0.24); }
  .cc-tile.schedule-on .tile-sub { color: rgba(255, 255, 255, 0.90); }

  /* 控制台 2x2 速览双列卡片 */
  .stat-grid-2 {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
  }
  .mini-stat-card {
    background: var(--card);
    border: 0.5px solid var(--sep);
    border-radius: 15px;
    padding: 11px 12px;
    box-shadow: var(--shadow);
    cursor: pointer;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .mini-stat-card:active { transform: scale(0.96); background: var(--card-press); }
  .mini-stat-top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 6px;
  }
  .mini-stat-title { font-size: 12px; font-weight: 600; color: var(--sub); }
  .mini-stat-val {
    font-size: 13.5px;
    font-weight: 700;
    color: var(--text);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .mini-stat-sub {
    font-size: 11px;
    color: var(--sub);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  /* 分组卡与表单布局 */
  .section-wrap { display: flex; flex-direction: column; }
  .section-head {
    padding: 0 6px 6px;
    font-size: 12.5px;
    font-weight: 700;
    color: var(--sub);
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }
  .section-footer {
    padding: 6px 6px 0;
    font-size: 11.5px;
    color: var(--sub);
    line-height: 1.45;
  }
  .section-footer.warn { color: var(--orange); }

  .ios-group {
    background: var(--card);
    border-radius: 16px;
    box-shadow: var(--shadow);
    border: 0.5px solid var(--sep);
    overflow: hidden;
  }

  .ios-row {
    position: relative;
    display: flex;
    align-items: center;
    gap: 11px;
    padding: 10px 13px;
    min-height: 44px;
    background: transparent;
    transition: background 0.16s;
  }
  .ios-row.clickable { cursor: pointer; }
  .ios-row.clickable:hover { background: rgba(120, 130, 150, 0.05); }
  .ios-row.clickable:active { background: var(--card-press); }
  .ios-row + .ios-row::before {
    content: "";
    position: absolute;
    left: 52px;
    right: 0;
    top: 0;
    height: 0.5px;
    background: var(--sep);
  }
  .ios-row.no-icon-sep + .ios-row::before,
  .ios-row.compact-sep + .ios-row::before { left: 13px; }

  .sf-badge {
    width: 28px;
    height: 28px;
    border-radius: 7.5px;
    display: flex;
    align-items: center;
    justify-content: center;
    color: #FFFFFF;
    flex-shrink: 0;
    box-shadow: inset 0 0.5px 0 rgba(255, 255, 255, 0.30);
  }
  .sf-badge svg { width: 15px; height: 15px; }
  .sf-blue { background: linear-gradient(180deg, #0A84FF, #007AFF); }
  .sf-green { background: linear-gradient(180deg, #32D74B, #28CD41); }
  .sf-indigo { background: linear-gradient(180deg, #6E6CEB, #5856D6); }
  .sf-orange { background: linear-gradient(180deg, #FFA726, #FF9500); }
  .sf-teal { background: linear-gradient(180deg, #5AC8FA, #32ADE6); }
  .sf-purple { background: linear-gradient(180deg, #BF5AF2, #AF52DE); }
  .sf-pink { background: linear-gradient(180deg, #FF375F, #FF2D55); }
  .sf-red { background: linear-gradient(180deg, #FF453A, #FF3B30); }
  .sf-gray { background: linear-gradient(180deg, #98989D, #8E8E93); }

  .row-main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 1px; }
  .row-title { font-size: 13.5px; font-weight: 600; color: var(--text); }
  .row-sub { font-size: 11.5px; color: var(--sub); line-height: 1.35; }
  .row-right { display: flex; align-items: center; gap: 6px; flex-shrink: 0; max-width: 52%; }
  .row-val {
    font-size: 13px;
    color: var(--sub);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    font-variant-numeric: tabular-nums;
  }
  .chevron-svg { width: 13px; height: 13px; color: var(--sub-2); flex-shrink: 0; }

  /* 双列紧凑开关网格 & 双列输入块 */
  .switch-grid-2 {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 1px;
    background: var(--sep);
  }
  .sg-cell {
    background: var(--card-solid);
    padding: 10px 11px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    transition: background 0.15s;
  }
  [data-theme="dark"] .sg-cell { background: rgba(28, 32, 43, 0.96); }
  .sg-cell:hover { background: var(--card-press); }
  .sg-cell.full { grid-column: span 2; }
  .sg-copy { min-width: 0; display: flex; flex-direction: column; gap: 1px; }
  .sg-title { font-size: 12.5px; font-weight: 600; color: var(--text); }
  .sg-sub { font-size: 10.5px; color: var(--sub); line-height: 1.25; }

  .form-2col {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
    padding: 10px 13px;
  }
  .form-2col + .ios-row::before,
  .ios-row + .form-2col::before {
    content: "";
    position: absolute;
    left: 13px;
    right: 0;
    top: 0;
    height: 0.5px;
    background: var(--sep);
  }
  .form-col-box {
    display: flex;
    flex-direction: column;
    gap: 4px;
    min-width: 0;
    background: var(--fill-2);
    border-radius: 11px;
    padding: 7px 10px;
  }
  .form-col-lbl { font-size: 11px; font-weight: 600; color: var(--sub); }
  .form-col-inrow { display: flex; align-items: center; gap: 4px; }
  .form-col-inrow input {
    border: none;
    outline: none;
    background: transparent;
    color: var(--text);
    font-family: inherit;
    font-size: 13px;
    font-weight: 600;
    width: 100%;
    min-width: 0;
    user-select: text;
  }

  .field-inline { display: flex; align-items: center; gap: 6px; flex: 1; min-width: 0; }
  .field-inline input {
    border: none;
    outline: none;
    background: transparent;
    color: var(--blue);
    font-family: inherit;
    font-size: 13px;
    font-weight: 600;
    text-align: right;
    text-overflow: ellipsis;
    width: 100%;
    min-width: 0;
    user-select: text;
  }
  .field-inline input::placeholder { color: var(--sub-2); font-weight: 400; }
  .field-inline input.left-align { text-align: left; color: var(--text); }

  .clear-circle {
    border: none;
    padding: 0;
    width: 18px;
    height: 18px;
    border-radius: 50%;
    background: var(--sub-2);
    color: #FFFFFF;
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    flex-shrink: 0;
    transition: transform 0.16s var(--spring), background 0.16s;
  }
  .clear-circle:hover { background: var(--red); transform: scale(1.1); }
  .clear-circle svg { width: 10px; height: 10px; }

  /* Apple 弹簧开关 */
  .ios-switch { position: relative; display: inline-block; width: 48px; height: 28px; flex-shrink: 0; }
  .ios-switch input { opacity: 0; width: 0; height: 0; }
  .ios-slider {
    position: absolute;
    inset: 0;
    cursor: pointer;
    background: var(--switch-off);
    border-radius: 28px;
    transition: background 0.24s var(--ease);
  }
  .ios-slider::before {
    content: "";
    position: absolute;
    height: 24px;
    width: 24px;
    left: 2px;
    top: 2px;
    background: #FFFFFF;
    border-radius: 50%;
    box-shadow: 0 2px 5px rgba(0, 0, 0, 0.22);
    transition: transform 0.26s var(--spring), width 0.18s;
  }
  .ios-switch:active .ios-slider::before { width: 28px; }
  .ios-switch input:checked + .ios-slider { background: var(--green); }
  .ios-switch input:checked + .ios-slider::before { transform: translateX(20px); }
  .ios-switch.sm { width: 40px; height: 24px; }
  .ios-switch.sm .ios-slider::before { width: 20px; height: 20px; }
  .ios-switch.sm input:checked + .ios-slider::before { transform: translateX(16px); }
  .ios-switch.xs { width: 34px; height: 20px; }
  .ios-switch.xs .ios-slider::before { width: 16px; height: 16px; }
  .ios-switch.xs input:checked + .ios-slider::before { transform: translateX(14px); }

  /* 步进器与分段控件 */
  .stepper-grp {
    display: inline-flex;
    align-items: center;
    background: var(--fill);
    border-radius: 9px;
    padding: 2px;
    gap: 2px;
  }
  .step-btn {
    border: none;
    width: 24px;
    height: 22px;
    border-radius: 7px;
    background: var(--card-solid);
    color: var(--text);
    font-family: inherit;
    font-size: 13px;
    font-weight: 700;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: transform 0.12s var(--spring), background 0.15s;
  }
  .step-btn:active { transform: scale(0.88); }
  .step-val {
    min-width: 58px;
    text-align: center;
    font-size: 11.5px;
    font-weight: 700;
    color: var(--blue);
    padding: 0 4px;
    font-variant-numeric: tabular-nums;
  }

  .segmented {
    position: relative;
    display: grid;
    grid-template-columns: 1fr 1fr;
    background: var(--fill);
    border-radius: 10px;
    padding: 2.5px;
    min-width: 140px;
  }
  .seg-thumb {
    position: absolute;
    top: 2.5px;
    bottom: 2.5px;
    left: 2.5px;
    width: calc(50% - 2.5px);
    background: var(--card-solid);
    border-radius: 7.5px;
    box-shadow: 0 2px 6px rgba(0, 0, 0, 0.12);
    transition: transform 0.28s var(--spring);
  }
  [data-theme="dark"] .seg-thumb { background: #3A3F50; }
  .seg-btn {
    position: relative;
    z-index: 1;
    border: none;
    background: transparent;
    color: var(--text);
    font-family: inherit;
    font-size: 12px;
    font-weight: 600;
    padding: 5px 6px;
    cursor: pointer;
    text-align: center;
    white-space: nowrap;
  }
  .segmented-3 { grid-template-columns: repeat(3, 1fr); width: 100%; min-width: 0; }
  .segmented-3 .seg-thumb { width: calc((100% - 5px) / 3); }
  .segmented-4 { grid-template-columns: repeat(4, 1fr); width: 100%; min-width: 0; }
  .segmented-4 .seg-thumb { width: calc((100% - 5px) / 4); }

  .mini-seg {
    display: inline-flex;
    align-items: center;
    background: var(--fill);
    border-radius: 8px;
    padding: 2px;
    gap: 2px;
    flex-wrap: nowrap;
  }
  .mini-seg-btn {
    border: none;
    background: transparent;
    color: var(--sub);
    font-family: inherit;
    font-size: 11px;
    font-weight: 600;
    padding: 4px 7px;
    border-radius: 6px;
    cursor: pointer;
    white-space: nowrap;
    transition: background 0.16s, color 0.16s, transform 0.12s;
  }
  .mini-seg-btn:active { transform: scale(0.94); }
  .mini-seg-btn.active {
    background: var(--card-solid);
    color: var(--blue);
    box-shadow: 0 1px 4px rgba(0, 0, 0, 0.12);
  }
  [data-theme="dark"] .mini-seg-btn.active { background: #3A3F50; }

  .ios-pill-btn {
    border: none;
    background: var(--pill-blue);
    color: var(--blue);
    font-family: inherit;
    font-size: 12.5px;
    font-weight: 700;
    padding: 5px 10px;
    border-radius: 9px;
    cursor: pointer;
    font-variant-numeric: tabular-nums;
    transition: transform 0.15s var(--spring), background 0.16s;
  }
  .ios-pill-btn:hover { filter: brightness(0.96); }
  .ios-pill-btn:active { transform: scale(0.93); }
  .ios-pill-btn.neutral { background: var(--fill); color: var(--text); }
  .pill-pair { display: flex; align-items: center; gap: 5px; }
  .pill-sep { font-size: 11.5px; color: var(--sub); }

  /* AI 数据总结 & 多轮对话工作台 */
  .ai-chat-card {
    background: var(--card);
    border: 0.5px solid var(--sep);
    border-radius: 18px;
    box-shadow: var(--shadow);
    overflow: hidden;
    display: flex;
    flex-direction: column;
  }
  .ai-card-hdr {
    padding: 10px 12px;
    background: linear-gradient(120deg, rgba(0, 122, 255, 0.08), rgba(175, 82, 222, 0.07));
    border-bottom: 0.5px solid var(--sep);
    display: flex;
    flex-direction: column;
    gap: 7px;
  }
  .ai-hdr-top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }
  .ai-hdr-left { display: flex; align-items: center; gap: 10px; min-width: 0; flex: 1; }
  .ai-hdr-left .row-title { white-space: nowrap; }
  .cat-mascot-btn {
    position: relative;
    width: 46px;
    height: 46px;
    border: none;
    border-radius: 14px;
    background: linear-gradient(145deg, rgba(10, 132, 255, 0.16), rgba(110, 92, 230, 0.22));
    box-shadow: 0 4px 14px rgba(10, 132, 255, 0.18), inset 0 0 0 1px rgba(255, 255, 255, 0.28);
    padding: 2px;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    cursor: pointer;
    overflow: visible;
    transition: box-shadow 0.25s, background 0.25s;
  }
  .cat-mascot-btn:hover {
    box-shadow: 0 6px 18px rgba(88, 86, 214, 0.34), inset 0 0 0 1px rgba(100, 210, 255, 0.55);
  }
  .cat-mascot-svg {
    width: 40px;
    height: 40px;
    overflow: visible;
    transform-origin: center center;
    will-change: transform;
  }
  .cat-ear-l { transform-origin: 12px 15px; transition: transform 0.24s var(--spring); }
  .cat-ear-r { transform-origin: 36px 15px; transition: transform 0.24s var(--spring); }
  .cat-mascot-btn.twitch-l .cat-ear-l { transform: rotate(-16deg) scale(1.08); }
  .cat-mascot-btn.twitch-r .cat-ear-r { transform: rotate(16deg) scale(1.08); }
  .cat-eyelid {
    transform-origin: center top;
    transform: scaleY(0);
    transition: transform 0.08s ease-out;
  }
  .cat-mascot-btn.is-blinking .cat-eyelid { transform: scaleY(1); }
  .cat-mascot-btn.wink-r .cat-eyelid-r { transform: scaleY(1); }
  .cat-antenna-dot {
    transition: fill 0.4s ease, filter 0.4s ease;
  }
  .cat-mascot-btn.mood-cheer .cat-antenna-dot { fill: #34C759; }
  .cat-mascot-btn.mood-love .cat-antenna-dot { fill: #FF375F; }
  .cat-mascot-btn.mood-curious .cat-antenna-dot { fill: #FF9F0A; }
  .cat-mascot-btn.mood-think .cat-antenna-dot { fill: #BF5AF2; }
  .cat-spark {
    opacity: 0;
    transform-origin: center;
    transition: opacity 0.35s ease, transform 0.45s var(--spring);
  }
  .cat-mascot-btn.mood-cheer .cat-spark,
  .cat-mascot-btn.mood-love .cat-spark { opacity: 1; transform: scale(1.1); }
  .ai-greeting-banner {
    display: none;
    align-items: flex-start;
    justify-content: space-between;
    gap: 8px;
    padding: 8px 12px;
    background: linear-gradient(125deg, rgba(10, 132, 255, 0.11), rgba(110, 92, 230, 0.09));
    border-bottom: 0.5px solid var(--sep);
    font-size: 12px;
    line-height: 1.5;
    color: var(--text);
    animation: card-rise 0.3s var(--ease);
  }
  .ai-greeting-banner.show { display: flex; }
  .ai-greeting-badge {
    display: inline-flex;
    align-items: center;
    flex-shrink: 0;
    font-size: 10.5px;
    font-weight: 700;
    padding: 2px 7px;
    border-radius: 999px;
    background: var(--pill-blue);
    color: var(--blue);
    margin-top: 1px;
  }
  .remote-guide-strip {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    padding: 8px 12px;
    background: linear-gradient(125deg, rgba(48, 209, 88, 0.08), rgba(10, 132, 255, 0.08));
    border-bottom: 0.5px solid var(--sep);
    font-size: 11.5px;
    line-height: 1.48;
    color: var(--text);
  }
  .remote-guide-strip strong { color: var(--blue); font-weight: 700; }
  .remote-guide-strip code {
    font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
    background: var(--fill-2);
    padding: 1px 5px;
    border-radius: 5px;
    font-size: 11px;
    color: var(--text);
  }
  .ai-bubble.is-spotlight {
    background: linear-gradient(135deg, rgba(52, 199, 89, 0.14), rgba(10, 132, 255, 0.12));
    border-color: rgba(52, 199, 89, 0.42);
  }
  .ai-mini-cat { width: 18px; height: 18px; flex-shrink: 0; vertical-align: middle; margin-right: 4px; }
  .ai-src-pill {
    font-size: 10.5px;
    font-weight: 600;
    padding: 2px 9px;
    border-radius: 999px;
    background: var(--pill-blue);
    color: var(--blue);
    max-width: 100%;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    align-self: flex-start;
  }
  .ai-style-bar {
    padding: 9px 11px 7px;
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    border-bottom: 0.5px solid var(--sep);
  }
  .ai-style-chip {
    border: none;
    background: var(--fill);
    color: var(--text);
    font-family: inherit;
    font-size: 11.5px;
    font-weight: 600;
    padding: 5px 10px;
    border-radius: 999px;
    cursor: pointer;
    transition: transform 0.16s var(--spring), background 0.16s, color 0.16s;
  }
  .ai-style-chip:hover { background: var(--pill-blue); color: var(--blue); transform: translateY(-1px); }
  .ai-style-chip:active { transform: scale(0.93); }
  .ai-style-chip.accent { background: var(--pill-indigo); color: var(--indigo); }

  .ai-chat-feed {
    height: 300px;
    overflow-y: auto;
    padding: 11px 13px;
    display: flex;
    flex-direction: column;
    gap: 9px;
    user-select: text;
  }
  .ai-qa-bar {
    padding: 6px 11px 8px;
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
    border-bottom: 0.5px solid var(--sep);
    background: var(--fill-2);
  }
  .home-cell-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 7px;
    padding: 10px 12px;
  }
  .home-cell-pill {
    background: var(--fill-2);
    border: 0.5px solid var(--sep);
    border-radius: 11px;
    padding: 7px 9px;
    cursor: pointer;
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
    transition: background 0.16s, transform 0.16s var(--spring);
  }
  .home-cell-pill:hover { background: var(--pill-blue); transform: translateY(-1px); }
  .home-cell-top { display: flex; align-items: center; justify-content: space-between; gap: 5px; }
  .home-cell-addr {
    font-family: "SF Mono", "Cascadia Mono", "Consolas", monospace;
    font-size: 11.5px;
    font-weight: 800;
    color: var(--blue);
    white-space: nowrap;
  }
  .home-cell-kind {
    font-size: 10px;
    font-weight: 700;
    padding: 1px 6px;
    border-radius: 999px;
    background: var(--card-solid);
    color: var(--sub);
    white-space: nowrap;
  }
  .home-cell-val {
    font-size: 11px;
    color: var(--text-2);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .home-mini-log {
    padding: 8px 12px 10px;
    max-height: 118px;
    overflow-y: auto;
    font-family: "SF Mono", "Cascadia Mono", "Consolas", "Microsoft YaHei UI", monospace;
    font-size: 11px;
    line-height: 1.5;
    user-select: text;
  }
  .quick-preset-bar {
    padding: 8px 12px 9px;
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    border-bottom: 0.5px solid var(--sep);
    background: var(--fill-2);
  }
  .sheet-badge-input {
    width: 78px;
    border: none;
    outline: none;
    background: var(--pill-indigo);
    color: var(--indigo);
    font-family: inherit;
    font-size: 11px;
    font-weight: 700;
    text-align: center;
    padding: 5px 6px;
    border-radius: 8px;
    user-select: text;
    flex-shrink: 0;
  }
  .label-inline-input {
    flex: 1;
    min-width: 70px;
    border: none;
    outline: none;
    background: var(--fill-2);
    color: var(--text);
    font-family: inherit;
    font-size: 11.5px;
    font-weight: 600;
    padding: 5px 8px;
    border-radius: 8px;
    user-select: text;
  }
  .ai-empty-guide {
    font-size: 12px;
    color: var(--sub);
    line-height: 1.55;
    padding: 8px 6px;
  }
  .ai-bubble {
    max-width: 92%;
    padding: 8px 11px;
    border-radius: 13px;
    font-size: 12.5px;
    line-height: 1.52;
    white-space: pre-wrap;
    word-break: break-word;
    animation: card-rise 0.24s var(--ease);
  }
  .ai-bubble.user {
    align-self: flex-end;
    background: linear-gradient(135deg, #007AFF, #2563EB);
    color: #FFFFFF;
    border-bottom-right-radius: 4px;
  }
  .ai-bubble.model {
    align-self: flex-start;
    background: var(--fill-2);
    border: 0.5px solid var(--sep);
    color: var(--text);
    border-bottom-left-radius: 4px;
  }
  .ai-bubble-meta {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    margin-top: 6px;
    padding-top: 5px;
    border-top: 0.5px solid var(--sep);
    font-size: 10.5px;
    color: var(--sub);
  }
  .ai-copy-btn {
    border: none;
    background: var(--pill-blue);
    color: var(--blue);
    font-family: inherit;
    font-size: 10.5px;
    font-weight: 700;
    padding: 3px 9px;
    border-radius: 999px;
    cursor: pointer;
    white-space: nowrap;
    flex-shrink: 0;
  }
  .typing-dots { display: inline-flex; align-items: center; gap: 4px; padding: 4px 2px; }
  .typing-dots span {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--blue);
    animation: typing-bounce 1.1s infinite ease-in-out;
  }
  .typing-dots span:nth-child(2) { animation-delay: 0.16s; }
  .typing-dots span:nth-child(3) { animation-delay: 0.32s; }
  @keyframes typing-bounce { 0%, 80%, 100% { transform: scale(0.6); opacity: 0.4; } 40% { transform: scale(1.0); opacity: 1; } }

  .ai-input-bar {
    padding: 8px 10px;
    border-top: 0.5px solid var(--sep);
    display: flex;
    align-items: center;
    gap: 8px;
    background: var(--fill-2);
  }
  .ai-input-bar input {
    flex: 1;
    min-width: 0;
    border: 0.5px solid var(--sep);
    outline: none;
    background: var(--card-solid);
    color: var(--text);
    font-family: inherit;
    font-size: 12.5px;
    padding: 7px 11px;
    border-radius: 999px;
    user-select: text;
  }
  .ai-send-btn {
    border: none;
    background: var(--blue);
    color: #FFFFFF;
    font-family: inherit;
    font-size: 12px;
    font-weight: 700;
    padding: 7px 13px;
    border-radius: 999px;
    cursor: pointer;
    flex-shrink: 0;
    transition: transform 0.15s var(--spring), opacity 0.15s;
  }
  .ai-send-btn:active { transform: scale(0.92); }
  .ai-send-btn:disabled { opacity: 0.5; cursor: not-allowed; }

  /* 折叠与预设列表样式 */
  .accordion-body { max-height: 0; overflow: hidden; transition: max-height 0.34s var(--ease); }
  .accordion-body.open { max-height: 1200px; }
  .accordion-body.tall.open { max-height: 3600px; }

  .cell-badge-input {
    width: 62px;
    border: none;
    outline: none;
    background: var(--pill-blue);
    color: var(--blue);
    font-family: "SF Mono", "Cascadia Mono", "Consolas", monospace;
    font-size: 12.5px;
    font-weight: 800;
    text-align: center;
    padding: 5px 6px;
    border-radius: 8px;
    text-transform: uppercase;
    user-select: text;
    flex-shrink: 0;
  }
  .preset-sub-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 7px;
  }
  .preset-sub-hint { font-size: 11px; color: var(--sub); line-height: 1.35; }
  .cutoff-box {
    background: var(--fill-2);
    border-radius: 10px;
    padding: 7px 9px;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .cutoff-top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }
  .cutoff-controls {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 6px;
  }
  .ut-sub-wrap {
    padding: 0 13px 10px;
  }
  .ut-meta-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 7px;
  }

  /* 每日排期表 */
  .tt-row {
    position: relative;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 9px 12px;
    cursor: pointer;
    transition: background 0.14s;
  }
  .tt-row:hover { background: rgba(120, 130, 150, 0.05); }
  .tt-row:active { background: var(--card-press); }
  .tt-row + .tt-row::before {
    content: "";
    position: absolute;
    left: 12px;
    right: 0;
    top: 0;
    height: 0.5px;
    background: var(--sep);
  }
  .tt-time-col { display: flex; flex-direction: column; align-items: flex-start; min-width: 54px; flex-shrink: 0; }
  .tt-hm { font-size: 14.5px; font-weight: 800; font-variant-numeric: tabular-nums; color: var(--text); }
  .tt-period { font-size: 10.5px; color: var(--sub); display: flex; align-items: center; gap: 3px; }
  .tt-next-tag { font-size: 9px; font-weight: 700; color: #FFFFFF; background: var(--blue); padding: 0 5px; border-radius: 999px; }
  .tt-next-tag.tomorrow { background: var(--indigo); }
  .tt-badges { flex: 1; min-width: 0; display: flex; flex-wrap: wrap; align-items: center; gap: 4px; }
  .tt-pill {
    font-size: 10.5px;
    font-weight: 600;
    padding: 2px 6px;
    border-radius: 6px;
    white-space: nowrap;
    max-width: 100%;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .tt-pill.cap-on { background: rgba(52, 199, 89, 0.14); color: #1E9E43; }
  .tt-pill.dt-on { background: rgba(0, 122, 255, 0.12); color: var(--blue); }
  .tt-pill.suf-on { background: rgba(88, 86, 214, 0.13); color: var(--indigo); }
  .tt-pill.date-on { background: rgba(50, 173, 230, 0.15); color: #0B78A8; }
  .tt-pill.off { background: var(--fill-2); color: var(--sub); }
  [data-theme="dark"] .tt-pill.cap-on { color: #30D158; }
  [data-theme="dark"] .tt-pill.suf-on { color: #9E9CFA; }
  [data-theme="dark"] .tt-pill.date-on { color: #64D2FF; }

  .tt-right-ctrl { display: flex; align-items: center; gap: 6px; flex-shrink: 0; }
  .tt-sil-wrap {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 3px 6px;
    border-radius: 999px;
    background: var(--fill-2);
  }
  .tt-sil-wrap.muted-dim { opacity: 0.55; }
  .tt-sil-lbl { font-size: 10.5px; font-weight: 600; color: var(--sub); min-width: 22px; text-align: center; }
  .tt-sil-lbl.sil { color: var(--orange); }

  /* 时段、目标规则、日志与指南 */
  .slot-block { position: relative; padding: 11px 13px; display: flex; flex-direction: column; gap: 9px; }
  .slot-block + .slot-block::before, .ios-row + .slot-block::before, .slot-block + .ios-row::before {
    content: "";
    position: absolute;
    left: 52px;
    right: 0;
    top: 0;
    height: 0.5px;
    background: var(--sep);
  }
  .slot-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
  .slot-title-wrap { display: flex; align-items: center; gap: 10px; }
  .slot-controls {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 7px;
    padding-left: 38px;
    transition: opacity 0.2s;
  }
  .slot-controls.disabled { opacity: 0.38; pointer-events: none; }

  .weekday-wrap { padding: 2px 13px 12px; display: none; flex-direction: column; gap: 7px; }
  .weekday-wrap.show { display: flex; }
  .weekday-grid { display: grid; grid-template-columns: repeat(7, 1fr); justify-items: center; gap: 6px; }
  .wday-btn {
    width: 34px;
    height: 34px;
    border: none;
    border-radius: 50%;
    background: var(--fill);
    color: var(--text);
    font-family: inherit;
    font-size: 12.5px;
    font-weight: 600;
    cursor: pointer;
    transition: transform 0.14s var(--spring), background 0.18s, color 0.18s;
  }
  .wday-btn.active { background: var(--blue); color: #FFFFFF; box-shadow: 0 3px 10px rgba(0, 122, 255, 0.30); }
  .wday-hint { font-size: 11px; color: var(--sub); }
  .wday-hint.err { color: var(--red); }

  .head-action-btn {
    border: none;
    background: transparent;
    color: var(--blue);
    font-family: inherit;
    font-size: 12px;
    font-weight: 700;
    cursor: pointer;
    padding: 0;
  }
  .tt-head-tools { display: flex; align-items: center; gap: 8px; }
  .target-item-row { position: relative; display: flex; align-items: center; gap: 9px; padding: 8px 13px; }
  .target-item-row + .target-item-row::before, .target-item-row + .ios-row::before, #dtTargetsList + .ios-row::before {
    content: "";
    position: absolute;
    left: 13px;
    right: 0;
    top: 0;
    height: 0.5px;
    background: var(--sep);
  }
  .target-input-wrap { flex: 1; min-width: 0; background: var(--fill-2); border-radius: 10px; padding: 6px 10px; }
  .target-input-wrap input {
    width: 100%;
    border: none;
    outline: none;
    background: transparent;
    color: var(--text);
    font-family: inherit;
    font-size: 13px;
    font-weight: 600;
    user-select: text;
  }
  .rule-card-row { position: relative; padding: 10px 13px; display: flex; flex-direction: column; gap: 7px; }
  .rule-card-row + .rule-card-row::before, .rule-card-row + .ios-row::before, .ios-row + .rule-card-row::before {
    content: "";
    position: absolute;
    left: 13px;
    right: 0;
    top: 0;
    height: 0.5px;
    background: var(--sep);
  }
  .rule-top { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
  .rule-actions { display: flex; align-items: center; gap: 8px; }
  .rule-input-box { display: flex; align-items: center; gap: 8px; background: var(--fill-2); border-radius: 10px; padding: 7px 10px; }
  .rule-input-box input {
    border: none;
    outline: none;
    background: transparent;
    color: var(--text);
    font-family: inherit;
    font-size: 13px;
    width: 100%;
    user-select: text;
  }
  .empty-rule-tip { padding: 14px; font-size: 12px; color: var(--sub); text-align: center; }
  .add-row-btn {
    position: relative;
    width: 100%;
    border: none;
    background: transparent;
    color: var(--blue);
    font-family: inherit;
    font-size: 13px;
    font-weight: 700;
    padding: 11px 13px;
    cursor: pointer;
    text-align: center;
  }
  .add-row-btn::before { content: ""; position: absolute; left: 13px; right: 0; top: 0; height: 0.5px; background: var(--sep); }

  .log-tools { display: flex; gap: 5px; flex-wrap: wrap; }
  .mini-pill {
    border: none;
    background: var(--fill);
    color: var(--blue);
    font-family: inherit;
    font-size: 11px;
    font-weight: 700;
    padding: 4px 9px;
    border-radius: 999px;
    cursor: pointer;
    transition: transform 0.12s var(--spring), background 0.15s;
  }
  .mini-pill:active { transform: scale(0.93); }
  .mini-pill.active { background: var(--blue); color: #FFFFFF; }
  .log-box {
    padding: 10px 13px;
    height: 290px;
    overflow-y: auto;
    font-family: "SF Mono", "Cascadia Mono", "Consolas", "Microsoft YaHei UI", monospace;
    font-size: 11.5px;
    line-height: 1.55;
    user-select: text;
  }
  .log-empty { color: var(--sub); text-align: center; padding-top: 110px; font-family: inherit; font-size: 12.5px; }
  .log-line { display: flex; align-items: flex-start; gap: 6px; margin-bottom: 3px; word-break: break-all; }
  .log-dot { width: 6px; height: 6px; border-radius: 50%; margin-top: 6px; flex-shrink: 0; background: var(--sub-2); }
  .log-dot.ok { background: var(--green); }
  .log-dot.err { background: var(--red); }
  .log-dot.warn { background: var(--orange); }
  .log-dot.head { background: var(--blue); }
  .log-time { color: var(--sub); flex-shrink: 0; font-variant-numeric: tabular-nums; }
  .log-msg.ok { color: var(--green); font-weight: 600; }
  .log-msg.err { color: var(--red); font-weight: 700; }
  .log-msg.warn { color: var(--orange); }
  .log-msg.head { color: var(--blue); font-weight: 700; }

  .guide-body { display: none; padding: 4px 13px 11px; }
  .guide-body.open { display: block; }
  .guide-item { padding: 7px 0; border-top: 0.5px solid var(--sep); }
  .guide-item-title { font-size: 12.5px; font-weight: 700; color: var(--text); margin-bottom: 2px; }
  .guide-item-desc { font-size: 11.5px; color: var(--sub); line-height: 1.45; }

  .danger-card-btn {
    width: 100%;
    border: 0.5px solid var(--sep);
    background: var(--card);
    color: var(--red);
    font-family: inherit;
    font-size: 14px;
    font-weight: 600;
    padding: 12px;
    border-radius: 14px;
    box-shadow: var(--shadow);
    cursor: pointer;
    transition: background 0.15s, transform 0.16s var(--spring);
  }
  .danger-card-btn:active { background: var(--card-press); transform: scale(0.985); }

  /* 底部悬浮毛玻璃胶囊导航栏 */
  .tabbar-wrap {
    position: fixed;
    left: 0;
    right: 0;
    bottom: 10px;
    z-index: 50;
    display: flex;
    justify-content: center;
    padding: 0 18px;
    pointer-events: none;
  }
  .tabbar {
    pointer-events: auto;
    position: relative;
    width: 100%;
    max-width: 520px;
    height: 58px;
    padding: 5px 7px;
    border-radius: 999px;
    background: var(--tabbar);
    backdrop-filter: saturate(195%) blur(28px);
    -webkit-backdrop-filter: saturate(195%) blur(28px);
    border: 0.5px solid var(--sep-strong);
    box-shadow: 0 10px 30px rgba(15, 32, 68, 0.16);
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    align-items: center;
  }
  .tab-pill-bg {
    position: absolute;
    top: 5px;
    bottom: 5px;
    left: 7px;
    width: calc((100% - 14px) / 4);
    border-radius: 999px;
    background: var(--pill-blue);
    transition: transform 0.34s var(--spring);
    pointer-events: none;
  }
  .tab-btn {
    position: relative;
    z-index: 1;
    border: none;
    background: transparent;
    color: var(--sub);
    font-family: inherit;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 2px;
    cursor: pointer;
    padding: 4px 0;
    transition: color 0.18s, transform 0.18s var(--spring);
  }
  .tab-btn:active { transform: scale(0.92); }
  .tab-btn svg { width: 20px; height: 20px; transition: transform 0.24s var(--spring); }
  .tab-btn.active svg { transform: translateY(-1px) scale(1.06); }
  .tab-btn span { font-size: 10.5px; font-weight: 600; }
  .tab-btn.active { color: var(--blue); }

  /* 弹窗、双轮选择器、双列单点微调弹窗 */
  .sheet-mask {
    position: fixed;
    inset: 0;
    z-index: 85;
    background: rgba(8, 12, 22, 0.38);
    backdrop-filter: blur(5px);
    display: none;
    align-items: center;
    justify-content: center;
    padding: 20px;
  }
  .sheet-mask.show { display: flex; }
  .ios-sheet {
    width: 100%;
    max-width: 440px;
    max-height: 88vh;
    overflow-y: auto;
    background: var(--sheet-bg);
    backdrop-filter: saturate(190%) blur(32px);
    -webkit-backdrop-filter: saturate(190%) blur(32px);
    border-radius: 22px;
    box-shadow: var(--shadow-lg);
    border: 0.5px solid var(--sep);
    padding-bottom: 16px;
    animation: pop-in 0.24s var(--spring);
  }
  .ios-sheet.wide { max-width: 620px; }
  @keyframes pop-in { from { opacity: 0; transform: scale(0.94) translateY(8px); } to { opacity: 1; transform: scale(1) translateY(0); } }
  .sheet-handle { width: 36px; height: 4px; border-radius: 999px; background: var(--sep-strong); margin: 9px auto 4px; }
  .sheet-header { display: flex; align-items: center; justify-content: space-between; padding: 8px 16px 12px; border-bottom: 0.5px solid var(--sep); }
  .sheet-btn { border: none; background: transparent; color: var(--blue); font-family: inherit; font-size: 14.5px; font-weight: 500; cursor: pointer; padding: 4px 6px; }
  .sheet-btn.done { font-weight: 700; }
  .sheet-title { font-size: 15px; font-weight: 700; color: var(--text); }
  .sheet-readout { text-align: center; font-size: 26px; font-weight: 800; color: var(--text); padding: 12px 16px 8px; font-variant-numeric: tabular-nums; }
  .preset-row { display: flex; flex-wrap: wrap; justify-content: center; gap: 7px; padding: 0 16px 12px; }
  .preset-chip { border: none; background: var(--fill); color: var(--text); font-family: inherit; font-size: 12px; font-weight: 600; padding: 5px 11px; border-radius: 999px; cursor: pointer; }
  .preset-chip.active { background: var(--blue); color: #FFFFFF; }
  .wheel-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; padding: 0 20px; }
  .wheel-col-label { text-align: center; font-size: 11.5px; font-weight: 600; color: var(--sub); margin-bottom: 6px; }
  .wheel-list { height: 184px; overflow-y: auto; border-radius: 14px; background: var(--fill-2); padding: 6px; display: flex; flex-direction: column; gap: 3px; }
  .wheel-opt { border: none; background: transparent; color: var(--text); font-family: inherit; font-size: 15px; font-weight: 500; padding: 7px 0; border-radius: 9px; cursor: pointer; font-variant-numeric: tabular-nums; }
  .wheel-opt.selected { background: var(--card-solid); color: var(--blue); font-weight: 700; box-shadow: 0 1px 4px rgba(0, 0, 0, 0.10); }

  .sheet-body-pad { padding: 14px 16px 6px; display: flex; flex-direction: column; gap: 12px; }
  .point-grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  @media (max-width: 560px) { .point-grid-2 { grid-template-columns: 1fr; } }
  .sheet-field-label { font-size: 12px; font-weight: 700; color: var(--sub); margin-bottom: 6px; }
  .point-preview-strip {
    padding: 9px 12px;
    border-radius: 12px;
    background: var(--fill-2);
    border: 0.5px solid var(--sep);
    display: flex;
    flex-direction: column;
    gap: 3px;
    font-size: 12px;
    color: var(--text);
  }

  .modal-mask { position: fixed; inset: 0; z-index: 95; background: rgba(0, 0, 0, 0.36); backdrop-filter: blur(6px); display: none; align-items: center; justify-content: center; padding: 24px; }
  .modal-mask.show { display: flex; }
  .ios-alert { width: 320px; background: var(--sheet-bg); backdrop-filter: saturate(180%) blur(28px); border-radius: 18px; overflow: hidden; box-shadow: var(--shadow-lg); border: 0.5px solid var(--sep); animation: pop-in 0.22s var(--spring); }
  .alert-body { padding: 18px 16px 15px; text-align: center; }
  .alert-title { font-size: 16px; font-weight: 700; margin-bottom: 5px; }
  .alert-msg { font-size: 13px; color: var(--text-2); line-height: 1.5; white-space: pre-line; text-align: left; }
  .alert-actions { display: grid; grid-template-columns: 1fr 1fr; border-top: 0.5px solid var(--sep); }
  .alert-actions.single { grid-template-columns: 1fr; }
  .alert-btn { border: none; background: transparent; padding: 12px 8px; font-family: inherit; font-size: 15px; font-weight: 600; color: var(--blue); cursor: pointer; }
  .alert-btn + .alert-btn { border-left: 0.5px solid var(--sep); }
  .alert-btn.danger { color: var(--red); }

  .toast {
    position: fixed;
    top: 16px;
    left: 50%;
    transform: translate(-50%, -76px);
    z-index: 105;
    background: rgba(20, 24, 34, 0.94);
    color: #FFFFFF;
    backdrop-filter: blur(18px);
    padding: 8px 16px;
    border-radius: 999px;
    font-size: 12.5px;
    font-weight: 600;
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.25);
    transition: transform 0.3s var(--spring), opacity 0.25s;
    opacity: 0;
    pointer-events: none;
  }
  .toast.show { transform: translate(-50%, 0); opacity: 1; }

  /* ==================== 昼夜自适应柔和背景动效（零遮挡文字） ==================== */
  .ambient-scene {
    position: fixed;
    inset: 0;
    z-index: 0;
    pointer-events: none;
    overflow: hidden;
  }
  .sky-layer {
    position: absolute;
    inset: 0;
    transition: opacity 0.65s ease;
  }
  .day-sky { opacity: 1; }
  .night-sky { opacity: 0; }
  [data-theme="dark"] .day-sky { opacity: 0; }
  [data-theme="dark"] .night-sky { opacity: 1; }

  .day-sun-halo {
    position: absolute;
    top: -120px;
    right: -90px;
    width: 460px;
    height: 460px;
    border-radius: 50%;
    background: radial-gradient(circle, rgba(255, 214, 132, 0.34) 0%, rgba(125, 198, 255, 0.16) 44%, transparent 72%);
    filter: blur(18px);
    animation: sun-breathe 11s ease-in-out infinite;
  }
  .day-sky-orb {
    position: absolute;
    bottom: -140px;
    left: -100px;
    width: 480px;
    height: 360px;
    border-radius: 50%;
    background: radial-gradient(circle, rgba(96, 175, 255, 0.22) 0%, rgba(152, 224, 255, 0.10) 48%, transparent 74%);
    filter: blur(28px);
    animation: orb-sway 17s ease-in-out infinite alternate;
  }
  .day-cloud {
    position: absolute;
    border-radius: 999px;
    background: linear-gradient(180deg, rgba(255, 255, 255, 0.66), rgba(240, 248, 255, 0.22));
    filter: blur(26px);
  }
  .day-cloud.c1 { top: 48px; left: -120px; width: 420px; height: 92px; animation: cloud-drift-a 48s ease-in-out infinite alternate; }
  .day-cloud.c2 { top: 170px; right: -110px; width: 360px; height: 78px; opacity: 0.72; animation: cloud-drift-b 58s ease-in-out infinite alternate; }
  .day-cloud.c3 { bottom: 64px; left: 18%; width: 460px; height: 84px; opacity: 0.52; animation: cloud-drift-a 66s ease-in-out infinite alternate-reverse; }
  .day-mote {
    position: absolute;
    border-radius: 50%;
    background: radial-gradient(circle, rgba(255, 255, 255, 0.78), rgba(140, 206, 255, 0.25));
    animation: mote-rise 16s ease-in-out infinite;
  }
  .day-mote.m1 { left: 14%; bottom: 10%; width: 10px; height: 10px; animation-delay: -2s; }
  .day-mote.m2 { left: 36%; bottom: 6%; width: 7px; height: 7px; animation-delay: -6s; animation-duration: 19s; }
  .day-mote.m3 { left: 68%; bottom: 12%; width: 11px; height: 11px; animation-delay: -11s; animation-duration: 21s; }
  .day-mote.m4 { left: 86%; bottom: 8%; width: 8px; height: 8px; animation-delay: -4s; animation-duration: 15s; }

  .night-aurora {
    position: absolute;
    border-radius: 50%;
    filter: blur(42px);
    mix-blend-mode: screen;
  }
  .night-aurora.a1 {
    top: -130px;
    left: 6%;
    width: 680px;
    height: 310px;
    background: radial-gradient(ellipse at center, rgba(48, 209, 180, 0.22), rgba(10, 132, 255, 0.16) 52%, transparent 76%);
    animation: aurora-flow-a 24s ease-in-out infinite alternate;
  }
  .night-aurora.a2 {
    top: -90px;
    right: 4%;
    width: 620px;
    height: 290px;
    background: radial-gradient(ellipse at center, rgba(94, 92, 230, 0.24), rgba(191, 90, 242, 0.14) 52%, transparent 76%);
    animation: aurora-flow-b 30s ease-in-out infinite alternate;
  }
  .night-stars {
    position: absolute;
    width: 2px;
    height: 2px;
    border-radius: 50%;
    background: transparent;
  }
  .night-stars.s1 {
    box-shadow:
      6vw 9vh 0 0 rgba(255,255,255,0.62), 14vw 22vh 0 0 rgba(200,230,255,0.52), 23vw 11vh 0 1px rgba(255,255,255,0.45),
      35vw 7vh 0 0 rgba(255,255,255,0.58), 47vw 16vh 0 0 rgba(180,220,255,0.46), 59vw 8vh 0 1px rgba(255,255,255,0.50),
      71vw 14vh 0 0 rgba(255,255,255,0.64), 83vw 10vh 0 0 rgba(200,232,255,0.56), 92vw 20vh 0 1px rgba(255,255,255,0.48),
      8vw 78vh 0 0 rgba(255,255,255,0.42), 21vw 86vh 0 0 rgba(190,225,255,0.45), 79vw 82vh 0 0 rgba(255,255,255,0.46),
      91vw 72vh 0 1px rgba(190,225,255,0.50), 95vw 44vh 0 0 rgba(255,255,255,0.52), 3vw 52vh 0 0 rgba(255,255,255,0.44);
    animation: star-twinkle 5.5s ease-in-out infinite alternate;
  }
  .night-stars.s2 {
    box-shadow:
      11vw 15vh 0 0 rgba(190,228,255,0.42), 28vw 18vh 0 0 rgba(255,255,255,0.44), 41vw 12vh 0 1px rgba(205,235,255,0.38),
      54vw 21vh 0 0 rgba(255,255,255,0.40), 65vw 13vh 0 0 rgba(200,230,255,0.46), 76vw 19vh 0 1px rgba(255,255,255,0.38),
      88vw 27vh 0 0 rgba(255,255,255,0.44), 16vw 89vh 0 0 rgba(255,255,255,0.34), 85vw 88vh 0 0 rgba(205,235,255,0.40);
    animation: star-twinkle 7.5s ease-in-out infinite alternate-reverse;
  }
  .night-meteor {
    position: absolute;
    top: 12%;
    left: 72%;
    width: 110px;
    height: 1.5px;
    background: linear-gradient(90deg, rgba(255,255,255,0), rgba(145, 225, 255, 0.72), rgba(255,255,255,0.92));
    transform: rotate(-24deg);
    opacity: 0;
    animation: meteor-glide 14s ease-in-out infinite;
  }
  @keyframes sun-breathe { 0%, 100% { transform: scale(1); opacity: 0.72; } 50% { transform: scale(1.08); opacity: 0.96; } }
  @keyframes orb-sway { 0% { transform: translate(0, 0); } 100% { transform: translate(60px, -24px); } }
  @keyframes cloud-drift-a { 0% { transform: translateX(0) translateY(0); } 100% { transform: translateX(180px) translateY(10px); } }
  @keyframes cloud-drift-b { 0% { transform: translateX(0) translateY(0); } 100% { transform: translateX(-160px) translateY(-10px); } }
  @keyframes mote-rise {
    0% { transform: translateY(0) scale(0.8); opacity: 0; }
    25% { opacity: 0.36; }
    75% { opacity: 0.24; }
    100% { transform: translateY(-210px) scale(1.15); opacity: 0; }
  }
  @keyframes aurora-flow-a { 0% { transform: translate(0, 0) scale(1); } 100% { transform: translate(90px, 24px) scale(1.08); } }
  @keyframes aurora-flow-b { 0% { transform: translate(0, 0) scale(1.04); } 100% { transform: translate(-80px, 18px) scale(0.96); } }
  @keyframes star-twinkle { 0% { opacity: 0.45; } 100% { opacity: 0.95; } }
  @keyframes meteor-glide {
    0%, 72% { transform: translate(0, 0) rotate(-24deg); opacity: 0; }
    76% { opacity: 0.85; }
    82% { transform: translate(-240px, 110px) rotate(-24deg); opacity: 0; }
    100% { transform: translate(-240px, 110px) rotate(-24deg); opacity: 0; }
  }

  /* ==================== 成员身份芯片、反馈按钮、自由选组、防撞提醒与隐藏超管后台 ==================== */
  .member-chip-btn {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    height: 32px;
    padding: 2px 10px 2px 3px;
    border-radius: 999px;
    border: 0.5px solid var(--sep);
    background: var(--card);
    color: var(--text);
    font-family: inherit;
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.04);
    transition: transform 0.16s, border-color 0.2s;
  }
  .member-chip-btn:hover { transform: translateY(-1px); border-color: var(--blue); }
  .member-mini-avatar {
    width: 24px;
    height: 24px;
    border-radius: 50%;
    object-fit: cover;
    background: var(--fill);
    flex-shrink: 0;
  }
  .feedback-pill-btn {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    height: 32px;
    padding: 0 10px;
    border-radius: 999px;
    border: 0.5px solid var(--sep);
    background: var(--fill);
    color: var(--text-2);
    font-family: inherit;
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
    transition: background 0.16s, color 0.16s;
  }
  .feedback-pill-btn:hover { background: var(--pill-blue); color: var(--blue); }
  .feedback-pill-btn svg { width: 13px; height: 13px; }

  .top-notice-banner {
    display: none;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 10px 14px;
    margin-bottom: 10px;
    border-radius: 15px;
    border: 0.5px solid var(--sep);
    backdrop-filter: blur(18px);
    font-size: 12.5px;
  }
  .top-notice-banner.show { display: flex; }
  .top-notice-banner.lock { background: rgba(255, 149, 0, 0.12); border-color: rgba(255, 149, 0, 0.34); color: var(--text); }
  .top-notice-banner.hands-off { background: linear-gradient(90deg, rgba(255, 149, 0, 0.22), rgba(255, 204, 0, 0.16)); border: 1px solid rgba(255, 159, 10, 0.65); color: var(--text); box-shadow: 0 4px 14px rgba(255, 149, 0, 0.15); }
  .top-notice-banner.preview { background: rgba(88, 86, 214, 0.14); border-color: rgba(88, 86, 214, 0.36); color: var(--text); }
  .notice-left { display: flex; align-items: center; gap: 10px; min-width: 0; flex: 1; }
  .notice-avatar { width: 28px; height: 28px; border-radius: 50%; object-fit: cover; flex-shrink: 0; }
  .notice-actions { display: flex; align-items: center; gap: 6px; flex-shrink: 0; }

  .ai-team-bar {
    display: flex;
    align-items: center;
    gap: 7px;
    margin-bottom: 8px;
    flex-wrap: wrap;
  }
  .ai-team-input-wrap {
    flex: 1;
    min-width: 170px;
    display: flex;
    align-items: center;
    gap: 6px;
    background: var(--fill-2);
    border: 0.5px solid var(--sep);
    border-radius: 10px;
    padding: 5px 10px;
  }
  .ai-team-input-wrap svg { width: 14px; height: 14px; color: var(--sub); flex-shrink: 0; }
  .ai-team-input-wrap input {
    width: 100%;
    border: none;
    background: transparent;
    color: var(--text);
    font-family: inherit;
    font-size: 12.5px;
    outline: none;
  }
  .dice-btn {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    border: 0.5px solid rgba(88, 86, 214, 0.28);
    background: var(--pill-indigo);
    color: var(--indigo);
    font-family: inherit;
    font-size: 12px;
    font-weight: 700;
    padding: 6px 11px;
    border-radius: 10px;
    cursor: pointer;
    transition: transform 0.15s, background 0.15s;
  }
  .dice-btn:hover { background: rgba(88, 86, 214, 0.22); }
  .dice-btn svg { width: 14px; height: 14px; }
  .dice-btn.spinning svg { animation: dice-spin 0.48s var(--spring); }
  @keyframes dice-spin { 0% { transform: rotate(0deg) scale(1); } 50% { transform: rotate(190deg) scale(1.18); } 100% { transform: rotate(360deg) scale(1); } }

  .rbac-locked {
    position: relative;
  }
  .rbac-lock-tag {
    display: none;
  }

  .card-modal {
    width: min(520px, 94vw);
    max-height: 88vh;
    background: var(--sheet-bg);
    backdrop-filter: saturate(180%) blur(28px);
    border-radius: 22px;
    border: 0.5px solid var(--sep);
    box-shadow: var(--shadow-lg);
    display: flex;
    flex-direction: column;
    overflow: hidden;
    animation: pop-in 0.22s var(--spring);
  }
  .card-modal.wide { width: min(1160px, 95vw); height: min(820px, 90vh); }
  .modal-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 13px 18px;
    border-bottom: 0.5px solid var(--sep);
    flex-shrink: 0;
  }
  .modal-title { font-size: 15.5px; font-weight: 800; color: var(--text); }
  .modal-scroll { padding: 16px 18px; overflow-y: auto; flex: 1; display: flex; flex-direction: column; gap: 12px; }
  .modal-foot { display: flex; align-items: center; justify-content: flex-end; gap: 8px; padding: 12px 18px; border-top: 0.5px solid var(--sep); flex-shrink: 0; }
  .pill-action-btn {
    border: none;
    border-radius: 10px;
    padding: 7px 14px;
    font-family: inherit;
    font-size: 12.5px;
    font-weight: 700;
    cursor: pointer;
    background: var(--fill);
    color: var(--text);
  }
  .pill-action-btn.primary { background: var(--blue); color: #FFFFFF; }
  .pill-action-btn.danger { background: rgba(255, 59, 48, 0.15); color: var(--red); }
  .pill-action-btn.ok { background: rgba(40, 205, 65, 0.16); color: var(--green); }

  .avatar-preset-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; }
  .avatar-preset-opt {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 4px;
    padding: 8px 4px;
    border-radius: 12px;
    border: 1.5px solid transparent;
    background: var(--fill-2);
    cursor: pointer;
    font-size: 11px;
    color: var(--text-2);
  }
  .avatar-preset-opt.active { border-color: var(--blue); background: var(--pill-blue); color: var(--blue); font-weight: 700; }
  .avatar-preset-opt img { width: 44px; height: 44px; border-radius: 14px; }

  .collab-collapse { border: 0.5px solid var(--sep); border-radius: 12px; padding: 10px 12px; background: var(--fill-2); }
  .collab-collapse summary { cursor: pointer; font-size: 12.5px; font-weight: 700; color: var(--blue); }

  .admin-nav-tabs { display: flex; gap: 6px; flex-wrap: wrap; padding: 10px 18px; border-bottom: 0.5px solid var(--sep); background: var(--fill-2); flex-shrink: 0; }
  .admin-tab-btn {
    border: none;
    background: transparent;
    color: var(--sub);
    font-family: inherit;
    font-size: 12.5px;
    font-weight: 700;
    padding: 6px 12px;
    border-radius: 999px;
    cursor: pointer;
  }
  .admin-tab-btn.active { background: var(--blue); color: #FFFFFF; }
  .admin-kpi-grid { display: grid; grid-template-columns: repeat(6, 1fr); gap: 8px; }
  @media (max-width: 900px) { .admin-kpi-grid { grid-template-columns: repeat(3, 1fr); } }
  .admin-kpi { background: var(--card); border: 0.5px solid var(--sep); border-radius: 12px; padding: 10px 12px; }
  .admin-kpi-val { font-size: 19px; font-weight: 800; color: var(--text); font-variant-numeric: tabular-nums; }
  .admin-kpi-lbl { font-size: 11.5px; color: var(--sub); margin-top: 2px; }
  .admin-box { background: var(--card); border: 0.5px solid var(--sep); border-radius: 14px; padding: 12px 14px; display: flex; flex-direction: column; gap: 9px; }
  .admin-box-title { font-size: 13px; font-weight: 800; color: var(--text); display: flex; align-items: center; justify-content: space-between; gap: 8px; flex-wrap: wrap; }
  .filter-bar { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; }
  .small-input {
    border: 0.5px solid var(--sep-strong);
    background: var(--fill-2);
    color: var(--text);
    border-radius: 8px;
    padding: 5px 9px;
    font-family: inherit;
    font-size: 12px;
    outline: none;
  }
  .member-card { border: 0.5px solid var(--sep); border-radius: 14px; background: var(--card); padding: 12px; display: flex; flex-direction: column; gap: 10px; }
  .member-top { display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; }
  .member-id-wrap { display: flex; align-items: center; gap: 10px; }
  .member-lg-avatar { width: 42px; height: 42px; border-radius: 50%; object-fit: cover; border: 1px solid var(--sep); }
  .perm-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 7px; }
  .perm-toggle-item { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 6px 9px; border-radius: 10px; background: var(--fill-2); font-size: 11.5px; }
  .audit-tag { display: inline-flex; align-items: center; padding: 3px 8px; border-radius: 8px; font-size: 11.5px; font-weight: 600; margin: 2px 4px 2px 0; }
  .audit-tag.ok { background: rgba(40, 205, 65, 0.14); color: var(--green); }
  .audit-tag.err { background: rgba(255, 59, 48, 0.14); color: var(--red); }
  .audit-tag.warn { background: rgba(255, 149, 0, 0.14); color: var(--orange); }
  .feed-item { border: 0.5px solid var(--sep); border-radius: 11px; padding: 9px 11px; background: var(--fill-2); font-size: 12px; display: flex; flex-direction: column; gap: 4px; }
  .field-textarea { width: 100%; min-height: 92px; border-radius: 10px; border: 0.5px solid var(--sep-strong); background: var(--fill-2); color: var(--text); padding: 9px 11px; font-family: inherit; font-size: 12.5px; resize: vertical; outline: none; }
</style>
</head>
<body>
  <div class="toast" id="toast">已保存</div>

  <!-- 昼夜柔和背景动效层（z-index:0，绝不遮挡文字与卡片） -->
  <div class="ambient-scene" id="ambientScene" aria-hidden="true">
    <div class="sky-layer day-sky">
      <div class="day-sun-halo"></div>
      <div class="day-sky-orb"></div>
      <div class="day-cloud c1"></div>
      <div class="day-cloud c2"></div>
      <div class="day-cloud c3"></div>
      <div class="day-mote m1"></div>
      <div class="day-mote m2"></div>
      <div class="day-mote m3"></div>
      <div class="day-mote m4"></div>
    </div>
    <div class="sky-layer night-sky">
      <div class="night-aurora a1"></div>
      <div class="night-aurora a2"></div>
      <div class="night-stars s1"></div>
      <div class="night-stars s2"></div>
      <div class="night-meteor"></div>
    </div>
  </div>

  <div class="shell">
    <!-- 顶部粘性导航 + 灵动岛（固定不滚动） -->
    <header class="top-header" id="topHeader">
      <div class="nav-bar">
        <div class="large-title-wrap">
          <img src="/icon.png?v=2.8.7" class="nav-app-icon" id="navAppIcon" alt="" title="暗号表助手" />
          <h1 class="large-title">暗号表助手</h1>
          <span class="ver-pill" id="verBadge">v2.8.7</span>
          <span class="ver-pill" id="hdrArchivePill" style="background:rgba(40,205,65,0.14);color:var(--green);">数据档案 0 轮</span>
        </div>

        <div class="header-center-island">
          <div class="island" id="island">
            <div class="island-top">
              <div class="island-left">
                <div class="island-dot" id="islandDot"></div>
                <div class="ios-spinner" id="iosSpinner">
                  <div style="transform:rotate(0deg);animation-delay:-0.9s"></div>
                  <div style="transform:rotate(30deg);animation-delay:-0.825s"></div>
                  <div style="transform:rotate(60deg);animation-delay:-0.75s"></div>
                  <div style="transform:rotate(90deg);animation-delay:-0.675s"></div>
                  <div style="transform:rotate(120deg);animation-delay:-0.6s"></div>
                  <div style="transform:rotate(150deg);animation-delay:-0.525s"></div>
                  <div style="transform:rotate(180deg);animation-delay:-0.45s"></div>
                  <div style="transform:rotate(210deg);animation-delay:-0.375s"></div>
                  <div style="transform:rotate(240deg);animation-delay:-0.3s"></div>
                  <div style="transform:rotate(270deg);animation-delay:-0.225s"></div>
                  <div style="transform:rotate(300deg);animation-delay:-0.15s"></div>
                  <div style="transform:rotate(330deg);animation-delay:-0.075s"></div>
                </div>
                <div class="island-text" id="stepText">空闲中，等待开始</div>
              </div>
              <div class="island-pill-tag" id="islandTag">待命</div>
              <div class="island-ctrls">
                <button class="island-ctrl-btn island-pause" id="pauseBtn" type="button" title="暂停当前运行（快捷键 F8）">
                  <svg viewBox="0 0 16 16" fill="currentColor" style="width:9px;height:9px;"><rect x="3" y="3" width="3.5" height="10" rx="1"/><rect x="9.5" y="3" width="3.5" height="10" rx="1"/></svg>
                  <span>暂停 F8</span>
                </button>
                <button class="island-ctrl-btn island-resume" id="resumeBtn" type="button" title="继续未完成步骤（快捷键 F8）" style="display:none;">
                  <svg viewBox="0 0 16 16" fill="currentColor" style="width:9px;height:9px;"><path d="M4 2.8v10.4l9-5.2-9-5.2z"/></svg>
                  <span>继续 F8</span>
                </button>
                <button class="island-ctrl-btn island-stop" id="stopBtn" type="button" title="取消本轮任务">
                  <svg viewBox="0 0 16 16" fill="currentColor" style="width:9px;height:9px;"><rect x="3" y="3" width="10" height="10" rx="2"/></svg>
                  <span>取消</span>
                </button>
              </div>
            </div>
            <div class="island-prog">
              <div class="island-track"><div class="island-fill" id="progressFill"></div></div>
              <div class="island-pct" id="progressPct">0%</div>
            </div>
            <div class="island-hint" id="islandHint">按键提示：按【F8】或点【暂停】让出电脑，忙完按【F8】/【继续】；【取消】终止</div>
            <div class="island-recovery" id="islandRecovery">
              <div class="rec-title" id="recTitle"></div>
              <div class="rec-detail" id="recDetail"></div>
              <div class="rec-actions" id="recActions"></div>
            </div>
            <div class="island-alert" id="islandAlert"></div>
          </div>
        </div>

        <div class="nav-actions">
          <button class="member-chip-btn" id="memberProfileBtn" type="button" title="设置唯一昵称、头像及多人防撞联机（所有人名字不可重复）">
            <img class="member-mini-avatar" id="memberMiniAvatar" src="" alt="" />
            <span id="memberMiniName">设置昵称</span>
          </button>
          <button class="feedback-pill-btn" id="mobileBridgeBtn" type="button" title="查看全球远程访问网站、扫码连接与修改访问密码">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="2" width="12" height="20" rx="2.5"/><line x1="12" y1="18" x2="12" y2="18.01"/></svg>
            <span>远程网站</span>
          </button>
          <button class="feedback-pill-btn" id="feedbackBtn" type="button" title="提交使用问题或建议反馈">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
            <span>反馈</span>
          </button>
          <button class="nav-circle-btn" id="updateBtn" title="检查更新" style="display:none;">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12m0 0l-4-4m4 4l4-4"/><path d="M4 17v2a2 2 0 002 2h12a2 2 0 002-2v-2"/></svg>
            <span class="update-dot" id="updateDot"></span>
          </button>
          <button class="nav-circle-btn" id="themeBtn" title="切换外观"></button>
        </div>
      </div>
    </header>

    <div class="scroll-stage" id="scrollStage">
    <!-- 超级管理员预览他人页面横幅 -->
    <div class="top-notice-banner preview" id="adminPreviewBanner">
      <div class="notice-left">
        <img class="notice-avatar" id="previewMemberAvatar" src="" alt="" />
        <div>
          <strong id="previewMemberTitle">正在查看成员视图</strong>
          <div style="font-size:11.5px;color:var(--sub);" id="previewMemberSub">当前展示该同事最近一次上报的状态、日志与设置体检结果</div>
        </div>
      </div>
      <div class="notice-actions">
        <button class="mini-pill active" id="exitPreviewBtn" type="button">回到我自己的界面 / 超管后台</button>
      </div>
    </div>

    <!-- 多人运行防撞占用与自动排队提示横幅 -->
    <div class="top-notice-banner lock" id="teamLockBanner">
      <div class="notice-left">
        <img class="notice-avatar" id="lockHolderAvatar" src="" alt="" />
        <div>
          <strong id="lockHolderTitle">表格正在被同事使用中</strong>
          <div style="font-size:11.5px;color:var(--sub);" id="lockHolderSub">为避免多人同时操作 WPS 造成混乱，已自动开启互锁保护</div>
        </div>
      </div>
      <div class="notice-actions">
        <button class="mini-pill active" id="bannerJoinQueueBtn" type="button">排在TA后面自动跑</button>
        <button class="mini-pill" id="bannerLeaveQueueBtn" type="button" style="display:none;">取消排队</button>
      </div>
    </div>

    <!-- 自动粘贴/键鼠敏感期防误触强提示横幅 -->
    <div class="top-notice-banner hands-off" id="handsOffBanner">
      <div class="notice-left">
        <span class="ver-pill" style="background:rgba(255,159,10,0.28);color:#FF9500;font-weight:800;">勿动键鼠</span>
        <div>
          <strong id="handsOffTitle">正在自动粘贴数据，请勿操作鼠标或键盘！</strong>
          <div style="font-size:11.5px;color:var(--sub);">需急停请按键盘【F8】暂停或点顶部【取消】</div>
        </div>
      </div>
    </div>

    <!-- 结构化错误码 + 大白话修障卡片（仅遇错/待恢复时自动弹出） -->
    <div class="error-diag-card" id="errorDiagCard">
      <div class="diag-head">
        <div class="diag-code-wrap">
          <span class="diag-code-badge" id="diagCodeBadge">ERR-RUN-900</span>
          <span class="diag-title" id="diagTitle">运行遇到异常</span>
        </div>
        <button class="mini-pill" id="diagDismissBtn" type="button" style="color:var(--sub);">收起</button>
      </div>
      <div class="diag-cols">
        <div class="diag-box">
          <div class="diag-box-lbl">大白话原因</div>
          <div class="diag-box-txt" id="diagReason"></div>
        </div>
        <div class="diag-box">
          <div class="diag-box-lbl">您自己怎么改 / 怎么处理</div>
          <div class="diag-box-txt" id="diagSelfFix"></div>
        </div>
      </div>
      <div class="diag-btns">
        <button class="mini-pill active" id="autoHealNowBtn" type="button">AI 看图自动调试并续跑</button>
        <button class="mini-pill" id="applyAiTuneBtn" type="button" style="display:none;background:rgba(88,86,214,0.14);color:var(--indigo);">一键按 AI 建议自调顺序并重试</button>
        <button class="mini-pill" id="askAiErrorBtn" type="button">让 AI 对比历史深入复盘</button>
        <button class="mini-pill" id="copyTellDevBtn" type="button">一键复制发给 Antigravity 改代码指令</button>
      </div>
      <div class="diag-ai-panel" id="diagAiPanel"></div>
      <div class="ag-prompt-box" id="agPromptBox">
        <div class="ag-prompt-head">
          <span class="ag-prompt-title">发给 Antigravity 的沟通改代码指令（已结合历次成败日志自动整理）</span>
          <button class="mini-pill active" id="copyAgBoxBtn" type="button">复制指令</button>
        </div>
        <textarea class="ag-prompt-pre" id="agPromptPre" readonly></textarea>
      </div>
    </div>

    <main class="main-content">
      <!-- Tab 1: 控制台（顶部4列全景数据看板 · 左列执行与格位矩阵 · 右列全能AI管家） -->
      <section class="tab-pane active" id="pane-home">
        <div class="kpi-ribbon-4">
          <div class="kpi-card" id="kpiRegCard">
            <div class="mini-stat-top">
              <span class="mini-stat-title">数据概览</span>
              <div class="sf-badge sf-blue" style="width:22px;height:22px;border-radius:6px;">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:12px;height:12px;"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
              </div>
            </div>
            <div class="mini-stat-val" id="kpiRegVal">待抓取首轮数据</div>
            <div class="mini-stat-sub" id="kpiRegSub">运行全流程后自动记录历次档案</div>
          </div>

          <div class="kpi-card k-purple" id="kpiTopCard">
            <div class="mini-stat-top">
              <span class="mini-stat-title">团队排行</span>
              <div class="sf-badge sf-purple" style="width:22px;height:22px;border-radius:6px;">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:12px;height:12px;"><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 00-3-3.87"/><path d="M16 3.13a4 4 0 010 7.75"/></svg>
              </div>
            </div>
            <div class="mini-stat-val" id="kpiTopVal">暂无榜单记录</div>
            <div class="mini-stat-sub" id="kpiTopSub">支持在右侧直接追问任意成员数据</div>
          </div>

          <div class="kpi-card k-teal" id="sumPresetsRow">
            <div class="mini-stat-top">
              <span class="mini-stat-title">格位写入</span>
              <div class="sf-badge sf-teal" style="width:22px;height:22px;border-radius:6px;">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:12px;height:12px;"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="3" y1="9" x2="21" y2="9"/></svg>
              </div>
            </div>
            <div class="mini-stat-val" id="sumPresetsVal">已启用 5 项</div>
            <div class="mini-stat-sub" id="sumPresetsSub">D2/D4/E4/L3/L4</div>
          </div>

          <div class="kpi-card k-green" data-jump="schedule">
            <div class="mini-stat-top">
              <span class="mini-stat-title">排期计划</span>
              <div class="sf-badge sf-green" style="width:22px;height:22px;border-radius:6px;">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:12px;height:12px;"><rect x="3" y="4" width="18" height="18" rx="3"/><line x1="3" y1="10" x2="21" y2="10"/></svg>
              </div>
            </div>
            <div class="mini-stat-val" id="sumNextRun">—</div>
            <div class="mini-stat-sub" id="sumScheduleSub">每天运行</div>
          </div>
        </div>

        <div class="bento-grid home-split">
          <div class="bento-col">
            <button class="hero-action" id="btnNow">
              <div class="hero-copy">
                <div class="hero-title">立即开始全流程</div>
                <div class="hero-sub">下载三份数据 · 填入在线表 · 自定义写单元格 · 截图发钉钉</div>
              </div>
              <div class="hero-play">
                <svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5.14v14l11-7-11-7z"/></svg>
              </div>
            </button>

            <div class="tile-grid">
              <button class="cc-tile" id="btnCapture">
                <div class="tile-ico orange">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="3"/><circle cx="12" cy="12" r="3"/></svg>
                </div>
                <div>
                  <div class="tile-title">仅截图发送</div>
                  <div class="tile-sub">仅校准汇总表截图送达钉钉</div>
                </div>
              </button>

              <button class="cc-tile" id="btnSchedule">
                <div class="tile-ico green">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>
                </div>
                <div>
                  <div class="tile-title" id="schedTitle">自动值守</div>
                  <div class="tile-sub" id="schedSub">点击开启定时计划</div>
                </div>
              </button>
            </div>

            <div class="section-wrap">
              <div class="section-head"><span>投递概览</span><span>点击修改</span></div>
              <div class="stat-grid-2">
                <div class="mini-stat-card" data-jump="dingtalk">
                  <div class="mini-stat-top">
                    <span class="mini-stat-title">目标群聊</span>
                    <div class="sf-badge sf-indigo" style="width:22px;height:22px;border-radius:6px;">
                      <svg viewBox="0 0 24 24" fill="currentColor" style="width:12px;height:12px;"><path d="M6.0 1.9Q12.5 6.8 18.2 11.1C19.3 12.0 19.1 13.8 17.6 14.7L14.9 16.2L17.4 16.2L9.3 22.1L11.9 17.6L9.9 17.6L11.5 15.1C8.4 15.0 6.2 13.5 5.9 11.3C7.7 12.4 9.7 12.9 11.7 12.9C7.5 12.0 5.0 9.8 4.8 6.8C7.0 8.5 9.6 9.9 12.4 10.6C8.0 8.8 5.5 5.6 6.0 1.9Z"/></svg>
                    </div>
                  </div>
                  <div class="mini-stat-val" id="sumTarget">文件小助手</div>
                  <div class="mini-stat-sub">Ctrl+Shift+F 唤醒并进入</div>
                </div>

                <div class="mini-stat-card" data-jump="dingtalk">
                  <div class="mini-stat-top">
                    <span class="mini-stat-title">发送策略</span>
                    <div class="sf-badge sf-orange" style="width:22px;height:22px;border-radius:6px;">
                      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:12px;height:12px;"><path d="M13.73 21a2 2 0 01-3.46 0"/><path d="M18.63 13A17.89 17.89 0 0118 8"/><line x1="1" y1="1" x2="23" y2="23"/></svg>
                    </div>
                  </div>
                  <div class="mini-stat-val" id="sumPolicy">静音 · 暂不发</div>
                  <div class="mini-stat-sub" id="sumPolicySub">日期+时间 · 默认无后缀</div>
                </div>
              </div>
            </div>

            <div class="section-wrap">
              <div class="section-head">
                <span>格位规则</span>
                <button class="head-action-btn" id="homeAddCellBtn" type="button">+ 新增格位</button>
              </div>
              <div class="ios-group">
                <div class="home-cell-grid" id="homeCellChips"></div>
              </div>
            </div>

            <div class="section-wrap">
              <div class="section-head">
                <span>运行动态（上次：<span id="lastRun">—</span>）</span>
                <button class="head-action-btn" data-jump="more" type="button">全部日志</button>
              </div>
              <div class="ios-group">
                <div class="home-mini-log" id="homeMiniLogs">
                  <div style="color:var(--sub);text-align:center;padding:18px 0;">运行启动后，这里会实时滚播关键步骤与数据归档记录</div>
                </div>
              </div>
            </div>
          </div>

          <!-- 控制台右列：内置全能 AI 智能管家 & 历次数据综合分析工作台 -->
          <div class="bento-col">
            <div class="ai-chat-card">
              <div class="ai-card-hdr">
                <div class="ai-hdr-top">
                  <div class="ai-hdr-left">
                    <button class="cat-mascot-btn mood-happy" id="catMascotBtn" type="button" title="点我换一句元气工作对话，鼠标移动时眼睛也会看你哦">
                      <svg class="cat-mascot-svg" id="catMascotSvg" viewBox="0 0 48 48" fill="none">
                        <defs>
                          <linearGradient id="catShellGrad" x1="6" y1="8" x2="42" y2="44" gradientUnits="userSpaceOnUse">
                            <stop offset="0%" stop-color="#38BDF8"/>
                            <stop offset="55%" stop-color="#4F46E5"/>
                            <stop offset="100%" stop-color="#9333EA"/>
                          </linearGradient>
                          <linearGradient id="catVisorGrad" x1="11" y1="19" x2="37" y2="37" gradientUnits="userSpaceOnUse">
                            <stop offset="0%" stop-color="#0F172A"/>
                            <stop offset="100%" stop-color="#1E1B4B"/>
                          </linearGradient>
                        </defs>
                        <!-- 顶部天线 -->
                        <line x1="24" y1="11" x2="24" y2="5" stroke="#60A5FA" stroke-width="2.2" stroke-linecap="round"/>
                        <circle class="cat-antenna-dot" id="catAntennaTip" cx="24" cy="4.2" r="2.7" fill="#38BDF8"/>
                        <!-- 两侧猫耳 -->
                        <g class="cat-ear-l">
                          <path d="M10 19 L8 7 L19 14 Z" fill="url(#catShellGrad)" stroke="#BAE6FD" stroke-width="1.1" stroke-linejoin="round"/>
                          <path d="M11 16.5 L10.2 10.2 L16.2 14 Z" fill="#FDA4AF" opacity="0.88"/>
                        </g>
                        <g class="cat-ear-r">
                          <path d="M38 19 L40 7 L29 14 Z" fill="url(#catShellGrad)" stroke="#BAE6FD" stroke-width="1.1" stroke-linejoin="round"/>
                          <path d="M37 16.5 L37.8 10.2 L31.8 14 Z" fill="#FDA4AF" opacity="0.88"/>
                        </g>
                        <!-- 圆润机器人猫头 -->
                        <rect x="7.5" y="13.5" width="33" height="28.5" rx="13" fill="url(#catShellGrad)" stroke="#E0F2FE" stroke-width="1.2"/>
                        <!-- 面罩屏幕 -->
                        <rect x="11" y="18.5" width="26" height="19.5" rx="9" fill="url(#catVisorGrad)" stroke="#38BDF8" stroke-width="0.9" stroke-opacity="0.6"/>
                        <!-- 腮红 -->
                        <ellipse class="cat-cheek" cx="15.2" cy="30.6" rx="2.6" ry="1.4" fill="#FB7185" opacity="0.58"/>
                        <ellipse class="cat-cheek" cx="32.8" cy="30.6" rx="2.6" ry="1.4" fill="#FB7185" opacity="0.58"/>
                        <!-- 灵动双眼 + 随光标瞳孔 + 眨眼上睑 -->
                        <g id="catEyesGroup">
                          <circle cx="17.8" cy="26.2" r="3.2" fill="#38BDF8" opacity="0.95"/>
                          <circle id="catPupilL" cx="17.8" cy="26.2" r="1.5" fill="#F8FAFC"/>
                          <circle cx="18.7" cy="25.2" r="0.65" fill="#FFFFFF"/>
                          <rect class="cat-eyelid cat-eyelid-l" x="14.2" y="22.8" width="7.2" height="7" rx="3.4" fill="#1E1B4B"/>
                          <circle cx="30.2" cy="26.2" r="3.2" fill="#38BDF8" opacity="0.95"/>
                          <circle id="catPupilR" cx="30.2" cy="26.2" r="1.5" fill="#F8FAFC"/>
                          <circle cx="31.1" cy="25.2" r="0.65" fill="#FFFFFF"/>
                          <rect class="cat-eyelid cat-eyelid-r" x="26.6" y="22.8" width="7.2" height="7" rx="3.4" fill="#1E1B4B"/>
                        </g>
                        <!-- 多表情嘴巴路径 -->
                        <path id="catMouthPath" d="M20.5 31.8 Q22.2 33.8 24 31.8 Q25.8 33.8 27.5 31.8" stroke="#F8FAFC" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" fill="none"/>
                        <!-- 情绪闪烁小星芒 -->
                        <path class="cat-spark" d="M4.5 11 L5.4 8.5 L6.3 11 L8.8 11.9 L6.3 12.8 L5.4 15.3 L4.5 12.8 L2 11.9 Z" fill="#FACC15"/>
                        <circle class="cat-spark" cx="43.5" cy="14" r="1.8" fill="#34D399"/>
                      </svg>
                    </button>
                    <div class="row-main">
                      <div class="row-title">数据管家</div>
                      <div class="row-sub">Soren Drake 设计 · 会自动播报变动、查档案也能陪你聊天解闷</div>
                    </div>
                  </div>
                  <div style="display:flex;align-items:center;gap:5px;flex-shrink:0;">
                    <button class="mini-pill" id="aiSuggestBtn" type="button" style="white-space:nowrap;">数据自检</button>
                    <button class="mini-pill" id="aiClearChatBtn" type="button" style="color:var(--sub);white-space:nowrap;">清空</button>
                  </div>
                </div>
                <span class="ai-src-pill" id="aiShotStatusPill">数据就绪</span>
              </div>

              <div class="ai-greeting-banner" id="aiMascotGreetingBar">
                <span class="ai-greeting-badge" id="aiGreetingPeriodBadge">今日打卡</span>
                <div id="aiGreetingText" style="flex:1;min-width:0;"></div>
                <button type="button" class="mini-pill" id="aiGreetingNextBtn" style="flex-shrink:0;">换一句</button>
              </div>

              <div class="remote-guide-strip" id="remoteGuideStrip">
                <div style="flex:1;min-width:0;">
                  <strong>云端异地连电脑：</strong>无论身在何处，用手机或电脑打开 <code>https://sorenskr.github.io/SheetMate/</code>，填上您的名字和远程密码，就能异地对话查数据、遥控跑表。
                </div>
                <div style="display:flex;gap:5px;flex-shrink:0;">
                  <button type="button" class="mini-pill active" id="copyGithubGuideBtn">复制网址</button>
                  <button type="button" class="mini-pill" id="openMobileGuideModalBtn">扫码/改密码</button>
                </div>
              </div>

              <div class="ai-team-bar">
                <div class="ai-team-input-wrap">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>
                  <input type="text" id="aiFocusTeamInput" placeholder="想聚焦看的组名（留空=所有组公平对比）" />
                </div>
                <button type="button" class="dice-btn" id="aiDiceTeamBtn" title="随机筛选一个团队组名进行分析">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="4"/><circle cx="8.5" cy="8.5" r="1.1" fill="currentColor"/><circle cx="15.5" cy="8.5" r="1.1" fill="currentColor"/><circle cx="12" cy="12" r="1.1" fill="currentColor"/><circle cx="8.5" cy="15.5" r="1.1" fill="currentColor"/><circle cx="15.5" cy="15.5" r="1.1" fill="currentColor"/></svg>
                  <span>随机筛组</span>
                </button>
                <button type="button" class="mini-pill" id="aiClearTeamBtn" title="恢复所有团队公平汇报">全部组</button>
              </div>

              <div class="ai-style-bar" id="aiStyleChips"></div>
              <div class="ai-qa-bar" id="aiConfigTplBar" style="margin-bottom:4px;">
                <button class="ai-style-chip" type="button" data-ai-tpl="帮我设置每周一到周五 9点到18点每2小时把昨天数据静音自动发送到「目标群名」">模板：工作日定时发</button>
                <button class="ai-style-chip" type="button" data-ai-tpl="帮我设置每天上午 09:30 把昨天数据静音自动发送到「文件小助手」，粘贴等待 5 秒">模板：每天定点发昨天</button>
                <button class="ai-style-chip" type="button" data-ai-tpl="帮我设置每周一、周三、周五 10点到20点每2小时发送，L4单元格写入「核心组」，发到「数据日报群」">模板：自选星期+写格位</button>
                <button class="ai-style-chip" type="button" data-ai-tpl="帮我把钉钉目标群改成「运营群1、运营群2」，粘贴前等待 4 秒并开启静音发送">模板：改群名与等待秒</button>
              </div>
              <div class="ai-qa-bar" id="aiQuickQaBar">
                <button class="ai-style-chip accent" type="button" data-ai-q="你是谁？用的是什么AI，能帮我做什么？">你是谁/能干嘛</button>
                <button class="ai-style-chip accent" type="button" data-ai-q="这个软件是谁设计开发的？">是谁设计的</button>
                <button class="ai-style-chip accent" type="button" data-ai-q="请结合历史运行记录，分析最近一次为什么失败、和之前成功有什么不同，并给我一段直接发给 Antigravity 改代码和调整模块顺序的指令">复盘报错 &amp; 写 Antigravity 指令</button>
                <button class="ai-style-chip accent" type="button" data-ai-q="怎么自己定义在表格哪个单元格里写什么内容？">怎么自定义单元格</button>
                <button class="ai-style-chip accent" type="button" data-ai-q="汇报一下当前软件配置状态与最新数据概况">当前状态概览</button>
                <button class="ai-style-chip accent" type="button" data-ai-q="结合历史数据档案，对比各团队和前几名个人最近的数据表现">查团队/个人档案</button>
              </div>

              <div class="ai-chat-feed" id="aiChatFeed">
                <div class="ai-empty-guide" id="aiEmptyGuide">
                  我是「暗号表助手」内置智能助手（由 Soren Drake 设计）。您可以：<br/>
                  1. 点上方任意总结模板或快捷提问胶囊，也可点击左上角小猫头像让我陪你聊句元气工作鼓励；<br/>
                  2. 直接提问软件用法（如“怎么在 I4 格自动填入内容”）、当前状态或排障问题，每次跑完表我也会第一时间告诉你关键数据涨跌变化；<br/>
                  3. 点名追问任意团队或成员数据（或在上方输入/掷骰子选组名），我会自动结合历次归档档案公平分析。
                </div>
              </div>

              <div class="ai-input-bar">
                <input type="text" id="aiChatInput" placeholder="随时提问：你是谁 / 怎么自定义格位 / 某某某数据怎么样 / 总结今日表…" />
                <button class="ai-send-btn" id="aiSendBtn" type="button">发送</button>
              </div>
            </div>
          </div>
        </div>
      </section>

      <!-- Tab 2: 钉钉投递（左：会话与消息时间AI附发 · 右：汇总表预设与双列行为开关） -->
      <section class="tab-pane" id="pane-dingtalk">
        <div class="bento-grid">
          <div class="bento-col">
            <div class="section-wrap">
              <div class="section-head">
                <span>目标会话</span>
                <button class="head-action-btn" id="addTargetBtn" type="button">+ 添加会话</button>
              </div>
              <div class="ios-group">
                <div id="dtTargetsList"></div>
                <div class="ios-row clickable" id="checkDingBtn">
                  <div class="sf-badge sf-teal">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11"/></svg>
                  </div>
                  <div class="row-main">
                    <div class="row-title">软件自检</div>
                    <div class="row-sub">排查权限不一致、登录主窗与兜底模板</div>
                  </div>
                  <div class="row-right">
                    <span class="row-val" id="dingCheckVal">一键自检</span>
                    <svg class="chevron-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18l6-6-6-6"/></svg>
                  </div>
                </div>
              </div>
              <div class="section-footer" id="kwPreview">将依次按 Ctrl+Shift+F 打开已勾选会话并完成投递</div>
            </div>

            <div class="section-wrap">
              <div class="section-head">
                <span>消息格式</span>
              </div>
              <div class="ios-group">
                <div class="slot-block" style="padding:10px 13px;">
                  <div class="preset-sub-row">
                    <span class="row-title" style="font-size:13px;">消息附带时间方式</span>
                    <div class="mini-seg" id="dtTimeModeSeg">
                      <button type="button" class="mini-seg-btn" data-val="datetime">日期+时间</button>
                      <button type="button" class="mini-seg-btn" data-val="date_only">仅发日期</button>
                      <button type="button" class="mini-seg-btn" data-val="none">不加日期</button>
                    </div>
                  </div>
                  <div class="preset-sub-row">
                    <span class="row-title" style="font-size:13px;">消息日期显示样式</span>
                    <div class="mini-seg" id="dtDateFmtSeg">
                      <button type="button" class="mini-seg-btn" data-val="YYYY-MM-DD">横杠</button>
                      <button type="button" class="mini-seg-btn" data-val="YYYY年M月D日">年月日</button>
                      <button type="button" class="mini-seg-btn" data-val="M月D日">X月X日</button>
                    </div>
                  </div>
                  <div class="preset-sub-row">
                    <span class="row-title" style="font-size:13px;">发送日期基准</span>
                    <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">
                      <div class="mini-seg" id="dtOffsetQuickSeg">
                        <button type="button" class="mini-seg-btn" data-off="0">当天</button>
                        <button type="button" class="mini-seg-btn" data-off="-1">前1天</button>
                        <button type="button" class="mini-seg-btn" data-off="-2">前2天</button>
                      </div>
                      <div class="stepper-grp">
                        <button type="button" class="step-btn" id="dtOffMinusBtn">-</button>
                        <span class="step-val" id="dtOffVal">当天 (0)</span>
                        <button type="button" class="step-btn" id="dtOffPlusBtn">+</button>
                      </div>
                    </div>
                  </div>
                  <div class="cutoff-box">
                    <div class="cutoff-top">
                      <div class="row-main">
                        <div class="row-title" style="font-size:12.5px;">凌晨/某时间点前消息自动减天</div>
                        <div class="row-sub" style="font-size:11px;">开启后，早于分界点发送时消息日期自动按设定偏移</div>
                      </div>
                      <label class="ios-switch xs"><input type="checkbox" id="dtCutoffEnabled" /><span class="ios-slider"></span></label>
                    </div>
                    <div class="cutoff-controls" id="dtCutoffCtrlWrap" style="display:none;">
                      <span class="pill-sep">每天</span>
                      <button type="button" class="ios-pill-btn" id="dtCutoffHmBtn">09:00</button>
                      <span class="pill-sep">前按</span>
                      <div class="stepper-grp">
                        <button type="button" class="step-btn" id="dtCutOffMinus">-</button>
                        <span class="step-val" id="dtCutOffVal">前1天</span>
                        <button type="button" class="step-btn" id="dtCutOffPlus">+</button>
                      </div>
                    </div>
                  </div>
                </div>

                <div class="ios-row compact-sep">
                  <div class="row-main">
                    <div class="row-title">发图时自动附带 AI 数据简报</div>
                    <div class="row-sub">由内置智能伙伴看图提取极简重点文字一并粘贴发送</div>
                  </div>
                  <label class="ios-switch sm"><input type="checkbox" id="aiAttachSummary" /><span class="ios-slider"></span></label>
                </div>
                <div class="ios-row compact-sep" id="aiAttachStyleRow" style="display:none;">
                  <div class="row-main">
                    <div class="row-title" style="font-size:12.5px;">随图简报风格</div>
                    <div class="mini-seg" id="aiAttachStyleSeg" style="margin-top:4px;flex-wrap:wrap;"></div>
                  </div>
                </div>
              </div>
            </div>

            <div class="section-wrap">
              <div class="section-head">
                <span>时段后缀</span>
                <button class="head-action-btn" id="addSuffixRuleBtn" type="button">+ 添加时段</button>
              </div>
              <div class="ios-group">
                <div id="suffixRulesList"></div>
                <div class="ios-row">
                  <div class="sf-badge sf-indigo">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20.59 13.41l-7.17 7.17a2 2 0 01-2.83 0L2 12V2h10l8.59 8.59a2 2 0 010 2.82z"/><line x1="7" y1="7" x2="7.01" y2="7"/></svg>
                  </div>
                  <div class="row-title" style="flex-shrink:0;">默认后缀</div>
                  <div class="field-inline">
                    <input type="text" id="dtSuffix" placeholder="留空则其余时段不加后缀" />
                    <button class="clear-circle" id="clearSuffixBtn" title="清空默认后缀">
                      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><line x1="4" y1="4" x2="12" y2="12"/><line x1="12" y1="4" x2="4" y2="12"/></svg>
                    </button>
                  </div>
                </div>
              </div>
              <div class="section-footer" id="msgPreview">当前时刻预览：—</div>
            </div>
          </div>

          <div class="bento-col">
            <div class="section-wrap" id="summaryPresetsSection">
              <div class="section-head">
                <span>格位校准</span>
                <div class="tt-head-tools">
                  <button class="head-action-btn" id="resetPresetsBtn" type="button" style="color:var(--sub);">恢复默认</button>
                  <button class="head-action-btn" id="addPresetBtn" type="button">+ 新增位置</button>
                </div>
              </div>
              <div class="ios-group">
                <div class="ios-row compact-sep">
                  <div class="sf-badge sf-blue">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15.5 14"/></svg>
                  </div>
                  <div class="row-main">
                    <div class="row-title">粘贴完数据后写入更新时间</div>
                    <div class="row-sub">对应 P4 最后更新时间 · 在 S4 写入精确到秒时间</div>
                  </div>
                  <label class="ios-switch"><input type="checkbox" id="utEnabled" /><span class="ios-slider"></span></label>
                </div>
                <div class="ut-sub-wrap" id="utConfigBox">
                  <div class="cutoff-box">
                    <div class="ut-meta-row">
                      <div style="display:flex;align-items:center;gap:6px;">
                        <span class="pill-sep">格位</span>
                        <input type="text" class="cell-badge-input" id="utCellInput" value="S4" placeholder="S4" />
                      </div>
                      <div class="mini-seg" id="utTimingSeg">
                        <button type="button" class="mini-seg-btn" data-val="paste">粘贴完数据后写</button>
                        <button type="button" class="mini-seg-btn" data-val="always">每次截图都写</button>
                      </div>
                    </div>
                    <div class="preset-sub-hint" id="utPreviewText">写入示例：2026年11月21日13:55:01</div>
                  </div>
                </div>
                <div class="ios-row" style="border-top:0.5px solid var(--sep);">
                  <div class="sf-badge sf-teal">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="9" y1="9" x2="9" y2="21"/></svg>
                  </div>
                  <div class="row-main">
                    <div class="row-title">截图前自动写入任意单元格</div>
                    <div class="row-sub">支持指定任意工作表、格位/区间、自动日期、当前时间、公式或固定内容</div>
                  </div>
                  <label class="ios-switch"><input type="checkbox" id="spEnabled" /><span class="ios-slider"></span></label>
                </div>
                <div class="accordion-body tall" id="spAccordion">
                  <div class="quick-preset-bar" id="presetQuickAddBar">
                    <button type="button" class="ai-style-chip" data-qcell="F2" data-qact="value" data-qval="全部模版(不含CS)" data-qlabel="筛选模版">+ F2 筛选模版</button>
                    <button type="button" class="ai-style-chip" data-qcell="H4" data-qact="value" data-qval="空" data-qlabel="筛选短链">+ H4 筛选短链</button>
                    <button type="button" class="ai-style-chip" data-qcell="I4" data-qact="clear" data-qval="" data-qlabel="减去短链">+ I4 减去短链</button>
                    <button type="button" class="ai-style-chip" data-qcell="D4:E4" data-qact="clear" data-qval="" data-qlabel="清空范围">+ D4:E4 清空</button>
                    <button type="button" class="ai-style-chip accent" data-qcell="" data-qact="now" data-qval="" data-qlabel="当前时间">+ 当前时间格</button>
                    <button type="button" class="ai-style-chip accent" data-qcell="" data-qact="formula" data-qval="=COUNTA(D8:D100)" data-qlabel="统计公式">+ 公式格(=)</button>
                    <button type="button" class="ai-style-chip accent" data-qcell="" data-qact="value" data-qval="" data-qlabel="自定义内容">+ 空白自定义</button>
                  </div>
                  <div id="summaryPresetsList"></div>
                </div>
              </div>
              <div class="section-footer" id="spFooterTip">通过左上角名称框精准定位 D2 / D4 / E4 / L3 / L4 及 S4 更新时间（采用原生 Win32 Unicode 写入绝不乱码）</div>
            </div>

            <div class="section-wrap">
              <div class="section-head"><span>投递设置</span></div>
              <div class="ios-group">
                <div class="slot-block" style="padding:10px 13px;border-bottom:0.5px solid var(--sep);">
                  <div class="preset-sub-row">
                    <div class="row-main">
                      <div class="row-title" style="font-size:13px;">打开会话后等待几秒再粘贴</div>
                      <div class="row-sub" style="font-size:11px;">钉钉刚唤醒或加载聊天记录慢时，等几秒再贴图防漏贴；没贴上还会自动补粘</div>
                    </div>
                    <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">
                      <div class="mini-seg" id="dtPasteDelayQuickSeg">
                        <button type="button" class="mini-seg-btn" data-sec="1">1秒</button>
                        <button type="button" class="mini-seg-btn" data-sec="3">3秒</button>
                        <button type="button" class="mini-seg-btn" data-sec="5">5秒</button>
                        <button type="button" class="mini-seg-btn" data-sec="8">8秒</button>
                      </div>
                      <div class="stepper-grp">
                        <button type="button" class="step-btn" id="dtPasteDelayMinus">-</button>
                        <span class="step-val" id="dtPasteDelayVal">3秒</span>
                        <button type="button" class="step-btn" id="dtPasteDelayPlus">+</button>
                      </div>
                    </div>
                  </div>
                </div>
                <div class="slot-block" style="padding:10px 13px;border-bottom:0.5px solid var(--sep);">
                  <div class="preset-sub-row">
                    <div class="row-main">
                      <div class="row-title" style="font-size:13px;">钉钉内部执行顺序（防卡顿自调）</div>
                      <div class="row-sub" style="font-size:11px;">若贴大图后老卡在静音，可切「防卡顿」顺序让文字先激活输入栏并设静音</div>
                    </div>
                  </div>
                  <div style="display:flex;flex-direction:column;gap:6px;margin-top:6px;">
                    <div class="preset-sub-row">
                      <span class="pill-sep">粘贴与静音先后</span>
                      <div class="mini-seg" id="dtComposeOrderSeg">
                        <button type="button" class="mini-seg-btn" data-val="image_text_silent">默认: 先贴图再设静音</button>
                        <button type="button" class="mini-seg-btn" data-val="text_silent_image">防卡顿: 先贴字设静音再贴图</button>
                      </div>
                    </div>
                    <div class="preset-sub-row">
                      <span class="pill-sep">静音菜单定位</span>
                      <div class="mini-seg" id="dtSilentOrderSeg">
                        <button type="button" class="mini-seg-btn" data-val="uia_first">优先控件树</button>
                        <button type="button" class="mini-seg-btn" data-val="arrow_img_then_uia">箭头识图+菜单混合</button>
                      </div>
                    </div>
                  </div>
                </div>
                <div class="switch-grid-2">
                  <div class="sg-cell">
                    <div class="sg-copy">
                      <span class="sg-title">复制汇总表图片</span>
                      <span class="sg-sub">选中 C6:AK 去白边</span>
                    </div>
                    <label class="ios-switch sm"><input type="checkbox" id="enableCapture" /><span class="ios-slider"></span></label>
                  </div>
                  <div class="sg-cell">
                    <div class="sg-copy">
                      <span class="sg-title">自动发送到钉钉</span>
                      <span class="sg-sub">唤醒并进入目标群</span>
                    </div>
                    <label class="ios-switch sm"><input type="checkbox" id="enableDingtalk" /><span class="ios-slider"></span></label>
                  </div>
                  <div class="sg-cell">
                    <div class="sg-copy">
                      <span class="sg-title">发送前清空输入框</span>
                      <span class="sg-sub">清掉旧草稿防错发</span>
                    </div>
                    <label class="ios-switch sm"><input type="checkbox" id="dtClearBeforeSend" /><span class="ios-slider"></span></label>
                  </div>
                  <div class="sg-cell">
                    <div class="sg-copy">
                      <span class="sg-title">设为静音发送</span>
                      <span class="sg-sub">无坐标精准点静音</span>
                    </div>
                    <label class="ios-switch sm"><input type="checkbox" id="dtSilentSend" /><span class="ios-slider"></span></label>
                  </div>
                  <div class="sg-cell">
                    <div class="sg-copy">
                      <span class="sg-title">粘贴后自动发出</span>
                      <span class="sg-sub">静音后等 1 秒 Enter</span>
                    </div>
                    <label class="ios-switch sm"><input type="checkbox" id="dtAutoSend" /><span class="ios-slider"></span></label>
                  </div>
                  <div class="sg-cell">
                    <div class="sg-copy">
                      <span class="sg-title">发送后最小化钉钉</span>
                      <span class="sg-sub">自动收起不遮挡</span>
                    </div>
                    <label class="ios-switch sm"><input type="checkbox" id="dtMinimizeAfterSend" /><span class="ios-slider"></span></label>
                  </div>
                  <div class="sg-cell full">
                    <div class="sg-copy">
                      <span class="sg-title">跑完后自动用浏览器/WPS打开表格</span>
                      <span class="sg-sub">默认关闭：防止自动值守频繁弹出网页干扰工作</span>
                    </div>
                    <label class="ios-switch sm"><input type="checkbox" id="openWpsAfterRun" /><span class="ios-slider"></span></label>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>

      <!-- Tab 3: 定时计划（左设周期时段 · 右看每日排期全览） -->
      <section class="tab-pane" id="pane-schedule">
        <div class="bento-grid">
          <div class="bento-col">
            <div class="section-wrap">
              <div class="section-head"><span>值守时段</span><span>点击调整</span></div>
              <div class="ios-group">
                <div class="slot-block">
                  <div class="slot-head">
                    <div class="slot-title-wrap">
                      <div class="sf-badge sf-blue">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="3"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>
                      </div>
                      <span class="row-title">运行周期</span>
                    </div>
                    <span class="row-sub" id="dayModeSummary">每天运行</span>
                  </div>
                  <div class="segmented segmented-4" id="dayModeSeg">
                    <div class="seg-thumb" id="dayModeThumb"></div>
                    <button class="seg-btn" data-val="daily">每天</button>
                    <button class="seg-btn" data-val="workday">工作日</button>
                    <button class="seg-btn" data-val="weekend">非工作日</button>
                    <button class="seg-btn" data-val="custom">自定义</button>
                  </div>
                </div>

                <div class="weekday-wrap" id="customDaysWrap">
                  <div class="weekday-grid" id="customDaysGrid">
                    <button type="button" class="wday-btn" data-day="0">一</button>
                    <button type="button" class="wday-btn" data-day="1">二</button>
                    <button type="button" class="wday-btn" data-day="2">三</button>
                    <button type="button" class="wday-btn" data-day="3">四</button>
                    <button type="button" class="wday-btn" data-day="4">五</button>
                    <button type="button" class="wday-btn" data-day="5">六</button>
                    <button type="button" class="wday-btn" data-day="6">日</button>
                  </div>
                  <div class="wday-hint" id="customDaysHint">请选择运行星期</div>
                </div>

                <div class="slot-block">
                  <div class="slot-head">
                    <div class="slot-title-wrap">
                      <div class="sf-badge sf-orange">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><line x1="12" y1="2" x2="12" y2="4"/><line x1="12" y1="20" x2="12" y2="22"/></svg>
                      </div>
                      <span class="row-title">白天时段</span>
                    </div>
                    <span class="row-sub" id="daySummary"></span>
                  </div>
                  <div class="slot-controls">
                    <div class="pill-pair">
                      <button class="ios-pill-btn" id="dayStartBtn">09:00</button>
                      <span class="pill-sep">至</span>
                      <button class="ios-pill-btn" id="dayEndBtn">18:00</button>
                    </div>
                    <button class="ios-pill-btn neutral" id="dayGapBtn">每 30 分钟</button>
                  </div>
                </div>

                <div class="slot-block">
                  <div class="slot-head">
                    <div class="slot-title-wrap">
                      <div class="sf-badge sf-indigo">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1111.21 3 7 7 0 0021 12.79z"/></svg>
                      </div>
                      <span class="row-title">晚上时段</span>
                    </div>
                    <label class="ios-switch sm"><input type="checkbox" id="nightEnabled" /><span class="ios-slider"></span></label>
                  </div>
                  <div class="slot-controls" id="nightBody">
                    <div class="pill-pair">
                      <button class="ios-pill-btn" id="nightStartBtn">18:00</button>
                      <span class="pill-sep">至</span>
                      <button class="ios-pill-btn" id="nightEndBtn">23:00</button>
                    </div>
                    <button class="ios-pill-btn neutral" id="nightGapBtn">每 30 分钟</button>
                  </div>
                </div>

                <div class="ios-row no-icon-sep">
                  <div class="sf-badge sf-green">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
                  </div>
                  <div class="row-main">
                    <div class="row-title">值守期间防休眠息屏</div>
                    <div class="row-sub">阻止电脑自动睡眠，避免夜间断连误抓</div>
                  </div>
                  <label class="ios-switch sm"><input type="checkbox" id="preventSleep" /><span class="ios-slider"></span></label>
                </div>
              </div>
              <div class="section-footer warn" id="overlapTip" style="display:none;">
                白天与晚上时段存在重叠，系统已合并去重生成准点排期。
              </div>
            </div>

            <div class="section-wrap">
              <div class="section-head"><span>发送时段</span></div>
              <div class="ios-group">
                <div class="ios-row">
                  <div class="sf-badge sf-red">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>
                  </div>
                  <div class="row-main">
                    <div class="row-title">仅允许时段截图发群</div>
                    <div class="row-sub">关闭时全天自动均发群，手动触发不受限</div>
                  </div>
                  <label class="ios-switch sm"><input type="checkbox" id="cpEnabled" /><span class="ios-slider"></span></label>
                </div>

                <div class="accordion-body" id="cpAccordion">
                  <div id="cpWindowsList"></div>
                  <button type="button" class="add-row-btn" id="addCpWinBtn">+ 添加允许截图发群时段</button>
                </div>
              </div>
            </div>
          </div>

          <div class="bento-col">
            <div class="section-wrap">
              <div class="section-head">
                <span>排期总览</span>
                <div class="tt-head-tools">
                  <button class="head-action-btn" id="resetAllPointsBtn" type="button" style="display:none;color:var(--red);">重置单点</button>
                  <span id="ttSummaryText">共 0 轮</span>
                </div>
              </div>
              <div class="ios-group" id="timetableList" style="max-height:540px;overflow-y:auto;"></div>
              <div class="section-footer">点击任意一行，可单独设置这个时间点「发不发钉钉」、「查哪天表格」和「群消息文字」</div>
            </div>
          </div>
        </div>
      </section>

      <!-- Tab 4: 记录账号（左：运行日志与操作指南 · 右：内置智能伙伴偏好 + 账号数据源 + 历史归档） -->
      <section class="tab-pane" id="pane-more">
        <div class="bento-grid">
          <div class="bento-col">
            <div class="section-wrap">
              <div class="section-head">
                <span>运行日志</span>
                <div class="log-tools">
                  <button class="mini-pill active" id="filterLogAllBtn" type="button">全部</button>
                  <button class="mini-pill" id="filterLogErrBtn" type="button">仅异常</button>
                  <button class="mini-pill active" id="autoScrollBtn" type="button">滚底</button>
                  <button class="mini-pill" id="copyLogBtn" type="button">复制</button>
                  <button class="mini-pill" id="clearLogBtn" type="button">清空</button>
                </div>
              </div>
              <div class="ios-group">
                <div class="log-box" id="logBox">
                  <div class="log-empty" id="logEmpty">暂无运行日志</div>
                </div>
              </div>
            </div>

            <div class="section-wrap">
              <div class="section-head"><span>按键排障</span><span>F8 / F9</span></div>
              <div class="ios-group">
                <div class="ios-row clickable" id="guideToggleRow">
                  <div class="sf-badge sf-blue">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M9.09 9a3 3 0 015.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
                  </div>
                  <div class="row-main">
                    <div class="row-title">异常自检与续跑</div>
                    <div class="row-sub" id="guideSummarySub">点击展开速查指南</div>
                  </div>
                  <div class="row-right">
                    <svg class="chevron-svg" id="guideChevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="transition:transform .25s;"><path d="M9 18l6-6-6-6"/></svg>
                  </div>
                </div>
                <div class="guide-body" id="guideBody">
                  <div class="guide-item">
                    <div class="guide-item-title">1. 全局快捷键暂停 / 继续未完成步骤</div>
                    <div class="guide-item-desc">运行中按【F8】或点灵动岛【暂停】，数据与截图保留；忙完按【F8】接着跑；手动打开群后按【F9】直接贴图发送。</div>
                  </div>
                  <div class="guide-item">
                    <div class="guide-item-title">2. 报错代码 ERR-XXX 怎么看与怎么改</div>
                    <div class="guide-item-desc">一旦失败，顶部红色诊断卡会亮出错误代码（如 ERR-VPN-102 没开VPN、ERR-DT-302 权限高低不一致、ERR-DT-304 钉钉焦点受阻），直接给出大白话自救办法与可一键复制发给开发者的修改指令。</div>
                  </div>
                  <div class="guide-item">
                    <div class="guide-item-title">3. 钉钉正在使用或关闭时自动暂停</div>
                    <div class="guide-item-desc">发钉钉途中如您切走或关了钉钉，软件会触发 ERR-DT-305 自动暂停并保留截图；按 F8 即继续未发完的群聊。</div>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <div class="bento-col">
            <div class="section-wrap">
              <div class="section-head">
                <span>智能设置</span>
              </div>
              <div class="ios-group">
                <div class="switch-grid-2">
                  <div class="sg-cell">
                    <div class="sg-copy">
                      <span class="sg-title">智能助手</span>
                      <span class="sg-sub">主动播报数据变化与陪伴问答</span>
                    </div>
                    <label class="ios-switch sm"><input type="checkbox" id="aiEnabled" /><span class="ios-slider"></span></label>
                  </div>
                  <div class="sg-cell">
                    <div class="sg-copy">
                      <span class="sg-title">报错自检</span>
                      <span class="sg-sub">失败后自动生成大白话方案</span>
                    </div>
                    <label class="ios-switch sm"><input type="checkbox" id="aiAutoDiagnose" /><span class="ios-slider"></span></label>
                  </div>
                  <div class="sg-cell" style="grid-column:1 / -1;">
                    <div class="sg-copy">
                      <span class="sg-title">自动修复</span>
                      <span class="sg-sub">卡住时先截屏看现场并自动调整窗口焦点/按钮续跑，自愈失败再提示人工</span>
                    </div>
                    <label class="ios-switch sm"><input type="checkbox" id="aiAutoHeal" /><span class="ios-slider"></span></label>
                  </div>
                </div>
                <div class="ios-row compact-sep">
                  <div class="row-main">
                    <div class="row-title" style="font-size:12.5px;">总结偏好</div>
                    <div class="rule-input-box" style="margin-top:5px;">
                      <input type="text" id="aiCustomPrompt" placeholder="例如：每次总结都重点对比各组完成率与增量榜单" />
                    </div>
                  </div>
                </div>
              </div>
            </div>

            <div class="section-wrap">
              <div class="section-head"><span>数据账号</span></div>
              <div class="ios-group">
                <div class="ios-row">
                  <div class="sf-badge sf-blue">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 007.54.54l3-3a5 5 0 00-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 00-7.54-.54l-3 3a5 5 0 007.07 7.07l1.71-1.71"/></svg>
                  </div>
                  <div class="row-title" style="flex-shrink:0;">WPS 链接</div>
                  <div class="field-inline">
                    <input type="text" id="wpsUrl" placeholder="在线表格网址" />
                  </div>
                </div>
                <div class="form-2col" style="position:relative;">
                  <div class="form-col-box">
                    <span class="form-col-lbl">Luya 后台账号</span>
                    <div class="form-col-inrow">
                      <input type="text" id="luyaUser" placeholder="账号" />
                    </div>
                  </div>
                  <div class="form-col-box">
                    <span class="form-col-lbl">Luya 后台密码</span>
                    <div class="form-col-inrow">
                      <input type="password" id="luyaPass" placeholder="密码" />
                      <button class="mini-pill" id="togglePassBtn" type="button" style="display:inline-flex;align-items:center;padding:3px 6px;" title="显示或隐藏密码"></button>
                    </div>
                  </div>
                </div>
              </div>
            </div>

            <div class="section-wrap">
              <div class="section-head"><span>截图归档</span><span>最多留存 20 张</span></div>
              <div class="ios-group">
                <div class="ios-row">
                  <div class="sf-badge sf-indigo">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/></svg>
                  </div>
                  <div class="row-main">
                    <div class="row-title">自动归档</div>
                    <div class="row-sub">供随时回看及 AI 上下轮增量对比</div>
                  </div>
                  <label class="ios-switch sm"><input type="checkbox" id="saveShotHistory" /><span class="ios-slider"></span></label>
                </div>

                <div class="ios-row clickable" id="openShotsBtn">
                  <div class="sf-badge sf-orange">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 01-2 2H4a2 2 0 01-2-2V5a2 2 0 012-2h5l2 3h9a2 2 0 012 2z"/></svg>
                  </div>
                  <div class="row-main">
                    <div class="row-title">打开历史截图文件夹</div>
                    <div class="row-sub">查看历次保存的汇总表高清图片</div>
                  </div>
                  <div class="row-right">
                    <svg class="chevron-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18l6-6-6-6"/></svg>
                  </div>
                </div>
              </div>
            </div>

            <button class="danger-card-btn" id="resetBtn" type="button">恢复默认设置</button>
          </div>
        </div>
      </section>
    </main>
    </div>
  </div>

  <!-- 底部悬浮圆角毛玻璃胶囊导航栏 -->
  <nav class="tabbar-wrap">
    <div class="tabbar" id="tabbar">
      <div class="tab-pill-bg" id="tabPillBg"></div>
      <button class="tab-btn active" data-tab="home">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
        <span>控制台</span>
      </button>
      <button class="tab-btn" data-tab="dingtalk">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round"><path d="M6.0 1.9Q12.5 6.8 18.2 11.1C19.3 12.0 19.1 13.8 17.6 14.7L14.9 16.2L17.4 16.2L9.3 22.1L11.9 17.6L9.9 17.6L11.5 15.1C8.4 15.0 6.2 13.5 5.9 11.3C7.7 12.4 9.7 12.9 11.7 12.9C7.5 12.0 5.0 9.8 4.8 6.8C7.0 8.5 9.6 9.9 12.4 10.6C8.0 8.8 5.5 5.6 6.0 1.9Z"/></svg>
        <span>钉钉投递</span>
      </button>
      <button class="tab-btn" data-tab="schedule">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>
        <span>定时计划</span>
      </button>
      <button class="tab-btn" data-tab="more">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="8" y1="13" x2="16" y2="13"/><line x1="8" y1="17" x2="13" y2="17"/></svg>
        <span>记录账号</span>
      </button>
    </div>
  </nav>

  <!-- 毛玻璃滚轮时间选择器 -->
  <div class="sheet-mask" id="pickerMask">
    <div class="ios-sheet">
      <div class="sheet-handle"></div>
      <div class="sheet-header">
        <button class="sheet-btn" id="pickerCancel">取消</button>
        <div class="sheet-title" id="pickerTitle">选择时间</div>
        <button class="sheet-btn done" id="pickerDone">完成</button>
      </div>
      <div class="sheet-readout" id="pickerReadout">09:00</div>
      <div class="preset-row" id="pickerPresets"></div>
      <div class="wheel-grid">
        <div>
          <div class="wheel-col-label" id="wheelLeftLabel">小时</div>
          <div class="wheel-list" id="wheelLeft"></div>
        </div>
        <div>
          <div class="wheel-col-label" id="wheelRightLabel">分钟</div>
          <div class="wheel-list" id="wheelRight"></div>
        </div>
      </div>
    </div>
  </div>

  <!-- 大白话单点时间设置弹窗（与排期列表 0ms 实时联动） -->
  <div class="sheet-mask" id="pointMask">
    <div class="ios-sheet wide">
      <div class="sheet-handle"></div>
      <div class="sheet-header">
        <button class="sheet-btn" id="pointCancel" type="button">取消</button>
        <div class="sheet-title" id="pointSheetTitle">12:00 时间点设置</div>
        <button class="sheet-btn done" id="pointDone" type="button">完成</button>
      </div>
      <div class="sheet-body-pad">
        <div class="point-preview-strip">
          <div>本轮动作：<strong id="pointActionPrevText">—</strong></div>
          <div>表格日期：<strong id="pointSheetPrevText">—</strong></div>
          <div id="pointMsgPrevRow">群消息词：<strong id="pointMsgPrevText">—</strong></div>
        </div>

        <div class="point-grid-2">
          <div>
            <div class="sheet-field-label">
              <span>是否发钉钉</span>
              <span id="pointModeDefaultSub" style="font-weight:400;margin-left:4px;font-size:11px;"></span>
            </div>
            <div class="segmented segmented-3" id="pointModeSeg">
              <div class="seg-thumb" id="pointModeThumb"></div>
              <button class="seg-btn" type="button" data-val="default">按默认</button>
              <button class="seg-btn" type="button" data-val="send">发钉钉</button>
              <button class="seg-btn" type="button" data-val="table_only">不发钉钉</button>
            </div>
          </div>

          <div>
            <div class="sheet-field-label">表格查哪天数据</div>
            <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">
              <div class="mini-seg" id="pointSheetOffSeg">
                <button type="button" class="mini-seg-btn" data-val="default">按默认</button>
                <button type="button" class="mini-seg-btn" data-val="0">今天</button>
                <button type="button" class="mini-seg-btn" data-val="-1">昨天</button>
              </div>
              <div class="stepper-grp">
                <button type="button" class="step-btn" id="ptSheetMinusBtn">-</button>
                <span class="step-val" id="ptSheetOffVal">按默认</span>
                <button type="button" class="step-btn" id="ptSheetPlusBtn">+</button>
              </div>
            </div>
          </div>
        </div>

        <div id="pointNoDingtalkTip" class="point-preview-strip" style="display:none;color:var(--sub);">当前时间点只更新在线表格，不会往钉钉发图或发消息。</div>

        <div id="pointDingtalkSettingsWrap" style="display:flex;flex-direction:column;gap:12px;">
          <div class="point-grid-2">
            <div>
              <div class="sheet-field-label">
                <span>发送提醒</span>
                <span id="pointSilentDefaultSub" style="font-weight:400;margin-left:4px;font-size:11px;"></span>
              </div>
              <div class="segmented segmented-3" id="pointSilentSeg">
                <div class="seg-thumb" id="pointSilentThumb"></div>
                <button class="seg-btn" type="button" data-val="default">按默认</button>
                <button class="seg-btn" type="button" data-val="on">静音发送</button>
                <button class="seg-btn" type="button" data-val="off">正常提醒</button>
              </div>
            </div>

            <div>
              <div class="sheet-field-label">群消息时间</div>
              <div class="mini-seg" id="pointMsgModeSeg" style="margin-bottom:6px;">
                <button type="button" class="mini-seg-btn" data-val="default">按默认</button>
                <button type="button" class="mini-seg-btn" data-val="datetime">日期+时间</button>
                <button type="button" class="mini-seg-btn" data-val="date_only">只发日期</button>
                <button type="button" class="mini-seg-btn" data-val="none">不加时间</button>
              </div>
              <div id="pointMsgDateRow" style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">
                <div class="mini-seg" id="pointMsgOffSeg">
                  <button type="button" class="mini-seg-btn" data-val="default">按默认</button>
                  <button type="button" class="mini-seg-btn" data-val="0">今天</button>
                  <button type="button" class="mini-seg-btn" data-val="-1">昨天</button>
                </div>
                <div class="stepper-grp">
                  <button type="button" class="step-btn" id="ptMsgMinusBtn">-</button>
                  <span class="step-val" id="ptMsgOffVal">按默认</span>
                  <button type="button" class="step-btn" id="ptMsgPlusBtn">+</button>
                </div>
              </div>
            </div>
          </div>

          <div class="ios-group" style="box-shadow:none;">
            <div class="ios-row" style="min-height:42px;">
              <div class="row-main">
                <div class="row-title">单独修改结尾词</div>
                <div class="row-sub" id="pointSufHint">不改时默认按全局设置；留空代表不加结尾词</div>
              </div>
              <label class="ios-switch sm"><input type="checkbox" id="pointCustomSufOn" /><span class="ios-slider"></span></label>
            </div>
            <div class="ios-row no-icon-sep" id="pointCustomSufWrap" style="display:none;min-height:42px;">
              <div class="rule-input-box" style="width:100%;">
                <input type="text" id="pointCustomSufInput" placeholder="输入结尾文字，留空代表不加结尾词" />
              </div>
            </div>
          </div>
        </div>

        <button class="danger-card-btn" id="pointResetBtn" type="button" style="box-shadow:none;">恢复默认设置</button>
      </div>
    </div>
  </div>

  <!-- 模态弹窗 -->
  <div class="modal-mask" id="modalMask">
    <div class="ios-alert">
      <div class="alert-body">
        <div class="alert-title" id="alertTitle">提示</div>
        <div class="alert-msg" id="alertMsg"></div>
      </div>
      <div class="alert-actions" id="alertActions">
        <button class="alert-btn" id="alertCancel">取消</button>
        <button class="alert-btn bold" id="alertOk">确定</button>
      </div>
    </div>
  </div>

  <!-- 0. 云端异地远程连接这台电脑 -->
  <div class="modal-mask" id="mobileBridgeModalMask">
    <div class="card-modal" style="width:min(680px,95vw);">
      <div class="modal-head">
        <div class="modal-title">云端远程</div>
        <button class="mini-pill" id="mobileModalCloseBtn" type="button">关闭</button>
      </div>
      <div class="modal-scroll">
        <div style="font-size:12.5px;color:var(--sub);line-height:1.55;background:var(--fill-2);border:0.5px solid var(--sep);border-radius:12px;padding:9px 11px;">
          <strong style="color:var(--text);">大白话使用说明：</strong>
          无论身在外地还是路上，用手机扫左边二维码（或直接打开右边网址），填上您在右上角起的<strong>名字</strong>和下方设置的<strong>远程密码</strong>，就能用手机通过云端跟这台电脑聊天查数据、遥控跑表发钉钉，两边的消息实时同步。
        </div>
        <div style="display:grid;grid-template-columns:196px 1fr;gap:14px;align-items:start;">
          <div style="background:var(--fill-2);border:0.5px solid var(--sep);border-radius:16px;padding:10px;display:flex;flex-direction:column;align-items:center;gap:6px;">
            <div id="mobileQrBox" style="width:174px;height:174px;display:flex;align-items:center;justify-content:center;border-radius:10px;overflow:hidden;background:#FFFFFF;"></div>
            <div style="font-size:11px;color:var(--sub);text-align:center;" id="mobileQrCaption">手机微信或相机扫码就能打开</div>
          </div>
          <div style="display:flex;flex-direction:column;gap:10px;">
            <div style="background:var(--fill-2);border:0.5px solid var(--sep);border-radius:13px;padding:10px;display:flex;flex-direction:column;gap:6px;">
              <div style="display:flex;justify-content:space-between;align-items:center;gap:6px;">
                <span style="font-size:12px;font-weight:700;">1. 云端异地访问网址（在任何地方都能打开）</span>
                <span class="ver-pill" id="cloudRelayStatusBadge" style="background:rgba(40,205,65,0.14);color:var(--green);">已连上云端</span>
              </div>
              <div style="display:flex;gap:6px;align-items:center;">
                <input type="text" id="mobileGithubUrlInput" class="small-input" style="flex:1;" readonly value="https://sorenskr.github.io/SheetMate/" />
                <button type="button" class="mini-pill active" id="copyMobileGithubBtn">复制</button>
                <button type="button" class="mini-pill" id="openMobileGithubBtn">打开</button>
              </div>
              <div style="font-size:11px;color:var(--sub);" id="mobileCloudRelayTip">电脑连着网就一直在云端待命，外地打开网址就能直接连。</div>
            </div>
            <div style="background:var(--fill-2);border:0.5px solid var(--sep);border-radius:13px;padding:10px;display:flex;flex-direction:column;gap:6px;">
              <div style="font-size:12px;font-weight:700;">2. 远程密码（默认是 admin，大家可随时自己改）</div>
              <div style="display:flex;gap:6px;align-items:center;">
                <input type="password" id="mobilePinInput" class="small-input" style="flex:1;" placeholder="输入自己想设的新远程密码" />
                <button type="button" class="eye-btn" id="toggleMobilePinEyeBtn" title="显示或隐藏密码" style="flex-shrink:0;">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:15px;height:15px;"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
                </button>
                <button type="button" class="pill-action-btn primary" id="saveMobilePinBtn" style="padding:5px 12px;font-size:12px;">保存新密码</button>
              </div>
              <div style="font-size:11px;color:var(--sub);" id="mobilePinHint">为了安全，不管您自己还是别人从网站连这台电脑，都要输对这里设置的密码。</div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- 1. 昵称与头像设置弹窗 -->
  <div class="modal-mask" id="profileModalMask">
    <div class="card-modal">
      <div class="modal-head">
        <div class="modal-title">昵称头像</div>
        <button class="mini-pill" id="profileCloseBtn" type="button">关闭</button>
      </div>
      <div class="modal-scroll">
        <div style="font-size:12px;color:var(--sub);line-height:1.5;">
          给自己起个好记的名字并挑个头像吧，多人同时运行时用于避免表格撞车。若暂不想起名，也可以直接跳过。
        </div>
        <div style="display:flex;align-items:center;gap:12px;">
          <img id="profileAvatarPreview" src="" alt="" style="width:62px;height:62px;border-radius:50%;object-fit:cover;border:1.5px solid var(--sep);flex-shrink:0;" />
          <div style="flex:1;display:flex;flex-direction:column;gap:7px;">
            <div style="display:flex;gap:6px;align-items:center;">
              <input type="text" id="profileNicknameInput" class="small-input" style="flex:1;font-size:13px;padding:7px 10px;" placeholder="输入昵称（例如：张三 / 小王 / XX组-李四）" />
              <button type="button" class="dice-btn" id="profileDiceBtn" title="随机推荐好听昵称">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="4"/><circle cx="8.5" cy="8.5" r="1.1" fill="currentColor"/><circle cx="15.5" cy="8.5" r="1.1" fill="currentColor"/><circle cx="12" cy="12" r="1.1" fill="currentColor"/><circle cx="8.5" cy="15.5" r="1.1" fill="currentColor"/><circle cx="15.5" cy="15.5" r="1.1" fill="currentColor"/></svg>
                <span>随机名字</span>
              </button>
            </div>
            <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
              <button type="button" class="mini-pill" id="profileSkipNameBtn">暂不起名（跳过设置）</button>
              <label class="mini-pill active" style="cursor:pointer;">
                <span>上传本地照片</span>
                <input type="file" id="profileAvatarFileInput" accept="image/*" style="display:none;" />
              </label>
            </div>
          </div>
        </div>
        <div>
          <div style="font-size:12px;font-weight:700;color:var(--sub);margin-bottom:7px;">或直接挑一只可爱小动物头像：</div>
          <div class="avatar-preset-grid" id="avatarPresetsGrid"></div>
        </div>
        <div style="font-size:12px;line-height:1.5;color:var(--green);background:rgba(40,205,65,0.10);border:0.5px solid rgba(40,205,65,0.28);border-radius:12px;padding:9px 11px;">
          <strong>已开启云端异地自动协同：</strong>无论大家在哪个城市或哪里打开软件，都会通过云端自动防重名并在跑表时错峰排队，无需任何手工配置。
        </div>
      </div>
      <div class="modal-foot">
        <button type="button" class="pill-action-btn primary" id="profileSaveBtn">保存名字与头像</button>
      </div>
    </div>
  </div>

  <!-- 2. 多人撞车避让提示与自动排队弹窗 -->
  <div class="modal-mask" id="lockWaitModalMask">
    <div class="card-modal" style="width:min(440px,92vw);">
      <div class="modal-head">
        <div class="modal-title">排队提醒</div>
        <button class="mini-pill" id="lockWaitCloseBtn" type="button">关闭</button>
      </div>
      <div class="modal-scroll">
        <div style="display:flex;align-items:center;gap:12px;padding:12px;border-radius:14px;background:rgba(255,149,0,0.12);border:0.5px solid rgba(255,149,0,0.32);">
          <img id="lockModalAvatar" src="" alt="" style="width:48px;height:48px;border-radius:50%;object-fit:cover;flex-shrink:0;" />
          <div>
            <div style="font-size:14.5px;font-weight:800;" id="lockModalHolderName">同事正在运行</div>
            <div style="font-size:12px;color:var(--sub);margin-top:3px;" id="lockModalHolderStep">正在处理表格，请勿同时修改 WPS</div>
          </div>
        </div>
        <div style="font-size:12.5px;color:var(--text-2);line-height:1.6;">
          为了防止多人同时写入在线表格导致数据覆盖混乱，系统已开启互斥锁保护。<br />
          您可以点击下方「排在TA后面自动跑」，轮到您时软件会自动接棒开跑。
        </div>
      </div>
      <div class="modal-foot">
        <button type="button" class="pill-action-btn" id="lockWaitCancelBtn">暂不执行</button>
        <button type="button" class="pill-action-btn primary" id="lockWaitQueueBtn">排在TA后面自动跑</button>
      </div>
    </div>
  </div>

  <!-- 3. 问题与建议反馈弹窗 -->
  <div class="modal-mask" id="feedbackModalMask">
    <div class="card-modal">
      <div class="modal-head">
        <div class="modal-title">意见反馈</div>
        <button class="mini-pill" id="fbCloseBtn" type="button">关闭</button>
      </div>
      <div class="modal-scroll">
        <div style="font-size:12px;color:var(--sub);">发送时会自动附带当前表格/钉钉配置摘要和最近异常记录，方便更快为您定位调整。</div>
        <div class="mini-seg" id="fbCategorySeg">
          <button type="button" class="mini-seg-btn active" data-val="报错卡顿">报错/卡顿</button>
          <button type="button" class="mini-seg-btn" data-val="设置看不懂">设置不会配</button>
          <button type="button" class="mini-seg-btn" data-val="权限申请">申请开通/调整设置</button>
          <button type="button" class="mini-seg-btn" data-val="功能建议">功能建议</button>
        </div>
        <textarea class="field-textarea" id="fbContentInput" placeholder="用大白话描述遇到的问题，例如：点击发钉钉时停在了搜索框，或需要调整定时与格位配置…"></textarea>
      </div>
      <div class="modal-foot">
        <button type="button" class="pill-action-btn" id="fbCancelBtn">取消</button>
        <button type="button" class="pill-action-btn primary" id="fbSubmitBtn">立即提交反馈</button>
      </div>
    </div>
  </div>

  <!-- 4. 超级管理员口令 + 密码双验证弹窗 -->
  <div class="modal-mask" id="adminAuthMask">
    <div class="card-modal" style="width:min(380px,92vw);">
      <div class="modal-head">
        <div class="modal-title">超管验证</div>
        <button class="mini-pill" id="adminAuthCloseBtn" type="button">取消</button>
      </div>
      <div class="modal-scroll">
        <div style="font-size:12px;color:var(--sub);">仅限管理员本人进入，请验证身份信息。</div>
        <input type="password" id="adminPasscodeInput" class="small-input" style="padding:8px 11px;font-size:13px;" autocomplete="new-password" placeholder="请输入管理员姓名" />
        <input type="password" id="adminPasswordInput" class="small-input" style="padding:8px 11px;font-size:13px;" autocomplete="new-password" placeholder="请输入管理员密码" />
      </div>
      <div class="modal-foot">
        <button type="button" class="pill-action-btn primary" id="adminLoginBtn">解锁总管后台</button>
      </div>
    </div>
  </div>

  <!-- 5. 隐藏超级管理员 5 栏全能后台控制台 -->
  <div class="modal-mask" id="adminConsoleMask">
    <div class="card-modal wide">
      <div class="modal-head">
        <div style="display:flex;align-items:center;gap:10px;">
          <div class="modal-title">总控后台</div>
          <span class="ver-pill" style="background:rgba(88,86,214,0.16);color:var(--indigo);">全员监控 · 权限调控 · 统计审计</span>
        </div>
        <div style="display:flex;gap:7px;">
          <button class="mini-pill active" id="adminRefreshBtn" type="button">刷新数据</button>
          <button class="mini-pill" id="adminCloseBtn" type="button">收起后台</button>
        </div>
      </div>

      <div class="admin-nav-tabs" id="adminTabsBar">
        <button type="button" class="admin-tab-btn active" data-atab="stats">统计看板 · 防撞锁与联机</button>
        <button type="button" class="admin-tab-btn" data-atab="members">人员管理 · 权限与体检</button>
        <button type="button" class="admin-tab-btn" data-atab="errors">全员报错与 AI 聊天</button>
        <button type="button" class="admin-tab-btn" data-atab="feedbacks">成员反馈收件箱</button>
        <button type="button" class="admin-tab-btn" data-atab="logic">全局软件逻辑与密码</button>
      </div>

      <div class="modal-scroll" id="adminScrollBody">
        <!-- Tab 1: 统计 + 防撞锁 + 联机 -->
        <div id="adminPane-stats" class="admin-pane" style="display:flex;flex-direction:column;gap:12px;">
          <div class="admin-box">
            <div class="admin-box-title">
              <span>运行统计</span>
              <div class="filter-bar">
                <button type="button" class="mini-pill active" data-qrange="today">今天</button>
                <button type="button" class="mini-pill" data-qrange="yesterday">昨天</button>
                <button type="button" class="mini-pill" data-qrange="7d">近7天</button>
                <button type="button" class="mini-pill" data-qrange="all">全部</button>
              </div>
            </div>
            <div class="filter-bar">
              <span style="font-size:12px;color:var(--sub);">开始：</span>
              <input type="text" id="statStartInput" class="small-input" placeholder="2026-11-22 00:00" style="width:148px;" />
              <span style="font-size:12px;color:var(--sub);">结束：</span>
              <input type="text" id="statEndInput" class="small-input" placeholder="2026-11-22 23:59" style="width:148px;" />
              <span style="font-size:12px;color:var(--sub);">成员：</span>
              <div class="mini-seg" id="statMemberFilterSeg" style="max-width:340px;overflow-x:auto;"></div>
              <button type="button" class="pill-action-btn primary" id="statQueryBtn">按此时间统计</button>
            </div>
            <div class="admin-kpi-grid" id="adminKpiGrid"></div>
          </div>

          <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;">
            <div class="admin-box">
              <div class="admin-box-title">
                <span>排队状态</span>
                <button type="button" class="pill-action-btn danger" id="adminForceUnlockBtn">一键强制解锁并清队</button>
              </div>
              <div id="adminLockDetail" style="font-size:12.5px;color:var(--text-2);">当前空闲，无人占用表格。</div>
            </div>
            <div class="admin-box">
              <div class="admin-box-title">
                <span>云端协同</span>
                <button type="button" class="pill-action-btn ok" id="adminCloudSyncNowBtn">立即云端同步</button>
              </div>
              <div style="font-size:12.5px;line-height:1.55;color:var(--sub);" id="adminCloudStatusTip">
                已自动启用全云端异地实时协同：无论同事在哪个城市打开软件，都会通过云端自动汇集跑表统计、错误日志、用户反馈、防撞排队与权限指令，无需配置局域网或共享盘。
              </div>
            </div>
          </div>

          <div class="admin-box">
            <div class="admin-box-title"><span>运行流水</span></div>
            <div id="adminRunsList" style="display:flex;flex-direction:column;gap:7px;"></div>
          </div>
        </div>

        <!-- Tab 2: 成员列表 + 体检 + 7项权限 + 预览他人 -->
        <div id="adminPane-members" class="admin-pane" style="display:none;flex-direction:column;gap:10px;"></div>

        <!-- Tab 3: 同步报错 + AI 聊天记录 -->
        <div id="adminPane-errors" class="admin-pane" style="display:none;flex-direction:column;gap:12px;">
          <div class="admin-box">
            <div class="admin-box-title"><span>异常汇总</span></div>
            <div id="adminErrorsList" style="display:flex;flex-direction:column;gap:8px;"></div>
          </div>
          <div class="admin-box">
            <div class="admin-box-title"><span>对话记录</span></div>
            <div id="adminAiChatsList" style="display:flex;flex-direction:column;gap:8px;"></div>
          </div>
        </div>

        <!-- Tab 4: 反馈收件箱 -->
        <div id="adminPane-feedbacks" class="admin-pane" style="display:none;flex-direction:column;gap:8px;"></div>

        <!-- Tab 5: 全局软件设计逻辑与密码 -->
        <div id="adminPane-logic" class="admin-pane" style="display:none;flex-direction:column;gap:12px;">
          <div class="admin-box">
            <div class="admin-box-title"><span>全局逻辑</span></div>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;font-size:12.5px;">
              <div>
                <div style="color:var(--sub);margin-bottom:4px;">防撞锁最长自动释放时间（秒，30-1800）：</div>
                <input type="text" id="logicLockTimeoutInput" class="small-input" style="width:100%;" value="180" />
              </div>
              <div>
                <div style="color:var(--sub);margin-bottom:4px;">全员强制粘贴等待秒数（留空=尊重成员本地设置，填数字=全员统一）：</div>
                <input type="text" id="logicPasteDelayInput" class="small-input" style="width:100%;" placeholder="留空不强制，或填 3~15" />
              </div>
              <div>
                <div style="color:var(--sub);margin-bottom:4px;">全员统一发图与静音顺序：</div>
                <div class="mini-seg" id="logicComposeSeg">
                  <button type="button" class="mini-seg-btn active" data-val="">各成员自选</button>
                  <button type="button" class="mini-seg-btn" data-val="image_text_silent">贴图→字→静音</button>
                  <button type="button" class="mini-seg-btn" data-val="text_silent_image">字→静音→贴图</button>
                </div>
              </div>
              <div>
                <div style="color:var(--sub);margin-bottom:4px;">全员统一静音兜底顺序：</div>
                <div class="mini-seg" id="logicSilentSeg">
                  <button type="button" class="mini-seg-btn active" data-val="">各成员自选</button>
                  <button type="button" class="mini-seg-btn" data-val="uia_first">控件树优先</button>
                  <button type="button" class="mini-seg-btn" data-val="arrow_img_then_uia">先图像再控件树</button>
                </div>
              </div>
            </div>
            <div>
              <div style="color:var(--sub);font-size:12.5px;margin-bottom:4px;">发送完是否全员强制最小化钉钉：</div>
              <div class="mini-seg" id="logicMinimizeSeg">
                <button type="button" class="mini-seg-btn active" data-val="none">不强制</button>
                <button type="button" class="mini-seg-btn" data-val="true">全部自动最小化</button>
                <button type="button" class="mini-seg-btn" data-val="false">禁止最小化(防ERR-DT-305)</button>
              </div>
            </div>
            <div>
              <button type="button" class="pill-action-btn primary" id="adminSaveLogicBtn">保存并下发全员运行逻辑</button>
            </div>
          </div>

          <div class="admin-box">
            <div class="admin-box-title">
              <span>接口配置</span>
              <button type="button" class="pill-action-btn" id="adminTestGlobalAiBtn">测试接口连通</button>
            </div>
            <div style="display:flex;flex-direction:column;gap:8px;font-size:12.5px;">
              <div>
                <div style="color:var(--sub);margin-bottom:4px;">接口协议模式：</div>
                <div class="mini-seg" id="adminGlobalAiProviderSeg">
                  <button type="button" class="mini-seg-btn active" data-val="gemini">标准云端协议</button>
                  <button type="button" class="mini-seg-btn" data-val="openai_compat">兼容中转协议 (Chat Completions)</button>
                  <button type="button" class="mini-seg-btn" data-val="local_only">内置基础模式</button>
                </div>
              </div>
              <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;">
                <div>
                  <div style="color:var(--sub);margin-bottom:4px;">接口 Base URL（官方协议留空，兼容中转填如 https://api.xxx.com/v1）：</div>
                  <input type="text" id="adminGlobalAiBaseInput" class="small-input" style="width:100%;" placeholder="留空则走默认官服域名" />
                </div>
                <div>
                  <div style="color:var(--sub);margin-bottom:4px;">指定模型名称（留空则自动优选可用模型，前台对所有普通成员隐藏）：</div>
                  <input type="text" id="adminGlobalAiModelInput" class="small-input" style="width:100%;" placeholder="留空自动优选，如 gpt-4o-mini / custom-model" />
                </div>
              </div>
              <div style="display:flex;gap:8px;align-items:center;">
                <input type="password" id="adminGlobalAiKeyInput" class="small-input" style="flex:1;" placeholder="输入全员默认 API Key（留空表示保留内置密钥）" />
                <button type="button" class="mini-pill" id="adminGlobalAiKeyToggle">显示/隐藏密钥</button>
                <button type="button" class="pill-action-btn primary" id="adminSaveGlobalAiBtn">保存全员 AI 接口</button>
              </div>
            </div>
          </div>

          <div class="admin-box">
            <div class="admin-box-title"><span>密码设置</span></div>
            <div style="display:flex;gap:8px;flex-wrap:wrap;">
              <input type="password" id="adminNewPasscodeInput" class="small-input" autocomplete="new-password" placeholder="新管理员姓名" />
              <input type="password" id="adminNewPasswordInput" class="small-input" autocomplete="new-password" placeholder="新管理员密码" />
              <button type="button" class="pill-action-btn danger" id="adminSaveCredBtn">保存姓名与密码</button>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>

<script>
const TOKEN = __TOKEN_JSON__;
const $ = (id) => document.getElementById(id);

const SVG_MOON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1111.21 3 7 7 0 0021 12.79z"/></svg>`;
const SVG_SUN = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><line x1="12" y1="2" x2="12" y2="4"/><line x1="12" y1="20" x2="12" y2="22"/><line x1="4.93" y1="4.93" x2="6.34" y2="6.34"/><line x1="17.66" y1="17.66" x2="19.07" y2="19.07"/><line x1="2" y1="12" x2="4" y2="12"/><line x1="20" y1="12" x2="22" y2="12"/><line x1="4.93" y1="19.07" x2="6.34" y2="17.66"/><line x1="17.66" y1="6.34" x2="19.07" y2="4.93"/></svg>`;
const SVG_EYE = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:14px;height:14px;"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>`;
const SVG_EYE_OFF = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:14px;height:14px;"><path d="M17.94 17.94A10.07 10.07 0 0112 20c-7 0-11-8-11-8a18.45 18.45 0 015.06-5.94"/><path d="M9.9 4.24A9.12 9.12 0 0112 4c7 0 11 8 11 8a18.5 18.5 0 01-2.16 3.19"/><line x1="1" y1="1" x2="23" y2="23"/></svg>`;
const SVG_CLOSE = `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><line x1="4" y1="4" x2="12" y2="12"/><line x1="12" y1="4" x2="4" y2="12"/></svg>`;
const SVG_CHEVRON = `<svg class="chevron-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18l6-6-6-6"/></svg>`;

const WEEK_SHORT = ["一", "二", "三", "四", "五", "六", "日"];
const WEEK_FULL = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
const DEFAULT_SUMMARY_PRESETS = [
  { enabled: true, sheet: "数据汇总", cell: "D2", label: "查询日期", action: "today", date_fmt: "YYYY年M月D日", time_fmt: "full", day_offset: 0, cutoff_enabled: false, cutoff_hm: "09:00", before_cutoff_offset: -1, only_after_paste: false, value: "" },
  { enabled: true, sheet: "数据汇总", cell: "D4", label: "日期范围起始", action: "clear", date_fmt: "YYYY年M月D日", time_fmt: "full", day_offset: 0, cutoff_enabled: false, cutoff_hm: "09:00", before_cutoff_offset: -1, only_after_paste: false, value: "" },
  { enabled: true, sheet: "数据汇总", cell: "E4", label: "日期范围结束", action: "clear", date_fmt: "YYYY年M月D日", time_fmt: "full", day_offset: 0, cutoff_enabled: false, cutoff_hm: "09:00", before_cutoff_offset: -1, only_after_paste: false, value: "" },
  { enabled: true, sheet: "数据汇总", cell: "L3", label: "参与计算小组", action: "value", date_fmt: "YYYY年M月D日", time_fmt: "full", day_offset: 0, cutoff_enabled: false, cutoff_hm: "09:00", before_cutoff_offset: -1, only_after_paste: false, value: "宋磊组,娄慧珍组,江峰组,刘全泉组,潘立浩组,李俊鑫组,慕杰组" },
  { enabled: true, sheet: "数据汇总", cell: "L4", label: "需要标记的团队", action: "value", date_fmt: "YYYY年M月D日", time_fmt: "full", day_offset: 0, cutoff_enabled: false, cutoff_hm: "09:00", before_cutoff_offset: -1, only_after_paste: false, value: "慕杰组" },
];

let cfg = {};
let latestTimetable = [];
let aiStyles = [];
let autoScroll = true;
let logFilterErrOnly = false;
let allLogItems = [];
let lastLogSeq = 0;
let saveTimer = null;
let updateUrl = "";
let pickerState = null;
let editingPointHm = "";
let editingPointMode = "default";
let editingPointSilent = "default";
let editingPtSheetOff = null;
let editingPtMsgMode = "default";
let editingPtMsgOff = null;
let editingPointOrigOv = null;
let lastRecSignature = "";
let lastBusyState = false;
let lastNativeHotkeys = [];
let currentErrorDetail = null;
let diagDismissedSig = "";
let lastAiResultSig = "";
let aiCandidateTeams = [];
let memberProfile = null;
let avatarPresets = [];
let selectedAvatarUrl = "";
let syncSettingsState = {};
let adminToken = sessionStorage.getItem("sheetmate_admin_tok") || "";
let adminDashData = null;
let adminActiveTab = "stats";
let adminStatMemberId = "";
let previewMemberUid = "";
let selfPreviewBackup = null;
let pendingLockRunType = "full";
let iconClickTimes = [];
let feedbackCategory = "报错卡顿";

function cleanText(s) {
  return String(s || "").replace(/[\u2600-\u27BF\uFE0F\u200D]|[\uD83C-\uDBFF][\uDC00-\uDFFF]/g, "").trim();
}
function escapeHtmlStr(s) {
  return String(s || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
function safeAvatarUrl(rawUrl) {
  const s = String(rawUrl || "").trim();
  if (/^data:image\/(?:png|jpeg|jpg|webp|svg\+xml);base64,[A-Za-z0-9+/=\s]+$/i.test(s)) {
    return s;
  }
  return "";
}
function safeExternalHttpUrl(rawUrl) {
  const s = String(rawUrl || "").trim();
  return /^https?:\/\/[^\s"'<>]+$/i.test(s) ? s : "";
}
function formatSafeMarkdown(raw) {
  const safeLines = cleanText(raw).split(/\r?\n/);
  const out = [];
  for (const line of safeLines) {
    const tr = line.trim();
    if (tr === "---" || tr === "***") {
      out.push(`<div class="md-hr"></div>`);
      continue;
    }
    let esc = escapeHtmlStr(line).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    if (/^#{1,4}\s+/.test(tr)) {
      esc = esc.replace(/^\s*#{1,4}\s+/, "");
      out.push(`<div class="md-h3">${esc}</div>`);
    } else if (/^>\s?/.test(tr)) {
      esc = esc.replace(/^\s*&gt;\s?/, "");
      out.push(`<div class="md-quote">${esc}</div>`);
    } else {
      out.push(esc);
    }
  }
  return out.join("<br />");
}
function pad2(n) { return String(Math.max(0, Number(n) || 0)).padStart(2, "0"); }
function hmStr(h, m) { return `${pad2(h)}:${pad2(m)}`; }
function parseHmStr(s, defH = 9, defM = 0) {
  const parts = String(s || "").split(":");
  if (parts.length !== 2) return [defH, defM];
  return [Math.max(0, Math.min(23, parseInt(parts[0], 10) || 0)), Math.max(0, Math.min(59, parseInt(parts[1], 10) || 0))];
}
function clampOff(v, def = 0) {
  if (v === null || v === undefined || v === "") return def;
  const n = parseInt(v, 10);
  return Number.isNaN(n) ? def : Math.max(-60, Math.min(7, n));
}
function formatOffsetLabel(off) {
  const n = clampOff(off, 0);
  if (n === 0) return "今天";
  if (n === -1) return "昨天";
  if (n === -2) return "前天";
  if (n < -2) return `前${Math.abs(n)}天`;
  if (n === 1) return "明天";
  return `后${n}天`;
}
function formatShortMdLocal(dayOffset = 0) {
  const d = new Date();
  d.setDate(d.getDate() + clampOff(dayOffset, 0));
  return `${d.getMonth() + 1}月${d.getDate()}日`;
}
function formatGapText(ih, im) {
  const h = Number(ih) || 0;
  const m = Number(im) || 0;
  if (h > 0 && m > 0) return `每 ${h} 时 ${m} 分`;
  if (h > 0) return `每 ${h} 小时`;
  return `每 ${Math.max(1, m)} 分钟`;
}
function slotMinutes(s) { const [h, m] = parseHmStr(s, 0, 0); return h * 60 + m; }
function inTimeWindowLocal(curMin, startStr, endStr, inclusiveEnd = true) {
  const s = slotMinutes(startStr);
  const e = slotMinutes(endStr);
  if (s === e) return curMin === s;
  if (s < e) return inclusiveEnd ? (curMin >= s && curMin <= e) : (curMin >= s && curMin < e);
  return inclusiveEnd ? (curMin >= s || curMin <= e) : (curMin >= s || curMin < e);
}
function slotsOverlap(s1, e1, s2, e2) {
  const toSegs = (a, b) => a <= b ? [[a, b]] : [[a, 1440], [0, b]];
  for (const [a1, a2] of toSegs(slotMinutes(s1), slotMinutes(e1))) {
    for (const [b1, b2] of toSegs(slotMinutes(s2), slotMinutes(e2))) {
      if (Math.max(a1, b1) < Math.min(a2, b2)) return true;
    }
  }
  return false;
}
function isCaptureWindowAllowedLocal(hm) {
  const cp = cfg && cfg.capture_plan ? cfg.capture_plan : {};
  const enabled = (typeof $ === "function" && $("cpEnabled")) ? $("cpEnabled").checked : Boolean(cp.enabled);
  if (!enabled) return true;
  const wins = Array.isArray(cp.windows) ? cp.windows : [];
  const curMin = slotMinutes(hm);
  for (const w of wins) {
    if (w && w.enabled !== false && inTimeWindowLocal(curMin, w.start || "09:00", w.end || "18:00", true)) {
      return true;
    }
  }
  return false;
}
function resolvePointActionStateLocal(hm, modeOverride = "default") {
  const m = modeOverride || "default";
  const gCap = (typeof $ === "function" && $("enableCapture")) ? $("enableCapture").checked : (cfg.enable_capture !== false);
  const gDt = (typeof $ === "function" && $("enableDingtalk")) ? $("enableDingtalk").checked : Boolean(cfg.enable_dingtalk);
  const allowed = isCaptureWindowAllowedLocal(hm);
  const defaultCapture = Boolean(gCap && allowed);
  const defaultDingtalk = Boolean(gDt && allowed && defaultCapture);
  if (m === "send") return { doCapture: true, doDingtalk: true, defaultCapture, defaultDingtalk };
  if (m === "table_only") return { doCapture: false, doDingtalk: false, defaultCapture, defaultDingtalk };
  return { doCapture: defaultCapture, doDingtalk: defaultDingtalk, defaultCapture, defaultDingtalk };
}
function isRunDayAllowedLocal(c) {
  const mode = (c && c.day_mode) || "daily";
  const wday = (new Date().getDay() + 6) % 7;
  if (mode === "workday") return wday < 5;
  if (mode === "weekend") return wday >= 5;
  if (mode === "custom") {
    const days = Array.isArray(c.custom_days) ? c.custom_days : [0, 1, 2, 3, 4];
    return days.includes(wday);
  }
  return true;
}
function buildDayModeLabel(c) {
  const mode = (c && c.day_mode) || "daily";
  if (mode === "workday") return "工作日（周一至周五）";
  if (mode === "weekend") return "非工作日（周六至周日）";
  if (mode === "custom") {
    const days = Array.isArray(c.custom_days) ? c.custom_days : [];
    if (!days.length) return "未选任何星期";
    return "自定义（周" + days.map((d) => WEEK_SHORT[d] || "").join("、") + "）";
  }
  return "每天运行";
}
function formatShiftedDateLocal(fmt, dayOffset = 0) {
  const d = new Date();
  d.setDate(d.getDate() + clampOff(dayOffset, 0));
  const y = d.getFullYear();
  const m = d.getMonth() + 1;
  const day = d.getDate();
  if (fmt === "M月D日") return `${m}月${day}日`;
  if (fmt === "YYYY-MM-DD") return `${y}-${pad2(m)}-${pad2(day)}`;
  return `${y}年${m}月${day}日`;
}
function resolvePresetDayOffsetLocal(item, hm) {
  const ov = cfg.point_overrides && cfg.point_overrides[hm];
  if (ov && ov.sheet_day_offset !== null && ov.sheet_day_offset !== undefined) {
    return { off: clampOff(ov.sheet_day_offset, 0), src: "单点" };
  }
  if (item && item.cutoff_enabled) {
    const cutMin = slotMinutes(item.cutoff_hm || "09:00");
    if (slotMinutes(hm) < cutMin) {
      return { off: clampOff(item.before_cutoff_offset, -1), src: `${item.cutoff_hm || "09:00"}前` };
    }
  }
  return { off: clampOff(item && item.day_offset, 0), src: "默认" };
}
function resolveDingtalkTimeLocal(hm) {
  const ov = (cfg.point_overrides && cfg.point_overrides[hm]) || {};
  const tMode = ["datetime", "date_only", "none"].includes(ov.msg_time_mode)
    ? ov.msg_time_mode
    : (["datetime", "date_only", "none"].includes(cfg.dt_time_mode) ? cfg.dt_time_mode : "datetime");
  const dFmt = ["YYYY-MM-DD", "YYYY年M月D日", "M月D日"].includes(cfg.dt_date_fmt) ? cfg.dt_date_fmt : "YYYY-MM-DD";
  let dOff = clampOff(cfg.dt_day_offset, 0);
  let src = "全局";
  if (ov.msg_day_offset !== null && ov.msg_day_offset !== undefined) {
    dOff = clampOff(ov.msg_day_offset, 0);
    src = "单点";
  } else if (cfg.dt_cutoff_enabled) {
    const cutMin = slotMinutes(cfg.dt_cutoff_hm || "09:00");
    if (slotMinutes(hm) < cutMin) {
      dOff = clampOff(cfg.dt_before_cutoff_offset, -1);
      src = `${cfg.dt_cutoff_hm || "09:00"}前`;
    }
  }
  return { tMode, dFmt, dOff, src };
}
function resolveSuffixByRulesOnly(hm) {
  const curMin = slotMinutes(hm);
  const rules = Array.isArray(cfg.suffix_rules) ? cfg.suffix_rules : [];
  for (const r of rules) {
    if (r && r.enabled !== false && inTimeWindowLocal(curMin, r.start || "00:00", r.end || "00:00", true)) {
      return { suf: String(r.suffix || "").trim(), src: `时段 ${r.start}-${r.end}` };
    }
  }
  const defSuf = ($("dtSuffix").value || "").trim();
  return { suf: defSuf, src: defSuf ? "默认后缀" : "默认无后缀" };
}
function resolveSuffixLocal(hm) {
  const ov = cfg.point_overrides && cfg.point_overrides[hm];
  if (ov && ov.suffix !== undefined && ov.suffix !== null) {
    return { suf: String(ov.suffix).trim(), src: "单点覆盖" };
  }
  return resolveSuffixByRulesOnly(hm);
}
function buildMessagePreviewLocal(hm) {
  const tp = resolveDingtalkTimeLocal(hm);
  const sufRes = resolveSuffixLocal(hm);
  const datePart = formatShiftedDateLocal(tp.dFmt, tp.dOff);
  const stamp = tp.tMode === "datetime" ? `${datePart} ${hm}` : (tp.tMode === "date_only" ? datePart : "");
  let text = "";
  if (stamp && sufRes.suf) text = `${stamp} ${sufRes.suf}`;
  else if (stamp) text = stamp;
  else if (sufRes.suf) text = sufRes.suf;
  else text = datePart;
  return { text, ...tp, suf: sufRes.suf, sufSrc: sufRes.src };
}

function getEnabledTargetsLocal() {
  const list = Array.isArray(cfg.dt_targets) ? cfg.dt_targets : [];
  if (list.length > 0) {
    return list.filter((t) => t && t.enabled !== false && String(t.name || "").trim()).map((t) => String(t.name).trim());
  }
  const s = String(cfg.dt_search || "文件小助手").trim();
  return s ? [s] : ["文件小助手"];
}
function syncPrimaryDtSearch() {
  const list = Array.isArray(cfg.dt_targets) ? cfg.dt_targets : [];
  const enabled = list.find((t) => t && t.enabled !== false && String(t.name || "").trim());
  const firstNamed = enabled || list.find((t) => t && String(t.name || "").trim());
  cfg.dt_search = firstNamed ? String(firstNamed.name).trim() : (String(cfg.dt_search || "").trim() || "文件小助手");
}

function ensureSummaryPresetsStruct() {
  if (!cfg.summary_presets || typeof cfg.summary_presets !== "object") {
    cfg.summary_presets = { enabled: true, items: JSON.parse(JSON.stringify(DEFAULT_SUMMARY_PRESETS)) };
  }
  if (!Array.isArray(cfg.summary_presets.items)) {
    cfg.summary_presets.items = JSON.parse(JSON.stringify(DEFAULT_SUMMARY_PRESETS));
  }
  return cfg.summary_presets;
}
function ensureUpdateTimeStruct() {
  if (!cfg.update_time_cell || typeof cfg.update_time_cell !== "object") {
    cfg.update_time_cell = { enabled: true, cell: "S4", only_after_paste: true };
  }
  if (!String(cfg.update_time_cell.cell || "").trim()) cfg.update_time_cell.cell = "S4";
  if (cfg.update_time_cell.enabled === undefined) cfg.update_time_cell.enabled = true;
  if (cfg.update_time_cell.only_after_paste === undefined) cfg.update_time_cell.only_after_paste = true;
  return cfg.update_time_cell;
}
function formatNowFullTimestampLocal() {
  const d = new Date();
  return `${d.getFullYear()}年${pad2(d.getMonth() + 1)}月${pad2(d.getDate())}日${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
}
function renderUpdateTimeControls() {
  const ut = ensureUpdateTimeStruct();
  const enabled = ut.enabled !== false;
  const cell = String(ut.cell || "S4").trim().toUpperCase() || "S4";
  const pasteOnly = ut.only_after_paste !== false;
  $("utEnabled").checked = enabled;
  $("utConfigBox").style.display = enabled ? "block" : "none";
  $("utCellInput").value = cell;
  const modeVal = pasteOnly ? "paste" : "always";
  for (const b of $("utTimingSeg").querySelectorAll(".mini-seg-btn")) {
    b.classList.toggle("active", b.dataset.val === modeVal);
  }
  $("utPreviewText").textContent = `写入示例（写入 ${cell} 格 · ${pasteOnly ? "仅贴数后" : "每次截图"}）：${formatNowFullTimestampLocal()}`;
}
function ensureAiStruct() {
  if (!cfg.ai_assistant || typeof cfg.ai_assistant !== "object") {
    cfg.ai_assistant = { enabled: true, auto_diagnose: true, auto_heal: true, auto_attach_summary: false, attach_style: "brief", custom_summary_prompt: "", focus_team: "" };
  }
  if (cfg.ai_assistant.auto_heal === undefined) cfg.ai_assistant.auto_heal = true;
  if (cfg.ai_assistant.focus_team === undefined) cfg.ai_assistant.focus_team = "";
  return cfg.ai_assistant;
}

function renderDtTargets() {
  const wrap = $("dtTargetsList");
  wrap.innerHTML = "";
  if (!Array.isArray(cfg.dt_targets) || !cfg.dt_targets.length) {
    cfg.dt_targets = [{ name: String(cfg.dt_search || "文件小助手").trim() || "文件小助手", enabled: true }];
  }
  cfg.dt_targets.forEach((tgt, idx) => {
    const row = document.createElement("div");
    row.className = "target-item-row";
    const sw = document.createElement("label");
    sw.className = "ios-switch sm";
    const chk = document.createElement("input");
    chk.type = "checkbox";
    chk.checked = tgt.enabled !== false;
    chk.onchange = () => { cfg.dt_targets[idx].enabled = chk.checked; syncPrimaryDtSearch(); scheduleSave(true); };
    const sl = document.createElement("span");
    sl.className = "ios-slider";
    sw.appendChild(chk);
    sw.appendChild(sl);

    const inpWrap = document.createElement("div");
    inpWrap.className = "target-input-wrap";
    const inp = document.createElement("input");
    inp.type = "text";
    inp.placeholder = "输入完整群名或联系人名称";
    inp.value = tgt.name || "";
    inp.oninput = () => { cfg.dt_targets[idx].name = inp.value; syncPrimaryDtSearch(); scheduleSave(false); };
    inpWrap.appendChild(inp);

    row.appendChild(sw);
    row.appendChild(inpWrap);
    if (cfg.dt_targets.length > 1) {
      const del = document.createElement("button");
      del.type = "button";
      del.className = "clear-circle";
      del.title = "删除该会话";
      del.innerHTML = SVG_CLOSE;
      del.onclick = () => { cfg.dt_targets.splice(idx, 1); syncPrimaryDtSearch(); renderDtTargets(); scheduleSave(true); };
      row.appendChild(del);
    }
    wrap.appendChild(row);
  });
}

function renderSummaryPresets() {
  const sp = ensureSummaryPresetsStruct();
  const wrap = $("summaryPresetsList");
  wrap.innerHTML = "";
  const items = sp.items;
  if (!items.length) {
    const tip = document.createElement("div");
    tip.className = "empty-rule-tip";
    tip.textContent = "暂无预设单元格规则，点击右上角「+ 新增位置」或「恢复默认」添加";
    wrap.appendChild(tip);
    return;
  }

  const ACTION_OPTS = [
    { id: "today", label: "自动日期" },
    { id: "now", label: "当前时间" },
    { id: "value", label: "自定义值" },
    { id: "formula", label: "公式(=)" },
    { id: "clear", label: "清空" },
  ];
  const FMT_OPTS = [{ id: "YYYY年M月D日", label: "年月日" }, { id: "M月D日", label: "X月X日" }, { id: "YYYY-MM-DD", label: "横杠" }];
  const TFMT_OPTS = [{ id: "full", label: "年月日时分秒" }, { id: "hm", label: "日期+时分" }, { id: "time_only", label: "仅时分秒" }];

  items.forEach((item, idx) => {
    const row = document.createElement("div");
    row.className = "rule-card-row";

    const top = document.createElement("div");
    top.className = "rule-top";
    const leftGrp = document.createElement("div");
    leftGrp.style.cssText = "display:flex;align-items:center;gap:6px;min-width:0;flex:1;";

    const sheetInp = document.createElement("input");
    sheetInp.type = "text";
    sheetInp.className = "sheet-badge-input";
    sheetInp.title = "目标工作表名称（默认 数据汇总）";
    sheetInp.placeholder = "数据汇总";
    sheetInp.value = item.sheet || "数据汇总";
    sheetInp.oninput = () => { cfg.summary_presets.items[idx].sheet = sheetInp.value.trim() || "数据汇总"; scheduleSave(false); };

    const cellInp = document.createElement("input");
    cellInp.type = "text";
    cellInp.className = "cell-badge-input";
    cellInp.placeholder = "如D2";
    cellInp.value = item.cell || "";
    cellInp.oninput = () => { cfg.summary_presets.items[idx].cell = cellInp.value.replace(/\s+/g, "").toUpperCase(); scheduleSave(false); };
    cellInp.onblur = () => { cellInp.value = (cfg.summary_presets.items[idx].cell || "").toUpperCase(); };

    const lblInp = document.createElement("input");
    lblInp.type = "text";
    lblInp.className = "label-inline-input";
    lblInp.placeholder = "格位用途备注（如筛选模版）";
    lblInp.value = item.label || "";
    lblInp.oninput = () => { cfg.summary_presets.items[idx].label = lblInp.value.trim(); scheduleSave(false); };

    leftGrp.appendChild(sheetInp);
    leftGrp.appendChild(cellInp);
    leftGrp.appendChild(lblInp);

    const actions = document.createElement("div");
    actions.className = "rule-actions";
    const sw = document.createElement("label");
    sw.className = "ios-switch sm";
    const chk = document.createElement("input");
    chk.type = "checkbox";
    chk.checked = item.enabled !== false;
    chk.onchange = () => { cfg.summary_presets.items[idx].enabled = chk.checked; scheduleSave(true); };
    const sl = document.createElement("span");
    sl.className = "ios-slider";
    sw.appendChild(chk);
    sw.appendChild(sl);

    const del = document.createElement("button");
    del.type = "button";
    del.className = "clear-circle";
    del.title = "删除该位置";
    del.innerHTML = SVG_CLOSE;
    del.onclick = () => { cfg.summary_presets.items.splice(idx, 1); renderSummaryPresets(); scheduleSave(true); };

    actions.appendChild(sw);
    actions.appendChild(del);
    top.appendChild(leftGrp);
    top.appendChild(actions);
    row.appendChild(top);

    const actRow = document.createElement("div");
    actRow.className = "preset-sub-row";
    const actSeg = document.createElement("div");
    actSeg.className = "mini-seg";
    const curAct = ["today", "now", "value", "formula", "clear"].includes(item.action) ? item.action : "value";
    for (const opt of ACTION_OPTS) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "mini-seg-btn" + (curAct === opt.id ? " active" : "");
      b.textContent = opt.label;
      b.onclick = () => { cfg.summary_presets.items[idx].action = opt.id; renderSummaryPresets(); scheduleSave(true); };
      actSeg.appendChild(b);
    }

    const timingWrap = document.createElement("label");
    timingWrap.style.cssText = "display:inline-flex;align-items:center;gap:4px;font-size:11px;color:var(--sub);cursor:pointer;";
    const pasteChk = document.createElement("input");
    pasteChk.type = "checkbox";
    pasteChk.checked = Boolean(item.only_after_paste);
    pasteChk.onchange = () => { cfg.summary_presets.items[idx].only_after_paste = pasteChk.checked; scheduleSave(true); };
    const pasteTxt = document.createElement("span");
    pasteTxt.textContent = "仅贴数后写";
    timingWrap.appendChild(pasteChk);
    timingWrap.appendChild(pasteTxt);

    actRow.appendChild(actSeg);
    actRow.appendChild(timingWrap);
    row.appendChild(actRow);

    if (curAct === "today") {
      const curFmt = item.date_fmt || "YYYY年M月D日";
      const curOff = clampOff(item.day_offset, 0);
      const cutOn = Boolean(item.cutoff_enabled);
      const cutHm = item.cutoff_hm || "09:00";
      const cutOff = clampOff(item.before_cutoff_offset, -1);

      const subRow = document.createElement("div");
      subRow.className = "preset-sub-row";

      const fmtSeg = document.createElement("div");
      fmtSeg.className = "mini-seg";
      for (const fopt of FMT_OPTS) {
        const fb = document.createElement("button");
        fb.type = "button";
        fb.className = "mini-seg-btn" + (curFmt === fopt.id ? " active" : "");
        fb.textContent = fopt.label;
        fb.onclick = () => { cfg.summary_presets.items[idx].date_fmt = fopt.id; renderSummaryPresets(); scheduleSave(true); };
        fmtSeg.appendChild(fb);
      }

      const offGrp = document.createElement("div");
      offGrp.style.cssText = "display:flex;align-items:center;gap:5px;flex-wrap:wrap;";
      const qSeg = document.createElement("div");
      qSeg.className = "mini-seg";
      for (const q of [{ v: 0, l: "当天" }, { v: -1, l: "前1天" }, { v: -2, l: "前2天" }]) {
        const qb = document.createElement("button");
        qb.type = "button";
        qb.className = "mini-seg-btn" + (curOff === q.v ? " active" : "");
        qb.textContent = q.l;
        qb.onclick = () => { cfg.summary_presets.items[idx].day_offset = q.v; renderSummaryPresets(); scheduleSave(true); };
        qSeg.appendChild(qb);
      }
      const stp = document.createElement("div");
      stp.className = "stepper-grp";
      const mBtn = document.createElement("button");
      mBtn.type = "button";
      mBtn.className = "step-btn";
      mBtn.textContent = "-";
      mBtn.onclick = () => { cfg.summary_presets.items[idx].day_offset = clampOff(curOff - 1, 0); renderSummaryPresets(); scheduleSave(true); };
      const vSpan = document.createElement("span");
      vSpan.className = "step-val";
      vSpan.textContent = formatOffsetLabel(curOff);
      const pBtn = document.createElement("button");
      pBtn.type = "button";
      pBtn.className = "step-btn";
      pBtn.textContent = "+";
      pBtn.onclick = () => { cfg.summary_presets.items[idx].day_offset = clampOff(curOff + 1, 0); renderSummaryPresets(); scheduleSave(true); };
      stp.appendChild(mBtn);
      stp.appendChild(vSpan);
      stp.appendChild(pBtn);
      offGrp.appendChild(qSeg);
      offGrp.appendChild(stp);

      subRow.appendChild(fmtSeg);
      subRow.appendChild(offGrp);
      row.appendChild(subRow);

      // 凌晨跨天时间分界框
      const cutBox = document.createElement("div");
      cutBox.className = "cutoff-box";
      const cutTop = document.createElement("div");
      cutTop.className = "cutoff-top";
      const cutText = document.createElement("div");
      cutText.className = "preset-sub-hint";
      cutText.textContent = `跨天规则：每天 ${cutHm} 前自动按「${formatOffsetLabel(cutOff)}」写入表格`;
      const cutSw = document.createElement("label");
      cutSw.className = "ios-switch xs";
      const cutChk = document.createElement("input");
      cutChk.type = "checkbox";
      cutChk.checked = cutOn;
      cutChk.onchange = () => { cfg.summary_presets.items[idx].cutoff_enabled = cutChk.checked; renderSummaryPresets(); scheduleSave(true); };
      const cutSl = document.createElement("span");
      cutSl.className = "ios-slider";
      cutSw.appendChild(cutChk);
      cutSw.appendChild(cutSl);
      cutTop.appendChild(cutText);
      cutTop.appendChild(cutSw);
      cutBox.appendChild(cutTop);

      if (cutOn) {
        const cCtrl = document.createElement("div");
        cCtrl.className = "cutoff-controls";
        const sp1 = document.createElement("span");
        sp1.className = "pill-sep";
        sp1.textContent = "每天";
        const hmBtn = document.createElement("button");
        hmBtn.type = "button";
        hmBtn.className = "ios-pill-btn";
        hmBtn.textContent = cutHm;
        hmBtn.onclick = () => {
          const [h, m] = parseHmStr(cutHm, 9, 0);
          openPicker({
            title: "选择跨天日期分界时间点", mode: "time", h, m,
            onConfirm: (nh, nm) => { cfg.summary_presets.items[idx].cutoff_hm = hmStr(nh, nm); renderSummaryPresets(); scheduleSave(true); },
          });
        };
        const sp2 = document.createElement("span");
        sp2.className = "pill-sep";
        sp2.textContent = "前按";
        const cStp = document.createElement("div");
        cStp.className = "stepper-grp";
        const cm = document.createElement("button");
        cm.type = "button";
        cm.className = "step-btn";
        cm.textContent = "-";
        cm.onclick = () => { cfg.summary_presets.items[idx].before_cutoff_offset = clampOff(cutOff - 1, -1); renderSummaryPresets(); scheduleSave(true); };
        const cv = document.createElement("span");
        cv.className = "step-val";
        cv.textContent = formatOffsetLabel(cutOff);
        const cp = document.createElement("button");
        cp.type = "button";
        cp.className = "step-btn";
        cp.textContent = "+";
        cp.onclick = () => { cfg.summary_presets.items[idx].before_cutoff_offset = clampOff(cutOff + 1, -1); renderSummaryPresets(); scheduleSave(true); };
        cStp.appendChild(cm);
        cStp.appendChild(cv);
        cStp.appendChild(cp);
        cCtrl.appendChild(sp1);
        cCtrl.appendChild(hmBtn);
        cCtrl.appendChild(sp2);
        cCtrl.appendChild(cStp);
        cutBox.appendChild(cCtrl);
      }
      row.appendChild(cutBox);

      const nowObj = new Date();
      const nowHm = hmStr(nowObj.getHours(), nowObj.getMinutes());
      const curRes = resolvePresetDayOffsetLocal(cfg.summary_presets.items[idx], nowHm);
      const prevBar = document.createElement("div");
      prevBar.className = "preset-sub-hint";
      prevBar.textContent = `此刻(${nowHm})自动填入：${formatShiftedDateLocal(curFmt, curRes.off)}（来源：${curRes.src}）`;
      row.appendChild(prevBar);
    } else if (curAct === "now") {
      const curTFmt = ["full", "hm", "time_only"].includes(item.time_fmt) ? item.time_fmt : "full";
      const subRow = document.createElement("div");
      subRow.className = "preset-sub-row";
      const tSeg = document.createElement("div");
      tSeg.className = "mini-seg";
      for (const topt of TFMT_OPTS) {
        const tb = document.createElement("button");
        tb.type = "button";
        tb.className = "mini-seg-btn" + (curTFmt === topt.id ? " active" : "");
        tb.textContent = topt.label;
        tb.onclick = () => { cfg.summary_presets.items[idx].time_fmt = topt.id; renderSummaryPresets(); scheduleSave(true); };
        tSeg.appendChild(tb);
      }
      const dNow = new Date();
      const exStr = curTFmt === "hm"
        ? `${dNow.getFullYear()}-${pad2(dNow.getMonth() + 1)}-${pad2(dNow.getDate())} ${pad2(dNow.getHours())}:${pad2(dNow.getMinutes())}`
        : (curTFmt === "time_only"
          ? `${pad2(dNow.getHours())}:${pad2(dNow.getMinutes())}:${pad2(dNow.getSeconds())}`
          : formatNowFullTimestampLocal());
      const hint = document.createElement("div");
      hint.className = "preset-sub-hint";
      hint.textContent = `自动写入此刻精确时间（如 ${exStr}）`;
      subRow.appendChild(tSeg);
      row.appendChild(subRow);
      row.appendChild(hint);
    } else if (curAct === "clear") {
      const hint = document.createElement("div");
      hint.className = "preset-sub-hint";
      hint.textContent = (item.label ? `${item.label}：` : "") + "定位到该格后自动清空原有内容";
      row.appendChild(hint);
    } else if (curAct === "formula") {
      const inpBox = document.createElement("div");
      inpBox.className = "rule-input-box";
      const inp = document.createElement("input");
      inp.type = "text";
      inp.placeholder = "输入以 = 开头的公式（例如 =SUM(G8:G50)）";
      inp.value = item.value || "";
      inp.oninput = () => { cfg.summary_presets.items[idx].value = inp.value; scheduleSave(false); };
      inpBox.appendChild(inp);
      const hint = document.createElement("div");
      hint.className = "preset-sub-hint";
      hint.textContent = "通过公式编辑栏直接写入（不加单引号转文本），写完自动回车生效";
      row.appendChild(inpBox);
      row.appendChild(hint);
    } else {
      const inpBox = document.createElement("div");
      inpBox.className = "rule-input-box";
      const inp = document.createElement("input");
      inp.type = "text";
      inp.placeholder = "输入要写入的固定文字、组别名单或公式(=开头)";
      inp.value = item.value || "";
      inp.oninput = () => { cfg.summary_presets.items[idx].value = inp.value; scheduleSave(false); };
      inpBox.appendChild(inp);
      row.appendChild(inpBox);
    }
    wrap.appendChild(row);
  });
}

function renderAiControls() {
  const ai = ensureAiStruct();
  const focusTeam = String(ai.focus_team || "").trim();
  if ($("aiFocusTeamInput") && document.activeElement !== $("aiFocusTeamInput")) {
    $("aiFocusTeamInput").value = focusTeam;
  }
  const styles = aiStyles.length ? aiStyles : [
    { id: "brief", name: "群极简战报", desc: "80字内重点" },
    { id: "mujie", name: focusTeam ? `${focusTeam} 专项分析` : "各团队横向点评", desc: focusTeam ? `重点看 ${focusTeam}` : "所有团队平等对比" },
    { id: "funnel", name: "转化率排查", desc: "找卡点给建议" },
    { id: "delta", name: "较上轮对比", desc: "算增量榜单" },
    { id: "boss", name: "一分钟汇报", desc: "三段式汇总" },
  ];

  // Tab 1 快捷总结风格胶囊
  const chipsWrap = $("aiStyleChips");
  chipsWrap.innerHTML = "";
  for (const s of styles) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "ai-style-chip";
    b.title = s.desc || s.name;
    b.textContent = s.name;
    b.onclick = () => sendAiSummary(s.id, "");
    chipsWrap.appendChild(b);
  }

  // Tab 2 随图附发风格分段选择
  const attWrap = $("aiAttachStyleSeg");
  attWrap.innerHTML = "";
  const curAtt = ai.attach_style || "brief";
  for (const s of styles) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "mini-seg-btn" + (curAtt === s.id ? " active" : "");
    b.textContent = s.name;
    b.onclick = () => {
      ensureAiStruct().attach_style = s.id;
      renderAiControls();
      scheduleSave(true);
    };
    attWrap.appendChild(b);
  }

  $("aiAttachSummary").checked = Boolean(ai.auto_attach_summary);
  $("aiAttachStyleRow").style.display = ai.auto_attach_summary ? "flex" : "none";
  $("aiEnabled").checked = ai.enabled !== false;
  $("aiAutoDiagnose").checked = ai.auto_diagnose !== false;
  $("aiAutoHeal").checked = ai.auto_heal !== false;
  $("aiCustomPrompt").value = ai.custom_summary_prompt || "";
}

function renderDingtalkDatePanel() {
  const tMode = ["datetime", "date_only", "none"].includes(cfg.dt_time_mode) ? cfg.dt_time_mode : "datetime";
  for (const b of $("dtTimeModeSeg").querySelectorAll(".mini-seg-btn")) {
    b.classList.toggle("active", b.dataset.val === tMode);
  }
  const dFmt = ["YYYY-MM-DD", "YYYY年M月D日", "M月D日"].includes(cfg.dt_date_fmt) ? cfg.dt_date_fmt : "YYYY-MM-DD";
  for (const b of $("dtDateFmtSeg").querySelectorAll(".mini-seg-btn")) {
    b.classList.toggle("active", b.dataset.val === dFmt);
  }
  const dOff = clampOff(cfg.dt_day_offset, 0);
  for (const b of $("dtOffsetQuickSeg").querySelectorAll(".mini-seg-btn")) {
    b.classList.toggle("active", Number(b.dataset.off) === dOff);
  }
  $("dtOffVal").textContent = formatOffsetLabel(dOff);

  const cutOn = Boolean(cfg.dt_cutoff_enabled);
  $("dtCutoffEnabled").checked = cutOn;
  $("dtCutoffCtrlWrap").style.display = cutOn ? "flex" : "none";
  $("dtCutoffHmBtn").textContent = cfg.dt_cutoff_hm || "09:00";
  $("dtCutOffVal").textContent = formatOffsetLabel(clampOff(cfg.dt_before_cutoff_offset, -1));
}

function clampPasteDelay(val, fallback = 3) {
  const n = Number(val);
  if (!Number.isFinite(n)) return fallback;
  return Math.max(0, Math.min(20, Math.round(n)));
}

function renderDtPasteDelay() {
  const sec = clampPasteDelay(cfg.dt_paste_delay_sec, 3);
  cfg.dt_paste_delay_sec = sec;
  if ($("dtPasteDelayVal")) $("dtPasteDelayVal").textContent = `${sec}秒`;
  if ($("dtPasteDelayQuickSeg")) {
    for (const b of $("dtPasteDelayQuickSeg").querySelectorAll(".mini-seg-btn")) {
      b.classList.toggle("active", Number(b.dataset.sec) === sec);
    }
  }
}

function ensurePipelineTuningStruct() {
  if (!cfg.pipeline_tuning || typeof cfg.pipeline_tuning !== "object") {
    cfg.pipeline_tuning = {};
  }
  const pt = cfg.pipeline_tuning;
  if (!["image_text_silent", "text_silent_image"].includes(pt.compose_step_order)) pt.compose_step_order = "image_text_silent";
  if (!["uia_first", "arrow_img_then_uia"].includes(pt.silent_step_order)) pt.silent_step_order = "uia_first";
  if (!["pywinauto_first", "uia_first"].includes(pt.paste_key_order)) pt.paste_key_order = "pywinauto_first";
  const wv = Number(pt.wps_refresh_wait_sec);
  pt.wps_refresh_wait_sec = Number.isFinite(wv) ? Math.max(3, Math.min(15, Math.round(wv))) : 5;
  return pt;
}

function renderPipelineTuning() {
  const pt = ensurePipelineTuningStruct();
  if ($("dtComposeOrderSeg")) {
    for (const b of $("dtComposeOrderSeg").querySelectorAll(".mini-seg-btn")) {
      b.classList.toggle("active", b.dataset.val === pt.compose_step_order);
    }
  }
  if ($("dtSilentOrderSeg")) {
    for (const b of $("dtSilentOrderSeg").querySelectorAll(".mini-seg-btn")) {
      b.classList.toggle("active", b.dataset.val === pt.silent_step_order);
    }
  }
}

function showToast(msg) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove("show"), 1900);
}

async function api(path, method = "GET", body = null, extraHeaders = null) {
  const headers = { "X-Token": TOKEN, "Content-Type": "application/json" };
  if (extraHeaders && typeof extraHeaders === "object") {
    Object.assign(headers, extraHeaders);
  }
  const opts = { method, headers };
  if (body !== null) opts.body = JSON.stringify(body);
  const res = await fetch(path, opts);
  return await res.json();
}

function scrollToLogBottom(smooth = false) {
  const box = $("logBox");
  if (!box) return;
  const go = () => {
    const target = Math.max(0, box.scrollHeight - box.clientHeight);
    if (smooth && typeof box.scrollTo === "function") {
      box.scrollTo({ top: box.scrollHeight, behavior: "smooth" });
    } else {
      box.scrollTop = target;
    }
  };
  requestAnimationFrame(() => {
    go();
    setTimeout(() => {
      if (autoScroll && box.scrollTop + box.clientHeight < box.scrollHeight - 4) {
        box.scrollTop = box.scrollHeight;
      }
    }, smooth ? 180 : 30);
  });
}

const TAB_ORDER = ["home", "dingtalk", "schedule", "more"];
function switchTab(name) {
  const idx = Math.max(0, TAB_ORDER.indexOf(name));
  $("tabPillBg").style.transform = `translateX(${idx * 100}%)`;
  for (const btn of $("tabbar").querySelectorAll(".tab-btn")) {
    btn.classList.toggle("active", btn.dataset.tab === name);
  }
  for (const pane of document.querySelectorAll(".tab-pane")) {
    pane.classList.toggle("active", pane.id === `pane-${name}`);
  }
  const stg = $("scrollStage");
  if (stg) stg.scrollTop = 0;
  window.scrollTo({ top: 0, behavior: "instant" });
  if (name === "more" && autoScroll) {
    scrollToLogBottom(false);
  }
}

function iosAlert(title, msg, { okText = "好", cancelText = null, danger = false } = {}) {
  return new Promise((resolve) => {
    $("alertTitle").textContent = title;
    $("alertMsg").textContent = msg;
    const actions = $("alertActions");
    const cancelBtn = $("alertCancel");
    const okBtn = $("alertOk");
    okBtn.textContent = okText;
    okBtn.className = "alert-btn " + (danger ? "danger" : "");
    if (cancelText) {
      actions.classList.remove("single");
      cancelBtn.style.display = "block";
      cancelBtn.textContent = cancelText;
    } else {
      actions.classList.add("single");
      cancelBtn.style.display = "none";
    }
    const mask = $("modalMask");
    mask.classList.add("show");
    const done = (v) => { mask.classList.remove("show"); resolve(v); };
    okBtn.onclick = () => done(true);
    cancelBtn.onclick = () => done(false);
  });
}

function openPicker({ title, mode, h, m, onConfirm }) {
  pickerState = { mode, h: Number(h) || 0, m: Number(m) || 0, onConfirm };
  $("pickerTitle").textContent = title;
  $("wheelLeftLabel").textContent = mode === "gap" ? "间隔小时" : "时";
  $("wheelRightLabel").textContent = mode === "gap" ? "间隔分钟" : "分";
  const presets = mode === "gap"
    ? [{ label: "15分钟", h: 0, m: 15 }, { label: "30分钟", h: 0, m: 30 }, { label: "45分钟", h: 0, m: 45 }, { label: "1小时", h: 1, m: 0 }, { label: "2小时", h: 2, m: 0 }]
    : [{ label: "06:00", h: 6, m: 0 }, { label: "08:00", h: 8, m: 0 }, { label: "09:00", h: 9, m: 0 }, { label: "12:00", h: 12, m: 0 }, { label: "18:00", h: 18, m: 0 }, { label: "23:00", h: 23, m: 0 }];

  const maxH = mode === "gap" ? 12 : 23;
  const leftEl = $("wheelLeft");
  const rightEl = $("wheelRight");
  leftEl.innerHTML = "";
  rightEl.innerHTML = "";

  for (let i = 0; i <= maxH; i++) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "wheel-opt";
    b.dataset.v = String(i);
    b.textContent = mode === "gap" ? `${i} 时` : pad2(i);
    b.onclick = () => { pickerState.h = i; syncPickerSelection(); };
    leftEl.appendChild(b);
  }
  for (let i = 0; i < 60; i++) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "wheel-opt";
    b.dataset.v = String(i);
    b.textContent = mode === "gap" ? `${i} 分` : pad2(i);
    b.onclick = () => { pickerState.m = i; syncPickerSelection(); };
    rightEl.appendChild(b);
  }

  const presetWrap = $("pickerPresets");
  presetWrap.innerHTML = "";
  for (const p of presets) {
    const c = document.createElement("button");
    c.type = "button";
    c.className = "preset-chip";
    c.textContent = p.label;
    c.dataset.h = String(p.h);
    c.dataset.m = String(p.m);
    c.onclick = () => { pickerState.h = p.h; pickerState.m = p.m; syncPickerSelection(); };
    presetWrap.appendChild(c);
  }
  $("pickerMask").classList.add("show");
  syncPickerSelection(true);
}

function syncPickerSelection(scroll = false) {
  if (!pickerState) return;
  const { mode, h, m } = pickerState;
  $("pickerReadout").textContent = mode === "gap" ? formatGapText(h, m) : hmStr(h, m);
  for (const chip of $("pickerPresets").querySelectorAll(".preset-chip")) {
    chip.classList.toggle("active", Number(chip.dataset.h) === h && Number(chip.dataset.m) === m);
  }
  for (const opt of $("wheelLeft").querySelectorAll(".wheel-opt")) {
    const sel = Number(opt.dataset.v) === h;
    opt.classList.toggle("selected", sel);
    if (sel && scroll) opt.scrollIntoView({ block: "center" });
  }
  for (const opt of $("wheelRight").querySelectorAll(".wheel-opt")) {
    const sel = Number(opt.dataset.v) === m;
    opt.classList.toggle("selected", sel);
    if (sel && scroll) opt.scrollIntoView({ block: "center" });
  }
}

function applySegmented(segId, thumbId, value) {
  const seg = $(segId);
  const btns = [...seg.querySelectorAll(".seg-btn")];
  const idx = Math.max(0, btns.findIndex((b) => b.dataset.val === value));
  $(thumbId).style.transform = `translateX(${idx * 100}%)`;
}

function renderCustomDays() {
  const isCustom = (cfg.day_mode || "daily") === "custom";
  $("customDaysWrap").classList.toggle("show", isCustom);
  $("dayModeSummary").textContent = buildDayModeLabel(cfg);
  const days = Array.isArray(cfg.custom_days) ? cfg.custom_days : [];
  for (const btn of $("customDaysGrid").querySelectorAll(".wday-btn")) {
    btn.classList.toggle("active", days.includes(Number(btn.dataset.day)));
  }
  const hint = $("customDaysHint");
  if (!days.length) {
    hint.textContent = "请至少选择一天运行星期，否则定时计划无法启动";
    hint.className = "wday-hint err";
  } else {
    hint.textContent = "已选运行日：" + days.map((d) => WEEK_FULL[d]).join("、");
    hint.className = "wday-hint";
  }
}

function renderSuffixRules() {
  const wrap = $("suffixRulesList");
  wrap.innerHTML = "";
  const rules = Array.isArray(cfg.suffix_rules) ? cfg.suffix_rules : [];
  if (!rules.length) {
    const tip = document.createElement("div");
    tip.className = "empty-rule-tip";
    tip.textContent = "暂无分时段规则，点右上角「+ 添加时段」可为特定时段单独加后缀";
    wrap.appendChild(tip);
    return;
  }
  rules.forEach((rule, idx) => {
    const row = document.createElement("div");
    row.className = "rule-card-row";
    const top = document.createElement("div");
    top.className = "rule-top";
    const pair = document.createElement("div");
    pair.className = "pill-pair";

    const stBtn = document.createElement("button");
    stBtn.type = "button";
    stBtn.className = "ios-pill-btn";
    stBtn.textContent = rule.start || "11:30";
    stBtn.onclick = () => {
      const [h, m] = parseHmStr(rule.start, 11, 30);
      openPicker({ title: "后缀生效开始时间", mode: "time", h, m, onConfirm: (nh, nm) => { cfg.suffix_rules[idx].start = hmStr(nh, nm); stBtn.textContent = cfg.suffix_rules[idx].start; scheduleSave(true); } });
    };
    const sep = document.createElement("span");
    sep.className = "pill-sep";
    sep.textContent = "至";
    const edBtn = document.createElement("button");
    edBtn.type = "button";
    edBtn.className = "ios-pill-btn";
    edBtn.textContent = rule.end || "12:30";
    edBtn.onclick = () => {
      const [h, m] = parseHmStr(rule.end, 12, 30);
      openPicker({ title: "后缀生效结束时间", mode: "time", h, m, onConfirm: (nh, nm) => { cfg.suffix_rules[idx].end = hmStr(nh, nm); edBtn.textContent = cfg.suffix_rules[idx].end; scheduleSave(true); } });
    };
    pair.appendChild(stBtn);
    pair.appendChild(sep);
    pair.appendChild(edBtn);

    const actions = document.createElement("div");
    actions.className = "rule-actions";
    const sw = document.createElement("label");
    sw.className = "ios-switch sm";
    const chk = document.createElement("input");
    chk.type = "checkbox";
    chk.checked = rule.enabled !== false;
    chk.onchange = () => { cfg.suffix_rules[idx].enabled = chk.checked; scheduleSave(true); };
    const sl = document.createElement("span");
    sl.className = "ios-slider";
    sw.appendChild(chk);
    sw.appendChild(sl);

    const del = document.createElement("button");
    del.type = "button";
    del.className = "clear-circle";
    del.title = "删除该时段规则";
    del.innerHTML = SVG_CLOSE;
    del.onclick = () => { cfg.suffix_rules.splice(idx, 1); renderSuffixRules(); scheduleSave(true); };
    actions.appendChild(sw);
    actions.appendChild(del);
    top.appendChild(pair);
    top.appendChild(actions);

    const inpBox = document.createElement("div");
    inpBox.className = "rule-input-box";
    const inp = document.createElement("input");
    inp.type = "text";
    inp.placeholder = "该时段后缀（如：午间数据）";
    inp.value = rule.suffix || "";
    inp.oninput = () => { cfg.suffix_rules[idx].suffix = inp.value.trim(); scheduleSave(false); };
    inpBox.appendChild(inp);

    row.appendChild(top);
    row.appendChild(inpBox);
    wrap.appendChild(row);
  });
}

function renderCaptureWindows() {
  const wrap = $("cpWindowsList");
  wrap.innerHTML = "";
  const wins = (cfg.capture_plan && Array.isArray(cfg.capture_plan.windows)) ? cfg.capture_plan.windows : [];
  if (!wins.length) {
    const tip = document.createElement("div");
    tip.className = "empty-rule-tip";
    tip.textContent = "暂未设置允许窗口，自动值守将仅更新表格不发钉钉";
    wrap.appendChild(tip);
    return;
  }
  wins.forEach((win, idx) => {
    const row = document.createElement("div");
    row.className = "rule-card-row";
    const top = document.createElement("div");
    top.className = "rule-top";
    const pair = document.createElement("div");
    pair.className = "pill-pair";

    const stBtn = document.createElement("button");
    stBtn.type = "button";
    stBtn.className = "ios-pill-btn";
    stBtn.textContent = win.start || "09:00";
    stBtn.onclick = () => {
      const [h, m] = parseHmStr(win.start, 9, 0);
      openPicker({ title: "允许发群开始时间", mode: "time", h, m, onConfirm: (nh, nm) => { cfg.capture_plan.windows[idx].start = hmStr(nh, nm); stBtn.textContent = cfg.capture_plan.windows[idx].start; scheduleSave(true); } });
    };
    const sep = document.createElement("span");
    sep.className = "pill-sep";
    sep.textContent = "至";
    const edBtn = document.createElement("button");
    edBtn.type = "button";
    edBtn.className = "ios-pill-btn";
    edBtn.textContent = win.end || "18:00";
    edBtn.onclick = () => {
      const [h, m] = parseHmStr(win.end, 18, 0);
      openPicker({ title: "允许发群结束时间", mode: "time", h, m, onConfirm: (nh, nm) => { cfg.capture_plan.windows[idx].end = hmStr(nh, nm); edBtn.textContent = cfg.capture_plan.windows[idx].end; scheduleSave(true); } });
    };
    pair.appendChild(stBtn);
    pair.appendChild(sep);
    pair.appendChild(edBtn);

    const actions = document.createElement("div");
    actions.className = "rule-actions";
    const sw = document.createElement("label");
    sw.className = "ios-switch sm";
    const chk = document.createElement("input");
    chk.type = "checkbox";
    chk.checked = win.enabled !== false;
    chk.onchange = () => { cfg.capture_plan.windows[idx].enabled = chk.checked; scheduleSave(true); };
    const sl = document.createElement("span");
    sl.className = "ios-slider";
    sw.appendChild(chk);
    sw.appendChild(sl);

    const del = document.createElement("button");
    del.type = "button";
    del.className = "clear-circle";
    del.title = "删除该窗口";
    del.innerHTML = SVG_CLOSE;
    del.onclick = () => { cfg.capture_plan.windows.splice(idx, 1); renderCaptureWindows(); scheduleSave(true); };
    actions.appendChild(sw);
    actions.appendChild(del);
    top.appendChild(pair);
    top.appendChild(actions);
    row.appendChild(top);
    wrap.appendChild(row);
  });
}

function _isPointOverrideEmpty(ov) {
  if (!ov) return true;
  return (
    (!ov.mode || ov.mode === "default")
    && (ov.suffix === null || ov.suffix === undefined)
    && (ov.silent === null || ov.silent === undefined)
    && (ov.sheet_day_offset === null || ov.sheet_day_offset === undefined)
    && (!ov.msg_time_mode || ov.msg_time_mode === "default")
    && (ov.msg_day_offset === null || ov.msg_day_offset === undefined)
  );
}

function renderTimetable(tt) {
  if (Array.isArray(tt)) latestTimetable = tt;
  const list = $("timetableList");
  list.innerHTML = "";
  const dayOk = isRunDayAllowedLocal(cfg);
  $("ttSummaryText").textContent = `共 ${latestTimetable.length} 轮${dayOk ? " · 今日运行" : " · 今日休眠"}`;

  const hasOv = Boolean(cfg.point_overrides && Object.keys(cfg.point_overrides).length > 0);
  $("resetAllPointsBtn").style.display = hasOv ? "inline-flex" : "none";

  if (!latestTimetable.length) {
    const empty = document.createElement("div");
    empty.className = "empty-rule-tip";
    empty.textContent = "当前时段配置未生成可执行时间点";
    list.appendChild(empty);
    return;
  }

  const now = new Date();
  const nowMin = now.getHours() * 60 + now.getMinutes();
  const hasAnyFutureDay = (cfg.day_mode || "daily") !== "custom" || (Array.isArray(cfg.custom_days) && cfg.custom_days.length > 0);
  const hasLaterToday = dayOk && latestTimetable.some((it) => it.min > nowMin);
  const enabledTargetCount = getEnabledTargetsLocal().length;
  let nextMarked = false;

  latestTimetable.forEach((item, idx) => {
    const row = document.createElement("div");
    row.className = "tt-row";

    const timeCol = document.createElement("div");
    timeCol.className = "tt-time-col";
    const hmEl = document.createElement("div");
    hmEl.className = "tt-hm";
    hmEl.textContent = item.time;
    const perEl = document.createElement("div");
    perEl.className = "tt-period";
    const perTxt = document.createElement("span");
    perTxt.textContent = item.period || "白天";
    perEl.appendChild(perTxt);
    if (dayOk && !nextMarked && item.min > nowMin) {
      nextMarked = true;
      const tag = document.createElement("span");
      tag.className = "tt-next-tag";
      tag.textContent = "下次";
      perEl.appendChild(tag);
    } else if (!hasLaterToday && hasAnyFutureDay && idx === 0 && !nextMarked) {
      nextMarked = true;
      const tag = document.createElement("span");
      tag.className = "tt-next-tag tomorrow";
      tag.textContent = "下轮";
      perEl.appendChild(tag);
    }
    timeCol.appendChild(hmEl);
    timeCol.appendChild(perEl);

    const rawOv = cfg.point_overrides && cfg.point_overrides[item.time];
    const hasPointOv = Boolean(rawOv && !_isPointOverrideEmpty(rawOv));
    const sOff = Number(item.sheet_offset_effective || 0);
    const relDay = formatOffsetLabel(sOff);
    const mdText = formatShortMdLocal(sOff);

    const badges = document.createElement("div");
    badges.className = "tt-badges";

    if (item.do_dingtalk) {
      const dtPill = document.createElement("span");
      dtPill.className = "tt-pill dt-on";
      dtPill.textContent = enabledTargetCount > 1 ? `发钉钉(${enabledTargetCount}群)` : "发钉钉";
      badges.appendChild(dtPill);

      const shPill = document.createElement("span");
      shPill.className = "tt-pill date-on";
      shPill.textContent = `查${relDay} (${mdText})`;
      badges.appendChild(shPill);

      const msgPrev = String(item.msg_preview || "").trim();
      const sufPill = document.createElement("span");
      sufPill.className = "tt-pill suf-on";
      sufPill.textContent = msgPrev ? `配文：${msgPrev}` : "配文：仅图片";
      badges.appendChild(sufPill);
    } else {
      const noDtPill = document.createElement("span");
      noDtPill.className = "tt-pill off";
      noDtPill.textContent = item.do_capture ? "不发钉钉 · 只存图" : "不发钉钉 · 只更表";
      badges.appendChild(noDtPill);

      const shPill = document.createElement("span");
      shPill.className = "tt-pill date-on";
      shPill.textContent = `查${relDay} (${mdText})`;
      badges.appendChild(shPill);
    }

    if (hasPointOv) {
      const ovPill = document.createElement("span");
      ovPill.className = "tt-pill off";
      ovPill.textContent = "单独设";
      badges.appendChild(ovPill);
    }

    const rightCtrl = document.createElement("div");
    rightCtrl.className = "tt-right-ctrl";
    if (item.do_dingtalk) {
      const silWrap = document.createElement("div");
      silWrap.className = "tt-sil-wrap";
      silWrap.onclick = (e) => e.stopPropagation();

      const silLbl = document.createElement("span");
      silLbl.className = "tt-sil-lbl" + (item.do_silent ? " sil" : "");
      silLbl.textContent = item.do_silent ? "静音" : "提醒";

      const silSw = document.createElement("label");
      silSw.className = "ios-switch xs";
      const silChk = document.createElement("input");
      silChk.type = "checkbox";
      silChk.checked = Boolean(item.do_silent);
      silChk.onchange = () => {
        if (!cfg.point_overrides) cfg.point_overrides = {};
        const globalSil = $("dtSilentSend").checked;
        const curOv = cfg.point_overrides[item.time] ? { ...cfg.point_overrides[item.time] } : { mode: "default", suffix: null, silent: null, sheet_day_offset: null, msg_time_mode: null, msg_day_offset: null };
        curOv.silent = silChk.checked === globalSil ? null : silChk.checked;
        if (_isPointOverrideEmpty(curOv)) delete cfg.point_overrides[item.time];
        else cfg.point_overrides[item.time] = curOv;
        _applyOvToLiveTimetable(item.time, cfg.point_overrides[item.time] || null);
        scheduleSave(true);
      };
      const silSlider = document.createElement("span");
      silSlider.className = "ios-slider";
      silSw.appendChild(silChk);
      silSw.appendChild(silSlider);
      silWrap.appendChild(silLbl);
      silWrap.appendChild(silSw);
      rightCtrl.appendChild(silWrap);
    }

    const chevWrap = document.createElement("div");
    chevWrap.style.cssText = "display:flex;align-items:center;";
    chevWrap.innerHTML = SVG_CHEVRON;
    rightCtrl.appendChild(chevWrap);

    row.appendChild(timeCol);
    row.appendChild(badges);
    row.appendChild(rightCtrl);
    row.onclick = () => openPointModal(item);
    list.appendChild(row);
  });
}

function _applyOvToLiveTimetable(hm, ovObj) {
  const livePt = Array.isArray(latestTimetable) ? latestTimetable.find((it) => it && it.time === hm) : null;
  if (!livePt) return;
  const ov = ovObj && typeof ovObj === "object" ? ovObj : {};
  const mode = ov.mode || "default";
  const act = resolvePointActionStateLocal(hm, mode);
  livePt.override_mode = mode;
  livePt.do_capture = act.doCapture;
  livePt.do_dingtalk = act.doDingtalk;

  const gSil = $("dtSilentSend") ? $("dtSilentSend").checked : (cfg.dt_silent_send !== false);
  const silVal = typeof ov.silent === "boolean" ? ov.silent : null;
  livePt.do_silent = (silVal !== null) ? silVal : gSil;
  livePt.silent_override = silVal;

  const dateItem = (ensureSummaryPresetsStruct().items || []).find((x) => x && x.enabled !== false && x.action === "today");
  let sOff = 0;
  if (ov.sheet_day_offset !== null && ov.sheet_day_offset !== undefined) sOff = clampOff(ov.sheet_day_offset, 0);
  else if (dateItem) sOff = resolvePresetDayOffsetLocal(dateItem, hm).off;
  const sFmt = dateItem ? (dateItem.date_fmt || "YYYY年M月D日") : "YYYY年M月D日";
  livePt.sheet_offset_effective = sOff;
  livePt.sheet_date_preview = formatShiftedDateLocal(sFmt, sOff);

  const msgPrev = buildMessagePreviewLocal(hm);
  livePt.suffix = msgPrev.suf;
  livePt.has_custom_suffix = ov.suffix !== null && ov.suffix !== undefined;
  livePt.msg_time_mode = msgPrev.tMode;
  livePt.msg_day_offset = msgPrev.dOff;
  livePt.msg_preview = msgPrev.text;
  renderTimetable(latestTimetable);
}

function _syncEditingPointLive(immediateSave = false) {
  if (!editingPointHm) return;
  if (!cfg.point_overrides) cfg.point_overrides = {};
  const hasCustom = $("pointCustomSufOn").checked;
  const silVal = editingPointSilent === "on" ? true : (editingPointSilent === "off" ? false : null);
  const sufVal = hasCustom ? $("pointCustomSufInput").value.trim() : null;
  const msgModeVal = editingPtMsgMode === "default" ? null : editingPtMsgMode;
  const ovObj = {
    mode: editingPointMode,
    suffix: sufVal,
    silent: silVal,
    sheet_day_offset: editingPtSheetOff,
    msg_time_mode: msgModeVal,
    msg_day_offset: editingPtMsgOff,
  };
  if (_isPointOverrideEmpty(ovObj)) delete cfg.point_overrides[editingPointHm];
  else cfg.point_overrides[editingPointHm] = ovObj;
  _applyOvToLiveTimetable(editingPointHm, cfg.point_overrides[editingPointHm] || null);
  scheduleSave(immediateSave);
}

function _refreshPointModalPreview() {
  if (!editingPointHm) return;
  const actState = resolvePointActionStateLocal(editingPointHm, editingPointMode);
  if ($("pointModeDefaultSub")) {
    $("pointModeDefaultSub").textContent = `默认：${actState.defaultDingtalk ? "发钉钉" : "不发钉钉"}`;
  }
  if ($("pointActionPrevText")) {
    $("pointActionPrevText").textContent = actState.doDingtalk ? "更新表格 + 发钉钉" : "只更新表格（不发钉钉）";
  }

  // 表格日期预览
  const dateItem = (ensureSummaryPresetsStruct().items || []).find((x) => x && x.enabled !== false && x.action === "today");
  let sOff = 0;
  let sSrc = "按默认";
  if (editingPtSheetOff !== null && editingPtSheetOff !== undefined) {
    sOff = clampOff(editingPtSheetOff, 0);
    sSrc = "单独设";
  } else if (dateItem) {
    const r = resolvePresetDayOffsetLocal(dateItem, editingPointHm);
    sOff = r.off;
    sSrc = r.src;
  }
  const sFmt = dateItem ? (dateItem.date_fmt || "YYYY年M月D日") : "YYYY年M月D日";
  $("pointSheetPrevText").textContent = `${formatOffsetLabel(sOff)} · ${formatShiftedDateLocal(sFmt, sOff)}（${sSrc}）`;

  // 根据本轮是否发钉钉动态显隐群消息/静音等钉钉专属区域
  if ($("pointMsgPrevRow")) $("pointMsgPrevRow").style.display = actState.doDingtalk ? "block" : "none";
  if ($("pointDingtalkSettingsWrap")) $("pointDingtalkSettingsWrap").style.display = actState.doDingtalk ? "flex" : "none";
  if ($("pointNoDingtalkTip")) $("pointNoDingtalkTip").style.display = actState.doDingtalk ? "none" : "block";

  // 钉钉文案预览与提醒方式
  const gSil = $("dtSilentSend") ? $("dtSilentSend").checked : true;
  const effSil = editingPointSilent === "on" ? true : (editingPointSilent === "off" ? false : gSil);
  const baseTp = resolveDingtalkTimeLocal(editingPointHm);
  const mMode = editingPtMsgMode === "default" ? (cfg.dt_time_mode || "datetime") : editingPtMsgMode;
  if ($("pointMsgDateRow")) $("pointMsgDateRow").style.display = (mMode === "none") ? "none" : "flex";
  const mOff = (editingPtMsgOff !== null && editingPtMsgOff !== undefined) ? clampOff(editingPtMsgOff, 0) : baseTp.dOff;
  const datePart = formatShiftedDateLocal(baseTp.dFmt, mOff);
  const stamp = mMode === "datetime" ? `${datePart} ${editingPointHm}` : (mMode === "date_only" ? datePart : "");
  const hasSuf = $("pointCustomSufOn").checked;
  const sufVal = hasSuf ? $("pointCustomSufInput").value.trim() : resolveSuffixByRulesOnly(editingPointHm).suf;
  const full = [stamp, sufVal].filter(Boolean).join(" ") || datePart;
  $("pointMsgPrevText").textContent = `「${full}」 · ${effSil ? "静音发送" : "正常提醒"}`;

  // 更新按钮选中高亮与步进示数
  for (const b of $("pointSheetOffSeg").querySelectorAll(".mini-seg-btn")) {
    const v = b.dataset.val;
    const active = v === "default" ? (editingPtSheetOff === null) : (editingPtSheetOff !== null && Number(v) === editingPtSheetOff);
    b.classList.toggle("active", active);
  }
  $("ptSheetOffVal").textContent = editingPtSheetOff === null ? "按默认" : formatOffsetLabel(editingPtSheetOff);

  for (const b of $("pointMsgModeSeg").querySelectorAll(".mini-seg-btn")) {
    b.classList.toggle("active", b.dataset.val === editingPtMsgMode);
  }
  for (const b of $("pointMsgOffSeg").querySelectorAll(".mini-seg-btn")) {
    const v = b.dataset.val;
    const active = v === "default" ? (editingPtMsgOff === null) : (editingPtMsgOff !== null && Number(v) === editingPtMsgOff);
    b.classList.toggle("active", active);
  }
  $("ptMsgOffVal").textContent = editingPtMsgOff === null ? "按默认" : formatOffsetLabel(editingPtMsgOff);
}

function openPointModal(item) {
  editingPointHm = item.time;
  $("pointSheetTitle").textContent = `${item.time} 时间点设置`;
  const ov = (cfg.point_overrides && cfg.point_overrides[item.time]) || {};
  editingPointOrigOv = cfg.point_overrides && cfg.point_overrides[item.time]
    ? JSON.parse(JSON.stringify(cfg.point_overrides[item.time]))
    : null;
  editingPointMode = ov.mode || "default";
  applySegmented("pointModeSeg", "pointModeThumb", editingPointMode);

  editingPointSilent = ov.silent === true ? "on" : (ov.silent === false ? "off" : "default");
  applySegmented("pointSilentSeg", "pointSilentThumb", editingPointSilent);
  $("pointSilentDefaultSub").textContent = "默认：" + ($("dtSilentSend").checked ? "静音发送" : "正常提醒");

  editingPtSheetOff = (ov.sheet_day_offset !== null && ov.sheet_day_offset !== undefined) ? clampOff(ov.sheet_day_offset, 0) : null;
  editingPtMsgMode = ["datetime", "date_only", "none"].includes(ov.msg_time_mode) ? ov.msg_time_mode : "default";
  editingPtMsgOff = (ov.msg_day_offset !== null && ov.msg_day_offset !== undefined) ? clampOff(ov.msg_day_offset, 0) : null;

  const baseRes = resolveSuffixByRulesOnly(item.time);
  $("pointSufHint").textContent = baseRes.suf
    ? `不单独修改时将使用：「${baseRes.suf}」`
    : "不单独修改时按默认（无结尾词）";

  const hasCustom = ov.suffix !== undefined && ov.suffix !== null;
  $("pointCustomSufOn").checked = hasCustom;
  $("pointCustomSufWrap").style.display = hasCustom ? "flex" : "none";
  $("pointCustomSufInput").value = hasCustom ? String(ov.suffix) : (item.suffix || "");
  _refreshPointModalPreview();
  $("pointMask").classList.add("show");
}

function renderHomeCellChips(sp, ut, curHm) {
  const wrap = $("homeCellChips");
  if (!wrap) return;
  wrap.innerHTML = "";
  const active = (sp.items || []).filter((it) => it && it.enabled !== false && String(it.cell || "").trim());
  const cards = [];
  for (const it of active.slice(0, 7)) {
    const act = it.action || "value";
    let kind = "固定值";
    let val = it.value || "(空)";
    if (act === "today") {
      const r2 = resolvePresetDayOffsetLocal(it, curHm);
      kind = "自动日期";
      val = `${formatShiftedDateLocal(it.date_fmt || "YYYY年M月D日", r2.off)} · ${r2.src}`;
    } else if (act === "now") {
      kind = "此刻时间";
      val = it.time_fmt === "hm" ? "YYYY-MM-DD HH:MM" : (it.time_fmt === "time_only" ? "HH:MM:SS" : "年月日时分秒");
    } else if (act === "formula") {
      kind = "公式";
      val = it.value || "=公式";
    } else if (act === "clear") {
      kind = "清空";
      val = "定位后清空该格";
    }
    const sh = (it.sheet && it.sheet !== "数据汇总") ? `${it.sheet}·` : "";
    const lbl = it.label ? ` (${it.label})` : "";
    cards.push({ addr: `${sh}${it.cell}${lbl}`, kind, val });
  }
  if (ut && ut.enabled !== false) {
    cards.push({
      addr: `${ut.cell || "S4"} (最后更新)`,
      kind: ut.only_after_paste === false ? "每次" : "贴数后",
      val: formatNowFullTimestampLocal(),
    });
  }
  if (!cards.length) {
    wrap.innerHTML = `<div style="grid-column:1 / -1;font-size:11.5px;color:var(--sub);text-align:center;padding:8px 0;">暂未启用任何自定义格位，点击右上角添加</div>`;
    return;
  }
  for (const c of cards) {
    const el = document.createElement("div");
    el.className = "home-cell-pill";
    el.onclick = () => {
      switchTab("dingtalk");
      setTimeout(() => {
        const sec = $("summaryPresetsSection");
        if (sec && sec.scrollIntoView) sec.scrollIntoView({ behavior: "smooth", block: "start" });
      }, 60);
    };
    const top = document.createElement("div");
    top.className = "home-cell-top";
    const addr = document.createElement("span");
    addr.className = "home-cell-addr";
    addr.textContent = c.addr;
    const knd = document.createElement("span");
    knd.className = "home-cell-kind";
    knd.textContent = c.kind;
    top.appendChild(addr);
    top.appendChild(knd);
    const val = document.createElement("div");
    val.className = "home-cell-val";
    val.textContent = c.val;
    el.appendChild(top);
    el.appendChild(val);
    wrap.appendChild(el);
  }
}

function applyDataBriefUI(brief) {
  if (!brief || typeof brief !== "object") return;
  const cnt = Number(brief.archive_count || 0);
  if ($("hdrArchivePill")) $("hdrArchivePill").textContent = `数据档案 ${cnt} 轮`;
  if ($("kpiRegVal") && brief.reg_val) $("kpiRegVal").textContent = brief.reg_val;
  if ($("kpiRegSub") && brief.reg_sub) $("kpiRegSub").textContent = brief.reg_sub;
  if ($("kpiTopVal") && brief.top_val) $("kpiTopVal").textContent = brief.top_val;
  if ($("kpiTopSub") && brief.top_sub) $("kpiTopSub").textContent = brief.top_sub;
}

function updateSummaries() {
  const enabledTargets = getEnabledTargetsLocal();
  if (!enabledTargets.length) {
    $("sumTarget").textContent = "未启用会话";
    $("kwPreview").textContent = "请至少开启或添加一个目标会话，否则本轮将跳过钉钉发送";
  } else if (enabledTargets.length === 1) {
    $("sumTarget").textContent = enabledTargets[0];
    $("kwPreview").textContent = `将在钉钉按 Ctrl+Shift+F 直接粘贴「${enabledTargets[0]}」并按 Enter 进入会话`;
  } else {
    $("sumTarget").textContent = `${enabledTargets[0]} 等 ${enabledTargets.length} 群`;
    $("kwPreview").textContent = `将依次投递共 ${enabledTargets.length} 个会话：${enabledTargets.join("、")}`;
  }

  const now = new Date();
  const curHm = hmStr(now.getHours(), now.getMinutes());
  const prev = buildMessagePreviewLocal(curHm);
  const modeLbl = prev.tMode === "datetime" ? "日期+时间" : (prev.tMode === "date_only" ? "仅发日期" : "不加日期");
  const offLbl = formatOffsetLabel(prev.dOff);
  $("msgPreview").textContent = `此刻(${curHm})钉钉气泡预览：「${prev.text}」（${modeLbl} · ${offLbl}）`;

  const policyParts = [];
  policyParts.push($("dtSilentSend").checked ? "静音" : "常规");
  policyParts.push($("dtAutoSend").checked ? "自动发送" : "暂不发");
  $("sumPolicy").textContent = policyParts.join(" · ");
  const pasteWait = clampPasteDelay(cfg.dt_paste_delay_sec, 3);
  $("sumPolicySub").textContent = `${modeLbl} (${offLbl}) · 等${pasteWait}秒粘贴 · ${prev.suf ? "「" + prev.suf + "」" : "无后缀"}`;

  const ds = hmStr(cfg.day.sh, cfg.day.sm);
  const de = hmStr(cfg.day.eh, cfg.day.em);
  const dg = cfg.day.ih * 60 + cfg.day.im;
  $("dayStartBtn").textContent = ds;
  $("dayEndBtn").textContent = de;
  $("dayGapBtn").textContent = formatGapText(cfg.day.ih, cfg.day.im);
  $("daySummary").textContent = `${ds}-${de} 每${dg}分`;

  const nOn = $("nightEnabled").checked;
  $("nightBody").classList.toggle("disabled", !nOn);
  const ns = hmStr(cfg.night.sh, cfg.night.sm);
  const ne = hmStr(cfg.night.eh, cfg.night.em);
  $("nightStartBtn").textContent = ns;
  $("nightEndBtn").textContent = ne;
  $("nightGapBtn").textContent = formatGapText(cfg.night.ih, cfg.night.im);

  $("overlapTip").style.display = (nOn && slotsOverlap(ds, de, ns, ne)) ? "block" : "none";
  $("dayModeSummary").textContent = buildDayModeLabel(cfg);
  $("sumScheduleSub").textContent = `${buildDayModeLabel(cfg)} · 全天 ${latestTimetable.length} 轮`;

  const sp = ensureSummaryPresetsStruct();
  const ut = ensureUpdateTimeStruct();
  renderHomeCellChips(sp, ut, curHm);
  const utSuffix = ut.enabled !== false ? ` · ${ut.cell || "S4"}时间` : "";
  const spActive = sp.items.filter((it) => it && it.enabled !== false && String(it.cell || "").trim());
  if (sp.enabled === false) {
    $("sumPresetsVal").textContent = ut.enabled !== false ? `仅写 ${ut.cell || "S4"}` : "已关闭";
    $("sumPresetsSub").textContent = ut.enabled !== false ? `最后更新时间 -> ${ut.cell || "S4"}` : "截图前暂不校准";
  } else {
    $("sumPresetsVal").textContent = `已启用 ${spActive.length} 项${utSuffix}`;
    const d2 = spActive.find((x) => x.cell === "D2" && x.action === "today");
    if (d2) {
      const r2 = resolvePresetDayOffsetLocal(d2, curHm);
      $("sumPresetsSub").textContent = `D2:${formatShiftedDateLocal(d2.date_fmt || "YYYY年M月D日", r2.off)} (${r2.src})`;
    } else {
      $("sumPresetsSub").textContent = spActive.length ? spActive.slice(0, 5).map((it) => it.cell).join("/") : "无启用格位";
    }
  }
}

function renderConfig() {
  if (!cfg.day) cfg.day = { sh: 9, sm: 0, eh: 18, em: 0, ih: 2, im: 0 };
  if (!cfg.night) cfg.night = { sh: 18, sm: 0, eh: 23, em: 0, ih: 2, im: 0 };
  if (!Array.isArray(cfg.custom_days)) cfg.custom_days = [0, 1, 2, 3, 4];
  if (!Array.isArray(cfg.suffix_rules)) cfg.suffix_rules = [];
  if (!Array.isArray(cfg.dt_targets)) cfg.dt_targets = [];
  if (!cfg.point_overrides) cfg.point_overrides = {};
  ensureSummaryPresetsStruct();
  ensureUpdateTimeStruct();
  ensureAiStruct();

  applySegmented("dayModeSeg", "dayModeThumb", cfg.day_mode || "daily");
  renderCustomDays();
  $("nightEnabled").checked = cfg.night_enabled !== false;
  $("preventSleep").checked = cfg.prevent_sleep !== false;

  $("enableCapture").checked = cfg.enable_capture !== false;
  $("enableDingtalk").checked = Boolean(cfg.enable_dingtalk);
  $("dtClearBeforeSend").checked = cfg.dt_clear_before_send !== false;
  $("dtSilentSend").checked = cfg.dt_silent_send !== false;
  $("dtAutoSend").checked = cfg.dt_auto_send === true;
  $("dtMinimizeAfterSend").checked = cfg.dt_minimize_after_send !== false;
  $("openWpsAfterRun").checked = cfg.open_wps_after_run === true;
  $("saveShotHistory").checked = cfg.save_shot_history !== false;
  renderDtTargets();
  renderDingtalkDatePanel();
  renderDtPasteDelay();
  renderPipelineTuning();
  renderAiControls();

  renderUpdateTimeControls();
  const spOn = cfg.summary_presets.enabled !== false;
  $("spEnabled").checked = spOn;
  $("spAccordion").classList.toggle("open", spOn);
  renderSummaryPresets();
  $("dtSuffix").value = cfg.dt_suffix || "";
  renderSuffixRules();

  const cpOn = Boolean(cfg.capture_plan && cfg.capture_plan.enabled);
  $("cpEnabled").checked = cpOn;
  $("cpAccordion").classList.toggle("open", cpOn);
  renderCaptureWindows();

  $("wpsUrl").value = cfg.wps_url || "";
  $("luyaUser").value = cfg.luya_user || "";
  $("luyaPass").value = cfg.luya_pass || "";

  updateSummaries();
}

function collectConfigFromDOM() {
  cfg.night_enabled = $("nightEnabled").checked;
  cfg.prevent_sleep = $("preventSleep").checked;
  cfg.enable_capture = $("enableCapture").checked;
  cfg.enable_dingtalk = $("enableDingtalk").checked;
  cfg.dt_clear_before_send = $("dtClearBeforeSend").checked;
  cfg.dt_silent_send = $("dtSilentSend").checked;
  cfg.dt_auto_send = $("dtAutoSend").checked;
  cfg.dt_minimize_after_send = $("dtMinimizeAfterSend").checked;
  cfg.open_wps_after_run = $("openWpsAfterRun").checked;
  cfg.save_shot_history = $("saveShotHistory").checked;
  cfg.dt_cutoff_enabled = $("dtCutoffEnabled").checked;
  cfg.dt_paste_delay_sec = clampPasteDelay(cfg.dt_paste_delay_sec, 3);
  cfg.pipeline_tuning = ensurePipelineTuningStruct();
  syncPrimaryDtSearch();
  ensureSummaryPresetsStruct();
  cfg.summary_presets.enabled = $("spEnabled").checked;
  const ut = ensureUpdateTimeStruct();
  ut.enabled = $("utEnabled").checked;
  ut.cell = $("utCellInput").value.replace(/\s+/g, "").toUpperCase() || "S4";
  cfg.dt_suffix = $("dtSuffix").value.trim();
  cfg.wps_url = $("wpsUrl").value.trim();
  cfg.luya_user = $("luyaUser").value.trim();
  cfg.luya_pass = $("luyaPass").value;

  const ai = ensureAiStruct();
  ai.enabled = $("aiEnabled").checked;
  ai.auto_diagnose = $("aiAutoDiagnose").checked;
  ai.auto_heal = $("aiAutoHeal").checked;
  ai.auto_attach_summary = $("aiAttachSummary").checked;
  ai.custom_summary_prompt = $("aiCustomPrompt").value.trim();
  if ($("aiFocusTeamInput")) ai.focus_team = $("aiFocusTeamInput").value.trim();

  if (!cfg.capture_plan) cfg.capture_plan = { enabled: false, windows: [] };
  cfg.capture_plan.enabled = $("cpEnabled").checked;
  return cfg;
}

function scheduleSave(immediate = false) {
  updateSummaries();
  clearTimeout(saveTimer);
  const runSave = async () => {
    const data = collectConfigFromDOM();
    if (previewMemberUid && adminToken) {
      const r = await api("/api/admin/member", "POST", { user_id: previewMemberUid, config: data }, { "X-Admin-Token": adminToken });
      showToast(r.ok ? "已远程修改并推送该成员设置" : (r.message || "远程修改失败"));
      return;
    }
    const res = await api("/api/config", "POST", data);
    if (res && res.ok === false && res.config) {
      cfg = res.config;
      renderConfig();
      if (Array.isArray(res.timetable)) {
        latestTimetable = res.timetable;
        renderTimetable(res.timetable);
      }
      showToast(res.message || "权限受限，已自动还原");
      return;
    }
    if (res && res.ai_meta) {
      aiStyles = Array.isArray(res.ai_meta.styles) ? res.ai_meta.styles : aiStyles;
      aiCandidateTeams = Array.isArray(res.ai_meta.candidate_teams) ? res.ai_meta.candidate_teams : aiCandidateTeams;
      renderAiControls();
    }
    if (res && Array.isArray(res.timetable)) {
      latestTimetable = res.timetable;
      renderTimetable(res.timetable);
      updateSummaries();
    }
    if (immediate) showToast("设置已保存");
  };
  if (immediate) runSave();
  else saveTimer = setTimeout(runSave, 350);
}

function formatCountdown(nextTs, serverNow) {
  if (!nextTs || nextTs <= 0) return "";
  const diff = Math.max(0, Math.round(nextTs - serverNow));
  if (diff <= 0) return "即将执行";
  const m = Math.floor(diff / 60);
  const s = diff % 60;
  return m > 0 ? `${m}分${s}秒后` : `${s}秒后`;
}

function renderLogs() {
  const box = $("logBox");
  const filtered = logFilterErrOnly ? allLogItems.filter((it) => it.level === "err" || it.level === "warn") : allLogItems;
  box.innerHTML = "";
  if (!filtered.length) {
    const emp = document.createElement("div");
    emp.className = "log-empty";
    emp.id = "logEmpty";
    emp.textContent = logFilterErrOnly ? "当前无异常或警告日志" : "暂无运行日志";
    box.appendChild(emp);
    return;
  }
  const frag = document.createDocumentFragment();
  for (const item of filtered) {
    const row = document.createElement("div");
    row.className = "log-line";
    const dot = document.createElement("span");
    dot.className = `log-dot ${item.level || "info"}`;
    const t = document.createElement("span");
    t.className = "log-time";
    t.textContent = item.time;
    const msg = document.createElement("span");
    msg.className = `log-msg ${item.level || "info"}`;
    msg.textContent = cleanText(item.text) || item.text;
    row.appendChild(dot);
    row.appendChild(t);
    row.appendChild(msg);
    frag.appendChild(row);
  }
  box.appendChild(frag);
  if (autoScroll) scrollToLogBottom(false);

  const miniBox = $("homeMiniLogs");
  if (miniBox) {
    const tail = allLogItems.slice(-7);
    if (!tail.length) {
      miniBox.innerHTML = `<div style="color:var(--sub);text-align:center;padding:18px 0;">运行启动后，这里会实时滚播关键步骤与数据归档记录</div>`;
    } else {
      miniBox.innerHTML = "";
      const mf = document.createDocumentFragment();
      for (const it of tail) {
        const r = document.createElement("div");
        r.className = "log-line";
        const d = document.createElement("span");
        d.className = `log-dot ${it.level || "info"}`;
        const tm = document.createElement("span");
        tm.className = "log-time";
        tm.textContent = it.time;
        const mg = document.createElement("span");
        mg.className = `log-msg ${it.level || "info"}`;
        mg.textContent = cleanText(it.text) || it.text;
        r.appendChild(d);
        r.appendChild(tm);
        r.appendChild(mg);
        mf.appendChild(r);
      }
      miniBox.appendChild(mf);
      miniBox.scrollTop = miniBox.scrollHeight;
    }
  }
}

function appendLogLines(items) {
  if (!items || !items.length) return;
  for (const it of items) {
    allLogItems.push(it);
  }
  if (allLogItems.length > 500) allLogItems = allLogItems.slice(-500);
  renderLogs();
}

let lastAiPanelSig = "";
let lastTellDevPrompt = "";

function renderErrorDiagCard(det, aiRes, aiBusy) {
  const card = $("errorDiagCard");
  if (!det || !det.code) {
    card.classList.remove("show");
    currentErrorDetail = null;
    diagDismissedSig = "";
    lastAiPanelSig = "";
    lastTellDevPrompt = "";
    return;
  }
  const sig = `${det.code}|${det.raw_error || ""}`;
  if (sig === diagDismissedSig) return;
  currentErrorDetail = det;
  $("diagCodeBadge").textContent = det.code;
  $("diagTitle").textContent = cleanText(det.title) || "运行遇到异常";
  $("diagReason").textContent = cleanText(det.plain_reason) || "";
  $("diagSelfFix").textContent = cleanText(det.self_fix) || "";
  card.classList.add("show");

  const aiPanel = $("diagAiPanel");
  const panelSig = aiBusy ? "busy" : ((aiRes && aiRes.reply) ? `${aiRes.mode || ""}|${aiRes.time || ""}|${aiRes.reply.length}` : "none");
  if (panelSig !== lastAiPanelSig) {
    lastAiPanelSig = panelSig;
    if (aiBusy) {
      aiPanel.textContent = "智能管家正在对照历史记录与报错原因复盘分析，请稍候…";
      aiPanel.classList.add("show");
    } else if (aiRes && aiRes.reply && (aiRes.mode === "diagnose" || aiRes.mode === "auto_heal")) {
      const srcTag = (aiRes.source === "gemini" || aiRes.source === "gemini_vision" || aiRes.source === "ai_cloud" || aiRes.source === "ai_vision")
        ? (aiRes.mode === "auto_heal" ? "智能视觉调试" : "智能历史复盘")
        : (aiRes.mode === "auto_heal" ? "AI 自动看图调试引擎" : "本地专家历史对比复盘");
      aiPanel.innerHTML = `<div class="md-h3">${escapeHtmlStr(srcTag)}</div>${formatSafeMarkdown(aiRes.reply)}`;
      aiPanel.classList.add("show");
    } else {
      aiPanel.classList.remove("show");
    }
  }

  const tuneBtn = $("applyAiTuneBtn");
  if (tuneBtn) {
    const recTune = aiRes && aiRes.recommended_tuning;
    const canTune = Boolean((recTune && recTune.has_tuning) || ["ERR-DT-303", "ERR-DT-304", "ERR-DT-305", "ERR-WPS-203", "ERR-WPS-204"].includes(det.code));
    tuneBtn.style.display = canTune ? "inline-flex" : "none";
    if (recTune && recTune.summary) tuneBtn.title = recTune.summary;
  }

  const agBox = $("agPromptBox");
  const agPre = $("agPromptPre");
  const nextAgPrompt = (aiRes && aiRes.antigravity_prompt) ? aiRes.antigravity_prompt : (det.tell_dev || "");
  lastTellDevPrompt = nextAgPrompt;
  if (agBox && agPre) {
    if (nextAgPrompt) {
      if (agPre.value !== nextAgPrompt) agPre.value = nextAgPrompt;
      agBox.classList.add("show");
    } else {
      agBox.classList.remove("show");
    }
  }
}

function buildMiniCatSvg() {
  return `<svg class="ai-mini-cat" viewBox="0 0 24 24" fill="none"><path d="M5 10L4 4l5 3.5h6L20 4l-1 6a6.5 6.5 0 01-14 0z" fill="#4F46E5" stroke="#38BDF8" stroke-width="1.1" stroke-linejoin="round"/><rect x="6.5" y="9.5" width="11" height="7.5" rx="3.5" fill="#0F172A"/><circle cx="9.8" cy="13" r="1.15" fill="#38BDF8"/><circle cx="14.2" cy="13" r="1.15" fill="#38BDF8"/><path d="M10.4 15.2q1.6 1.1 3.2 0" stroke="#F8FAFC" stroke-width="1.1" stroke-linecap="round"/></svg>`;
}

function renderAiChatFeed(history, pendingText = null) {
  const feed = $("aiChatFeed");
  const guide = $("aiEmptyGuide");
  const items = Array.isArray(history) ? history : [];
  if (!items.length && !pendingText) {
    feed.innerHTML = "";
    if (guide) {
      guide.style.display = "block";
      feed.appendChild(guide);
    }
    return;
  }
  feed.innerHTML = "";
  for (const turn of items) {
    const b = document.createElement("div");
    const isSpot = turn.source === "data_spotlight";
    b.className = `ai-bubble ${turn.role === "user" ? "user" : "model"}${isSpot ? " is-spotlight" : ""}`;
    const txt = document.createElement("div");
    if (turn.role === "user") {
      const viaBadge = turn.via === "mobile"
        ? `[手机${turn.sender ? " · " + turn.sender : ""}] `
        : "";
      txt.textContent = viaBadge + (cleanText(turn.text) || turn.text);
    } else {
      txt.innerHTML = formatSafeMarkdown(turn.text || "");
    }
    b.appendChild(txt);
    if (turn.role !== "user") {
      const meta = document.createElement("div");
      meta.className = "ai-bubble-meta";
      const infoSpan = document.createElement("span");
      infoSpan.style.cssText = "display:inline-flex;align-items:center;";
      const srcDesc = (turn.source === "gemini" || turn.source === "ai_cloud" || turn.source === "data_spotlight" || turn.source === "greeting")
        ? "智能管家"
        : (turn.shot_info ? "智能助手" : "贴士");
      infoSpan.innerHTML = `${buildMiniCatSvg()}<span>${escapeHtmlStr([turn.time, srcDesc, turn.shot_info].filter(Boolean).join(" · "))}</span>`;
      const cpBtn = document.createElement("button");
      cpBtn.type = "button";
      cpBtn.className = "ai-copy-btn";
      cpBtn.textContent = "复制文案";
      cpBtn.onclick = async () => {
        try {
          await navigator.clipboard.writeText(turn.text || "");
          showToast("已复制总结文案");
        } catch (_e) { showToast("复制失败，请手动选中"); }
      };
      meta.appendChild(infoSpan);
      meta.appendChild(cpBtn);
      b.appendChild(meta);
    }
    feed.appendChild(b);
  }
  if (pendingText) {
    const u = document.createElement("div");
    u.className = "ai-bubble user";
    u.textContent = pendingText;
    feed.appendChild(u);

    const w = document.createElement("div");
    w.className = "ai-bubble model";
    w.innerHTML = `<div class="typing-dots"><span></span><span></span><span></span></div>`;
    feed.appendChild(w);
  }
  feed.scrollTop = feed.scrollHeight;
}

let cachedChatHistory = [];
let lastSpotlightSeq = 0;
let lastChatSeq = -1;
async function sendAiSummary(styleId, rawPrompt) {
  const input = $("aiChatInput");
  const promptText = String(rawPrompt !== undefined ? rawPrompt : input.value).trim();
  const sMeta = (aiStyles || []).find((x) => x.id === styleId);
  const label = promptText || (sMeta ? `请按【${sMeta.name}】帮我总结当前数据` : "总结当前数据");
  if (rawPrompt === undefined || rawPrompt === null || rawPrompt === input.value) {
    input.value = "";
  }
  $("aiSendBtn").disabled = true;
  renderAiChatFeed(cachedChatHistory, label);
  try {
    await api("/api/config", "POST", collectConfigFromDOM());
    const res = await api("/api/ai-summary", "POST", { style_id: styleId, prompt: promptText, action: "send" });
    if (res && res.updated_config) {
      cfg = res.updated_config;
      if (Array.isArray(res.updated_timetable)) latestTimetable = res.updated_timetable;
      if (res.ai_meta) {
        aiStyles = Array.isArray(res.ai_meta.styles) ? res.ai_meta.styles : aiStyles;
        aiCandidateTeams = Array.isArray(res.ai_meta.candidate_teams) ? res.ai_meta.candidate_teams : aiCandidateTeams;
      }
      renderConfig();
      renderTimetable(latestTimetable);
      showToast("AI 已为您改好设置，请查看下方核对结果");
    }
    if (Array.isArray(res.history)) {
      cachedChatHistory = res.history;
      if (typeof res.chat_seq === "number") lastChatSeq = res.chat_seq;
    } else if (res.reply) {
      cachedChatHistory.push({ role: "user", text: label, time: "" });
      cachedChatHistory.push({ role: "model", text: res.reply, model: res.model, source: res.source, shot_info: res.shot_info });
    }
    if (res.shot_info) $("aiShotStatusPill").textContent = res.shot_info;
    renderAiChatFeed(cachedChatHistory);
  } catch (e) {
    renderAiChatFeed(cachedChatHistory);
    showToast("AI 总结请求失败");
  } finally {
    $("aiSendBtn").disabled = false;
  }
}

async function pollState() {
  try {
    const st = await api(`/api/state?since=${lastLogSeq}`);
    $("verBadge").textContent = `v${st.version}`;
    if (previewMemberUid) {
      updateTeamLockBanner(st.lock_status, st.queue_status, st.member_profile);
      return;
    }

    const island = $("island");
    const busy = Boolean(st.busy);
    lastBusyState = busy;
    lastNativeHotkeys = Array.isArray(st.native_hotkeys) ? st.native_hotkeys : [];
    const paused = Boolean(st.paused);
    const rec = st.recovery_prompt || {};
    const recMode = rec.mode || "running";
    const inRecovery = recMode === "waiting_fix" || recMode === "login_wps";
    const hasAlert = Boolean((!inRecovery && st.need_login_prompt) || st.error_text);

    island.classList.toggle("busy", busy);
    island.classList.toggle("paused", busy && paused);
    island.classList.toggle("recovery", busy && inRecovery);
    island.classList.toggle("expanded", busy || hasAlert);

    const dot = $("islandDot");
    dot.className = "island-dot"
      + (st.status_level === "err" ? " err"
        : st.status_level === "stopped" ? " stopped"
        : (paused || inRecovery) ? " paused"
        : st.schedule_running ? " sched" : "");

    $("stepText").textContent = cleanText(st.step_text) || "空闲中";
    const pct = Math.round((st.progress || 0) * 100);
    $("progressFill").style.width = `${pct}%`;
    $("progressPct").textContent = `${pct}%`;

    if (busy) {
      $("pauseBtn").style.display = (paused || inRecovery) ? "none" : "inline-flex";
      $("resumeBtn").style.display = paused && !inRecovery ? "inline-flex" : "none";
      $("islandHint").textContent = cleanText(rec.hint)
        || (paused ? "已暂停：忙完按【F8】或点【继续 F8】接着跑；点【取消】终止" : "按键提示：按【F8】/【暂停】让出电脑，忙完按【F8】续跑");
    }

    const recBox = $("islandRecovery");
    if (busy && (inRecovery || paused) && (rec.title || rec.detail)) {
      recBox.className = "island-recovery show" + (recMode === "login_wps" ? " login-mode" : "");
      $("recTitle").textContent = cleanText(rec.title) || "需要处理后继续";
      $("recDetail").textContent = cleanText(rec.detail) || "";
      const acts = Array.isArray(rec.actions) ? rec.actions : [];
      const sig = JSON.stringify(acts);
      if (sig !== lastRecSignature) {
        lastRecSignature = sig;
        const wrap = $("recActions");
        wrap.innerHTML = "";
        for (const act of acts) {
          const b = document.createElement("button");
          b.type = "button";
          b.className = `rec-btn ${act.style === "primary" ? "primary" : "secondary"}`;
          b.textContent = cleanText(act.label) || "继续";
          b.onclick = async () => {
            const res = await api("/api/resume", "POST", { action: act.id || "continue" });
            showToast(res.message || "已恢复执行");
            pollState();
          };
          wrap.appendChild(b);
        }
      }
    } else {
      recBox.className = "island-recovery";
      lastRecSignature = "";
    }

    const cd = st.schedule_running ? formatCountdown(st.next_run_ts, st.server_now) : "";
    $("islandTag").textContent = (st.schedule_running && cd) ? cd : (st.status_level === "err" ? "异常" : (st.status_level === "ok" ? "已完成" : "待命"));

    const alertEl = $("islandAlert");
    if (!inRecovery && st.need_login_prompt) {
      alertEl.className = "island-alert login";
      alertEl.textContent = "请在弹出的浏览器中扫码登录 WPS，完成后将自动继续";
    } else if (st.error_text) {
      alertEl.className = "island-alert err";
      alertEl.textContent = cleanText(st.error_text);
    } else {
      alertEl.className = "island-alert";
      alertEl.textContent = "";
    }

    const activeDet = (rec.error_detail && inRecovery) ? rec.error_detail : st.error_detail;
    renderErrorDiagCard(activeDet, st.ai_result, Boolean(st.ai_busy));

    $("lastRun").textContent = st.last_run_str || "—";
    $("sumNextRun").textContent = st.next_run_str && st.next_run_str !== "—" && cd ? `${st.next_run_str} (${cd})` : (st.next_run_str || "—");
    $("btnNow").disabled = busy;
    $("btnCapture").disabled = busy;

    const schedBtn = $("btnSchedule");
    schedBtn.classList.toggle("schedule-on", Boolean(st.schedule_running));
    $("schedTitle").textContent = st.schedule_running ? "值守运行中" : "自动值守";
    $("schedSub").textContent = st.schedule_running ? (cd ? `距下次 ${cd}` : "点击可停止") : "点击开启定时计划";

    if (st.log_seq < lastLogSeq) {
      allLogItems = [];
      renderLogs();
    }
    lastLogSeq = st.log_seq;
    appendLogLines(st.logs);

    if (st.update_info && st.update_info.url) {
      const safeU = safeExternalHttpUrl(st.update_info.url);
      if (safeU) {
        updateUrl = safeU;
        $("updateBtn").style.display = "flex";
        $("updateDot").style.display = "block";
        $("updateBtn").title = `发现新版 v${st.update_info.version}，点击下载`;
      }
    }
    if (st.data_brief) applyDataBriefUI(st.data_brief);
    if (typeof st.chat_seq === "number" && st.chat_seq !== lastChatSeq && Array.isArray(st.ai_history)) {
      lastChatSeq = st.chat_seq;
      cachedChatHistory = st.ai_history;
      renderAiChatFeed(cachedChatHistory);
    }
    if (st.spotlight_seq && st.spotlight_seq > lastSpotlightSeq && st.latest_spotlight) {
      lastSpotlightSeq = st.spotlight_seq;
      const sp = st.latest_spotlight;
      const spText = String(sp.reply || "").trim();
      const tail = cachedChatHistory[cachedChatHistory.length - 1];
      if (spText && (!tail || tail.text !== spText)) {
        cachedChatHistory.push({
          role: "model",
          text: spText,
          time: sp.time || "",
          source: "data_spotlight",
          shot_info: sp.summary_line || "本轮数据变动播报",
        });
        if (cachedChatHistory.length > 20) cachedChatHistory = cachedChatHistory.slice(-20);
        renderAiChatFeed(cachedChatHistory);
      }
      if (sp.summary_line) {
        $("aiShotStatusPill").textContent = sp.summary_line;
        showToast(sp.summary_line);
      }
      if (typeof setCatMascotMood === "function") setCatMascotMood("cheer", 5000);
    }
    if (st.member_profile && !previewMemberUid) {
      memberProfile = st.member_profile;
      updateMemberTopChip(st.member_profile);
    }
    if (st.sync_settings) syncSettingsState = st.sync_settings;
    if (!previewMemberUid && st.permissions) {
      applyPermissionsUI(st.permissions);
    }
    if (!previewMemberUid && st.remote_config_pulled) {
      const fresh = await api("/api/config");
      if (fresh && fresh.config) {
        cfg = fresh.config;
        latestTimetable = Array.isArray(fresh.timetable) ? fresh.timetable : latestTimetable;
        renderConfig();
        renderTimetable(latestTimetable);
        showToast("已同步最新团队标准配置");
      }
    }
    updateTeamLockBanner(st.lock_status, st.queue_status, st.member_profile);
    const hoBanner = $("handsOffBanner");
    if (hoBanner) {
      const hoOn = Boolean(busy && st.hands_off && st.hands_off_text);
      hoBanner.classList.toggle("show", hoOn);
      if (hoOn && $("handsOffTitle")) $("handsOffTitle").textContent = cleanText(st.hands_off_text) || "正在自动粘贴数据，请勿操作鼠标或键盘！";
    }
  } catch (_err) {
    // 忽略瞬时刷新抖动
  }
}

function initTheme() {
  const saved = localStorage.getItem("sheetmate_ios18_theme") || "light";
  document.documentElement.setAttribute("data-theme", saved);
  $("themeBtn").innerHTML = saved === "dark" ? SVG_SUN : SVG_MOON;
  $("themeBtn").onclick = () => {
    const cur = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", cur);
    localStorage.setItem("sheetmate_ios18_theme", cur);
    $("themeBtn").innerHTML = cur === "dark" ? SVG_SUN : SVG_MOON;
  };
}

function bindEvents() {
  for (const btn of $("tabbar").querySelectorAll(".tab-btn")) {
    btn.onclick = () => switchTab(btn.dataset.tab);
  }
  for (const el of document.querySelectorAll(".cc-tile, .mini-stat-card")) {
    el.addEventListener("mousemove", (ev) => {
      const r = el.getBoundingClientRect();
      el.style.setProperty("--mx", `${ev.clientX - r.left}px`);
      el.style.setProperty("--my", `${ev.clientY - r.top}px`);
    });
  }
  for (const el of document.querySelectorAll("[data-jump]")) {
    el.onclick = () => switchTab(el.dataset.jump);
  }
  $("sumPresetsRow").onclick = () => {
    switchTab("dingtalk");
    setTimeout(() => {
      const sec = $("summaryPresetsSection");
      if (sec && sec.scrollIntoView) sec.scrollIntoView({ behavior: "smooth", block: "start" });
    }, 60);
  };
  if ($("homeAddCellBtn")) {
    $("homeAddCellBtn").onclick = () => {
      switchTab("dingtalk");
      const sp = ensureSummaryPresetsStruct();
      sp.enabled = true;
      $("spEnabled").checked = true;
      $("spAccordion").classList.add("open");
      setTimeout(() => {
        const sec = $("summaryPresetsSection");
        if (sec && sec.scrollIntoView) sec.scrollIntoView({ behavior: "smooth", block: "start" });
      }, 60);
    };
  }
  for (const b of document.querySelectorAll("#aiQuickQaBar [data-ai-q]")) {
    b.onclick = () => sendAiSummary("custom", b.dataset.aiQ || b.textContent);
  }
  for (const qb of document.querySelectorAll("#presetQuickAddBar [data-qcell]")) {
    qb.onclick = () => {
      const sp = ensureSummaryPresetsStruct();
      sp.enabled = true;
      $("spEnabled").checked = true;
      $("spAccordion").classList.add("open");
      const qcell = (qb.dataset.qcell || "").toUpperCase();
      const qact = qb.dataset.qact || "value";
      const qval = qb.dataset.qval || "";
      const qlabel = qb.dataset.qlabel || "";
      const existIdx = qcell ? sp.items.findIndex((it) => String(it.cell || "").toUpperCase() === qcell) : -1;
      if (existIdx >= 0) {
        sp.items[existIdx].enabled = true;
        renderSummaryPresets();
        const rows = $("summaryPresetsList").querySelectorAll(".rule-card-row");
        if (rows[existIdx]) rows[existIdx].scrollIntoView({ behavior: "smooth", block: "nearest" });
        showToast(`格位 ${qcell} 已存在，已为您高亮定位`);
        scheduleSave(true);
        return;
      }
      sp.items.push({
        enabled: true,
        sheet: "数据汇总",
        cell: qcell,
        label: qlabel,
        action: qact,
        date_fmt: "YYYY年M月D日",
        time_fmt: "full",
        day_offset: 0,
        cutoff_enabled: false,
        cutoff_hm: "09:00",
        before_cutoff_offset: -1,
        only_after_paste: false,
        value: qval,
      });
      renderSummaryPresets();
      scheduleSave(false);
      const cells = $("summaryPresetsList").querySelectorAll(".cell-badge-input");
      if (cells.length) {
        const targetInp = cells[cells.length - 1];
        targetInp.scrollIntoView({ behavior: "smooth", block: "nearest" });
        targetInp.focus();
      }
    };
  }

  document.addEventListener("keydown", async (e) => {
    if (!lastBusyState || e.repeat) return;
    if (e.key === "F8" || e.key === "F9") {
      if (lastNativeHotkeys.includes(e.key)) return;
      e.preventDefault();
      const res = await api("/api/hotkey", "POST", { slot: e.key.toLowerCase() });
      if (res && res.message) showToast(res.message);
      pollState();
    }
  });

  // 结构化错误诊断按钮
  $("diagDismissBtn").onclick = () => {
    if (currentErrorDetail) diagDismissedSig = `${currentErrorDetail.code}|${currentErrorDetail.raw_error || ""}`;
    $("errorDiagCard").classList.remove("show");
  };
  const copyAgFn = async () => {
    const txt = ($("agPromptPre") && $("agPromptPre").value) || lastTellDevPrompt || (currentErrorDetail && currentErrorDetail.tell_dev) || "";
    if (!txt) return;
    try {
      await navigator.clipboard.writeText(txt);
      showToast("已复制整段 Antigravity 改代码指令，可直接粘贴发送");
    } catch (_e) { showToast("复制失败，请手动选中下方文本框内容"); }
  };
  $("copyTellDevBtn").onclick = copyAgFn;
  if ($("copyAgBoxBtn")) $("copyAgBoxBtn").onclick = copyAgFn;
  if ($("applyAiTuneBtn")) {
    $("applyAiTuneBtn").onclick = async () => {
      const btn = $("applyAiTuneBtn");
      btn.disabled = true;
      try {
        const res = await api("/api/ai-apply-tuning", "POST", {});
        showToast(res.message || "已完成自调");
        if (res.config) {
          cfg = res.config;
          renderConfig();
        }
        pollState();
      } catch (_e) {
        showToast("自调应用失败，请稍后重试");
      } finally {
        btn.disabled = false;
      }
    };
  }
  $("autoHealNowBtn").onclick = async () => {
    const panel = $("diagAiPanel");
    lastAiPanelSig = "";
    panel.textContent = "智能管家正在截屏观察现场并自动调试修复，请稍候…";
    panel.classList.add("show");
    $("autoHealNowBtn").disabled = true;
    try {
      const res = await api("/api/ai-auto-heal", "POST", {});
      const tag = (res.source === "gemini_vision" || res.source === "gemini" || res.source === "ai_vision" || res.source === "ai_cloud")
        ? "智能视觉调试"
        : "AI 自动调试引擎";
      panel.innerHTML = `<div class="md-h3">${escapeHtmlStr(tag)}</div>${formatSafeMarkdown(res.reply || "")}`;
      if (res.resumed) showToast("智能伙伴已自动调整现场并恢复运行");
      pollState();
    } catch (_e) {
      panel.textContent = "AI 自动调试请求失败，请检查网络连接";
    } finally {
      $("autoHealNowBtn").disabled = false;
    }
  };
  $("askAiErrorBtn").onclick = async () => {
    const panel = $("diagAiPanel");
    lastAiPanelSig = "";
    panel.textContent = "智能管家正在对照历次运行日志整理分析建议，请稍候…";
    panel.classList.add("show");
    try {
      const res = await api("/api/ai-diagnose", "POST", { mode: "diagnose" });
      const tag = (res.source === "gemini" || res.source === "ai_cloud") ? "智能历史复盘" : "专家历史复盘";
      panel.innerHTML = `<div class="md-h3">${escapeHtmlStr(tag)}</div>${formatSafeMarkdown(res.reply || "")}`;
      renderErrorDiagCard(currentErrorDetail, res, false);
    } catch (_e) {
      panel.textContent = "AI 诊断请求失败，请检查网络连接";
    }
  };

  // Tab 1 右侧 AI 总结工作台
  $("aiSendBtn").onclick = () => sendAiSummary("custom", $("aiChatInput").value);
  $("aiChatInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendAiSummary("custom", $("aiChatInput").value);
    }
  });
  $("aiClearChatBtn").onclick = async () => {
    await api("/api/ai-summary", "POST", { action: "clear" });
    cachedChatHistory = [];
    renderAiChatFeed([]);
    showToast("对话记录已清空");
  };
  $("aiSuggestBtn").onclick = async () => {
    $("aiSendBtn").disabled = true;
    renderAiChatFeed(cachedChatHistory, "请审核我当前的排期、表格日期和钉钉配置，给几条防踩坑优化建议");
    try {
      await api("/api/config", "POST", collectConfigFromDOM());
      const res = await api("/api/ai-diagnose", "POST", { mode: "suggest" });
      cachedChatHistory.push({ role: "user", text: "请审核我当前的排期、表格日期和钉钉配置，给几条优化建议", time: "" });
      cachedChatHistory.push({ role: "model", text: res.reply || "暂无建议", time: res.time || "", model: res.model || "", source: res.source || "" });
      renderAiChatFeed(cachedChatHistory);
    } finally {
      $("aiSendBtn").disabled = false;
    }
  };

  // Tab 2 钉钉文案时间格式与偏移控制
  for (const b of $("dtTimeModeSeg").querySelectorAll(".mini-seg-btn")) {
    b.onclick = () => { cfg.dt_time_mode = b.dataset.val; renderDingtalkDatePanel(); scheduleSave(true); };
  }
  for (const b of $("dtDateFmtSeg").querySelectorAll(".mini-seg-btn")) {
    b.onclick = () => { cfg.dt_date_fmt = b.dataset.val; renderDingtalkDatePanel(); scheduleSave(true); };
  }
  for (const b of $("dtOffsetQuickSeg").querySelectorAll(".mini-seg-btn")) {
    b.onclick = () => { cfg.dt_day_offset = Number(b.dataset.off); renderDingtalkDatePanel(); scheduleSave(true); };
  }
  $("dtOffMinusBtn").onclick = () => { cfg.dt_day_offset = clampOff((cfg.dt_day_offset || 0) - 1, 0); renderDingtalkDatePanel(); scheduleSave(true); };
  $("dtOffPlusBtn").onclick = () => { cfg.dt_day_offset = clampOff((cfg.dt_day_offset || 0) + 1, 0); renderDingtalkDatePanel(); scheduleSave(true); };
  $("dtCutoffEnabled").addEventListener("change", () => {
    cfg.dt_cutoff_enabled = $("dtCutoffEnabled").checked;
    renderDingtalkDatePanel();
    scheduleSave(true);
  });
  $("dtCutoffHmBtn").onclick = () => {
    const [h, m] = parseHmStr(cfg.dt_cutoff_hm || "09:00", 9, 0);
    openPicker({
      title: "消息日期分界时间点", mode: "time", h, m,
      onConfirm: (nh, nm) => { cfg.dt_cutoff_hm = hmStr(nh, nm); renderDingtalkDatePanel(); scheduleSave(true); },
    });
  };
  $("dtCutOffMinus").onclick = () => { cfg.dt_before_cutoff_offset = clampOff(clampOff(cfg.dt_before_cutoff_offset, -1) - 1, -1); renderDingtalkDatePanel(); scheduleSave(true); };
  $("dtCutOffPlus").onclick = () => { cfg.dt_before_cutoff_offset = clampOff(clampOff(cfg.dt_before_cutoff_offset, -1) + 1, -1); renderDingtalkDatePanel(); scheduleSave(true); };

  if ($("dtPasteDelayQuickSeg")) {
    for (const b of $("dtPasteDelayQuickSeg").querySelectorAll(".mini-seg-btn")) {
      b.onclick = () => { cfg.dt_paste_delay_sec = clampPasteDelay(b.dataset.sec, 3); renderDtPasteDelay(); scheduleSave(true); };
    }
  }
  if ($("dtPasteDelayMinus")) {
    $("dtPasteDelayMinus").onclick = () => { cfg.dt_paste_delay_sec = clampPasteDelay(clampPasteDelay(cfg.dt_paste_delay_sec, 3) - 1, 3); renderDtPasteDelay(); scheduleSave(true); };
  }
  if ($("dtPasteDelayPlus")) {
    $("dtPasteDelayPlus").onclick = () => { cfg.dt_paste_delay_sec = clampPasteDelay(clampPasteDelay(cfg.dt_paste_delay_sec, 3) + 1, 3); renderDtPasteDelay(); scheduleSave(true); };
  }
  if ($("dtComposeOrderSeg")) {
    for (const b of $("dtComposeOrderSeg").querySelectorAll(".mini-seg-btn")) {
      b.onclick = () => { ensurePipelineTuningStruct().compose_step_order = b.dataset.val; renderPipelineTuning(); scheduleSave(true); };
    }
  }
  if ($("dtSilentOrderSeg")) {
    for (const b of $("dtSilentOrderSeg").querySelectorAll(".mini-seg-btn")) {
      b.onclick = () => { ensurePipelineTuningStruct().silent_step_order = b.dataset.val; renderPipelineTuning(); scheduleSave(true); };
    }
  }

  $("aiAttachSummary").addEventListener("change", () => {
    ensureAiStruct().auto_attach_summary = $("aiAttachSummary").checked;
    $("aiAttachStyleRow").style.display = $("aiAttachSummary").checked ? "flex" : "none";
    scheduleSave(true);
  });

  // Tab 3 周期与自定义星期
  for (const btn of $("dayModeSeg").querySelectorAll(".seg-btn")) {
    btn.onclick = () => { cfg.day_mode = btn.dataset.val; applySegmented("dayModeSeg", "dayModeThumb", cfg.day_mode); renderCustomDays(); scheduleSave(true); };
  }
  for (const btn of $("customDaysGrid").querySelectorAll(".wday-btn")) {
    btn.onclick = () => {
      const d = Number(btn.dataset.day);
      const cur = new Set(Array.isArray(cfg.custom_days) ? cfg.custom_days : []);
      if (cur.has(d)) cur.delete(d); else cur.add(d);
      cfg.custom_days = [...cur].sort((a, b) => a - b);
      renderCustomDays();
      scheduleSave(true);
    };
  }

  $("addTargetBtn").onclick = () => {
    if (!Array.isArray(cfg.dt_targets)) cfg.dt_targets = [];
    cfg.dt_targets.push({ name: "", enabled: true });
    renderDtTargets();
    const inputs = $("dtTargetsList").querySelectorAll("input[type='text']");
    if (inputs.length) inputs[inputs.length - 1].focus();
    updateSummaries();
  };
  $("utEnabled").addEventListener("change", () => {
    const ut = ensureUpdateTimeStruct();
    ut.enabled = $("utEnabled").checked;
    renderUpdateTimeControls();
    scheduleSave(true);
  });
  $("utCellInput").oninput = () => {
    const ut = ensureUpdateTimeStruct();
    ut.cell = $("utCellInput").value.replace(/\s+/g, "").toUpperCase();
    scheduleSave(false);
  };
  $("utCellInput").onblur = () => {
    const ut = ensureUpdateTimeStruct();
    ut.cell = (ut.cell || "S4").toUpperCase();
    renderUpdateTimeControls();
  };
  for (const b of $("utTimingSeg").querySelectorAll(".mini-seg-btn")) {
    b.onclick = () => {
      ensureUpdateTimeStruct().only_after_paste = b.dataset.val !== "always";
      renderUpdateTimeControls();
      scheduleSave(true);
    };
  }
  $("spEnabled").addEventListener("change", () => {
    ensureSummaryPresetsStruct();
    cfg.summary_presets.enabled = $("spEnabled").checked;
    $("spAccordion").classList.toggle("open", cfg.summary_presets.enabled);
    scheduleSave(true);
  });
  $("addPresetBtn").onclick = () => {
    const sp = ensureSummaryPresetsStruct();
    sp.enabled = true;
    $("spEnabled").checked = true;
    $("spAccordion").classList.add("open");
    sp.items.push({ enabled: true, sheet: "数据汇总", cell: "", label: "", action: "value", date_fmt: "YYYY年M月D日", time_fmt: "full", day_offset: 0, cutoff_enabled: false, cutoff_hm: "09:00", before_cutoff_offset: -1, only_after_paste: false, value: "" });
    renderSummaryPresets();
    const cells = $("summaryPresetsList").querySelectorAll(".cell-badge-input");
    if (cells.length) cells[cells.length - 1].focus();
    updateSummaries();
  };
  $("resetPresetsBtn").onclick = async () => {
    const ok = await iosAlert("恢复汇总表默认校准规则", "将恢复为：D2 自动日期、清空 D4/E4、L3 设为 7 个计算小组、L4 设为慕杰组。", { okText: "恢复默认", cancelText: "取消" });
    if (!ok) return;
    cfg.summary_presets = { enabled: true, items: JSON.parse(JSON.stringify(DEFAULT_SUMMARY_PRESETS)) };
    $("spEnabled").checked = true;
    $("spAccordion").classList.add("open");
    renderSummaryPresets();
    scheduleSave(true);
  };
  $("checkDingBtn").onclick = async () => {
    $("dingCheckVal").textContent = "检测中…";
    try {
      const r = await api("/api/check-dingtalk", "POST");
      $("dingCheckVal").textContent = r.ok ? "环境正常" : "需排查";
      await iosAlert(r.ok ? "钉钉环境检测通过" : "钉钉环境待排查", r.summary || "检测完成");
    } catch (_e) { $("dingCheckVal").textContent = "检测失败"; }
  };
  $("openShotsBtn").onclick = async () => {
    const r = await api("/api/open-shots", "POST");
    showToast(r.message || "已尝试打开文件夹");
  };
  $("resetAllPointsBtn").onclick = async () => {
    const ok = await iosAlert("重置全部单点设置", "将清空排期表中所有时间节点的单独覆盖，恢复跟随全局规则。", { okText: "全部重置", cancelText: "取消", danger: true });
    if (!ok) return;
    cfg.point_overrides = {};
    scheduleSave(true);
  };
  $("addSuffixRuleBtn").onclick = () => {
    if (!Array.isArray(cfg.suffix_rules)) cfg.suffix_rules = [];
    cfg.suffix_rules.push({ enabled: true, start: "12:00", end: "13:00", suffix: "午间数据" });
    renderSuffixRules();
    scheduleSave(true);
  };
  $("addCpWinBtn").onclick = () => {
    if (!cfg.capture_plan) cfg.capture_plan = { enabled: true, windows: [] };
    if (!Array.isArray(cfg.capture_plan.windows)) cfg.capture_plan.windows = [];
    cfg.capture_plan.windows.push({ enabled: true, start: "09:00", end: "18:00" });
    renderCaptureWindows();
    scheduleSave(true);
  };

  for (const id of [
    "nightEnabled", "preventSleep", "enableCapture", "enableDingtalk",
    "dtClearBeforeSend", "dtSilentSend", "dtAutoSend", "dtMinimizeAfterSend",
    "openWpsAfterRun", "saveShotHistory", "aiEnabled", "aiAutoDiagnose", "aiAutoHeal"
  ]) {
    $(id).addEventListener("change", () => scheduleSave(true));
  }
  $("cpEnabled").addEventListener("change", () => {
    $("cpAccordion").classList.toggle("open", $("cpEnabled").checked);
    scheduleSave(true);
  });
  for (const id of ["dtSuffix", "wpsUrl", "luyaUser", "luyaPass", "aiCustomPrompt"]) {
    $(id).addEventListener("input", () => scheduleSave(false));
  }
  $("clearSuffixBtn").onclick = () => { $("dtSuffix").value = ""; scheduleSave(true); };

  $("dayStartBtn").onclick = () => openPicker({ title: "白天开始时间", mode: "time", h: cfg.day.sh, m: cfg.day.sm, onConfirm: (h, m) => { cfg.day.sh = h; cfg.day.sm = m; scheduleSave(true); } });
  $("dayEndBtn").onclick = () => openPicker({ title: "白天结束时间", mode: "time", h: cfg.day.eh, m: cfg.day.em, onConfirm: (h, m) => { cfg.day.eh = h; cfg.day.em = m; scheduleSave(true); } });
  $("dayGapBtn").onclick = () => openPicker({ title: "白天执行间隔", mode: "gap", h: cfg.day.ih, m: cfg.day.im, onConfirm: (h, m) => { cfg.day.ih = h; cfg.day.im = m; scheduleSave(true); } });
  $("nightStartBtn").onclick = () => openPicker({ title: "晚上开始时间", mode: "time", h: cfg.night.sh, m: cfg.night.sm, onConfirm: (h, m) => { cfg.night.sh = h; cfg.night.sm = m; scheduleSave(true); } });
  $("nightEndBtn").onclick = () => openPicker({ title: "晚上结束时间", mode: "time", h: cfg.night.eh, m: cfg.night.em, onConfirm: (h, m) => { cfg.night.eh = h; cfg.night.em = m; scheduleSave(true); } });
  $("nightGapBtn").onclick = () => openPicker({ title: "晚上执行间隔", mode: "gap", h: cfg.night.ih, m: cfg.night.im, onConfirm: (h, m) => { cfg.night.ih = h; cfg.night.im = m; scheduleSave(true); } });

  $("pickerCancel").onclick = () => $("pickerMask").classList.remove("show");
  $("pickerMask").onclick = (e) => { if (e.target === $("pickerMask")) $("pickerMask").classList.remove("show"); };
  $("pickerDone").onclick = () => {
    if (!pickerState) return;
    const { mode, h, m, onConfirm } = pickerState;
    if (mode === "gap" && h === 0 && m === 0) { showToast("执行间隔不能为 0 分钟"); return; }
    $("pickerMask").classList.remove("show");
    onConfirm(h, m);
  };

  // 单点设置弹窗事件（点击即刻刷新弹窗本身与背后的排期列表）
  for (const btn of $("pointModeSeg").querySelectorAll(".seg-btn")) {
    btn.onclick = () => {
      editingPointMode = btn.dataset.val;
      applySegmented("pointModeSeg", "pointModeThumb", editingPointMode);
      _refreshPointModalPreview();
      _syncEditingPointLive(true);
    };
  }
  for (const btn of $("pointSilentSeg").querySelectorAll(".seg-btn")) {
    btn.onclick = () => {
      editingPointSilent = btn.dataset.val;
      applySegmented("pointSilentSeg", "pointSilentThumb", editingPointSilent);
      _refreshPointModalPreview();
      _syncEditingPointLive(true);
    };
  }
  for (const b of $("pointSheetOffSeg").querySelectorAll(".mini-seg-btn")) {
    b.onclick = () => {
      editingPtSheetOff = b.dataset.val === "default" ? null : Number(b.dataset.val);
      _refreshPointModalPreview();
      _syncEditingPointLive(true);
    };
  }
  $("ptSheetMinusBtn").onclick = () => {
    editingPtSheetOff = clampOff((editingPtSheetOff === null ? 0 : editingPtSheetOff) - 1, -1);
    _refreshPointModalPreview();
    _syncEditingPointLive(true);
  };
  $("ptSheetPlusBtn").onclick = () => {
    editingPtSheetOff = clampOff((editingPtSheetOff === null ? 0 : editingPtSheetOff) + 1, 0);
    _refreshPointModalPreview();
    _syncEditingPointLive(true);
  };

  for (const b of $("pointMsgModeSeg").querySelectorAll(".mini-seg-btn")) {
    b.onclick = () => {
      editingPtMsgMode = b.dataset.val;
      _refreshPointModalPreview();
      _syncEditingPointLive(true);
    };
  }
  for (const b of $("pointMsgOffSeg").querySelectorAll(".mini-seg-btn")) {
    b.onclick = () => {
      editingPtMsgOff = b.dataset.val === "default" ? null : Number(b.dataset.val);
      _refreshPointModalPreview();
      _syncEditingPointLive(true);
    };
  }
  $("ptMsgMinusBtn").onclick = () => {
    editingPtMsgOff = clampOff((editingPtMsgOff === null ? 0 : editingPtMsgOff) - 1, -1);
    _refreshPointModalPreview();
    _syncEditingPointLive(true);
  };
  $("ptMsgPlusBtn").onclick = () => {
    editingPtMsgOff = clampOff((editingPtMsgOff === null ? 0 : editingPtMsgOff) + 1, 0);
    _refreshPointModalPreview();
    _syncEditingPointLive(true);
  };

  $("pointCustomSufOn").addEventListener("change", () => {
    $("pointCustomSufWrap").style.display = $("pointCustomSufOn").checked ? "flex" : "none";
    _refreshPointModalPreview();
    _syncEditingPointLive(true);
  });
  $("pointCustomSufInput").addEventListener("input", () => {
    _refreshPointModalPreview();
    _syncEditingPointLive(false);
  });
  $("pointCancel").onclick = () => {
    if (editingPointHm) {
      if (!cfg.point_overrides) cfg.point_overrides = {};
      if (editingPointOrigOv && !_isPointOverrideEmpty(editingPointOrigOv)) {
        cfg.point_overrides[editingPointHm] = JSON.parse(JSON.stringify(editingPointOrigOv));
      } else {
        delete cfg.point_overrides[editingPointHm];
      }
      _applyOvToLiveTimetable(editingPointHm, cfg.point_overrides[editingPointHm] || null);
      scheduleSave(true);
    }
    editingPointHm = "";
    editingPointOrigOv = null;
    $("pointMask").classList.remove("show");
  };
  const finishPointModal = () => {
    if (editingPointHm) _syncEditingPointLive(true);
    editingPointHm = "";
    editingPointOrigOv = null;
    $("pointMask").classList.remove("show");
  };
  $("pointMask").onclick = (e) => { if (e.target === $("pointMask")) finishPointModal(); };
  $("pointDone").onclick = finishPointModal;
  $("pointResetBtn").onclick = () => {
    if (editingPointHm && cfg.point_overrides) {
      delete cfg.point_overrides[editingPointHm];
      _applyOvToLiveTimetable(editingPointHm, null);
    }
    editingPointHm = "";
    editingPointOrigOv = null;
    $("pointMask").classList.remove("show");
    scheduleSave(true);
  };

  $("togglePassBtn").innerHTML = SVG_EYE;
  $("togglePassBtn").onclick = () => {
    const inp = $("luyaPass");
    const show = inp.type === "password";
    inp.type = show ? "text" : "password";
    $("togglePassBtn").innerHTML = show ? SVG_EYE_OFF : SVG_EYE;
  };

  $("btnNow").onclick = async () => {
    await api("/api/config", "POST", collectConfigFromDOM());
    const r = await api("/api/run-now", "POST");
    if (!r.ok && r.locked) {
      openLockWaitModal(r.locked_by, "full");
      return;
    }
    showToast(r.message);
    pollState();
  };
  $("btnCapture").onclick = async () => {
    await api("/api/config", "POST", collectConfigFromDOM());
    const r = await api("/api/run-capture", "POST");
    if (!r.ok && r.locked) {
      openLockWaitModal(r.locked_by, "capture");
      return;
    }
    showToast(r.message);
    pollState();
  };
  $("btnSchedule").onclick = async () => {
    await api("/api/config", "POST", collectConfigFromDOM());
    const r = await api("/api/schedule", "POST");
    if (!r.ok) { await iosAlert("无法开启计划", r.message); return; }
    showToast(r.message);
    pollState();
  };
  $("pauseBtn").onclick = async () => {
    $("pauseBtn").disabled = true;
    $("island").classList.add("paused");
    $("stepText").textContent = "已请求暂停，请稍候…";
    try {
      const r = await api("/api/pause", "POST");
      showToast(r.message);
      await pollState();
    } finally { $("pauseBtn").disabled = false; }
  };
  $("resumeBtn").onclick = async () => {
    $("resumeBtn").disabled = true;
    $("island").classList.remove("paused");
    $("stepText").textContent = "正在恢复运行…";
    try {
      const r = await api("/api/resume", "POST", { action: "continue" });
      showToast(r.message);
      await pollState();
    } finally { $("resumeBtn").disabled = false; }
  };
  $("stopBtn").onclick = async () => {
    $("stopBtn").disabled = true;
    $("island").classList.remove("busy", "paused", "recovery");
    $("stepText").textContent = "已手动取消";
    try {
      const r = await api("/api/stop", "POST");
      showToast(r.message);
      await pollState();
    } finally { $("stopBtn").disabled = false; }
  };
  $("guideToggleRow").onclick = () => {
    const body = $("guideBody");
    const open = body.classList.toggle("open");
    $("guideChevron").style.transform = open ? "rotate(90deg)" : "rotate(0deg)";
    $("guideSummarySub").textContent = open ? "点击收起说明" : "点击展开速查指南";
  };

  $("filterLogAllBtn").onclick = () => {
    logFilterErrOnly = false;
    $("filterLogAllBtn").classList.add("active");
    $("filterLogErrBtn").classList.remove("active");
    renderLogs();
  };
  $("filterLogErrBtn").onclick = () => {
    logFilterErrOnly = true;
    $("filterLogErrBtn").classList.add("active");
    $("filterLogAllBtn").classList.remove("active");
    renderLogs();
  };
  $("autoScrollBtn").onclick = () => {
    autoScroll = true;
    $("autoScrollBtn").classList.add("active");
    scrollToLogBottom(true);
    showToast("已滚到最新日志底部");
  };
  if ($("logBox")) {
    $("logBox").addEventListener("scroll", () => {
      const b = $("logBox");
      const dist = b.scrollHeight - b.scrollTop - b.clientHeight;
      if (dist > 36 && autoScroll) {
        autoScroll = false;
        $("autoScrollBtn").classList.remove("active");
      } else if (dist <= 12 && !autoScroll) {
        autoScroll = true;
        $("autoScrollBtn").classList.add("active");
      }
    });
  }
  $("copyLogBtn").onclick = async () => {
    try { await navigator.clipboard.writeText($("logBox").innerText || ""); showToast("已复制运行日志"); }
    catch (_e) { showToast("复制失败，请手动选中"); }
  };
  $("clearLogBtn").onclick = async () => {
    await api("/api/clear-logs", "POST");
    allLogItems = [];
    lastLogSeq = 0;
    renderLogs();
    showToast("日志已清空");
  };

  $("resetBtn").onclick = async () => {
    const ok = await iosAlert("恢复默认设置", "当前时段、执行间隔、预设校准与钉钉群设置将重置为默认值。", { okText: "恢复默认", cancelText: "取消", danger: true });
    if (!ok) return;
    const res = await api("/api/reset", "POST");
    cfg = res.config;
    latestTimetable = Array.isArray(res.timetable) ? res.timetable : [];
    renderConfig();
    renderTimetable(latestTimetable);
    showToast("已恢复默认设置");
  };

  $("updateBtn").onclick = async () => {
    const safeU = safeExternalHttpUrl(updateUrl);
    if (!safeU) return;
    const ok = await iosAlert("发现新版本", "检测到新版 SheetMate，是否前往 GitHub 下载？", { okText: "立即前往", cancelText: "稍后" });
    if (ok) window.open(safeU, "_blank", "noopener,noreferrer");
  };

  window.addEventListener("pagehide", () => {
    try { navigator.sendBeacon(`/api/close?token=${encodeURIComponent(TOKEN)}`, "{}"); } catch (_e) {}
  });

  bindTeamCollabAndAdminEvents();
}

function updateMemberTopChip(prof) {
  if (!prof) return;
  if ($("memberMiniName")) {
    const nick = String(prof.nickname || "").trim();
    $("memberMiniName").textContent = nick || (prof.initialized ? "小伙伴" : "设置昵称");
  }
  if ($("memberMiniAvatar")) $("memberMiniAvatar").src = safeAvatarUrl(prof.avatar);
}

function applyPermissionsUI(perms) {
  const p = perms && typeof perms === "object" ? perms : {};
  if ($("btnNow")) $("btnNow").disabled = Boolean(lastBusyState || p.can_run === false);
  if ($("btnCapture")) $("btnCapture").disabled = Boolean(lastBusyState || p.can_run === false);
  if ($("aiSendBtn")) $("aiSendBtn").disabled = (p.can_use_ai === false);
  if ($("aiChatInput")) $("aiChatInput").disabled = (p.can_use_ai === false);
}

function updateTeamLockBanner(lockSt, queueSt, prof) {
  const banner = $("teamLockBanner");
  if (!banner) return;
  const myUid = (prof && prof.user_id) || (memberProfile && memberProfile.user_id) || "";
  const lck = (lockSt && lockSt.lock) || {};
  const lockedByOther = Boolean(lockSt && lockSt.locked && lck.holder_id && lck.holder_id !== myUid);
  const inQueue = Boolean(queueSt && queueSt.in_queue);
  if (!lockedByOther && !inQueue) {
    banner.classList.remove("show");
    return;
  }
  banner.classList.add("show");
  if ($("lockHolderAvatar")) $("lockHolderAvatar").src = safeAvatarUrl(lck.holder_avatar || (prof && prof.avatar));
  const elapsed = lck.acquired_ts ? Math.max(0, Math.round(Date.now() / 1000 - lck.acquired_ts)) : 0;
  if (lockedByOther) {
    $("lockHolderTitle").textContent = `${lck.holder_name || "同事"} 正在占用运行锁（已跑 ${elapsed} 秒）`;
    $("lockHolderSub").textContent = `${lck.mode_label || "任务"} · ${lck.step_text || "运行中"}`;
  } else if (inQueue) {
    $("lockHolderTitle").textContent = `您当前正在排队第 ${queueSt.position || 1} 位`;
    $("lockHolderSub").textContent = "等待当前占用释放后会自动开始执行您的任务";
  }
  $("bannerJoinQueueBtn").style.display = (!inQueue && lockedByOther) ? "inline-flex" : "none";
  $("bannerLeaveQueueBtn").style.display = inQueue ? "inline-flex" : "none";
}

function openLockWaitModal(holder, runType) {
  pendingLockRunType = runType || "full";
  const h = holder || {};
  if ($("lockModalAvatar")) $("lockModalAvatar").src = safeAvatarUrl(h.holder_avatar);
  $("lockModalHolderName").textContent = `${h.holder_name || "同事"} 正在跑表`;
  $("lockModalHolderStep").textContent = `${h.mode_label || "全流程"}：${h.step_text || "正在处理表格"}`;
  $("lockWaitModalMask").classList.add("show");
}

async function applyFocusTeamUpdate(rawTeam, toastMsg = "") {
  const val = String(rawTeam || "").trim();
  const ai = ensureAiStruct();
  ai.focus_team = val;
  if ($("aiFocusTeamInput")) $("aiFocusTeamInput").value = val;
  const res = await api("/api/config", "POST", collectConfigFromDOM());
  if (res && res.ai_meta) {
    aiStyles = Array.isArray(res.ai_meta.styles) ? res.ai_meta.styles : aiStyles;
    aiCandidateTeams = Array.isArray(res.ai_meta.candidate_teams) ? res.ai_meta.candidate_teams : aiCandidateTeams;
  }
  renderAiControls();
  if (toastMsg) showToast(toastMsg);
}

async function openProfileModal() {
  const data = await api("/api/profile");
  if (data && data.profile) memberProfile = data.profile;
  if (Array.isArray(data.presets)) avatarPresets = data.presets;
  if (data && data.sync_settings) syncSettingsState = data.sync_settings;
  const prof = memberProfile || {};
  selectedAvatarUrl = safeAvatarUrl(prof.avatar) || safeAvatarUrl(avatarPresets[0] && avatarPresets[0].url);
  $("profileNicknameInput").value = prof.nickname || "";
  $("profileAvatarPreview").src = selectedAvatarUrl;

  const grid = $("avatarPresetsGrid");
  grid.innerHTML = "";
  for (const av of avatarPresets) {
    const safeUrl = safeAvatarUrl(av.url);
    const item = document.createElement("button");
    item.type = "button";
    item.className = "avatar-preset-opt" + (selectedAvatarUrl === safeUrl ? " active" : "");
    item.innerHTML = `<img src="${safeUrl}" alt="" /><span>${escapeHtmlStr(av.name)}</span>`;
    item.onclick = () => {
      selectedAvatarUrl = safeUrl;
      $("profileAvatarPreview").src = safeUrl;
      for (const ch of grid.querySelectorAll(".avatar-preset-opt")) ch.classList.remove("active");
      item.classList.add("active");
    };
    grid.appendChild(item);
  }

  $("profileModalMask").classList.add("show");
}

function resizeUploadedAvatar(file) {
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = () => {
      const img = new Image();
      img.onload = () => {
        const size = 96;
        const c = document.createElement("canvas");
        c.width = size;
        c.height = size;
        const ctx = c.getContext("2d");
        const side = Math.min(img.width, img.height);
        const sx = Math.max(0, (img.width - side) / 2);
        const sy = Math.max(0, (img.height - side) / 2);
        ctx.drawImage(img, sx, sy, side, side, 0, 0, size, size);
        resolve(c.toDataURL("image/jpeg", 0.85));
      };
      img.src = String(reader.result || "");
    };
    reader.readAsDataURL(file);
  });
}

function fmtLocalYmdHm(d) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function openAdminEntry() {
  if (adminToken) {
    $("adminConsoleMask").classList.add("show");
    loadAdminDashboard();
  } else {
    $("adminPasscodeInput").value = "";
    $("adminPasswordInput").value = "";
    $("adminAuthMask").classList.add("show");
    setTimeout(() => $("adminPasscodeInput").focus(), 60);
  }
}

async function loadAdminDashboard() {
  if (!adminToken) return;
  const st = encodeURIComponent(($("statStartInput") && $("statStartInput").value.trim()) || "");
  const ed = encodeURIComponent(($("statEndInput") && $("statEndInput").value.trim()) || "");
  const uid = encodeURIComponent(adminStatMemberId || "");
  const res = await api(`/api/admin/dashboard?start_time=${st}&end_time=${ed}&user_id=${uid}`, "GET", null, { "X-Admin-Token": adminToken });
  if (!res || res.auth_required) {
    adminToken = "";
    sessionStorage.removeItem("sheetmate_admin_tok");
    $("adminConsoleMask").classList.remove("show");
    $("adminAuthMask").classList.add("show");
    return;
  }
  adminDashData = res;
  renderAdminDashboard(res);
}

function renderAdminDashboard(d) {
  if (!d) return;
  const stats = d.stats || {};
  const members = Array.isArray(d.members) ? d.members : [];

  // 1. 统计看板成员筛选胶囊
  const mSeg = $("statMemberFilterSeg");
  if (mSeg) {
    mSeg.innerHTML = "";
    const addMF = (val, label) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "mini-seg-btn" + (adminStatMemberId === val ? " active" : "");
      b.textContent = label;
      b.onclick = () => { adminStatMemberId = val; loadAdminDashboard(); };
      mSeg.appendChild(b);
    };
    addMF("", "全员");
    for (const m of members) {
      addMF(m.user_id, m.admin_remark ? `${m.display_name}(${m.admin_remark})` : (m.display_name || m.member_code));
    }
  }

  $("adminKpiGrid").innerHTML = [
    ["总运行次", Number(stats.total_runs) || 0],
    ["成功次", Number(stats.ok_runs) || 0],
    ["失败次", Number(stats.err_runs) || 0],
    ["成功率", `${Number(stats.success_rate) || 0}%`],
    ["均耗时", `${Number(stats.avg_duration_sec) || 0}秒`],
    ["活跃人数", `${Number(stats.active_users_in_range) || 0}/${Number(stats.total_registered_members) || 0}`],
  ].map(([lbl, val]) => `<div class="admin-kpi"><div class="admin-kpi-val">${escapeHtmlStr(val)}</div><div class="admin-kpi-lbl">${escapeHtmlStr(lbl)}</div></div>`).join("");

  // 锁与联机状态
  const lckSt = d.lock_status || {};
  const lck = lckSt.lock || {};
  if (lckSt.locked && lck.holder_id) {
    const qNames = (lckSt.queue || []).map((q) => q.display_name).join("、");
    $("adminLockDetail").innerHTML = `<div><strong>当前占用人：</strong>${escapeHtmlStr(lck.holder_name)} (${escapeHtmlStr(lck.mode_label)}) - ${escapeHtmlStr(lck.step_text)}</div><div style="margin-top:4px;color:var(--sub);">排队队列(${(lckSt.queue || []).length})：${escapeHtmlStr(qNames || "暂无排队")}</div>`;
  } else {
    $("adminLockDetail").textContent = "当前空闲，无人占用表格锁。";
  }

  if ($("adminCloudStatusTip")) {
    const cnt = Array.isArray(d.members) ? d.members.length : 0;
    $("adminCloudStatusTip").textContent = `全员云端异地实时协同已自动开启（当前已汇集 ${cnt} 位成员状态），全国任何角落打开均自动防撞、统计与同步，无需填任何内网设置。`;
  }

  // 成员排行与流水
  const runsWrap = $("adminRunsList");
  runsWrap.innerHTML = "";
  const pms = Array.isArray(stats.per_member) ? stats.per_member : [];
  if (pms.length) {
    const hdr = document.createElement("div");
    hdr.className = "feed-item";
    hdr.innerHTML = "<strong>成员累计排行</strong>" + pms.map((pm) =>
      `<div style="display:flex;justify-content:space-between;align-items:center;padding:3px 0;">
        <span>${escapeHtmlStr(pm.display_name || "未知成员")}${pm.admin_remark ? " [" + escapeHtmlStr(pm.admin_remark) + "]" : ""}</span>
        <span>总 <strong>${Number(pm.total_runs) || 0}</strong> 次（成 ${Number(pm.ok_runs) || 0} / 败 ${Number(pm.err_runs) || 0}）</span>
      </div>`
    ).join("");
    runsWrap.appendChild(hdr);
  }
  const rlist = Array.isArray(stats.recent_runs) ? stats.recent_runs : [];
  if (!rlist.length) {
    const emptyR = document.createElement("div");
    emptyR.style.cssText = "color:var(--sub);font-size:12px;";
    emptyR.textContent = "该时间范围内暂无运行流水记录";
    runsWrap.appendChild(emptyR);
  } else {
    for (const r of rlist.slice(0, 20)) {
      const row = document.createElement("div");
      row.className = "feed-item";
      row.innerHTML = `<div style="display:flex;justify-content:space-between;"><strong>${escapeHtmlStr(r.member_name || r.display_name || "成员")} (${r.mode === "capture_only" ? "仅截图" : "全流程"} · ${r.trigger_type === "schedule" ? "定时" : "手动"})</strong><span style="color:${r.status === "ok" ? "var(--green)" : "var(--red)"}">${r.status === "ok" ? "成功" : escapeHtmlStr(r.err_code || r.status || "异常")} · ${Number(r.duration_sec) || 0}s</span></div><div style="color:var(--sub);">${escapeHtmlStr(r.ts)}</div>`;
      runsWrap.appendChild(row);
    }
  }

  // 2. 成员列表 + 体检 + 7 项权限 + 切换视角预览
  const memPane = $("adminPane-members");
  memPane.innerHTML = "";
  const pMeta = Array.isArray(d.permission_meta) ? d.permission_meta : [];
  if (!members.length) {
    memPane.innerHTML = `<div class="admin-box">暂无已连接成员记录</div>`;
  }
  for (const m of members) {
    const card = document.createElement("div");
    card.className = "member-card";
    const auditHtml = (m.config_audit && m.config_audit.length)
      ? m.config_audit.map((iss) => `<span class="audit-tag ${iss.level === "err" ? "err" : "warn"}" title="${escapeHtmlStr(iss.reason + " -> " + iss.fix)}">问题：${escapeHtmlStr(iss.item)}（${escapeHtmlStr(iss.fix)}）</span>`).join("")
      : `<span class="audit-tag ok">设置体检正常，未发现常见配置漏项</span>`;

    const top = document.createElement("div");
    top.className = "member-top";
    top.innerHTML = `
      <div class="member-id-wrap">
        <img class="member-lg-avatar" src="${safeAvatarUrl(m.avatar)}" alt="" />
        <div>
          <div style="font-size:14px;font-weight:800;">${escapeHtmlStr(m.display_name)} <span style="font-size:11.5px;color:var(--sub);font-weight:500;">编号 ${escapeHtmlStr(m.member_code)}</span></div>
          <div style="font-size:11.5px;color:var(--sub);">状态：${escapeHtmlStr(m.step_text || "待命")} · 最后活跃：${escapeHtmlStr(m.last_seen_str || "—")}</div>
        </div>
      </div>
      <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;">
        <input type="text" class="small-input js-remark-inp" value="${escapeHtmlStr(m.admin_remark || "")}" placeholder="给TA设个备注" style="width:120px;" />
        <button type="button" class="pill-action-btn js-preview-btn">切换看TA的页面</button>
        <button type="button" class="pill-action-btn primary js-save-mem-btn">保存权限与备注</button>
      </div>
    `;
    card.appendChild(top);

    const auditDiv = document.createElement("div");
    auditDiv.innerHTML = auditHtml;
    card.appendChild(auditDiv);

    const pGrid = document.createElement("div");
    pGrid.className = "perm-grid";
    const curPerms = Object.assign({}, m.permissions || {});
    for (const pm of pMeta) {
      const pRow = document.createElement("label");
      pRow.className = "perm-toggle-item";
      pRow.title = pm.desc || "";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = curPerms[pm.key] !== false;
      cb.onchange = () => { curPerms[pm.key] = cb.checked; };
      const txt = document.createElement("span");
      txt.textContent = pm.title;
      pRow.appendChild(txt);
      pRow.appendChild(cb);
      pGrid.appendChild(pRow);
    }
    card.appendChild(pGrid);

    const ov = (m.ai_api_override && typeof m.ai_api_override === "object")
      ? Object.assign({ mode: "default", provider: "gemini", api_base: "", api_key: "", model: "" }, m.ai_api_override)
      : { mode: "default", provider: "gemini", api_base: "", api_key: "", model: "" };
    if (ov.mode === "inherit") ov.mode = "default";
    const aiBox = document.createElement("div");
    aiBox.style.cssText = "margin-top:8px;padding-top:8px;border-top:0.5px dashed var(--sep);display:flex;flex-direction:column;gap:6px;font-size:12px;";
    aiBox.innerHTML = `
      <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;">
        <span style="font-weight:700;color:var(--sub);">TA 的专属 AI 接口（普通成员前台不可见）</span>
        <div class="mini-seg js-mem-ai-mode">
          <button type="button" class="mini-seg-btn ${ov.mode !== "custom" && ov.mode !== "local_only" ? "active" : ""}" data-val="default">跟随全员默认</button>
          <button type="button" class="mini-seg-btn ${ov.mode === "custom" ? "active" : ""}" data-val="custom">单独指定 API</button>
          <button type="button" class="mini-seg-btn ${ov.mode === "local_only" ? "active" : ""}" data-val="local_only">内置基础模式</button>
        </div>
      </div>
      <div class="js-mem-ai-custom" style="display:${ov.mode === "custom" ? "grid" : "none"};grid-template-columns:1fr 1fr;gap:6px;">
        <div class="mini-seg js-mem-ai-prov" style="grid-column:1 / -1;">
          <button type="button" class="mini-seg-btn ${ov.provider !== "openai_compat" ? "active" : ""}" data-val="gemini">标准云端协议</button>
          <button type="button" class="mini-seg-btn ${ov.provider === "openai_compat" ? "active" : ""}" data-val="openai_compat">兼容中转 (OpenAI格式)</button>
        </div>
        <input type="text" class="small-input js-mem-ai-base" value="${escapeHtmlStr(ov.api_base || "")}" placeholder="Base URL（官方留空）" />
        <input type="text" class="small-input js-mem-ai-model" value="${escapeHtmlStr(ov.model || "")}" placeholder="模型名（留空自动优选）" />
        <input type="password" class="small-input js-mem-ai-key" value="${escapeHtmlStr(ov.api_key || "")}" placeholder="单独分配的 API Key" />
        <button type="button" class="pill-action-btn js-test-mem-ai">测试 TA 的接口</button>
      </div>
    `;
    const customPane = aiBox.querySelector(".js-mem-ai-custom");
    for (const b of aiBox.querySelectorAll(".js-mem-ai-mode .mini-seg-btn")) {
      b.onclick = () => {
        for (const x of aiBox.querySelectorAll(".js-mem-ai-mode .mini-seg-btn")) x.classList.remove("active");
        b.classList.add("active");
        customPane.style.display = b.dataset.val === "custom" ? "grid" : "none";
      };
    }
    for (const b of aiBox.querySelectorAll(".js-mem-ai-prov .mini-seg-btn")) {
      b.onclick = () => {
        for (const x of aiBox.querySelectorAll(".js-mem-ai-prov .mini-seg-btn")) x.classList.remove("active");
        b.classList.add("active");
      };
    }
    const readMemAi = () => {
      const mBtn = aiBox.querySelector(".js-mem-ai-mode .mini-seg-btn.active");
      const pBtn = aiBox.querySelector(".js-mem-ai-prov .mini-seg-btn.active");
      return {
        mode: mBtn ? mBtn.dataset.val : "inherit",
        provider: pBtn ? pBtn.dataset.val : "gemini",
        api_base: aiBox.querySelector(".js-mem-ai-base").value.trim(),
        api_key: aiBox.querySelector(".js-mem-ai-key").value.trim(),
        model: aiBox.querySelector(".js-mem-ai-model").value.trim(),
      };
    };
    aiBox.querySelector(".js-test-mem-ai").onclick = async () => {
      const ma = readMemAi();
      const testObj = ma.mode === "local_only" ? { provider: "local_only" } : { provider: ma.provider, api_base: ma.api_base, api_key: ma.api_key, model: ma.model };
      showToast("正在测试该成员专属 AI 接口…");
      const r = await api("/api/admin/test-ai", "POST", { ai_api: testObj }, { "X-Admin-Token": adminToken });
      await iosAlert(r.ok ? "测试连通成功" : "测试异常", cleanText(r.reply) || "已完成检测");
    };
    card.appendChild(aiBox);

    top.querySelector(".js-save-mem-btn").onclick = async () => {
      const remark = top.querySelector(".js-remark-inp").value.trim();
      const res = await api("/api/admin/member", "POST", {
        user_id: m.user_id,
        remark,
        permissions: curPerms,
        ai_api_override: readMemAi(),
      }, { "X-Admin-Token": adminToken });
      showToast(res.message || "已保存成员设置与专属接口");
      loadAdminDashboard();
    };
    top.querySelector(".js-preview-btn").onclick = () => {
      enterAdminMemberPreview(m);
    };
    memPane.appendChild(card);
  }

  // 3. 同步报错 & AI 聊天
  const errBox = $("adminErrorsList");
  const errs = Array.isArray(d.errors) ? d.errors : [];
  errBox.innerHTML = errs.length ? errs.map((e) => `
    <div class="feed-item">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;">
        <div style="display:flex;align-items:center;gap:8px;">
          <img src="${safeAvatarUrl(e.avatar)}" style="width:24px;height:24px;border-radius:50%;object-fit:cover;" alt="" />
          <strong>${escapeHtmlStr(e.display_name || e.member_name || "成员")}${e.admin_remark ? " [" + escapeHtmlStr(e.admin_remark) + "]" : ""}</strong>
          <span class="audit-tag err">${escapeHtmlStr(e.code || "ERR")}</span>
        </div>
        <span style="color:var(--sub);">${escapeHtmlStr(e.ts || "")}</span>
      </div>
      <div>${escapeHtmlStr(e.title || "")}：${escapeHtmlStr(e.plain_reason || e.raw_error || "")}</div>
    </div>
  `).join("") : `<div style="color:var(--sub);font-size:12px;">暂无全员异常报错记录</div>`;

  const chatBox = $("adminAiChatsList");
  const chats = Array.isArray(d.ai_chats) ? d.ai_chats : [];
  chatBox.innerHTML = chats.length ? chats.map((c) => `
    <div class="feed-item">
      <div style="display:flex;justify-content:space-between;gap:8px;">
        <div style="display:flex;align-items:center;gap:7px;">
          <img src="${safeAvatarUrl(c.avatar)}" style="width:20px;height:20px;border-radius:50%;object-fit:cover;" alt="" />
          <strong>${escapeHtmlStr(c.display_name || c.member_name || "成员")}${c.admin_remark ? " [" + escapeHtmlStr(c.admin_remark) + "]" : ""} · ${c.role === "user" ? "提问" : "AI 回复"}</strong>
        </div>
        <span style="color:var(--sub);">${escapeHtmlStr(c.ts || "")}</span>
      </div>
      <div style="white-space:pre-wrap;">${escapeHtmlStr(c.text || "")}</div>
    </div>
  `).join("") : `<div style="color:var(--sub);font-size:12px;">暂无成员 AI 对话记录</div>`;

  // 4. 成员反馈收件箱
  const fbPane = $("adminPane-feedbacks");
  const fbs = Array.isArray(d.feedbacks) ? d.feedbacks : [];
  fbPane.innerHTML = "";
  if (!fbs.length) {
    fbPane.innerHTML = `<div class="admin-box" style="color:var(--sub);">暂无同事提交的反馈</div>`;
  } else {
    for (const fb of fbs) {
      const box = document.createElement("div");
      box.className = "admin-box";
      box.innerHTML = `
        <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;">
          <div style="display:flex;align-items:center;gap:8px;">
            <img src="${safeAvatarUrl(fb.avatar)}" style="width:28px;height:28px;border-radius:50%;object-fit:cover;" alt="" />
            <strong>${escapeHtmlStr(fb.display_name || fb.member_name || "成员")}${fb.admin_remark ? " [" + escapeHtmlStr(fb.admin_remark) + "]" : ""}</strong>
            <span class="audit-tag warn">${escapeHtmlStr(fb.category || "反馈")}</span>
            <span class="audit-tag ${fb.status === "resolved" ? "ok" : "warn"}">${fb.status === "resolved" ? "已处理" : "待处理"}</span>
          </div>
          <span style="font-size:11.5px;color:var(--sub);">${escapeHtmlStr(fb.ts || "")}</span>
        </div>
        <div style="font-size:13px;white-space:pre-wrap;">${escapeHtmlStr(fb.content || "")}</div>
        ${(() => { const cbSnap = fb.config_brief || fb.config_snapshot || {}; return `<div style="font-size:11.5px;color:var(--sub);">附带状态快照：目标群=${escapeHtmlStr(cbSnap.dt_search || "-")} · 粘贴等待=${Number(cbSnap.dt_paste_delay_sec) || 3}s · 静音发送=${Boolean(cbSnap.dt_silent_send)} · 自动发=${Boolean(cbSnap.dt_auto_send)}</div>`; })()}
        <div style="display:flex;gap:6px;align-items:center;">
          <input type="text" class="small-input js-fb-reply" style="flex:1;" value="${escapeHtmlStr(fb.admin_reply || "")}" placeholder="填写处理回复或备注" />
          <button type="button" class="pill-action-btn ok js-fb-done">标为已处理</button>
        </div>
      `;
      box.querySelector(".js-fb-done").onclick = async () => {
        const reply = box.querySelector(".js-fb-reply").value.trim();
        const r = await api("/api/admin/feedback", "POST", { id: fb.id, status: "resolved", reply }, { "X-Admin-Token": adminToken });
        showToast(r.message || "已标为已处理");
        loadAdminDashboard();
      };
      fbPane.appendChild(box);
    }
  }

  // 5. 全局软件逻辑
  const gl = d.global_logic || {};
  $("logicLockTimeoutInput").value = String(gl.lock_timeout_sec || 180);
  $("logicPasteDelayInput").value = (gl.enforce_paste_delay_sec === null || gl.enforce_paste_delay_sec === undefined) ? "" : String(gl.enforce_paste_delay_sec);
  for (const b of $("logicComposeSeg").querySelectorAll(".mini-seg-btn")) {
    b.classList.toggle("active", b.dataset.val === (gl.enforce_compose_order || ""));
  }
  for (const b of $("logicSilentSeg").querySelectorAll(".mini-seg-btn")) {
    b.classList.toggle("active", b.dataset.val === (gl.enforce_silent_order || ""));
  }
  const minVal = typeof gl.enforce_minimize_after_send === "boolean" ? String(gl.enforce_minimize_after_send) : "none";
  for (const b of $("logicMinimizeSeg").querySelectorAll(".mini-seg-btn")) {
    b.classList.toggle("active", b.dataset.val === minVal);
  }
  const gAi = (gl.global_ai_api && typeof gl.global_ai_api === "object") ? gl.global_ai_api : {};
  if ($("adminGlobalAiProviderSeg")) {
    const gp = gAi.provider || "gemini";
    for (const b of $("adminGlobalAiProviderSeg").querySelectorAll(".mini-seg-btn")) {
      b.classList.toggle("active", b.dataset.val === gp);
    }
  }
  if ($("adminGlobalAiBaseInput")) $("adminGlobalAiBaseInput").value = gAi.api_base || "";
  if ($("adminGlobalAiModelInput")) $("adminGlobalAiModelInput").value = gAi.model || "";
  if ($("adminGlobalAiKeyInput")) $("adminGlobalAiKeyInput").value = gAi.api_key || "";
}

function enterAdminMemberPreview(mem) {
  if (!selfPreviewBackup) {
    selfPreviewBackup = { cfg: JSON.parse(JSON.stringify(cfg)), logs: allLogItems.slice() };
  }
  previewMemberUid = mem.user_id;
  $("adminConsoleMask").classList.remove("show");
  $("adminPreviewBanner").classList.add("show");
  $("previewMemberAvatar").src = safeAvatarUrl(mem.avatar);
  $("previewMemberTitle").textContent = `正在预览【${mem.display_name}${mem.admin_remark ? " · " + mem.admin_remark : ""}】的实时界面（改设置将远程推送给TA）`;
  $("previewMemberSub").textContent = `对方状态：${mem.step_text || "待命"} · 上次运行：${mem.last_run_str || "—"} · 下次计划：${mem.next_run_str || "—"}`;
  if (mem.config && typeof mem.config === "object" && Object.keys(mem.config).length) {
    cfg = JSON.parse(JSON.stringify(mem.config));
    renderConfig();
  }
  $("stepText").textContent = mem.step_text || "待命";
  const rlogs = Array.isArray(mem.recent_logs) ? mem.recent_logs : [];
  allLogItems = rlogs.map((line, i) => {
    const s = String(line || "");
    const m = /^\[(\d\d:\d\d:\d\d)\]\s*(.*)$/.exec(s);
    const txt = m ? m[2] : s;
    const lvl = txt.includes("失败") || txt.includes("ERR-") || txt.includes("异常") ? "err" : (txt.includes("完成") || txt.includes("成功") ? "ok" : "info");
    return { id: i + 1, time: m ? m[1] : "--:--:--", level: lvl, text: txt };
  });
  renderLogs();
  renderErrorDiagCard(mem.last_error || null, null, false);
  applyPermissionsUI(mem.permissions || {});
  switchTab("home");
  showToast(`已切入 ${mem.display_name} 的实时状态视角`);
}

function exitAdminMemberPreview() {
  previewMemberUid = "";
  $("adminPreviewBanner").classList.remove("show");
  if (selfPreviewBackup) {
    cfg = selfPreviewBackup.cfg;
    allLogItems = selfPreviewBackup.logs;
    selfPreviewBackup = null;
    renderConfig();
    renderLogs();
  }
  pollState();
  $("adminConsoleMask").classList.add("show");
  loadAdminDashboard();
}

function bindTeamCollabAndAdminEvents() {
  // AI 自然语言排期模板填充
  for (const tb of document.querySelectorAll("#aiConfigTplBar [data-ai-tpl]")) {
    tb.onclick = () => {
      const inp = $("aiChatInput");
      if (!inp) return;
      inp.value = tb.dataset.aiTpl || "";
      inp.focus();
      showToast("已填入设定模板，改好目标群与时间后点发送即可");
    };
  }
  // AI 自定义聚焦组名 + 骰子筛选
  if ($("aiFocusTeamInput")) {
    $("aiFocusTeamInput").addEventListener("change", () => applyFocusTeamUpdate($("aiFocusTeamInput").value));
  }
  if ($("aiDiceTeamBtn")) {
    $("aiDiceTeamBtn").onclick = async () => {
      const btn = $("aiDiceTeamBtn");
      btn.classList.remove("spinning");
      void btn.offsetWidth;
      btn.classList.add("spinning");
      setTimeout(() => btn.classList.remove("spinning"), 500);
      const pool = (aiCandidateTeams && aiCandidateTeams.length)
        ? aiCandidateTeams.slice()
        : ["一组", "二组", "三组", "精英组", "核心组"];
      const cur = $("aiFocusTeamInput") ? $("aiFocusTeamInput").value.trim() : "";
      const choices = pool.filter((x) => x !== cur);
      const pick = (choices.length ? choices : pool)[Math.floor(Math.random() * (choices.length ? choices.length : pool.length))];
      await applyFocusTeamUpdate(pick, `已随机切换聚焦团队：${pick}`);
    };
  }
  if ($("aiClearTeamBtn")) {
    $("aiClearTeamBtn").onclick = () => applyFocusTeamUpdate("", "已清空组名筛选，恢复全部团队公平分析");
  }

  // 成员身份头像与编号
  $("memberProfileBtn").onclick = openProfileModal;
  $("profileCloseBtn").onclick = () => $("profileModalMask").classList.remove("show");
  $("profileDiceBtn").onclick = async () => {
    const r = await api(`/api/profile/random-name?exclude=${encodeURIComponent($("profileNicknameInput").value.trim())}`);
    if (r && r.nickname) $("profileNicknameInput").value = r.nickname;
  };
  $("profileSkipNameBtn").onclick = async () => {
    const r = await api("/api/profile", "POST", { skip_name: true, avatar: selectedAvatarUrl });
    if (r && r.ok) {
      memberProfile = r.profile;
      updateMemberTopChip(r.profile);
      $("profileModalMask").classList.remove("show");
      showToast(r.message);
    }
  };
  $("profileAvatarFileInput").onchange = async (ev) => {
    const f = ev.target.files && ev.target.files[0];
    if (!f) return;
    selectedAvatarUrl = await resizeUploadedAvatar(f);
    $("profileAvatarPreview").src = selectedAvatarUrl;
    for (const ch of $("avatarPresetsGrid").querySelectorAll(".avatar-preset-opt")) ch.classList.remove("active");
    showToast("已导入自定义头像");
  };
  $("profileSaveBtn").onclick = async () => {
    const nick = $("profileNicknameInput").value.trim();
    const r = await api("/api/profile", "POST", {
      nickname: nick,
      skip_name: !nick,
      avatar: selectedAvatarUrl,
    });
    if (r && r.ok) {
      memberProfile = r.profile;
      updateMemberTopChip(r.profile);
      $("profileModalMask").classList.remove("show");
      showToast(r.message);
    } else {
      showToast((r && r.message) || "该名字已经被占用，每个人的名字不能重复，请换一个");
      $("profileNicknameInput").focus();
      $("profileNicknameInput").select();
    }
  };

  // 全球远程控制网站扫码、端到端加密云中继与密码设置
  let mobileTunnelPollTimer = null;
  const renderMobileBridgeModal = (info) => {
    if (!info) return;
    const ghUrl = info.github_pages_url || "https://sorenskr.github.io/SheetMate/";
    const lanList = Array.isArray(info.lan_urls) ? info.lan_urls : [];
    const pub = info.public_url || "";
    if ($("mobileGithubUrlInput")) $("mobileGithubUrlInput").value = ghUrl;
    if ($("mobileLanUrlInput")) $("mobileLanUrlInput").value = lanList[0] || "";
    if ($("mobilePublicUrlInput")) $("mobilePublicUrlInput").value = pub;
    if ($("copyMobilePublicBtn")) $("copyMobilePublicBtn").style.display = pub ? "inline-flex" : "none";
    if ($("mobilePinInput") && document.activeElement !== $("mobilePinInput")) {
      $("mobilePinInput").value = info.remote_password || "";
    }
    if ($("mobileQrBox")) {
      $("mobileQrBox").innerHTML = info.qr_svg || "<span style='font-size:12px;color:#64748B;'>暂未生成二维码</span>";
    }
    if ($("cloudRelayStatusBadge") && $("mobileCloudRelayTip")) {
      if (!info.local_nick) {
        $("cloudRelayStatusBadge").textContent = "先起名字";
        $("cloudRelayStatusBadge").style.color = "var(--orange)";
        $("cloudRelayStatusBadge").style.background = "rgba(255,159,10,0.14)";
        $("mobileCloudRelayTip").textContent = "请先在软件右上角起好名字，之后手机打开网址填这个名字就能连。";
      } else if (info.cloud_relay_connected) {
        $("cloudRelayStatusBadge").textContent = "已联网就绪";
        $("cloudRelayStatusBadge").style.color = "var(--green)";
        $("cloudRelayStatusBadge").style.background = "rgba(40,205,65,0.14)";
        $("mobileCloudRelayTip").textContent = `已连好！在手机打开网址填「${info.local_nick}」和下方密码就能连上这台电脑。`;
      } else {
        $("cloudRelayStatusBadge").textContent = "联网中";
        $("cloudRelayStatusBadge").style.color = "var(--blue)";
        $("cloudRelayStatusBadge").style.background = "var(--pill-blue)";
        $("mobileCloudRelayTip").textContent = info.cloud_relay_error || "正在连接网络，请稍等几秒…";
      }
    }
    if ($("toggleMobileTunnelBtn")) {
      $("toggleMobileTunnelBtn").textContent = info.tunnel_running ? "关闭备用链接" : "开启备用链接";
      $("toggleMobileTunnelBtn").className = "mini-pill " + (info.tunnel_running ? "danger" : "");
    }
    if ($("mobileTunnelStatusTip")) {
      $("mobileTunnelStatusTip").textContent = pub
        ? `临时链接已通：${pub}`
        : (info.tunnel_error || (info.tunnel_running ? "正在开通临时链接，请等 2~5 秒…" : "一般直接用上面的全球远程网址就行，不用单独开这个。"));
    }
    if (info.tunnel_running && !pub) {
      clearTimeout(mobileTunnelPollTimer);
      mobileTunnelPollTimer = setTimeout(async () => {
        if ($("mobileBridgeModalMask") && $("mobileBridgeModalMask").classList.contains("show")) {
          const fresh = await api("/api/mobile-bridge/info");
          renderMobileBridgeModal(fresh);
        }
      }, 1500);
    } else {
      clearTimeout(mobileTunnelPollTimer);
    }
  };
  const openRemoteGuideModal = async () => {
    $("mobileBridgeModalMask").classList.add("show");
    const info = await api("/api/mobile-bridge/info");
    renderMobileBridgeModal(info);
  };
  if ($("mobileBridgeBtn")) $("mobileBridgeBtn").onclick = openRemoteGuideModal;
  if ($("openMobileGuideModalBtn")) $("openMobileGuideModalBtn").onclick = openRemoteGuideModal;
  if ($("copyGithubGuideBtn")) {
    $("copyGithubGuideBtn").onclick = async () => {
      try {
        await navigator.clipboard.writeText("https://sorenskr.github.io/SheetMate/");
        showToast("已复制远程网址，打开后填好名字和密码就能连电脑");
      } catch (_e) {}
    };
  }
  if ($("toggleMobilePinEyeBtn") && $("mobilePinInput")) {
    $("toggleMobilePinEyeBtn").onclick = () => {
      const inp = $("mobilePinInput");
      inp.type = inp.type === "password" ? "text" : "password";
    };
  }
  if ($("copyMobileGithubBtn")) {
    $("copyMobileGithubBtn").onclick = async () => {
      const v = $("mobileGithubUrlInput").value.trim();
      if (!v) return;
      try { await navigator.clipboard.writeText(v); showToast("已复制远程网址"); } catch (_e) {}
    };
  }
  if ($("openMobileGithubBtn")) {
    $("openMobileGithubBtn").onclick = () => {
      const v = safeExternalHttpUrl($("mobileGithubUrlInput").value);
      if (v) window.open(v, "_blank", "noopener,noreferrer");
    };
  }
  if ($("mobileModalCloseBtn")) {
    $("mobileModalCloseBtn").onclick = () => {
      clearTimeout(mobileTunnelPollTimer);
      $("mobileBridgeModalMask").classList.remove("show");
    };
  }
  if ($("copyMobileLanBtn")) {
    $("copyMobileLanBtn").onclick = async () => {
      const v = $("mobileLanUrlInput").value.trim();
      if (!v) return;
      try { await navigator.clipboard.writeText(v); showToast("已复制同 Wi-Fi 备用网址"); } catch (_e) {}
    };
  }
  if ($("copyMobilePublicBtn")) {
    $("copyMobilePublicBtn").onclick = async () => {
      const v = $("mobilePublicUrlInput").value.trim();
      if (!v) return;
      try { await navigator.clipboard.writeText(v); showToast("已复制临时备用链接"); } catch (_e) {}
    };
  }
  if ($("openMobileWebBtn")) {
    $("openMobileWebBtn").onclick = () => {
      const v = safeExternalHttpUrl($("mobilePublicUrlInput").value || $("mobileLanUrlInput").value);
      if (v) window.open(v, "_blank", "noopener,noreferrer");
    };
  }
  if ($("toggleMobileTunnelBtn")) {
    $("toggleMobileTunnelBtn").onclick = async () => {
      const isOff = $("toggleMobileTunnelBtn").textContent.includes("开启");
      showToast(isOff ? "正在开通临时备用链接…" : "正在关闭备用链接…");
      const res = await api("/api/mobile-bridge/tunnel", "POST", { enable: isOff });
      renderMobileBridgeModal(res);
      if (res && res.message) showToast(res.message);
    };
  }
  if ($("saveMobilePinBtn")) {
    $("saveMobilePinBtn").onclick = async () => {
      const newPin = $("mobilePinInput").value.trim();
      if (!newPin) {
        showToast("密码不能为空，请输入自己想设的新密码");
        return;
      }
      const r = await api("/api/mobile-bridge/tunnel", "POST", { action: "pin_only", pin: newPin });
      renderMobileBridgeModal(r);
      showToast((r && r.message) || "新远程密码已保存！以后连这台电脑都要输这个新密码");
    };
  }

  // 多人撞车排队弹窗 & 横幅
  const closeLockModal = () => $("lockWaitModalMask").classList.remove("show");
  $("lockWaitCloseBtn").onclick = closeLockModal;
  $("lockWaitCancelBtn").onclick = closeLockModal;
  $("lockWaitQueueBtn").onclick = async () => {
    const r = await api("/api/lock/queue", "POST", { action: "join", run_type: pendingLockRunType });
    closeLockModal();
    showToast(r.message || "已加入自动排队");
    pollState();
  };
  $("bannerJoinQueueBtn").onclick = async () => {
    const r = await api("/api/lock/queue", "POST", { action: "join", run_type: "full" });
    showToast(r.message || "已加入排队");
    pollState();
  };
  $("bannerLeaveQueueBtn").onclick = async () => {
    const r = await api("/api/lock/queue", "POST", { action: "leave" });
    showToast(r.message || "已取消排队");
    pollState();
  };

  // 反馈通道
  $("feedbackBtn").onclick = () => {
    $("fbContentInput").value = "";
    $("feedbackModalMask").classList.add("show");
  };
  $("fbCloseBtn").onclick = () => $("feedbackModalMask").classList.remove("show");
  $("fbCancelBtn").onclick = () => $("feedbackModalMask").classList.remove("show");
  for (const b of $("fbCategorySeg").querySelectorAll(".mini-seg-btn")) {
    b.onclick = () => {
      for (const x of $("fbCategorySeg").querySelectorAll(".mini-seg-btn")) x.classList.remove("active");
      b.classList.add("active");
      feedbackCategory = b.dataset.val;
    };
  }
  $("fbSubmitBtn").onclick = async () => {
    const txt = $("fbContentInput").value.trim();
    const r = await api("/api/feedback", "POST", { category: feedbackCategory, content: txt });
    if (r && r.ok) {
      $("feedbackModalMask").classList.remove("show");
      showToast(r.message || "反馈已提交，感谢您的建议");
    } else {
      showToast((r && r.message) || "请输入反馈内容");
    }
  };

  // 隐藏超级管理员双触发入口（1. 图标连续点5次 2. Ctrl+Shift+Alt+A）
  if ($("navAppIcon")) {
    $("navAppIcon").onclick = () => {
      const now = Date.now();
      iconClickTimes = iconClickTimes.filter((t) => now - t < 2400);
      iconClickTimes.push(now);
      if (iconClickTimes.length >= 5) {
        iconClickTimes = [];
        openAdminEntry();
      }
    };
  }
  window.addEventListener("keydown", (ev) => {
    if (ev.ctrlKey && ev.shiftKey && ev.altKey && (ev.key === "a" || ev.key === "A")) {
      ev.preventDefault();
      openAdminEntry();
    }
  });
  $("adminAuthCloseBtn").onclick = () => {
    $("adminPasscodeInput").value = "";
    $("adminPasswordInput").value = "";
    $("adminAuthMask").classList.remove("show");
  };
  $("adminLoginBtn").onclick = async () => {
    const r = await api("/api/admin/login", "POST", {
      passcode: $("adminPasscodeInput").value,
      password: $("adminPasswordInput").value,
    });
    if (!r || !r.ok) {
      showToast((r && r.message) || "管理员姓名或密码不正确");
      return;
    }
    $("adminPasscodeInput").value = "";
    $("adminPasswordInput").value = "";
    adminToken = r.admin_token;
    sessionStorage.setItem("sheetmate_admin_tok", adminToken);
    $("adminAuthMask").classList.remove("show");
    $("adminConsoleMask").classList.add("show");
    loadAdminDashboard();
  };
  $("adminPasscodeInput").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") $("adminPasswordInput").focus();
  });
  $("adminPasswordInput").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") $("adminLoginBtn").click();
  });
  $("adminCloseBtn").onclick = () => $("adminConsoleMask").classList.remove("show");
  $("adminRefreshBtn").onclick = loadAdminDashboard;
  $("exitPreviewBtn").onclick = exitAdminMemberPreview;

  // 超管后台 5 标签切换
  for (const b of $("adminTabsBar").querySelectorAll(".admin-tab-btn")) {
    b.onclick = () => {
      adminActiveTab = b.dataset.atab;
      for (const x of $("adminTabsBar").querySelectorAll(".admin-tab-btn")) x.classList.toggle("active", x === b);
      for (const p of document.querySelectorAll(".admin-pane")) {
        p.style.display = (p.id === `adminPane-${adminActiveTab}`) ? "flex" : "none";
      }
    };
  }

  // 快捷日期范围查询
  for (const qb of document.querySelectorAll("[data-qrange]")) {
    qb.onclick = () => {
      for (const x of document.querySelectorAll("[data-qrange]")) x.classList.remove("active");
      qb.classList.add("active");
      const mode = qb.dataset.qrange;
      const now = new Date();
      if (mode === "all") {
        $("statStartInput").value = "";
        $("statEndInput").value = "";
      } else if (mode === "today") {
        const s = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 0, 0);
        const e = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 23, 59);
        $("statStartInput").value = fmtLocalYmdHm(s);
        $("statEndInput").value = fmtLocalYmdHm(e);
      } else if (mode === "yesterday") {
        const y = new Date(now.getTime() - 86400000);
        const s = new Date(y.getFullYear(), y.getMonth(), y.getDate(), 0, 0);
        const e = new Date(y.getFullYear(), y.getMonth(), y.getDate(), 23, 59);
        $("statStartInput").value = fmtLocalYmdHm(s);
        $("statEndInput").value = fmtLocalYmdHm(e);
      } else if (mode === "7d") {
        const w = new Date(now.getTime() - 6 * 86400000);
        const s = new Date(w.getFullYear(), w.getMonth(), w.getDate(), 0, 0);
        const e = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 23, 59);
        $("statStartInput").value = fmtLocalYmdHm(s);
        $("statEndInput").value = fmtLocalYmdHm(e);
      }
      loadAdminDashboard();
    };
  }
  $("statQueryBtn").onclick = loadAdminDashboard;
  $("adminForceUnlockBtn").onclick = async () => {
    const r = await api("/api/admin/unlock", "POST", { clear_queue: true }, { "X-Admin-Token": adminToken });
    showToast(r.message || "已强制释放锁定");
    loadAdminDashboard();
  };
  for (const segId of ["logicComposeSeg", "logicSilentSeg", "logicMinimizeSeg", "adminGlobalAiProviderSeg"]) {
    const seg = $(segId);
    if (!seg) continue;
    for (const b of seg.querySelectorAll(".mini-seg-btn")) {
      b.onclick = () => {
        for (const x of seg.querySelectorAll(".mini-seg-btn")) x.classList.remove("active");
        b.classList.add("active");
      };
    }
  }
  if ($("adminGlobalAiKeyToggle") && $("adminGlobalAiKeyInput")) {
    $("adminGlobalAiKeyToggle").onclick = () => {
      const inp = $("adminGlobalAiKeyInput");
      inp.type = inp.type === "password" ? "text" : "password";
    };
  }
  const readGlobalAiPayload = () => {
    const pBtn = $("adminGlobalAiProviderSeg") && $("adminGlobalAiProviderSeg").querySelector(".mini-seg-btn.active");
    return {
      provider: pBtn ? pBtn.dataset.val : "gemini",
      api_base: $("adminGlobalAiBaseInput") ? $("adminGlobalAiBaseInput").value.trim() : "",
      api_key: $("adminGlobalAiKeyInput") ? $("adminGlobalAiKeyInput").value.trim() : "",
      model: $("adminGlobalAiModelInput") ? $("adminGlobalAiModelInput").value.trim() : "",
    };
  };
  if ($("adminTestGlobalAiBtn")) {
    $("adminTestGlobalAiBtn").onclick = async () => {
      showToast("正在测试全员默认 AI 接口…");
      const r = await api("/api/admin/test-ai", "POST", { ai_api: readGlobalAiPayload() }, { "X-Admin-Token": adminToken });
      await iosAlert(r.ok ? "接口连通成功" : "接口测试异常", cleanText(r.reply) || "已完成检测");
    };
  }
  if ($("adminSaveGlobalAiBtn")) {
    $("adminSaveGlobalAiBtn").onclick = async () => {
      const r = await api("/api/admin/global-logic", "POST", { global_ai_api: readGlobalAiPayload() }, { "X-Admin-Token": adminToken });
      showToast(r.message || "已更新全员默认 AI 接口并即时下发");
      loadAdminDashboard();
    };
  }
  if ($("adminCloudSyncNowBtn")) {
    $("adminCloudSyncNowBtn").onclick = async () => {
      $("adminCloudSyncNowBtn").disabled = true;
      $("adminCloudSyncNowBtn").textContent = "同步中…";
      try {
        const r = await api("/api/admin/sync", "POST", { action: "cloud_refresh" }, { "X-Admin-Token": adminToken });
        showToast((r && r.message) || "已完成云端异地数据同步");
        await loadAdminDashboard();
      } finally {
        $("adminCloudSyncNowBtn").disabled = false;
        $("adminCloudSyncNowBtn").textContent = "立即云端同步";
      }
    };
  }
  $("adminSaveLogicBtn").onclick = async () => {
    const comp = $("logicComposeSeg").querySelector(".mini-seg-btn.active");
    const sil = $("logicSilentSeg").querySelector(".mini-seg-btn.active");
    const minB = $("logicMinimizeSeg").querySelector(".mini-seg-btn.active");
    const minVal = minB ? minB.dataset.val : "none";
    const pdRaw = $("logicPasteDelayInput").value.trim();
    const r = await api("/api/admin/global-logic", "POST", {
      lock_timeout_sec: parseInt($("logicLockTimeoutInput").value || "180", 10) || 180,
      enforce_paste_delay_sec: pdRaw === "" ? null : parseInt(pdRaw, 10),
      enforce_compose_order: comp ? comp.dataset.val : "",
      enforce_silent_order: sil ? sil.dataset.val : "",
      enforce_minimize_after_send: minVal === "true" ? true : (minVal === "false" ? false : null),
    }, { "X-Admin-Token": adminToken });
    showToast(r.message || "全局运行逻辑已更新并下发");
    loadAdminDashboard();
  };
  $("adminSaveCredBtn").onclick = async () => {
    const r = await api("/api/admin/credentials", "POST", {
      passcode: $("adminNewPasscodeInput").value.trim(),
      password: $("adminNewPasswordInput").value.trim(),
    }, { "X-Admin-Token": adminToken });
    if (r && r.ok) {
      $("adminNewPasscodeInput").value = "";
      $("adminNewPasswordInput").value = "";
    }
    showToast((r && r.message) || "管理员姓名与密码已更新");
  };
}

let mascotMoodTimer = null;
const CAT_MOUTHS = {
  happy: "M20.2 31.6 Q22.1 34 24 31.8 Q25.9 34 27.8 31.6",
  cheer: "M20 31.2 Q24 35.8 28 31.2 Z",
  wink: "M20.6 32 Q24 34.4 27.6 31.4",
  curious: "M22.5 32.3 A1.6 1.6 0 1 0 25.7 32.3 A1.6 1.6 0 1 0 22.5 32.3",
  sleepy: "M21.2 32.4 Q24 31.7 26.8 32.4",
  think: "M20.8 32.6 Q23.2 31.4 27.2 32.5",
  love: "M20 31.4 Q22.2 34.5 24 31.8 Q25.8 34.5 28 31.4",
  cool: "M20.4 32.2 Q24.4 33.4 27.8 31.2",
};

function setCatMascotMood(mood, durationMs = 0) {
  const btn = $("catMascotBtn");
  const mouth = $("catMouthPath");
  if (!btn || !mouth) return;
  const moods = Object.keys(CAT_MOUTHS);
  const pick = CAT_MOUTHS[mood] ? mood : moods[Math.floor(Math.random() * moods.length)];
  for (const k of moods) btn.classList.remove("mood-" + k);
  btn.classList.remove("wink-r");
  btn.classList.add("mood-" + pick);
  if (pick === "wink") btn.classList.add("wink-r");
  mouth.setAttribute("d", CAT_MOUTHS[pick]);
  clearTimeout(mascotMoodTimer);
  if (durationMs > 0) {
    mascotMoodTimer = setTimeout(() => {
      setCatMascotMood("happy", 0);
    }, durationMs);
  }
}

function initMascotLifeEngine() {
  const btn = $("catMascotBtn");
  const svg = $("catMascotSvg");
  const pupL = $("catPupilL");
  const pupR = $("catPupilR");
  if (!btn || !svg) return;

  let startTs = performance.now();
  let boost = 0;
  const stepMotion = (now) => {
    if (document.hidden) {
      requestAnimationFrame(stepMotion);
      return;
    }
    const t = (now - startTs) * 0.001;
    // 多组互质频率三角波叠加，保证小猫悬浮/微倾轨迹无限不循环
    const dx = Math.sin(t * 0.73) * 1.65 + Math.sin(t * 1.91) * 0.75;
    const dy = Math.sin(t * 1.13) * 2.15 + Math.cos(t * 2.67) * 0.85 - boost * 4.2;
    const rot = Math.sin(t * 0.59) * 3.8 + Math.sin(t * 1.79) * 1.7;
    const sc = 1 + Math.sin(t * 1.37) * 0.024 + boost * 0.09;
    boost *= 0.9;
    svg.style.transform = `translate3d(${dx.toFixed(2)}px, ${dy.toFixed(2)}px, 0) rotate(${rot.toFixed(2)}deg) scale(${sc.toFixed(3)})`;
    requestAnimationFrame(stepMotion);
  };
  requestAnimationFrame(stepMotion);

  // 随机单次/双击眨眼与左右抖耳
  const scheduleBlink = () => {
    const waitMs = 2100 + Math.random() * 4200;
    setTimeout(() => {
      btn.classList.add("is-blinking");
      setTimeout(() => {
        btn.classList.remove("is-blinking");
        if (Math.random() < 0.28) {
          setTimeout(() => {
            btn.classList.add("is-blinking");
            setTimeout(() => btn.classList.remove("is-blinking"), 95);
          }, 130);
        }
      }, 115);
      scheduleBlink();
    }, waitMs);
  };
  scheduleBlink();

  const scheduleTwitch = () => {
    const waitMs = 2800 + Math.random() * 4800;
    setTimeout(() => {
      const cls = Math.random() < 0.5 ? "twitch-l" : "twitch-r";
      btn.classList.add(cls);
      setTimeout(() => btn.classList.remove(cls), 360);
      scheduleTwitch();
    }, waitMs);
  };
  scheduleTwitch();

  const moodList = ["happy", "curious", "wink", "cheer", "think", "love", "cool", "sleepy"];
  const scheduleMoodShift = () => {
    const waitMs = 5500 + Math.random() * 7500;
    setTimeout(() => {
      const nxt = moodList[Math.floor(Math.random() * moodList.length)];
      setCatMascotMood(nxt, 3600 + Math.random() * 2400);
      scheduleMoodShift();
    }, waitMs);
  };
  scheduleMoodShift();

  // 瞳孔跟随光标方向
  window.addEventListener("mousemove", (ev) => {
    if (!pupL || !pupR) return;
    const rect = btn.getBoundingClientRect();
    if (!rect.width) return;
    const cx = rect.left + rect.width * 0.5;
    const cy = rect.top + rect.height * 0.5;
    const angle = Math.atan2(ev.clientY - cy, ev.clientX - cx);
    const dist = Math.min(1, Math.hypot(ev.clientX - cx, ev.clientY - cy) / 260);
    const ox = Math.cos(angle) * 1.45 * dist;
    const oy = Math.sin(angle) * 1.25 * dist;
    pupL.setAttribute("cx", (17.8 + ox).toFixed(2));
    pupL.setAttribute("cy", (26.2 + oy).toFixed(2));
    pupR.setAttribute("cx", (30.2 + ox).toFixed(2));
    pupR.setAttribute("cy", (26.2 + oy).toFixed(2));
  }, { passive: true });

  btn.onclick = () => {
    boost = 1.0;
    const cls = Math.random() < 0.5 ? "twitch-l" : "twitch-r";
    btn.classList.add(cls);
    setTimeout(() => btn.classList.remove(cls), 380);
    const altMoods = ["cheer", "love", "wink", "cool"];
    setCatMascotMood(altMoods[Math.floor(Math.random() * altMoods.length)], 4500);
    loadStartupGreeting(true);
  };
  if ($("aiGreetingNextBtn")) {
    $("aiGreetingNextBtn").onclick = () => {
      boost = 0.85;
      setCatMascotMood("love", 4200);
      loadStartupGreeting(true);
    };
  }
}

async function loadStartupGreeting(force = false) {
  const nextBtn = $("aiGreetingNextBtn");
  const txtEl = $("aiGreetingText");
  if (force && nextBtn) {
    nextBtn.disabled = true;
    nextBtn.textContent = "换词中…";
  }
  try {
    const r = await api(`/api/ai-greeting?force=${force ? "1" : "0"}&_t=${Date.now()}`);
    if (r && r.show && r.reply && $("aiMascotGreetingBar") && txtEl) {
      if ($("aiGreetingPeriodBadge")) {
        $("aiGreetingPeriodBadge").textContent = r.period || "小猫开麦";
      }
      txtEl.style.transition = "opacity 0.14s ease";
      txtEl.style.opacity = "0.25";
      setTimeout(() => {
        txtEl.textContent = cleanText(r.reply) || r.reply;
        txtEl.style.opacity = "1";
      }, 60);
      $("aiMascotGreetingBar").classList.add("show");
      if (force) setCatMascotMood("cheer", 4200);
    }
  } catch (_e) {
  } finally {
    if (force && nextBtn) {
      setTimeout(() => {
        nextBtn.disabled = false;
        nextBtn.textContent = "换一句";
      }, 180);
    }
  }
}

(async function bootstrap() {
  initTheme();
  bindEvents();
  initMascotLifeEngine();
  try {
    if (window.outerWidth < 680 && window.screen && window.screen.availHeight >= 760) {
      const tw = Math.min(1320, Math.round(window.screen.availWidth * 0.78));
      const th = Math.min(920, Math.round(window.screen.availHeight * 0.84));
      window.resizeTo(tw, th);
    }
  } catch (_e) {}
  const data = await api("/api/config");
  cfg = data.config;
  latestTimetable = Array.isArray(data.timetable) ? data.timetable : [];
  if (data.ai_meta) {
    aiStyles = Array.isArray(data.ai_meta.styles) ? data.ai_meta.styles : [];
    aiCandidateTeams = Array.isArray(data.ai_meta.candidate_teams) ? data.ai_meta.candidate_teams : [];
    cachedChatHistory = Array.isArray(data.ai_meta.history) ? data.ai_meta.history : [];
    $("aiShotStatusPill").textContent = data.ai_meta.latest_shot_label ? ("已就绪 · " + data.ai_meta.latest_shot_label) : "数据就绪";
    if (data.ai_meta.data_brief) applyDataBriefUI(data.ai_meta.data_brief);
    renderAiChatFeed(cachedChatHistory);
  }
  if (data.profile) {
    memberProfile = data.profile;
    updateMemberTopChip(data.profile);
  }
  if (Array.isArray(data.presets)) avatarPresets = data.presets;
  if (data.sync_settings) syncSettingsState = data.sync_settings;
  renderConfig();
  renderTimetable(latestTimetable);
  await pollState();
  setInterval(pollState, 900);
  setTimeout(() => loadStartupGreeting(false), 350);
  if (memberProfile && !memberProfile.initialized) {
    setTimeout(() => openProfileModal(), 500);
  }
})();
</script>
</body>
</html>
"""


def _find_static_asset(filename):
    """兼容源码运行与 PyInstaller 单文件/文件夹(_internal)目录，查找打包静态图标资源。"""
    candidates = [Path(__file__).resolve().parent / filename]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.insert(0, Path(meipass) / filename)
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates.append(exe_dir / "_internal" / filename)
        candidates.append(exe_dir / filename)
    for p in candidates:
        try:
            if p.exists():
                return p
        except Exception:
            pass
    return None


def _make_handler(ctrl, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def _check_auth(self, parsed):
            hdr = self.headers.get("X-Token") or ""
            qs = parse_qs(parsed.query)
            qtok = (qs.get("token") or [""])[0]
            return hmac.compare_digest(hdr, token) or hmac.compare_digest(qtok, token)

        def _send_security_headers(self):
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")

        def _send_json(self, payload, status=HTTPStatus.OK):
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self._send_security_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _read_json(self):
            length = _safe_int(self.headers.get("Content-Length"), 0)
            if length <= 0:
                return {}
            if length > MAX_UI_BODY_BYTES:
                raise ValueError("PayloadTooLarge")
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except Exception:
                return {}

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path in ("/favicon.ico", "/icon.png"):
                fname = "icon.ico" if parsed.path == "/favicon.ico" else "icon.png"
                ctype = "image/x-icon" if fname == "icon.ico" else "image/png"
                asset = _find_static_asset(fname)
                if asset is not None:
                    raw = asset.read_bytes()
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                self.send_response(HTTPStatus.NOT_FOUND)
                self.end_headers()
                return

            if not self._check_auth(parsed):
                self._send_json({"ok": False, "message": "Forbidden"}, HTTPStatus.FORBIDDEN)
                return

            if parsed.path == "/":
                body = HTML_TEMPLATE.replace("__TOKEN_JSON__", json.dumps(token)).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self._send_security_headers()
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if parsed.path == "/api/state":
                qs = parse_qs(parsed.query)
                since = _safe_int((qs.get("since") or ["0"])[0], 0)
                self._send_json(ctrl.get_state(since=since))
                return

            if parsed.path == "/api/config":
                self._send_json({
                    "ok": True,
                    "config": ctrl.get_public_config(),
                    "timetable": ctrl.get_timetable(),
                    "ai_meta": ctrl.get_ai_meta(),
                    **(ctrl.get_profile_payload() if hasattr(ctrl, "get_profile_payload") else {}),
                })
                return

            if parsed.path == "/api/ai-greeting":
                qs = parse_qs(parsed.query)
                frc = str((qs.get("force") or ["0"])[0]).lower() in ("1", "true", "yes")
                self._send_json(ctrl.get_startup_greeting(force=frc))
                return

            if parsed.path == "/api/profile":
                self._send_json(ctrl.get_profile_payload())
                return

            if parsed.path == "/api/profile/random-name":
                qs = parse_qs(parsed.query)
                ex = (qs.get("exclude") or [""])[0]
                self._send_json(ctrl.get_random_nickname_payload(exclude=ex))
                return

            if parsed.path == "/api/lock/status":
                lst = ctrl.hub.get_lock_status() if ctrl.hub else {"locked": False, "lock": {}, "queue": []}
                self._send_json({"ok": True, **lst})
                return

            if parsed.path == "/api/mobile-bridge/info":
                self._send_json(ctrl.get_mobile_bridge_info())
                return

            if parsed.path == "/api/admin/dashboard":
                atok = self.headers.get("X-Admin-Token") or ""
                if not ctrl.admin_is_authed(atok):
                    self._send_json({"ok": False, "auth_required": True, "message": "需要超级管理员口令与密码"}, HTTPStatus.FORBIDDEN)
                    return
                qs = parse_qs(parsed.query)
                s_t = (qs.get("start_time") or [""])[0]
                e_t = (qs.get("end_time") or [""])[0]
                u_id = (qs.get("user_id") or [""])[0]
                self._send_json(ctrl.admin_dashboard_payload(start_time=s_t, end_time=e_t, user_id=u_id))
                return

            self._send_json({"ok": False, "message": "Not Found"}, HTTPStatus.NOT_FOUND)

        def do_POST(self):
            try:
                self._dispatch_post()
            except ValueError as exc:
                if str(exc) == "PayloadTooLarge":
                    self._send_json(
                        {"ok": False, "message": "请求体超过大小限制"},
                        HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    )
                    return
                self._send_json({"ok": False, "message": "请求参数异常"}, HTTPStatus.BAD_REQUEST)

        def _dispatch_post(self):
            parsed = urlparse(self.path)
            if not self._check_auth(parsed):
                self._send_json({"ok": False, "message": "Forbidden"}, HTTPStatus.FORBIDDEN)
                return

            if parsed.path == "/api/config":
                body = self._read_json()
                self._send_json(ctrl.save_config_checked(body))
                return

            if parsed.path == "/api/run-now":
                self._send_json(ctrl.start_run_now())
                return

            if parsed.path == "/api/run-capture":
                self._send_json(ctrl.start_capture_only())
                return

            if parsed.path == "/api/schedule":
                body = self._read_json()
                enable = body.get("enable") if isinstance(body, dict) and "enable" in body else None
                ok, msg, running = ctrl.toggle_schedule(enable)
                self._send_json({"ok": ok, "message": msg, "schedule_running": running})
                return

            if parsed.path == "/api/pause":
                ok, msg = ctrl.pause_current_task()
                self._send_json({"ok": ok, "message": msg})
                return

            if parsed.path == "/api/resume":
                body = self._read_json()
                act = body.get("action") if isinstance(body, dict) else "continue"
                ok, msg = ctrl.resume_current_task(act or "continue")
                self._send_json({"ok": ok, "message": msg})
                return

            if parsed.path == "/api/stop":
                ok, msg = ctrl.stop_current_task()
                self._send_json({"ok": ok, "message": msg})
                return

            if parsed.path == "/api/hotkey":
                body = self._read_json()
                slot = body.get("slot") if isinstance(body, dict) else "f8"
                ok, msg = ctrl.trigger_hotkey(slot)
                self._send_json({"ok": ok, "message": msg})
                return

            if parsed.path == "/api/ai-diagnose":
                body = self._read_json()
                mode = (body.get("mode") if isinstance(body, dict) else None) or "diagnose"
                question = (body.get("question") if isinstance(body, dict) else None) or ""
                res = ctrl.ask_ai(mode=mode, question=question)
                self._send_json(res)
                return

            if parsed.path == "/api/ai-auto-heal":
                res = ctrl.trigger_ai_auto_heal()
                self._send_json(res)
                return

            if parsed.path == "/api/ai-apply-tuning":
                body = self._read_json()
                patch = body.get("patch") if isinstance(body, dict) else None
                res_now = bool(body.get("resume_now", True)) if isinstance(body, dict) else True
                res = ctrl.apply_ai_tuning(patch=patch, resume_now=res_now)
                self._send_json(res)
                return

            if parsed.path == "/api/ai-summary":
                body = self._read_json()
                sid = (body.get("style_id") if isinstance(body, dict) else None) or "brief"
                prompt = (body.get("prompt") if isinstance(body, dict) else None) or ""
                act = (body.get("action") if isinstance(body, dict) else None) or "send"
                res = ctrl.summarize_ai(style_id=sid, prompt=prompt, action=act)
                self._send_json(res)
                return

            if parsed.path == "/api/reset":
                saved = ctrl.reset_config()
                self._send_json({"ok": True, "config": saved, "timetable": ctrl.get_timetable()})
                return

            if parsed.path == "/api/clear-logs":
                ctrl.clear_logs()
                self._send_json({"ok": True})
                return

            if parsed.path == "/api/check-dingtalk":
                if hasattr(ctrl.app, "diagnose_dingtalk_env"):
                    res = ctrl.app.diagnose_dingtalk_env()
                    self._send_json({"ok": bool(res.get("ok")), **res})
                else:
                    self._send_json({"ok": False, "summary": "当前版本不支持环境自检"})
                return

            if parsed.path == "/api/open-shots":
                try:
                    shots_dir = ctrl.app.app_dir() / "history_shots"
                    shots_dir.mkdir(parents=True, exist_ok=True)
                    if os.name == "nt":
                        os.startfile(str(shots_dir))
                    self._send_json({"ok": True, "path": str(shots_dir), "message": "已打开历史截图文件夹"})
                except Exception as exc:
                    self._send_json({"ok": False, "message": f"打开文件夹失败：{exc}"})
                return

            if parsed.path == "/api/profile":
                body = self._read_json()
                self._send_json(ctrl.update_profile_payload(body))
                return

            if parsed.path == "/api/sync-config":
                atok = self.headers.get("X-Admin-Token") or ""
                if not ctrl.admin_is_authed(atok):
                    self._send_json({"ok": False, "auth_required": True, "message": "需要超级管理员验证"}, HTTPStatus.FORBIDDEN)
                    return
                body = self._read_json()
                self._send_json(ctrl.update_sync_config_payload(body))
                return

            if parsed.path == "/api/lock/queue":
                body = self._read_json()
                self._send_json(ctrl.handle_queue_action(body))
                return

            if parsed.path == "/api/feedback":
                body = self._read_json()
                self._send_json(ctrl.submit_feedback_payload(body))
                return

            if parsed.path == "/api/mobile-bridge/tunnel":
                body = self._read_json()
                self._send_json(ctrl.control_mobile_tunnel(body))
                return

            if parsed.path == "/api/admin/login":
                body = self._read_json()
                self._send_json(ctrl.admin_login(body))
                return

            if parsed.path.startswith("/api/admin/"):
                atok = self.headers.get("X-Admin-Token") or ""
                if not ctrl.admin_is_authed(atok):
                    self._send_json({"ok": False, "auth_required": True, "message": "需要超级管理员验证"}, HTTPStatus.FORBIDDEN)
                    return
                body = self._read_json()
                if parsed.path == "/api/admin/unlock":
                    self._send_json(ctrl.admin_unlock_payload(body))
                    return
                if parsed.path == "/api/admin/member":
                    self._send_json(ctrl.admin_member_payload(body))
                    return
                if parsed.path == "/api/admin/global-logic":
                    self._send_json(ctrl.admin_global_logic_payload(body))
                    return
                if parsed.path == "/api/admin/sync":
                    self._send_json(ctrl.update_sync_config_payload(body))
                    return
                if parsed.path == "/api/admin/server":
                    self._send_json(ctrl.admin_server_toggle_payload(body))
                    return
                if parsed.path == "/api/admin/feedback":
                    self._send_json(ctrl.admin_feedback_resolve_payload(body))
                    return
                if parsed.path == "/api/admin/credentials":
                    self._send_json(ctrl.admin_credentials_payload(body))
                    return
                if parsed.path == "/api/admin/test-ai":
                    self._send_json(ctrl.admin_test_ai_payload(body))
                    return

            if parsed.path == "/api/close":
                ctrl.close_requested_at = time.time()
                self._send_json({"ok": True})
                return

            self._send_json({"ok": False, "message": "Not Found"}, HTTPStatus.NOT_FOUND)

    return Handler


def start_ui_server(app, glass, tk_root):
    """启动本地 HTTP 控制面板服务，返回 (server, ctrl, url, token)。"""
    token = secrets.token_urlsafe(24)
    ctrl = _SheetMateController(app, glass, tk_root)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(ctrl, token))
    server.daemon_threads = True
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/?token={token}"
    return server, ctrl, url, token


def show_settings_window(app):
    """极速并行拉起高信息密度宽屏圆角控制中心，随后后台完成 Tk 灵动岛浮窗挂载。"""
    sw, sh = 1440, 900
    if os.name == "nt":
        try:
            u32 = ctypes.windll.user32
            sw = int(u32.GetSystemMetrics(0) or 1440)
            sh = int(u32.GetSystemMetrics(1) or 900)
        except Exception:
            pass
    win_w = min(1320, max(1040, int(sw * 0.78)))
    win_h = min(920, max(780, int(sh * 0.84)))
    wx = max(16, (sw - win_w) // 2)
    wy = max(16, (sh - win_h) // 2)

    server, ctrl, url, _token = start_ui_server(app, None, None)

    shell_exe, _kind = _find_app_shell_browser()
    proc = None
    launched_at = time.time()

    if shell_exe:
        rt_base = app.get_local_runtime_dir() if hasattr(app, "get_local_runtime_dir") else (app.app_dir() / "runtime_data")
        shell_dir = Path(rt_base) / "ui_shell_v22"
        shell_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            shell_exe,
            f"--app={url}",
            f"--user-data-dir={shell_dir}",
            f"--window-size={win_w},{win_h}",
            f"--window-position={wx},{wy}",
            "--no-proxy-server",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-component-update",
            "--disable-extensions",
            "--disable-sync",
            "--disable-background-networking",
            "--disk-cache-size=52428800",
            "--disable-features=msEdgeSidebarV2",
        ]
        try:
            proc = subprocess.Popen(cmd)
            _apply_square_rounded_win32(wx, wy, win_w, win_h)
        except Exception:
            proc = None

    if proc is None:
        webbrowser.open(url)

    import tkinter as tk
    import neu_ui as neu

    tk_root = tk.Tk()
    tk_root.withdraw()
    try:
        ico_path = _find_static_asset("icon.ico")
        if ico_path is not None:
            tk_root.iconbitmap(str(ico_path))
    except Exception:
        pass
    glass = neu.GlassProgress(tk_root, title="暗号表助手")
    ctrl.attach_glass(glass, tk_root)

    def _shutdown():
        try:
            ctrl.save_config(ctrl.get_config())
        except Exception:
            pass
        try:
            app.request_stop()
        except Exception:
            pass
        try:
            server.shutdown()
        except Exception:
            pass
        try:
            tk_root.destroy()
        except Exception:
            pass
        os._exit(0)

    def _watch_lifecycle():
        now = time.time()
        if ctrl.close_requested_at > 0 and (now - ctrl.close_requested_at) > 1.6 and (now - ctrl.last_heartbeat) > 1.4:
            _shutdown()
            return
        if proc is not None and (now - launched_at) > 3.0 and proc.poll() is not None and (now - ctrl.last_heartbeat) > 2.5:
            _shutdown()
            return
        tk_root.after(450, _watch_lifecycle)

    tk_root.after(1000, _watch_lifecycle)
    tk_root.mainloop()
