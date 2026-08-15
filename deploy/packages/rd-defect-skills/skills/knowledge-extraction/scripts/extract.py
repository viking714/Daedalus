"""knowledge-extraction 核心脚本 — 经验抽取与沉淀。

从 skills.py 的 knowledge_miner（已重命名）迁移而来。
Evaluator 裁定完成后，从修复结果中抽取可复用模式与标签。

完整实现（对接 lessons 表 + 去重策略）计划在第二步完成。
当前为轻量实现（标签抽取 + 模式识别）。
"""

import re
import uuid
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(_here))))
if _root not in sys.path:
    sys.path.insert(0, _root)


def _rand(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def extract_knowledge(root_cause: str = "", fix_diff: str = "",
                      test_report: dict = None, verdict: dict = None,
                      task_id: str = "", repo: str = "",
                      retry_count: int = 0) -> dict:
    """从修复结果中抽取可复用经验。

    Args:
        root_cause: 根因描述文本
        fix_diff: 修复 diff 文本
        test_report: 测试报告
        verdict: 评估裁定
        task_id: 任务 ID
        repo: 仓库名
        retry_count: 重试次数

    Returns:
        {knowledge_id, pattern, tags, source_decision, mode}
    """
    root_cause = root_cause or ""
    final_decision = (verdict or {}).get("decision", "")
    success = final_decision == "pass"

    # 标签抽取
    stop_words = {
        "the", "and", "for", "with", "that", "this", "from",
        "into", "when", "error", "null",
    }
    keywords = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", root_cause)
    tags = sorted({k.lower() for k in keywords if k.lower() not in stop_words})[:10]

    result = {
        "knowledge_id": _rand("KM"),
        "pattern": (root_cause or "unknown")[:200],
        "tags": tags,
        "source_decision": final_decision,
        "success": success,
        "mode": "tag_extraction_only",
    }

    # TODO: 完整实现（第二步）——
    # 1. 对接 lessons 表（设计 §5.2）
    # 2. 字段抽取：fix_pattern / error_signature / affected_modules / edge_cases
    # 3. 写入前去重：pgvector cosine similarity → MERGE/SIMILAR/NEW
    # 4. 合并规则：取并集、保留更详细、merge_count+1
    result["_note"] = (
        "lightweight mode: tag extraction only. "
        "Full implementation (lessons table + dedup + merge) planned in step 2."
    )

    return result
