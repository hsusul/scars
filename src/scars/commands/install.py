"""`scars init`: create the store, register repos, install hook configuration, mine history.

Steps, in order:

1. Create `~/.scars` (0700) with lessons.db, telemetry.db and `logs/`.
2. Find the git repos that recent transcripts (last 30 days) worked in, by their git common dir, and ask
   for each unregistered one whether it is the person's own code or an upstream project. With `--yes`
   nothing is asked, and only repos that are surely the person's own are registered: those with no
   git remote at all. Every other repo is listed and left for a later interactive run.
3. Show the exact hook configuration files with a diff against what is on disk, and write each group
   only after a yes: the Claude Code plugin as a local marketplace under `~/.scars/plugins/` (Claude
   Code's own settings file is never touched; init prints the two `claude plugin` commands instead), the
   Codex hooks merged into `~/.codex/hooks.json`, and on macOS the launchd job that runs `scars mine`
   every 2 hours. `--yes` answers yes.
4. Mine history and list the top 5 waiting lessons with how often each would have fired in the last 30
   days: the mined failures in that window with the lesson's error signature.

Every write is idempotent: a file that already has the planned content is reported unchanged, and the
Codex merge replaces only entries that run `scars-hook`.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import plistlib
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from scars import hook, paths, store
from scars.commands import Context, UserError, mining
from scars.commands import inbox as inbox_cmd
from scars.mine import default_sources, existing_dir
from scars.model import RepoId, TriggerKind

DAY = 86400.0
DISCOVERY_DAYS = 30
TOP = 5
HOOK_MARK = "scars-hook"
LAUNCHD_LABEL = "dev.scars.miner"
_CWD = re.compile(r'"cwd"\s*:\s*("(?:[^"\\]|\\.)*")')


@dataclass(frozen=True)
class PlannedFile:
    path: Path
    content: str

    def current(self) -> str | None:
        try:
            return self.path.read_text()
        except FileNotFoundError:
            return None

    def diff(self) -> str:
        old = self.current()
        if old == self.content:
            return ""
        lines = difflib.unified_diff(
            [] if old is None else old.splitlines(keepends=True),
            self.content.splitlines(keepends=True),
            fromfile="/dev/null" if old is None else str(self.path),
            tofile=str(self.path),
        )
        return "".join(lines)

    def write(self) -> bool:
        """Write atomically. False when the file already had this content."""
        if self.current() == self.content:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".scars-tmp")
        tmp.write_text(self.content)
        os.replace(tmp, self.path)
        return True


def _template(name: str) -> str:
    return files("scars").joinpath("plugins", *name.split("/")).read_text()


def installed_script(name: str) -> str:
    """The absolute path of an installed console script, next to this interpreter or on PATH."""
    beside = Path(sys.executable).parent / name
    if beside.exists():
        return str(beside)
    found = shutil.which(name)
    if found is None:
        raise UserError(f"cannot find the installed {name} script; reinstall scars")
    return os.path.abspath(found)


def _replace(value: object, old: str, new: str) -> object:
    match value:
        case str():
            return value.replace(old, new)
        case list():
            return [_replace(v, old, new) for v in value]
        case dict():
            return {k: _replace(v, old, new) for k, v in value.items()}
    return value


def hooks_config(name: str, hook_path: str) -> dict[str, object]:
    """A packaged hooks file with every command pointing at the shell-quoted `hook_path`."""
    config = _replace(json.loads(_template(name)), "@SCARS_HOOK@", shlex.quote(hook_path))
    assert isinstance(config, dict)
    return config


def _json(value: object) -> str:
    return json.dumps(value, indent=2) + "\n"


def claude_files(scars_home: Path, hook_path: str) -> list[PlannedFile]:
    base = scars_home / "plugins"
    return [
        PlannedFile(
            base / ".claude-plugin" / "marketplace.json", _template(".claude-plugin/marketplace.json")
        ),
        PlannedFile(
            base / "claude" / ".claude-plugin" / "plugin.json", _template("claude/.claude-plugin/plugin.json")
        ),
        PlannedFile(
            base / "claude" / "hooks" / "hooks.json",
            _json(hooks_config("claude/hooks/hooks.json", hook_path)),
        ),
    ]


def _runs_scars(handler: object) -> bool:
    return isinstance(handler, dict) and HOOK_MARK in str(handler.get("command", ""))


def merge_codex(existing: dict[str, object], ours: dict[str, object]) -> dict[str, object]:
    """`existing` with every handler that runs `scars-hook` replaced by `ours`; nothing else changes."""
    merged = dict(existing)
    hooks = merged.get("hooks", {})
    if not isinstance(hooks, dict):
        raise UserError("~/.codex/hooks.json has a hooks value that is not an object; fix it first")
    out: dict[str, list[object]] = {}
    for event, groups in hooks.items():
        kept = []
        for group in groups if isinstance(groups, list) else []:
            if isinstance(group, dict) and isinstance(group.get("hooks"), list):
                handlers = [h for h in group["hooks"] if not _runs_scars(h)]
                if handlers:
                    kept.append({**group, "hooks": handlers})
            else:
                kept.append(group)
        if kept:
            out[event] = kept
    ours_hooks = ours["hooks"]
    assert isinstance(ours_hooks, dict)
    for event, groups in ours_hooks.items():
        out.setdefault(event, []).extend(groups)
    merged["hooks"] = out
    return merged


def codex_file(user_home: Path, hook_path: str) -> PlannedFile:
    path = user_home / ".codex" / "hooks.json"
    existing: dict[str, object] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text())
        except (ValueError, OSError):
            raise UserError(
                f"cannot parse {path}; fix it or move it away, then run scars init again"
            ) from None
        if not isinstance(loaded, dict):
            raise UserError(f"{path} is not a JSON object; fix it or move it away")
        existing = loaded
    return PlannedFile(path, _json(merge_codex(existing, hooks_config("codex/hooks.json", hook_path))))


def launchd_file(user_home: Path, scars_path: str, scars_home: Path, env: dict[str, str]) -> PlannedFile:
    job = plistlib.loads(_template("launchd/dev.scars.miner.plist").encode())
    job["ProgramArguments"] = [scars_path, "mine"]
    log = str(scars_home / "logs" / "miner.log")
    job["StandardOutPath"] = job["StandardErrorPath"] = log
    if env:
        job["EnvironmentVariables"] = env
    content = plistlib.dumps(job).decode()
    return PlannedFile(user_home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist", content)


# ---------------------------------------------------------------- repos


def transcript_cwds(user_home: Path, since: float) -> set[str]:
    """Working directories named in transcripts modified since `since`."""
    found: set[str] = set()
    for source in default_sources(user_home):
        if not source.root.is_dir():
            continue
        for path in source.root.glob(source.pattern):
            try:
                if path.stat().st_mtime < since:
                    continue
                text = path.read_text(errors="replace")
            except OSError:
                continue
            for m in _CWD.finditer(text):
                try:
                    found.add(json.loads(m[1]))
                except ValueError:
                    continue
    return found


@dataclass(frozen=True)
class FoundRepo:
    common_dir: str
    name: str
    has_remote: bool


def _git(*args: str) -> str | None:
    try:
        r = subprocess.run(["git", *args], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def repo_name(common_dir: str) -> str:
    path = common_dir.rstrip("/")
    if os.path.basename(path) == ".git":
        path = os.path.dirname(path)
    return os.path.basename(path).removesuffix(".git") or "repo"


def discover(cwds: set[str]) -> list[FoundRepo]:
    by_common: dict[str, FoundRepo] = {}
    for cwd in sorted(cwds):
        found = existing_dir(cwd)
        common = hook.git_common_dir(found) if found else None
        if common is None:
            continue
        common = os.path.realpath(common)
        if common not in by_common:
            remotes = _git("--git-dir", common, "remote")
            by_common[common] = FoundRepo(common, repo_name(common), bool(remotes and remotes.strip()))
    return sorted(by_common.values(), key=lambda r: (r.name, r.common_dir))


def _repo_id(db: sqlite3.Connection, name: str) -> RepoId:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "repo"
    candidate, n = slug, 1
    while db.execute("select 1 from repos where id = ?", (candidate,)).fetchone():
        n += 1
        candidate = f"{slug}-{n}"
    return RepoId(candidate)


def register_repos(ctx: Context, scars_home: Path, yes: bool) -> None:
    repos = discover(transcript_cwds(ctx.user_home, ctx.clock() - DISCOVERY_DAYS * DAY))
    db = store.connect(scars_home / "lessons.db")
    try:
        fresh = [r for r in repos if store.repo_for_common_dir(db, (r.common_dir,)) is None]
        if not repos:
            ctx.print("repos: no git repos in the last 30 days of transcripts")
        elif not fresh:
            ctx.print(f"repos: all {len(repos)} found are registered")
        for repo in fresh:
            if yes:
                if repo.has_remote:
                    ctx.print(
                        f"repo {repo.name} ({repo.common_dir}): "
                        "has a remote, so not sure it is yours; skipped"
                    )
                    continue
                owner = "own"
            else:
                answer = ctx.ask(
                    f"register {repo.name} ({repo.common_dir})? [o]wn, [u]pstream, [s]kip: "
                ).lower()
                owner = {"o": "own", "own": "own", "u": "upstream", "upstream": "upstream"}.get(answer)
                if owner is None:
                    ctx.print(f"repo {repo.name}: skipped")
                    continue
            with store.transaction(db):
                repo_id = _repo_id(db, repo.name)
                store.add_repo(db, repo_id, repo.name, owner)
                store.add_clone(db, repo.common_dir, repo_id, ctx.clock())
            ctx.print(f"repo {repo.name}: registered as {repo_id} ({owner})")
    finally:
        db.close()


# ---------------------------------------------------------------- install


def _offer(ctx: Context, title: str, planned: list[PlannedFile], yes: bool) -> bool:
    """Show the diff of `planned` and write it after consent. True when the files are in place."""
    diffs = [(f, f.diff()) for f in planned]
    if not any(d for _, d in diffs):
        ctx.print(f"{title}: unchanged")
        return True
    ctx.print(f"{title}: these files would change")
    for f, d in diffs:
        ctx.print(d.rstrip("\n") if d else f"unchanged {f.path}")
    if not (yes or ctx.confirm(f"write the {title}?")):
        ctx.print(f"{title}: not written")
        return False
    for f in planned:
        f.write()
    ctx.print(f"{title}: written")
    return True


def install(ctx: Context, scars_home: Path, yes: bool, platform: str) -> None:
    hook_path = installed_script("scars-hook")
    if _offer(ctx, "Claude Code plugin", claude_files(scars_home, hook_path), yes):
        plugins = scars_home / "plugins"
        ctx.print("to turn it on in Claude Code, run:")
        ctx.print(f"  claude plugin marketplace add {shlex.quote(str(plugins))}")
        ctx.print("  claude plugin install scars@scars-local")
    if _offer(ctx, "Codex hooks", [codex_file(ctx.user_home, hook_path)], yes):
        ctx.print("Codex asks you to review and trust the new hooks the next time it starts")
    if platform != "darwin":
        ctx.print("miner schedule: launchd is macOS only; run `scars mine` every 2 hours from cron instead")
        return
    env = {} if scars_home == ctx.user_home / ".scars" else {"SCARS_HOME": str(scars_home)}
    job = launchd_file(ctx.user_home, installed_script("scars"), scars_home, env)
    if _offer(ctx, "miner schedule", [job], yes):
        ctx.print(f"to start it, run: launchctl bootstrap gui/{os.getuid()} {shlex.quote(str(job.path))}")


def fired_counts(
    db: sqlite3.Connection, lesson_ids: list[tuple[str, str | None]], since: float
) -> dict[str, int]:
    calls = store.mined_calls_since(db, since)
    failed: dict[str, int] = {}
    for c in calls:
        if c.status == "failed" and c.error_signature:
            failed[c.error_signature] = failed.get(c.error_signature, 0) + 1
    return {lid: failed.get(sig, 0) if sig else 0 for lid, sig in lesson_ids}


def show_top(ctx: Context, scars_home: Path) -> None:
    db = store.connect(scars_home / "lessons.db")
    try:
        top = inbox_cmd.rank(db, ctx.clock())[:TOP]
        if not top:
            ctx.print("no lessons waiting yet; scars mine keeps looking")
            return
        sigs = [
            (r.lesson.id, next((t.spec for t in r.lesson.triggers if t.kind is TriggerKind.ERROR), None))
            for r in top
        ]
        fired = fired_counts(db, sigs, ctx.clock() - 30 * DAY)
    finally:
        db.close()
    ctx.print("top lessons waiting for review:")
    for r in top:
        n = fired[r.lesson.id]
        ctx.print(f"  {r.lesson.id}  {r.lesson.status}  AVOID {r.lesson.text.avoid}")
        ctx.print(f"    would have fired {n} time{'s' if n != 1 else ''} in the last 30 days")
    ctx.print("review them with: scars inbox")


def cmd_init(args: argparse.Namespace, ctx: Context) -> int:
    scars_home = paths.home()
    scars_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(scars_home, 0o700)
    (scars_home / "logs").mkdir(mode=0o700, exist_ok=True)
    db = store.init_lessons(scars_home / "lessons.db")
    store.ensure_miner_schema(db)
    db.close()
    store.init_telemetry(scars_home / "telemetry.db").close()
    ctx.print(f"store: {scars_home}")
    register_repos(ctx, scars_home, args.yes)
    install(ctx, scars_home, args.yes, ctx.platform)
    summary = mining.run(ctx)
    for line in mining.summary_lines(summary):
        ctx.print(line)
    show_top(ctx, scars_home)
    guard = ctx.user_home / ".claude" / "hooks" / "guard-bash.sh"
    if guard.exists():
        ctx.print(f"to teach {guard.name}'s rules at session start, run: scars import-guard")
    return 0
