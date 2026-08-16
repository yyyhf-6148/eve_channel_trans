# -*- coding: utf-8 -*-
"""半透明悬浮窗界面（仅 Windows）。

结构：
- 控制面板（root）：显示角色/窗口/待翻译状态，提供“设置/退出”；
- 每个选中频道一个 ChannelOverlay：由“消息窗口 + 标题栏窗口”两个 Toplevel 组成，
  标题栏窗口始终可点击（含固定按钮），消息窗口在固定后整窗鼠标穿透（WS_EX_TRANSPARENT）。
  固定后标题栏上的“固定 / ×”按钮隐藏，仅保留独立的“取消固定”小按钮窗口可点击。
"""
import ctypes
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import eve_logs
from config import get, set as cfg_set, save_config

# 版本 / 作者信息（显示在设置窗口底部）
APP_VERSION = "1.0.0"
APP_AUTHOR = "MaoSama"

# Windows 扩展样式
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
GWL_EXSTYLE = -20
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_FRAMECHANGED = 0x0020

_user32 = ctypes.windll.user32

# 配色
BG = "#101418"
BG_HEADER = "#16232e"
BG_STATUS = "#0b0f12"
FG = "#e6edf3"
FG_DIM = "#9aa5b1"
FG_SPK = "#58a6ff"
FG_STATUS = "#7d8590"
BTN_BG = "#24384a"
BTN_ACTIVE = "#2f4a63"


def _hwnd(win):
    """获取窗口的顶层 HWND（overrideredirect 的 Tk 窗口需通过 GetParent 取顶层）。"""
    hwnd = int(win.winfo_id())
    try:
        parent = int(_user32.GetParent(hwnd))
        if parent:
            return parent
    except Exception:
        pass
    return hwnd


def set_click_through(win, enabled):
    """设置/取消窗口的鼠标点击穿透。"""
    hwnd = _hwnd(win)
    style = int(_user32.GetWindowLongW(hwnd, GWL_EXSTYLE))
    if enabled:
        style |= WS_EX_TRANSPARENT | WS_EX_LAYERED
    else:
        style &= ~WS_EX_TRANSPARENT
    _user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
    _user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                         SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED)


