---
name: patch-generate
description: patch-generate Skill - patch-generate
---
# patch-generate

## 类型
AgentTeams Skill — prompt-only

## 角色
Developer (主用)

## 功能
生成统一格式 diff。

## 使用场景
Developer 完成多文件编辑后，汇总产出 `fix.diff`。

## 输入参数
| 参数 | 必填 | 说明 |
|------|------|------|
| `repo_path` | 是 | 仓库根路径 |
| `base_commit` | 否 | 基准 commit SHA |
| `target_files` | 否 | 修改文件列表，默认全部 |

## 输出结果
unified diff 格式字符串（`--- a/...` / `+++ b/...` 头），写入 `fix.diff` 文件。

## 调用条件
Developer 完成所有编辑后、状态推进到 `testing` 之前调用。

## 依赖工具/系统
Worker 原生工具 `git diff`。

## 执行方式
由 Worker runtime (OpenClaw) 直接提供，通过 bash-exec 执行 `git diff`。

## 失败处理
- 无 git 仓库 → 返回错误
- 非 UTF-8 文件 → 提示二进制文件单独处理
- 编码异常 → 回退到文件级 diff

## 权限与安全
- 只读访问 git 历史
- 不修改任何文件
- 不暴露敏感信息（如密钥、token）

## 复用价值
**中**。主要 Developer 使用；任何需要产出"代码变更"产物的场景可复用。
