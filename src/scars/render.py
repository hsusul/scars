"""Lesson template, sanitizer, lints and secret scan.

Lesson text is untrusted: the miner lifts it from tool output. `check` runs four stages in a fixed order
and raises `RejectedText` at the first rule that fires. Within a stage, fields go in the order avoid,
instead, reason, and each field meets the stage's rules in table order. A rejected candidate is dropped
whole; nothing is ever stripped or redacted. The same checks run again inside `render`, so text edited
in the database behind the CLI's back is never shown.

The policy is blunt on purpose. These checks sit behind trust tiers and human review, so dropping a
harmless candidate is cheap and showing a harmful one is not. Where a rule cannot be complete, it is
written as an allowlist (printable ASCII only, known programs only, allowed path roots only) rather than
a list of bad phrases.

1. Sanitizer: empty and over-cap fields, ANSI escapes, control characters (every C0 and C1 character,
   tab and newline included), anything outside printable ASCII, code fences and URLs.
2. Imperative lint: the fixed `IMPERATIVE_RULES` table, plus the backticked-command rule for `instead`.
3. Secret scan: known token formats, matched wherever they start, and a high-entropy fallback.
4. Machine-scope lint: machine lessons reach every repo, so they may not name a repo, a GitHub
   `owner/name`, a `..` path segment, or an absolute path outside `ALLOWED_ROOTS`.

Rejection messages name the field and the rule, never the matched text, so a secret or a private repo
name cannot leak through an error message or a log line.
"""

import math
import re
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from scars.model import (
    TEXT_CAP,
    Lesson,
    LessonId,
    LessonText,
    MachineScope,
    PathScope,
    RepoScope,
    Scope,
    SessionScope,
    Status,
)
from scars.shell import Unsupported, parse

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
    Rule("sanitizer.empty", lambda text: text == ""),
    Rule("sanitizer.over_cap", lambda text: len(text) > TEXT_CAP),
    # Checked before plain control characters so an escape sequence gets the more specific name.
    Rule("sanitizer.ansi_escape", _chars((0x1B, 0x1B), (0x9B, 0x9B))),
    Rule("sanitizer.control_char", _chars((0x00, 0x1F), (0x7F, 0x9F))),
    # Printable ASCII only. This one rule covers Unicode line separators, non-breaking and other
    # Unicode spaces, homoglyphs, fullwidth forms, combining marks, bidi, zero-width and tag characters.
    Rule("sanitizer.non_ascii", lambda text: any(ord(ch) > 0x7E for ch in text)),
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

_OVERRIDE_VERBS = r"(?:ignor|disregard|forget|overrid|overrul|bypass|supersed)\w*"
_OVERRIDE_TARGETS = (
    r"(?:previous\w*|prior|above|earlier|everything|all|other|instructions?|rules?|prompts?|system"
    r"|guidelines?|directions?|directives?)"
)
_FEW_WORDS = r"\W+(?:\w+\W+){0,4}?"
_INTERPRETER_FLAG = r"\b.*?\s-[a-z]*"
_SECRET_NOUNS = r"(?:tokens?|secrets?|passwords?|passwd|credentials?|api[ _-]?keys?)"
_LEAK_VERBS = r"(?:print|cat|echo|send|upload|paste|dump|post|share|reveal|copy|show)\w*"

