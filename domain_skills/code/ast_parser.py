"""基于 tree-sitter 的多语言代码切分（设计指定 AST 框架）。

职责：把仓库解析为「函数/类/方法」粒度的代码块（chunk），
每块带稳定 id（path:symbol:start_line），供向量库/图库/全文库三端共用同一主键。

- 使用 tree-sitter + tree-sitter-languages（预编译多语言 grammar，免编译、跨语言）；
- 第三方库懒加载：未装时模块仍可 import，仅 parse 时抛错；
- 节点类型用「包含 function/method/class 或 *_definition/_declaration」的启发式匹配，
  对 Python/JS/TS/Go/Java 等通用，无需逐语言硬编码。
"""

import os
import re
from typing import Optional

try:
    from db.base import DbUnavailable
except ImportError:  # 兼容 python -m 包模式
    from ..db.base import DbUnavailable

# 按扩展名选择 tree-sitter 语言
_EXT_LANG = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".java": "java",
    ".rb": "ruby",
    ".rs": "rust",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
}

_SKIP_DIRS = {".git", "node_modules", "__pycache__", "venv", ".venv", "build", "dist"}
_DEF_RE = re.compile(r"(function|method|class|_definition|_declaration)")

# 调用节点类型（跨语言启发式：python=call / js,ts,go,rust=call_expression / java=method_invocation）
_CALL_TYPES = {"call", "call_expression", "method_invocation", "invocation_expression", "macro_invocation"}
_ID_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# import 语句里的模块名：`from X import Y` / `import X` / `import x from "mod"` / `require("mod")`
_IMPORT_MOD_RE = re.compile(r"(?:from|import|require\()\s*[\"']?([A-Za-z_][\w\./]*)")
# 语言内建/超高频调用名：连 CALLS 边时跳过，避免海量误连边
_CALL_STOPLIST = {
    "print", "len", "str", "int", "float", "bool", "dict", "list", "set", "tuple",
    "append", "extend", "get", "pop", "items", "keys", "values", "join", "split",
    "format", "repr", "isinstance", "issubclass", "super", "type", "range", "open",
    "hasattr", "getattr", "setattr", "enumerate", "zip", "map", "filter", "sorted",
    "add", "update", "startswith", "endswith", "strip", "lstrip", "rstrip", "replace",
    "encode", "decode", "lower", "upper", "next", "iter", "callable", "vars", "id",
}


