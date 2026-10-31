"""Turn one failure-then-fix pair into a candidate lesson, or a named reason why not.

- Only structured data built from the parsed calls goes into a lesson, never transcript prose. The failed
  and the passing input must each be one plain command (`render.plain_command`): a compound, wrapped,
  redirected or run-time-expanded command is not lessonable, and neither is an input holding the scars
  header, so a lesson shown to an agent never feeds back into a new one.
- `avoid` is the failed command reduced to its program plus the arguments the passing command does not
  have, written as `shlex.join` and never backticked. When the failed command has no such argument,
  `avoid` is the whole failed command, because the bare program name would warn against every use of it.
- `instead` is `run `<passing command>` instead`, with the passing command written as `shlex.join` of its
  words. The observation stores that same text as its passing input, so the hook's known commands always
  contain it; one over the 1 KB observation cap drops the candidate instead of being cut.
- `reason` is `render.MINED_REASON`. The error signature is kept only as the error trigger.
- Triggers: the `error` trigger at the repair stage, plus a `command` pre-trigger at the gate stage made
  from the program and its distinguishing arguments (`prog ** arg1 ** arg2`), when at least one such
  argument can be written as a spec token. An argument after `--`, one the shell expands at run time, or
  one holding a space, `*` or a leading `!` cannot, and is left out. A spec of the program alone would
  match every run of it, so then the lesson gets its error trigger only. The gate stage is the one that
  evaluates command triggers; the Phase 1 hook delivers nothing there, so the pre-trigger serves the
  precision check and outcome attribution until gate blocking ships.
- The would-be lesson must pass `render.check_budgets`, with its own command as the known command, so it
  still fits each stage's budget with the longest header it can reach (the store's 10-character ids).
- Scope is the repo of the passing call's working directory, or the session when that directory is in
  no registered clone. Repos registered as `upstream` give trust class `third_party_repo`.
- The fingerprint hashes the scope and the error signature, never the command text.
"""

import hashlib
import shlex
from dataclasses import dataclass

from scars import render
from scars.match import InvalidTrigger, parse_spec
from scars.mine.pairs import Pair
from scars.model import (
    ErrorSignature,
    Lesson,
    LessonId,
    LessonText,
    Level,
    RepoId,
    RepoScope,
    Scope,
    SessionId,
    SessionScope,
    Stage,
    Status,
    ToolKind,
    Trigger,
    TriggerKind,
    TrustClass,
)
from scars.shell import SimpleCommand, Unsupported, parse
from scars.signature import HEADER_PREFIX

INSTEAD_TEMPLATE = render.INSTEAD_TEMPLATES[2]
OBSERVATION_CAP = 1024


