"""Public provider information for the local CLI subscription channel."""

import shutil

from kg_pilot.codex_subscription import SOL_MODEL


def status() -> dict:
    available = shutil.which("codex") is not None
    current = {
        "provider_id": "codex",
        "provider": "Codex CLI",
        "model": SOL_MODEL,
        "effort": "high",
        "implemented": True,
        "available": available,
        "auth_status": "unknown" if available else "not_installed",
        "capabilities": {"text": True, "images": True, "quota": True},
    }
    providers = [current]
    for provider_id, provider in (
        ("claude_code", "Claude Code CLI"),
        ("opencode", "OpenCode"),
        ("gemini", "Gemini CLI"),
    ):
        providers.append({
            "provider_id": provider_id,
            "provider": provider,
            "model": None,
            "effort": None,
            "implemented": False,
            "available": False,
            "auth_status": "not_implemented",
            "capabilities": {"text": False, "images": False, "quota": False},
        })
    return {
        **current,
        "channel": "subscription",
        "configured": available,
        "message": "使用本机 Codex 订阅。" if available else "未找到 Codex CLI。",
        "quota": {"status": "unknown" if available else "unavailable", "reset_at": None},
        "providers": providers,
    }
