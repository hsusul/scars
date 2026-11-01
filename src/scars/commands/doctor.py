"""`scars doctor`: is scars delivering lessons, and is its store intact?

Each check prints `ok` or `FAIL` with a reason, and any failure makes the exit code 1. Failures:

- the store is missing, or the event log's hash chain or replay disagrees with the lessons rows,
- the store sits on a synced folder, a network file system or an external disk,
- the kill switch is on, so no hook consults lessons,
- no harness has scars hooks configured,
- the hook shim uses a different store,
- a harness's hook errored on more than 1% of its runs in the last 24 hours,
- Claude Code's sandbox settings let agent commands write the store (see `claude_sandbox`; settings
  doctor cannot read are reported as `unknown`, which does not fail),
- the canary: a synthetic Claude Code `PostToolUseFailure` payload, run through the hook in this process
  against a temporary home holding one built-in lesson, must return that lesson within 100 ms.

Per harness it also reports the last hook run, p50 and p99 latency and the error classes of the last 24
hours. A harness with no runs is reported but is not a failure: it may simply not have been used.

Every printed line passes the sanitizer and secret scan (see `CleanLines`); stored values (error classes,
event chain problems, exception text) also go through `shown`. Telemetry that does not load is a failed
`telemetry` check, not a crash.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import plistlib
import re
import shlex
import sqlite3
import statistics
import subprocess
import tempfile
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from scars import hook, paths, store, telemetry
from scars.commands import Context, clean, connect_readonly, shown, unrenderable
from scars.commands.import_guard import create_pinned
from scars.commands.inspect import when
from scars.model import Harness, HitOutcome, LessonText, Stage, Trigger, TriggerKind
from scars.signature import signature

DAY = 86400.0
CANARY_BUDGET_MS = 100.0
ERROR_RATE_LIMIT = 0.01
SYNCED_MARKERS = (
    "/Library/Mobile Documents/",
    "/Library/CloudStorage/",
    "/Dropbox/",
    "/Google Drive/",
    "/OneDrive/",
    "/iCloud Drive/",
    "/Box/",
)
NETWORK_FS = frozenset({"nfs", "smbfs", "afpfs", "webdav", "cifs", "smb3", "sshfs", "fuse.sshfs", "9p"})
CANARY_ERROR = "Error: scars doctor canary failure"
CANARY_TEXT = LessonText(
    avoid="the scars doctor canary command",
    instead="read the scars doctor report",
    reason="scars doctor checks that a failure brings back its lesson",
)


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool | None
    """None when doctor cannot tell, which is reported but does not fail doctor."""
    detail: str


_DF_ROW = re.compile(r"^.*?\s+\d+\s+\d+\s+\d+\s+\d+%\s+(?P<mount>.+)$")


def mount_point(df_output: str) -> str | None:
    """The mount point in `df -P` output. The row is read by its four numeric columns, because the file
    system name before them and the mount point after them may both contain spaces."""
    lines = df_output.strip().splitlines()
    found = _DF_ROW.match(lines[-1]) if len(lines) >= 2 else None
    return found["mount"].strip() if found else None


def _mount_line(path: Path) -> str | None:
    try:
        df = subprocess.run(["df", "-P", str(path)], capture_output=True, text=True, timeout=5).stdout
        mounts = subprocess.run(["mount"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    mount_point_ = mount_point(df)
    if mount_point_ is None:
        return None
    for line in mounts.splitlines():
        if f" on {mount_point_} " in line:
            return line
    return None


def _internal(mount_line: str) -> bool | None:
    device = mount_line.split(" on ", 1)[0]
    try:
        out = subprocess.run(["diskutil", "info", "-plist", device], capture_output=True, timeout=5)
        info = plistlib.loads(out.stdout) if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException, ValueError):
        return None
    if not isinstance(info, dict) or "Internal" not in info:
        return None
    return bool(info["Internal"]) and not info.get("RemovableMedia", False)


def store_location_problem(home: Path) -> str | None:
    """Why `home` is a poor place for the store, or None. The store wants an internal, unsynced disk:
    a sync client can copy a database mid-write, and a slow or detachable disk breaks hook latency."""
    real = os.path.realpath(home) + "/"
    for marker in SYNCED_MARKERS:
        if marker in real:
            return f"it is inside a synced folder ({marker.strip('/')})"
    line = _mount_line(home)
    if line is None:
        return None
    words = set(line.replace("(", " ").replace(")", " ").replace(",", " ").split())
    if words & NETWORK_FS:
        return "it is on a network file system"
    if _internal(line) is False:
        return "it is on an external disk"
    return None


def pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * p))]


def harness_report(hits: list[telemetry.Hit], harness: Harness) -> tuple[str, bool]:
    """One line about a harness's last 24 hours of hook runs, and whether its error rate is acceptable."""
    mine = [h for h in hits if h.harness == harness.value]
    if not mine:
        return f"{harness}: no hook runs in the last 24 hours", True
    latencies = [h.latency_ms for h in mine if h.latency_ms is not None]
    errors = Counter(h.error_class or "unknown" for h in mine if h.outcome is HitOutcome.ERROR)
    classes = Counter(h.error_class for h in mine if h.error_class)
    # An ignored event does no work that could fail, so it would only dilute the rate.
    handled = sum(h.outcome is not HitOutcome.IGNORED for h in mine)
    rate = sum(errors.values()) / handled if handled else 0.0
    timing = (
        f"p50 {statistics.median(latencies):.1f} ms, p99 {pct(latencies, 0.99):.1f} ms"
        if latencies
        else "no latency"
    )
    error_text = ", ".join(f"{shown(str(k))} {v}" for k, v in sorted(classes.items(), key=str)) or "none"
    line = (
        f"{harness}: last hook {when(max(h.at for h in mine))}, {len(mine)} runs, {timing}, "
        f"errors {sum(errors.values())} ({rate:.1%}), error classes: {error_text}"
    )
    return line, rate <= ERROR_RATE_LIMIT


