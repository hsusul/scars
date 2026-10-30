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

Both harness configs run a shim, `~/.scars/bin/scars-hook`, which execs the installed `scars-hook` by
absolute path, so the hook command in them stays the same across upgrades and reinstalls. When the
installed `scars-hook` lives inside a git checkout, init refuses unless `--allow-dev-install` is given:
an agent working in that checkout could edit the code every hook run executes.

Every write is idempotent: a file that already has the planned content is reported unchanged. A write
goes to the real file behind any symlink, through a temp file in the same directory that is fsynced and
renamed into place, and keeps the existing file's mode. A read-only file is replaced only after the
person confirms it, never under `--yes`. The Codex merge accepts only the documented shape of
`hooks.json` (any other shape stops init before it changes anything) and removes only handlers whose
command is exactly one scars renders; everything else in the file is kept.
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
import stat
import subprocess
import sys
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
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
LAUNCHD_LABEL = "dev.scars.miner"
CONFIG_MODE = 0o600
PLIST_MODE = 0o644
SHIM_MODE = 0o755
_CWD = re.compile(r'"cwd"\s*:\s*("(?:[^"\\]|\\.)*")')


@dataclass(frozen=True)
class PlannedFile:
    path: Path
    content: str
    mode: int = CONFIG_MODE
    """The mode of a new file. An existing file keeps its own mode unless `force_mode` is set."""
    force_mode: bool = False

    def target(self) -> Path:
        """The file a write lands in: `path` with every symlink resolved."""
        return Path(os.path.realpath(self.path))

    def current(self) -> str | None:
        try:
            return self.path.read_text()
        except FileNotFoundError:
            return None

    def _mode_ok(self) -> bool:
        try:
            return not self.force_mode or stat.S_IMODE(os.stat(self.target()).st_mode) == self.mode
        except FileNotFoundError:
            return False

    def up_to_date(self) -> bool:
        return self.current() == self.content and self._mode_ok()

    def read_only(self) -> bool:
        target = self.target()
        return target.exists() and not os.access(target, os.W_OK)

    def diff(self) -> str:
        old = self.current()
        if old == self.content:
            return "" if self._mode_ok() else f"mode of {self.path} becomes {self.mode:o}\n"
        lines = difflib.unified_diff(
            [] if old is None else old.splitlines(keepends=True),
            self.content.splitlines(keepends=True),
            fromfile="/dev/null" if old is None else str(self.path),
            tofile=str(self.path),
        )
        return "".join(lines)

    def write(self) -> bool:
        """Write atomically into the real file. False when it already had this content and mode.

        The temp file has a unique name in the target's own directory, so concurrent inits never share
        one and the rename never crosses a file system. File and directory are fsynced before and after
        the rename, so a crash leaves the old file or the new one.
        """
        if self.up_to_date():
            return False
        target = self.target()
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            mode = self.mode if self.force_mode else stat.S_IMODE(os.stat(target).st_mode)
        except FileNotFoundError:
            mode = self.mode
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".scars-tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(self.content)
                f.flush()
                os.fchmod(f.fileno(), mode)
                os.fsync(f.fileno())
            os.replace(tmp, target)
        except BaseException:
            with suppress(FileNotFoundError):
                os.unlink(tmp)
            raise
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
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


def shim_file(scars_home: Path, hook_path: str) -> PlannedFile:
    """`~/.scars/bin/scars-hook`, which execs the installed hook with the harness's arguments."""
    content = f'#!/bin/sh\nexec {shlex.quote(hook_path)} "$@"\n'
    return PlannedFile(scars_home / "bin" / "scars-hook", content, SHIM_MODE, force_mode=True)


def git_checkout_of(path: str) -> Path | None:
    """The nearest directory holding `path` (after resolving symlinks) that has a `.git` entry."""
    for parent in Path(os.path.realpath(path)).parents:
        if (parent / ".git").exists():
            return parent
    return None


def codex_shape_problem(config: dict[str, object]) -> str | None:
    """Where `config` leaves the documented `hooks.json` shape, or None when it follows it.

    The shape: an object whose optional `hooks` maps each event to a list of groups, each group an
    object with a `hooks` list of handler objects. Anything else might be another tool's format that a
    merge would silently damage, so init refuses it instead of guessing.
    """
    hooks = config.get("hooks", {})
    if not isinstance(hooks, dict):
        return "hooks is not an object"
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            return f"hooks.{event} is not a list"
        for i, group in enumerate(groups):
            where = f"hooks.{event}[{i}]"
            if not isinstance(group, dict):
                return f"{where} is not an object"
            if not isinstance(group.get("hooks"), list):
                return f"{where}.hooks is not a list"
            for j, handler in enumerate(group["hooks"]):
                if not isinstance(handler, dict):
                    return f"{where}.hooks[{j}] is not an object"
    return None


def _commands(config: dict[str, object]) -> set[str]:
    hooks = config["hooks"]
    assert isinstance(hooks, dict)
    return {h["command"] for groups in hooks.values() for g in groups for h in g["hooks"]}


