<div align="center">
  <img src="asset/logo.png" alt="Daedalus Logo" width="200"/>
  <h1>Daedalus</h1>
  <p><strong>An autonomous software company, built from agent teams.</strong></p>
  <p>From issue to resolution — architected, coded, tested, and shipped by AI agents working as one organization.</p>

  <p>
    <a href="https://github.com/agentscope-ai/AgentTeams"><img src="https://img.shields.io/badge/Orchestration-AgentTeams-blueviolet" alt="Orchestration"/></a>
    <img src="https://img.shields.io/badge/Runtime-OpenClaw%20%7C%20QwenPaw%20%7C%20Hermes-blue" alt="Runtime"/>
    <img src="https://img.shields.io/badge/Benchmark-SWE--bench-green" alt="SWE-bench"/>
    <img src="https://img.shields.io/badge/Status-Production--oriented-orange" alt="Status"/>
    <img src="https://img.shields.io/badge/License-Apache--2.0-lightgrey" alt="License"/>
  </p>
</div>

---

## Why Daedalus?

In Greek mythology, **Daedalus** was the master craftsman — architect of the Labyrinth, maker of wings. He didn't just build things; he built *systems that solved impossible problems*.

Daedalus brings that spirit to software engineering. It is not a coding assistant, and not a demo. It is a **production-oriented, multi-agent organization** that mirrors the structure of a real software company: a team of specialized agents — each with a defined role, clear decision boundaries, and auditable traces — collaborating to autonomously resolve real development issues.

Today's Daedalus runs a full R&D team: bug fixes, feature development (including greenfield projects), and production incident triage — three task types, one closed loop. Tomorrow, it grows into the complete software company: operations, CI/CD, and continuous integration — the entire software lifecycle, end to end.

## How It Works

An issue enters. A resolved, tested, verified patch exits. Everything in between is handled by the team:

<!-- Diagram source of truth: asset/architecture.svg. To re-export the PNG, render the SVG at its viewBox size with any SVG→PNG tool (e.g. Playwright screenshot or cairosvg). -->

<img src="asset/architecture.png" alt="Daedalus end-to-end flow. Left: issue sources - human chat room, Jira ticket listener, and incident alerts. Middle: the Daedalus 7-role agent team - Team Leader routing feature/greenfield, bug, and incident paths through PO, Architect, Developer, Tester, Reviewer, and Ops Analyst - invoking the skills layer over MCP to databases and GitHub. Right: delivery - a merge-ready pull request confirmed by human review; blocked cases escalate to a human; AgentLoop monitoring spans the full link." />

Issues arrive through the team chat room, a Jira ticket listener, or an incident alert. The Team Leader parses the task envelope (type: incident / bug / feature / greenfield), routes it down the matching pipeline, and the team resolves it — the result ships as a merge-ready pull request, today confirmed by a human reviewer before merge. Each agent runs in its own Docker container, coordinates through the AgentTeams orchestration layer, and shares artifacts through a common workspace. Every step leaves an execution trace — auditable, replayable, and accountable.

| Layer | Technology |
|-------|-----------|
| **Agent Orchestration** | [AgentTeams (Hiclaw)](https://github.com/agentscope-ai/AgentTeams) — declarative Worker / Team / Manager / Human resources |
| **Agent Runtime** | OpenClaw / QwenPaw / Hermes (pluggable) |
| **Execution Environment** | Docker — every agent isolated in its own container |
| **Vector Database** | PostgreSQL + pgvector — semantic & hybrid code search |
| **Knowledge Graph** | Neo4j  — code dependency & engineering knowledge |
| **State & Cache** | Redis — file-hash cache for commit status monitoring |
| **Shared Storage** | MinIO — artifact exchange between agents |
| **Skill Protocol** | MCP (Model Context Protocol) — domain skills served to every worker |

## The Team

| Role | Responsibility |
|------|----------------|
| **Team Leader** (coordinator) | Receives the task envelope, routes work, arbitrates rollback, and owns the final Verdict. |
| **PO** | Product Owner. Runs Gate0 clarification and authors the PRD; does not write code or make technical choices. |
| **Architect** | Bug fix: root-cause analysis. Feature / greenfield: docs-first architecture design (ADD), tech-stack rationale, and visual baseline extraction. |
| **Developer** | Implements the fix or feature according to PRD+ADD; bug fix uses minimal patch; frontend changes consume `ui_spec` and run `visual_check` self-check. |
| **Tester** | Derives tests from PRD independently, executes them, and runs visual regression where applicable. |
| **Reviewer** | The senior quality gate. Reviews code/design with the strongest model, outputs `failure_class`, and blocks Verdict until quality is proven. |
| **Ops Analyst** | Incident triage only. Diagnoses production environments, produces a diagnosis report, and routes code issues back as bugs. Never mutates production. |

New roles plug into the same declarative YAML templates — the org chart is configuration, not code.

## Quick Start

### Prerequisites

| Requirement | Details |
|-------------|---------|
| **Docker Desktop** | Each agent role runs in its own Docker container. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) before proceeding. |
| **Memory** | At least **32 GB RAM** for local deployment — the full stack (agents, PostgreSQL, Neo4j, Redis, MinIO) is memory-intensive. |
| **Disk** | ~50 GB free for images, vectors, and knowledge-graph data. |

