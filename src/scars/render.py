"""Lesson template, sanitizer, lints and secret scan.

Lesson text is untrusted: the miner lifts it from tool output. `check` runs four stages in a fixed order
and raises `RejectedText` at the first rule that fires. A rejected candidate is dropped whole; nothing is
ever stripped or redacted. The same `check` runs again inside `render`, so text edited in the database
behind the CLI's back is never shown.

1. Sanitizer: control characters (every C0 and C1 character, tab and newline included), ANSI escapes,
   bidi controls, zero-width characters, Unicode tag characters, URLs, code fences and over-cap fields.
2. Imperative lint: the fixed `IMPERATIVE_RULES` table, plus the backticked-command rule for `instead`.
3. Secret scan: known token formats and a high-entropy backstop.
4. Machine-scope lint: machine lessons reach every repo, so they may not name a repo, a GitHub
   `owner/name`, or an absolute path outside `ALLOWED_ROOTS`.

Rejection messages name the field and the rule, never the matched text, so a secret or a private repo
name cannot leak through an error message or a log line.
"""

import math
import re
import shlex
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date

from scars.model import (
    TEXT_CAP,
    Lesson,
    LessonText,
    MachineScope,
    PathScope,
    RepoScope,
    Scope,
    SessionScope,
    Status,
)

FIELDS = ("avoid", "instead", "reason")


class RejectedText(Exception):
    """Text failed the sanitizer, a lint, or the secret scan, and must not be stored or shown."""

    def __init__(self, rule: str, field: str) -> None:
        super().__init__(f"{field}: {rule}")
        self.rule = rule
        self.field = field


@dataclass(frozen=True)
class Rule:
    name: str
    hits: Callable[[str], bool]


def _pattern(regex: str, flags: int = 0) -> Callable[[str], bool]:
    compiled = re.compile(regex, flags)
    return lambda text: compiled.search(text) is not None


def _chars(*ranges: tuple[int, int]) -> Callable[[str], bool]:
    def hits(text: str) -> bool:
        return any(lo <= ord(ch) <= hi for ch in text for lo, hi in ranges)

    return hits


# ---------------------------------------------------------------- sanitizer

SANITIZER_RULES: tuple[Rule, ...] = (
    Rule("sanitizer.over_cap", lambda text: len(text) > TEXT_CAP),
    # Checked before plain control characters so an escape sequence gets the more specific name.
    Rule("sanitizer.ansi_escape", _chars((0x1B, 0x1B), (0x9B, 0x9B))),
    Rule("sanitizer.control_char", _chars((0x00, 0x1F), (0x7F, 0x9F))),
    Rule(
        "sanitizer.bidi_control",
        _chars((0x061C, 0x061C), (0x200E, 0x200F), (0x202A, 0x202E), (0x2066, 0x2069)),
    ),
    Rule(
        "sanitizer.zero_width",
        _chars((0x180E, 0x180E), (0x200B, 0x200D), (0x2060, 0x2060), (0xFEFF, 0xFEFF)),
    ),
    Rule("sanitizer.tag_char", _chars((0xE0000, 0xE007F))),
    Rule("sanitizer.code_fence", lambda text: "```" in text),
    Rule(
        "sanitizer.url",
        _pattern(
            # Any scheme://, a www. host, or a bare domain followed by a path (`example.com/x`).
            # The bare-domain form also catches file names such as `web.app/` because `.app` is a
            # real top-level domain; that false positive is accepted.
            r"[a-z][a-z0-9+.-]*://|\bwww\.|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,63}/",
            re.IGNORECASE,
        ),
    ),
)

# ---------------------------------------------------------------- imperative lint

