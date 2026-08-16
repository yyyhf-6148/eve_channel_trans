# -*- coding: utf-8 -*-
"""EVE Online 本地聊天自动翻译悬浮窗 —— 程序入口。

用法：
    1. 安装依赖：  pip install -r requirements.txt
    2. 运行：      python main.py
    3. 首次启动会自动弹出设置，填入 API 地址/Key/模型，
       自动检测 EVE 日志目录并选择角色后保存。
"""
import os
import sys


def _setup_exception_logging():
    """安装全局异常钩子：主线程 / tkinter 回调 / 后台线程的未捕获异常
    都会打印到控制台，并追加写入 exe 同目录的 error.log。"""
    import traceback
    import threading

    def _log(typ, val, tb):
        msg = "".join(traceback.format_exception(typ, val, tb))
        print(msg, file=sys.stderr)
        try:
            import config
            log_path = os.path.join(config.APP_DIR, "error.log")
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except Exception:
            pass

    # 主线程未捕获异常
    sys.excepthook = _log
    # tkinter 回调异常（按钮、after、事件绑定等）
    try:
        import tkinter as tk
        tk.Tk.report_callback_exception = \
            lambda self, typ, val, tb: _log(typ, val, tb)
    except Exception:
        pass
    # 后台线程异常（Python 3.8+）
    if hasattr(threading, "excepthook"):
        threading.excepthook = \
            lambda args: _log(args.exc_type, args.exc_value, args.exc_traceback)


def main():
    if os.name != "nt":
        print("该工具仅支持 Windows 系统。")
        sys.exit(1)

    _setup_exception_logging()

    # 高清 DPI 支持
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    try:
        import requests  # noqa: F401
    except ImportError:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "缺少依赖",
            "未安装 requests 库。\n请在命令行执行：\n\n    pip install -r requirements.txt",
        )
        root.destroy()
        sys.exit(1)

    import config
    config.load_config()

    from overlay import OverlayApp
    app = OverlayApp()
    app.run()


if __name__ == "__main__":
    main()
