"""The precision check over a batch's call window, with each command parsed once.

`match.precision` replays one trigger over every call and parses each call's command again for every
trigger, so a batch costs lessons times calls parses. `CallIndex` parses each shell command once, groups
the simple commands by program, and replays a command trigger only over the simple commands whose program
its pattern can name. The counts are the ones `match.precision` returns for the same calls: a call counts
once when any of its simple commands matches, and a path trigger is checked with `match.call_matches`.
Results are memoized per trigger and signature, since the window does not change during a run.
"""

from collections.abc import Callable, Iterable
from dataclasses import replace

from scars.match import (
    CommandPattern,
    ExactArg,
    InvalidTrigger,
    PathPattern,
    Precision,
    RecordedCall,
    call_matches,
    command_matches,
    parse_spec,
)
from scars.model import ErrorSignature, ToolKind, Trigger
from scars.shell import SimpleCommand, Unsupported, parse


class CallIndex:
    def __init__(self, calls: Iterable[RecordedCall], root_of: Callable[[str], str | None]) -> None:
        self._calls = list(calls)
        self._root_of = root_of
        self._by_program: dict[str, list[tuple[int, SimpleCommand]]] = {}
        self._memo: dict[tuple[Trigger, str], Precision] = {}
        self._with_roots: list[RecordedCall] | None = None
        parsed: dict[str, tuple[SimpleCommand, ...]] = {}
        for i, call in enumerate(self._calls):
            if call.tool_kind is not ToolKind.SHELL or call.command is None:
                continue
            if call.command not in parsed:
                try:
                    parsed[call.command] = tuple(parse(call.command))
                except Unsupported:
                    parsed[call.command] = ()
            for simple in parsed[call.command]:
                self._by_program.setdefault(simple.program, []).append((i, simple))

    def _command_hits(self, pattern: CommandPattern) -> set[int]:
        if isinstance(pattern.program, ExactArg):
            programs = [pattern.program.text] if pattern.program.text in self._by_program else []
        else:
            programs = [p for p in self._by_program if p.startswith(pattern.program.text)]
        return {i for p in programs for i, simple in self._by_program[p] if command_matches(pattern, simple)}

    def _path_hits(self, trigger: Trigger) -> set[int]:
        if self._with_roots is None:
            self._with_roots = [
                replace(c, repo_root=self._root_of(c.cwd)) if c.paths and c.repo_root is None else c
                for c in self._calls
            ]
        return {i for i, c in enumerate(self._with_roots) if c.paths and call_matches(trigger, c)}

    def precision(self, trigger: Trigger, error_signature: ErrorSignature) -> Precision:
        """`match.precision(trigger, calls, error_signature=...)`, with path calls given their repo root.

        Raises `InvalidTrigger` for a malformed spec and `ValueError` for a kind other than command or path.
        """
        key = (trigger, error_signature)
        if key not in self._memo:
            match parse_spec(trigger.kind, trigger.spec):
                case CommandPattern() as pattern:
                    hits = self._command_hits(pattern)
                case PathPattern():
                    hits = self._path_hits(trigger)
                case _:
                    raise ValueError(f"precision applies to command and path triggers, not {trigger.kind}")
            passing = sum(1 for i in hits if self._calls[i].succeeded)
            failing = sum(
                1
                for i in hits
                if not self._calls[i].succeeded and self._calls[i].error_signature == error_signature
            )
            self._memo[key] = Precision(failing, passing, len(hits), len(self._calls))
        return self._memo[key]


__all__ = ["CallIndex", "InvalidTrigger"]