def _mentions(path: Path, text: str) -> bool:
    try:
        return text in path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def _plugin_state(ctx: Context, scars_home: Path) -> dict[Harness, str]:
    claude_files = scars_home / "plugins" / "claude" / "hooks" / "hooks.json"
    installed = ctx.user_home / ".claude" / "plugins" / "installed_plugins.json"
    if not claude_files.exists():
        claude = "not installed (run scars init)"
    elif _mentions(installed, "scars@scars-local"):
        claude = "installed"
    else:
        claude = "files written, not installed in Claude Code (claude plugin install scars@scars-local)"
    return {Harness.CLAUDE: claude}


def _hook_store(ctx: Context, scars_home: Path) -> Check:
    shim = scars_home / "bin" / "scars-hook"
    try:
        words = shlex.split(shim.read_text(encoding="utf-8"), comments=True)
    except (OSError, ValueError) as exc:
        return Check("hook store", False, f"cannot read {shim}: {shown(str(exc))}; run scars init")
    home = ctx.user_home / ".scars"
    if len(words) == 5 and words[0] == "export" and words[1].startswith("SCARS_HOME="):
        home = Path(words[1].removeprefix("SCARS_HOME="))
        words = words[2:]
    if len(words) != 3 or words[0] != "exec" or words[2] != "$@" or not home.is_absolute():
        return Check("hook store", False, f"cannot determine the store from {shim}; run scars init")
    if os.path.realpath(home) != os.path.realpath(scars_home):
        return Check("hook store", False, f"shim uses {home}, doctor uses {scars_home}; run scars init")
    return Check("hook store", True, str(scars_home))


CLAUDE_MANAGED_SETTINGS = {
    "darwin": "Library/Application Support/ClaudeCode/managed-settings.json",
    "linux": "etc/claude-code/managed-settings.json",
}
_GLOB = re.compile(r"[*?\[]")
_EDIT_RULE = re.compile(r"Edit\((?P<path>.*)\)")


