# HF Demo Evidence Pack / HF 案例证据包

本目录为 V4「HF 酸洗模拟案例」提供**可追溯的公开来源安全证据**。它解决的是 P2 的核心问题：在没有可合法随仓库分发的完整 HF SDS 时，如何让 Demo 的每一条安全信息都能回溯到真实、权威、可公开访问的来源。

> 本证据包不是 SDS，也不得作为 SDS 证据进入工作流的安全结论。
> 它只用于作品集演示与人工交叉核对。

## 1. 两条证据轨道（不得混用）

| 轨道 | 内容 | 来源 | 状态 | 展示标注 |
| --- | --- | --- | --- | --- |
| A. 真实公开来源安全证据 | 危险性、暴露限值、PPE、急救、泄漏、消防等**逐字引用片段** | NIOSH / OSHA 等美国政府公开资料 | ✅ 已有（`hf_evidence_pack.json`） | “公开来源证据（非 SDS）” |
| B. HF SDS 证据 | SDS 各章节原文与证据页码 | 需登记可合法再分发的 SDS | ⏳ `pending_real_source` | “HF SDS 证据（待补来源）” |

模拟业务数据（作业名称、区域、人员、责任人、日期、隐患编号、整改证据占位）始终单独标记为“模拟数据 / Demo”，**不得与轨道 A 或 B 混排在同一列表或表格中**。

## 2. 来源清单与入仓策略

| 来源 | 机构 | 类型 | 可访问 | 是否可入仓 | 说明 |
| --- | --- | --- | --- | --- | --- |
| [NIOSH Pocket Guide — Hydrogen fluoride](https://www.cdc.gov/niosh/npg/npgd0334.html) | NIOSH (US CDC) | 官方公开数据库 | 是 | ✅ 链接 + 短引用片段 | 美国联邦政府作品，公共领域候选 |
| [NIOSH ERSHDB — Hydrogen Fluoride/Hydrofluoric Acid](https://www.cdc.gov/niosh/ershdb/emergencyresponsecard_29750030.html) | NIOSH (US CDC) | 官方应急数据库 | 是 | ✅ 链接 + 短引用片段 | 应急场景内容，展示时需与常规作业区分 |
| [OSHA Occupational Chemical Database — Hydrogen Fluoride](https://www.osha.gov/chemicaldata/622) | OSHA (US DOL) | 官方监管数据库 | 是 | ✅ 链接 + 短引用片段 | 暴露限值交叉核对 |
| [NOAA CAMEO Chemicals — HFX](https://cameochemicals.noaa.gov/chris/HFX.pdf) | NOAA | 应急响应数据 | 是 | ⚠️ 仅外部引用 | 未收录进证据包，仅作外部核对 |
| [CDC/NIOSH Criteria Document (1976)](https://stacks.cdc.gov/view/cdc/19358/cdc_19358_DS1.pdf) | NIOSH (US CDC) | 历史标准文档 | 是 | ⚠️ 仅外部引用 | 附录 MSDS 为历史格式，不能当现行 SDS |
| [New Jersey RTK Fact Sheet — Hydrogen Fluoride](https://nj.gov/health/eoh/rtkweb/documents/fs/3759.pdf) | NJ DOH | 州政府资料 | 是 | ⚠️ 仅外部引用 | 州政府作品版权状态需单独核验 |
| [Cal/OSHA HF Fact Sheet](http://www.dir.ca.gov/dosh/dosh_publications/Hydrogen-Flouride-fs.pdf) | Cal/OSHA | 州政府资料 | 是 | ⚠️ 仅外部引用 | 同上 |
| Airgas / Honeywell 厂商 SDS | 厂商 | 厂商 SDS | 是 | ⛔ 禁止入仓 | 版权约束，仅可由人工在仓库外核对 |

## 3. 证据条目字段（`hf_evidence_pack.json` → `items[]`）

| 字段 | 含义 |
| --- | --- |
| `evidence_id` | 证据唯一编号 |
| `source_title` | 来源文档标题 |
| `source_url` | 来源链接 |
| `organization` | 发布机构 |
| `section` | 来源页面中的章节/区块 |
| `topic` | 主题标签（`hazard_identification` / `ppe` / `first_aid` / `spill_response` 等） |
| `passage` | **原样引用**的支持性片段（不得改写、不得由模型补全） |
| `locator` | 在来源页面中的定位说明 |
| `retrieved_at` | 抓取日期（ISO） |
| `evidence_type` | 证据类型 |
| `usage_note` | 使用限制说明 |

## 4. 手动补充合法 HF SDS 的兼容方案

当前 RAG 流程依赖 PDF。**不伪造 PDF**，提供两条兼容路径：

### 路径 A：应用内上传（立即可用，无需改代码）

1. 通过合法途径获得 HF SDS PDF（自有生成 / 权利人许可 / 已核验公共领域）；
2. 在应用侧边栏上传该 PDF，点击「构建 / 更新知识库」；
3. 现有 `rag.py` 流程立即生效，HF 相关提问可通过化学品覆盖校验并返回文件名 / 页码 / 原文证据；
4. 注意：此路径下 SDS 仅存在于当前会话，刷新即失效。

### 路径 B：登记为案例资产（供 P3 工作流自动使用）

1. 将 PDF 保存为 `data/demo_sds/<文件名>.pdf`；
2. 在 `hf_sds_source_registry.json` 的 `registered_demo_sds` 填写 `file_name`、`source_id`、`added_on`、`legal_confirmation`；
3. 将 `status` 置为 `ready`，并在 `.gitignore` 中为该文件增加白名单（仅当许可明确允许公开再分发时）；
4. `require_hf_sds_source()` 将返回该 PDF 路径，P3 才能从中提取 SDS 证据并生成 JSA 草稿。

## 5. 当前缺口（Gaps）

- SDS 第 3 节 成分/组成信息（浓度、杂质）；
- SDS 第 7 节 操作处置与储存的完整条款；
- SDS 第 9 节 完整理化特性；
- SDS 第 13 节 废弃处置与本地法规要求；
- 中文版 SDS 与国内法规符合性；
- 以上缺口在 `hf_evidence_pack.json` 的 `gaps` 字段中同步登记。

## 6. 维护规则

- `passage` 必须原样引用；新增/修改证据须同步更新 `retrieved_at` 并运行测试；
- 测试会校验必填字段、来源机构白名单、URL 白名单、`retrieved_at` 为 ISO 日期；
- 模拟业务数据与真实公开证据在数据结构与展示上必须分开，测试会锁定该边界。
