---
name: repair-planning
description: repair-planning Skill - repair-planning
---
# repair-planning

## 类型
AgentTeams Skill — with-scripts（核心脚本：`scripts/repair_plan.py` / `scripts/risk_gate.py`）

## 角色
Fixer (主用)

## 功能
修复方案规划与风险闸门。Fixer 在编辑前制定具体方案，并通过风险闸门决定是否执行。
**先识别输入类型**：根因报告（Bug 修复）→ 精准修复方案；需求规格（Feature Request）→ 稳健实现方案（覆盖多解释）。

## 使用场景
Fixer 进入 `fixing` 状态、收到 Analyzer 产出（根因报告 或 需求规格）后、实际编辑之前。

## 输入参数
| 参数 | 必填 | 说明 |
|------|------|------|
| `root_cause` | 否 | 根因分析结果（Bug 修复场景） |
| `requirement_spec` | 否 | 需求规格（Feature Request 场景，含 intent + ambiguities） |
| `impact` | 否 | 影响面分析结果 |
| `max_files_per_round` | 否 | 单轮文件上限，默认 `MAX_FILES=5` |
| `risk_level` | 否 | 风险等级 |
| `touches` | 否 | 涉及的敏感模块列表 |
| `approval_required` | 否 | 是否需要审批 |

## 输出结果
- **Bug 修复**：修复方案（`file_edits` 列表）+ 风险等级 + 闸门决策（pass / warn / block）。
- **新功能需求**：稳健实现方案（`file_edits` 列表，每项标注「锚定的本质意图」和「覆盖的解释」）+ 风险等级 + 闸门决策。

## 关键原则（新功能需求）
- **锚定本质意图，而非示例**：示例的列名/格式不是需求，底层能力才是
- **覆盖多种解释**：需求有歧义时，实现能覆盖所有合理解释的稳健版本

## 调用条件
Fixer 处于 `fixing` 状态、Analyzer 已输出根因报告、Fixer 未做实际编辑之前。

## 依赖 MCP 原语
`neo4j_impact_stats`（再确认影响面）

## 执行方式
- `repair_plan.py`：基于根因和影响面生成修复步骤计划
- `risk_gate.py`：默认拒绝原则，敏感模块/高危需人工审批

## 闭环阈值
- `MAX_FILES = 5`：单轮修改文件数上限
- 涉及敏感模块（auth / payment / db_schema / security / crypto）+ 高风险 → block

## 失败处理
- 风险等级 ≥ L3 → block 并 escalate
- 方案涉及 >5 文件 → 拆分多轮
- `risk_gate` 与 `impact-analysis` 结论矛盾 → 保守侧生效

## 复用价值
**中**。主要 Fixer 使用；可推广到"代码变更方案设计"通用场景。
