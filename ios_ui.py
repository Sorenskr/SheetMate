# -*- coding: utf-8 -*-
"""SheetMate v2.0.0 Apple iOS 18 原生风格控制中心界面与调度后端。"""

import json
import os
import re
import secrets
import subprocess
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

STEP_PROGRESS = {
    "正在打开浏览器…": 0.06,
    "正在打开WPS汇总表…": 0.16,
    "正在登录后台…": 0.18,
    "正在下载三份数据…": 0.32,
    "正在整理数据格式…": 0.45,
    "正在写入在线表格…": 0.60,
    "正在切换到数据汇总表…": 0.72,
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


class _SheetMateController:
    """线程安全的配置、日志、任务执行与自动值守调度控制器。"""

    def __init__(self, app, glass, tk_root):
        self.app = app
        self.glass = glass
        self.tk_root = tk_root
        self._lock = threading.RLock()
        self._task_lock = threading.Lock()

        self.cfg = app.load_config()
        self.logs = []
        self._log_seq = 0

        self.job_running = False
        self.capture_running = False
        self.schedule_running = False
        self.stop_flag = False
        self.need_login_prompt = False

        self.status_level = "idle"
        self.step_text = "空闲中，等待开始"
        self.progress = 0.0
        self.error_text = ""
        self.last_run_str = "—"
        self.next_run_str = "—"
        self.next_run_ts = 0.0
        self.last_heartbeat = time.time()
        self.close_requested_at = 0.0

        self.update_info = None

        app.set_gui_logger(self._on_log)
        app.set_step_callback(self._on_step)
        app.set_last_run_callback(self._on_last_run)
        app.set_login_prompt(self._on_login_prompt_show, self._on_login_prompt_close)

        threading.Thread(target=self._check_update_worker, daemon=True).start()

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
            self.step_text = clean_text
            prog = STEP_PROGRESS.get(raw) or STEP_PROGRESS.get(clean_text)
            if prog is not None:
                self.progress = prog
                if not self.glass.closed:
                    self.glass.update_step(clean_text, prog)
            if raw.startswith("✅"):
                self.status_level = "ok"
            elif raw.startswith("❌"):
                self.status_level = "err"
            elif raw.startswith("⏹"):
                self.status_level = "stopped"
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

    def get_config(self):
        with self._lock:
            return json.loads(json.dumps(self.cfg))

    def save_config(self, new_cfg):
        with self._lock:
            merged = self.app._deep_merge(self.app.DEFAULT_CONFIG, new_cfg)
            self.app.save_config(merged)
            self.cfg = self.app.load_config()
            return json.loads(json.dumps(self.cfg))

    def reset_config(self):
        import copy
        with self._lock:
            default = copy.deepcopy(self.app.DEFAULT_CONFIG)
            self.app.save_config(default)
            self.cfg = self.app.load_config()
            return json.loads(json.dumps(self.cfg))

    def clear_logs(self):
        with self._lock:
            self.logs.clear()

    def get_state(self, since=0):
        with self._lock:
            self.last_heartbeat = time.time()
            delta = [item for item in self.logs if item["id"] > since]
            return {
                "version": self.app.APP_VERSION,
                "job_running": self.job_running,
                "capture_running": self.capture_running,
                "busy": self.job_running or self.capture_running,
                "schedule_running": self.schedule_running,
                "need_login_prompt": self.need_login_prompt,
                "status_level": self.status_level,
                "step_text": self.step_text,
                "progress": self.progress,
                "error_text": self.error_text,
                "last_run_str": self.last_run_str,
                "next_run_str": self.next_run_str,
                "next_run_ts": self.next_run_ts,
                "server_now": time.time(),
                "log_seq": self._log_seq,
                "logs": delta,
                "update_info": self.update_info,
            }

    def start_run_now(self):
        with self._lock:
            if self.job_running or self.capture_running:
                return False, "当前已有任务正在运行，请稍候"
            if not self._task_lock.acquire(blocking=False):
                return False, "当前已有任务正在运行，请稍候"
            self.job_running = True
            self.error_text = ""
            self.status_level = "running"
            self.progress = 0.02
            self.step_text = "准备开始手动任务…"
        threading.Thread(target=self._run_once_worker, args=("手动", True), daemon=True).start()
        return True, "已开始全流程任务"

    def start_capture_only(self):
        with self._lock:
            if self.job_running or self.capture_running:
                return False, "当前已有任务正在运行，请稍候"
            if not self._task_lock.acquire(blocking=False):
                return False, "当前已有任务正在运行，请稍候"
            self.capture_running = True
            self.error_text = ""
            self.status_level = "running"
            self.progress = 0.04
            self.step_text = "正在打开WPS汇总表…"
        threading.Thread(target=self._capture_only_worker, daemon=True).start()
        return True, "已开始仅截图发送"

    def _run_once_worker(self, round_name, owns_lock=True):
        self.glass.show()
        self.glass.update_step(f"正在运行第 {round_name} 轮…", 0.02)
        try:
            run_cfg = self.get_config()
            if isinstance(round_name, int) and not self.app.capture_allowed_now(run_cfg):
                run_cfg["enable_capture"] = False
                run_cfg["enable_dingtalk"] = False
                self.app.log("  当前不在截图发送时段，本轮只下载并更新表格")
                self._on_step("不在发群时段，仅更新表格")
            self.app.run_once(round_name, run_cfg)
            done_text = "手动任务已完成" if not isinstance(round_name, int) else f"第 {round_name} 轮已完成"
            with self._lock:
                self.status_level = "ok"
                self.step_text = done_text
                self.progress = 1.0
                self.error_text = ""
            self.glass.finish("本轮已完成")
            self.glass.close(1100)
        except Exception as exc:
            if "手动停止" in str(exc):
                self.app.log("已手动停止")
                with self._lock:
                    self.status_level = "stopped"
                    self.step_text = "已手动停止"
                    self.error_text = ""
                self.glass.fail("已手动停止")
                self.glass.close(700)
            else:
                self.app.log_error(exc, f"执行失败-{round_name}")
                tip = _strip_symbols(self.app.human_error(exc))
                self.app.log(f"❌ 本轮失败：{tip}")
                with self._lock:
                    self.status_level = "err"
                    self.step_text = "运行失败，请查看详情"
                    self.error_text = tip
                self.glass.fail(tip)
                self.glass.close(1800)
        finally:
            with self._lock:
                self.job_running = False
                self.need_login_prompt = False
            if owns_lock:
                try:
                    self._task_lock.release()
                except Exception:
                    pass

    def _capture_only_worker(self):
        self.glass.show()
        self.glass.update_step("正在打开WPS汇总表…", 0.05)
        try:
            run_cfg = self.get_config()
            self.app.run_capture_only("截图", run_cfg)
            with self._lock:
                self.status_level = "ok"
                self.step_text = "截图发送已完成"
                self.progress = 1.0
                self.error_text = ""
            self.glass.finish("截图发送已完成")
            self.glass.close(1400)
        except Exception as exc:
            if "手动停止" in str(exc):
                self.app.log("已手动停止")
                with self._lock:
                    self.status_level = "stopped"
                    self.step_text = "已手动停止"
                    self.error_text = ""
                self.glass.fail("已手动停止")
                self.glass.close(700)
            else:
                self.app.log_error(exc, "独立截图失败")
                tip = _strip_symbols(self.app.human_error(exc))
                self.app.log(f"❌ 独立截图失败：{tip}")
                with self._lock:
                    self.status_level = "err"
                    self.step_text = "截图发送失败"
                    self.error_text = tip
                self.glass.fail(tip)
                self.glass.close(1800)
        finally:
            with self._lock:
                self.capture_running = False
                self.need_login_prompt = False
            try:
                self._task_lock.release()
            except Exception:
                pass

    def stop_current_task(self):
        self.app.request_stop()
        with self._lock:
            if self.schedule_running:
                self.stop_flag = True
        return True, "已发送停止指令"

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
            day_gap = cfg["day"]["ih"] * 60 + cfg["day"]["im"]
            night_gap = cfg["night"]["ih"] * 60 + cfg["night"]["im"]
            if day_gap < 1 or (cfg.get("night_enabled", True) and night_gap < 1):
                return False, "白天或夜间的执行间隔不能为 0 分钟", False

            self.schedule_running = True
            self.stop_flag = False
            self.error_text = ""
        threading.Thread(target=self._schedule_loop, daemon=True).start()
        return True, "已开启自动值守", True

    def _compute_next_slot_start(self, cfg, now_ts):
        slots = [f"{cfg['day']['sh']:02d}:{cfg['day']['sm']:02d}"]
        if cfg.get("night_enabled", True):
            slots.append(f"{cfg['night']['sh']:02d}:{cfg['night']['sm']:02d}")
        slot_ends = {slots[0]: f"{cfg['day']['eh']:02d}:{cfg['day']['em']:02d}"}
        if len(slots) > 1:
            slot_ends[slots[1]] = f"{cfg['night']['eh']:02d}:{cfg['night']['em']:02d}"

        day_mode = cfg.get("day_mode", "daily")
        base_min = int(now_ts // 60) * 60
        for offset_min in range(1, 4 * 24 * 60):
            ts = base_min + offset_min * 60
            lt = time.localtime(ts)
            if day_mode == "workday" and lt.tm_wday >= 5:
                continue
            for st in slots:
                if self.app.in_slot(lt, st, slot_ends[st]):
                    return ts
        return 0.0

    def _schedule_loop(self):
        round_num = 1
        last_run_start = 0.0
        while not self.stop_flag:
            cfg = self.get_config()
            now_ts = time.time()
            now = time.localtime(now_ts)

            if cfg.get("day_mode", "daily") == "workday" and now.tm_wday >= 5:
                nxt = self._compute_next_slot_start(cfg, now_ts)
                with self._lock:
                    if not self.job_running and not self.capture_running:
                        self.status_level = "idle"
                        self.step_text = "非工作日，自动值守休眠中"
                    self.next_run_ts = nxt
                    self.next_run_str = time.strftime("%m-%d %H:%M", time.localtime(nxt)) if nxt else "—"
                for _ in range(10):
                    if self.stop_flag:
                        break
                    time.sleep(1)
                continue

            day_start = f"{cfg['day']['sh']:02d}:{cfg['day']['sm']:02d}"
            day_end = f"{cfg['day']['eh']:02d}:{cfg['day']['em']:02d}"
            night_start = f"{cfg['night']['sh']:02d}:{cfg['night']['sm']:02d}"
            night_end = f"{cfg['night']['eh']:02d}:{cfg['night']['em']:02d}"

            slot = None
            if self.app.in_slot(now, day_start, day_end):
                slot = "day"
            elif cfg.get("night_enabled", True) and self.app.in_slot(now, night_start, night_end):
                slot = "night"

            if slot is None:
                nxt = self._compute_next_slot_start(cfg, now_ts)
                with self._lock:
                    if not self.job_running and not self.capture_running:
                        self.status_level = "idle"
                        self.step_text = "不在运行时段，静默待命中"
                    self.next_run_ts = nxt
                    self.next_run_str = time.strftime("%H:%M", time.localtime(nxt)) if nxt else "—"
                for _ in range(10):
                    if self.stop_flag:
                        break
                    time.sleep(1)
                continue

            gap_min = (cfg["day"]["ih"] * 60 + cfg["day"]["im"]) if slot == "day" else (cfg["night"]["ih"] * 60 + cfg["night"]["im"])
            interval_sec = max(60, gap_min * 60)
            since = now_ts - last_run_start

            if (last_run_start == 0.0 or since >= interval_sec) and not self.job_running and not self.capture_running:
                if self._task_lock.acquire(blocking=False):
                    last_run_start = time.time()
                    with self._lock:
                        self.job_running = True
                        self.status_level = "running"
                        self.progress = 0.02
                        self.error_text = ""
                        self.next_run_ts = last_run_start + interval_sec
                        self.next_run_str = time.strftime("%H:%M", time.localtime(self.next_run_ts))
                    self._run_once_worker(round_num, owns_lock=True)
                    round_num += 1
                    continue

            next_ts = (last_run_start + interval_sec) if last_run_start else now_ts
            with self._lock:
                if not self.job_running and not self.capture_running:
                    slot_label = "白天" if slot == "day" else "夜间"
                    self.status_level = "idle"
                    self.step_text = f"自动值守中（{slot_label}时段）"
                self.next_run_ts = next_ts
                self.next_run_str = time.strftime("%H:%M", time.localtime(next_ts))

            for _ in range(5):
                if self.stop_flag:
                    break
                time.sleep(1)

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
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Cdefs%3E%3ClinearGradient id='g' x1='0' y1='0' x2='1' y2='1'%3E%3Cstop offset='0%25' stop-color='%23007AFF'/%3E%3Cstop offset='100%25' stop-color='%235856D6'/%3E%3C/linearGradient%3E%3C/defs%3E%3Crect width='64' height='64' rx='15' fill='url(%23g)'/%3E%3Crect x='14' y='34' width='9' height='16' rx='3' fill='white'/%3E%3Crect x='27.5' y='24' width='9' height='26' rx='3' fill='white'/%3E%3Crect x='41' y='14' width='9' height='36' rx='3' fill='white'/%3E%3C/svg%3E" />
<style>
  :root {
    --bg: #F2F2F7;
    --nav: rgba(242, 242, 247, 0.78);
    --tabbar: rgba(249, 249, 251, 0.86);
    --card: #FFFFFF;
    --card-press: #F5F5F8;
    --sep: rgba(60, 60, 67, 0.12);
    --sep-strong: rgba(60, 60, 67, 0.22);
    --text: #000000;
    --text-2: #3C3C43;
    --sub: #8E8E93;
    --sub-2: #AEAEB2;
    --fill: rgba(120, 120, 128, 0.12);
    --fill-2: rgba(120, 120, 128, 0.08);
    --pill-blue: rgba(0, 122, 255, 0.10);
    --switch-off: rgba(120, 120, 128, 0.20);
    --shadow: 0 2px 14px rgba(0, 0, 0, 0.045);
    --shadow-lg: 0 12px 32px rgba(0, 0, 0, 0.12);
    --sheet-bg: rgba(255, 255, 255, 0.94);
    --blue: #007AFF;
    --green: #34C759;
    --orange: #FF9500;
    --red: #FF3B30;
    --indigo: #5856D6;
    --teal: #32ADE6;
    --purple: #AF52DE;
    --pink: #FF2D55;
    --spring: cubic-bezier(0.32, 0.72, 0, 1);
  }
  [data-theme="dark"] {
    --bg: #000000;
    --nav: rgba(16, 16, 18, 0.82);
    --tabbar: rgba(22, 22, 24, 0.88);
    --card: #1C1C1E;
    --card-press: #262629;
    --sep: rgba(84, 84, 88, 0.42);
    --sep-strong: rgba(84, 84, 88, 0.65);
    --text: #FFFFFF;
    --text-2: #EBEBF5;
    --sub: #8E8E93;
    --sub-2: #636366;
    --fill: rgba(120, 120, 128, 0.28);
    --fill-2: rgba(120, 120, 128, 0.18);
    --pill-blue: rgba(10, 132, 255, 0.18);
    --switch-off: rgba(120, 120, 128, 0.34);
    --shadow: 0 4px 20px rgba(0, 0, 0, 0.45);
    --shadow-lg: 0 18px 40px rgba(0, 0, 0, 0.55);
    --sheet-bg: rgba(28, 28, 30, 0.94);
    --blue: #0A84FF;
    --green: #30D158;
    --orange: #FF9F0A;
    --red: #FF453A;
    --indigo: #5E5CE6;
    --teal: #64D2FF;
    --purple: #BF5AF2;
    --pink: #FF375F;
  }

  * {
    box-sizing: border-box;
    -webkit-tap-highlight-color: transparent;
  }
  html, body {
    margin: 0;
    padding: 0;
    min-height: 100vh;
    background: var(--bg);
    color: var(--text);
    font-family: "SF Pro Text", "SF Pro Display", -apple-system, BlinkMacSystemFont, "PingFang SC", "Helvetica Neue", "Microsoft YaHei UI", sans-serif;
    font-size: 14px;
    line-height: 1.4;
    letter-spacing: -0.01em;
    -webkit-font-smoothing: antialiased;
    overflow-x: hidden;
    user-select: none;
  }
  body::before {
    content: "";
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    height: 220px;
    background: radial-gradient(120% 200px at 50% 0%, rgba(0, 122, 255, 0.08), rgba(88, 86, 214, 0.03) 58%, transparent 100%);
    pointer-events: none;
    z-index: 0;
  }
  [data-theme="dark"] body::before {
    background: radial-gradient(120% 200px at 50% 0%, rgba(10, 132, 255, 0.14), rgba(94, 92, 230, 0.06) 58%, transparent 100%);
  }

  ::-webkit-scrollbar { width: 4px; height: 4px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: rgba(142, 142, 147, 0.30); border-radius: 99px; }

  .shell {
    position: relative;
    z-index: 1;
    max-width: 430px;
    margin: 0 auto;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
  }

  /* 顶部毛玻璃导航 + 灵动岛 */
  .top-header {
    position: sticky;
    top: 0;
    z-index: 40;
    padding: 10px 16px 10px;
    background: var(--nav);
    backdrop-filter: saturate(190%) blur(28px);
    -webkit-backdrop-filter: saturate(190%) blur(28px);
    border-bottom: 0.5px solid var(--sep);
  }

  .island-slot {
    display: flex;
    justify-content: center;
    margin-bottom: 10px;
  }
  .island {
    background: #0B0B0D;
    color: #FFFFFF;
    border-radius: 999px;
    padding: 7px 14px;
    min-width: 196px;
    max-width: 100%;
    box-shadow: 0 6px 22px rgba(0, 0, 0, 0.18), inset 0 0 0 0.5px rgba(255, 255, 255, 0.14);
    transition: all 0.36s var(--spring);
  }
  .island.expanded {
    width: 100%;
    border-radius: 22px;
    padding: 12px 14px;
    box-shadow: 0 10px 28px rgba(0, 0, 0, 0.24), inset 0 0 0 0.5px rgba(255, 255, 255, 0.16);
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
    box-shadow: 0 0 0 3px rgba(48, 209, 88, 0.22);
    flex-shrink: 0;
    transition: background 0.25s;
  }
  .island-dot.err { background: #FF453A; box-shadow: 0 0 0 3px rgba(255, 69, 58, 0.25); }
  .island-dot.stopped { background: #8E8E93; box-shadow: none; }
  .island-dot.sched {
    background: #30D158;
    animation: dot-breathe 1.8s ease-in-out infinite;
  }
  @keyframes dot-breathe {
    0%, 100% { box-shadow: 0 0 0 2px rgba(48, 209, 88, 0.22); }
    50% { box-shadow: 0 0 0 5px rgba(48, 209, 88, 0.42); }
  }

  .ios-spinner {
    position: relative;
    width: 15px;
    height: 15px;
    flex-shrink: 0;
    display: none;
  }
  .island.busy .ios-spinner { display: block; }
  .island.busy .island-dot { display: none; }
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
  @keyframes ios-spin {
    0% { opacity: 1; }
    100% { opacity: 0.16; }
  }

  .island-text {
    font-size: 12.5px;
    font-weight: 600;
    color: rgba(255, 255, 255, 0.95);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .island-pill-tag {
    font-size: 11px;
    font-weight: 600;
    color: rgba(255, 255, 255, 0.7);
    font-variant-numeric: tabular-nums;
    flex-shrink: 0;
  }
  .island-stop {
    display: none;
    border: none;
    background: #FF453A;
    color: #FFFFFF;
    font-size: 11.5px;
    font-weight: 600;
    padding: 4px 10px;
    border-radius: 999px;
    cursor: pointer;
    align-items: center;
    gap: 4px;
    flex-shrink: 0;
  }
  .island.busy .island-stop { display: inline-flex; }
  .island.busy .island-pill-tag { display: none; }
  .island-stop:active { transform: scale(0.92); }

  .island-prog {
    display: none;
    align-items: center;
    gap: 8px;
    margin-top: 9px;
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
    transition: width 0.35s var(--spring);
  }
  .island-pct {
    font-size: 11px;
    font-weight: 600;
    color: rgba(255, 255, 255, 0.85);
    min-width: 30px;
    text-align: right;
    font-variant-numeric: tabular-nums;
  }

  .island-alert {
    display: none;
    margin-top: 8px;
    padding: 7px 10px;
    border-radius: 12px;
    font-size: 11.5px;
    font-weight: 500;
    line-height: 1.35;
    word-break: break-word;
  }
  .island-alert.err {
    display: block;
    background: rgba(255, 69, 58, 0.22);
    color: #FF8A84;
  }
  .island-alert.login {
    display: block;
    background: rgba(10, 132, 255, 0.24);
    color: #7CC4FF;
  }

  /* iOS Large Title Bar */
  .nav-bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
  }
  .large-title-wrap {
    display: flex;
    align-items: baseline;
    gap: 8px;
  }
  .large-title {
    margin: 0;
    font-size: 25px;
    font-weight: 700;
    letter-spacing: -0.6px;
    color: var(--text);
  }
  .ver-pill {
    font-size: 11px;
    font-weight: 600;
    padding: 2px 8px;
    border-radius: 999px;
    background: var(--pill-blue);
    color: var(--blue);
    font-variant-numeric: tabular-nums;
  }
  .nav-actions {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .nav-circle-btn {
    position: relative;
    width: 32px;
    height: 32px;
    border: none;
    border-radius: 50%;
    background: var(--fill);
    color: var(--text);
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    transition: transform 0.16s var(--spring), background 0.2s;
  }
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

  /* 主内容滚动区 */
  .main-content {
    flex: 1;
    padding: 14px 16px 94px;
  }
  .tab-pane {
    display: none;
    flex-direction: column;
    gap: 18px;
    animation: pane-in 0.22s var(--spring);
  }
  .tab-pane.active { display: flex; }
  @keyframes pane-in {
    from { opacity: 0; transform: translateY(4px); }
    to { opacity: 1; transform: translateY(0); }
  }

  /* Shortcuts 风格主运行卡 */
  .hero-action {
    border: none;
    width: 100%;
    text-align: left;
    border-radius: 20px;
    padding: 18px 18px;
    background: linear-gradient(135deg, #007AFF 0%, #1B64F2 52%, #4C46D6 100%);
    color: #FFFFFF;
    box-shadow: 0 10px 26px rgba(0, 122, 255, 0.28), inset 0 1px 0 rgba(255, 255, 255, 0.25);
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 14px;
    cursor: pointer;
    transition: transform 0.18s var(--spring), opacity 0.2s;
  }
  .hero-action:active:not(:disabled) { transform: scale(0.975); }
  .hero-action:disabled { opacity: 0.55; cursor: not-allowed; }
  .hero-copy {
    display: flex;
    flex-direction: column;
    gap: 4px;
    min-width: 0;
  }
  .hero-title {
    font-size: 18px;
    font-weight: 700;
    letter-spacing: -0.3px;
  }
  .hero-sub {
    font-size: 12.5px;
    color: rgba(255, 255, 255, 0.85);
  }
  .hero-play {
    width: 42px;
    height: 42px;
    border-radius: 50%;
    background: rgba(255, 255, 255, 0.22);
    backdrop-filter: blur(8px);
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
  }
  .hero-play svg { width: 18px; height: 18px; color: #FFFFFF; margin-left: 2px; }

  /* 双栏控制中心磁贴 */
  .tile-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
  }
  .cc-tile {
    border: 0.5px solid var(--sep);
    background: var(--card);
    color: var(--text);
    border-radius: 18px;
    padding: 15px 14px 14px;
    box-shadow: var(--shadow);
    cursor: pointer;
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 10px;
    text-align: left;
    transition: transform 0.18s var(--spring), background 0.25s, border-color 0.25s, box-shadow 0.25s;
  }
  .cc-tile:active:not(:disabled) { transform: scale(0.96); }
  .cc-tile:disabled { opacity: 0.5; cursor: not-allowed; }
  .tile-ico {
    width: 36px;
    height: 36px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    color: #FFFFFF;
  }
  .tile-ico svg { width: 18px; height: 18px; }
  .tile-ico.orange { background: var(--orange); }
  .tile-ico.green { background: var(--green); }
  .tile-title { font-size: 14.5px; font-weight: 700; letter-spacing: -0.2px; }
  .tile-sub { font-size: 11.5px; color: var(--sub); line-height: 1.3; }

  .cc-tile.schedule-on {
    background: linear-gradient(145deg, #34C759 0%, #248A3D 100%);
    color: #FFFFFF;
    border-color: transparent;
    box-shadow: 0 8px 22px rgba(52, 199, 89, 0.30);
  }
  .cc-tile.schedule-on .tile-ico {
    background: rgba(255, 255, 255, 0.24);
  }
  .cc-tile.schedule-on .tile-sub {
    color: rgba(255, 255, 255, 0.88);
  }

  /* iOS 18 设置分组列表 */
  .section-wrap {
    display: flex;
    flex-direction: column;
  }
  .section-head {
    padding: 0 14px 6px;
    font-size: 12.5px;
    font-weight: 500;
    color: var(--sub);
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .section-footer {
    padding: 7px 14px 0;
    font-size: 12px;
    color: var(--sub);
    line-height: 1.45;
  }
  .section-footer.warn { color: var(--orange); }

  .ios-group {
    background: var(--card);
    border-radius: 14px;
    box-shadow: var(--shadow);
    border: 0.5px solid var(--sep);
    overflow: hidden;
  }

  .ios-row {
    position: relative;
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 11px 14px;
    min-height: 48px;
    background: var(--card);
    transition: background 0.15s;
  }
  .ios-row.clickable { cursor: pointer; }
  .ios-row.clickable:active { background: var(--card-press); }
  .ios-row + .ios-row::before {
    content: "";
    position: absolute;
    left: 56px;
    right: 0;
    top: 0;
    height: 0.5px;
    background: var(--sep);
  }
  .ios-row.no-icon-sep + .ios-row::before {
    left: 14px;
  }

  /* Apple 30x30 连续圆角方形 SF 图标块 */
  .sf-badge {
    width: 30px;
    height: 30px;
    border-radius: 7.5px;
    display: flex;
    align-items: center;
    justify-content: center;
    color: #FFFFFF;
    flex-shrink: 0;
    box-shadow: inset 0 0.5px 0 rgba(255, 255, 255, 0.25);
  }
  .sf-badge svg { width: 17px; height: 17px; }
  .sf-blue { background: linear-gradient(180deg, #0A84FF, #007AFF); }
  .sf-green { background: linear-gradient(180deg, #32D74B, #28CD41); }
  .sf-indigo { background: linear-gradient(180deg, #6E6CEB, #5856D6); }
  .sf-orange { background: linear-gradient(180deg, #FFA726, #FF9500); }
  .sf-teal { background: linear-gradient(180deg, #5AC8FA, #32ADE6); }
  .sf-purple { background: linear-gradient(180deg, #BF5AF2, #AF52DE); }
  .sf-pink { background: linear-gradient(180deg, #FF375F, #FF2D55); }
  .sf-red { background: linear-gradient(180deg, #FF453A, #FF3B30); }
  .sf-gray { background: linear-gradient(180deg, #98989D, #8E8E93); }

  .row-main {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 1.5px;
  }
  .row-title {
    font-size: 14.5px;
    font-weight: 500;
    color: var(--text);
  }
  .row-sub {
    font-size: 12px;
    color: var(--sub);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .row-right {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-shrink: 0;
    max-width: 52%;
  }
  .row-val {
    font-size: 13.5px;
    color: var(--sub);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    font-variant-numeric: tabular-nums;
  }
  .chevron-svg {
    width: 13px;
    height: 13px;
    color: var(--sub-2);
    flex-shrink: 0;
    transition: transform 0.25s var(--spring);
  }

  /* 内联清爽无框输入框 */
  .field-inline {
    display: flex;
    align-items: center;
    gap: 6px;
    flex: 1;
    min-width: 0;
  }
  .field-inline input {
    border: none;
    outline: none;
    background: transparent;
    color: var(--blue);
    font-family: inherit;
    font-size: 14px;
    font-weight: 500;
    text-align: right;
    text-overflow: ellipsis;
    width: 100%;
    min-width: 0;
    user-select: text;
  }
  .field-inline input::placeholder {
    color: var(--sub-2);
    font-weight: 400;
  }
  .field-inline input.left-align {
    text-align: left;
    color: var(--text);
  }
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
  }
  .clear-circle svg { width: 10px; height: 10px; }

  /* 苹果 51x31 弹簧开关 */
  .ios-switch {
    position: relative;
    display: inline-block;
    width: 51px;
    height: 31px;
    flex-shrink: 0;
  }
  .ios-switch input {
    opacity: 0;
    width: 0;
    height: 0;
  }
  .ios-slider {
    position: absolute;
    inset: 0;
    cursor: pointer;
    background: var(--switch-off);
    border-radius: 31px;
    transition: background 0.24s var(--spring);
  }
  .ios-slider::before {
    content: "";
    position: absolute;
    height: 27px;
    width: 27px;
    left: 2px;
    top: 2px;
    background: #FFFFFF;
    border-radius: 50%;
    box-shadow: 0 2px 5px rgba(0, 0, 0, 0.20), 0 0 1px rgba(0, 0, 0, 0.08);
    transition: transform 0.26s var(--spring);
  }
  .ios-switch input:checked + .ios-slider { background: var(--green); }
  .ios-switch input:checked + .ios-slider::before { transform: translateX(20px); }

  /* 滑块分段控件 */
  .segmented {
    position: relative;
    display: grid;
    grid-template-columns: 1fr 1fr;
    background: var(--fill);
    border-radius: 9px;
    padding: 2px;
    min-width: 158px;
  }
  .seg-thumb {
    position: absolute;
    top: 2px;
    bottom: 2px;
    left: 2px;
    width: calc(50% - 2px);
    background: var(--card);
    border-radius: 7px;
    box-shadow: 0 1px 4px rgba(0, 0, 0, 0.12);
    transition: transform 0.26s var(--spring);
  }
  [data-theme="dark"] .seg-thumb { background: #3A3A3C; }
  .seg-btn {
    position: relative;
    z-index: 1;
    border: none;
    background: transparent;
    color: var(--text);
    font-family: inherit;
    font-size: 12.5px;
    font-weight: 600;
    padding: 5px 10px;
    cursor: pointer;
    text-align: center;
  }

  /* 时段卡片与纯苹果胶囊（无原生 ugly select / time input） */
  .slot-block {
    position: relative;
    padding: 12px 14px;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .slot-block + .slot-block::before,
  .ios-row + .slot-block::before,
  .slot-block + .ios-row::before {
    content: "";
    position: absolute;
    left: 56px;
    right: 0;
    top: 0;
    height: 0.5px;
    background: var(--sep);
  }
  .slot-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
  }
  .slot-title-wrap {
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .slot-controls {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 8px;
    padding-left: 42px;
    transition: opacity 0.2s;
  }
  .slot-controls.disabled {
    opacity: 0.38;
    pointer-events: none;
  }
  .pill-pair {
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .ios-pill-btn {
    border: none;
    background: var(--pill-blue);
    color: var(--blue);
    font-family: inherit;
    font-size: 13.5px;
    font-weight: 600;
    padding: 5px 11px;
    border-radius: 9px;
    cursor: pointer;
    font-variant-numeric: tabular-nums;
    transition: transform 0.14s var(--spring), background 0.16s;
  }
  .ios-pill-btn:active { transform: scale(0.94); }
  .ios-pill-btn.neutral {
    background: var(--fill);
    color: var(--text);
  }
  .pill-sep {
    font-size: 12px;
    color: var(--sub);
  }

  .accordion-body {
    max-height: 0;
    overflow: hidden;
    transition: max-height 0.32s var(--spring);
  }
  .accordion-body.open {
    max-height: 460px;
  }

  /* 日志控制台 */
  .log-tools {
    display: flex;
    gap: 6px;
  }
  .mini-pill {
    border: none;
    background: var(--fill);
    color: var(--blue);
    font-family: inherit;
    font-size: 11.5px;
    font-weight: 600;
    padding: 4px 10px;
    border-radius: 999px;
    cursor: pointer;
  }
  .mini-pill.active {
    background: var(--blue);
    color: #FFFFFF;
  }
  .log-box {
    background: var(--card);
    padding: 10px 14px;
    height: 220px;
    overflow-y: auto;
    font-family: "SF Mono", "Cascadia Mono", "Consolas", "Microsoft YaHei UI", monospace;
    font-size: 11.5px;
    line-height: 1.55;
    user-select: text;
  }
  .log-empty {
    color: var(--sub);
    text-align: center;
    padding-top: 84px;
    font-family: inherit;
    font-size: 12.5px;
  }
  .log-line {
    display: flex;
    align-items: flex-start;
    gap: 6px;
    margin-bottom: 3px;
    word-break: break-all;
  }
  .log-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    margin-top: 6px;
    flex-shrink: 0;
    background: var(--sub-2);
  }
  .log-dot.ok { background: var(--green); }
  .log-dot.err { background: var(--red); }
  .log-dot.warn { background: var(--orange); }
  .log-dot.head { background: var(--blue); }
  .log-time { color: var(--sub); flex-shrink: 0; font-variant-numeric: tabular-nums; }
  .log-msg.ok { color: var(--green); font-weight: 500; }
  .log-msg.err { color: var(--red); font-weight: 600; }
  .log-msg.warn { color: var(--orange); }
  .log-msg.head { color: var(--blue); font-weight: 600; }

  /* 独立红色 iOS 操作卡 */
  .danger-card-btn {
    width: 100%;
    border: 0.5px solid var(--sep);
    background: var(--card);
    color: var(--red);
    font-family: inherit;
    font-size: 15px;
    font-weight: 500;
    padding: 13px;
    border-radius: 14px;
    box-shadow: var(--shadow);
    cursor: pointer;
    transition: background 0.15s, transform 0.16s var(--spring);
  }
  .danger-card-btn:active {
    background: var(--card-press);
    transform: scale(0.985);
  }

  /* 底部固定毛玻璃 4 栏 Tab Bar */
  .tabbar-wrap {
    position: fixed;
    left: 0;
    right: 0;
    bottom: 0;
    z-index: 50;
    display: flex;
    justify-content: center;
    pointer-events: none;
  }
  .tabbar {
    pointer-events: auto;
    width: 100%;
    max-width: 430px;
    height: 64px;
    padding: 6px 8px 10px;
    background: var(--tabbar);
    backdrop-filter: saturate(190%) blur(28px);
    -webkit-backdrop-filter: saturate(190%) blur(28px);
    border-top: 0.5px solid var(--sep);
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    align-items: center;
  }
  .tab-btn {
    border: none;
    background: transparent;
    color: var(--sub);
    font-family: inherit;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 3px;
    cursor: pointer;
    padding: 2px 0;
    transition: color 0.18s, transform 0.14s var(--spring);
  }
  .tab-btn:active { transform: scale(0.92); }
  .tab-btn svg { width: 22px; height: 22px; }
  .tab-btn span { font-size: 10.5px; font-weight: 500; }
  .tab-btn.active { color: var(--blue); }
  .tab-btn.active span { font-weight: 600; }

  /* 底部毛玻璃滚轮选择器（替代原生 Ugly select & input[type=time]） */
  .sheet-mask {
    position: fixed;
    inset: 0;
    z-index: 85;
    background: rgba(0, 0, 0, 0.34);
    backdrop-filter: blur(4px);
    display: none;
    align-items: flex-end;
    justify-content: center;
  }
  .sheet-mask.show { display: flex; }
  .ios-sheet {
    width: 100%;
    max-width: 430px;
    background: var(--sheet-bg);
    backdrop-filter: saturate(190%) blur(32px);
    -webkit-backdrop-filter: saturate(190%) blur(32px);
    border-radius: 22px 22px 0 0;
    box-shadow: var(--shadow-lg);
    border-top: 0.5px solid var(--sep);
    padding-bottom: 18px;
    animation: sheet-up 0.26s var(--spring);
  }
  @keyframes sheet-up {
    from { transform: translateY(100%); }
    to { transform: translateY(0); }
  }
  .sheet-handle {
    width: 36px;
    height: 4.5px;
    border-radius: 999px;
    background: var(--sep-strong);
    margin: 9px auto 4px;
  }
  .sheet-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 8px 16px 12px;
    border-bottom: 0.5px solid var(--sep);
  }
  .sheet-btn {
    border: none;
    background: transparent;
    color: var(--blue);
    font-family: inherit;
    font-size: 15px;
    font-weight: 500;
    cursor: pointer;
    padding: 4px 6px;
  }
  .sheet-btn.done { font-weight: 700; }
  .sheet-title { font-size: 15px; font-weight: 600; color: var(--text); }
  .sheet-readout {
    text-align: center;
    font-size: 26px;
    font-weight: 700;
    color: var(--text);
    padding: 12px 16px 8px;
    letter-spacing: -0.5px;
    font-variant-numeric: tabular-nums;
  }
  .preset-row {
    display: flex;
    flex-wrap: wrap;
    justify-content: center;
    gap: 7px;
    padding: 0 16px 12px;
  }
  .preset-chip {
    border: none;
    background: var(--fill);
    color: var(--text);
    font-family: inherit;
    font-size: 12px;
    font-weight: 600;
    padding: 5px 11px;
    border-radius: 999px;
    cursor: pointer;
  }
  .preset-chip.active {
    background: var(--blue);
    color: #FFFFFF;
  }
  .wheel-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
    padding: 0 20px;
  }
  .wheel-col-label {
    text-align: center;
    font-size: 11.5px;
    font-weight: 600;
    color: var(--sub);
    margin-bottom: 6px;
  }
  .wheel-list {
    height: 184px;
    overflow-y: auto;
    border-radius: 14px;
    background: var(--fill-2);
    padding: 6px;
    display: flex;
    flex-direction: column;
    gap: 3px;
  }
  .wheel-opt {
    border: none;
    background: transparent;
    color: var(--text);
    font-family: inherit;
    font-size: 15px;
    font-weight: 500;
    padding: 7px 0;
    border-radius: 9px;
    cursor: pointer;
    font-variant-numeric: tabular-nums;
  }
  .wheel-opt.selected {
    background: var(--card);
    color: var(--blue);
    font-weight: 700;
    box-shadow: 0 1px 4px rgba(0, 0, 0, 0.08);
  }

  /* iOS 毛玻璃 Alert 弹窗 */
  .modal-mask {
    position: fixed;
    inset: 0;
    z-index: 95;
    background: rgba(0, 0, 0, 0.36);
    backdrop-filter: blur(6px);
    display: none;
    align-items: center;
    justify-content: center;
    padding: 24px;
  }
  .modal-mask.show { display: flex; }
  .ios-alert {
    width: 280px;
    background: var(--sheet-bg);
    backdrop-filter: saturate(180%) blur(28px);
    border-radius: 16px;
    overflow: hidden;
    box-shadow: var(--shadow-lg);
    border: 0.5px solid var(--sep);
    animation: pop-in 0.22s var(--spring);
  }
  @keyframes pop-in {
    from { opacity: 0; transform: scale(1.08); }
    to { opacity: 1; transform: scale(1); }
  }
  .alert-body {
    padding: 18px 16px 15px;
    text-align: center;
  }
  .alert-title { font-size: 16px; font-weight: 700; margin-bottom: 5px; }
  .alert-msg { font-size: 13px; color: var(--text-2); line-height: 1.4; }
  .alert-actions {
    display: grid;
    grid-template-columns: 1fr 1fr;
    border-top: 0.5px solid var(--sep);
  }
  .alert-actions.single { grid-template-columns: 1fr; }
  .alert-btn {
    border: none;
    background: transparent;
    padding: 12px 8px;
    font-family: inherit;
    font-size: 15.5px;
    font-weight: 500;
    color: var(--blue);
    cursor: pointer;
  }
  .alert-btn + .alert-btn { border-left: 0.5px solid var(--sep); }
  .alert-btn.bold { font-weight: 700; }
  .alert-btn.danger { color: var(--red); font-weight: 700; }

  /* 顶部 Toast */
  .toast {
    position: fixed;
    top: 16px;
    left: 50%;
    transform: translate(-50%, -76px);
    z-index: 100;
    background: rgba(28, 28, 30, 0.92);
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
</style>
</head>
<body>
  <div class="toast" id="toast">已保存</div>

  <div class="shell">
    <!-- 顶部粘性导航 + 灵动岛 -->
    <header class="top-header">
      <div class="island-slot">
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
            <button class="island-stop" id="stopBtn" title="停止任务">
              <svg viewBox="0 0 16 16" fill="currentColor" style="width:10px;height:10px;"><rect x="3" y="3" width="10" height="10" rx="2"/></svg>
              <span>停止</span>
            </button>
          </div>
          <div class="island-prog">
            <div class="island-track"><div class="island-fill" id="progressFill"></div></div>
            <div class="island-pct" id="progressPct">0%</div>
          </div>
          <div class="island-alert" id="islandAlert"></div>
        </div>
      </div>

      <div class="nav-bar">
        <div class="large-title-wrap">
          <h1 class="large-title">暗号表助手</h1>
          <span class="ver-pill" id="verBadge">v2.0.0</span>
        </div>
        <div class="nav-actions">
          <button class="nav-circle-btn" id="updateBtn" title="检查更新" style="display:none;">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12m0 0l-4-4m4 4l4-4"/><path d="M4 17v2a2 2 0 002 2h12a2 2 0 002-2v-2"/></svg>
            <span class="update-dot" id="updateDot"></span>
          </button>
          <button class="nav-circle-btn" id="themeBtn" title="切换外观"></button>
        </div>
      </div>
    </header>

    <main class="main-content">
      <!-- Tab 1: 控制台 -->
      <section class="tab-pane active" id="pane-home">
        <button class="hero-action" id="btnNow">
          <div class="hero-copy">
            <div class="hero-title">立即开始全流程</div>
            <div class="hero-sub">下载三份数据 · 填入在线表 · 截图发钉钉</div>
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
              <div class="tile-sub">仅截取汇总表并送达钉钉</div>
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
          <div class="section-head"><span>当前配置概览</span><span>点击可快速修改</span></div>
          <div class="ios-group">
            <div class="ios-row clickable" data-jump="dingtalk">
              <div class="sf-badge sf-indigo">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 2L11 13"/><path d="M22 2l-7 20-4-9-9-4 20-7z"/></svg>
              </div>
              <div class="row-main">
                <div class="row-title">目标钉钉会话</div>
                <div class="row-sub">打开后按 Ctrl+Shift+F 粘贴并回车</div>
              </div>
              <div class="row-right">
                <span class="row-val" id="sumTarget">文件小助手</span>
                <svg class="chevron-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18l6-6-6-6"/></svg>
              </div>
            </div>

            <div class="ios-row clickable" data-jump="dingtalk">
              <div class="sf-badge sf-orange">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M13.73 21a2 2 0 01-3.46 0"/><path d="M18.63 13A17.89 17.89 0 0118 8"/><path d="M6.26 6.26A5.86 5.86 0 006 8c0 7-3 9-3 9h14"/><path d="M18 8a6 6 0 00-9.33-5"/><line x1="1" y1="1" x2="23" y2="23"/></svg>
              </div>
              <div class="row-main">
                <div class="row-title">发送策略</div>
                <div class="row-sub">草稿清理 · 静音 · 确认方式</div>
              </div>
              <div class="row-right">
                <span class="row-val" id="sumPolicy">静音 · 暂不发</span>
                <svg class="chevron-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18l6-6-6-6"/></svg>
              </div>
            </div>

            <div class="ios-row clickable" data-jump="schedule">
              <div class="sf-badge sf-green">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="3"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>
              </div>
              <div class="row-main">
                <div class="row-title">定时计划</div>
                <div class="row-sub" id="sumScheduleSub">每天运行</div>
              </div>
              <div class="row-right">
                <span class="row-val" id="sumNextRun">—</span>
                <svg class="chevron-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18l6-6-6-6"/></svg>
              </div>
            </div>

            <div class="ios-row clickable" data-jump="more">
              <div class="sf-badge sf-gray">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 109-9 9.75 9.75 0 00-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l3 2"/></svg>
              </div>
              <div class="row-main">
                <div class="row-title">上次完成时间</div>
                <div class="row-sub">查看详细运行日志记录</div>
              </div>
              <div class="row-right">
                <span class="row-val" id="lastRun">—</span>
                <svg class="chevron-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18l6-6-6-6"/></svg>
              </div>
            </div>
          </div>
        </div>
      </section>

      <!-- Tab 2: 钉钉投递 -->
      <section class="tab-pane" id="pane-dingtalk">
        <div class="section-wrap">
          <div class="section-head"><span>目标与附言</span></div>
          <div class="ios-group">
            <div class="ios-row">
              <div class="sf-badge sf-teal">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="4"/><line x1="12" y1="2" x2="12" y2="5"/></svg>
              </div>
              <div class="row-title" style="flex-shrink:0;">目标群聊</div>
              <div class="field-inline">
                <input type="text" id="dtSearch" placeholder="输入完整群名或联系人" />
                <button class="clear-circle" id="clearSearchBtn" title="清空">
                  <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><line x1="4" y1="4" x2="12" y2="12"/><line x1="12" y1="4" x2="4" y2="12"/></svg>
                </button>
              </div>
            </div>

            <div class="ios-row">
              <div class="sf-badge sf-indigo">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20.59 13.41l-7.17 7.17a2 2 0 01-2.83 0L2 12V2h10l8.59 8.59a2 2 0 010 2.82z"/><line x1="7" y1="7" x2="7.01" y2="7"/></svg>
              </div>
              <div class="row-title" style="flex-shrink:0;">时间后缀</div>
              <div class="field-inline">
                <input type="text" id="dtSuffix" placeholder="例如：暗号数据" />
              </div>
            </div>
          </div>
          <div class="section-footer" id="kwPreview">打开钉钉后按 Ctrl+Shift+F 粘贴目标名称并按 Enter 直达</div>
          <div class="section-footer" id="msgPreview" style="padding-top:3px;">消息附言预览：—</div>
        </div>

        <div class="section-wrap">
          <div class="section-head"><span>投递行为控制</span></div>
          <div class="ios-group">
            <div class="ios-row">
              <div class="sf-badge sf-green">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/></svg>
              </div>
              <div class="row-main">
                <div class="row-title">复制汇总表图片</div>
                <div class="row-sub">选中 C6:AJ 并高清复制到剪贴板</div>
              </div>
              <label class="ios-switch"><input type="checkbox" id="enableCapture" /><span class="ios-slider"></span></label>
            </div>

            <div class="ios-row">
              <div class="sf-badge sf-blue">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/></svg>
              </div>
              <div class="row-main">
                <div class="row-title">自动发送到钉钉</div>
                <div class="row-sub">自动唤醒钉钉并进入目标会话框</div>
              </div>
              <label class="ios-switch"><input type="checkbox" id="enableDingtalk" /><span class="ios-slider"></span></label>
            </div>

            <div class="ios-row">
              <div class="sf-badge sf-purple">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/></svg>
              </div>
              <div class="row-main">
                <div class="row-title">发送前清空输入框</div>
                <div class="row-sub">粘贴前先清空草稿，防止错发旧内容</div>
              </div>
              <label class="ios-switch"><input type="checkbox" id="dtClearBeforeSend" /><span class="ios-slider"></span></label>
            </div>

            <div class="ios-row">
              <div class="sf-badge sf-orange">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M13.73 21a2 2 0 01-3.46 0"/><path d="M18.63 13A17.89 17.89 0 0118 8"/><path d="M6.26 6.26A5.86 5.86 0 006 8c0 7-3 9-3 9h14"/><line x1="1" y1="1" x2="23" y2="23"/></svg>
              </div>
              <div class="row-main">
                <div class="row-title">设为静音发送</div>
                <div class="row-sub">自动展开发送按钮右侧菜单并选中</div>
              </div>
              <label class="ios-switch"><input type="checkbox" id="dtSilentSend" /><span class="ios-slider"></span></label>
            </div>

            <div class="ios-row">
              <div class="sf-badge sf-pink">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
              </div>
              <div class="row-main">
                <div class="row-title">粘贴后自动按 Enter 发送</div>
                <div class="row-sub">关闭时贴好图文与静音后等待确认</div>
              </div>
              <label class="ios-switch"><input type="checkbox" id="dtAutoSend" /><span class="ios-slider"></span></label>
            </div>
          </div>
        </div>
      </section>

      <!-- Tab 3: 定时计划 -->
      <section class="tab-pane" id="pane-schedule">
        <div class="section-wrap">
          <div class="section-head"><span>自动值守时段</span><span>点击时间胶囊调整</span></div>
          <div class="ios-group">
            <div class="ios-row">
              <div class="sf-badge sf-blue">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="3"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>
              </div>
              <div class="row-main">
                <div class="row-title">运行日期</div>
              </div>
              <div class="segmented" id="dayModeSeg">
                <div class="seg-thumb" id="dayModeThumb"></div>
                <button class="seg-btn" data-val="daily">每天运行</button>
                <button class="seg-btn" data-val="workday">仅工作日</button>
              </div>
            </div>

            <div class="slot-block">
              <div class="slot-head">
                <div class="slot-title-wrap">
                  <div class="sf-badge sf-orange">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><line x1="12" y1="2" x2="12" y2="4"/><line x1="12" y1="20" x2="12" y2="22"/><line x1="4.93" y1="4.93" x2="6.34" y2="6.34"/><line x1="17.66" y1="17.66" x2="19.07" y2="19.07"/><line x1="2" y1="12" x2="4" y2="12"/><line x1="20" y1="12" x2="22" y2="12"/><line x1="4.93" y1="19.07" x2="6.34" y2="17.66"/><line x1="17.66" y1="6.34" x2="19.07" y2="4.93"/></svg>
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
                <label class="ios-switch"><input type="checkbox" id="nightEnabled" /><span class="ios-slider"></span></label>
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
          </div>
          <div class="section-footer warn" id="overlapTip" style="display:none;">
            白天与晚上时段存在重叠，系统会按统一节拍运行，交界处不会重复连跑。
          </div>
        </div>

        <div class="section-wrap">
          <div class="section-head"><span>发群时段限制</span></div>
          <div class="ios-group">
            <div class="ios-row">
              <div class="sf-badge sf-red">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>
              </div>
              <div class="row-main">
                <div class="row-title">仅允许时段截图发群</div>
                <div class="row-sub">关闭时全天自动均发群，手动触发不受限</div>
              </div>
              <label class="ios-switch"><input type="checkbox" id="cpEnabled" /><span class="ios-slider"></span></label>
            </div>

            <div class="accordion-body" id="cpAccordion">
              <div class="ios-row">
                <div class="row-main"><div class="row-title">适用日期</div></div>
                <div class="segmented" id="cpProfileSeg">
                  <div class="seg-thumb" id="cpProfileThumb"></div>
                  <button class="seg-btn" data-val="workday">工作日</button>
                  <button class="seg-btn" data-val="weekend">非工作日</button>
                </div>
              </div>

              <div class="slot-block">
                <div class="slot-head">
                  <span class="row-title">允许时段 1</span>
                  <label class="ios-switch"><input type="checkbox" id="cpSlot1" /><span class="ios-slider"></span></label>
                </div>
                <div class="slot-controls" id="cpSlot1Body" style="padding-left:0;">
                  <div class="pill-pair">
                    <button class="ios-pill-btn" id="cpStart1Btn">09:00</button>
                    <span class="pill-sep">至</span>
                    <button class="ios-pill-btn" id="cpEnd1Btn">18:00</button>
                  </div>
                </div>
              </div>

              <div class="slot-block">
                <div class="slot-head">
                  <span class="row-title">允许时段 2</span>
                  <label class="ios-switch"><input type="checkbox" id="cpSlot2" /><span class="ios-slider"></span></label>
                </div>
                <div class="slot-controls" id="cpSlot2Body" style="padding-left:0;">
                  <div class="pill-pair">
                    <button class="ios-pill-btn" id="cpStart2Btn">18:00</button>
                    <span class="pill-sep">至</span>
                    <button class="ios-pill-btn" id="cpEnd2Btn">22:00</button>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>

      <!-- Tab 4: 记录与账号 -->
      <section class="tab-pane" id="pane-more">
        <div class="section-wrap">
          <div class="section-head">
            <span>运行日志</span>
            <div class="log-tools">
              <button class="mini-pill active" id="autoScrollBtn">自动滚底</button>
              <button class="mini-pill" id="copyLogBtn">复制</button>
              <button class="mini-pill" id="clearLogBtn">清空</button>
            </div>
          </div>
          <div class="ios-group">
            <div class="log-box" id="logBox">
              <div class="log-empty" id="logEmpty">暂无运行日志</div>
            </div>
          </div>
        </div>

        <div class="section-wrap">
          <div class="section-head"><span>数据源与后台账号</span></div>
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

            <div class="ios-row">
              <div class="sf-badge sf-teal">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 00-4-4H8a4 4 0 00-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
              </div>
              <div class="row-title" style="flex-shrink:0;">Luya 账号</div>
              <div class="field-inline">
                <input type="text" id="luyaUser" placeholder="后台登录账号" />
              </div>
            </div>

            <div class="ios-row">
              <div class="sf-badge sf-gray">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0110 0v4"/></svg>
              </div>
              <div class="row-title" style="flex-shrink:0;">Luya 密码</div>
              <div class="field-inline">
                <input type="password" id="luyaPass" placeholder="后台登录密码" />
                <button class="mini-pill" id="togglePassBtn" type="button" style="display:inline-flex;align-items:center;padding:4px 8px;" title="显示或隐藏密码"></button>
              </div>
            </div>
          </div>
        </div>

        <button class="danger-card-btn" id="resetBtn">恢复默认设置</button>
      </section>
    </main>
  </div>

  <!-- 底部固定毛玻璃 4 栏 Tab Bar -->
  <nav class="tabbar-wrap">
    <div class="tabbar" id="tabbar">
      <button class="tab-btn active" data-tab="home">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
        <span>控制台</span>
      </button>
      <button class="tab-btn" data-tab="dingtalk">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M22 2L11 13"/><path d="M22 2l-7 20-4-9-9-4 20-7z"/></svg>
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

  <!-- iOS 底部毛玻璃滚轮选择器 -->
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

  <!-- iOS 毛玻璃模态弹窗 -->
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

<script>
const TOKEN = __TOKEN_JSON__;
const $ = (id) => document.getElementById(id);

const SVG_MOON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1111.21 3 7 7 0 0021 12.79z"/></svg>`;
const SVG_SUN = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><line x1="12" y1="2" x2="12" y2="4"/><line x1="12" y1="20" x2="12" y2="22"/><line x1="4.93" y1="4.93" x2="6.34" y2="6.34"/><line x1="17.66" y1="17.66" x2="19.07" y2="19.07"/><line x1="2" y1="12" x2="4" y2="12"/><line x1="20" y1="12" x2="22" y2="12"/><line x1="4.93" y1="19.07" x2="6.34" y2="17.66"/><line x1="17.66" y1="6.34" x2="19.07" y2="4.93"/></svg>`;
const SVG_EYE = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:14px;height:14px;"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>`;
const SVG_EYE_OFF = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:14px;height:14px;"><path d="M17.94 17.94A10.07 10.07 0 0112 20c-7 0-11-8-11-8a18.45 18.45 0 015.06-5.94"/><path d="M9.9 4.24A9.12 9.12 0 0112 4c7 0 11 8 11 8a18.5 18.5 0 01-2.16 3.19"/><line x1="1" y1="1" x2="23" y2="23"/></svg>`;

let cfg = {};
let cpProfile = "workday";
let autoScroll = true;
let lastLogSeq = 0;
let saveTimer = null;
let updateUrl = "";
let pickerState = null;

function cleanText(s) {
  return String(s || "").replace(/[\u2600-\u27BF\uFE0F\u200D]|[\uD83C-\uDBFF][\uDC00-\uDFFF]/g, "").trim();
}

function pad2(n) {
  return String(Math.max(0, Number(n) || 0)).padStart(2, "0");
}

function hmStr(h, m) {
  return `${pad2(h)}:${pad2(m)}`;
}

function formatGapText(ih, im) {
  const h = Number(ih) || 0;
  const m = Number(im) || 0;
  if (h > 0 && m > 0) return `每 ${h} 时 ${m} 分`;
  if (h > 0) return `每 ${h} 小时`;
  return `每 ${Math.max(1, m)} 分钟`;
}

function slotMinutes(s) {
  const parts = String(s || "0:0").split(":");
  return (parseInt(parts[0], 10) || 0) * 60 + (parseInt(parts[1], 10) || 0);
}

function slotsOverlap(s1, e1, s2, e2) {
  const toSegments = (a, b) => a <= b ? [[a, b]] : [[a, 1440], [0, b]];
  for (const [a1, a2] of toSegments(slotMinutes(s1), slotMinutes(e1))) {
    for (const [b1, b2] of toSegments(slotMinutes(s2), slotMinutes(e2))) {
      if (Math.max(a1, b1) < Math.min(a2, b2)) return true;
    }
  }
  return false;
}

function showToast(msg) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove("show"), 1800);
}

async function api(path, method = "GET", body = null) {
  const opts = {
    method,
    headers: { "X-Token": TOKEN, "Content-Type": "application/json" }
  };
  if (body !== null) opts.body = JSON.stringify(body);
  const res = await fetch(path, opts);
  return await res.json();
}

function switchTab(name) {
  for (const btn of $("tabbar").querySelectorAll(".tab-btn")) {
    btn.classList.toggle("active", btn.dataset.tab === name);
  }
  for (const pane of document.querySelectorAll(".tab-pane")) {
    pane.classList.toggle("active", pane.id === `pane-${name}`);
  }
  window.scrollTo({ top: 0, behavior: "instant" });
}

function iosAlert(title, msg, { okText = "好", cancelText = null, danger = false } = {}) {
  return new Promise((resolve) => {
    $("alertTitle").textContent = title;
    $("alertMsg").textContent = msg;
    const actions = $("alertActions");
    const cancelBtn = $("alertCancel");
    const okBtn = $("alertOk");
    okBtn.textContent = okText;
    okBtn.className = "alert-btn " + (danger ? "danger" : "bold");
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
    const done = (v) => {
      mask.classList.remove("show");
      resolve(v);
    };
    okBtn.onclick = () => done(true);
    cancelBtn.onclick = () => done(false);
  });
}

function openPicker({ title, mode, h, m, onConfirm }) {
  pickerState = {
    mode,
    h: Number(h) || 0,
    m: Number(m) || 0,
    onConfirm,
  };
  $("pickerTitle").textContent = title;
  $("wheelLeftLabel").textContent = mode === "gap" ? "间隔小时" : "时";
  $("wheelRightLabel").textContent = mode === "gap" ? "间隔分钟" : "分";

  const presets = mode === "gap"
    ? [
        { label: "15分钟", h: 0, m: 15 },
        { label: "30分钟", h: 0, m: 30 },
        { label: "45分钟", h: 0, m: 45 },
        { label: "1小时", h: 1, m: 0 },
        { label: "2小时", h: 2, m: 0 },
      ]
    : [
        { label: "08:00", h: 8, m: 0 },
        { label: "09:00", h: 9, m: 0 },
        { label: "12:00", h: 12, m: 0 },
        { label: "18:00", h: 18, m: 0 },
        { label: "22:00", h: 22, m: 0 },
        { label: "23:59", h: 23, m: 59 },
      ];

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
    b.onclick = () => {
      pickerState.h = i;
      syncPickerSelection();
    };
    leftEl.appendChild(b);
  }
  for (let i = 0; i < 60; i++) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "wheel-opt";
    b.dataset.v = String(i);
    b.textContent = mode === "gap" ? `${i} 分` : pad2(i);
    b.onclick = () => {
      pickerState.m = i;
      syncPickerSelection();
    };
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
    c.onclick = () => {
      pickerState.h = p.h;
      pickerState.m = p.m;
      syncPickerSelection();
    };
    presetWrap.appendChild(c);
  }

  $("pickerMask").classList.add("show");
  syncPickerSelection(true);
}

function syncPickerSelection(scroll = false) {
  if (!pickerState) return;
  const { mode, h, m } = pickerState;
  $("pickerReadout").textContent = mode === "gap"
    ? formatGapText(h, m)
    : hmStr(h, m);

  for (const chip of $("pickerPresets").querySelectorAll(".preset-chip")) {
    const match = Number(chip.dataset.h) === h && Number(chip.dataset.m) === m;
    chip.classList.toggle("active", match);
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

function updateSummaries() {
  const targetName = ($("dtSearch").value || "").trim() || "文件小助手";
  $("sumTarget").textContent = targetName;
  $("kwPreview").textContent = `将在钉钉按 Ctrl+Shift+F 直接粘贴「${targetName}」并按 Enter 进入会话`;

  const suffix = ($("dtSuffix").value || "").trim() || "数据";
  const now = new Date();
  const stamp = `${now.getFullYear()}-${pad2(now.getMonth() + 1)}-${pad2(now.getDate())} ${pad2(now.getHours())}:${pad2(now.getMinutes())}`;
  $("msgPreview").textContent = `消息附言预览：${stamp} ${suffix}`;

  const policyParts = [];
  policyParts.push($("dtClearBeforeSend").checked ? "先清空" : "保留草稿");
  policyParts.push($("dtSilentSend").checked ? "静音" : "常规");
  policyParts.push($("dtAutoSend").checked ? "自动发送" : "暂不发送");
  $("sumPolicy").textContent = policyParts.join(" · ");

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
  $("sumScheduleSub").textContent = `${cfg.day_mode === "workday" ? "仅工作日" : "每天运行"} · 白天 ${ds}-${de}${nOn ? " / 夜间" : ""}`;
}

function renderCaptureProfile() {
  if (!cfg.capture_plan) cfg.capture_plan = { enabled: false, workday: {}, weekend: {} };
  if (!cfg.capture_plan[cpProfile]) {
    cfg.capture_plan[cpProfile] = {
      slot1: true, start1: "09:00", end1: "18:00",
      slot2: false, start2: "18:00", end2: "22:00",
    };
  }
  const cp = cfg.capture_plan[cpProfile];
  $("cpSlot1").checked = Boolean(cp.slot1);
  $("cpStart1Btn").textContent = cp.start1 || "09:00";
  $("cpEnd1Btn").textContent = cp.end1 || "18:00";
  $("cpSlot1Body").classList.toggle("disabled", !$("cpSlot1").checked);

  $("cpSlot2").checked = Boolean(cp.slot2);
  $("cpStart2Btn").textContent = cp.start2 || "18:00";
  $("cpEnd2Btn").textContent = cp.end2 || "22:00";
  $("cpSlot2Body").classList.toggle("disabled", !$("cpSlot2").checked);

  applySegmented("cpProfileSeg", "cpProfileThumb", cpProfile);
}

function renderConfig() {
  if (!cfg.day) cfg.day = { sh: 9, sm: 0, eh: 18, em: 0, ih: 0, im: 30 };
  if (!cfg.night) cfg.night = { sh: 18, sm: 0, eh: 23, em: 0, ih: 0, im: 30 };

  applySegmented("dayModeSeg", "dayModeThumb", cfg.day_mode || "daily");
  $("nightEnabled").checked = cfg.night_enabled !== false;

  $("enableCapture").checked = cfg.enable_capture !== false;
  $("enableDingtalk").checked = Boolean(cfg.enable_dingtalk);
  $("dtClearBeforeSend").checked = cfg.dt_clear_before_send !== false;
  $("dtSilentSend").checked = cfg.dt_silent_send !== false;
  $("dtAutoSend").checked = cfg.dt_auto_send === true;
  $("dtSearch").value = cfg.dt_search || "";
  $("dtSuffix").value = cfg.dt_suffix || "";

  const cpOn = Boolean(cfg.capture_plan && cfg.capture_plan.enabled);
  $("cpEnabled").checked = cpOn;
  $("cpAccordion").classList.toggle("open", cpOn);
  renderCaptureProfile();

  $("wpsUrl").value = cfg.wps_url || "";
  $("luyaUser").value = cfg.luya_user || "";
  $("luyaPass").value = cfg.luya_pass || "";

  updateSummaries();
}

function collectConfigFromDOM() {
  cfg.night_enabled = $("nightEnabled").checked;
  cfg.enable_capture = $("enableCapture").checked;
  cfg.enable_dingtalk = $("enableDingtalk").checked;
  cfg.dt_clear_before_send = $("dtClearBeforeSend").checked;
  cfg.dt_silent_send = $("dtSilentSend").checked;
  cfg.dt_auto_send = $("dtAutoSend").checked;
  cfg.dt_search = $("dtSearch").value.trim() || "文件小助手";
  cfg.dt_suffix = $("dtSuffix").value.trim() || "数据";
  cfg.wps_url = $("wpsUrl").value.trim();
  cfg.luya_user = $("luyaUser").value.trim();
  cfg.luya_pass = $("luyaPass").value;

  if (!cfg.capture_plan) cfg.capture_plan = { enabled: false, workday: {}, weekend: {} };
  cfg.capture_plan.enabled = $("cpEnabled").checked;
  if (!cfg.capture_plan[cpProfile]) cfg.capture_plan[cpProfile] = {};
  cfg.capture_plan[cpProfile].slot1 = $("cpSlot1").checked;
  cfg.capture_plan[cpProfile].slot2 = $("cpSlot2").checked;
  return cfg;
}

function scheduleSave(immediate = false) {
  updateSummaries();
  clearTimeout(saveTimer);
  const runSave = async () => {
    const data = collectConfigFromDOM();
    await api("/api/config", "POST", data);
    if (immediate) showToast("设置已保存");
  };
  if (immediate) {
    runSave();
  } else {
    saveTimer = setTimeout(runSave, 350);
  }
}

function formatCountdown(nextTs, serverNow) {
  if (!nextTs || nextTs <= 0) return "";
  const diff = Math.max(0, Math.round(nextTs - serverNow));
  if (diff <= 0) return "即将执行";
  const m = Math.floor(diff / 60);
  const s = diff % 60;
  return m > 0 ? `${m}分${s}秒后` : `${s}秒后`;
}

function appendLogLines(items) {
  if (!items || !items.length) return;
  const empty = $("logEmpty");
  if (empty) empty.style.display = "none";
  const box = $("logBox");
  const frag = document.createDocumentFragment();
  for (const item of items) {
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
  while (box.children.length > 500) {
    box.removeChild(box.firstChild);
  }
  if (autoScroll) {
    box.scrollTop = box.scrollHeight;
  }
}

async function pollState() {
  try {
    const st = await api(`/api/state?since=${lastLogSeq}`);
    $("verBadge").textContent = `v${st.version}`;

    const island = $("island");
    const busy = Boolean(st.busy);
    const hasAlert = Boolean(st.need_login_prompt || st.error_text);
    island.classList.toggle("busy", busy);
    island.classList.toggle("expanded", busy || hasAlert);

    const dot = $("islandDot");
    dot.className = "island-dot"
      + (st.status_level === "err" ? " err"
        : st.status_level === "stopped" ? " stopped"
        : st.schedule_running ? " sched" : "");

    $("stepText").textContent = cleanText(st.step_text) || "空闲中";
    const pct = Math.round((st.progress || 0) * 100);
    $("progressFill").style.width = `${pct}%`;
    $("progressPct").textContent = `${pct}%`;

    const cd = st.schedule_running ? formatCountdown(st.next_run_ts, st.server_now) : "";
    if (st.schedule_running && cd) {
      $("islandTag").textContent = cd;
    } else if (st.status_level === "err") {
      $("islandTag").textContent = "异常";
    } else if (st.status_level === "ok") {
      $("islandTag").textContent = "已完成";
    } else {
      $("islandTag").textContent = "待命";
    }

    const alertEl = $("islandAlert");
    if (st.need_login_prompt) {
      alertEl.className = "island-alert login";
      alertEl.textContent = "请在弹出的浏览器中扫码登录 WPS，完成后将自动继续";
    } else if (st.error_text) {
      alertEl.className = "island-alert err";
      alertEl.textContent = cleanText(st.error_text);
    } else {
      alertEl.className = "island-alert";
      alertEl.textContent = "";
    }

    $("lastRun").textContent = st.last_run_str || "—";
    $("sumNextRun").textContent = st.next_run_str && st.next_run_str !== "—" && cd
      ? `${st.next_run_str} (${cd})` : (st.next_run_str || "—");

    $("btnNow").disabled = busy;
    $("btnCapture").disabled = busy;

    const schedBtn = $("btnSchedule");
    schedBtn.classList.toggle("schedule-on", Boolean(st.schedule_running));
    $("schedTitle").textContent = st.schedule_running ? "值守运行中" : "自动值守";
    $("schedSub").textContent = st.schedule_running
      ? (cd ? `距下次 ${cd}` : "点击可停止定时计划")
      : "点击开启定时计划";

    if (st.log_seq < lastLogSeq) {
      $("logBox").innerHTML = `<div class="log-empty" id="logEmpty">暂无运行日志</div>`;
    }
    lastLogSeq = st.log_seq;
    appendLogLines(st.logs);

    if (st.update_info && st.update_info.url) {
      updateUrl = st.update_info.url;
      $("updateBtn").style.display = "flex";
      $("updateDot").style.display = "block";
      $("updateBtn").title = `发现新版 v${st.update_info.version}，点击下载`;
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
  for (const row of document.querySelectorAll(".ios-row[data-jump]")) {
    row.onclick = () => switchTab(row.dataset.jump);
  }

  for (const btn of $("dayModeSeg").querySelectorAll(".seg-btn")) {
    btn.onclick = () => {
      cfg.day_mode = btn.dataset.val;
      applySegmented("dayModeSeg", "dayModeThumb", cfg.day_mode);
      scheduleSave(true);
    };
  }
  for (const btn of $("cpProfileSeg").querySelectorAll(".seg-btn")) {
    btn.onclick = () => {
      collectConfigFromDOM();
      cpProfile = btn.dataset.val;
      renderCaptureProfile();
    };
  }

  for (const id of [
    "nightEnabled", "enableCapture", "enableDingtalk",
    "dtClearBeforeSend", "dtSilentSend", "dtAutoSend",
    "cpSlot1", "cpSlot2"
  ]) {
    $(id).addEventListener("change", () => {
      if (id === "cpSlot1") $("cpSlot1Body").classList.toggle("disabled", !$("cpSlot1").checked);
      if (id === "cpSlot2") $("cpSlot2Body").classList.toggle("disabled", !$("cpSlot2").checked);
      scheduleSave(true);
    });
  }

  $("cpEnabled").addEventListener("change", () => {
    $("cpAccordion").classList.toggle("open", $("cpEnabled").checked);
    scheduleSave(true);
  });

  for (const id of ["dtSearch", "dtSuffix", "wpsUrl", "luyaUser", "luyaPass"]) {
    $(id).addEventListener("input", () => scheduleSave(false));
  }
  $("clearSearchBtn").onclick = () => {
    $("dtSearch").value = "";
    scheduleSave(true);
  };

  // 时间 & 间隔底部滚轮选择器绑定
  $("dayStartBtn").onclick = () => openPicker({
    title: "白天开始时间", mode: "time", h: cfg.day.sh, m: cfg.day.sm,
    onConfirm: (h, m) => { cfg.day.sh = h; cfg.day.sm = m; scheduleSave(true); }
  });
  $("dayEndBtn").onclick = () => openPicker({
    title: "白天结束时间", mode: "time", h: cfg.day.eh, m: cfg.day.em,
    onConfirm: (h, m) => { cfg.day.eh = h; cfg.day.em = m; scheduleSave(true); }
  });
  $("dayGapBtn").onclick = () => openPicker({
    title: "白天执行间隔", mode: "gap", h: cfg.day.ih, m: cfg.day.im,
    onConfirm: (h, m) => { cfg.day.ih = h; cfg.day.im = m; scheduleSave(true); }
  });

  $("nightStartBtn").onclick = () => openPicker({
    title: "晚上开始时间", mode: "time", h: cfg.night.sh, m: cfg.night.sm,
    onConfirm: (h, m) => { cfg.night.sh = h; cfg.night.sm = m; scheduleSave(true); }
  });
  $("nightEndBtn").onclick = () => openPicker({
    title: "晚上结束时间", mode: "time", h: cfg.night.eh, m: cfg.night.em,
    onConfirm: (h, m) => { cfg.night.eh = h; cfg.night.em = m; scheduleSave(true); }
  });
  $("nightGapBtn").onclick = () => openPicker({
    title: "晚上执行间隔", mode: "gap", h: cfg.night.ih, m: cfg.night.im,
    onConfirm: (h, m) => { cfg.night.ih = h; cfg.night.im = m; scheduleSave(true); }
  });

  const bindCpTimeBtn = (btnId, key, title) => {
    $(btnId).onclick = () => {
      const val = (cfg.capture_plan && cfg.capture_plan[cpProfile] && cfg.capture_plan[cpProfile][key]) || "09:00";
      const [h, m] = val.split(":").map((x) => parseInt(x, 10) || 0);
      openPicker({
        title, mode: "time", h, m,
        onConfirm: (nh, nm) => {
          cfg.capture_plan[cpProfile][key] = hmStr(nh, nm);
          renderCaptureProfile();
          scheduleSave(true);
        }
      });
    };
  };
  bindCpTimeBtn("cpStart1Btn", "start1", "时段 1 开始时间");
  bindCpTimeBtn("cpEnd1Btn", "end1", "时段 1 结束时间");
  bindCpTimeBtn("cpStart2Btn", "start2", "时段 2 开始时间");
  bindCpTimeBtn("cpEnd2Btn", "end2", "时段 2 结束时间");

  $("pickerCancel").onclick = () => $("pickerMask").classList.remove("show");
  $("pickerMask").onclick = (e) => {
    if (e.target === $("pickerMask")) $("pickerMask").classList.remove("show");
  };
  $("pickerDone").onclick = () => {
    if (!pickerState) return;
    const { mode, h, m, onConfirm } = pickerState;
    if (mode === "gap" && h === 0 && m === 0) {
      showToast("执行间隔不能为 0 分钟");
      return;
    }
    $("pickerMask").classList.remove("show");
    onConfirm(h, m);
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
    showToast(r.message);
    pollState();
  };
  $("btnCapture").onclick = async () => {
    await api("/api/config", "POST", collectConfigFromDOM());
    const r = await api("/api/run-capture", "POST");
    showToast(r.message);
    pollState();
  };
  $("btnSchedule").onclick = async () => {
    await api("/api/config", "POST", collectConfigFromDOM());
    const r = await api("/api/schedule", "POST");
    if (!r.ok) {
      await iosAlert("无法开启计划", r.message);
      return;
    }
    showToast(r.message);
    pollState();
  };
  $("stopBtn").onclick = async () => {
    const r = await api("/api/stop", "POST");
    showToast(r.message);
    pollState();
  };

  $("autoScrollBtn").onclick = () => {
    autoScroll = !autoScroll;
    $("autoScrollBtn").classList.toggle("active", autoScroll);
  };
  $("copyLogBtn").onclick = async () => {
    try {
      await navigator.clipboard.writeText($("logBox").innerText || "");
      showToast("已复制运行日志");
    } catch (_e) {
      showToast("复制失败，请手动选中");
    }
  };
  $("clearLogBtn").onclick = async () => {
    await api("/api/clear-logs", "POST");
    $("logBox").innerHTML = `<div class="log-empty" id="logEmpty">暂无运行日志</div>`;
    lastLogSeq = 0;
    showToast("日志已清空");
  };

  $("resetBtn").onclick = async () => {
    const ok = await iosAlert(
      "恢复默认设置",
      "当前时段、执行间隔与钉钉群设置将重置为出厂默认值。",
      { okText: "恢复默认", cancelText: "取消", danger: true }
    );
    if (!ok) return;
    const res = await api("/api/reset", "POST");
    cfg = res.config;
    renderConfig();
    showToast("已恢复默认设置");
  };

  $("updateBtn").onclick = async () => {
    if (!updateUrl) return;
    const ok = await iosAlert("发现新版本", "检测到新版 SheetMate，是否前往 GitHub 下载？", {
      okText: "立即前往", cancelText: "稍后"
    });
    if (ok) window.open(updateUrl, "_blank");
  };

  window.addEventListener("pagehide", () => {
    try {
      navigator.sendBeacon(`/api/close?token=${encodeURIComponent(TOKEN)}`, "{}");
    } catch (_e) {}
  });
}

(async function bootstrap() {
  initTheme();
  bindEvents();
  const data = await api("/api/config");
  cfg = data.config;
  renderConfig();
  await pollState();
  setInterval(pollState, 900);
})();
</script>
</body>
</html>
"""


def _make_handler(ctrl, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def _check_auth(self, parsed):
            hdr = self.headers.get("X-Token") or ""
            qs = parse_qs(parsed.query)
            qtok = (qs.get("token") or [""])[0]
            return hdr == token or qtok == token

        def _send_json(self, payload, status=HTTPStatus.OK):
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except Exception:
                return {}

        def do_GET(self):
            parsed = urlparse(self.path)
            if not self._check_auth(parsed):
                self._send_json({"ok": False, "message": "Forbidden"}, HTTPStatus.FORBIDDEN)
                return

            if parsed.path == "/":
                body = HTML_TEMPLATE.replace("__TOKEN_JSON__", json.dumps(token)).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if parsed.path == "/api/state":
                qs = parse_qs(parsed.query)
                since = int((qs.get("since") or ["0"])[0] or 0)
                self._send_json(ctrl.get_state(since=since))
                return

            if parsed.path == "/api/config":
                self._send_json({"ok": True, "config": ctrl.get_config()})
                return

            self._send_json({"ok": False, "message": "Not Found"}, HTTPStatus.NOT_FOUND)

        def do_POST(self):
            parsed = urlparse(self.path)
            if not self._check_auth(parsed):
                self._send_json({"ok": False, "message": "Forbidden"}, HTTPStatus.FORBIDDEN)
                return

            if parsed.path == "/api/config":
                body = self._read_json()
                saved = ctrl.save_config(body)
                self._send_json({"ok": True, "config": saved})
                return

            if parsed.path == "/api/run-now":
                ok, msg = ctrl.start_run_now()
                self._send_json({"ok": ok, "message": msg})
                return

            if parsed.path == "/api/run-capture":
                ok, msg = ctrl.start_capture_only()
                self._send_json({"ok": ok, "message": msg})
                return

            if parsed.path == "/api/schedule":
                body = self._read_json()
                enable = body.get("enable") if isinstance(body, dict) and "enable" in body else None
                ok, msg, running = ctrl.toggle_schedule(enable)
                self._send_json({"ok": ok, "message": msg, "schedule_running": running})
                return

            if parsed.path == "/api/stop":
                ok, msg = ctrl.stop_current_task()
                self._send_json({"ok": ok, "message": msg})
                return

            if parsed.path == "/api/reset":
                saved = ctrl.reset_config()
                self._send_json({"ok": True, "config": saved})
                return

            if parsed.path == "/api/clear-logs":
                ctrl.clear_logs()
                self._send_json({"ok": True})
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
    """启动 iOS 18 风格桌面控制中心 + 隐藏 Tk 驱动灵动岛浮窗。"""
    import tkinter as tk
    import neu_ui as neu

    tk_root = tk.Tk()
    tk_root.withdraw()
    glass = neu.GlassProgress(tk_root, title="暗号表助手")

    server, ctrl, url, _token = start_ui_server(app, glass, tk_root)

    win_w, win_h = 430, 840
    sw, sh = tk_root.winfo_screenwidth(), tk_root.winfo_screenheight()
    wx = max(20, (sw - win_w) // 2)
    wy = max(20, (sh - win_h) // 2)

    shell_exe, _kind = _find_app_shell_browser()
    proc = None
    launched_at = time.time()

    if shell_exe:
        shell_dir = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SheetMate" / "ui_shell"
        shell_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            shell_exe,
            f"--app={url}",
            f"--user-data-dir={shell_dir}",
            f"--window-size={win_w},{win_h}",
            f"--window-position={wx},{wy}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=msEdgeSidebarV2",
        ]
        try:
            proc = subprocess.Popen(cmd)
        except Exception:
            proc = None

    if proc is None:
        webbrowser.open(url)

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
