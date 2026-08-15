---
name: multi-file-edit
description: multi-file-edit Skill - multi-file-edit
---
# multi-file-edit

## 类型
AgentTeams Skill — prompt-only

## 角色
Fixer (主用)

## 功能
多文件协调编辑。按 `repair-planning` 输出的方案顺序编辑。

## 使用场景
Fixer 单轮需修改多文件时。

## 输入参数
| 参数 | 必填 | 说明 |
|------|------|------|
| `edit_plan` | 是 | 文件路径 + 目标修改的列表 |
| `atomic` | 否 | 是否原子化，默认 true |

## 输出结果
各文件编辑结果（成功/失败/行号变化）、整体一致性报告。

## 调用条件
Fixer 处于 `fixing` 状态、`repair-planning` 已输出方案、文件数 ≤ 5（`MAX_FILES`）。

## 依赖工具/系统
Worker 原生工具 `edit_file`、`git`。

## 执行方式
由 Worker runtime 直接提供，不经过 MCP Server。

## 失败处理
- 单文件失败 → `atomic=true` 时回滚所有文件
- 记录失败文件供下一轮 `repair-planning` 调整方案

## 权限与安全
- 沙箱路径限制
- 禁止修改 `.git/` / `.env` / `secrets/`
- 编辑前自动备份（`*.bak`）

## 复用价值
**中**。主要 Fixer 使用；任何"批量修改代码"场景可复用。
