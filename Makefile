run-mcp-server:
	/opt/anaconda3/envs/GoAI/bin/python mcp_server/server.py

# 统一部署：单一安装脚本 + 单一运行脚本（配置见 deploy/config.env）
install:
	bash deploy/scripts/install.sh

start:
	bash deploy/scripts/run.sh start

stop:
	bash deploy/scripts/run.sh stop

restart:
	bash deploy/scripts/run.sh restart

status:
	bash deploy/scripts/run.sh status

test:
	python -m py_compile mcp_server/server.py mcp_server/composed_tools.py mcp_server/mcp_primitives.py

swe-bench:
	python scripts/swe_bench_runner.py

swe-bench-list:
	python scripts/swe_bench_runner.py --list

swe-bench-dry:
	python scripts/swe_bench_runner.py --dry-run

swe-bench-index:
	python scripts/swe_bench_runner.py --index-only

swe-bench-reset:
	python scripts/swe_bench_runner.py --reset-db-only

swe-bench-clean:
	python scripts/swe_bench_runner.py --reset-db

swe-bench-rerun:
	python scripts/swe_bench_runner.py --rerun-all