class AstParser:
    def __init__(self) -> None:
        self._parsers = {}  # lang -> tree_sitter Parser
        self._ts = None

    def _get_parser(self, lang: str):
        if lang in self._parsers:
            return self._parsers[lang]
        try:
            import tree_sitter_languages as tsl
        except ImportError as e:
            raise DbUnavailable("tree-sitter-languages 未安装：pip install tree-sitter tree-sitter-languages") from e
        parser = tsl.get_parser(lang)
        self._parsers[lang] = parser
        return parser

    def _symbol_of(self, node) -> str:
        name_node = node.child_by_field_name("name")
        if name_node is not None:
            return name_node.text.decode("utf-8", "replace")
        # 退化：找第一个 identifier
        for child in node.children:
            if child.type == "identifier":
                return child.text.decode("utf-8", "replace")
        # decorated_definition 等包装节点：名字在内部的定义子节点上
        for child in node.children:
            if _DEF_RE.search(child.type):
                return self._symbol_of(child)
        return "<anonymous>"

    def _calls_of(self, node) -> list:
        """抽取子树内的被调符号名（取调用目标的最后一个标识符，如 self.add_url_rule -> add_url_rule）。"""
        calls, seen = [], set()
        stack = [node]
        while stack:
            n = stack.pop()
            if n.type in _CALL_TYPES:
                target = n.child_by_field_name("function") or n.child_by_field_name("name")
                if target is not None:
                    ids = _ID_RE.findall(target.text.decode("utf-8", "replace"))
                    if ids:
                        name = ids[-1]
                        if name not in _CALL_STOPLIST and name not in seen:
                            seen.add(name)
                            calls.append(name)
            stack.extend(n.children)
        return calls[:50]  # 单块上限，防异常大函数刷爆边数

    def _imports_of(self, root_node) -> list:
        """抽取文件顶层 import 的模块名（点号路径转斜杠，供图库按路径片段匹配文件）。"""
        mods, seen = [], set()
        for child in root_node.children:
            if "import" not in child.type:
                continue
            text = child.text.decode("utf-8", "replace")
            for m in _IMPORT_MOD_RE.findall(text):
                frag = m.replace(".", "/").strip("/")
                if frag and frag not in seen:
                    seen.add(frag)
                    mods.append(frag)
        return mods[:30]

    def _walk(self, node, path: str, repo: str, out: list, in_class: bool = False,
              class_name: Optional[str] = None) -> None:
        t = node.type
        if _DEF_RE.search(t):
            kind = "class" if "class" in t else ("method" if ("method" in t or in_class) else "function")
            content = node.text.decode("utf-8", "replace")
            start = node.start_point[0] + 1
            chunk = {
                "chunk_id": f"{path}:{self._symbol_of(node)}:{start}",
                "repo": repo,
                "path": path,
                "symbol": self._symbol_of(node),
                "kind": kind,
                "content": content,
                "start_line": start,
                # 类块不抽调用（会与其方法块重复），函数/方法块抽取被调符号名
                "calls": [] if kind == "class" else self._calls_of(node),
            }
            # 方法块记录所属类名，供图库建 HAS_METHOD 边、检索时做「同类兄弟方法」扩充
            if kind == "method" and class_name:
                chunk["parent_class"] = class_name
            out.append(chunk)
            if kind == "class":
                # 递归进类体：每个方法独立成块（类块保留作整体概览），
                # 避免大类被嵌入端截断导致靠后方法检索不到。
                sym = self._symbol_of(node)
                for child in node.children:
                    self._walk(child, path, repo, out, in_class=True, class_name=sym)
            # 函数/方法体内的嵌套函数不再单独成块
            return
        for child in node.children:
            self._walk(child, path, repo, out, in_class=in_class, class_name=class_name)

    def parse_file(self, path: str, repo: str = "", display_path: Optional[str] = None) -> list:
        ext = os.path.splitext(path)[1].lower()
        lang = _EXT_LANG.get(ext)
        if not lang:
            return []
        parser = self._get_parser(lang)
        with open(path, "rb") as f:
            data = f.read()
        tree = parser.parse(data)
        logical_path = display_path or path
        chunks = []
        self._walk(tree.root_node, logical_path, repo, chunks)
        # 退化：无函数/类的文件，整文件作为一个 module 块
        if not chunks:
            chunks.append({
                "chunk_id": f"{logical_path}:<module>:1",
                "repo": repo,
                "path": logical_path,
                "symbol": "<module>",
                "kind": "module",
                "content": data.decode("utf-8", "replace"),
                "start_line": 1,
                "calls": [],
            })
        # 文件级 import 模块名，挂到每个块上（图库按 path 去重建 IMPORTS 边）
        imports = self._imports_of(tree.root_node)
        for c in chunks:
            c["file_imports"] = imports
        return chunks

    def parse_repo(self, repo_path: str) -> list:
        repo_path = os.path.abspath(repo_path)
        all_chunks = []
        for root, dirs, files in os.walk(repo_path):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
            for fn in files:
                ext = os.path.splitext(fn)[1].lower()
                if ext not in _EXT_LANG:
                    continue
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, repo_path)
                try:
                    all_chunks.extend(self.parse_file(full, repo=os.path.basename(repo_path), display_path=rel))
                except Exception:  # 单文件解析失败不阻断整库
                    continue
        return all_chunks
