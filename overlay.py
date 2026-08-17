# -*- coding: utf-8 -*-
"""半透明悬浮窗界面（仅 Windows）。

结构：
- 控制面板（root）：显示角色/窗口/待翻译状态，提供“设置/退出”；
- 每个选中频道一个 ChannelOverlay：单窗口（顶部标题栏 + 消息区 + 底部发送输入框）。
  未固定时整窗可点，标题栏可拖动、四边/四角可缩放；
  固定后整窗鼠标穿透（WS_EX_TRANSPARENT），仅独立的“取消固定”小按钮窗口可点击。
  底部输入框输入中文后回车，翻译成英文并复制到剪贴板，请求会带上最近 10 条消息作为上文。

线程模型：
- _watch_loop（后台）扫描日志，把带递增序号 seq 的新消息放入 raw_q；
- 多个 _translate_loop worker（后台）并发翻译，结果带同一 seq 放入 done_q；
- _poll_done（主线程）按 seq 顺序渲染，保证并发翻译下显示顺序不乱；
- _send_worker_loop（后台）处理输入框的发送请求（中文→英文，带最近 10 条上文），
  结果经 send_done_q 回到主线程，由 _poll_done 复制到剪贴板并渲染。
- self.overlays / self.watchers 由 _lock 保护，避免后台线程与主线程竞态。
"""
import ctypes
import itertools
import os
import queue
import threading
import time
import tkinter as tk
from collections import deque
from tkinter import messagebox, ttk

import eve_logs
from config import DEFAULTS, get, log_error, set as cfg_set, save_config
from translator import (NetworkError, RateLimitError, ServerError,
                        TranslationError, Translator)

# 版本 / 作者信息（显示在设置窗口底部）
APP_VERSION = "1.3.0"
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

_WDEF = DEFAULTS["window"]