def _reaches(entry: str, store: Path, user_home: Path, *, exact: bool = False) -> bool:
    """Whether a path rule written in harness settings covers `store`. A rule with a wildcard is taken to
    cover everything under its literal prefix, unless `exact` asks for a rule that surely covers it, as a
    deny rule must."""
    if entry == "~" or entry.startswith("~/"):
        entry = str(user_home) + entry[1:]
    if not entry.startswith("/"):
        return False
    targets = {str(store), os.path.realpath(store)}
    if exact:
        entry = entry.removesuffix("/**")
    if wildcard := _GLOB.search(entry):
        return not exact and any((t + "/").startswith(entry[: wildcard.start()]) for t in targets)
    roots = {os.path.normpath(entry), os.path.realpath(entry)}
    return any(t == r or t.startswith(r.rstrip("/") + "/") for t in targets for r in roots)


def _strings(value: object) -> list[str] | None:
    if value is None:
        return []
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return value
    return None


def _section(settings: dict[str, object], *keys: str) -> dict[str, object]:
    for key in keys:
        value = settings.get(key)
        settings = value if isinstance(value, dict) else {}
    return settings


def claude_sandbox(ctx: Context, store: Path) -> Check | None:
    """Whether Claude Code's Bash sandbox, as its user and managed settings configure it, keeps commands
    from writing `store`. None when Claude Code is not installed."""
    name = "claude sandbox"
    user = ctx.user_home / ".claude" / "settings.json"
    managed = ctx.system_root / CLAUDE_MANAGED_SETTINGS.get(ctx.platform, CLAUDE_MANAGED_SETTINGS["linux"])
    if not user.parent.is_dir() and not managed.exists():
        return None
    sources: list[tuple[Path, dict[str, object]]] = []
    for path in (managed, user):
        try:
            settings = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as exc:
            return Check(name, None, f"cannot parse {path}: {shown(str(exc))}")
        if not isinstance(settings, dict):
            return Check(name, None, f"cannot parse {path}: not a JSON object")
        sources.append((path, settings))
    if not sources:
        return Check(name, None, f"no {user}")
    # Managed settings win over the user's for a single value; lists from both apply.
    enabled = next(
        ((p, s["sandbox"]["enabled"]) for p, s in sources if "enabled" in _section(s, "sandbox")), None
    )
    if enabled is None:
        return Check(name, None, f"no sandbox.enabled in {' or '.join(str(p) for p, _ in sources)}")
    path, value = enabled
    if not isinstance(value, bool):
        return Check(name, None, f"sandbox.enabled in {path} is not true or false")
    if not value:
        return Check(name, False, f"sandbox.enabled is false in {path}, so Bash commands can write {store}")
    for path, settings in sources:
        if _section(settings, "sandbox", "filesystem").get("disabled") is True:
            detail = f"sandbox.filesystem.disabled is true in {path}, so Bash commands can write {store}"
            return Check(name, False, detail)
    allows: list[tuple[str, str, Path]] = []
    for path, settings in sources:
        filesystem, permissions = (
            _section(settings, "sandbox", "filesystem"),
            _section(settings, "permissions"),
        )
        lists = {
            "sandbox.filesystem.denyWrite": filesystem.get("denyWrite"),
            "sandbox.filesystem.allowWrite": filesystem.get("allowWrite"),
            "permissions.additionalDirectories": permissions.get("additionalDirectories"),
            "permissions.allow": permissions.get("allow"),
        }
        for key, value in lists.items():
            entries = _strings(value)
            if entries is None:
                return Check(name, None, f"{key} in {path} is not a list of strings")
            for entry in entries:
                if key == "sandbox.filesystem.denyWrite":
                    if _reaches(entry, store, ctx.user_home, exact=True):
                        detail = f"on, and {key} {shown(entry)} in {path} denies writes to {store}"
                        return Check(name, True, detail)
                    continue
                target = entry
                if key == "permissions.allow":
                    rule = _EDIT_RULE.fullmatch(entry)
                    # Edit rules write an absolute path as //path; /path is relative to the project.
                    if rule is None or not rule["path"].startswith(("//", "~/")):
                        continue
                    target = rule["path"].removeprefix("/")
                if _reaches(target, store, ctx.user_home):
                    allows.append((key, entry, path))
    if allows:
        key, entry, path = allows[0]
        return Check(name, False, f"{key} {shown(entry)} in {path} lets Bash commands write {store}")
    return Check(name, True, f"on, and no write rule reaches {store}")


