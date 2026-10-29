# scars

scars stops coding agents from repeating mistakes that another session on the same machine already hit and fixed.

It mines past Claude Code and Codex sessions for failures that were followed by a working fix. Each one becomes a short lesson. The lesson appears where it helps: before the agent plans, or right after the same failure happens again.

Status: under construction. Nothing is usable yet.

## Read the spec

- [PRD](docs/PRD.md) covers the problem, the evidence, the phases and their gates.
- [Architecture](docs/ARCHITECTURE.md) covers the schema, the hook contract, matching and the miner.
- [Threat model](docs/THREAT-MODEL.md) and [eval plan](docs/EVAL-PLAN.md).
- [Critique](docs/CRITIQUE.md) records every expert-panel and review finding and where the docs resolve it. Decision records are in `docs/adr/`.
- [Research](research/README.md) holds the original market and engineering research, and `research/experiments/` holds the probes behind the numbers.

`scripts/check_traceability.py` checks that every critique finding is answered in the docs. `tests/test_schema.py` checks that the SQL in `docs/ARCHITECTURE.md` matches the shipped schema in `src/scars/schema/`. CI runs both.
