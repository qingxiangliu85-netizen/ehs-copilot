# Demo SDS

公开仓库不提供真实厂商 SDS 文件。目录中的 `EHS_Copilot_Demo_Synthetic_SDS.pdf` 由本项目自行生成，仅用于演示软件流程；对应文本位于 `EHS_Copilot_Demo_Synthetic_SDS_SOURCE.md`。PDF 每页均标注：

> Synthetic SDS for demonstration only – not for real-world safety decisions.

自动化测试还会在运行时生成其他模拟 PDF，并在测试结束后由临时目录自动清理。

如需录制演示或补充截图，只能在本地使用以下材料：

- 自行制作并标注为模拟数据的 SDS；
- 权利人明确允许公开再分发的 SDS；
- 许可证和来源已经核验的公共领域材料。

请勿提交供应商版权状态不明确的 SDS，也不要提交包含实验室人员、企业内部信息、个人隐私或受限资料的文件。本目录下其他 PDF 均由 `.gitignore` 默认排除，只有上述自行生成的 Demo PDF 允许提交。

## HF 案例来源状态（V4 P2）

`hf_sds_source_registry.json` 登记 HF 酸洗 Demo 案例的 SDS 来源状态与候选公开资料。当前状态为 `pending_real_source`：

- 尚未引入可合法再分发、且可核验的 HF SDS；
- 在来源补齐前，`demo_cases.py` 不生成任何 HF 危险性、PPE、急救、泄漏或消防结论；
- NIOSH / OSHA / NOAA 等政府公开资料仅可作交叉核对参考，不能替代 SDS，详见登记表；
- 已建立的公开来源证据包见 `data/demo_evidence/`（逐字引用 + 来源链接，标注为「非 SDS」）；
- 补齐来源后，需同步更新 `registered_demo_sds`、`.gitignore` 白名单与对应测试。

> **P1 更新（2026-09-14）**：Safety Review Pack 的内置资料库见 `data/demo_documents/`
> （真实 Methanol SDS + 未核验 HF 镜像 + 两份 Synthetic Demo SOP，来源与核验状态详见
> `data/demo_documents/README.md`）。本目录的 Synthetic SDS 知识库仍是可选回退路径，
> 演示金路径应优先使用 `data/demo_documents/` 中的真实 SDS。
