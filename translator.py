# -*- coding: utf-8 -*-
"""OpenAI 通用格式的翻译客户端。

- 配置实时读取 config（可用环境变量 EVE_TRANS_BASE_URL / EVE_TRANS_API_KEY /
  EVE_TRANS_MODEL 覆盖，便于避免明文 Key 落盘）；
- 对常见的 HTTP 错误分类为可重试/不可重试异常，供上层决定是否退避重试。
"""
import os

import requests

from config import get

SYSTEM_PROMPT = (
    "你是一个游戏聊天翻译助手。把玩家发来的 EVE Online 本地聊天消息翻译成{target}。"
    "规则：1) 只输出译文本身，不要加任何解释、引号或前缀；"
    "2) 地名、星系名、物品名、舰船名、联盟/军团名以及 ISK、DPS、SRP 等专有缩写尽量保留原文或给出玩家通用的说法；"
    "3) 语气保持口语化，贴合游戏语境；4) 原文就是{target}时可只做轻微润色或原样返回。"
)

SEND_SYSTEM_PROMPT = (
    "你是 EVE Online 聊天翻译助手。玩家要把输入的中文消息翻译成{target}后发到游戏频道里。"
    "规则：1) 只输出译文本身，不要加任何解释、引号或前缀；"
    "2) 结合给出的聊天记录上下文，人名、地名、舰船名、联盟/军团名以及 ISK、DPS、SRP 等缩写保持说法一致；"
    "3) 语气保持口语化，贴合游戏语境。"
)


class TranslationError(Exception):
    """翻译失败。retryable=True 表示可以退避重试。"""
    retryable = False


class ConfigError(TranslationError):
    """配置缺失/错误。"""


class AuthError(TranslationError):
    """API Key 无效或无权限。"""


class RateLimitError(TranslationError):
    retryable = True


class ServerError(TranslationError):
    retryable = True


class NetworkError(TranslationError):
    retryable = True


class QuotaError(TranslationError):
    """余额不足。"""


class BadResponseError(TranslationError):
    """服务端返回了无法解析/格式异常的内容。"""


def _env(name):
    return os.environ.get(name, "").strip()


class Translator:
    """使用 OpenAI 兼容的 /chat/completions 接口做翻译，配置实时读取。"""

    def translate(self, text):
        return self.translate_to(text)

    def translate_to(self, text, target=None, context=None):
        """翻译 text 到 target；context 传入最近聊天记录作为上文（用于发送翻译）。"""
        base = (_env("EVE_TRANS_BASE_URL") or get("api.base_url", "") or "").rstrip("/")
        if not base:
            raise ConfigError("未配置 API 地址（请点右上角“设置”）")
        model = _env("EVE_TRANS_MODEL") or get("api.model", "") or ""
        if not model:
            raise ConfigError("未配置模型名称")
        key = _env("EVE_TRANS_API_KEY") or (get("api.api_key", "") or "").strip()
        temperature = float(get("api.temperature", 0.3) or 0.3)
        timeout = float(get("api.timeout", 60) or 60)
        if not target:
            target = get("translation.target_language", "简体中文") or "简体中文"

        url = f"{base}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        if context:
            # 带上最近聊天记录作为上文，保证译文风格与专有名词说法一致
            system = SEND_SYSTEM_PROMPT.format(target=target)
            user = (f"频道最近的聊天记录（作为上文，仅供参照）：\n{context}\n\n"
                    f"请把下面这句话翻译成{target}：\n{text}")
        else:
            system = SYSTEM_PROMPT.format(target=target)
            user = text
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
        }
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
        except requests.exceptions.Timeout as e:
            raise NetworkError(f"请求超时（{timeout}s）") from e
        except requests.exceptions.RequestException as e:
            raise NetworkError(f"网络错误：{e}") from e

        if resp.status_code == 200:
            data = self._parse_json(resp)
            try:
                content = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                raise BadResponseError(f"响应格式异常：{str(data)[:200]}")
            return (content or "").strip()
        self._raise_http_error(resp)

    @staticmethod
    def _parse_json(resp):
        try:
            return resp.json()
        except ValueError:
            raise BadResponseError(f"响应不是合法 JSON：{resp.text[:200]}")

    @staticmethod
    def _raise_http_error(resp):
        try:
            data = resp.json()
            msg = data.get("error", {}).get("message") or str(data)[:200]
        except ValueError:
            msg = resp.text[:200]
        code = resp.status_code
        if code in (401, 403):
            raise AuthError(f"API Key 无效或无权限（HTTP {code}）：{msg}")
        if code == 402:
            raise QuotaError(f"账户余额不足（HTTP 402）：{msg}")
        if code == 429:
            raise RateLimitError(f"请求过于频繁，已触发限流（HTTP 429）：{msg}")
        if 500 <= code < 600:
            raise ServerError(f"服务端错误（HTTP {code}）：{msg}")
        raise BadResponseError(f"请求失败（HTTP {code}）：{msg}")
