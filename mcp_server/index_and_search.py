#!/usr/bin/env python3
"""索引 + 混合检索 一次性联调脚本（eval helper 前置工具）。

用法:
  # 直接索引本地仓库并检索
  python index_and_search.py --repo /tmp/flask --query "blueprint route registration"

  # 自动 clone 后索引检索
  python index_and_search.py --clone https://github.com/pallets/flask --repo /tmp/flask \
      --query "how does url mapping work"

  # 仅做 DB 连通健康探测
  python index_and_search.py --check-db

  # 跳过索引（已索引过，仅检索），配合随机切换仓库/issue 很有用
  python index_and_search.py --repo /tmp/flask --query "..." --no-index

设计要点:
  - 仅用标准库 (urllib)，不引入额外依赖；
  - repo_indexer 返回 unavailable/error 时明确报错并中止，不静默假成功；
  - 检索结果打印 来源路径 :: 符号 + 分数 + 片段，便于人工判断召回质量。
"""
import argparse
import json
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
# 优先新布局（统一部署脚本生成的 deploy/db/.env），兼容旧名 .env.db
for _candidate in (os.path.join(_REPO_ROOT, "deploy", "db", ".env"),
                   os.path.join(_REPO_ROOT, "deploy", "db", ".env.db")):
    if os.path.exists(_candidate):
        os.environ.setdefault("AGENTTEAMS_ENV_FILE", _candidate)
        break


def _health():
    try:
        from db.health import db_health
        return 200, db_health({})
    except Exception as e:  # noqa: BLE001
        return 0, {"status": "transport_error", "reason": str(e)}


def _call_skill(name: str, payload: dict):
    try:
        from skills import skill_repo_indexer, skill_hybrid_search
        handlers = {
            "repo_indexer": skill_repo_indexer,
            "hybrid_search": skill_hybrid_search,
        }
        return 200, handlers[name](payload)
    except Exception as e:  # noqa: BLE001
        return 0, {"status": "transport_error", "reason": str(e)}


def main() -> None:
    ap = argparse.ArgumentParser(description="仓库索引 + 混合检索 联调脚本")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--repo", help="本地仓库路径")
    ap.add_argument("--clone", help="git url，自动 clone 到 --repo")
    ap.add_argument("--branch", default=None, help="clone 时指定分支")
    ap.add_argument("--query", default=None, help="检索查询")
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--commit", default="", help="base_commit，传给 repo_indexer 做增量更新（单命名空间 per repo）")
    ap.add_argument("--no-index", action="store_true", help="跳过索引，直接检索")
    ap.add_argument("--check-db", action="store_true", help="仅检查四库连通性")
    args = ap.parse_args()

    if args.check_db:
        _, body = _health()
        print(json.dumps(body, ensure_ascii=False, indent=2))
        return

    if args.clone and args.repo:
        cmd = ["git", "clone", "--depth", "1"]
        if args.branch:
            cmd += ["--branch", args.branch]
        cmd += [args.clone, args.repo]
        subprocess.run(cmd, check=True)

    if not args.repo:
        ap.error("--repo 必填（或配合 --clone 使用）")

    if not args.no_index:
        print(f"[1/2] 索引仓库: {args.repo} (commit={args.commit[:8] or 'HEAD'})")
        st, body = _call_skill("repo_indexer", {
            "repo_path": args.repo, "commit": args.commit,
        })
        print(f"  -> HTTP {st} {json.dumps(body, ensure_ascii=False)}")
        if body.get("status") in ("unavailable", "error"):
            print("  索引失败，请检查数据库栈（bash deploy/scripts/run.sh status）/ deploy/db/.env / 依赖；中止。")
            sys.exit(1)

    if args.query:
        # 单命名空间 per repo（与 skills.py:repo_indexer 一致，不再拼接 commit hash）
        ns = os.path.basename(os.path.abspath(args.repo))
        print(f"[2/2] 混合检索: {args.query!r} (ns={ns})")
        st, body = _call_skill(
            "hybrid_search",
            {"query": args.query, "top_k": args.top_k, "ns": ns},
        )
        if body.get("status") == "unavailable":
            print("  检索不可用，请检查 DB 连通。")
            sys.exit(1)
        results = body.get("results", [])
        print(f"  candidates={body.get('candidates')} returned={len(results)}")
        for i, r in enumerate(results):
            print(f"  #{i + 1} score={r.get('score'):.4f} {r.get('path')} :: {r.get('symbol')}")
            snippet = (r.get("content") or "")[:200].replace("\n", " ")
            print(f"       {snippet}")
        expansion = body.get("graph_expansion", [])
        if expansion:
            print(f"  --- 图谱扩充 ({len(expansion)} 条) ---")
            for i, r in enumerate(expansion):
                print(f"  +{i + 1} [{r.get('relation')} via {r.get('via')}] "
                      f"{r.get('path')} :: {r.get('symbol')}")
    else:
        print("未给 --query，仅完成索引。")


if __name__ == "__main__":
    main()