def _canary_payload(session: str, cwd: str) -> str:
    return json.dumps(
        {
            "session_id": session,
            "cwd": cwd,
            "hook_event_name": "PostToolUseFailure",
            "tool_name": "Bash",
            "tool_input": {"command": "scars-canary"},
            "tool_use_id": "toolu_canary",
            "error": f"Exit code 1\n{CANARY_ERROR}",
        }
    )


DELIVERY = {
    None: "lessons are shown",
    paths.BASELINE: "log-only baseline (BASELINE): hooks record matches and show nothing",
    paths.ARMS: (
        "arms (ARMS): each session is on or off by its id; off sessions record matches and show nothing"
    ),
}


def run_canary(budget_ms: float = CANARY_BUDGET_MS) -> Check:
    """Run the hook in this process against a temporary home with one canary lesson."""
    saved = {k: os.environ.get(k) for k in ("SCARS_HOME", "SCARS_OFF", "SCARS_ARM")}
    with tempfile.TemporaryDirectory(prefix="scars-canary-") as tmp:
        home = Path(tmp) / "home"
        db = store.init_lessons(home / "lessons.db")
        sig = signature(CANARY_ERROR)
        assert sig is not None
        trigger = Trigger(TriggerKind.ERROR, sig, Stage.REPAIR)
        lesson_id = create_pinned(db, CANARY_TEXT, (trigger,), "canary", actor="human", at=time.time())
        db.close()
        store.init_telemetry(home / "telemetry.db").close()
        os.environ["SCARS_HOME"] = str(home)
        os.environ.pop("SCARS_OFF", None)
        os.environ.pop("SCARS_ARM", None)
        outputs: list[str] = []
        elapsed = 0.0
        error = None
        try:
            # The first run is untimed: it pays one-off costs (first git spawn, cold page cache) that
            # say nothing about the hook. Each run uses its own session so deduplication cannot hide it.
            for session in ("scars-doctor-warmup", "scars-doctor-canary"):
                out = io.StringIO()
                started = time.perf_counter()
                hook.run(
                    Harness.CLAUDE, "PostToolUseFailure", io.StringIO(_canary_payload(session, tmp)), out
                )
                elapsed = (time.perf_counter() - started) * 1000
                outputs.append(out.getvalue())
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    if error is not None:
        return Check("canary", False, f"the hook raised {shown(error)}")
    if not all(lesson_id in out for out in outputs):
        return Check("canary", False, "the hook did not return the canary lesson")
    if elapsed > budget_ms:
        return Check("canary", False, f"the canary lesson took {elapsed:.0f} ms, over {budget_ms:.0f} ms")
    return Check("canary", True, f"lesson returned in {elapsed:.0f} ms")


def _store_checks(ctx: Context, scars_home: Path, location: Callable[[Path], str | None]) -> list[Check]:
    checks = []
    lessons = scars_home / "lessons.db"
    if not lessons.exists():
        return [Check("store", False, f"no store at {scars_home}; run scars init")]
    problem = location(scars_home)
    checks.append(
        Check("store location", problem is None, f"{scars_home}: {problem}" if problem else str(scars_home))
    )
    try:
        db = connect_readonly(lessons, "lessons")
        try:
            problems = store.verify(db)
            refused = unrenderable(db, store.read_lessons(db)[0])
            to_review = store.signature_reviews(db)
        finally:
            db.close()
    except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
        checks.append(Check("event chain", False, f"cannot read the lessons store: {shown(str(exc))}"))
        return checks
    if problems:
        listed = "; ".join(shown(p) for p in problems[:3])
        more = f"; and {len(problems) - 3} more" if len(problems) > 3 else ""
        checks.append(Check("event chain", False, listed + more))
    else:
        checks.append(Check("event chain", True, "hash chain and replay match the lessons"))
    if refused:
        listed = "; ".join(f"{shown(lesson_id)}: {exc.rule} in {exc.field}" for lesson_id, exc in refused[:3])
        more = f"; and {len(refused) - 3} more" if len(refused) > 3 else ""
        checks.append(Check("lessons render", False, listed + more))
    else:
        checks.append(Check("lessons render", True, "every active lesson renders"))
    if to_review:
        listed = "; ".join(
            f"{shown(lesson_id)}: error trigger made by older signature rules" for lesson_id in to_review[:3]
        )
        more = f"; and {len(to_review) - 3} more" if len(to_review) > 3 else ""
        checks.append(Check("signature rules", False, listed + more))
    else:
        checks.append(Check("signature rules", True, "every error trigger follows the current rules"))
    return checks


