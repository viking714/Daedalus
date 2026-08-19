---
name: result-judge
description: result-judge Skill - result-judge
---
# result-judge

## 类型
AgentTeams Skill — prompt-only

## 角色
Tester (主用) / Evaluator (主用)

## 功能
基于结果裁定通过/驳回。Tester 反馈失败时识别"是否值得重试"、Evaluator 四维度审查。

> **v0.1.1 变更**：此 Skill 已从 MCP 工具转为 prompt-only。判定逻辑由 Worker 根据本 SKILL.md 中的指令自行完成，不再调用 MCP Server。

## 裁定规则

### 基础判定（Tester 使用）
基于测试结果和当前轮次：
- `test_result.passed == true` → decision: `success`
- `current_round >= max_round` → decision: `handoff`（人工介入）
- 否则 → decision: `retry`（进入下一轮修复）

### 四维度审查（Evaluator 使用，Bug 修复场景）
| 维度 | 说明 |
|------|------|
| 正确性 | 修复是否解决了 Issue 描述的问题 |
| 完整性 | 是否覆盖了所有受影响模块和边界情况 |
| 一致性 | 代码风格、API 契约是否与项目一致 |
| 质量 | 是否引入新问题、是否有潜在风险 |

### 独立验收（Evaluator 使用，新功能需求场景）
对照 Analyzer 的需求规格（intent / acceptance_criteria / ambiguities）独立裁决：
- 判断「实现是否真的满足本质意图」，而非"看起来合理"，也非"是否匹配 issue 示例"
- 警惕「看起来对但没抓住重点」：产出示例格式但没交付底层能力 → FAIL
- 需求有歧义时，实现应覆盖多种解释；只覆盖一种窄假设 → 标注风险

## 阈值配置
- `MAX_ROUND = 3`：超过则 handoff

## 调用条件
- Tester 完成一轮测试后
- Evaluator 进入 `evaluating` 阶段时

## 依赖工具/系统
无外部依赖（纯 LLM 推理 + Worker 原生工具）。

## 执行方式
由 Worker runtime 根据本 SKILL.md 指令直接执行，不经过 MCP Server。

## 失败处理
- 四维度评分内部矛盾 → escalate
- 评分置信度 < 0.6 → escalate

## 权限与安全
仅生成判定，不修改任何数据；不读取敏感文件。

## 复用价值
**高**。Tester/Evaluator 共用，可推广到任何"质量评估"环节。
