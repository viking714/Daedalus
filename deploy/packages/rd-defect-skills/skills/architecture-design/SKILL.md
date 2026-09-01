---
name: architecture-design
version: 0.2.0
description: Architect 主用（feature 流程）：ADD 生成规范、docs 优先阅读、视觉基线提取。
type: prompt-only
roles: [architect]
---

# architecture-design

## 用途

本技能规范 Architect 在 feature / greenfield 任务中的产出：

1. 优先阅读 repo `docs/` 既有设计文档，无文档则从源码逆向。
2. 产出 `add.json` 并写入 MinIO。
3. 前端任务提取视觉基线，写入 `ui_spec.baseline_sources`。

## 设计输入

- `tasks/{task_id}/prd.json`
- `docs/` 目录下的设计文档
- 仓库源码（用于逆向和验证）

## 执行步骤

1. 读取 PRD，理解意图、验收标准、视觉预期。
2. 列出 `docs/` 文档，按相关性排序阅读。
3. 记录 `docs_code_divergence`：文档与代码不一致处及采信结论。
4. 制定 `tech_stack`，每个选型记录 `rationale` 和 `alternatives_rejected`。
5. 显式命名 `design_patterns` 及解决的问题。
6. 列出 `dependencies_rationale`，按 mainstream/niche/self-built 分类并记录风险。
7. 前端任务：使用 `visual-check` 提取存量页面 DOM 结构快照，写入 `ui_spec.baseline_sources`。
8. 输出 `add.json` 到 MinIO。

## ADD 产出要求

- 严格遵循 `add.schema.json`。
- `design_patterns` 和 `dependencies_rationale` 必填（Reviewer 会核验）。
- `ui_spec` 在前端任务中必填，必须引用成熟设计系统（Ant Design / Material Design 3），禁止自创设计系统。
- `change_plan` 中标注每个文件的 new/modify/delete 和风险等级。

## 输出示例

```json
{
  "task_id": "FEAT-0001",
  "design_inputs": {...},
  "tech_stack": {...},
  "design_patterns": [...],
  "dependencies_rationale": [...],
  "ui_spec": {...},
  "module_design": [...],
  "change_plan": [...],
  "rollout_risks": [...]
}
```
