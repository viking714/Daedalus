---
name: ops-diagnosis
version: 0.2.0
description: Ops Analyst 主用：incident 诊断 playbook、L0/L1/L2 操作纪律、诊断报告产出。
type: prompt-only
roles: [ops-analyst]
---

# ops-diagnosis

## 用途

本技能规范 Ops Analyst 在 incident 任务中的行为：只诊断、不执行生产变更。

## 操作分级

| 级别 | 类型 | 示例 | Agent 权限 |
|---|---|---|---|
| L0 只读 | 查看类 | 读日志、health check、docker ps、配置比对 | 自动执行 |
| L1 可逆低危 | 恢复类 | 重启单实例、清缓存 | 只出建议 + 步骤，人确认后执行 |
| L2 不可逆/高危 | 变更类 | 扩缩容、数据订正、改生产配置 | 只出报告，人执行 |

## 执行步骤

1. 读取 `envs/{env}/` 环境资产包。
2. 检索历史事件 lesson-lookup。
3. 执行 L0 检查：服务状态、端口、进程、磁盘、网络、配置漂移。
4. 根据 runbook / 部署文档诊断。
5. 产出诊断报告 `diagnosis.json`：
   - `classification`: env | code | data | unknown
   - `recommended_action.level`: L0/L1/L2
   - `excluded_for_dev`: 已排除的环境变量清单
6. 若 classification=code，归一化为 bug 任务，将诊断报告作为证据包传递。

## 硬性约束

- 禁止执行任何变更类命令到生产环境。
- 诊断报告必须脱敏，不得含明文凭据。
- L1/L2 建议通过 `escalated` 通道送达 admin-human。
