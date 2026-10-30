"""Turn one failure-then-fix pair into a candidate lesson, or a named reason why not.

- `avoid` is the failed command reduced to its program plus the arguments the passing command does not
  have, shell-quoted and never backticked.
- `instead` is `run `<passing command>` instead`, with the passing command normalized, so it equals the
  observed command the render check compares it with.
- `reason` is the error signature.
- Triggers: the `error` trigger at the repair stage, plus a `command` pre-trigger at the plan stage made
  from the program and its distinguishing arguments (`prog ** arg1 ** arg2`), when those form a valid
  spec. An argument after `--`, one the shell expands at run time, or one holding a space, `*` or a
  leading `!` cannot be written as a spec token and is left out of the pre-trigger.
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
    LessonText,
    RepoId,
    RepoScope,
    Scope,
    SessionId,
    SessionScope,
    Stage,
    ToolKind,
    Trigger,
    TriggerKind,
    TrustClass,
)
from scars.shell import SimpleCommand, Unsupported, parse

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
    """Both inputs secret-scanned and capped at 1 KB, as stored in an observation."""


def fingerprint(scope: Scope, sig: str) -> str:
    repo_id = scope.repo_id if isinstance(scope, RepoScope) else ""
    session_id = scope.session_id if isinstance(scope, SessionScope) else ""
    parts = (scope.kind, repo_id, "", session_id, sig)
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def _first(command: str) -> SimpleCommand:
    try:
        commands = parse(command)
    except Unsupported:
        raise Skipped("unparseable_command") from None
    if not commands:
        raise Skipped("unparseable_command")
    return commands[0]


def _spec_token(arg: str) -> bool:
    return bool(arg) and not any(c.isspace() for c in arg) and "*" not in arg and not arg.startswith("!")


def pre_trigger_spec(failed: SimpleCommand, differing: frozenset[str]) -> str | None:
    """`prog ** a ** b` for the failed command's distinguishing arguments, or None when no valid spec."""
    if not _spec_token(failed.program) or "/" in failed.program:
        return None
    tokens = [failed.program]
    for i, arg in enumerate(failed.args):
        if arg == "--" or i in failed.uncertain:
            break
        if arg in differing and _spec_token(arg):
            tokens += ["**", arg]
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


def build(pair: Pair, repo_id: RepoId | None, owner_class: str | None) -> Candidate:
    """Raise `Skipped` or `render.RejectedText`, or return a checked candidate."""
    failed, passing = pair.failed, pair.passing
    if passing.tool_kind is not ToolKind.SHELL or passing.command is None:
        raise Skipped("non_shell_tool")
    if render.normalize_command(failed.input) == render.normalize_command(passing.input):
        raise Skipped("identical_retry")
    failed_cmd, passing_cmd = _first(failed.input), _first(passing.input)
    differing = [a for a in failed_cmd.args if a not in set(passing_cmd.args)]
    avoid = " ".join([failed_cmd.program, *(shlex.quote(a) for a in differing)])
    command = render.normalize_command(passing.input)
    instead = INSTEAD_TEMPLATE.format(command=command)
    scope: Scope = RepoScope(repo_id) if repo_id else SessionScope(SessionId(failed.session_id))
    trust = TrustClass.THIRD_PARTY_REPO if owner_class == "upstream" else TrustClass.OWN_CODE
    text = render.check_fields(
        avoid, instead, failed.signature, scope, known_commands=frozenset({command}), trust_class=trust
    )
    for field, value in (("failed_input", failed.input), ("passing_input", passing.input)):
        if (rule := secret_rule(value)) is not None:
            raise render.RejectedText(rule, field)
    try:
        parse_spec(TriggerKind.ERROR, failed.signature)
    except InvalidTrigger:
        raise Skipped("invalid_error_trigger") from None
    spec = pre_trigger_spec(failed_cmd, frozenset(differing))
    return Candidate(
        fingerprint=fingerprint(scope, failed.signature),
        scope=scope,
        trust_class=trust,
        text=text,
        error_trigger=Trigger(TriggerKind.ERROR, failed.signature, Stage.REPAIR),
        pre_trigger=Trigger(TriggerKind.COMMAND, spec, Stage.PLAN) if spec else None,
        fix_program=passing_cmd.program,
        signature=failed.signature,
        failed_input=_cap(failed.input),
        passing_input=_cap(passing.input),
    )