# 并发翻译 worker 数上限（默认最多同时 3 个请求，兼顾延迟与限流）
MAX_TRANSLATE_WORKERS = 3
# 可退避重试的瞬时错误
_TRANSIENT = (NetworkError, RateLimitError, ServerError)


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
    """单个频道对应的翻译悬浮窗（单窗口：顶部标题栏 + 消息区）。

    未固定：整窗可点，标题栏可拖动窗口，四边/四角可缩放；
    固定：整窗鼠标穿透（WS_EX_TRANSPARENT），仅独立“取消固定”小按钮可点击。
    """

    BAR_H = 30    # 标题栏设计高度（取消固定按钮定位用；标题栏本身由 pack 自适应）
    EDGE = 6      # 边缘缩放判定宽度
    MIN_W = 240
    MIN_H = 180   # 总高下限（标题栏 30 + 消息区 + 发送输入框）

    def __init__(self, app, channel, character):
        self.app = app
        self.channel = channel
        self.pinned = bool((get("window.pinned", {}) or {}).get(channel))
        self._drag = None
        self._resize = None
        self._pos = None  # 窗口屏幕坐标（权威值，避免依赖未映射窗口的实时查询）
        self.recent = deque(maxlen=10)  # 最近消息缓存，作为发送翻译的上文

        w = max(int(get("window.width", _WDEF["width"])), self.MIN_W)
        h = max(int(get("window.height", _WDEF["height"])), self.MIN_H)
        fs = int(get("window.font_size", _WDEF["font_size"]))

        # ---------- 主窗口（标题栏 + 消息区，未固定时整窗可点） ----------
        self.win = tk.Toplevel(app.root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=BG)
        self.win.geometry(f"{w}x{h}")

        self.header = tk.Frame(self.win, bg=BG_HEADER)
        self.header.pack(fill="x")

        btn_args = dict(bg=BTN_BG, fg=FG, relief="flat", bd=0, cursor="hand2",
                        activebackground=BTN_ACTIVE, activeforeground="#ffffff",
                        font=("Microsoft YaHei UI", 9), padx=8, pady=2)
        self.title_lbl = tk.Label(self.header, text=f"{channel} · 翻译", bg=BG_HEADER,
                                  fg=FG, font=("Microsoft YaHei UI", fs + 1, "bold"),
                                  anchor="w")
        self.title_lbl.pack(side="left", padx=8, pady=5, fill="x", expand=True)
        self.btn_close = tk.Button(self.header, text="×",
                                   command=lambda: app.close_overlay(channel),
                                   **btn_args)
        self.btn_close.pack(side="right", padx=(2, 6), pady=4)
        self.btn_pin = tk.Button(self.header, text="固定", command=self.toggle_pin,
                                 **btn_args)
        self.btn_pin.pack(side="right", padx=2, pady=4)

        # ---------- 底部发送输入框：输入中文回车翻译成英文并复制 ----------
        # 必须先于 text 创建并 pack(side="bottom")：pack 按调用顺序分配空间，
        # 后 pack 的 text(expand) 会抢占全部剩余空间，把输入框压成 1px
        self.entry_frame = tk.Frame(self.win, bg=BG)
        self.entry_frame.pack(side="bottom", fill="x", padx=6, pady=(0, 6))
        # 翻译状态提示（必须先于 entry pack，否则被 expand 的 entry 抢占宽度）
        self._send_status_job = None
        self.send_status = tk.Label(self.entry_frame, text="", bg=BG, fg=FG_DIM,
                                    anchor="e", width=9,
                                    font=("Microsoft YaHei UI", 9))
        self.send_status.pack(side="right", padx=(6, 0))
        self.entry = tk.Entry(self.entry_frame, bg="#1c242c", fg=FG,
                              insertbackground=FG, relief="flat",
                              font=("Microsoft YaHei UI", fs))
        self.entry.pack(fill="x", ipady=3)
        self.entry.bind("<Return>", self._submit_send)
        self.entry.bind("<FocusIn>", self._entry_focus_in)
        self.entry.bind("<FocusOut>", self._entry_focus_out)
        self._placeholder = "输入中文，回车翻译成英文并复制"
        self._entry_ph = False
        self._set_entry_placeholder()
        self.text = tk.Text(self.win, bg=BG, fg=FG, wrap="word", relief="flat", bd=0,
                            font=("Microsoft YaHei UI", fs), padx=8, pady=6,
                            highlightthickness=0)
        # 不显示右侧滚动条轨道，直接用鼠标滚轮滚动
        self.text.pack(fill="both", expand=True)
        self.text.tag_configure("orig", foreground=FG_DIM)
        self.text.tag_configure("spk", foreground=FG_SPK)
        self.text.tag_configure("trans", foreground=FG)
        self.text.tag_configure("send", foreground="#79c0ff")
        self.text.tag_configure("send_ok", foreground="#7ee787")
        self.text.tag_configure("send_err", foreground="#ff7b72")
        self.text.configure(state="disabled")
        self.text.bind("<Button-3>", self._context_menu)

        # ---------- 固定后唯一可点击的“取消固定”按钮（独立窗口） ----------
        # 固定后整窗点击穿透，只有这个小窗口能点到
        self.pin_btn_win = tk.Toplevel(app.root)
        self.pin_btn_win.withdraw()
        self.pin_btn_win.overrideredirect(True)
        self.pin_btn_win.attributes("-topmost", True)
        self.pin_btn_win.configure(bg=BG_HEADER)
        self.pin_btn_unpin = tk.Button(self.pin_btn_win, text="取消固定",
                                       command=self.toggle_pin, **btn_args)
        self.pin_btn_unpin.pack(fill="both", expand=True)

        # 拖动：标题栏 / 标题文字（单窗口，移动即整体移动，无需同步）
        for wdg in (self.header, self.title_lbl):
            wdg.bind("<ButtonPress-1>", self._start_drag)
            wdg.bind("<B1-Motion>", self._do_drag)
            wdg.bind("<Button-3>", self._context_menu)
        # 缩放：消息区四周边缘；标题栏/标题文字只把顶部边缘/角作为整体窗口的“上”把手
        # 注意：标题栏已绑定拖动，缩放处理器必须用 add="+" 追加，否则 bind() 默认替换
        # 会覆盖拖动绑定导致标题栏无法拖动窗口
        for wdg in (self.win, self.text):
            wdg.bind("<Motion>", self._on_motion)
            wdg.bind("<ButtonPress-1>", self._on_resize_start)
            wdg.bind("<B1-Motion>", self._on_resize_drag)
            wdg.bind("<ButtonRelease-1>", self._on_resize_end)
        for wdg in (self.header, self.title_lbl):
            wdg.bind("<Motion>", self._on_motion)
            wdg.bind("<ButtonPress-1>", self._on_resize_start, add="+")
            wdg.bind("<B1-Motion>", self._on_resize_drag, add="+")
            wdg.bind("<ButtonRelease-1>", self._on_resize_end)

        self._load_position()
        self._apply_style()
        self.win.deiconify()
        if self.pinned:
            # 恢复上次的固定状态：整窗穿透 + 隐藏标题栏按钮 + 显示取消固定钮
            set_click_through(self.win, True)
            self.btn_pin.pack_forget()
            self.btn_close.pack_forget()
            self.entry_frame.pack_forget()
            for wdg in (self.win, self.text):
                try:
                    wdg.configure(cursor="arrow")
                except tk.TclError:
                    pass
            self._sync_pin_btn()
            self.app.root.after(120, self._sync_pin_btn)
        else:
            self.pin_btn_win.withdraw()

    # ---------- 固定 / 穿透 ----------
    def toggle_pin(self, event=None):
        self.set_pinned(not self.pinned)

    def set_pinned(self, pinned):
        if self.pinned == pinned:
            return
        self.pinned = pinned
        self._save_pin_state()
        if pinned:
            # 固定后隐藏标题栏上的“固定 / ×”按钮，只保留独立“取消固定”按钮
            set_click_through(self.win, True)
            self.btn_pin.pack_forget()
            self.btn_close.pack_forget()
            self.entry_frame.pack_forget()
            for wdg in (self.win, self.text):
                try:
                    wdg.configure(cursor="arrow")
                except tk.TclError:
                    pass
            self._sync_pin_btn()
        else:
            try:
                self.pin_btn_win.withdraw()
                # 恢复标题栏上的“固定 / ×”按钮（保持原有左右顺序）
                self.btn_close.pack(side="right", padx=(2, 6), pady=4)
                self.btn_pin.pack(side="right", padx=2, pady=4)
                # 恢复输入框：必须先于 text 重 pack，否则 text(expand) 抢占全部空间
                self.text.pack_forget()
                self.entry_frame.pack(side="bottom", fill="x", padx=6, pady=(0, 6))
                self.text.pack(fill="both", expand=True)
                set_click_through(self.win, False)
                self.win.lift()
            except tk.TclError:
                pass

    def _save_pin_state(self):
        pinned = dict(get("window.pinned", {}) or {})
        pinned[self.channel] = self.pinned
        cfg_set("window.pinned", pinned)
        save_config()

    # ---------- 位置 / 拖动 ----------
    def _current_win_pos(self):
        """返回窗口屏幕坐标，优先使用显式维护的 _pos。"""
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

    def _screen_bounds(self):
        """虚拟桌面范围（支持多显示器负坐标），返回 (x, y, w, h)。"""
        try:
            return tuple(_user32.GetSystemMetrics(m) for m in
                         (76, 77, 78, 79))  # SM_XVIRTUALSCREEN/Y/CXVIRTUALSCREEN/CYVIRTUALSCREEN
        except Exception:
            return None

    def _clamp_pos(self, x, y):
        """把窗口位置限制在可见区域内，保证标题栏至少部分可见，避免拖出屏幕找不回。"""
        b = self._screen_bounds()
        if not b:
            return x, y
        sx, sy, sw, sh = b
        return min(max(x, sx), sx + sw - 40), min(max(y, sy), sy + sh - 30)

    def _start_drag(self, event):
        if self.pinned:  # 固定后禁止拖动
            return
        if event.widget in (self.btn_pin, self.btn_close):
            return
        if self._resize_region_for(event) is not None:
            return  # 按在缩放把手上，交给缩放逻辑处理，不启动移动
        # 用屏幕坐标，避免控制面板不在 (0,0) 时拖动错位
        self._drag = (event.x_root - self.win.winfo_rootx(),
                      event.y_root - self.win.winfo_rooty())

    def _do_drag(self, event):
        if self._drag is None or self.pinned:
            return
        x, y = self._clamp_pos(event.x_root - self._drag[0],
                               event.y_root - self._drag[1])
        self._pos = (x, y)
        self.win.geometry(f"+{x}+{y}")

    # ---------- 边缘拖动改变大小（仅未固定时） ----------
    _RESIZE_CURSORS = {
        "nw": "size_nw_se", "se": "size_nw_se",
        "ne": "size_ne_sw", "sw": "size_ne_sw",
        "w": "sb_h_double_arrow", "e": "sb_h_double_arrow",
        "n": "sb_v_double_arrow", "s": "sb_v_double_arrow",
    }

    def _resize_region(self, x, y, w, h):
        """根据控件内坐标判断所在缩放区域（边/角），控件内部返回 None。"""
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

    def _resize_region_for(self, event):
        """根据事件所在控件返回缩放区域。"""
        wdg = event.widget
        if wdg in (self.entry, self.entry_frame):
            return None  # 输入框区域不触发缩放/拖动，避免误改窗口大小
        if wdg in (self.header, self.title_lbl):
            # 标题栏：仅顶部边缘/角作为整体窗口的上边缩放把手
            if event.y > self.EDGE:
                return None
            x = event.x
            w = self.win.winfo_width()
            if x <= self.EDGE:
                return "nw"
            if x >= w - self.EDGE:
                return "ne"
            return "n"
        return self._resize_region(event.x, event.y,
                                   wdg.winfo_width(), wdg.winfo_height())

    def _on_motion(self, event):
        if self.pinned or self._resize:
            return
        region = self._resize_region_for(event)
        cursor = self._RESIZE_CURSORS.get(region, "arrow")
        self.win.configure(cursor=cursor)
        try:
            self.text.configure(cursor=cursor)
        except tk.TclError:
            pass

    def _on_resize_start(self, event):
        if self.pinned:
            return
        region = self._resize_region_for(event)
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
        nx, ny = self._clamp_pos(nx, ny)
        self._pos = (nx, ny)
        self.win.geometry(f"{nw}x{nh}+{nx}+{ny}")

    def _on_resize_end(self, event):
        if self._resize is None:
            return
        self._resize = None
        self.win.update_idletasks()
        # 记住新尺寸（含标题栏的总尺寸），下次启动恢复
        cfg_set("window.width", max(self.win.winfo_width(), self.MIN_W))
        cfg_set("window.height", max(self.win.winfo_height(), self.MIN_H))

    def _sync_pin_btn(self):
        """固定后：独立“取消固定”按钮窗口跟随主窗口右上角。"""
        if not self.pinned:
            return
        try:
            x = self.win.winfo_rootx()
            y = self.win.winfo_rooty()
            w = self.win.winfo_width()
            if w <= 1:
                w = int(get("window.width", _WDEF["width"]))
            bw = max(self.pin_btn_unpin.winfo_reqwidth(), 88)
            self.pin_btn_win.deiconify()
            self.pin_btn_win.lift()
            # lift 会重置坐标，置顶后再定位
            self.pin_btn_win.geometry(f"{bw}x{self.BAR_H - 8}+{x + w - bw - 6}+{y + 4}")
        except tk.TclError:
            pass

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
        alpha = float(get("window.opacity", _WDEF["opacity"]))
        fs = int(get("window.font_size", _WDEF["font_size"]))
        for w in (self.win, self.pin_btn_win):
            try:
                w.attributes("-alpha", alpha)
            except tk.TclError:
                pass
        self.text.configure(font=("Microsoft YaHei UI", fs))
        self.title_lbl.configure(font=("Microsoft YaHei UI", fs + 1, "bold"))

    # ---------- 消息 / 菜单 ----------
    def add_messages(self, items):
        """批量插入并渲染消息（一次状态切换/滚动，减少重绘）。"""
        if not items:
            return
        self.text.configure(state="normal")
        for item in items:
            speaker = item.get("speaker", "")
            original = item.get("original", "")
            translated = item.get("translated", "")
            if speaker:
                self.text.insert("end", f"[{speaker}] ", "spk")
            self.text.insert("end", f"{original}\n", "orig")
            self.text.insert("end", f"    {translated}\n\n", "trans")
            self.recent.append(item)  # 缓存作为发送翻译的上文
        if float(self.text.index("end-1c")) > 1500:
            self.text.delete("1.0", "300.0")
        self.text.see("end")
        self.text.configure(state="disabled")

    # ---------- 底部输入框：中文→英文 翻译并复制 ----------
    def _set_entry_placeholder(self):
        self.entry.delete(0, "end")
        self.entry.insert(0, self._placeholder)
        self.entry.configure(fg=FG_DIM)
        self._entry_ph = True

    def _entry_focus_in(self, event=None):
        if self._entry_ph:
            self.entry.delete(0, "end")
            self.entry.configure(fg=FG)
            self._entry_ph = False

    def _entry_focus_out(self, event=None):
        if not self.entry.get().strip():
            self._set_entry_placeholder()

    def _build_send_context(self):
        """拼装最近 10 条消息作为翻译上文。"""
        lines = []
        for m in self.recent:
            speaker = m.get("speaker", "")
            original = m.get("original", "")
            translated = m.get("translated", "")
            line = f"[{speaker}] {original}" if speaker else original
            if translated and not translated.startswith("[翻译失败"):
                line += f"  → {translated}"
            lines.append(line)
        return "\n".join(lines)

    def _submit_send(self, event=None):
        if self.pinned:
            return
        text = self.entry.get().strip()
        if not text:
            return
        self.entry.delete(0, "end")
        self.entry.configure(fg=FG)
        self._entry_ph = False
        # 立即反馈“翻译中”，避免无响应感
        self._set_send_status("翻译中…", "#79c0ff")
        self._show_send_row(f"[发送] 我：{text}\n", "send")
        self.app.submit_send(self.channel, text, self._build_send_context())

    def _set_send_status(self, text, color):
        """输入框右侧的状态提示；空闲 3 秒后自动清空。"""
        if self._send_status_job is not None:
            try:
                self.win.after_cancel(self._send_status_job)
            except Exception:
                pass
            self._send_status_job = None
        self.send_status.configure(text=text, fg=color)
        if text:
            self._send_status_job = self.win.after(
                3000, lambda: self._set_send_status("", FG_DIM))

    def _show_send_row(self, text, tag):
        self.text.configure(state="normal")
        self.text.insert("end", text, tag)
        if float(self.text.index("end-1c")) > 1500:
            self.text.delete("1.0", "300.0")
        self.text.see("end")
        self.text.configure(state="disabled")

    def _on_send_result(self, result):
        """主线程回调：渲染译文并复制到剪贴板。"""
        if result.get("ok"):
            translated = result.get("translated", "")
            self._show_send_row(f"    {translated}  (已复制到剪贴板)\n", "send_ok")
            self._set_send_status("已复制", "#7ee787")
            try:
                self.app.root.clipboard_clear()
                self.app.root.clipboard_append(translated)
            except tk.TclError:
                pass
        else:
            err = result.get("error", "") or ""
            self._show_send_row(f"    [发送失败] {err}\n", "send_err")
            self._set_send_status("失败", "#ff7b72")

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
        for w in (self.win, self.pin_btn_win):
            try:
                w.destroy()
            except tk.TclError:
                pass