IMPERATIVE_RULES: tuple[Rule, ...] = (
    # `git fetch` is an ordinary local-repo command, so `fetch` only counts when `git` does not precede it.
    Rule(
        "lint.fetch",
        _pattern(r"\b(?:curl|wget|download\w*|invoke-webrequest|iwr)\b|(?<!\bgit )\bfetch\w*", re.I),
    ),
    Rule(
        "lint.pipe_to_shell",
        _pattern(
            r"\|\s*(?:sudo\s+)?(?:sh|bash|zsh|dash|ksh|csh|tcsh|fish|i?python[0-9.]*|py|perl|ruby|node"
            r"|deno|bun|osascript|pwsh|powershell|source)\b"
            r"|\b(?:sh|bash|zsh|dash|ksh|fish|source)\s+<\(|\bxargs\b.*?\b(?:sh|bash|zsh)\b",
            re.I,
        ),
    ),
    # Inline code runs whatever the lesson author wrote, so it is rejected anywhere, backticked or not.
    # `osascript` is rejected outright: it runs AppleScript from a file as easily as from `-e`.
    Rule(
        "lint.inline_code",
        _pattern(
            r"\b(?:i?python[0-9.]*|pypy[0-9.]*|py)" + _INTERPRETER_FLAG + r"c\b"
            r"|\b(?:node|nodejs|deno|bun)\b.*?\s(?:-[a-z]*[ep]\b|--eval\b|--print\b)|\bdeno\s+eval\b"
            r"|\b(?:perl|ruby)" + _INTERPRETER_FLAG + r"e\b"
            r"|\b(?:bash|sh|zsh|fish|dash|ksh|csh|tcsh)" + _INTERPRETER_FLAG + r"c\b"
            r"|\bosascript\b"
            r"|\b(?:powershell|pwsh)\b.*?\s-(?:c|command|e|ec|encodedcommand)\b"
            r"|\beval\b|\bexec\b|\bos\s*\.\s*(?:system|popen|exec\w*|spawn\w*)|\bsubprocess\b"
            r"|\bpopen\b|\b__import__\b|-execdir\b|-okdir\b|-ok\b",
            re.I,
        ),
    ),
    # A known program can be told to run another through its own options, config keys or environment.
    Rule(
        "lint.exec_hook",
        _pattern(
            r"\bgit\b.*?\s(?:-c|--config-env|--exec-path|--upload-pack|--receive-pack)\b"
            r"|\b(?:core\.(?:pager|editor|sshcommand|askpass|hookspath|fsmonitor|gitproxy)|alias\.[\w-]"
            r"|(?:diff|merge|filter)\.[\w-]+\.(?:command|driver|clean|smudge|process|textconv)"
            r"|credential\.helper|sequence\.editor)"
            r"|\b(?:\w*(?:_COMMAND|SHELL|PAGER|EDITOR|VISUAL|ASKPASS)|LD_\w+|DYLD_\w+|BASH_ENV|ENV"
            r"|PROMPT_COMMAND|PYTHONSTARTUP|PYTHONPATH|NODE_OPTIONS|PERL5OPT|PERL5LIB|RUBYOPT"
            r"|GIT_EXTERNAL_DIFF|GIT_EXEC_PATH|GIT_CONFIG\w*|GIT_DIR|GIT_TEMPLATE_DIR)=",
            re.I,
        ),
    ),
    Rule(
        "lint.credentials",
        _pattern(
            r"\.ssh\b|\bid_(?:rsa|dsa|ecdsa|ed25519)\b|(?<![\w.])\.env\b|\bkeychain\b"
            r"|\bsecurity\s+(?:find|dump|export)-|find-(?:generic|internet)-password"
            r"|\.netrc\b|\.npmrc\b|\.pypirc\b|\.git-credentials\b|\.aws/credentials|\.azure\b"
            r"|\.kube/config|\bkubeconfig\b|\.docker/config\.json|\bgh/hosts\.yml"
            r"|application_default_credentials|\.config/gcloud\b|(?<![\w.-])op\s"
            r"|\bprivate key\b|\b[\w-]*tokens?\.(?:txt|json|ya?ml)\b"
            r"|/etc/(?:passwd|shadow|master\.passwd|sudoers|ssh)\b"
            r"|\b" + _LEAK_VERBS + r"\b.{0,40}?\b" + _SECRET_NOUNS + r"\b"
            r"|\b" + _SECRET_NOUNS + r"\b.{0,40}?\b" + _LEAK_VERBS + r"\b",
            re.I,
        ),
    ),
    Rule(
        "lint.override_instructions",
        _pattern(
            r"\b" + _OVERRIDE_VERBS + _FEW_WORDS + _OVERRIDE_TARGETS + r"\b"
            r"|\b(?:instructions?|prompts?|guidelines?|directives?)"
            + _FEW_WORDS
            + _OVERRIDE_VERBS
            + r"|\bobey\w*|\bonly follow\b|\byou are now\b|\bnew instructions?\b|\bsystem prompt\b"
            r"|\bdeveloper message\b|\bfrom now on\b|\bpay no (?:attention|heed)\b|\bcame before\b"
            r"|\bfollow (?:only )?(?:this|these)\b|\bthis lesson\b|\b(?:priority|precedence)\b"
            r"|\byou (?:must|should|shall|will|need to|have to|are)\b"
            r"|\b(?:assistant|claude|codex|chatgpt|llm)\b",
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
            r"|\bscars_off\b|\bscars off\b|git_ssl_no_verify|node_tls_reject_unauthorized"
            r"|\bsudo\b|\bdoas\b",
            re.I,
        ),
    ),
)

