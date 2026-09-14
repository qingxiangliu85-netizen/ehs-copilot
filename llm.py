"""LLM access layer with a single replaceable interface."""

from __future__ import annotations

import json
from typing import Any, Mapping

from config import (
    LLM_TIMEOUT_SECONDS,
    NOT_FOUND_MESSAGE,
    OPENAI_API_KEY,
    OPENAI_BASE_URL,
    OPENAI_MODEL,
)


SYSTEM_PROMPT = f"""You are an EHS SDS information retrieval assistant.

STRICT RULES:

1. Answer only using the provided SDS context.
2. Do not use outside knowledge.
3. Do not guess, infer, estimate, or fabricate safety information.
4. If the answer cannot be found in the provided SDS context, reply exactly:
   “{NOT_FOUND_MESSAGE}”
5. Prioritize accurate hazard, PPE, storage, first-aid, spill and emergency information.
6. Keep the response concise and professional.
7. Answer in the same language as the user's question.
8. The AI response is only an information retrieval aid and does not replace the original SDS or site-specific EHS procedures.
"""


class LLMConfigurationError(RuntimeError):
    """Raised when the required LLM configuration is missing."""


class LLMResponseError(RuntimeError):
    """Raised when the LLM service returns an unusable response."""


def is_llm_configured() -> bool:
    """Return whether a non-placeholder API configuration is available."""
    placeholder_values = {
        "your-api-key-here",
        "your_api_key_here",
        "changeme",
        "replace-me",
    }
    return bool(
        OPENAI_API_KEY
        and OPENAI_MODEL
        and OPENAI_API_KEY.casefold() not in placeholder_values
    )


def generate_response(prompt: str) -> str:
    """Generate one answer through an OpenAI-compatible chat API."""
    from openai import OpenAI

    if not is_llm_configured():
        raise LLMConfigurationError(
            "未检测到 OPENAI_API_KEY。请复制 .env.example 为 .env，并填写有效 API Key。"
        )
    if not OPENAI_MODEL:
        raise LLMConfigurationError("OPENAI_MODEL 不能为空。")

    client_kwargs: dict[str, object] = {
        "api_key": OPENAI_API_KEY,
        "timeout": LLM_TIMEOUT_SECONDS,
    }
    if OPENAI_BASE_URL:
        client_kwargs["base_url"] = OPENAI_BASE_URL

    client = OpenAI(**client_kwargs)
    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        temperature=0,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    )

    content = response.choices[0].message.content
    if not content or not content.strip():
        raise LLMResponseError("LLM 返回了空内容，请稍后重试。")
    return content.strip()


def generate_structured_response(
    *,
    system_prompt: str,
    user_prompt: str,
) -> dict[str, Any]:
    """Return one constrained JSON object through the configured chat API.

    Phase 1 validates the returned fields again in its domain layer.  This
    helper intentionally does not know about Safety Review Packs and leaves the
    existing SDS-specific interface unchanged.
    """
    from openai import OpenAI

    if not is_llm_configured():
        raise LLMConfigurationError("未配置LLM，使用规则与语义检索模式。")

    client_kwargs: dict[str, object] = {
        "api_key": OPENAI_API_KEY,
        "timeout": LLM_TIMEOUT_SECONDS,
    }
    if OPENAI_BASE_URL:
        client_kwargs["base_url"] = OPENAI_BASE_URL
    client = OpenAI(**client_kwargs)
    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": str(system_prompt)},
            {"role": "user", "content": str(user_prompt)},
        ],
    )
    content = response.choices[0].message.content
    if not content or not content.strip():
        raise LLMResponseError("LLM返回了空的结构化响应。")
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError) as exc:
        raise LLMResponseError("LLM未返回有效JSON对象。") from exc
    if not isinstance(parsed, Mapping):
        raise LLMResponseError("LLM结构化响应必须是JSON对象。")
    return dict(parsed)
