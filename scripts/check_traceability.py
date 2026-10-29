"""Fail if any act-on or consider finding in docs/CRITIQUE.md is not cited by a design doc.

Tracks critique findings (Cxx) and review-round findings (Rxx). Also fails when a required design doc is
missing, when fewer findings are tracked than expected, when an ADR-NNNN reference has no file, or when a
relative markdown link or a backticked research path in docs/ points at a missing file.

Usage: python3 scripts/check_traceability.py
"""

import pathlib
import re
import subprocess
import sys

MIN_TRACKED = {"C": 30, "R": 26}
REQUIRED = ["PRD.md", "ARCHITECTURE.md", "THREAT-MODEL.md", "EVAL-PLAN.md", "CRITIQUE.md"]

root = pathlib.Path(__file__).resolve().parent.parent
docs = root / "docs"
tracked_files = set(
    subprocess.run(
        ["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.splitlines()
)


def committed(path: pathlib.Path) -> bool:
    rel = path.resolve().relative_to(root).as_posix()
    return rel in tracked_files or any(f.startswith(rel + "/") for f in tracked_files)


problems = [f"required doc missing: {name}" for name in REQUIRED if not (docs / name).exists()]
if problems:
    print("\n".join("FAIL " + p for p in problems))
    sys.exit(1)

critique = (docs / "CRITIQUE.md").read_text()
tracked: dict[str, str] = {}
for sec in re.split(r"^### ", critique, flags=re.M):
    title = sec.splitlines()[0].strip().lower() if sec.strip() else ""
    if title in ("act on", "consider"):
        for fid in re.findall(r"^\| ([CR]\d\d) \|", sec, flags=re.M):
            tracked[fid] = title

for prefix, minimum in MIN_TRACKED.items():
    count = sum(1 for f in tracked if f.startswith(prefix))
    if count < minimum:
        problems.append(f"only {count} {prefix} findings tracked, expected at least {minimum}")

design_docs = [docs / name for name in REQUIRED if name != "CRITIQUE.md"] + sorted(
    (docs / "adr").glob("*.md")
)
cited: dict[str, list[str]] = {}
for p in design_docs:
    for fid in set(re.findall(r"\b[CR]\d\d\b", p.read_text())):
        cited.setdefault(fid, []).append(p.name)

for fid, bucket in sorted(tracked.items()):
    if fid not in cited:
        problems.append(f"{fid} ({bucket}) is not cited by any design doc")

adr_files = {p.name[:4] for p in (docs / "adr").glob("*.md")}
for p in [docs / "CRITIQUE.md", *design_docs]:
    text = p.read_text()
    for num in sorted(set(re.findall(r"ADR-(\d{4})", text))):
        if num not in adr_files:
            problems.append(f"{p.name} cites ADR-{num}, which has no file")
    for link in re.findall(r"\]\((?!https?://)([^)#\s]+)", text):
        if not committed(p.parent / link):
            problems.append(f"{p.name} links to a file git does not track: {link}")
    for path in sorted(set(re.findall(r"`(?:\.\./)*(research/[\w./-]+)`", text))):
        if not committed(root / path):
            problems.append(f"{p.name} names a file git does not track: {path}")

print(
    f"{len(tracked)} tracked findings, {sum(1 for f in tracked if f in cited)} cited, {len(design_docs)} design docs"
)
for prob in problems:
    print("FAIL", prob)
sys.exit(1 if problems else 0)
