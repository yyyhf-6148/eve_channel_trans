# -*- coding: utf-8 -*-
"""OpenAI 通用格式的翻译客户端。"""
import requests

from config import get

SYSTEM_PROMPT = (
    "你是一个游戏聊天翻译助手。把玩家发来的 EVE Online 本地聊天消息翻译成{target}。"
    "规则：1) 只输出译文本身，不要加任何解释、引号或前缀；"
    "2) 地名、星系名、物品名、舰船名、联盟/军团名以及 ISK、DPS、SRP 等专有缩写尽量保留原文或给出玩家通用的说法；"
    "3) 语气保持口语化，贴合游戏语境；4) 原文就是{target}时可只做轻微润色或原样返回。"
)


class Translator:
    """使用 OpenAI 兼容的 /chat/completions 接口做翻译，配置实时读取 config。"""

    def translate(self, text):
        base = (get("api.base_url", "") or "").rstrip("/")
        if not base:
            raise RuntimeError("未配置 API 地址（请点右上角“设置”）")
        model = get("api.model", "gpt-4o-mini") or ""
        if not model:
            raise RuntimeError("未配置模型名称")
        key = (get("api.api_key", "") or "").strip()
        temperature = float(get("api.temperature", 0.3) or 0.3)
        timeout = float(get("api.timeout", 60) or 60)
        target = get("translation.target_language", "简体中文") or "简体中文"

        url = f"{base}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT.format(target=target)},
                {"role": "user", "content": text},
            ],
            "temperature": temperature,
        }
        resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise RuntimeError(f"响应格式异常：{str(data)[:200]}")
        return (content or "").strip()
