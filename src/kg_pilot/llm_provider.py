"""Shared OpenAI-compatible connection validation and credential-safe transport.

This module never discovers credentials. Callers supply a connection and its key
as one snapshot; redirects are never followed and upstream bodies never escape.
"""
from __future__ import annotations

import ipaddress
import base64
import json
import re
import threading
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

PARATERA_BASE_URL = "https://llmapi.paratera.com/v1"
CONFIG_LOCK = threading.RLock()


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


def normalize_base_url(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise ProviderError("请输入有效的 API 基础地址。")
    value = value.strip().rstrip("/")
    if any(c.isspace() or ord(c) < 32 or c == "\\" for c in value):
        raise ProviderError("API 地址不能包含空白或反斜杠。")
    try:
        parsed = urlsplit(value)
        hostname, port = parsed.hostname, parsed.port
        if not hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
            raise ValueError
        hostname = hostname.encode("idna").decode("ascii").lower()
        loopback = hostname == "localhost"
        try:
            loopback = loopback or ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            pass
        if parsed.scheme.lower() != "https" and not (parsed.scheme.lower() == "http" and loopback):
            raise ValueError
        path = parsed.path.rstrip("/")
        if path.endswith("/chat/completions"):
            path = path[:-len("/chat/completions")]
        if any(segment in (".", "..") for segment in path.split("/")) or "%" in path:
            raise ValueError
        host = "[" + hostname + "]" if ":" in hostname else hostname
        default_port = 443 if parsed.scheme.lower() == "https" else 80
        netloc = host + (":" + str(port) if port and port != default_port else "")
        return urlunsplit((parsed.scheme.lower(), netloc, path, "", ""))
    except (ValueError, UnicodeError):
        raise ProviderError("API 地址须为 HTTPS；本机 localhost/loopback 服务可使用 HTTP，且不能包含账号、查询参数或片段。") from None


def connection(config: dict | None = None, *, base_url: str = "", provider: str = "") -> dict:
    config = config or {}
    if not isinstance(config, dict) or not isinstance(provider, str):
        raise ProviderError("API 连接参数格式不正确。")
    address = normalize_base_url(base_url or config.get("base_url") or PARATERA_BASE_URL)
    kind = provider.strip().lower().replace("-", "_") or config.get("provider", "")
    if kind in ("openai", "generic", "api"):
        kind = "openai_compatible"
    if kind not in ("", "paratera", "openai_compatible"):
        raise ProviderError("请选择并行平台或 OpenAI compatible 服务。")
    # A custom host is always treated as a generic service, even when migrating
    # an old configuration whose provider field still says paratera.
    if address != PARATERA_BASE_URL:
        kind = "openai_compatible"
    kind = kind or "paratera"
    return {"provider": kind, "provider_id": kind, "base_url": address}


def public_connection(config: dict | None = None) -> dict:
    result = connection(config)
    result["provider"] = "并行智算云" if result["provider_id"] == "paratera" else "OpenAI compatible"
    return result


def valid_model(value) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 200 and not any(c.isspace() or ord(c) < 32 for c in value)


def check_key(key: str) -> None:
    if not isinstance(key, str) or not key:
        raise ProviderError("请输入 API 密钥。")
    if len(key) > 4096 or any(c.isspace() or ord(c) < 32 for c in key):
        raise ProviderError("密钥格式不正确，请检查是否包含空格或换行。")


def request(method: str, endpoint: str, key: str, payload=None, *, config: dict, timeout: int = 180, transport=None) -> dict:
    target = connection(config)
    if endpoint not in ("/models", "/chat/completions"):
        raise ProviderError("不支持的模型 API 操作。")
    check_key(key)
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout, connect=15), follow_redirects=False, transport=transport) as client:
            response = client.request(method, target["base_url"] + endpoint,
                                      headers={"Authorization": "Bearer " + key}, json=payload)
        if response.status_code in (401, 403):
            raise ProviderError("模型服务拒绝了当前密钥或模型权限，请检查 API 密钥及模型权限。", status=response.status_code)
        if response.status_code == 429:
            raise ProviderError("模型服务当前限流或额度不足，请检查额度后稍后重试。", status=429)
        if response.status_code >= 400 or response.is_redirect:
            raise ProviderError(f"模型服务请求未成功（HTTP {response.status_code}），请检查 API 地址或模型权限。", status=response.status_code)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError
        return result
    except ProviderError:
        raise
    except httpx.TimeoutException:
        raise ProviderError("模型服务响应超时，本次未取得完整结果，请稍后重试。") from None
    except httpx.HTTPError:
        raise ProviderError("无法连接模型服务，请检查 API 地址、本机网络或代理配置。") from None
    except (ValueError, TypeError):
        raise ProviderError("模型服务返回了无法解析的数据，本次未生成结果。") from None
    except Exception:
        raise ProviderError("模型服务请求未能完成，请检查连接设置后重试。") from None


def chat_models(response: dict) -> list[str]:
    excluded = re.compile(r"embed|rerank|ocr|whisper|tts|dall.e|flux|stable.diffusion|text.to.image|text.to.video", re.I)
    result = []
    for row in response.get("data", []) if isinstance(response.get("data"), list) else []:
        if not isinstance(row, dict) or not valid_model(row.get("id")):
            continue
        if not excluded.search(row["id"] + " " + str(row.get("type", "")) + " " + str(row.get("task", ""))):
            result.append(row["id"])
    return sorted(set(result))