class ChannelOverlay:
    """单个频道对应的翻译悬浮窗（消息窗口 + 标题栏窗口）。"""

    BAR_H = 30

    def __init__(self, app, channel, character):
        self.app = app
        self.channel = channel
        self.pinned = False
        self._drag = None
        self._resize = None
        self._pos = None  # 消息窗口屏幕坐标（权威值，避免依赖未映射窗口的实时查询）

        w = int(get("window.width", 440))
        h = int(get("window.height", 340))
        fs = int(get("window.font_size", 10))

        # ---------- 消息窗口 ----------
        self.win = tk.Toplevel(app.root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=BG)
        # 消息窗口从标题栏下方开始（不重叠），避免标题栏被消息窗盖住而无法拖动/点按钮
        self.win.geometry(f"{w}x{max(h - self.BAR_H, 120)}")

        self.text = tk.Text(self.win, bg=BG, fg=FG, wrap="word", relief="flat", bd=0,
                            font=("Microsoft YaHei UI", fs), padx=8, pady=6,
                            highlightthickness=0)
        # 不显示右侧滚动条轨道，直接用鼠标滚轮滚动
        self.text.pack(fill="both", expand=True)
        self.text.tag_configure("orig", foreground=FG_DIM)
        self.text.tag_configure("spk", foreground=FG_SPK)
        self.text.tag_configure("trans", foreground=FG)
        self.text.configure(state="disabled")
        self.text.bind("<Button-3>", self._context_menu)

        # ---------- 标题栏窗口（独立，固定后仍可点击） ----------
        self.bar = tk.Toplevel(app.root)
        self.bar.withdraw()
        self.bar.overrideredirect(True)
        self.bar.attributes("-topmost", True)
        self.bar.configure(bg=BG_HEADER)

        btn_args = dict(bg=BTN_BG, fg=FG, relief="flat", bd=0, cursor="hand2",
                        activebackground=BTN_ACTIVE, activeforeground="#ffffff",
                        font=("Microsoft YaHei UI", 9), padx=8, pady=2)
        self.title_lbl = tk.Label(self.bar, text=f"{channel} · 翻译", bg=BG_HEADER,
                                  fg=FG, font=("Microsoft YaHei UI", fs + 1, "bold"),
                                  anchor="w")
        self.title_lbl.pack(side="left", padx=8, pady=5, fill="x", expand=True)
        self.btn_close = tk.Button(self.bar, text="×",
                                   command=lambda: app.close_overlay(channel),
                                   **btn_args)
        self.btn_close.pack(side="right", padx=(2, 6), pady=4)
        self.btn_pin = tk.Button(self.bar, text="固定", command=self.toggle_pin,
                                 **btn_args)
        self.btn_pin.pack(side="right", padx=2, pady=4)

        self.bar.bind("<ButtonPress-1>", self._start_drag)
        self.bar.bind("<B1-Motion>", self._do_drag)
        self.bar.bind("<Button-3>", self._context_menu)
        self.title_lbl.bind("<ButtonPress-1>", self._start_drag)
        self.title_lbl.bind("<B1-Motion>", self._do_drag)
        self.title_lbl.bind("<Button-3>", self._context_menu)

        # ---------- 固定后唯一可点击的“取消固定”按钮（独立窗口） ----------
        # 固定后 win 与 bar 整体点击穿透，只有这个小窗口能点到
        self.pin_btn_win = tk.Toplevel(app.root)
        self.pin_btn_win.withdraw()
        self.pin_btn_win.overrideredirect(True)
        self.pin_btn_win.attributes("-topmost", True)
        self.pin_btn_win.configure(bg=BG_HEADER)
        self.pin_btn_unpin = tk.Button(self.pin_btn_win, text="取消固定",
                                       command=self.toggle_pin, **btn_args)
        self.pin_btn_unpin.pack(fill="both", expand=True)

        # 未固定时支持边缘拖动改变大小（鼠标到窗口边缘显示缩放光标）
        for wdg in (self.win, self.text):
            wdg.bind("<Motion>", self._on_motion)
            wdg.bind("<ButtonPress-1>", self._on_resize_start)
            wdg.bind("<B1-Motion>", self._on_resize_drag)
            wdg.bind("<ButtonRelease-1>", self._on_resize_end)

        self.win.bind("<Configure>", self._on_configure)

        self._load_position()
        self._apply_style()
        self.win.deiconify()
        self.bar.deiconify()
        self._sync_bar()  # 窗口显示后再同步标题栏位置，避免 withdraw 时坐标无效
        # 窗口真正映射到屏幕后再兜底同步一次，防止 winfo_rootx 尚未生效
        self.app.root.after(120, self._sync_bar)

    # ---------- 固定 / 穿透 ----------
    def toggle_pin(self, event=None):
        self.set_pinned(not self.pinned)

    def set_pinned(self, pinned):
        self.pinned = pinned
        set_click_through(self.win, pinned)
        # 固定后标题栏也整体穿透，除独立“取消固定”按钮外一概点不到
        set_click_through(self.bar, pinned)
        if pinned:
            # 固定后隐藏标题栏上的“固定 / ×”按钮，只保留独立“取消固定”按钮
            self.btn_pin.pack_forget()
            self.btn_close.pack_forget()
            self._sync_pin_btn()
            for wdg in (self.win, self.text):
                try:
                    wdg.configure(cursor="arrow")
                except tk.TclError:
                    pass
        else:
            try:
                self.pin_btn_win.withdraw()
                # 恢复标题栏上的“固定 / ×”按钮（保持原有左右顺序）
                self.btn_close.pack(side="right", padx=(2, 6), pady=4)
                self.btn_pin.pack(side="right", padx=2, pady=4)
                self.bar.lift()  # 确保标题栏仍在消息窗之上
                self._sync_bar()  # lift 会重置 bar 坐标，需重新同步
            except tk.TclError:
                pass

    # ---------- 位置 / 拖动 ----------
    def _current_win_pos(self):
        """返回消息窗口的屏幕坐标，优先使用显式维护的 _pos。"""
        if self._pos is not None:
            return self._pos
        try:
            self.win.update_idletasks()
            x = self.win.winfo_rootx()
            y = self.win.winfo_rooty()
            if x != 0 or y != 0:  # 窗口尚未真正映射时 winfo_rootx 可能是 (0,0)
                self._pos = (x, y)
        except tk.TclError:
            pass
        return self._pos if self._pos is not None else (0, 0)

    def _sync_bar(self):
        try:
            x, y = self._current_win_pos()
            self.win.update_idletasks()
            w = self.win.winfo_width()
            if w <= 1:
                w = int(get("window.width", 440))
            # 注意：overrideredirect 窗口在 Windows 上 lift() 会把坐标重置，
            # 因此必须先置顶、后设置 geometry
            self.bar.lift()
            self.bar.geometry(f"{w}x{self.BAR_H}+{x}+{y - self.BAR_H}")
            self._sync_pin_btn()
        except tk.TclError:
            pass

    def _sync_pin_btn(self):
        """固定后：独立“取消固定”按钮窗口跟随标题栏右侧。"""
        if not self.pinned:
            return
        try:
            bx = self.bar.winfo_rootx()
            by = self.bar.winfo_rooty()
            bw = self.bar.winfo_width()
            if bw <= 1:
                bw = int(get("window.width", 440))
            w = max(self.pin_btn_unpin.winfo_reqwidth(), 88)
            self.pin_btn_win.deiconify()
            self.pin_btn_win.lift()
            # lift 会重置坐标，置顶后再定位
            self.pin_btn_win.geometry(f"{w}x{self.BAR_H - 8}+{bx + bw - w - 6}+{by + 4}")
        except tk.TclError:
            pass

    def _on_configure(self, event):
        if event.widget is self.win and self.bar.winfo_exists():
            try:
                x = self.win.winfo_rootx()
                y = self.win.winfo_rooty()
                if x != 0 or y != 0:
                    self._pos = (x, y)
            except tk.TclError:
                pass
            self._sync_bar()

    def _start_drag(self, event):
        if self.pinned:  # 固定后禁止拖动
            return
        if event.widget in (self.btn_pin, self.btn_close):
            return
        # 用屏幕坐标，避免控制面板不在 (0,0) 时拖动错位
        self._drag = (event.x_root - self.win.winfo_rootx(),
                      event.y_root - self.win.winfo_rooty())

    def _do_drag(self, event):
        if self._drag is None or self.pinned:
            return
        x = event.x_root - self._drag[0]
        y = event.y_root - self._drag[1]
        self._pos = (x, y)
        self.win.geometry(f"+{x}+{y}")
        self._sync_bar()

    # ---------- 边缘拖动改变大小（仅未固定时） ----------
    EDGE = 6
    MIN_W = 240
    MIN_H = 120
    _RESIZE_CURSORS = {
        "nw": "size_nw_se", "se": "size_nw_se",
        "ne": "size_ne_sw", "sw": "size_ne_sw",
        "w": "sb_h_double_arrow", "e": "sb_h_double_arrow",
        "n": "sb_v_double_arrow", "s": "sb_v_double_arrow",
    }

    def _resize_region(self, x, y):
        """根据窗口内坐标判断所在缩放区域（边/角），窗口内部返回 None。"""
        w = self.win.winfo_width()
        h = self.win.winfo_height()
        left = x <= self.EDGE
        right = x >= w - self.EDGE
        top = y <= self.EDGE
        bottom = y >= h - self.EDGE
        if left and top:
            return "nw"
        if right and top:
            return "ne"
        if left and bottom:
            return "sw"
        if right and bottom:
            return "se"
        if left:
            return "w"
        if right:
            return "e"
        if top:
            return "n"
        if bottom:
            return "s"
        return None

    def _on_motion(self, event):
        if self.pinned or self._resize:
            return
        region = self._resize_region(event.x, event.y)
        cursor = self._RESIZE_CURSORS.get(region, "arrow")
        self.win.configure(cursor=cursor)
        try:
            self.text.configure(cursor=cursor)
        except tk.TclError:
            pass

    def _on_resize_start(self, event):
        if self.pinned:
            return
        region = self._resize_region(event.x, event.y)
        if region is None:
            return
        self._resize = {
            "region": region,
            "x0": event.x_root,
            "y0": event.y_root,
            "gx": self._current_win_pos()[0],
            "gy": self._current_win_pos()[1],
            "gw": self.win.winfo_width(),
            "gh": self.win.winfo_height(),
        }

    def _on_resize_drag(self, event):
        r = self._resize
        if r is None:
            return
        dx = event.x_root - r["x0"]
        dy = event.y_root - r["y0"]
        nx, ny, nw, nh = r["gx"], r["gy"], r["gw"], r["gh"]
        reg = r["region"]
        if "e" in reg:
            nw = max(nw + dx, self.MIN_W)
        if "s" in reg:
            nh = max(nh + dy, self.MIN_H)
        if "w" in reg:
            nw = max(nw - dx, self.MIN_W)
            nx = r["gx"] + (r["gw"] - nw)
        if "n" in reg:
            nh = max(nh - dy, self.MIN_H)
            ny = r["gy"] + (r["gh"] - nh)
        self._pos = (nx, ny)
        self.win.geometry(f"{nw}x{nh}+{nx}+{ny}")
        self._sync_bar()

    def _on_resize_end(self, event):
        if self._resize is None:
            return
        self._resize = None
        self.win.update_idletasks()
        # 记住新尺寸（height 为含标题栏的总高），下次启动恢复
        cfg_set("window.width", max(self.win.winfo_width(), self.MIN_W))
        cfg_set("window.height", max(self.win.winfo_height() + self.BAR_H, self.MIN_H))
        self._sync_bar()

    def _load_position(self):
        pos = self.app.get_channel_pos(self.channel)
        if pos:
            self._pos = (int(pos[0]), int(pos[1]))
            self.win.geometry(f"+{self._pos[0]}+{self._pos[1]}")

    def _save_position(self):
        try:
            # 保存屏幕坐标，恢复时才不会因控制面板位置而偏移
            self.app.set_channel_pos(self.channel, list(self._current_win_pos()))
        except Exception:
            pass

    # ---------- 样式 ----------
    def _apply_style(self):
        alpha = float(get("window.opacity", 0.85))
        fs = int(get("window.font_size", 10))
        for w in (self.win, self.bar, self.pin_btn_win):
            try:
                w.attributes("-alpha", alpha)
            except tk.TclError:
                pass
        self.text.configure(font=("Microsoft YaHei UI", fs))
        self.title_lbl.configure(font=("Microsoft YaHei UI", fs + 1, "bold"))

    # ---------- 消息 / 菜单 ----------
    def add_message(self, item):
        speaker = item.get("speaker", "")
        original = item.get("original", "")
        translated = item.get("translated", "")
        self.text.configure(state="normal")
        if speaker:
            self.text.insert("end", f"[{speaker}] ", "spk")
        self.text.insert("end", f"{original}\n", "orig")
        self.text.insert("end", f"    {translated}\n\n", "trans")
        if float(self.text.index("end-1c")) > 1500:
            self.text.delete("1.0", "300.0")
        self.text.see("end")
        self.text.configure(state="disabled")

    def _context_menu(self, event=None):
        menu = tk.Menu(self.win, tearoff=0, bg=BG_HEADER, fg=FG,
                       activebackground=BTN_ACTIVE, activeforeground="#ffffff")
        menu.add_command(label="取消固定" if self.pinned else "固定",
                         command=self.toggle_pin)
        menu.add_command(label="设置…", command=self.app.open_settings)
        menu.add_separator()
        menu.add_command(label="关闭该窗口",
                         command=lambda: self.app.close_overlay(self.channel))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def destroy(self):
        self._save_position()
        for w in (self.win, self.bar, self.pin_btn_win):
            try:
                w.destroy()
            except tk.TclError:
                pass


