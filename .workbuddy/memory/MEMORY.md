# EHS Copilot — 项目长期约定

> 每日细节见 `memory/YYYY-MM-DD.md`。本文件只记跨会话仍然成立的事实与硬约定。

## 项目定位（不可含糊）

面向真实 EHS 业务流程设计的 **AI 工作流原型**，**未在真实企业生产环境部署**。README 顶部、Disclaimer、评测章节都必须保持这一表述。所有 SDS/JSA/隐患数据为模拟数据。

## 架构约定

- `workflow/` + `tools/` **只做编排与适配，不复制业务逻辑**。算术、校验、聚合一律留在 `rag.py` / `jsa.py` / `hazards.py` / `dashboard.py`。
- 六个工具全部是**薄包装**；工具**永不抛异常**，统一返回 `{status: ok|blocked|error}`。
- 会话上下文通过 `contextvars`（`tools/context.py`）传递，**不要**把 FAISS 向量库塞进图状态。
- 安全规则**不自己算分**：风险分值只由 `jsa.calculate_risk` 产生，再由同一函数复算校验。
- `app.py` 的既有四个页面尽量不动；新增页面以追加导航项的方式接入。

## HITL 硬约定

- **`run_workflow(..., auto_approve=True)` 的默认值是有意为之**，用于保住第一阶段的程序化调用语义与 52 个原测试。**Streamlit 页面强制 `auto_approve=False`**，真实路径必然人工审批。以后若要翻转默认值，必须同时重跑全部测试。
- `interrupt()` 所在节点在恢复时会**从头重跑**，且该节点在 `interrupt()` 之前的 return 更新**全部丢失**。所以：(a) 中断点前不能有副作用；(b) 需要在暂停时仍可见的状态（如紧急事件提示）必须放在**不会中断**的节点（`route`）。
- 一个节点一次只允许一个 `interrupt()`。
- 「修改」只允许改参数、不允许换工具；**任何会引入新审批项的修改一律按拒绝处理**（单次审批不能给升级操作背书）——这是设计行为，不是 bug。

## 评测约定（改 `evals/` 前必读）

- 数据集 `evals/dataset.jsonl` 是**固定回归基线**；`python evals/evaluate.py` 输出 `results.json` + `RESULTS.md`。数字必须实际运行得出，**禁止手写**。
- 所有场景以 `auto_approve=False` 回放（真实 HITL 路径）。
- 指标口径：
  - **Tool Call Accuracy = 计划序列 AND 实际执行序列都对**。只看实际执行会让暂停场景"空序列自动通过"。
  - **Citation Coverage 分母 = 真正交付的 SDS 结论**；被正确拦截的不计入分母（另列 withheld）。`human_confirmed` 计入分母、不计入分子 → 因此该指标**故意**停在 85.7%。
  - **Approval Compliance = 148 条不变式通过率**，不是场景通过率；其中有"自动放行不算人工审批"这条通用不变式。
  - Routing / Tool Call 是 **in-sample**，不等于泛化能力，README 已写明。
- 若数据集的 `expect.status == "rejected"` 而 `decision.action == "modify"`，检查项必须是**"拒绝是对的"**，不能断言"修改已生效"（`hz-08` 就是这种场景）。

## 已知真实缺陷（不要当成测试 bug 去"修测试"）

- **路由盲点**：`今天有几个隐患要到期` 会被规划成 `create_hazard`（`几个` 不在 `_READ_ONLY_HINTS` 内）；`把 HZ-001 关掉`、`帮我评估一下这个作业的风险`、`检查一下配电箱`、`危险源辨识怎么做` 均识别不出任务。风险已被审批门兜住，但计划是错的。
- 参数抽取退化：`为该作业做 JSA` → JSA「作业名称」= 「该作业」。
- 上述已写入 README「项目限制」，**修复前不要从 README 删除**。

## 本机环境陷阱

- `os.symlink()` 在本机静默产出 0 字节文件 → 必须保留 `.env` 的 `HF_HUB_DISABLE_SYMLINKS=1`；embedding 模型走本地目录 `D:\ai-cache\models\...`。
- **bash 垫片已损坏**：`ls`/`tail`/`grep`/`dirname` 等 `command not found`，`/tmp` 重定向不生效。**长驻进程用 `run_in_background`**；文本处理改用 `.venv/Scripts/python.exe -c "..."` 内联脚本；文件检索用 Glob/Grep 工具。
- 同一文件的多次编辑**必须串行**，并行 Edit 会互相覆盖。
