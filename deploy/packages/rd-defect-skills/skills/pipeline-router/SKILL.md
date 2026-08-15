---
name: pipeline-router
description: pipeline-router Skill - pipeline-router
---
# pipeline-router

## 类型
AgentTeams Skill — with-scripts（核心脚本：`scripts/task_router.py` / `scripts/state_manager.py` / `scripts/handoff.py` / `scripts/loop_judge.py`）

## 角色
Manager (主用)

## 功能
任务路由、状态推进、人工交接。Manager 在每个阶段过渡时调用，决定下游 Agent 与状态切换。

## 使用场景
Manager 每次收到 Worker 产出后、推进状态机前调用。

## 输入参数
| 参数 | 必填 | 说明 |
|------|------|------|
| `task_id` | 是 | 任务标识 |
| `current_state` | 是 | 当前 TaskState |
| `current_stage` | 否 | 当前阶段（analyzing/fixing/testing/evaluating） |
| `round` | 否 | 当前轮次 |
| `from_stage` | 否 | 来源阶段 |
| `to_stage` | 否 | 目标阶段 |
| `owner_agent` | 否 | 负责 Agent |

## 输出结果
派单指令（`next_agent` + `next_stage`）+ 状态推进结果 + 人工交接消息（可选）。

## 调用条件
Manager 每次收到 Worker 产出后调用。

## 依赖 MCP 原语
`redis_set_repo_state`（状态快照）

## 状态机
```
analyzing → fixing → testing → evaluating → resolved / escalated
```

## 闭环阈值
- `MAX_ROUND = 3`：超过则 handoff
- `MAX_FILES = 5`：单轮修改文件数上限
- `TOKEN_BUDGET = 100000`：超预算触发二次压缩
- `TASK_TIMEOUT_MIN = 30`：超时转 handoff

## 执行方式
- `task_router.py`：路由决策（流水线推进 / 达阈值转 handoff）
- `state_manager.py`：状态机管理（版本号 + 阶段一致性校验 + 闭环阈值闸门）
- `handoff.py`：人工交接包生成
- `loop_judge.py`：循环判定（检测重复失败时触发人工交接）

## 失败处理
- 状态机冲突 → 回滚到上一稳定态
- Matrix 通信失败 → 重试 3 次后 escalate
- `regression_cycle_count >= 3` → 强制 `escalated`

## 复用价值
**中**。仅 Manager 使用；可作为"多阶段任务调度"通用模式。
