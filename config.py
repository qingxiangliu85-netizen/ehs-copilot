"""Central configuration for the EHS Copilot MVP."""

from __future__ import annotations

import os

from dotenv import load_dotenv


load_dotenv()


def _env_int(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是整数，当前值为：{raw_value!r}") from exc
    if value <= 0:
        raise ValueError(f"{name} 必须大于 0，当前值为：{value}")
    return value


CHUNK_SIZE = _env_int("CHUNK_SIZE", 1000)
CHUNK_OVERLAP = _env_int("CHUNK_OVERLAP", 150)
TOP_K = _env_int("TOP_K", 5)

if CHUNK_OVERLAP >= CHUNK_SIZE:
    raise ValueError("CHUNK_OVERLAP 必须小于 CHUNK_SIZE")

EMBEDDING_MODEL_NAME = os.getenv(
    "EMBEDDING_MODEL_NAME",
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
).strip()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
LLM_TIMEOUT_SECONDS = _env_int("LLM_TIMEOUT_SECONDS", 60)

# V5 P0A: SQLite prototype store.  An empty value falls back to
# ``data/ehs_copilot.db``; tests always pass their own temporary path.
EHS_DB_PATH = os.getenv("EHS_DB_PATH", "").strip()

NOT_FOUND_MESSAGE = "当前 SDS 知识库中未找到相关信息。"
SAFETY_DISCLAIMER = (
    "AI 输出仅用于信息检索辅助。实际操作前请核对原始 SDS 及所在单位 EHS 制度。"
)
