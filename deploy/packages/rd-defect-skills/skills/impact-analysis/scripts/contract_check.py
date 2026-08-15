"""impact-analysis 核心脚本 — 契约检查。

从 skills.py 提取，检测补丁是否改动既有接口/函数签名。
"""

import re
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(_here))))
if _root not in sys.path:
    sys.path.insert(0, _root)

_SIGNATURE_RE = re.compile(
    r"^\s*(def |public |private |protected |function |=>|\w+\s*\([^)]*\)\s*\{?)\s*\w+"
)


def check_contract(patch_text: str = None, changed_files: list = None) -> dict:
    """契约核查：检测补丁是否改动既有接口/函数签名。

    Args:
        patch_text: diff 文本
        changed_files: 修改文件列表

    Returns:
        {contract_safe: bool, violations: [str], checked_files: [str]}
    """
    changed_files = changed_files or []
    violations = []

    if patch_text:
        # 使用本地解析避免循环依赖
        parsed = _parse_patch(patch_text)
        for f, added, removed in parsed:
            for line in added + removed:
                if _SIGNATURE_RE.match(line):
                    violations.append(
                        f"{f}: signature line changed -> {line.strip()[:80]}"
                    )
    else:
        for f in changed_files:
            violations.append(
                f"{f}: changed without diff; assume potential contract impact"
            )

    return {
        "contract_safe": len(violations) == 0,
        "violations": violations,
        "checked_files": changed_files or [f for f, _, _ in _parse_patch(patch_text)] if patch_text else [],
    }


def _parse_patch(patch_text: str) -> list:
    """解析 unified diff，返回 [(file, added_lines, removed_lines)]."""
    try:
        from unidiff import PatchSet
    except ImportError:
        return _parse_manual(patch_text)
    ps = PatchSet.from_string(patch_text or "")
    return [(p.path,
             [l.value for h in p.hunks for l in h.target_lines()],
             [l.value for h in p.hunks for l in h.source_lines()]) for p in ps]


def _parse_manual(patch_text: str) -> list:
    files = []
    cur_file = "<unknown>"
    added, removed = [], []
    for line in (patch_text or "").splitlines():
        if line.startswith("+++ b/"):
            if cur_file != "<unknown>":
                files.append((cur_file, added, removed))
            cur_file = line[len("+++ b/"):].strip()
            added, removed = [], []
        elif line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            removed.append(line[1:])
    if cur_file != "<unknown>":
        files.append((cur_file, added, removed))
    return files