class Skipped(Exception):
    """The pair yields no lesson for a structural reason; `reason` is a short counter name."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Candidate:
    fingerprint: str
    scope: Scope
    trust_class: TrustClass
    text: LessonText
    error_trigger: Trigger
    pre_trigger: Trigger | None
    fix_program: str
    signature: ErrorSignature
    failed_input: str
    passing_input: str
    """Both inputs secret-scanned, as stored in an observation. The failed input is capped at 1 KB; the
    passing input is the normalized command quoted in `instead`, never cut."""


def fingerprint(scope: Scope, sig: str) -> str:
    repo_id = scope.repo_id if isinstance(scope, RepoScope) else ""
    session_id = scope.session_id if isinstance(scope, SessionScope) else ""
    parts = (scope.kind, repo_id, "", session_id, sig)
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def _plain(command: str) -> SimpleCommand:
    if HEADER_PREFIX in command.lower():
        raise Skipped("scars_header")
    try:
        commands = parse(command)
    except Unsupported:
        raise Skipped("unparseable_command") from None
    if not commands:
        raise Skipped("unparseable_command")
    if len(commands) > 1:
        raise Skipped("compound_command")
    simple = render.plain_command(command)
    if simple is None:
        raise Skipped("not_plain_command")
    return simple


def _spec_token(arg: str) -> bool:
    return bool(arg) and not any(c.isspace() for c in arg) and "*" not in arg and not arg.startswith("!")


def pre_trigger_spec(failed: SimpleCommand, differing: frozenset[str]) -> str | None:
    """`prog ** a ** b` for the failed command's distinguishing arguments, or None when no valid spec
    keeps at least one argument."""
    if not _spec_token(failed.program) or "/" in failed.program:
        return None
    tokens = [failed.program]
    for i, arg in enumerate(failed.args):
        if arg == "--" or i in failed.uncertain:
            break
        if arg in differing and _spec_token(arg):
            tokens += ["**", arg]
    if len(tokens) == 1:
        return None
    spec = " ".join(tokens)
    try:
        parse_spec(TriggerKind.COMMAND, spec)
    except InvalidTrigger:
        return None
    return spec


def secret_rule(text: str) -> str | None:
    """The name of the first secret rule `text` trips, or None."""
    return next((rule.name for rule in render.SECRET_RULES if rule.hits(text)), None)


def _cap(text: str) -> str:
    raw = text.encode()
    return text if len(raw) <= OBSERVATION_CAP else raw[:OBSERVATION_CAP].decode(errors="ignore")


_PREVIEW_ID = LessonId("L-00000000")


def _check_budgets(
    text: LessonText, scope: Scope, trust: TrustClass, triggers: tuple[Trigger, ...], command: str
) -> None:
    preview = Lesson(
        id=_PREVIEW_ID,
        revision=1,
        status=Status.CANDIDATE,
        level=Level.NOTE,
        pinned=False,
        scope=scope,
        text=text,
        trust_class=trust,
        triggers=triggers,
        fingerprint="",
        sessions_seen=1,
    )
    try:
        render.check_budgets(preview, known_commands=frozenset({command}))
    except render.RejectedText as exc:
        if not exc.rule.startswith("budget."):
            raise
        raise Skipped(f"over_{exc.rule.removeprefix('budget.')}_budget") from None


def build(pair: Pair, repo_id: RepoId | None, owner_class: str | None) -> Candidate:
    """Raise `Skipped` or `render.RejectedText`, or return a checked candidate."""
    failed, passing = pair.failed, pair.passing
    if passing.tool_kind is not ToolKind.SHELL or passing.command is None:
        raise Skipped("non_shell_tool")
    failed_cmd, passing_cmd = _plain(failed.input), _plain(passing.input)
    if failed_cmd.program != passing_cmd.program:
        raise Skipped("program_mismatch")
    if failed_cmd.args == passing_cmd.args:
        raise Skipped("identical_retry")
    differing = [a for a in failed_cmd.args if a not in set(passing_cmd.args)]
    avoid = shlex.join([failed_cmd.program, *(differing or failed_cmd.args)])
    command = render.plain_text(passing_cmd)
    if len(command.encode()) > OBSERVATION_CAP:
        raise Skipped("passing_input_too_long")
    instead = INSTEAD_TEMPLATE.format(command=command)
    scope: Scope = RepoScope(repo_id) if repo_id else SessionScope(SessionId(failed.session_id))
    trust = TrustClass.THIRD_PARTY_REPO if owner_class == "upstream" else TrustClass.OWN_CODE
    text = render.check_fields(
        avoid, instead, render.MINED_REASON, scope, known_commands=frozenset({command}), trust_class=trust
    )
    inputs = (("failed_input", failed.input), ("passing_input", passing.input), ("reason", failed.signature))
    for field, value in inputs:
        if (rule := secret_rule(value)) is not None:
            raise render.RejectedText(rule, field)
    try:
        parse_spec(TriggerKind.ERROR, failed.signature)
    except InvalidTrigger:
        raise Skipped("invalid_error_trigger") from None
    spec = pre_trigger_spec(failed_cmd, frozenset(differing))
    error_trigger = Trigger(TriggerKind.ERROR, failed.signature, Stage.REPAIR)
    pre_trigger = Trigger(TriggerKind.COMMAND, spec, Stage.GATE) if spec else None
    triggers = (error_trigger, *([pre_trigger] if pre_trigger else []))
    _check_budgets(text, scope, trust, triggers, command)
    return Candidate(
        fingerprint=fingerprint(scope, failed.signature),
        scope=scope,
        trust_class=trust,
        text=text,
        error_trigger=error_trigger,
        pre_trigger=pre_trigger,
        fix_program=passing_cmd.program,
        signature=failed.signature,
        failed_input=_cap(failed.input),
        passing_input=command,
    )
