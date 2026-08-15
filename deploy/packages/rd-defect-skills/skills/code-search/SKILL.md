---
name: code-search
description: code-search Skill - code-search
---
# code-search

## 类型
AgentTeams Skill — with-scripts（核心脚本：`scripts/context_packer.py`）

## 角色
Analyzer (主用) / Manager (备用)

## 功能
语义+关键词搜索+结果打包。通过 MCP 原语 `hybrid_search` 实现三库融合检索，并将结果打包为结构化上下文。

## 使用场景
- Analyzer 根因分析时语义锚定
- Manager 调度初期对 Issue 做初步匹配

## 输入参数
| 参数 | 必填 | 说明 |
|------|------|------|
| `query_text` | 是 | 自然语言查询 |
| `top_k` | 否 | 默认 10 |
| `repo_filter` | 否 | 仓库过滤 |
| `strategy` | 否 | semantic / keyword / hybrid，默认 hybrid |

## 输出结果
结构化 Top-K 结果：`file_path` / `line_range` / `score` / `snippet` / `context_window`，外加 Neo4j 图谱扩展结果。

## 调用条件
Analyzer 进入 `analyzing` 阶段时触发。

## 依赖 MCP 原语
`hybrid_search`（共享服务，RRF 融合）/ `pgvector_search` / `meili_keyword_search` / `neo4j_expand_chunks` / `embed_texts`

## 执行方式
脚本通过 `hybrid_search` MCP 原语获取融合结果，`context_packer.py` 打包为带上下文的结构化输出。

## 检索流程（三库融合）
```
pgvector_search (向量召回) + meili_keyword_search (关键词召回)
    → RRF 融合
    → neo4j_expand_chunks (图谱扩展)
    → context_packer (打包结构化上下文)
```

## 失败处理
- 召回为空 → 降级为纯关键词搜索
- RRF 融合超时 → 返回单路 Top-K
- Neo4j 扩展失败 → 仅返回融合结果，标记"无调用链上下文"

## 复用价值
**高**。Analyzer / Fixer / Manager 均可调用；可作为通用"代码语义搜索"服务。
