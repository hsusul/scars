import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass

from scars import __version__, hook


@dataclass(frozen=True)
class Command:
    name: str
    help: str
    handler: Callable[[argparse.Namespace], int] | None = None


def _not_implemented(name: str) -> Callable[[argparse.Namespace], int]:
    def handler(_: argparse.Namespace) -> int:
        print(f"scars {name}: not implemented yet", file=sys.stderr)
        return 2

    return handler


PHASE_1_COMMANDS = (
    Command("init", "install hooks, register repos, mine history and show the first candidates"),
    Command("inbox", "review waiting candidates one keystroke at a time"),
    Command("promote", "promote a lesson so it can reach other sessions"),
    Command("pin", "inject a promoted lesson at session start"),
    Command("unpin", "stop injecting a lesson at session start"),
    Command("level", "set a promoted lesson's level to note or warn"),
    Command("ls", "list lessons"),
    Command("why", "explain why a lesson fired"),
    Command("test", "replay a lesson's triggers against recent tool calls"),
    Command("mute", "stop a lesson from firing"),
    Command("retire", "retire a lesson"),
    Command("doctor", "check hook health, sandbox coverage and store integrity"),
    Command("bench", "measure full-hook latency under load"),
    Command("off", "turn every scars hook off on the next tool call"),
    Command("on", "turn scars hooks back on"),
    Command("mine", "read new transcript bytes and update lessons"),
    Command("import-guard", "turn an existing guard hook's deny reasons into pinned lessons"),
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scars", description="Lessons from past agent sessions.")
    parser.add_argument("--version", action="version", version=f"scars {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    for command in PHASE_1_COMMANDS:
        p = sub.add_parser(command.name, help=command.help)
        p.set_defaults(handler=command.handler or _not_implemented(command.name))
    hook_parser = sub.add_parser("hook", help="entry point that agent hooks call")
    hook_parser.add_argument("harness", choices=["claude", "codex"])
    hook_parser.add_argument("event")
    hook_parser.set_defaults(handler=lambda args: hook.main(args.harness, args.event))
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    return args.handler(args)