### Single-Server Deployment

Everything (AgentTeams platform, databases, and the domain-skills MCP server) runs on one
machine — your laptop or a single server. No remote orchestration or SSH tunnels needed;
just run the commands on the target machine. All operations are exposed as `make` targets
(thin wrappers over `deploy/scripts/install.sh` and `deploy/scripts/run.sh`).

First-time setup:

```bash
cp deploy/config.env.example deploy/config.env   # fill in API keys / admin password
make install                                     # install DB stack + MCP server + platform
                                                 # (Playwright for visual-check is auto-installed;
                                                 #  failure only degrades visual checks, never blocks install)
```

Daily operation — bring up the entire company with one command:

```bash
make start      # start DB stack + MCP server + platform
make stop       # shut everything down
make restart    # stop, then start
make status     # deployment summary
```

## Evaluation

There are currently two ways to verify the agent team end-to-end:

**1. SWE-Bench automated runner**

`scripts/swe_bench_runner.py` drives the full pipeline against real SWE-bench cases (pallets/flask). It submits issues through the AgentTeams flow (Architect → Developer → Tester → Reviewer on the bug pipeline), collects patches, and validates them with the official SWE-bench test harness.

```bash
# Full pipeline: index → submit → wait → verify
make swe-bench

# List available Flask instances
make swe-bench-list

# Dry run (plan only, no actions)
make swe-bench-dry
```

Other runner targets: `make swe-bench-index` (index only), `make swe-bench-reset` / `make swe-bench-clean` (reset state/DB), `make swe-bench-rerun` (rerun all).

**2. AgentTeams chat room**

Open the AgentTeams chat UI and send a task directly to the **Team Leader** agent. It parses the task envelope, routes work to the team (bug → Architect; feature → PO; incident → Ops Analyst), and drives the full resolution loop interactively. This is the fastest way to test a specific bug, feature request, or incident report.

## Repository Layout

```
deploy/          AgentTeams installers, 7 worker/team templates, skill package (v0.2.0), ops scripts
mcp_server/     Domain-skills MCP server (composed tools, data primitives, embeddings)
scripts/        SWE-bench automated evaluation runner
asset/          Architecture diagrams (SVG source + exported PNG)
docs/           Design documents (02_详细设计: v3.0 detailed design)
```

## Roadmap

Daedalus is production-oriented, and the roadmap reflects that:

- **Jira integration** — a listener service that watches a Jira project and routes incoming issues directly to the agent team, closing the loop from ticket creation to resolved patch
- **Full lifecycle deepening** — beyond the delivered bug / feature / greenfield / incident pipelines: richer release engineering, requirements management, and cross-team collaboration
- **Engineering memory** — post-mortems and a living R&D knowledge base, so the team learns from every resolved issue instead of starting from zero
- **Progressive delivery** — canary release with automated result confirmation
- **Full-link observability** — AgentLoop integration for end-to-end agent monitoring
- **Skill engineering** — versioned, lifecycle-managed skill platform

## Acknowledgments

- [AgentTeams (Hiclaw)](https://hiclaw.io/) — the agent orchestration foundation
- [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) — reference implementation

---

<div align="center">
  <sub>Daedalus — the craftsman never sleeps.</sub>
</div>