class OverlayApp:
    def __init__(self):
        self.raw_q = queue.Queue()       # 待翻译消息（带递增 seq）
        self.done_q = queue.Queue()      # 已翻译结果（带同 seq）
        self.send_q = queue.Queue()      # 输入框发送请求（中文→英文，带上文）
        self.send_done_q = queue.Queue() # 发送翻译结果
        self.stop_event = threading.Event()
        self._lock = threading.Lock()    # 保护 overlays / watchers
        self.overlays = {}               # channel -> ChannelOverlay
        self.watchers = {}
        self.translator = None
        self._workers = []
        self._seq = itertools.count()
        self._pending = {}               # seq -> item（排序缓冲，保证显示顺序）
        self._next_seq = 0
        self._drag = None
        self._settings_open = False

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=BG)

        self.font = ("Microsoft YaHei UI", int(get("window.font_size", _WDEF["font_size"])))
        self._build_control_panel()
        self.root.deiconify()
        self._load_ctrl_position()
        self.rebuild_overlays()
        self._apply_style()

        self._start_workers()
        self.root.after(120, self._poll_done)
        self.root.after(1000, self._tick_status)  # 定时刷新状态栏

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
        with self._lock:
            pinned = dict(get("window.pinned", {}) or {})
            for ch in list(self.overlays):
                if ch not in channels:
                    self.overlays[ch].destroy()
                    del self.overlays[ch]
                    pinned.pop(ch, None)  # 关闭窗口时清掉该频道的固定状态
            cfg_set("window.pinned", pinned)
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
        self._seq = itertools.count()
        self._pending = {}
        self._next_seq = 0
        threading.Thread(target=self._watch_loop, daemon=True).start()
        n = max(1, min(len(self.overlays) or 1, MAX_TRANSLATE_WORKERS))
        self._workers = []
        for _ in range(n):
            t = threading.Thread(target=self._translate_loop, daemon=True)
            t.start()
            self._workers.append(t)
        # 输入框发送翻译：单线程串行处理即可（频率低）
        t = threading.Thread(target=self._send_worker_loop, daemon=True)
        t.start()
        self._workers.append(t)

    def _restart_workers(self):
        self.stop_event.set()
        self._pending = {}
        self._next_seq = 0
        with self._lock:
            self.watchers = {ch: None for ch in self.overlays}
        self.raw_q = queue.Queue()
        self.done_q = queue.Queue()
        self.send_q = queue.Queue()
        self.send_done_q = queue.Queue()
        self._start_workers()

    def _watch_loop(self):
        while not self.stop_event.is_set():
            log_dir = get("eve.log_dir", "")
            character = get("eve.character", "")
            with self._lock:
                channels = list(self.overlays.keys())
            if log_dir and character and channels:
                for ch in channels:
                    with self._lock:
                        w = self.watchers.get(ch)
                        if (w is None
                                or w.log_dir != log_dir or w.character != character
                                or w.channel != ch):
                            w = eve_logs.ChatLogWatcher(log_dir, character, ch)
                            self.watchers[ch] = w
                    try:
                        for msg in w.poll():
                            msg["channel"] = ch
                            msg["seq"] = next(self._seq)
                            self.raw_q.put(msg)
                    except Exception as e:
                        log_error(f"[监听] channel={ch} 读取日志异常", e)
            self.stop_event.wait(1.0)

    def _translate_with_retry(self, translator, text, target=None, context=None):
        """带退避重试的翻译；不可重试错误直接返回失败信息。"""
        attempts = 3
        last = None
        for attempt in range(attempts):
            try:
                return translator.translate_to(text, target, context)
            except _TRANSIENT as e:
                last = e
                time.sleep(min(1.5 * (attempt + 1), 8))
            except TranslationError as e:
                return f"[翻译失败] {e}"
        return f"[翻译失败，已重试 {attempts} 次] {last}"

    def _translate_loop(self):
        while not self.stop_event.is_set():
            try:
                msg = self.raw_q.get(timeout=1)
            except queue.Empty:
                continue
            channel = msg.get("channel", "")
            seq = msg.get("seq")
            speaker = msg.get("speaker", "")
            original = msg.get("text", "")
            if not original.strip():
                continue
            if not get("api.base_url", "").strip() or not get("api.model", "").strip():
                translated = "[未配置 API，请点“设置”]"
            else:
                if self.translator is None:
                    self.translator = Translator()
                translated = self._translate_with_retry(self.translator, original)
                if translated.startswith("[翻译失败"):
                    log_error(f"[翻译] channel={channel} {translated}")
            self.done_q.put({"channel": channel, "seq": seq, "speaker": speaker,
                             "original": original, "translated": translated})

    def submit_send(self, channel, text, context):
        """把输入框的中文翻译成英文并复制，经后台线程处理。"""
        self.send_q.put({"channel": channel, "text": text, "context": context})

    def _send_worker_loop(self):
        while not self.stop_event.is_set():
            try:
                req = self.send_q.get(timeout=1)
            except queue.Empty:
                continue
            channel = req.get("channel", "")
            text = req.get("text", "")
            if not text.strip():
                continue
            if self.translator is None:
                self.translator = Translator()
            try:
                translated = self._translate_with_retry(
                    self.translator, text, target="English",
                    context=req.get("context") or "")
                ok = not translated.startswith("[翻译失败")
            except Exception as e:
                translated = f"[翻译失败] {e}"
                ok = False
            if not ok:
                log_error(f"[发送] channel={channel} {translated}")
            self.send_done_q.put({"channel": channel, "ok": ok,
                                  "translated": translated,
                                  "error": "" if ok else translated})

    # ---------- UI 刷新 ----------
    def _poll_done(self):
        # 先一次性收走全部结果，避免逐条处理时重复进入事件循环
        try:
            while True:
                item = self.done_q.get_nowait()
                self._pending[item["seq"]] = item
        except queue.Empty:
            pass
        batch = {}
        # 按 seq 连续出列，保证并发翻译下显示顺序不乱
        while self._next_seq in self._pending:
            it = self._pending.pop(self._next_seq)
            batch.setdefault(it.get("channel"), []).append(it)
            self._next_seq += 1
        # 防呆：某个请求卡住导致积压过多时，按序号直接全部展示
        if len(self._pending) > 200:
            for s in sorted(self._pending):
                it = self._pending.pop(s)
                batch.setdefault(it.get("channel"), []).append(it)
            self._next_seq = s + 1
        # 每个频道攒批渲染一次，减少 Text 状态切换与重绘
        for ch, items in batch.items():
            ov = self.overlays.get(ch)
            if ov is not None:
                ov.add_messages(items)
        # 输入框发送结果：主线程渲染并复制到剪贴板
        try:
            while True:
                r = self.send_done_q.get_nowait()
                ov = self.overlays.get(r.get("channel"))
                if ov is not None:
                    ov._on_send_result(r)
        except queue.Empty:
            pass
        self.root.after(120, self._poll_done)

    def _update_ctrl_label(self):
        char = get("eve.character", "")
        with self._lock:
            chans = ", ".join(self.overlays.keys()) or "无"
        pend = self.raw_q.qsize()
        self.status_lbl.configure(
            text=f"角色：{char or '未选择'}\n窗口：{chans}\n待翻译：{pend}")

    def _tick_status(self):
        self._update_ctrl_label()
        self.root.after(1000, self._tick_status)

    def _apply_style(self):
        self.root.attributes("-alpha", float(get("window.opacity", _WDEF["opacity"])))
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
        v_opacity = tk.DoubleVar(value=float(get("window.opacity", _WDEF["opacity"])))
        v_font = tk.IntVar(value=int(get("window.font_size", _WDEF["font_size"])))

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