SHELL_BUILTINS = frozenset({"cd", "pushd", "popd", "true", "false"})
"""Builtins that run no program, so they need no entry in `known_programs`."""

WRAPPER_PROGRAMS = frozenset({"env", "time", "nohup", "sudo", "uv", "npx", "python", "python3", "bash", "sh"})
"""Words the shell parser unwraps to reach the program they run. The parser reports only the inner
program, so a wrapper named in a backticked command must be a known program too."""

LAUNCHERS = frozenset(
    {"xargs", "nice", "timeout", "gtimeout", "watch", "nohup", "command", "builtin", "exec", "eval"}
    | {"env", "sudo", "doas", "su", "caffeinate", "open", "launchctl", "script", "flock", "parallel"}
    | {"strace", "ltrace", "dtrace", "dtruss", "lldb", "gdb", "entr", "chroot", "unshare", "nsenter"}
    | {"osascript", "at", "batch", "crontab", "ssh", "sshpass", "uvx", "pipx", "bunx", "source", "."}
)
"""Programs whose job is to run another program named in their arguments, which the known-program
rule cannot see. A backticked command may not run one."""

LAUNCHING_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "npm": frozenset({"exec", "x"}),
    "pnpm": frozenset({"dlx", "exec"}),
    "yarn": frozenset({"dlx", "exec"}),
    "bun": frozenset({"x"}),
    "uv": frozenset({"tool"}),
}

XCRUN_TOOLS = frozenset(
    {"simctl", "xcodebuild", "devicectl", "notarytool", "stapler", "xctrace", "swift", "swiftc"}
    | {"clang", "clang++", "lipo", "otool", "codesign", "actool", "ibtool", "altool"}
)
"""`xcrun` runs a developer tool by name, so its first operand must be one of these."""


def _launches(program: str, args: tuple[str, ...]) -> bool:
    if program in LAUNCHERS:
        return True
    if program == "xcrun":
        operands = [a for a in args if not a.startswith("-")]
        return "-r" in args or "--run" in args or (bool(operands) and operands[0] not in XCRUN_TOOLS)
    return bool(args) and args[0] in LAUNCHING_SUBCOMMANDS.get(program, frozenset())


_SUBSTITUTION_PLACEHOLDER = "SUBST"
_MAX_SUBSTITUTION_DEPTH = 3


def _split_substitutions(command: str, depth: int = 0) -> list[str]:
    """The outer command with each `$(...)` body replaced by a placeholder word, then each body.

    The shell parser rejects `$(`, so each body is parsed as a command of its own. Parentheses are
    counted without regard to quoting; a miscount leaves a fragment that fails to parse, which rejects.
    """
    if depth > _MAX_SUBSTITUTION_DEPTH:
        raise Unsupported("substitutions nested too deep")
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
                raise Unsupported("unbalanced $(")
            inner.extend(_split_substitutions(command[i + 2 : j - 1], depth + 1))
            outer.append(_SUBSTITUTION_PLACEHOLDER)
            i = j
        else:
            outer.append(command[i])
            i += 1
    return ["".join(outer), *inner]


def _words(command: str) -> set[str]:
    return {w.strip("\"'") for w in command.split()}


