"""Streamlit entry point for EHS Copilot v0.1."""

from __future__ import annotations

import re
from pathlib import Path

import streamlit as st

from config import NOT_FOUND_MESSAGE, SAFETY_DISCLAIMER
from llm import LLMConfigurationError, LLMResponseError, is_llm_configured
from rag import (
    SDSProcessingError,
    answer_question,
    build_knowledge_base,
    collect_sources,
    retrieve_documents,
)


EXAMPLE_QUESTIONS = (
    "主要危险性是什么？",
    "操作需要哪些 PPE？",
    "皮肤接触后如何处理？",
    "应该如何储存？",
    "泄漏时采取什么措施？",
    "火灾时使用什么灭火介质？",
)
DEMO_SDS_PATH = (
    Path(__file__).resolve().parent
    / "data"
    / "demo_sds"
    / "EHS_Copilot_Demo_Synthetic_SDS.pdf"
)
class LocalDemoPDF:
    """Adapt a repository demo PDF to the existing upload interface."""

    def __init__(self, path: Path):
        self.name = path.name
        self._payload = path.read_bytes()

    def getvalue(self) -> bytes:
        return self._payload


st.set_page_config(
    page_title="EHS Copilot｜AI-powered SDS Safety Assistant",
    page_icon="🧪",
    layout="wide",
)