def doctor(
    ctx: Context,
    *,
    location: Callable[[Path], str | None] | None = None,
    canary: Callable[[], Check] | None = None,
) -> list[Check | str]:
    """Report lines and checks in print order. The location probe and canary default to the real ones."""
    location = location or store_location_problem
    canary = canary or run_canary
    scars_home = paths.home()
    out: list[Check | str] = []
    hits: list[telemetry.Hit] = []
    tele_path = scars_home / "telemetry.db"
    reports = None
    if tele_path.exists() and (schema := _telemetry_schema(tele_path)) is not None:
        out.append(schema)
    try:
        if tele_path.exists():
            tele = connect_readonly(tele_path, "hits")
            try:
                hits = telemetry.hits_since(tele, ctx.clock() - DAY)
            finally:
                tele.close()
        reports = {harness: harness_report(hits, harness) for harness in Harness}
    except (sqlite3.Error, OSError, ValueError, TypeError, OverflowError) as exc:
        out.append(Check("telemetry", False, f"cannot read telemetry.db: {shown(str(exc))}"))
        reports = {harness: harness_report([], harness) for harness in Harness}
    plugins = _plugin_state(ctx, scars_home)
    for harness in Harness:
        line, healthy = reports[harness]
        out.append(f"{line}; hooks {plugins[harness]}")
        if not healthy:
            out.append(
                Check(f"{harness} hook errors", False, "more than 1% of runs failed in the last 24 hours")
            )
    if not any(state == "installed" for state in plugins.values()):
        out.append(Check("hooks", False, "no harness has scars hooks installed"))
    else:
        out.append(_hook_store(ctx, scars_home))
    if (sandbox := claude_sandbox(ctx, scars_home)) is not None:
        out.append(sandbox)
    off = paths.is_off() or paths.off_value(ctx.env.get("SCARS_OFF"))
    out.append(Check("kill switch", not off, "on: hooks do nothing (scars on)" if off else "off"))
    out.append(Check("delivery", True, DELIVERY[paths.delivery_mode()]))
    out.extend(_store_checks(ctx, scars_home, location))
    out.append(canary())
    return out


def _telemetry_schema(path: Path) -> Check | None:
    """Report a telemetry.db older than this scars, without writing it: doctor only reads. None when the
    file is current, or unreadable, which the hits read reports."""
    current = store.TELEMETRY_SCHEMA_VERSION
    try:
        db = connect_readonly(path, "hits")
        try:
            found = db.execute("pragma user_version").fetchone()[0]
        finally:
            db.close()
    except (sqlite3.Error, OSError):
        return None
    if not 0 < found < current:
        return None
    detail = f"version {found} is older than this scars (version {current}); "
    return Check("telemetry schema", True, detail + "scars mine or scars init upgrades it")


def cmd_doctor(_: argparse.Namespace, ctx: Context) -> int:
    failed = False
    for item in doctor(ctx):
        if isinstance(item, str):
            ctx.print(item)
            continue
        label = {True: "ok  ", False: "FAIL", None: "unknown"}[item.ok]
        ctx.print(f"{label}  {item.name}: {clean(item.detail)}")
        failed |= item.ok is False
    return 1 if failed else 0