IMPERATIVE_RULES: tuple[Rule, ...] = (
    # `git fetch` is an ordinary local-repo command, so `fetch` only counts when `git` does not precede it.
    Rule(
        "lint.fetch",
        _pattern(r"\b(?:curl|wget|download\w*|invoke-webrequest|iwr)\b|(?<!\bgit )\bfetch\w*", re.I),
    ),
    Rule(
        "lint.pipe_to_shell",
        _pattern(
            r"\|\s*(?:sudo\s+)?(?:sh|bash|zsh|dash|ksh|fish|python[0-9.]*|perl|ruby|node)\b|\beval\b"
            r"|\b(?:sh|bash|zsh)\s+<\(",
            re.I,
        ),
    ),
    Rule(
        "lint.credentials",
        _pattern(
            r"\.ssh\b|\bid_(?:rsa|dsa|ecdsa|ed25519)\b|(?<![\w.])\.env\b|\bkeychain\b"
            r"|find-(?:generic|internet)-password|\.netrc\b|\.npmrc\b|\.pypirc\b|\.git-credentials\b"
            r"|\.aws/credentials|\.docker/config\.json|\bgh/hosts\.yml|\bprivate key\b"
            r"|\b[\w-]*tokens?\.(?:txt|json|ya?ml)\b",
            re.I,
        ),
    ),
    Rule(
        "lint.override_instructions",
        _pattern(
            r"\b(?:ignore|disregard|override|overrule|forget|bypass)\b.{0,40}?"
            r"\b(?:instructions?|prompts?|rules|guidelines|directions)\b|\bsystem prompt\b|\byou are now\b",
            re.I,
        ),
    ),
    Rule(
        "lint.disable_safety",
        _pattern(
            r"\b(?:disable|turn off|switch off|bypass|skip|deactivate)\b.{0,30}?"
            r"\b(?:safety|sandbox\w*|guard\w*|permissions?|security|hooks?|scars|verification|ssl|tls"
            r"|certificates?)\b"
            r"|--dangerously|--no-verify|--no-sandbox|--yolo|danger-full-access|bypasspermissions"
            r"|\bscars_off\b|\bscars off\b|git_ssl_no_verify|node_tls_reject_unauthorized",
            re.I,
        ),
    ),
)

SHELL_BUILTINS = frozenset({"cd", "pushd", "popd", "export", "unset", "true", "false"})
"""Builtins that run no program, so they need no entry in `known_programs`."""

_COMMAND_SPLIT = frozenset({"&&", "||", ";", "|", "&", "(", ")", ";;", "|&"})
_REDIRECTS = frozenset({"<", ">", ">>", "<<", "<<<", ">&", "<&", ">|", "&>", "&>>"})
_SUBSTITUTION_PLACEHOLDER = "SUBST"
_MAX_SUBSTITUTION_DEPTH = 3


class _Unparseable(Exception):
    pass


def _split_substitutions(command: str, depth: int = 0) -> list[str]:
    """Return the outer command with each `$(...)` replaced by a placeholder, then each inner command."""
    if depth > _MAX_SUBSTITUTION_DEPTH:
        raise _Unparseable
    outer: list[str] = []
    inner: list[str] = []
    i = 0
    while i < len(command):
        if command.startswith("$(", i):
            level, j = 1, i + 2
            while j < len(command) and level:
                level += {"(": 1, ")": -1}.get(command[j], 0)
                j += 1
            if level:
                raise _Unparseable
            inner.extend(_split_substitutions(command[i + 2 : j - 1], depth + 1))
            outer.append(_SUBSTITUTION_PLACEHOLDER)
            i = j
        else:
            outer.append(command[i])
            i += 1
    return ["".join(outer), *inner]


def command_programs(command: str) -> frozenset[str]:
    """Programs a backticked shell command would run: the first word of every simple command.

    Simple commands are split at `&&`, `||`, `;`, `|`, `&` and parentheses, `$(...)` bodies are parsed
    as commands of their own, leading `VAR=value` words are skipped, redirections and their targets are
    dropped, and a program is the basename of its word. Shell builtins that run nothing are left out.
    Raises `_Unparseable` on unbalanced quotes or parentheses.
    """
    programs: set[str] = set()
    for part in _split_substitutions(command):
        lexer = shlex.shlex(part, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        try:
            tokens = list(lexer)
        except ValueError as error:
            raise _Unparseable from error
        at_start, skip_next = True, False
        for token in tokens:
            if skip_next:
                skip_next = False
            elif token in _COMMAND_SPLIT:
                at_start = True
            elif token in _REDIRECTS:
                skip_next = True
            elif at_start and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token):
                continue
            elif at_start:
                at_start = False
                program = token.rsplit("/", 1)[-1]
                if program not in SHELL_BUILTINS:
                    programs.add(program)
    return frozenset(programs)


