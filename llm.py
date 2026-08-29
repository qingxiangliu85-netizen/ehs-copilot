"""LLM access layer with a single replaceable interface."""

from __future__ import annotations

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
