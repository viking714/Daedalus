run-mcp-server:
	/opt/anaconda3/envs/GoAI/bin/python mcp_server/server.py

build-package:
	bash deploy/scripts/build-skills-package.sh

install-agentteams:
	bash deploy/install/install_agentteams.sh

setup:
	bash deploy/scripts/setup.sh

start:
	bash deploy/scripts/start.sh

stop:
	bash deploy/scripts/start.sh stop

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
