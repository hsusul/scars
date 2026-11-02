"""The `scars` command line.

Each command parses its arguments here, then runs a handler in `scars.commands` with a `Context` that
stands for the terminal. Exit codes: 0 on success, 1 on a user error (one line on stderr) or a failed
`doctor` check, 2 for a command that is not built yet.

`HOME`, `SCARS_HOME` and the arguments must be UTF-8; anything else is a user error before the command
starts. A crash in a read command (`ls`, `why`, `test`, `doctor`, `learn`) prints one line naming the
exception type and exits 1: its traceback could quote stored text, so it goes to
`<scars home>/logs/cli.log`, and only when `SCARS_DEBUG` is set. Output never fails on encoding: what the
terminal cannot show is written as a backslash escape.
"""

import argparse
import os
import sys
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from importlib import import_module

from scars import __version__, hook, paths

Configure = Callable[[argparse.ArgumentParser], None]


def _none(_: argparse.ArgumentParser) -> None:
    return None


def _lesson_id(p: argparse.ArgumentParser) -> None:
    p.add_argument("id", help="lesson id, such as L-1a2b3c4d")


def _promote(p: argparse.ArgumentParser) -> None:
    _lesson_id(p)
    reach = p.add_mutually_exclusive_group()
    reach.add_argument("--machine", action="store_true", help="reach every repo on this machine")
    reach.add_argument("--repo", metavar="REPO_ID", help="reach this registered repo")


def _level(p: argparse.ArgumentParser) -> None:
    _lesson_id(p)
    p.add_argument("level", help="note or warn")


STATUS_CHOICES = ("candidate", "provisional", "promoted", "retired", "superseded", "all")


def _ls(p: argparse.ArgumentParser) -> None:
    p.add_argument("--status", choices=STATUS_CHOICES, help="one status, or all (default: active lessons)")
    p.add_argument("--repo", metavar="REPO_ID", help="only lessons scoped to this repo")
    p.add_argument("--firing", action="store_true", help="only lessons shown in the last 30 days")
    p.add_argument("--stale", action="store_true", help="only lessons neither seen nor shown in 30 days")


def _why(p: argparse.ArgumentParser) -> None:
    p.add_argument("id", help="lesson id, or last for the lesson shown most recently")


def _init(p: argparse.ArgumentParser) -> None:
    p.add_argument("--yes", action="store_true", help="answer yes; register only repos with no git remote")
    p.add_argument(
        "--allow-dev-install",
        action="store_true",
        help="install even when scars-hook lives inside a git checkout an agent could edit",
    )


def _mine(p: argparse.ArgumentParser) -> None:
    p.add_argument("--since", help="only transcripts modified since 30d, 12h or a date like 2026-10-01")


def _bench(p: argparse.ArgumentParser) -> None:
    p.add_argument("--runs", type=int, default=200, help="hook runs per case (default 200)")
    p.add_argument("--lessons", type=int, default=1000, help="lessons in the store (default 1000)")
    p.add_argument("--concurrency", type=int, default=16, help="concurrent hook processes (default 16)")
    p.add_argument("--cmd", help="hook command before `<harness> <event>` (default: scars-hook)")