def model_preference(name: str):
    lower = name.casefold()
    if "deepseek" not in lower:
        return 100, name
    for rank, marker in enumerate(("v4.1-flash", "v4-flash", "v3.2", "v3.1", "v3", "v4-pro", "r1")):
        if marker in lower:
            return rank, name
    return 20, name


def verify_connection(config: dict, key: str, model: str, *, transport=None) -> tuple[str, list[str]]:
    target = connection(config)
    check_key(key)
    if model and not valid_model(model):
        raise ProviderError("模型名称格式不正确。")
    generic = target["provider_id"] == "openai_compatible"
    # A manually supplied generic model is authoritative. Many compatible
    # gateways do not implement /models at all; only the chat probe is needed.
    models = [] if generic and model else chat_models(
        request("GET", "/models", key, config=target, transport=transport))
    models = [name for name in models if key not in name]
    if model and key in model:
        raise ProviderError("模型名称不能包含 API 密钥，请检查输入。")
    if not generic:
        if not models:
            raise ProviderError("此账号没有返回可用的聊天模型。")
        if model and model not in models:
            raise ProviderError("选择的聊天模型不在此账号返回的模型列表中，请重新连接。")
    elif not model and not models:
        raise ProviderError("服务未返回可用模型列表，请手动填写模型名称后连接。")
    selected = model or min(models, key=model_preference)
    if generic:
        result = request("POST", "/chat/completions", key, {
            "model": selected, "messages": [{"role": "user", "content": "Reply with OK."}],
            "stream": False,
        }, config=target, transport=transport)
        try:
            content = result["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError
        except (KeyError, IndexError, TypeError, ValueError):
            raise ProviderError("聊天接口未返回有效文本，请检查地址与模型是否支持 Chat Completions。") from None
        models = sorted(set([*models, selected]))
    return selected, models


def encode_credential(key: str, config: dict) -> bytes:
    return json.dumps({"version": 1, "base_url": connection(config)["base_url"], "api_key": key}, ensure_ascii=False).encode("utf-8")


def decode_credential(data: bytes, config: dict) -> str:
    address = connection(config)["base_url"]
    try:
        text = data.decode("utf-8").strip()
        if text.startswith("{"):
            record = json.loads(text)
            if record.get("version") != 1 or normalize_base_url(record.get("base_url", "")) != address:
                raise ValueError
            key = record.get("api_key")
            check_key(key)
            return key
        if address != PARATERA_BASE_URL:
            raise ValueError
        check_key(text)
        return text
    except (ValueError, TypeError, UnicodeError, ProviderError):
        raise ProviderError("保存的密钥与当前 API 地址不匹配，请重新输入该地址的密钥。") from None


def ensure_saved_binding(config: dict) -> None:
    # This public metadata check happens before any credential decryption.
    address = connection(config)["base_url"]
    binding = config.get("credential_base_url", PARATERA_BASE_URL)
    if normalize_base_url(binding) != address:
        raise ProviderError("API 地址已改变，请重新输入该地址的密钥。")


def save_connection(config_path: Path, secret_path: Path, config: dict, *, key: str | None, protect) -> None:
    """Commit public config and DPAPI ciphertext in one atomic file replace.

    Legacy standalone secret files remain untouched and are never consulted by
    versioned configurations. No two-file commit can leave a mismatched pair.
    """
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_temp = config_path.with_name(config_path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        saved = {**config, "credential_version": 1}
        if key is not None:
            saved["encrypted_credential"] = base64.b64encode(protect(encode_credential(key, config))).decode("ascii")
        config_temp.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
        config_temp.replace(config_path)
    finally:
        config_temp.unlink(missing_ok=True)


def saved_credential(config: dict, secret_path: Path, *, protect) -> str:
    if "credential_version" in config:
        if config.get("credential_version") != 1:
            raise ProviderError("保存的密钥配置版本无法识别，请重新配置。")
        ensure_saved_binding(config)
        try:
            encoded = config["encrypted_credential"]
            if not isinstance(encoded, str) or len(encoded) > 65536:
                raise ValueError
            encrypted = base64.b64decode(encoded, validate=True)
            return decode_credential(protect(encrypted, decrypt=True), config)
        except (KeyError, ValueError, TypeError):
            raise ProviderError("本机保存的模型密钥无法读取，请重新配置。") from None
    if secret_path.is_file():
        ensure_saved_binding(config)
        return decode_credential(protect(secret_path.read_bytes(), decrypt=True), config)
    return ""


def ensure_environment_compatible(config: dict, key: str, model: str, *, address: str = "", env_key: str = "", env_model: str = "") -> None:
    if (address and normalize_base_url(address) != connection(config)["base_url"] or
            env_key and key and env_key.strip() != key.strip() or
            env_model and model and env_model.strip() != model.strip()):
        raise ProviderError("当前连接受环境变量管理，所填地址、密钥或模型与环境设置冲突；请先修改环境变量并重启服务。")
