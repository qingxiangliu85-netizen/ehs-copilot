"""V5 product UI (P0C).

This package is the business front end of EHS Copilot V5.  It replaces the V4
multi-page console with five first-level pages:

* 今日工作台 — what the signed-in Demo persona must handle today;
* 作业许可   — permit list and permit detail (JSA and SDS live inside it);
* 隐患与整改 — hazard list and hazard detail;
* 风险看板   — five KPIs and only the charts that have real data;
* 资料与审计 — SDS knowledge base + append-only operation records.

Design rules kept from the project conventions:

* the UI never re-implements a business rule — every click is forwarded to the
  same ``services.*`` command the API would call, and those commands re-check
  permission and state, so hiding a button is never the security boundary;
* technical vocabulary (workflow engine, planners, tool calls, raw payloads)
  stays out of the business screens; the preserved technical console keeps
  running as a separate entry point (:mod:`legacy_console`).

Modules are imported explicitly (``from ui import today``) so the package has no
import-order coupling.
"""

from __future__ import annotations
