# EVE 频道翻译悬浮窗

一个针对 **EVE Online** 本地聊天日志的实时翻译工具（仅 Windows）。

监听游戏内的聊天窗口日志，把玩家消息实时翻译为指定语言，显示在半透明悬浮窗中；
悬浮窗内还可直接输入中文，一键翻译成英文并复制到剪贴板，方便在游戏里回复。

> 免责声明：本工具只读取你电脑本地的游戏聊天日志并调用第三方翻译接口，非官方工具，与 CCP Games 无关。

---

## 功能特性

- **实时翻译**：自动监听 EVE 聊天日志目录，选中任意频道（本地 / 舰队 / 军团 / Intel 等）生成对应悬浮窗，新消息即时翻译显示
- **并发翻译**：最多 3 个请求并发处理，并按消息顺序渲染，翻译结果不乱序
- **半透明悬浮窗**：
  - 拖动标题栏移动窗口，四边 / 四角拖拽缩放，可跨多显示器
  - **固定**：整窗鼠标穿透，可叠在游戏画面上，只保留一个小「取消固定」按钮
  - 右键菜单：固定 / 设置 / 关闭窗口
  - 位置、大小、固定状态自动记忆，重启恢复
- **一键发送翻译**：窗口底部输入框输入中文，回车 → 带上该频道最近 10 条消息作为上下文翻译成英文 → 自动复制到剪贴板，直接粘贴进游戏
  - 输入框右侧实时提示：`翻译中…` → `已复制` / `失败`
- **深色主题**，字号 / 透明度可调

---

## 安装与运行

### 方式一：直接使用 exe（推荐）

1. 从 [Releases](https://github.com/yyyhf-6148/eve_channel_trans/releases) 下载 `EVE频道翻译-v*.exe`
2. 双击运行（首次启动会自动生成 `config.json` 并弹出设置窗口）

### 方式二：源码运行

需要 **Windows** + **Python 3.12+**（自带 tkinter）：

```bash
pip install -r requirements.txt
python main.py
```

也可以直接双击 `run.bat`（会自动安装缺失的依赖并以无控制台方式启动）。

### 首次配置

启动后会自动弹出设置窗口，填写：

- **API 地址 / Key / 模型**：任意 OpenAI 兼容的 `/chat/completions` 接口，例如 DeepSeek（`https://api.deepseek.com`）
- **日志目录**：点「自动检测」找到 EVE 日志（默认 `D:\Documents\EVE\logs\Chatlogs`）
- **角色**：选择游戏角色后「刷新窗口」，勾选需要翻译的聊天窗口（本地 / 舰队 / 军团…）

> EVE 客户端需开启「导出日志」并在日志中启用对应窗口，工具才会读到新消息。

---

## 配置说明

配置保存在程序同目录的 `config.json`（**含 API Key，请勿上传 / 分享**）。

| 配置项 | 说明 | 默认 |
|---|---|---|
| `api.base_url` | OpenAI 兼容接口地址 | `https://api.deepseek.com` |
| `api.api_key` | API 密钥 | 空 |
| `api.model` | 模型名称 | `deepseek-chat` |
| `api.temperature` | 采样温度 | `0.3` |
| `translation.target_language` | 聊天翻译目标语言 | `简体中文` |
| `eve.log_dir` | EVE 日志目录 | 空 |
| `eve.character` | 游戏角色名 | 空 |
| `eve.channels` | 需要翻译的频道列表 | `[]` |
| `window.opacity` | 悬浮窗透明度 | `0.75` |
| `window.font_size` | 悬浮窗字号 | `10` |

### 环境变量覆盖（可选）

可用环境变量临时覆盖 API 配置，避免明文 Key 写入磁盘：

- `EVE_TRANS_BASE_URL`
- `EVE_TRANS_API_KEY`
- `EVE_TRANS_MODEL`

### 错误日志

运行异常会追加写入程序同目录的 `error.log`，排查问题时很有用。

---

## 打包为 exe

```bash
pip install pyinstaller
pyinstaller --noconfirm --clean build.spec
```

产物：`dist/EVE频道翻译.exe`（单文件、无控制台窗口）。首次运行会自动生成 `config.json`。

---

## 自动发布 Releases

仓库内置 GitHub Actions 工作流（`.github/workflows/release.yml`）：

- 推送 `v*` 标签时自动在 Windows 上打包 exe 并发布到 Releases
- 也可在 Actions 页面手动触发（不建 Release，仅留构建产物）

发版流程：

```bash
git tag v1.4.0
git push origin v1.4.0
```

---

## 目录结构

```
local-trans/
├── main.py          # 程序入口（异常日志 / DPI / 依赖检查）
├── config.py        # 配置读写（config.json + 默认值 + 错误日志）
├── overlay.py       # 悬浮窗界面（控制面板 + 频道悬浮窗 + 发送输入框）
├── translator.py    # OpenAI 兼容翻译客户端（错误分类 / 重试）
├── eve_logs.py      # EVE 聊天日志扫描 / 角色 / 频道检测
├── build.spec       # PyInstaller 打包配置
├── requirements.txt # 运行依赖
├── run.bat          # 一键启动（自动装依赖）
└── .github/workflows/release.yml  # 自动打包发布
```
