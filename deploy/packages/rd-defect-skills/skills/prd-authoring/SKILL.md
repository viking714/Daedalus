---
name: prd-authoring
version: 0.2.0
description: PO 主用：PRD 生成规范、DoR 检查表、Gate0 需求澄清协议。
type: prompt-only
roles: [product-owner]
---

# prd-authoring

## 用途

本技能规范 PO 在 feature / greenfield 任务中的产出：

1. 使用 DoR（Definition of Ready）检查表判断需求是否就绪。
2. 在需求不明时进入 Gate0 澄清循环，生成结构化问题清单。
3. 产出 `prd.json` 并写入 MinIO，作为下游 Architect/Developer/Tester/Reviewer 的机读契约。

## Gate0 DoR 检查表

每项必须回答「是」才进入 PRD 起草：

- [ ] 用户角色与目标场景明确
- [ ] 功能需求可被独立测试
- [ ] 每条验收标准使用 Given-When-Then 格式
- [ ] 歧义点已列出合理解释且用户已选择
- [ ] 边界条件（必须覆盖/可跳过）清晰
- [ ] 非功能需求（性能/安全/兼容性）已声明
- [ ] 视觉预期已询问（页面结构/空态/错误态）

## 澄清协议

1. 读取 task envelope 与 requirement。
2. 跑 DoR 检查表。
3. 若未通过，生成 `clarification.json`：
   - `questions`: 歧义点 + 为何阻塞 + 建议选项
   - `channel`: Jira comment / 聊天室
   - `round_num`: 当前轮次（上限 3）
4. 等待用户回复，重复直到 DoR 通过或超限转 TL 仲裁。

## PRD 产出要求

- 严格遵循 `prd.schema.json`。
- `visual_acceptance` 在前端相关任务中必填。
- 不自造需求、不默认假设；判断权始终在用户。
- 将 `prd.json` 推送到 MinIO `tasks/{task_id}/prd.json`。

## 输出示例

```json
{
  "task_id": "FEAT-0001",
  "task_type": "feature",
  "intent": "...",
  "functional_requirements": [...],
  "acceptance_criteria": [
    {"id": "AC-1", "given": "...", "when": "...", "then": "..."}
  ],
  "visual_acceptance": [
    {"id": "VA-1", "check": "..."}
  ]
}
```
