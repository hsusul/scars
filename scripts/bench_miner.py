"""Miner throughput on synthetic Claude transcripts.

Writes a throwaway transcript tree with `--calls` tool calls, of which `--lessons` are failure-then-fix
pairs that each create one lesson (the failure, a read, the fix), and the rest are ordinary shell calls
and reads spread over many programs. Then times one `mine.run` over it in a throwaway home and prints the
elapsed time and the rate. The pairs run first, so every precision check replays the whole call window.

Usage: uv run python scripts/bench_miner.py [--calls N] [--lessons L] [--per-file K]
"""

import argparse
import json
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from scars import mine
from scars.model import Harness

T0 = time.time() - 3600 * 24


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).isoformat().replace("+00:00", "Z")


def _call(
    records: list[str], at: float, tool_id: str, name: str, args: dict[str, object], error: str | None
) -> None:
    use = {"type": "tool_use", "id": tool_id, "name": name, "input": args}
    result = {
        "type": "tool_result",
        "tool_use_id": tool_id,
        "is_error": error is not None,
        "content": error or "ok",
    }
    for kind, at_, block, extra in (
        ("assistant", at, use, {"model": "bench-model"}),
        ("user", at + 0.5, result, {}),
    ):
        message = {"content": [block], **extra}
        records.append(
            json.dumps({"type": kind, "timestamp": _iso(at_), "cwd": "/bench/loose", "message": message})
        )


def _word(k: int) -> str:
    """Letters only, because error signatures fold digits together."""
    out = ""
    while True:
        out = chr(ord("a") + k % 26) + out
        k //= 26
        if not k:
            return "opt" + out


def build(root: Path, calls: int, lessons: int, per_file: int) -> None:
    stream: list[tuple[str, dict[str, object], str | None]] = []
    for k in range(lessons):
        word = _word(k)
        error = f"tool{k}: error: unknown option {word}"
        stream.append(("Bash", {"command": f"tool{k} --{word} target"}, error))
        stream.append(("Read", {"file_path": f"/bench/loose/f{k}.py"}, None))
        stream.append(("Bash", {"command": f"tool{k} target"}, None))
    for i in range(max(0, calls - len(stream))):
        if i % 3 == 0:
            stream.append(("Read", {"file_path": f"/bench/loose/r{i}.py"}, None))
        else:
            stream.append(
                ("Bash", {"command": f"tool{i % (lessons or 1)} --flag{i % 7} item{i} | wc -l"}, None)
            )
    at = T0
    for start in range(0, len(stream), per_file):
        records: list[str] = []
        for j, (name, args, error) in enumerate(stream[start : start + per_file]):
            at += 1
            _call(records, at, f"tu{start + j}", name, args, error)
        path = root / "p" / f"session{start // per_file:05d}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(records) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--calls", type=int, default=1000)
    parser.add_argument("--lessons", type=int, default=50)
    parser.add_argument("--per-file", type=int, default=500)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="scars bench ") as tmp:
        root = Path(tmp)
        build(root / "projects", args.calls, args.lessons, args.per_file)
        size = sum(p.stat().st_size for p in (root / "projects").rglob("*.jsonl"))
        started = time.perf_counter()
        summary = mine.run(
            root / "home", [mine.Source(Harness.CLAUDE, root / "projects", "*/*.jsonl")], time.time()
        )
        elapsed = time.perf_counter() - started
    print(
        f"calls={summary.calls} lessons={summary.candidates_created} size={size / 1e6:.1f} MB "
        f"elapsed={elapsed:.2f}s rate={summary.calls / elapsed:,.0f} calls/s"
    )


if __name__ == "__main__":
    main()
