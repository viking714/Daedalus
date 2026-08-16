"""knowledge-extraction 核心脚本 — 经验抽取与沉淀。

对齐方案设计 §5.3「写入流程」：
Evaluator 裁定完成后，从各阶段产物抽取结构化字段，向量化 root_cause 后
调用 LessonsStore.upsert_with_dedup 完成写入前去重 + 合并（MERGE/SIMILAR/NEW）。

字段来源（设计 §5.3 输入来源表）：
- root_cause / fix_strategy      ← root_cause_report（根因段落文本）
- fix_pattern / affected_modules / diff_summary ← fix_diff（文件路径 + 变更摘要）
- error_signature / test_changes ← test_report（异常类型 + 测试用例）
- edge_cases / success           ← verdict（驳回原因 + 裁定结果）
- task_id / repo / retry_count   ← TaskState（任务元数据）

依赖：mcp_server/db/lessons.py（LessonsStore）+ mcp_server/embed（EmbeddingService）。
lessons 表不可用时返回 unavailable 并携带已抽取字段，不抛异常。
"""

import re
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(_here))))
if _root not in sys.path:
    sys.path.insert(0, _root)

from mcp_server.db.base import DbUnavailable
from mcp_server.db.lessons import LessonsStore
from mcp_server.embed.embeddings import EmbeddingService

_STOP_WORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "when",
    "error", "null", "not", "are", "was", "has", "have", "will", "should",
    "case", "test", "file", "line",
}


def extract_knowledge(root_cause: str = "", fix_diff: str = "",
                      test_report: dict = None, verdict: dict = None,
                      task_id: str = "", repo: str = "",
                      retry_count: int = 0, root_cause_report: str = "",
                      fix_strategy: str = "") -> dict:
    """从修复结果抽取经验并写入 lessons 表（带去重合并）。

    Args:
        root_cause: 根因描述文本（优先）；为空时回退 root_cause_report
        fix_diff: 修复 diff 文本
        test_report: 测试报告 dict（含 error_type / failed_cases 等）
        verdict: 评估裁定 dict（含 decision / reasons 等）
        task_id: 任务 ID
        repo: 仓库名
        retry_count: 重试次数
        root_cause_report: 根因报告全文（root_cause 为空时的备选）
        fix_strategy: 修复策略大类（可选）

    Returns:
        {
            status, decision, lesson_id, knowledge_id, related_to, score,
            tags, success, mode, lesson
        }
    """
    test_report = test_report or {}
    verdict = verdict or {}

    # —— 字段抽取 ——
    root_cause = (root_cause or root_cause_report or "").strip()
    if not root_cause:
        return {"status": "error", "reason": "root_cause required (cannot vectorize empty root cause)"}

    decision = str(verdict.get("decision", "")).lower()
    success = decision == "pass"
    edge_cases = _as_list(
        verdict.get("reasons") or verdict.get("reject_reasons") or verdict.get("edge_cases")
    )
    resolution_summary = (
        verdict.get("summary") or verdict.get("resolution_summary") or ""
    )

    error_signature = (
        test_report.get("error_type") or test_report.get("failure_type") or ""
    )
    test_changes = _format_test_changes(test_report)

    affected_modules, fix_pattern, diff_summary = _parse_diff(fix_diff)
    tags = _extract_tags(root_cause)

    lesson = {
        "repo": repo or "",
        "task_id": task_id or "",
        "root_cause": root_cause,
        "fix_pattern": fix_pattern,
        "error_signature": error_signature,
        "fix_strategy": fix_strategy or verdict.get("fix_strategy") or "",
        "affected_modules": affected_modules,
        "tags": tags,
        "diff_summary": diff_summary,
        "test_changes": test_changes,
        "edge_cases": edge_cases,
        "success": success,
        "resolution_summary": resolution_summary,
        "retry_count": int(retry_count or 0),
        "merge_count": 1,
    }

    # —— 向量化 + 写入前去重合并 ——
    try:
        emb = EmbeddingService()
        vec = emb.embed([root_cause])[0]
        store = LessonsStore()
        result = store.upsert_with_dedup(lesson, vec)
    except DbUnavailable as e:
        return {"status": "unavailable", "reason": str(e), "lesson": lesson, "mode": "full"}

    return {
        "status": "ok",
        "decision": result["decision"],
        "lesson_id": result["lesson_id"],
        "knowledge_id": result["lesson_id"],  # 兼容旧字段名
        "related_to": result["related_to"],
        "score": result["score"],
        "tags": tags,
        "success": success,
        "mode": "full",
        "lesson": lesson,
    }


# --------------------------------------------------------------------------- #
# 内部：字段抽取辅助
# --------------------------------------------------------------------------- #


def _as_list(x):
    """将各类输入归一化为 list（处理 None / 单值 / list 三种形态）。"""
    if x is None:
        return []
    if isinstance(x, (list, tuple)):
        return list(x)
    if isinstance(x, str):
        return [x] if x.strip() else []
    return [x]


def _parse_diff(fix_diff: str):
    """从 unified diff 提取 affected_modules / fix_pattern / diff_summary。

    返回 (affected_modules, fix_pattern, diff_summary) 三元组。
    """
    fix_diff = fix_diff or ""
    # 提取 +++ b/path 形式（无 /dev/null 的占位）
    files = re.findall(r"^\+\+\+ b/(.+)$", fix_diff, re.MULTILINE)
    if not files:
        files = re.findall(r"^diff --git a/(.+?) b/", fix_diff, re.MULTILINE)
    files = [f.strip() for f in files if f.strip() and f.strip() != "/dev/null"]

    affected_modules = []
    for f in files:
        mod = f.split("/")[0] if "/" in f else ""
        if mod and mod not in affected_modules:
            affected_modules.append(mod)

    added = len(re.findall(r"^\+(?!\+\+)", fix_diff, re.MULTILINE))
    removed = len(re.findall(r"^-(?!--)", fix_diff, re.MULTILINE))

    if files:
        summary = f"修改 {len(files)} 个文件（+{added}/-{removed}）：" + ", ".join(files[:5])
        if len(files) > 5:
            summary += f" 等 {len(files)} 个文件"
    else:
        summary = f"diff 无文件路径（+{added}/-{removed}）"

    return affected_modules, summary, summary


def _extract_tags(text: str) -> list:
    """从根因文本提取英文标识符关键词（停用词过滤）。"""
    keywords = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text or "")
    return sorted({k.lower() for k in keywords if k.lower() not in _STOP_WORDS})[:10]


def _format_test_changes(test_report: dict) -> str:
    """从测试报告提取测试变更摘要（失败用例名 + 通过数）。"""
    parts = []
    failed = test_report.get("failed_cases") or test_report.get("failed") or []
    if failed:
        names = [
            c.get("name", str(c)) if isinstance(c, dict) else str(c)
            for c in list(failed)[:5]
        ]
        parts.append("失败用例: " + ", ".join(names))
    passed = test_report.get("passed_count")
    if passed is None:
        passed = test_report.get("passed")
    if passed is not None:
        parts.append(f"通过: {passed}")
    return "; ".join(parts) or ""
