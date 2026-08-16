# -*- coding: utf-8 -*-
"""配置读写：所有设置保存在脚本同目录的 config.json 中。

PyInstaller 打包后（frozen）__file__ 指向临时解压目录，
因此改用 exe 所在目录保存配置，保证重启后配置不丢失。
"""
import json
import os
import sys

if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

DEFAULTS = {
    "api": {
        # OpenAI 兼容接口，例如 https://api.deepseek.com
        "base_url": "https://api.deepseek.com",
        "api_key": "",
        "model": "deepseek-chat",
        "temperature": 0.3,
        "timeout": 60,
    },
    "translation": {
        "target_language": "简体中文",
    },
    "eve": {
        "log_dir": "",
        "character": "",
        "channels": [],
    },
    "window": {
        "opacity": 0.75,
        "font_size": 10,
        "width": 383,
        "height": 233,
        "pos_x": None,
        "pos_y": None,
        "positions": {},
    },
}

# 全局共享的配置字典（各模块直接读取/修改）
cfg = {}


def _merge(defaults, loaded):
    merged = dict(defaults)
    if isinstance(defaults, dict) and isinstance(loaded, dict):
        for k, v in loaded.items():
            if k in defaults and isinstance(defaults[k], dict) and isinstance(v, dict):
                merged[k] = _merge(defaults[k], v)
            else:
                merged[k] = v
    return merged


def load_config():
    """加载 config.json（不存在则用默认值并自动生成），返回配置字典。"""
    global cfg
    loaded = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                loaded = json.load(f)
        except Exception:
            loaded = {}
    cfg = _merge(DEFAULTS, loaded)
    # 配置文件不存在时自动生成，保证单 exe 首次启动即创建干净配置
    if not os.path.exists(CONFIG_PATH):
        try:
            save_config()
        except Exception:
            pass
    return cfg


def save_config():
    """将当前配置写回 config.json。"""
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def get(key_path, default=None):
    """按 'a.b.c' 形式读取配置值。"""
    node = cfg
    for part in key_path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return default
    return node


def set(key_path, value):
    """按 'a.b.c' 形式写入配置值。"""
    keys = key_path.split(".")
    node = cfg
    for part in keys[:-1]:
        node = node.setdefault(part, {})
    node[keys[-1]] = value
