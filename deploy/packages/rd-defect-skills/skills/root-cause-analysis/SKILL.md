---
name: root-cause-analysis
description: root-cause-analysis Skill - root-cause-analysis
---
# root-cause-analysis

## 类型
AgentTeams Skill — with-scripts（核心脚本：`scripts/root_cause.py`）

## 角色
Analyzer (主用)

## 功能
分析阶段的核心能力。**先判断任务类型**，再分流：
- **Bug 修复** → 根因推断与复现脚本生成（整合 `code-search` / `impact-analysis` / `lesson-lookup` 多源信息）
- **新功能需求（Feature Request）** → 需求规格化（提取本质意图 / 功能需求 / 歧义点 / 边界条件 / 验收口径 / 约束）

## 使用场景
Analyzer 在 `analyzing` 阶段整合多源信息后。

## 输入参数
| 参数 | 必填 | 说明 |
|------|------|------|
| `issue_text` | 否 | Issue 描述（据此先判断任务类型） |
| `context_pack` | 否 | 来自 code-search 的上下文 |
| `suspect_symbol` | 否 | 嫌疑符号名（Bug 修复场景） |
| `ns` | 否 | 命名空间 |

## 输出结果
- **Bug 修复**：`analysis_report.json`（结构化根因 + 修复策略）+ `root_cause_report.md`（人类可读）+ 可选复现脚本。
- **新功能需求**：需求规格 JSON（`task_type="feature_request"`），字段：
  ```
  {
    "task_type": "feature_request",
    "intent": "本质意图（一句话）",
    "functional_requirements": ["功能点..."],
    "ambiguities": [{"point": "歧义点", "interpretations": ["解释A", "解释B"]}],
    "boundary_conditions": {"must_cover": ["..."], "can_skip": ["..."]},
    "acceptance_criteria": ["验收标准..."],
    "constraints": ["约束..."]
  }
  ```

## 关键原则（新功能需求）
- **示例 ≠ 需求**：issue 示例里的确切列名/格式不是需求本身，需求是底层能力
  （如「展示 route 归属」，而非「显示 Domain 列」）
- **歧义显式化**：需求模糊时列出所有合理解释，让 Fixer 实现覆盖多解释的稳健版本

## 调用条件
Analyzer 完成 `code-search` / `impact-analysis` / `lesson-lookup` 三个前置 Skill 后调用。

## 依赖 MCP 原语
`neo4j_dep_subgraph`（获取依赖子图，辅助根因推断）

## 执行方式
`root_cause.py` 整合上下文，结合 Neo4j 依赖子图做启发式分析；Neo4j 不可用时降级为纯启发式。

## 失败处理
- 根因不明确 → 输出 2~3 个候选根因 + 置信度
- 复现脚本生成失败 → 仅输出分析报告
- 置信度 < 0.5 → escalate

## 复用价值
**中**。主要 Analyzer 使用；可作为"故障根因分析"通用能力。
