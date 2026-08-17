# -*- coding: utf-8 -*-
r"""
EVE Online 聊天日志的发现与解析。

日志默认位置（Windows）：
    真实的“文档”目录\EVE\logs\Chatlogs\
    （“文档”目录可能被系统重定向，例如 D:\Documents）

文件命名：
    <频道名>_<地点>_<yyyyMMdd_HHmmss>[_<角色ID>].txt
    “本地”频道在中文客户端前缀为“本地_”，英文客户端为“Local_”。

多角色场景：
    - 开启“按角色保存日志”：Chatlogs 下有 <角色名> 子目录；
    - 未开启：日志平铺在 Chatlogs 根目录，文件名带角色 ID 后缀
      （如 本地_20260815_071353_2119420699.txt），文件头部有
      “Listener: 角色名”字段，用于识别该日志所属角色。

日志编码：新版客户端为 UTF-16LE（带 BOM），旧版为 UTF-8 / ANSI。
日志行格式：
    [ 2026.08.15 12:00:00 ] 玩家名 > 消息内容
"""
import glob
import os
import re
import threading
import time

MESSAGE_RE = re.compile(
    r"^\[\s*(\d{4}\.\d{2}\.\d{2} \d{2}:\d{2}:\d{2})\s*\]\s+(.+?)\s*>\s*(.*)$"
)
LISTENER_RE = re.compile(r"^\s*Listener:\s*(.+?)\s*$", re.M)
LOCAL_PREFIXES = ("本地_", "Local_", "local_")
SUFFIX_RE = re.compile(r"_(\d{6,})\.txt$")

_header_cache = {}
_header_lock = threading.Lock()
_char_lock = threading.Lock()


# ---------- 编码 / 基础读取 ----------

