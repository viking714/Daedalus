---
name: env-inventory
version: 0.2.0
description: Ops Analyst 主用：环境资产包读取约定，runbook 索引，脱敏访问规则。
type: prompt-only
roles: [ops-analyst]
---

# env-inventory

## 用途

本技能约定环境资产包的存储结构与读取方式，解决「部署脚本与实际配置不入代码库」的可达性问题。

## MinIO 路径

```
envs/{env}/
  ├── env.yaml          # 拓扑清单
  ├── runbooks/         # 已知故障模式诊断路径
  ├── deploy/           # 实际部署脚本与生效配置（脱敏版）
  └── baseline/         # 配置基线快照
```

## env.yaml 字段

- `services`: 服务列表
- `dependencies`: 依赖关系
- `logs`: 日志位置
- `monitoring`: 监控面板入口
- `health_checks`: 健康检查端点

## 访问规则

- 仅 `ops-analyst` 角色可读取 `envs/` 路径。
- 资产包内配置为脱敏版，凭据替换为占位符。
- 原始配置中若含真实凭据，读取后执行 redaction 检查。

## 输出

将读取到的 `env.yaml` 和 `runbooks/` 索引合并为 `env_inventory.json`，供 ops-diagnosis 使用。