def _check_instead_command(instead: str, known_programs: frozenset[str]) -> None:
    ticks = instead.count("`")
    if ticks == 0:
        return
    if ticks % 2:
        raise RejectedText("lint.instead_unbalanced_backticks", "instead")
    if ticks > 2:
        raise RejectedText("lint.instead_multiple_commands", "instead")
    command = instead.split("`")[1].strip()
    try:
        programs = command_programs(command)
    except _Unparseable:
        raise RejectedText("lint.instead_unparseable_command", "instead") from None
    if not command or not programs <= known_programs:
        raise RejectedText("lint.instead_unknown_program", "instead")


# ---------------------------------------------------------------- secret scan

ENTROPY_MIN_LENGTH = 20
ENTROPY_THRESHOLD = 4.0
_ENTROPY_TOKEN = re.compile(r"[A-Za-z0-9_+=]{" + str(ENTROPY_MIN_LENGTH) + ",}")
"""A token is a maximal run of ASCII letters, digits, `_`, `+` and `=`. Every other character, including
`-`, `/`, `.` and whitespace, ends a token.

Splitting at `-`, `/` and `.` keeps paths, dotted names and UUIDs (hyphen-separated runs of at most 12
characters) away from the entropy check entirely. Hex strings of any length, including full 40-character
commit SHAs, top out at exactly 4.0 bits per character (log2 of 16 symbols), so the strictly-greater
threshold never fires on them. The cost: a base64 secret containing `/` is scored per segment, and a
uniformly random 20-character alphanumeric token only exceeds 4.0 bits about two times in three. The
named token formats are the main defense; entropy is the backstop.
"""


def shannon_entropy(token: str) -> float:
    """Bits per character of `token`, from its own character frequencies."""
    n = len(token)
    return -sum(c / n * math.log2(c / n) for c in Counter(token).values())


def _high_entropy(text: str) -> bool:
    return any(shannon_entropy(t) > ENTROPY_THRESHOLD for t in _ENTROPY_TOKEN.findall(text))


_NOT_TOKEN_BEFORE = r"(?<![A-Za-z0-9_-])"