class OverlayApp:
    def __init__(self):
        self.raw_q = queue.Queue()       # 待翻译消息
        self.done_q = queue.Queue()      # 已翻译结果
        self.stop_event = threading.Event()
        self.overlays = {}               # channel -> ChannelOverlay
        self.watchers = {}
        self.translator = None
        self._drag = None
        self._settings_open = False

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=BG)

        self.font = ("Microsoft YaHei UI", int(get("window.font_size", 10)))
        self._build_control_panel()
        self.root.deiconify()
        self._load_ctrl_position()
        self.rebuild_overlays()
        self._apply_style()

        self._start_workers()
        self.root.after(120, self._poll_done)

        # 首次运行且未填 API Key 时，自动打开设置
        if not get("api.api_key", "").strip():
            self.root.after(400, self.open_settings)

    # ---------- 控制面板 ----------
    def _build_control_panel(self):
        root = self.root
        root.geometry("300x118")
        header = tk.Frame(root, bg=BG_HEADER)
        header.pack(fill="x")
        title = tk.Label(header, text="翻译控制", bg=BG_HEADER, fg=FG,
                         font=("Microsoft YaHei UI", 10, "bold"), anchor="w")
        title.pack(side="left", padx=8, pady=5, fill="x", expand=True)

        btn_args = dict(bg=BTN_BG, fg=FG, relief="flat", bd=0, cursor="hand2",
                        activebackground=BTN_ACTIVE, activeforeground="#ffffff",
                        font=("Microsoft YaHei UI", 9), padx=8, pady=2)
        self.btn_quit = tk.Button(header, text="×", command=self.quit_app, **btn_args)
        self.btn_quit.pack(side="right", padx=(2, 6), pady=4)
        self.btn_set = tk.Button(header, text="设置", command=self.open_settings,
                                 **btn_args)
        self.btn_set.pack(side="right", padx=2, pady=4)

        for w in (header, title):
            w.bind("<ButtonPress-1>", self._start_ctrl_drag)
            w.bind("<B1-Motion>", self._do_ctrl_drag)
            w.bind("<Button-3>", self._ctrl_menu)

        body = tk.Frame(root, bg=BG)
        body.pack(fill="both", expand=True, padx=8, pady=6)
        self.status_lbl = tk.Label(body, text="就绪", bg=BG, fg=FG_DIM, anchor="w",
                                   justify="left", font=("Microsoft YaHei UI", 9))
        self.status_lbl.pack(fill="both", expand=True)
        for w in (body, self.status_lbl, root):
            w.bind("<Button-3>", self._ctrl_menu)

    def _ctrl_menu(self, event=None):
        menu = tk.Menu(self.root, tearoff=0, bg=BG_HEADER, fg=FG,
                       activebackground=BTN_ACTIVE, activeforeground="#ffffff")
        menu.add_command(label="设置…", command=self.open_settings)
        menu.add_separator()
        menu.add_command(label="退出", command=self.quit_app)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _start_ctrl_drag(self, event):
        if event.widget in (self.btn_set, self.btn_quit):
            return
        self._drag = (event.x_root - self.root.winfo_x(),
                      event.y_root - self.root.winfo_y())

    def _do_ctrl_drag(self, event):
        if self._drag is None:
            return
        self.root.geometry(f"+{event.x_root - self._drag[0]}+{event.y_root - self._drag[1]}")

    def _load_ctrl_position(self):
        pos = self.get_channel_pos("__ctrl__")
        if pos:
            self.root.geometry(f"+{int(pos[0])}+{int(pos[1])}")

    # ---------- 窗口位置存取 ----------
    def get_channel_pos(self, channel):
        positions = get("window.positions", {}) or {}
        return positions.get(channel)

    def set_channel_pos(self, channel, xy):
        positions = dict(get("window.positions", {}) or {})
        positions[channel] = xy
        cfg_set("window.positions", positions)

    # ---------- 悬浮窗管理 ----------
    def rebuild_overlays(self):
        channels = [c for c in get("eve.channels", []) if c and c.strip()]
        for ch in list(self.overlays):
            if ch not in channels:
                self.overlays[ch].destroy()
                del self.overlays[ch]
        base_x = self.root.winfo_rootx() + 40
        base_y = self.root.winfo_rooty() + 30
        idx = 0
        for ch in channels:
            if ch not in self.overlays:
                ov = ChannelOverlay(self, ch, get("eve.character", ""))
                self.overlays[ch] = ov
                if self.get_channel_pos(ch) is None:  # 级联偏移避免完全重叠
                    ov._pos = (base_x + 30 * idx, base_y + 30 * idx)
                    ov.win.geometry(f"+{ov._pos[0]}+{ov._pos[1]}")
                    ov._sync_bar()
                idx += 1
        self.watchers = {ch: None for ch in channels}
        self._update_ctrl_label()

    def close_overlay(self, channel):
        channels = [c for c in get("eve.channels", []) if c != channel]
        cfg_set("eve.channels", channels)
        save_config()
        self.rebuild_overlays()

    # ---------- 后台线程 ----------
    def _start_workers(self):
        self.stop_event.clear()
        threading.Thread(target=self._watch_loop, daemon=True).start()
        threading.Thread(target=self._translate_loop, daemon=True).start()

    def _restart_workers(self):
        self.stop_event.set()
        self.watchers = {ch: None for ch in self.overlays}
        self.raw_q = queue.Queue()
        self.done_q = queue.Queue()
        self._start_workers()

    def _watch_loop(self):
        while not self.stop_event.is_set():
            log_dir = get("eve.log_dir", "")
            character = get("eve.character", "")
            channels = list(self.overlays.keys())
            if log_dir and character and channels:
                for ch in channels:
                    w = self.watchers.get(ch)
                    if (w is None
                            or w.log_dir != log_dir or w.character != character
                            or w.channel != ch):
                        w = eve_logs.ChatLogWatcher(log_dir, character, ch)
                        self.watchers[ch] = w
                    try:
                        for msg in w.poll():
                            msg["channel"] = ch
                            self.raw_q.put(msg)
                    except Exception:
                        pass
            self.stop_event.wait(1.0)

    def _translate_loop(self):
        while not self.stop_event.is_set():
            try:
                msg = self.raw_q.get(timeout=1)
            except queue.Empty:
                continue
            speaker = msg.get("speaker", "")
            original = msg.get("text", "")
            channel = msg.get("channel", "")
            if not original.strip():
                continue
            if not get("api.base_url", "").strip() or not get("api.model", "").strip():
                self.done_q.put({"channel": channel, "speaker": speaker,
                                 "original": original,
                                 "translated": "[未配置 API，请点“设置”]"})
                continue
            if self.translator is None:
                from translator import Translator
                self.translator = Translator()
            result = None
            err = ""
            for attempt in range(3):
                try:
                    result = self.translator.translate(original)
                    break
                except Exception as e:
                    err = str(e)
                    time.sleep(1.5 * (attempt + 1))
            if result is None:
                result = f"[翻译失败] {err}"
            self.done_q.put({"channel": channel, "speaker": speaker,
                             "original": original, "translated": result})

    # ---------- UI 刷新 ----------
    def _poll_done(self):
        try:
            while True:
                item = self.done_q.get_nowait()
                ov = self.overlays.get(item.get("channel"))
                if ov is not None:
                    ov.add_message(item)
        except queue.Empty:
            pass
        self.root.after(120, self._poll_done)

    def _update_ctrl_label(self):
        char = get("eve.character", "")
        chans = ", ".join(self.overlays.keys()) or "无"
        pend = self.raw_q.qsize()
        self.status_lbl.configure(
            text=f"角色：{char or '未选择'}\n窗口：{chans}\n待翻译：{pend}")

    def _tick_status(self):
        self._update_ctrl_label()
        self.root.after(1000, self._tick_status)

    def _apply_style(self):
        self.root.attributes("-alpha", float(get("window.opacity", 0.85)))
        for ov in self.overlays.values():
            ov._apply_style()

    # ---------- 设置对话框 ----------
    def open_settings(self):
        if self._settings_open:
            return
        self._settings_open = True

        dlg = tk.Toplevel(self.root)
        dlg.title("设置")
        dlg.configure(bg=BG)
        dlg.attributes("-topmost", True)
        dlg.resizable(False, False)

        v_base = tk.StringVar(value=get("api.base_url", ""))
        v_key = tk.StringVar(value=get("api.api_key", ""))
        v_model = tk.StringVar(value=get("api.model", ""))
        v_temp = tk.StringVar(value=str(get("api.temperature", 0.3)))
        v_target = tk.StringVar(value=get("translation.target_language", "简体中文"))
        v_logdir = tk.StringVar(value=get("eve.log_dir", ""))
        v_char = tk.StringVar(value=get("eve.character", ""))
        v_opacity = tk.DoubleVar(value=float(get("window.opacity", 0.85)))
        v_font = tk.IntVar(value=int(get("window.font_size", 10)))

        def row(label):
            f = tk.Frame(dlg, bg=BG)
            f.pack(fill="x", padx=10, pady=4)
            tk.Label(f, text=label, bg=BG, fg="#c9d1d9", width=11, anchor="w",
                     font=("Microsoft YaHei UI", 9)).pack(side="left")
            return f

        def entry(r):
            e = tk.Entry(r, bg="#1c242c", fg=FG, insertbackground=FG, relief="flat")
            e.pack(side="left", fill="x", expand=True, padx=(0, 6))
            return e

        def sm_btn(r, text, cmd):
            tk.Button(r, text=text, command=cmd, bg=BTN_BG, fg=FG, relief="flat",
                      cursor="hand2", font=("Microsoft YaHei UI", 9)).pack(side="right")

        e_base = entry(row("API 地址"))
        e_base.configure(textvariable=v_base)

        r_key = row("API Key")
        e_key = entry(r_key)
        e_key.configure(textvariable=v_key, show="*")

        def toggle_show():
            e_key.configure(show="" if e_key.cget("show") == "*" else "*")
        sm_btn(r_key, "显示", toggle_show)

        e_model = entry(row("模型"))
        e_model.configure(textvariable=v_model)

        e_temp = entry(row("温度"))
        e_temp.configure(textvariable=v_temp, width=8)

        e_target = entry(row("目标语言"))
        e_target.configure(textvariable=v_target)

        r_log = row("日志目录")
        e_log = entry(r_log)
        e_log.configure(textvariable=v_logdir)

        def auto_detect():
            dirs = eve_logs.find_chatlog_dirs()
            v_logdir.set(dirs[0] if dirs else "")
            refresh_chars()
        sm_btn(r_log, "自动检测", auto_detect)

        r_char = row("角色")
        cb = ttk.Combobox(r_char, textvariable=v_char, state="readonly")
        cb.pack(side="left", fill="x", expand=True, padx=(0, 6))

        def refresh_chars():
            log_dir = v_logdir.get().strip()
            if not log_dir:
                dirs = eve_logs.find_chatlog_dirs()
                if dirs:
                    v_logdir.set(dirs[0])
                    log_dir = dirs[0]
            if not log_dir:
                cb["values"] = []
                return
            chars = eve_logs.list_characters(log_dir)
            cb["values"] = chars
            if chars and v_char.get() not in chars:
                # 默认选中“日志最新”的角色（通常是当前在线的角色）
                def last_time(c):
                    p = eve_logs.find_latest_local_file(log_dir, c)
                    return os.path.getmtime(p) if p else 0
                v_char.set(max(chars, key=last_time))
            refresh_channels()
        sm_btn(r_char, "刷新角色", refresh_chars)
        cb.bind("<<ComboboxSelected>>", lambda e: refresh_channels())

        # 翻译窗口（频道）选择 —— 方块打勾
        r_chans = row("翻译窗口")
        canvas = tk.Canvas(r_chans, bg="#1c242c", highlightthickness=0, height=130)
        canvas.pack(side="left", fill="both", expand=True, padx=(0, 6))
        sbc = tk.Scrollbar(r_chans, command=canvas.yview, bg=BG_HEADER)
        sbc.pack(side="right", fill="y")
        canvas.configure(yscrollcommand=sbc.set)
        box_frame = tk.Frame(canvas, bg="#1c242c")
        canvas.create_window((0, 0), window=box_frame, anchor="nw")
        box_frame.bind("<Configure>",
                       lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<MouseWheel>",
                    lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))

        _chk_vars = {}

        def rebuild_checks(chans, saved):
            for w in box_frame.winfo_children():
                w.destroy()
            _chk_vars.clear()
            for ch in chans:
                v = tk.BooleanVar(value=(ch in saved))
                _chk_vars[ch] = v
                tk.Checkbutton(box_frame, text=ch, variable=v, bg="#1c242c",
                               fg=FG, anchor="w", selectcolor="#1c242c",
                               activebackground="#1c242c",
                               activeforeground="#ffffff",
                               font=("Microsoft YaHei UI", 9),
                               highlightthickness=0, bd=0
                               ).pack(fill="x", padx=6, pady=1)

        def refresh_channels():
            log_dir = v_logdir.get().strip()
            char = v_char.get().strip()
            if not log_dir or not char or not os.path.isdir(log_dir):
                rebuild_checks([], [])
                return
            chans = eve_logs.list_channels(log_dir, char)
            saved = get("eve.channels", ["本地"]) or []
            rebuild_checks(chans, saved)
        sm_btn(r_chans, "刷新窗口", refresh_channels)

        hint = tk.Label(dlg, text="勾选需要翻译的窗口，每个勾选的窗口会生成一个翻译悬浮窗",
                        bg=BG, fg=FG_STATUS, anchor="w",
                        font=("Microsoft YaHei UI", 8))
        hint.pack(fill="x", padx=10, pady=(0, 4))

        r_op = row("窗口透明度")
        tk.Scale(r_op, from_=0.4, to=1.0, resolution=0.05, orient="horizontal",
                 variable=v_opacity, bg=BG, fg=FG, highlightthickness=0,
                 troughcolor="#1c242c", activebackground=BTN_ACTIVE
                 ).pack(side="left", fill="x", expand=True)

        r_font = row("字号")
        tk.Spinbox(r_font, from_=8, to=16, textvariable=v_font, width=6,
                   bg="#1c242c", fg=FG, buttonbackground=BTN_BG, relief="flat"
                   ).pack(side="left", anchor="w")

        def save():
            try:
                cfg_set("api.base_url", v_base.get().strip())
                cfg_set("api.api_key", v_key.get().strip())
                cfg_set("api.model", v_model.get().strip())
                cfg_set("api.temperature", float(v_temp.get() or 0.3))
                cfg_set("translation.target_language",
                        v_target.get().strip() or "简体中文")
                cfg_set("eve.log_dir", v_logdir.get().strip())
                cfg_set("eve.character", v_char.get().strip())
                cfg_set("eve.channels",
                        [ch for ch, v in _chk_vars.items() if v.get()])
                cfg_set("window.opacity", round(float(v_opacity.get()), 2))
                cfg_set("window.font_size", int(v_font.get()))
                save_config()
            except Exception as ex:
                messagebox.showerror("设置保存失败", str(ex), parent=dlg)
                return
            self._apply_style()
            self.rebuild_overlays()
            self._restart_workers()
            dlg.destroy()
            self._settings_open = False

        def cancel():
            dlg.destroy()
            self._settings_open = False

        btnf = tk.Frame(dlg, bg=BG)
        btnf.pack(fill="x", padx=10, pady=(8, 10))
        tk.Button(btnf, text="保存", command=save, bg=BTN_BG, fg=FG, relief="flat",
                  cursor="hand2", padx=16, pady=3, activebackground=BTN_ACTIVE,
                  activeforeground="#ffffff").pack(side="right")
        tk.Button(btnf, text="取消", command=cancel, bg="#2d333b", fg=FG, relief="flat",
                  cursor="hand2", padx=16, pady=3, activebackground="#444c56",
                  activeforeground="#ffffff").pack(side="right", padx=6)

        about = tk.Label(dlg, text=f"Version {APP_VERSION}  |  Made by {APP_AUTHOR}",
                         bg=BG, fg=FG_DIM, font=("Microsoft YaHei UI", 8))
        about.pack(fill="x", pady=(0, 8))

        dlg.protocol("WM_DELETE_WINDOW", cancel)
        dlg.after(100, refresh_chars)

    # ---------- 生命周期 ----------
    def quit_app(self):
        self.stop_event.set()
        try:
            self.set_channel_pos("__ctrl__",
                                 [self.root.winfo_x(), self.root.winfo_y()])
            for ov in self.overlays.values():
                ov._save_position()
            save_config()
        except Exception:
            pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()