def decode_log_bytes(raw):
    """按 BOM / 编码回退链解码日志字节（支持 UTF-16 / UTF-8 / GBK 等）。"""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    for enc in ("utf-8-sig", "utf-8", "gb18030", "cp1252"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def parse_message(line):
    """解析一行日志，返回 {'time', 'speaker', 'text'}；不是聊天行则返回 None。"""
    m = MESSAGE_RE.match(line.strip())
    if not m:
        return None
    return {
        "time": m.group(1),
        "speaker": m.group(2).strip(),
        "text": m.group(3).strip(),
    }


# ---------- 目录探测 ----------

def _registry_documents_dir():
    """通过注册表 Shell Folders 的 Personal 项读取真实的“文档”目录。"""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders")
        value, _ = winreg.QueryValueEx(key, "Personal")
        winreg.CloseKey(key)
        return value
    except Exception:
        return None


def user_documents_dirs():
    r"""返回可能的“文档”目录（可正确处理重定向到 D:\Documents 等情况）。

    依次尝试：
    1. 注册表 Shell Folders 的 Personal 项；
    2. Windows 已知文件夹 API（SHGetKnownFolderPath）；
    3. 常见的用户目录候选路径。
    """
    dirs = []
    for probe in (_registry_documents_dir(),):
        if probe and probe not in dirs:
            dirs.append(probe)
    try:
        import ctypes
        from ctypes import wintypes

        shell32 = ctypes.windll.shell32
        ole32 = ctypes.windll.ole32
        SHGetKnownFolderPath = shell32.SHGetKnownFolderPath
        SHGetKnownFolderPath.argtypes = (
            ctypes.c_void_p, wintypes.DWORD, wintypes.HANDLE,
            ctypes.POINTER(ctypes.c_wchar_p))
        SHGetKnownFolderPath.restype = wintypes.HRESULT
        CoTaskMemFree = ole32.CoTaskMemFree
        CoTaskMemFree.argtypes = (ctypes.c_void_p,)
        # FOLDERID_Documents = {FDD39AD0-238F-46AF-ADB4-6C85480369C7}
        guid = (ctypes.c_ubyte * 16)(
            0xD0, 0x39, 0xAD, 0xFD, 0x8F, 0x23, 0xAF, 0x46,
            0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7)
        buf = ctypes.c_wchar_p()
        if SHGetKnownFolderPath(guid, 0, None, ctypes.byref(buf)) == 0 and buf.value:
            if buf.value not in dirs:
                dirs.append(buf.value)
            CoTaskMemFree(buf)
    except Exception:
        pass
    home = os.path.expanduser("~")
    for d in (os.path.join(home, "Documents"),
              os.path.join(home, "OneDrive", "Documents"),
              os.path.join(home, "OneDrive", "文档"),
              os.path.join(home, "文档")):
        if os.path.isdir(d) and d not in dirs:
            dirs.append(d)
    return dirs


def find_chatlog_dirs():
    """自动探测 EVE 聊天日志目录，返回所有候选路径。"""
    result = []
    for doc in user_documents_dirs():
        chat = os.path.join(doc, "EVE", "logs", "Chatlogs")
        if os.path.isdir(chat):
            result.append(chat)
    return result


# ---------- 角色识别 ----------

def is_local_file(path):
    name = os.path.basename(path)
    return name.endswith(".txt") and name.startswith(LOCAL_PREFIXES)


def list_local_files(log_dir):
    return [p for p in glob.glob(os.path.join(log_dir, "*.txt")) if is_local_file(p)]


# ---------- 频道识别 ----------

CHANNEL_ALIASES = {
    "本地": ("本地_", "Local_", "local_"),
    "Local": ("本地_", "Local_", "local_"),
    "local": ("本地_", "Local_", "local_"),
}


def channel_prefixes(channel):
    """返回匹配某频道名的文件名前缀元组。"""
    return CHANNEL_ALIASES.get(channel, (channel + "_",))


def file_channel(path):
    """从日志文件名提取频道/窗口名（第一个 _ 之前的部分）。"""
    name = os.path.basename(path)
    return name.split("_", 1)[0] if name.endswith(".txt") else None


def is_channel_file(path, channel):
    name = os.path.basename(path)
    if not name.endswith(".txt"):
        return False
    return name.startswith(channel_prefixes(channel))


_char_files_cache = {}


def _character_files(log_dir, character):
    """返回该角色名下的所有日志文件路径（带缓存，目录 mtime 变化时自动失效）。"""
    sub = os.path.join(log_dir, character)
    if os.path.isdir(sub):  # 按角色建子目录
        key = (log_dir, character, "sub")
        try:
            mtime = os.stat(sub).st_mtime_ns
        except OSError:
            return []
        with _char_lock:
            cached = _char_files_cache.get(key)
            if cached and cached[0] == mtime:
                return cached[1]
            result = glob.glob(os.path.join(sub, "*.txt"))
            _char_files_cache[key] = (mtime, result)
            return result

    # 平铺模式：按角色 ID 后缀分组，再按 Listener 字段归属角色
    key = (log_dir, character)
    try:
        mtime = os.stat(log_dir).st_mtime_ns
    except OSError:
        return []
    with _char_lock:
        cached = _char_files_cache.get(key)
        if cached and cached[0] == mtime:
            return cached[1]
    groups = {}
    no_suffix = []
    for p in glob.glob(os.path.join(log_dir, "*.txt")):
        m = SUFFIX_RE.search(os.path.basename(p))
        if m:
            groups.setdefault(m.group(1), []).append(p)
        else:
            no_suffix.append(p)
    result = []
    for files in groups.values():
        if character_of_file(max(files, key=os.path.getmtime)) == character:
            result.extend(files)
    for p in no_suffix:
        if character_of_file(p) == character:
            result.append(p)
    with _char_lock:
        if len(_char_files_cache) > 128:
            _char_files_cache.clear()
        _char_files_cache[key] = (mtime, result)
    return result


def list_channels(log_dir, character):
    """列出指定角色有日志记录的频道/窗口名（如 本地、舰队、军团、联盟…）。"""
    if not log_dir or not os.path.isdir(log_dir) or not character:
        return []
    return sorted({file_channel(p) for p in _character_files(log_dir, character)
                   if file_channel(p)})


def find_latest_channel_file(log_dir, character, channel):
    """返回指定角色、指定频道最新的日志文件；找不到返回 None。"""
    if not log_dir or not os.path.isdir(log_dir) or not character:
        return None
    files = [p for p in _character_files(log_dir, character)
             if is_channel_file(p, channel)]
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def character_of_file(path):
    """读取文件头部的 Listener 字段，返回该日志所属的角色名；失败返回 None。"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    with _header_lock:
        cached = _header_cache.get(path)
        if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
            return cached[2]
    try:
        with open(path, "rb") as f:
            raw = f.read(4096)
    except OSError:
        return None
    text = decode_log_bytes(raw)
    m = LISTENER_RE.search(text)
    char = m.group(1).strip() if m else None
    with _header_lock:
        if len(_header_cache) > 2000:
            _header_cache.clear()
        _header_cache[path] = (st.st_mtime_ns, st.st_size, char)
    return char


def list_characters(log_dir):
    """列出可选择的角色。

    优先识别 Chatlogs 下的角色子目录；若无子目录（日志平铺），
    则按文件名的角色 ID 后缀分组，读取每组最新文件头部的
    Listener 字段得到角色名。
    """
    if not log_dir or not os.path.isdir(log_dir):
        return []
    subdirs = [e for e in sorted(os.listdir(log_dir))
               if os.path.isdir(os.path.join(log_dir, e))
               and glob.glob(os.path.join(log_dir, e, "*.txt"))]
    if subdirs:
        return subdirs

    groups = {}
    for p in list_local_files(log_dir):
        m = SUFFIX_RE.search(os.path.basename(p))
        groups.setdefault(m.group(1) if m else "", []).append(p)
    chars = set()
    for key, files in groups.items():
        if not key:
            for p in files:
                c = character_of_file(p)
                if c:
                    chars.add(c)
            continue
        newest = max(files, key=lambda p: os.path.getmtime(p))
        c = character_of_file(newest)
        if c:
            chars.add(c)
    return sorted(chars)


def find_latest_local_file(log_dir, character):
    """返回指定角色当前最新的本地聊天日志文件，找不到返回 None。"""
    return find_latest_channel_file(log_dir, character, "本地")


# ---------- 增量监听 ----------

class ChatLogWatcher:
    """监听指定角色、指定频道（如 本地/舰队）最新的聊天日志，只返回“新增”的消息。

    - 首次接触某个文件时直接跳到文件末尾，避免翻译历史记录；
    - 换空间/换站导致日志文件滚动时，自动跟随到该角色该频道的最新文件；
    - 支持 UTF-16 编码日志的增量读取。
    """

    def __init__(self, log_dir, character, channel="本地"):
        self.log_dir = log_dir
        self.character = character
        self.channel = channel
        self._file = None
        self._enc = None
        self._offset = 0
        self._seen = set()

    @staticmethod
    def _detect_encoding(path):
        try:
            with open(path, "rb") as f:
                head = f.read(2)
        except OSError:
            return None
        if head in (b"\xff\xfe", b"\xfe\xff"):
            return "utf-16"
        return None

    def refresh(self):
        newest = find_latest_channel_file(self.log_dir, self.character, self.channel)
        if not newest:
            self._file = None
            self._offset = 0
            return
        if self._file != newest:
            self._file = newest
            self._enc = self._detect_encoding(newest)
            try:
                with open(newest, "rb") as f:
                    f.seek(0, os.SEEK_END)
                    self._offset = f.tell()
            except OSError:
                self._offset = 0

    def poll(self):
        """读取自上次 poll 以来新增的消息列表。"""
        self.refresh()
        if not self._file:
            return []
        try:
            with open(self._file, "rb") as f:
                f.seek(self._offset)
                chunk = f.read()
                if not chunk:
                    return []
                if self._enc == "utf-16" and len(chunk) % 2:
                    # 防止把一个 UTF-16 字符拦腰截断，留一个字节下轮再读
                    chunk = chunk[:-1]
                    self._offset += len(chunk)
                else:
                    self._offset = f.tell()
        except OSError:
            return []
        text = (chunk.decode(self._enc, errors="replace")
                if self._enc else decode_log_bytes(chunk))
        messages = []
        for line in text.splitlines():
            msg = parse_message(line)
            if not msg:
                continue
            key = (msg["time"], msg["speaker"], msg["text"])
            if key in self._seen:
                continue
            self._seen.add(key)
            if len(self._seen) > 500:
                self._seen = set(list(self._seen)[-300:])
            messages.append(msg)
        return messages