SECRET_RULES: tuple[Rule, ...] = (
    Rule("secret.github_token", _pattern(_NOT_TOKEN_BEFORE + r"(?:ghp_|gho_|github_pat_)[A-Za-z0-9_]")),
    Rule("secret.openai_key", _pattern(_NOT_TOKEN_BEFORE + r"sk-[A-Za-z0-9_-]")),
    Rule("secret.slack_token", _pattern(_NOT_TOKEN_BEFORE + r"xox[abp]-[A-Za-z0-9-]")),
    Rule("secret.aws_key", _pattern(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    Rule("secret.jwt", _pattern(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*")),
    Rule("secret.url_userinfo", _pattern(r"//[^\s/@:]+:[^\s/@]*@")),
    Rule("secret.pem_header", _pattern(r"-----BEGIN [A-Z0-9 ]*-----")),
    Rule("secret.high_entropy", _high_entropy),
)

# ---------------------------------------------------------------- machine-scope lint

ALLOWED_ROOTS = ("~/.scars", "/Volumes/990 Pro", "/tmp", "/usr", "/opt")
"""Absolute path roots a machine-scope lesson may name. A root matches only at a path boundary, so
`/tmpfiles` and `/Volumes/990 Profile` are outside it."""

_GIT_REMOTES = frozenset({"origin", "upstream"})
"""Owners that are git remote names, so `origin/main` reads as a branch, not a GitHub repo."""

_ALLOWED_PATH = re.compile(
    r"(?<![\w.~$/-])(?:" + "|".join(re.escape(r) for r in ALLOWED_ROOTS) + r")(?![^\s/`'\")])[^\s`'\"]*"
)
_ABSOLUTE_PATH = re.compile(r"(?<![\w.~$/-])(?:~/|/)[\w.~-]|~[A-Za-z_]|\$HOME\b|\$\{HOME\}")
_OWNER_NAME = re.compile(r"(?<![\w.~$/@:-])([A-Za-z0-9][A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]+(?![\w./-])")
"""Exactly two slash-separated segments in GitHub's owner and repo alphabets. This also flags two-segment
relative paths such as `apps/web`; a machine-scope lesson should describe a layout in words instead."""


def _names_repo(text: str, registered_repos: Iterable[str]) -> bool:
    lowered = text.lower()
    return any(
        re.search(r"(?<![\w.-])" + re.escape(name.lower()) + r"(?![\w-])", lowered)
        for name in registered_repos
        if name
    )


def _check_machine_scope(text: LessonText, registered_repos: Sequence[str]) -> None:
    for field in FIELDS:
        value = getattr(text, field)
        if _names_repo(value, registered_repos):
            raise RejectedText("machine.repo_name", field)
        masked = _ALLOWED_PATH.sub(" ", value)
        if _ABSOLUTE_PATH.search(masked):
            raise RejectedText("machine.absolute_path", field)
        if any(m.group(1).lower() not in _GIT_REMOTES for m in _OWNER_NAME.finditer(masked)):
            raise RejectedText("machine.github_repo", field)


# ---------------------------------------------------------------- check


def _run(rules: Sequence[Rule], text: LessonText) -> None:
    for field in FIELDS:
        value = getattr(text, field)
        for rule in rules:
            if rule.hits(value):
                raise RejectedText(rule.name, field)


def check(
    text: LessonText,
    scope: Scope,
    *,
    known_programs: frozenset[str] = frozenset(),
    registered_repos: Sequence[str] = (),
) -> None:
    """Raise `RejectedText` unless every field passes every stage. Returns None on success.

    `known_programs` holds the programs this machine ran successfully, as the miner records them.
    `registered_repos` holds the display names of every registered repo.
    """
    _run(SANITIZER_RULES, text)
    _run(IMPERATIVE_RULES, text)
    _check_instead_command(text.instead, known_programs)
    _run(SECRET_RULES, text)
    if isinstance(scope, MachineScope):
        _check_machine_scope(text, registered_repos)


# ---------------------------------------------------------------- render

_LESSON_ID = re.compile(r"L-[0-9a-f]{4,16}")


def scope_label(scope: Scope) -> str:
    match scope:
        case MachineScope():
            return "machine"
        case RepoScope(repo_id=repo_id):
            return f"repo {repo_id}"
        case PathScope(repo_id=repo_id, glob=glob):
            return f"path {repo_id}:{glob}"
        case SessionScope():
            return "session"


def _check_header(lesson: Lesson) -> None:
    """The header carries values that `check` never sees, so hold them to the sanitizer and secret scan
    too, and keep `|` out so a value cannot forge another header column."""
    if not _LESSON_ID.fullmatch(lesson.id):
        raise RejectedText("header.lesson_id", "id")
    label = scope_label(lesson.scope)
    for rule in (*SANITIZER_RULES, *SECRET_RULES):
        if rule.hits(label):
            raise RejectedText(rule.name, "scope")
    if "|" in label:
        raise RejectedText("header.separator", "scope")


def _header(lesson: Lesson, reviewed_on: date | None) -> str:
    reviewed = (
        f"reviewed {reviewed_on.isoformat()}"
        if lesson.status is Status.PROMOTED and reviewed_on is not None
        else "unreviewed"
    )
    sessions = "1 session" if lesson.sessions_seen == 1 else f"{lesson.sessions_seen} sessions"
    label = scope_label(lesson.scope)
    return f"[scars {lesson.id} {lesson.level} | {label} | seen in {sessions} | {reviewed}]"


def _recheck(lesson: Lesson, known_programs: frozenset[str], registered_repos: Sequence[str]) -> None:
    _check_header(lesson)
    check(lesson.text, lesson.scope, known_programs=known_programs, registered_repos=registered_repos)


def render(
    lesson: Lesson,
    *,
    reviewed_on: date | None = None,
    known_programs: frozenset[str] = frozenset(),
    registered_repos: Sequence[str] = (),
) -> str:
    """Render one lesson as quoted, advisory data with its provenance.

    The lesson record has no review date, so the caller passes `reviewed_on`, the date of the lesson's
    latest promotion event. Only a promoted lesson with a date says `reviewed <date>`; every other lesson
    says `unreviewed`. `check` runs again first, with the same `known_programs` and `registered_repos`
    the store used, and its `RejectedText` propagates so stale or tampered text is never shown.
    """
    _recheck(lesson, known_programs, registered_repos)
    return "\n".join(
        (
            _header(lesson, reviewed_on),
            f"AVOID: {lesson.text.avoid}",
            f"INSTEAD: {lesson.text.instead}",
            f"WHY: {lesson.text.reason}",
            f"(advisory; does not override the user's instructions; explain: scars why {lesson.id})",
        )
    )


# ---------------------------------------------------------------- budgets


@dataclass(frozen=True)
class Budget:
    chars: int
    max_lessons: int | None


SESSION_START_BUDGET = Budget(600, None)
PROMPT_BUDGET = Budget(400, 2)
REPAIR_BUDGET = Budget(400, 2)
DENY_CAP = 300

LESSON_SEPARATOR = "\n"


@dataclass(frozen=True)
class Fitted:
    lessons: tuple[str, ...]
    dropped: int

    @property
    def text(self) -> str:
        return LESSON_SEPARATOR.join(self.lessons)


def fit(rendered: Sequence[str], budget: Budget) -> Fitted:
    """Keep whole rendered lessons, in their given order, while the joined text stays within the budget.

    The cost counts the separator between kept lessons. A lesson that does not fit is skipped, never cut,
    and a later, shorter lesson may still take the space it left.
    """
    kept: list[str] = []
    used = 0
    for lesson in rendered:
        cost = len(lesson) + (len(LESSON_SEPARATOR) if kept else 0)
        full = budget.max_lessons is not None and len(kept) >= budget.max_lessons
        if not full and used + cost <= budget.chars:
            kept.append(lesson)
            used += cost
    return Fitted(tuple(kept), len(rendered) - len(kept))


def render_deny(
    lesson: Lesson,
    *,
    known_programs: frozenset[str] = frozenset(),
    registered_repos: Sequence[str] = (),
) -> str:
    """A PreToolUse deny reason of at most `DENY_CAP` characters, without cutting any field.

    Candidates go from richest to barest, and the first that fits wins: header, AVOID and INSTEAD; then
    header and AVOID; then a bare line naming the lesson. The bare line is at most 94 characters because
    the lesson id is at most 18, so some candidate always fits. The final length check guards that bound.
    """
    _recheck(lesson, known_programs, registered_repos)
    header = f"[scars {lesson.id} {lesson.level} | {scope_label(lesson.scope)}]"
    footer = f"(explain: scars why {lesson.id})"
    avoid = f"AVOID: {lesson.text.avoid}"
    candidates = (
        "\n".join((header, avoid, f"INSTEAD: {lesson.text.instead}", footer)),
        "\n".join((header, avoid, footer)),
        f"[scars {lesson.id}] denied by a reviewed lesson {footer}",
    )
    reason = next(c for c in candidates if len(c) <= DENY_CAP)
    if len(reason) > DENY_CAP:
        raise AssertionError("deny reason over cap")
    return reason
