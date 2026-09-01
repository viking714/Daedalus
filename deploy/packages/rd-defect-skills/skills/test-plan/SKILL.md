---
name: test-plan
version: 0.2.0
description: Tester 主用：从 PRD 验收标准独立派生 test_plan.json。
type: prompt-only
roles: [tester]
---

# test-plan

## 用途

本技能规范 Tester 从 PRD 独立派生测试计划，避免自我验证陷阱。

## 执行步骤

1. 读取 `tasks/{task_id}/prd.json`。
2. 对每条 `acceptance_criteria` 生成至少一个测试用例。
3. 对每条 `visual_acceptance` 生成视觉回归用例。
4. 补充边界与回归用例。
5. 输出 `test_plan.json` 到 MinIO。

## 用例类型

- `functional`: 功能验收用例
- `visual`: 视觉回归用例
- `regression`: 全量回归用例
- `boundary`: 边界条件用例

## 输出要求

- 严格遵循 `test_plan.schema.json`。
- 测试计划必须在开发完成后、提交前执行。
- 视觉回归用例需关联基线快照。
