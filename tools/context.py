"""Shared runtime context for the EHS Copilot workflow tools.

Tools never own session state.  They read the objects they need from a
:class:`ToolContext` supplied by the caller (the Streamlit page, the workflow
graph or a unit test), so the same tool function behaves identically in all
three places.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator


@dataclass
class ToolContext:
    """Session-scoped objects that a tool may read from or write back to.

    ``jsa_records`` and ``hazard_records`` are the *live* session lists, so a
    tool that appends or updates a record is immediately visible to the JSA and
    hazard pages of the Streamlit app.
    """

    vector_store: Any = None
    jsa_records: list[dict[str, object]] = field(default_factory=list)
    hazard_records: list[dict[str, object]] = field(default_factory=list)
    loaded_files: tuple[str, ...] = ()

    @property
    def sds_ready(self) -> bool:
        """Return whether an SDS knowledge base has been built in this session."""
        return self.vector_store is not None

    def describe(self) -> dict[str, object]:
        """Return a small, display-ready snapshot of the current context."""
        return {
            "sds_ready": self.sds_ready,
            "jsa_records": len(self.jsa_records),
            "hazard_records": len(self.hazard_records),
            "loaded_files": list(self.loaded_files),
        }


class ToolContextError(RuntimeError):
    """Raised when a workflow tool runs without an active ToolContext."""


_CURRENT_CONTEXT: ContextVar[ToolContext | None] = ContextVar(
    "ehs_copilot_tool_context", default=None
)


@contextmanager
def use_context(context: ToolContext) -> Iterator[ToolContext]:
    """Bind ``context`` for the duration of the ``with`` block."""
    token = _CURRENT_CONTEXT.set(context)
    try:
        yield context
    finally:
        _CURRENT_CONTEXT.reset(token)


def current_context() -> ToolContext:
    """Return the context bound by :func:`use_context`."""
    context = _CURRENT_CONTEXT.get()
    if context is None:
        raise ToolContextError(
            "工作流工具缺少会话上下文，请先通过 use_context(...) 绑定。"
        )
    return context