def _check_instead_command(instead: str, known_programs: frozenset[str] | None) -> None:
    """At most one backticked command, parsed by the shell parser, running only known programs.

    A program word that contains `/` is rejected: the parser reports only its basename, and an explicit
    path can run any file under a known name. `known_programs=None` skips only the membership rule.
    """
    ticks = instead.count("`")
    if ticks == 0:
        return
    if ticks % 2:
        raise RejectedText("lint.instead_unbalanced_backticks", "instead")
    if ticks > 2:
        raise RejectedText("lint.instead_multiple_commands", "instead")
    command = instead.split("`")[1]
    try:
        pieces = _split_substitutions(command)
        parsed = [c for piece in pieces for c in parse(piece)]
    except Unsupported:
        raise RejectedText("lint.instead_unparseable_command", "instead") from None
    if not parsed:
        raise RejectedText("lint.instead_unknown_program", "instead")
    for c in parsed:
        if re.search("/" + re.escape(c.program) + r"(?![A-Za-z0-9._+-])", command):
            raise RejectedText("lint.instead_program_path", "instead")
        if _launches(c.program, c.args):
            raise RejectedText("lint.instead_launcher", "instead")
    if known_programs is None:
        return
    programs = {c.program for c in parsed} | (_words(command) & WRAPPER_PROGRAMS)
    if not programs - SHELL_BUILTINS <= known_programs:
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
named token formats are the main defense; entropy is the fallback.
"""


def shannon_entropy(token: str) -> float:
    """Bits per character of `token`, from its own character frequencies."""
    n = len(token)
    return -sum(c / n * math.log2(c / n) for c in Counter(token).values())


def _high_entropy(text: str) -> bool:
    return any(shannon_entropy(t) > ENTROPY_THRESHOLD for t in _ENTROPY_TOKEN.findall(text))


SECRET_RULES: tuple[Rule, ...] = (
    # Named formats match wherever they start, so a prefix such as `-` or `x` cannot hide them.
    Rule("secret.github_token", _pattern(r"(?:ghp_|gho_|ghu_|ghs_|ghr_|github_pat_)[A-Za-z0-9_]")),
    # `sk-` also occurs inside words (`task-runner`, `disk-usage`), so a bare `sk-` counts only when no
    # letter or digit precedes it; the project prefixes and long `sk-` runs count anywhere.
    Rule(
        "secret.openai_key",
        _pattern(r"sk-(?:proj|svcacct|admin|ant)-|sk-[A-Za-z0-9_-]{16,}|(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]"),
    ),
    Rule("secret.slack_token", _pattern(r"xox[abposr]-[A-Za-z0-9-]")),
    Rule("secret.aws_key", _pattern(r"(?:AKIA|ASIA)[0-9A-Z]{16}")),
    Rule("secret.gitlab_token", _pattern(r"glpat-[A-Za-z0-9_-]")),
    Rule("secret.google_api_key", _pattern(r"AIza[0-9A-Za-z_-]{20,}")),
    Rule("secret.npm_token", _pattern(r"npm_[A-Za-z0-9]{20,}")),
    Rule("secret.stripe_key", _pattern(r"(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]")),
    Rule("secret.jwt", _pattern(r"eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*")),
    Rule("secret.url_userinfo", _pattern(r"//[^\s/@:]+:[^\s/@]*@")),
    Rule("secret.pem_header", _pattern(r"-----BEGIN [A-Z0-9 ]*-----")),
    Rule("secret.high_entropy", _high_entropy),
)

# ---------------------------------------------------------------- machine-scope lint

ALLOWED_ROOTS = ("~/.scars", "/Volumes/990 Pro", "/tmp", "/usr", "/opt")
"""Absolute path roots a machine-scope lesson may name. A path is normalized first (repeated `/` and
`.` segments removed, `$HOME` read as `~`), then matches a root only when it equals the root or
continues it with `/`, so `/tmpfiles` and `/Volumes/990 Profile` are outside it."""

_GIT_REMOTES = frozenset({"origin", "upstream"})
"""Owners that are git remote names, so `origin/main` reads as a branch, not a GitHub repo."""

_PATH_START = re.compile(r"(?<![\w.~$/-])(?:~/|/+(?=[\w.~-])|~(?=[A-Za-z_])|\$HOME\b|\$\{HOME\})")
_PATH_BODY = re.compile(r"[^\s`'\"]*")
_QUOTED = re.compile(r"\"([^\"]*)\"|'([^']*)'")
_DOTDOT = re.compile(r"(?<![^\s/`'\"])\.\.(?![^\s/`'\"])")
_SENTENCE_END = ".,;:!?"
_ROOTS_LONGEST_FIRST = sorted(ALLOWED_ROOTS, key=len, reverse=True)
_OWNER_NAME = re.compile(r"(?<![\w.~$/@:-])([A-Za-z0-9][A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]+(?![\w./-])")
"""Exactly two slash-separated segments in GitHub's owner and repo alphabets. This also flags two-segment
relative paths such as `apps/web`; a machine-scope lesson should describe a layout in words instead."""


@dataclass(frozen=True)
class _PathSpan:
    start: int
    end: int
    path: str
    quoted: bool


def _unquoted_paths(text: str, lo: int, hi: int) -> Iterator[_PathSpan]:
    """Absolute paths in `text[lo:hi]`. An allowed root that contains a space is read whole."""
    pos = lo
    while (m := _PATH_START.search(text, pos, hi)) is not None:
        start = m.start()
        root = next((r for r in _ROOTS_LONGEST_FIRST if text.startswith(r, start, hi)), "")
        body = _PATH_BODY.match(text, start + len(root), hi)
        end = body.end() if body else start + len(root)
        path = text[start:end].rstrip(")")
        if path[-1:] in _SENTENCE_END and path[-2:-1] not in _SENTENCE_END:
            path = path[:-1].rstrip(")")
        yield _PathSpan(start, end, path, quoted=False)
        pos = max(end, start + 1)


def _paths(text: str) -> Iterator[_PathSpan]:
    """Every absolute path in `text`. A quoted string that starts with a path is one path, spaces included."""
    pos = 0
    for q in _QUOTED.finditer(text):
        yield from _unquoted_paths(text, pos, q.start())
        inner = q.group(1) if q.group(1) is not None else q.group(2)
        if _PATH_START.match(inner):
            yield _PathSpan(q.start(), q.end(), inner, quoted=True)
        else:
            yield from _unquoted_paths(text, q.start() + 1, q.end() - 1)
        pos = q.end()
    yield from _unquoted_paths(text, pos, len(text))


def _normalize(path: str) -> str:
    path = re.sub(r"^(?:\$HOME|\$\{HOME\})", "~", path)
    head, *segments = path.split("/")
    kept = [s for s in segments if s not in ("", ".")]
    return "/".join([head, *kept]) if kept else (head or "/")


def _allowed(path: str) -> bool:
    if re.search(r"\s(?:/|~|\$HOME|\$\{HOME\})", path):
        return False
    normal = _normalize(path)
    return any(normal == root or normal.startswith(root + "/") for root in ALLOWED_ROOTS)


def _names_repo(text: str, registered_repos: Iterable[str]) -> bool:
    lowered = text.lower()
    return any(
        re.search(r"(?<![\w.-])" + re.escape(name.lower()) + r"(?![\w-])", lowered)
        for name in registered_repos
        if name
    )


def _check_machine_scope(values: Mapping[str, str], registered_repos: Sequence[str]) -> None:
    for field in FIELDS:
        value = values[field]
        if _names_repo(value, registered_repos):
            raise RejectedText("machine.repo_name", field)
        if _DOTDOT.search(value):
            raise RejectedText("machine.dotdot_path", field)
        masked = list(value)
        for span in _paths(value):
            if not _allowed(span.path):
                raise RejectedText("machine.absolute_path", field)
            masked[span.start : span.end] = " " * (span.end - span.start)
        rest = "".join(masked)
        if any(m.group(1).lower() not in _GIT_REMOTES for m in _OWNER_NAME.finditer(rest)):
            raise RejectedText("machine.github_repo", field)


# ---------------------------------------------------------------- check


def _run(rules: Sequence[Rule], values: Mapping[str, str]) -> None:
    for field in FIELDS:
        for rule in rules:
            if rule.hits(values[field]):
                raise RejectedText(rule.name, field)


def _check_values(
    values: Mapping[str, str],
    scope: Scope,
    known_programs: frozenset[str] | None,
    registered_repos: Sequence[str],
) -> None:
    _run(SANITIZER_RULES, values)
    _run(IMPERATIVE_RULES, values)
    _check_instead_command(values["instead"], known_programs)
    _run(SECRET_RULES, values)
    if isinstance(scope, MachineScope):
        _check_machine_scope(values, registered_repos)


def check(
    text: LessonText,
    scope: Scope,
    *,
    known_programs: frozenset[str] | None,
    registered_repos: Sequence[str] = (),
) -> None:
    """Raise `RejectedText` unless every field passes every stage.

    `known_programs` holds the programs this machine ran successfully, as the miner records them. It is
    required so that a caller cannot skip the membership rule by accident; `None` skips that one rule.
    `registered_repos` holds the display names of every registered repo.
    """
    values = {field: getattr(text, field) for field in FIELDS}
    _check_values(values, scope, known_programs, registered_repos)


def check_fields(
    avoid: str,
    instead: str,
    reason: str,
    scope: Scope,
    *,
    known_programs: frozenset[str] | None,
    registered_repos: Sequence[str] = (),
) -> LessonText:
    """Check raw field strings and return them as `LessonText`.

    Unlike constructing `LessonText` first, an empty or over-cap field comes back as `RejectedText`
    (`sanitizer.empty`, `sanitizer.over_cap`) naming the field, so the miner can drop the candidate the
    same way it drops any other rejection.
    """
    values = {"avoid": avoid, "instead": instead, "reason": reason}
    _check_values(values, scope, known_programs, registered_repos)
    return LessonText(avoid=avoid, instead=instead, reason=reason)


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
    """The header carries values that `check` never sees, so hold them to the sanitizer (printable
    ASCII only) and the secret scan too, and keep `|` out so a value cannot forge another header column."""
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


def _recheck(lesson: Lesson, known_programs: frozenset[str] | None, registered_repos: Sequence[str]) -> None:
    _check_header(lesson)
    check(lesson.text, lesson.scope, known_programs=known_programs, registered_repos=registered_repos)


def render(
    lesson: Lesson,
    *,
    reviewed_on: date | None = None,
    known_programs: frozenset[str] | None = None,
    registered_repos: Sequence[str] = (),
) -> str:
    """Render one lesson as quoted, advisory data with its provenance.

    The lesson record has no review date, so the caller passes `reviewed_on`, the date of the lesson's
    latest promotion event. Only a promoted lesson with a date says `reviewed <date>`; every other lesson
    says `unreviewed`. Every check runs again first and its `RejectedText` propagates, so stale or
    tampered text is never shown. With `known_programs=None` (a hook that lacks the miner's program list)
    only the program-membership rule is skipped.
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
class Rendered:
    lesson_id: LessonId
    text: str


@dataclass(frozen=True)
class Fitted:
    kept: tuple[Rendered, ...]
    dropped: int

    @property
    def ids(self) -> tuple[LessonId, ...]:
        """The lessons actually shown, for the hook's `shown` rows."""
        return tuple(r.lesson_id for r in self.kept)

    @property
    def text(self) -> str:
        return LESSON_SEPARATOR.join(r.text for r in self.kept)


def fit(rendered: Sequence[Rendered], budget: Budget) -> Fitted:
    """Keep whole rendered lessons, in their given order, while the joined text stays within the budget.

    The cost counts the separator between kept lessons. A lesson that does not fit is skipped, never cut,
    and a later, shorter lesson may still take the space it left.
    """
    kept: list[Rendered] = []
    used = 0
    for item in rendered:
        cost = len(item.text) + (len(LESSON_SEPARATOR) if kept else 0)
        full = budget.max_lessons is not None and len(kept) >= budget.max_lessons
        if not full and used + cost <= budget.chars:
            kept.append(item)
            used += cost
    return Fitted(tuple(kept), len(rendered) - len(kept))


def render_deny(
    lesson: Lesson,
    *,
    known_programs: frozenset[str] | None = None,
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