def _learn(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dry-run", action="store_true", help="print each verdict and write nothing")


def _import_guard(p: argparse.ArgumentParser) -> None:
    p.add_argument("path", nargs="?", help="guard script (default ~/.claude/hooks/guard-bash.sh)")


@dataclass(frozen=True)
class Command:
    name: str
    help: str
    handler: str | None = None
    """`module:function` under `scars.commands`, imported only when the command runs."""
    configure: Configure = _none
    guarded: bool = False
    """Every output line goes through the sanitizer and secret scan: agents run these commands too."""


PHASE_1_COMMANDS = (
    Command(
        "init",
        "install hooks, register repos, mine history and show the first candidates",
        "install:cmd_init",
        _init,
    ),
    Command("inbox", "review waiting candidates one keystroke at a time", "inbox:cmd_inbox"),
    Command("promote", "promote a lesson so it can reach other sessions", "lifecycle:cmd_promote", _promote),
    Command("pin", "inject a promoted lesson at session start", "lifecycle:cmd_pin", _lesson_id),
    Command("unpin", "stop injecting a lesson at session start", "lifecycle:cmd_unpin", _lesson_id),
    Command("level", "set a promoted lesson's level to note or warn", "lifecycle:cmd_level", _level),
    Command("ls", "list lessons", "inspect:cmd_ls", _ls, guarded=True),
    Command("why", "explain why a lesson fired", "inspect:cmd_why", _why, guarded=True),
    Command(
        "test",
        "replay a lesson's triggers against recent tool calls",
        "inspect:cmd_test",
        _lesson_id,
        guarded=True,
    ),
    Command("mute", "stop a lesson from firing", "lifecycle:cmd_mute", _lesson_id),
    Command("retire", "retire a lesson", "lifecycle:cmd_retire", _lesson_id),
    Command(
        "doctor", "check hook health, store location and store integrity", "doctor:cmd_doctor", guarded=True
    ),
    Command("bench", "measure full-hook latency under load", "bench:cmd_bench", _bench),
    Command("off", "turn every scars hook off on the next tool call", "lifecycle:cmd_off"),
    Command("on", "turn scars hooks back on", "lifecycle:cmd_on"),
    Command("mine", "read new transcript bytes and update lessons", "mining:cmd_mine", _mine),
    Command(
        "learn",
        "ask claude to judge and rewrite candidates seen in 2 or more sessions",
        "learn:cmd_learn",
        _learn,
        guarded=True,
    ),
    Command(
        "import-guard",
        "turn an existing guard hook's deny reasons into pinned lessons",
        "import_guard:cmd_import_guard",
        _import_guard,
    ),
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scars", description="Lessons from past agent sessions.")
    parser.add_argument("--version", action="version", version=f"scars {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    for command in PHASE_1_COMMANDS:
        p = sub.add_parser(command.name, help=command.help, description=command.help)
        command.configure(p)
        p.set_defaults(handler=command.handler, guarded=command.guarded)
    hook_parser = sub.add_parser("hook", help="entry point that agent hooks call")
    hook_parser.add_argument("harness", choices=["claude"])
    hook_parser.add_argument("event")
    hook_parser.set_defaults(handler="hook")
    return parser


def _tolerant_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        with suppress(AttributeError, ValueError, OSError):
            stream.reconfigure(errors="backslashreplace")  # type: ignore[union-attr]


def _check_input(argv: list[str]) -> None:
    paths.check_env()
    for i, arg in enumerate(argv, 1):
        paths.text(arg, f"argument {i}")


def _log_crash() -> str | None:
    """Append the current traceback to the CLI log; the log's path, or None when it cannot be written."""
    import traceback

    logs = os.path.join(paths.home_path(), "logs")
    try:
        os.makedirs(logs, mode=0o700, exist_ok=True)
        log = os.path.join(logs, "cli.log")
        with open(log, "a", encoding="utf-8", errors="backslashreplace") as f:
            traceback.print_exc(file=f)
    except OSError:
        return None
    return log


def main(argv: list[str] | None = None, ctx: object = None) -> int:
    """Run one command. `ctx` replaces the terminal in tests; it must be a `scars.commands.Context`."""
    if ctx is None:
        _tolerant_output()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.handler == "hook":
        return hook.main(args.harness, args.event)
    if args.handler is None:
        print(f"scars {args.command}: not implemented yet", file=sys.stderr)
        return 2
    from scars.commands import Context, UserError

    context = ctx if isinstance(ctx, Context) else Context()
    if args.guarded:
        context = context.guarded()
    module, _, function = args.handler.partition(":")
    handler = getattr(import_module(f"scars.commands.{module}"), function)
    try:
        _check_input(sys.argv[1:] if argv is None else argv)
        return handler(args, context)
    except (UserError, paths.NotUtf8) as exc:
        print(f"scars {args.command}: {exc}", file=context.stderr)
        return 1
    except Exception as exc:
        if not args.guarded:
            raise
        where = "run with SCARS_DEBUG=1 for details"
        if context.env.get("SCARS_DEBUG") and (log := _log_crash()) is not None:
            where = f"traceback in {log}"
        print(f"scars {args.command}: internal error ({type(exc).__name__}); {where}", file=context.stderr)
        return 1
    finally:
        for stream in (context.stdout, context.stderr):
            if args.guarded:
                stream.close()  # a CleanLines: prints its last partial line, leaves the terminal open
            else:
                stream.flush()