def initialize_state() -> None:
    defaults = {
        "vector_store": None,
        "loaded_files": [],
        "page_count": 0,
        "chunk_count": 0,
        "messages": [],
        "suggested_question": None,
        "knowledge_base_mode": None,
        "auto_demo_attempted": False,
        "auto_demo_error": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def build_evidence(documents: tuple[object, ...]) -> list[dict[str, object]]:
    evidence: list[dict[str, object]] = []
    seen: set[tuple[str, int]] = set()
    for document in documents:
        source = str(document.metadata.get("source", "未知文件"))
        page = int(document.metadata.get("page", 0))
        source_page = (source, page)
        if source_page in seen:
            continue
        seen.add(source_page)
        snippet = " ".join(str(document.page_content).split())
        if len(snippet) > 320:
            snippet = f"{snippet[:320].rstrip()}…"
        evidence.append(
            {
                "rank": len(evidence) + 1,
                "source": source,
                "page": page,
                "snippet": snippet,
            }
        )
    return evidence


def render_evidence(
    evidence: list[dict[str, object]],
    sources: tuple[tuple[str, int], ...] = (),
) -> None:
    st.markdown("#### 证据来源")
    if evidence:
        for item in evidence:
            with st.container(border=True):
                st.markdown(
                    f"**相关性排序 #{item.get('rank', 1)} · "
                    f"{item['source']} · 第 {item['page']} 页**"
                )
                st.caption(str(item["snippet"]))
        return
    if sources:
        for source, page in sources:
            with st.container(border=True):
                st.markdown(f"**{source} · 第 {page} 页**")
                st.caption("该历史消息未保存检索片段，请以原始 SDS 为准。")
        return
    st.caption("未检索到能够支持该回答的 SDS 证据。")


def render_assistant_response(
    content: str,
    evidence: list[dict[str, object]],
    sources: tuple[tuple[str, int], ...] = (),
    response_mode: str = "ai",
) -> None:
    st.markdown("### 查询结果")
    st.markdown("#### 最相关信息 / 摘要")
    st.markdown(content)
    render_evidence(evidence, sources)
    st.caption(SAFETY_DISCLAIMER)


def render_empty_state() -> None:
    st.info(
        "上传 SDS 后，可直接用自然语言查询危险性、PPE、储存、急救、消防及泄漏处置信息，"
        "无需逐页翻阅文档。"
    )
    st.markdown("### 三步开始使用")
    step_columns = st.columns(3)
    steps = (
        ("① 上传 SDS", "在左侧上传一个或多个包含可复制文字的 SDS PDF。"),
        ("② 构建知识库", "点击“构建 / 更新知识库”，系统将解析文件并建立索引。"),
        ("③ 基于证据提问", "输入问题，系统将进行语义检索并显示摘要、来源文件、页码和原文。"),
    )
    for column, (title, description) in zip(step_columns, steps):
        with column:
            with st.container(border=True):
                st.markdown(f"**{title}**")
                st.caption(description)


def render_example_questions(knowledge_base_ready: bool) -> None:
    st.markdown("### 可以问什么？")
    st.caption(
        "知识库就绪后，可点击示例直接提问。"
        if knowledge_base_ready
        else "完成 SDS 知识库构建后，即可点击以下示例问题。"
    )
    question_columns = st.columns(2)
    for index, example in enumerate(EXAMPLE_QUESTIONS):
        if question_columns[index % 2].button(
            example,
            key=f"example_question_{index}",
            use_container_width=True,
            disabled=not knowledge_base_ready,
        ):
            st.session_state.suggested_question = example


def activate_knowledge_base(result: object, mode: str) -> None:
    st.session_state.vector_store = result.vector_store
    st.session_state.loaded_files = list(result.file_names)
    st.session_state.page_count = result.page_count
    st.session_state.chunk_count = result.chunk_count
    st.session_state.messages = []
    st.session_state.knowledge_base_mode = mode


def build_extractive_summary(documents: tuple[object, ...]) -> str:
    """Extract a concise passage from the top retrieved SDS result."""
    if not documents:
        return NOT_FOUND_MESSAGE

    lines = [line.strip() for line in str(documents[0].page_content).splitlines()]
    chinese_lines: list[str] = []
    other_lines: list[str] = []
    for line in lines:
        if not line:
            continue
        lowered = line.casefold()
        if lowered.startswith("synthetic sds for demonstration only") or line.startswith(
            "模拟 SDS，仅用于软件演示"
        ):
            continue
        if line in {"中文 / CHINESE", "英文 / ENGLISH"}:
            continue
        if lowered.startswith("ehs copilot demo cleaner") or lowered.startswith(
            "synthetic sds | page"
        ):
            continue
        if re.match(r"^(section|第)\s*\d+", line, flags=re.IGNORECASE):
            continue
        if (
            not re.search(r"[\u4e00-\u9fff]", line)
            and line.upper() == line
            and len(line.split()) <= 8
        ):
            continue
        line = re.sub(r"^[A-Z0-9][A-Z0-9_-]{2,}:\s*", "", line)
        if re.search(r"[\u4e00-\u9fff]", line):
            chinese_lines.append(line)
        else:
            other_lines.append(line)

    summary = " ".join(chinese_lines or other_lines).strip()
    if not summary:
        return "匹配到以下 SDS 证据，请核对原文。"
    if len(summary) > 360:
        summary = f"{summary[:360].rstrip()}…"
    return summary


def build_retrieval_response(vector_store: object, question: str) -> dict[str, object]:
    documents = tuple(retrieve_documents(vector_store, question))
    evidence = build_evidence(documents)
    sources = collect_sources(documents)
    content = build_extractive_summary(documents)
    return {
        "content": content,
        "sources": sources,
        "evidence": evidence,
        "response_mode": "retrieval",
    }


initialize_state()
llm_available = is_llm_configured()

if (
    st.session_state.vector_store is None
    and not st.session_state.auto_demo_attempted
):
    st.session_state.auto_demo_attempted = True
    try:
        with st.spinner("正在自动加载 Synthetic SDS 并建立 Demo 知识库……"):
            demo_result = build_knowledge_base([LocalDemoPDF(DEMO_SDS_PATH)])
        activate_knowledge_base(demo_result, mode="demo")
    except Exception as exc:
        st.session_state.auto_demo_error = str(exc)

with st.sidebar:
    st.header("EHS Copilot")
    st.caption("上传并构建自己的 SDS 知识库")
    uploaded_files = st.file_uploader(
        "上传一个或多个 SDS PDF",
        type=["pdf"],
        accept_multiple_files=True,
        help="v0.1 仅解析包含可复制文字的 PDF，暂不支持扫描件 OCR。",
    )

    if uploaded_files:
        st.markdown("**待构建文件：**")
        for uploaded_file in uploaded_files:
            st.write(f"- {uploaded_file.name}")

    if st.button("构建 / 更新知识库", type="primary", use_container_width=True):
        if not uploaded_files:
            st.warning("请先上传至少一份 SDS PDF。")
        else:
            try:
                with st.spinner("正在解析 PDF、生成 Embedding 并构建 FAISS 索引……"):
                    result = build_knowledge_base(uploaded_files)
                activate_knowledge_base(result, mode="uploaded")
                st.success(
                    f"知识库已更新：{len(result.file_names)} 个文件，"
                    f"{result.page_count} 个文本页，{result.chunk_count} 个文本块。"
                )
            except SDSProcessingError as exc:
                st.error(str(exc))
            except Exception as exc:
                st.error(f"知识库构建失败：{exc}")

    st.divider()
    st.markdown("**当前已加载文件：**")
    if st.session_state.loaded_files:
        if st.session_state.knowledge_base_mode == "demo":
            st.success("Demo 知识库已就绪")
        for file_name in st.session_state.loaded_files:
            st.write(f"- {file_name}")
        st.caption(
            f"文本页：{st.session_state.page_count}｜"
            f"文本块：{st.session_state.chunk_count}"
        )
    else:
        st.caption("尚未构建知识库")

    st.divider()
    st.warning(SAFETY_DISCLAIMER)

st.title("EHS Copilot")
st.subheader("AI × EHS 危化品 SDS 智能检索助手")
st.markdown(
    "**基于语义检索快速定位危险性、PPE、储存、急救、泄漏及消防信息，"
    "并提供原始 SDS 页码与证据追溯。**"
)

capability_columns = st.columns(3)
for column, capability in zip(
    capability_columns,
    ("AI Semantic Search", "SDS Evidence Retrieval", "Multi-PDF Knowledge Base"),
):
    with column:
        with st.container(border=True):
            st.markdown(f"**{capability}**")

if st.session_state.auto_demo_error:
    st.error(f"Demo 知识库自动加载失败：{st.session_state.auto_demo_error}")

if st.session_state.vector_store is None:
    render_empty_state()
else:
    if st.session_state.knowledge_base_mode == "demo":
        st.success("Demo 知识库已就绪，可以开始提问。")
    else:
        st.success("知识库已就绪，可以开始提问。")
    status_columns = st.columns(3)
    status_columns[0].metric("已加载 SDS", f"{len(st.session_state.loaded_files)} 份")
    status_columns[1].metric("文本页", st.session_state.page_count)
    status_columns[2].metric("文本 Chunks", st.session_state.chunk_count)

render_example_questions(st.session_state.vector_store is not None)

st.divider()

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        if message["role"] == "assistant":
            render_assistant_response(
                message["content"],
                message.get("evidence", []),
                message.get("sources", ()),
                message.get("response_mode", "ai"),
            )
        else:
            st.markdown(message["content"])

typed_question = st.chat_input(
    "例如：HF 皮肤接触后应该如何处理？",
    disabled=st.session_state.vector_store is None,
)
question = typed_question or st.session_state.pop("suggested_question", None)

if question:
    st.session_state.suggested_question = None
    st.session_state.messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    with st.chat_message("assistant"):
        try:
            with st.spinner("正在检索 SDS……"):
                if llm_available:
                    result = answer_question(st.session_state.vector_store, question)
                    response = {
                        "content": result.answer,
                        "sources": result.sources,
                        "evidence": build_evidence(result.retrieved_documents),
                        "response_mode": "ai",
                    }
                else:
                    response = build_retrieval_response(
                        st.session_state.vector_store,
                        question,
                    )
            render_assistant_response(
                response["content"],
                response["evidence"],
                response["sources"],
                response["response_mode"],
            )
            st.session_state.messages.append({"role": "assistant", **response})
        except LLMConfigurationError as exc:
            st.warning(str(exc))
        except LLMResponseError as exc:
            st.error(str(exc))
        except Exception as exc:
            st.error(f"问答失败：{exc}")

st.divider()
st.markdown("**Tech Stack：Python · Streamlit · HuggingFace Embeddings · FAISS**")
st.markdown("**Safety Note**")
st.caption(
    "For information retrieval assistance only. Always verify the original SDS "
    "and site-specific EHS procedures."
)