def merge_codex(existing: dict[str, object], ours: dict[str, object]) -> dict[str, object]:
    """`existing` (already checked by `codex_shape_problem`) with scars's handlers replaced by `ours`.

    A handler is scars's only when its command equals, exactly, a command in `ours`. A group is dropped
    only when removing scars's handlers emptied it; empty groups and events that were already empty stay.
    """
    scars = _commands(ours)
    hooks = existing.get("hooks", {})
    assert isinstance(hooks, dict)
    out: dict[str, list[object]] = {}
    for event, groups in hooks.items():
        kept: list[object] = []
        for group in groups:
            others = [
                h for h in group["hooks"] if not (isinstance(c := h.get("command"), str) and c in scars)
            ]
            if len(others) == len(group["hooks"]):
                kept.append(group)
            elif others:
                kept.append({**group, "hooks": others})
        out[event] = kept
    ours_hooks = ours["hooks"]
    assert isinstance(ours_hooks, dict)
    for event, groups in ours_hooks.items():
        out.setdefault(event, []).extend(groups)
    return {**existing, "hooks": out}


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
        if (problem := codex_shape_problem(loaded)) is not None:
            raise UserError(
                f"{path} is not in the shape scars knows ({problem}); nothing was changed. "
                "Fix it or move it away, then run scars init again"
            )
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
    return PlannedFile(user_home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist", content, PLIST_MODE)


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


class Remote(StrEnum):
    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"
    """git failed or timed out, so the repo may well have a remote."""


@dataclass(frozen=True)
class FoundRepo:
    common_dir: str
    name: str
    remote: Remote


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
            remote = Remote.UNKNOWN if remotes is None else Remote.YES if remotes.strip() else Remote.NO
            by_common[common] = FoundRepo(common, repo_name(common), remote)
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
                if repo.remote is not Remote.NO:
                    why = (
                        "has a remote, so not sure it is yours"
                        if repo.remote is Remote.YES
                        else "cannot tell whether it has a git remote"
                    )
                    ctx.print(f"repo {repo.name} ({repo.common_dir}): {why}; skipped")
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
        if f.up_to_date() or not f.read_only():
            continue
        if yes:
            ctx.print(
                f"{title}: not written: {f.path} is read-only; run scars init without --yes to replace it"
            )
            return False
        if not ctx.confirm(f"{f.path} is read-only; replace it anyway?"):
            ctx.print(f"{title}: not written")
            return False
    for f in planned:
        f.write()
    ctx.print(f"{title}: written")
    return True


@dataclass(frozen=True)
class Plan:
    """Every file init may write, built before anything is touched so a refusal changes nothing."""

    claude: list[PlannedFile]
    codex: list[PlannedFile]
    launchd: PlannedFile | None


def plan(ctx: Context, scars_home: Path, hook_path: str) -> Plan:
    shim = shim_file(scars_home, hook_path)
    command = str(shim.path)
    job = None
    if ctx.platform == "darwin":
        env = {} if scars_home == ctx.user_home / ".scars" else {"SCARS_HOME": str(scars_home)}
        job = launchd_file(ctx.user_home, installed_script("scars"), scars_home, env)
    return Plan(
        claude=[shim, *claude_files(scars_home, command)],
        codex=[shim, codex_file(ctx.user_home, command)],
        launchd=job,
    )


def install(ctx: Context, scars_home: Path, files_: Plan, yes: bool) -> None:
    if _offer(ctx, "Claude Code plugin", files_.claude, yes):
        plugins = scars_home / "plugins"
        ctx.print("to turn it on in Claude Code, run:")
        ctx.print(f"  claude plugin marketplace add {shlex.quote(str(plugins))}")
        ctx.print("  claude plugin install scars@scars-local")
    if _offer(ctx, "Codex hooks", files_.codex, yes):
        ctx.print("Codex asks you to review and trust the new hooks the next time it starts")
    if files_.launchd is None:
        ctx.print("miner schedule: launchd is macOS only; run `scars mine` every 2 hours from cron instead")
        return
    job = files_.launchd
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


def hook_script(ctx: Context, allow_dev_install: bool) -> str:
    """The installed `scars-hook`, refused when it sits in a git checkout unless that is allowed."""
    hook_path = installed_script("scars-hook")
    checkout = git_checkout_of(hook_path)
    if checkout is not None:
        real = os.path.realpath(hook_path)
        if not allow_dev_install:
            raise UserError(
                f"refused: {real} is inside the git checkout {checkout}, and an agent working in that "
                "checkout could edit the hook code every session runs; install scars outside a checkout, "
                "or pass --allow-dev-install to accept that"
            )
        ctx.print(f"warning: {real} is inside the git checkout {checkout} (allowed by --allow-dev-install)")
    return hook_path


def cmd_init(args: argparse.Namespace, ctx: Context) -> int:
    scars_home = paths.home()
    files_ = plan(ctx, scars_home, hook_script(ctx, args.allow_dev_install))
    scars_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(scars_home, 0o700)
    (scars_home / "logs").mkdir(mode=0o700, exist_ok=True)
    db = store.init_lessons(scars_home / "lessons.db")
    store.ensure_miner_schema(db)
    db.close()
    tele = store.init_telemetry(scars_home / "telemetry.db")
    store.ensure_telemetry_schema(tele)
    tele.close()
    ctx.print(f"store: {scars_home}")
    register_repos(ctx, scars_home, args.yes)
    install(ctx, scars_home, files_, args.yes)
    summary = mining.run(ctx)
    for line in mining.summary_lines(summary):
        ctx.print(line)
    show_top(ctx, scars_home)
    guard = ctx.user_home / ".claude" / "hooks" / "guard-bash.sh"
    if guard.exists():
        ctx.print(f"to teach {guard.name}'s rules at session start, run: scars import-guard")
    return 0
