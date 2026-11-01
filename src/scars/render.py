"""Lesson template, sanitizer, lints and secret scan.

Lesson text is untrusted: the miner lifts it from tool output. `check` runs four stages in a fixed order
and raises `RejectedText` at the first rule that fires. Within a stage, fields go in the order avoid,
instead, reason, and each field meets the stage's rules in table order. A rejected candidate is dropped
whole; nothing is ever stripped or redacted. The same checks run again inside `render`, so text edited
in the database behind the CLI's back is never shown.

The policy is blunt on purpose. These checks sit behind trust tiers and human review, so dropping a
harmless candidate is cheap and showing a harmful one is not. Where a rule cannot be complete, it is
written as an allowlist (printable ASCII only, commands already run on this machine only, allowed path
roots only) rather than a list of bad phrases.

1. Sanitizer: empty and over-cap fields, ANSI escapes, control characters (every C0 and C1 character,
   tab and newline included), anything outside printable ASCII, code fences and URLs.
2. Imperative lint: the fixed `IMPERATIVE_RULES` table on every field, then the shape rules. A lesson
   whose trust class is not `human` has a fixed structure: `instead` is exactly one of
   `INSTEAD_TEMPLATES` around one observed command, and `avoid` and `reason` hold no code span at all
   (see `_check_shape`). Free prose is never searched for commands; the structure leaves no room for one.
   A `human` lesson keeps free wording, subject to every other rule.
3. Secret scan: known token formats, matched wherever they start, and a high-entropy fallback.
4. Machine-scope lint: machine lessons reach every repo, so they may not name a repo, a GitHub
   `owner/name`, a `..` path segment, or an absolute path outside `ALLOWED_ROOTS`, including a path
   attached to an option (`-I/x`, `--prefix=/x`, `key=/x`), a relative path of three or more
   segments, or a host (`localhost`, an IPv4 address, `host:port`).
5. Mined structure: a lesson whose trust class is not `human` holds only what the miner builds from
   parsed calls, never transcript prose. `avoid` is one plain command and the command in `instead` is
   another with the same program, each written as `shlex.join` of its words (see `plain_command`). A
   failure fixed only by quoting bare glob characters keeps its recorded text (see `quote_only_fix`).
   `reason` is exactly `MINED_REASON`. The agent already sees its own error at the repair stage, so the
   error text adds nothing a lesson needs and is the one field an attacker fully controls. A mined lesson
   about a non-shell tool takes the one other shape `tool_text` writes instead (see `FieldChange`): it is
   recognized when `instead` has no backtick and reads as field changes, and it must then read back to
   exactly the same text.

Rejection messages name the field and the rule, never the matched text, so a secret or a private repo
name cannot leak through an error message or a log line.
"""

import json
import math
import re
import shlex
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date

from scars.model import (
    TEXT_CAP,
    Lesson,
    LessonId,
    LessonText,
    Level,
    MachineScope,
    PathScope,
    RepoScope,
    Scope,
    SessionScope,
    Stage,
    Status,
    TrustClass,
)
from scars.shell import SimpleCommand, Unsupported, parse
from scars.signature import HEADER_PREFIX

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
            r"\bgit\b.*?\s(?:(?-i:-c)|--config-env|--exec-path|--upload-pack|--receive-pack)\b"
            r"|\b(?:core\.(?:pager|editor|sshcommand|askpass|hookspath|fsmonitor|gitproxy)|alias\.[\w-]"
            r"|(?:diff|merge|filter)\.[\w-]+\.(?:command|driver|clean|smudge|process|textconv)"
            r"|credential\.helper|sequence\.editor)"
            r"|\b(?:\w*(?:_COMMAND|SHELL|PAGER|EDITOR|VISUAL|ASKPASS)|LD_\w+|DYLD_\w+|BASH_ENV|ENV"
            r"|PROMPT_COMMAND|PYTHONSTARTUP|PYTHONPATH|NODE_OPTIONS|PERL5OPT|PERL5LIB|RUBYOPT"
            r"|GIT_EXTERNAL_DIFF|GIT_EXEC_PATH|GIT_CONFIG\w*|GIT_DIR|GIT_TEMPLATE_DIR|GIT_SSH\w*)=",
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


def normalize_command(command: str) -> str:
    """`command` stripped, with each run of spaces and tabs outside quotes collapsed to one space.

    A newline separates two commands, so it is never collapsed: `echo a` newline `echo b` stays
    different from `echo a echo b`. Nothing else is rewritten: quoting, escapes, newlines and word
    order stay as written. A backslash outside
    single quotes keeps the next character literal, so an escaped space is never collapsed. The miner
    and the hook normalize the commands they pass as `known_commands` with this same function, and
    `check` normalizes both sides again before comparing.
    """
    out: list[str] = []
    quote = ""
    i = 0
    text = command.strip()
    while i < len(text):
        ch = text[i]
        if ch == "\\" and quote != "'" and i + 1 < len(text):
            out.append(text[i : i + 2])
            i += 2
            continue
        if quote:
            quote = "" if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        elif ch in " \t":
            if not out or out[-1] != " ":
                out.append(" ")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


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


def _command_views(command: str) -> list[str]:
    """The command as the shell will see it, for the imperative rules to scan.

    Each piece (the outer command and every `$(...)` body) must parse with the shell parser. Each piece
    yields two dequoted views: its words after shell quote removal, wrappers kept (`s''h -c id` reads
    `sh -c id`), and each parsed simple command's program and arguments. Quotes therefore cannot hide
    `-e`, `-c` or a config key from a rule that also runs on the raw text.
    """
    views: list[str] = []
    try:
        for piece in _split_substitutions(command):
            parsed = parse(piece)
            if not parsed:
                raise Unsupported("no command")
            views.append(" ".join(shlex.split(piece)))
            views.extend(" ".join((c.program, *c.args)) for c in parsed)
    except (Unsupported, ValueError):
        raise RejectedText("lint.instead_unparseable_command", "instead") from None
    return views


INSTEAD_TEMPLATES: tuple[str, ...] = (
    "run `{command}`",
    "run `{command}` first",
    "run `{command}` instead",
    "use `{command}`",
    "use `{command}` instead",
)
"""The only shapes `instead` may take unless the lesson's trust class is `human`.

Fill one with `template.format(command=normalize_command(passing_input))`. The match is exact: same
case, single spaces, no trailing punctuation, nothing before or after. The command itself may be any
text without a backtick; it must still parse, pass every imperative rule, and equal an observed command.
"""


def _template_pattern(template: str) -> str:
    prefix, suffix = template.split("{command}")
    return re.escape(prefix) + "[^`]*" + re.escape(suffix)


_INSTEAD_SHAPE = re.compile("|".join(f"(?:{_template_pattern(t)})" for t in INSTEAD_TEMPLATES))
_CODE_SPAN = re.compile(r"`|<\s*/?\s*(?:code|pre|kbd)\b|\$\(", re.IGNORECASE)
"""A backtick, an HTML `code`, `pre` or `kbd` tag (opening or closing, any case), or `$(`."""


def _check_tool_shape(values: Mapping[str, str], known_commands: frozenset[str]) -> None:
    """The structural rules for a structured tool lesson: no code span in any field, both fields read
    back as the same field changes, and `instead` is one this machine observed as written."""
    for field in FIELDS:
        if _CODE_SPAN.search(values[field]):
            raise RejectedText("lint.code_span_outside_instead", field)
    parsed = parse_tool_text(values["avoid"], values["instead"])
    if parsed is None:
        raise RejectedText("mined.tool_avoid_shape", "avoid")
    tool, changes = parsed
    if not _consistent(changes) or tool_text(tool, changes) != (values["avoid"], values["instead"]):
        raise RejectedText("mined.tool_fields_mismatch", "instead")
    if values["instead"] not in known_commands:
        raise RejectedText("mined.tool_unobserved", "instead")


def _check_instead_command(instead: str, known_commands: frozenset[str], trust_class: TrustClass) -> None:
    """At most one backticked command, and it must be one this machine already ran successfully.

    Unless the trust class is `human`, `instead` must also be exactly one of `INSTEAD_TEMPLATES`, so a
    second command cannot ride along in surrounding prose or in an HTML code tag.

    Programs are not trusted by name: a known program runs arbitrary code through its own subcommands,
    options and environment (`npm run`, `make -f`, `GIT_SSH=`). So a command is allowed only when its
    normalized text equals a normalized entry of `known_commands`, the passing inputs observed on this
    machine, or when the lesson's trust class is `human` (a person wrote or approved it at a terminal).

    For every trust class, observed or not, the command must still parse, and every imperative rule
    runs on its dequoted views as well as on the raw text. An observed `python -c` stays rejected: one
    session's inline code should not be re-suggested to every session.
    """
    human = trust_class is TrustClass.HUMAN
    ticks = instead.count("`")
    if ticks == 0:
        if human:
            return
        raise RejectedText("lint.instead_template", "instead")
    if ticks % 2:
        raise RejectedText("lint.instead_unbalanced_backticks", "instead")
    if ticks > 2:
        raise RejectedText("lint.instead_multiple_commands", "instead")
    if not human and _INSTEAD_SHAPE.fullmatch(instead) is None:
        raise RejectedText("lint.instead_template", "instead")
    command = instead.split("`")[1]
    for view in _command_views(command):
        for rule in IMPERATIVE_RULES:
            if rule.hits(view):
                raise RejectedText(rule.name, "instead")
    if human:
        return
    if normalize_command(command) not in {normalize_command(k) for k in known_commands}:
        raise RejectedText("lint.instead_unobserved_command", "instead")


def _check_shape(values: Mapping[str, str], known_commands: frozenset[str], trust_class: TrustClass) -> None:
    """The structural rules, field by field: `instead` via `_check_instead_command`, and for every trust
    class but `human`, no code span in `avoid` or `reason`. The miner writes `avoid` as the failed input
    without backticks and `reason` as an error signature, so neither needs one."""
    for field in FIELDS:
        if field == "instead":
            _check_instead_command(values[field], known_commands, trust_class)
        elif trust_class is not TrustClass.HUMAN and _CODE_SPAN.search(values[field]):
            raise RejectedText("lint.code_span_outside_instead", field)


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


_SECRET_WORD = re.compile(
    r"(?:^|[-_.])(?:password|passwd|token|secret|auth|api[-_]?key|access[-_]key|private[-_]key)(?=$|[-_.])",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(r"'[^']*'|\"[^\"]*\"|[^\s'\"]+")
_NAMED_ARGUMENT = re.compile(
    r"(?:(?<![\w-])--([a-z0-9]++(?:[-_.][a-z0-9]++)*+)(?:[=:]|\s++(?!-))"
    r"|(?<![\w./-])([a-z0-9_][a-z0-9_.-]*+)=)(?=\S)",
    re.IGNORECASE,
)
"""An option (`--github-token X`, `--token=X`, `--token:X`) or a `NAME=value` key (`DB_PASSWORD=X`) has a
secret name when one of its `-`, `_` or `.` separated parts is a secret word, so `--tokenizer` and
`author=` do not. Any non-empty value counts, however plain: the name is the evidence. The value is
scanned separately, so a key in the value of another name (`FOO=TOKEN=x`) is still found. Names are
matched possessively and each unquoted value's end is reused to keep the scan linear."""


def _secret_values(text: str) -> list[tuple[int, int]]:
    """Spans of the values passed to secret-named options and keys, except the `***` that masking leaves."""
    spans = []
    unquoted_end = 0
    for named in _NAMED_ARGUMENT.finditer(text):
        if not _SECRET_WORD.search(named.group(1) or named.group(2)):
            continue
        start = named.end()
        quoted = text[start] in "'\""
        if quoted or start >= unquoted_end:
            value = _SECRET_VALUE.match(text, start)
            if value is None:
                continue
            stop = value.end()
            if not quoted:
                unquoted_end = stop
        else:
            stop = unquoted_end
        if stop - start != 3 or not text.startswith("***", start):
            spans.append((start, stop))
    return spans


def names_secret(text: str) -> bool:
    """Whether `text` passes a value to an option or key named as a secret."""
    return bool(_secret_values(text))


def mask_secrets(text: str) -> str:
    """`text` with the value of every option or key named as a secret replaced by `***`."""
    out, end = [], 0
    for start, stop in _secret_values(text):
        if start < end:
            end = max(end, stop)
            continue
        out += [text[end:start], "***"]
        end = stop
    return "".join(out) + text[end:]


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
    Rule("secret.keyed_argument", names_secret),
)


# ---------------------------------------------------------------- machine-scope lint

ALLOWED_ROOTS = ("~/.scars", "/Volumes/990 Pro", "/tmp", "/usr", "/opt")
"""Absolute path roots a machine-scope lesson may name. A path is normalized first (repeated `/` and
`.` segments removed, `$HOME` read as `~`), then matches a root only when it equals the root or
continues it with `/`, so `/tmpfiles` and `/Volumes/990 Profile` are outside it."""

_GIT_REMOTES = frozenset({"origin", "upstream"})
"""Owners that are git remote names, so `origin/main` reads as a branch, not a GitHub repo."""

_PATH_HEAD = r"(?:~/|/+(?=[\w.~-])|~(?=[A-Za-z_])|\$HOME\b|\$\{HOME\})"
_PATH_START = re.compile(r"(?<![\w.~$/-])" + _PATH_HEAD)
_ATTACHED_START = re.compile(r"(?:(?<=[=:,])|(?<=(?<![\w-])-[A-Za-z]))" + _PATH_HEAD)
"""A path glued to an option or a key: after `=`, `:` or `,` (`--prefix=/x`, `PATH=/a:/b`, `-Wl,/x`),
or after a one-letter short option (`-I/x`, `-L~/lib`). `_PATH_START` skips these because a word
character or `-` precedes the path."""
_PATH_BODY = re.compile(r"[^\s`'\"]*")
_QUOTED = re.compile(r"\"([^\"]*)\"|'([^']*)'")
_DOTDOT = re.compile(r"(?<![^\s/`'\"])\.\.(?![^\s/`'\"])")
_SENTENCE_END = ".,;:!?"
_ROOTS_LONGEST_FIRST = sorted(ALLOWED_ROOTS, key=len, reverse=True)
_OWNER_NAME = re.compile(r"(?<![\w.~$/@:-])([A-Za-z0-9][A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]+(?![\w./-])")
"""Exactly two slash-separated segments in GitHub's owner and repo alphabets. This also flags two-segment
relative paths such as `apps/web`; a machine-scope lesson should describe a layout in words instead."""
_RELATIVE_PATH = re.compile(r"(?<![\w.~$/@:-])[\w.-]+(?:/[\w.-]+){2,}")
"""A relative path of three or more segments, which names one repo's layout (or `owner/repo/...`)."""
_HOST = re.compile(
    r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])"
    r"|\blocalhost\b"
    r"|(?<![\w.-])(?=[\w.-]*[A-Za-z])[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*:\d{2,5}\b",
    re.IGNORECASE,
)
"""An IPv4 address, `localhost`, or `host:port`: a service on one machine or network. A `file:line`
with a line of two or more digits reads as `host:port` and is rejected too; blunt on purpose."""


@dataclass(frozen=True)
class _PathSpan:
    start: int
    end: int
    path: str
    quoted: bool


def _unquoted_paths(
    text: str, lo: int, hi: int, start_pattern: re.Pattern[str] = _PATH_START
) -> Iterator[_PathSpan]:
    """Absolute paths in `text[lo:hi]`. An allowed root that contains a space is read whole."""
    pos = lo
    while (m := start_pattern.search(text, pos, hi)) is not None:
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


_SUBPATH_START = re.compile(r"(?:^|(?<=[\s=:,])|(?<=(?<![\w-])-[A-Za-z]))" + _PATH_HEAD)
_SUBPATH_END = re.compile(r"[\s=:,]")


def _allowed(path: str) -> bool:
    """Every path inside `path` is under an allowed root, not just the first.

    A span can hold several paths: `/usr/bin:/Users/h/bin`, `/tmp/x=/Users/y`, or a quoted string such
    as `"/tmp/x -I/Users/h"`. Each one starts at the beginning, after whitespace, `=`, `:` or `,`, or
    after a one-letter short option, and runs to the next whitespace, `=`, `:` or `,`, except that an
    allowed root containing a space is read whole.
    """
    for m in _SUBPATH_START.finditer(path):
        start = m.start()
        root = next((r for r in _ROOTS_LONGEST_FIRST if path.startswith(r, start)), "")
        stop = _SUBPATH_END.search(path, start + len(root))
        normal = _normalize(path[start : stop.start() if stop else len(path)])
        if not any(normal == r or normal.startswith(r + "/") for r in ALLOWED_ROOTS):
            return False
    return True


def _names_repo(text: str, registered_repos: Iterable[str]) -> bool:
    lowered = text.lower()
    return any(
        re.search(r"(?<![\w.-])" + re.escape(name.lower()) + r"(?![\w-])", lowered)
        for name in registered_repos
        if name
    )


def _check_machine_scope(values: Mapping[str, str], registered_repos: Sequence[str], writing: bool) -> None:
    """`writing` adds the relative-path and host rules. They came after human machine lessons were
    already stored, so they hold only when a person writes one; a stored lesson keeps rendering."""
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
        for span in _unquoted_paths(rest, 0, len(rest), _ATTACHED_START):
            if not _allowed(span.path):
                raise RejectedText("machine.absolute_path", field)
            masked[span.start : span.end] = " " * (span.end - span.start)
        rest = "".join(masked)
        if any(m.group(1).lower() not in _GIT_REMOTES for m in _OWNER_NAME.finditer(rest)):
            raise RejectedText("machine.github_repo", field)
        if writing and _RELATIVE_PATH.search(rest):
            raise RejectedText("machine.relative_path", field)
        if writing and _HOST.search(value):
            raise RejectedText("machine.host", field)


# ---------------------------------------------------------------- mined structure

MINED_REASON = "AVOID failed, INSTEAD passed"


def plain_command(command: str) -> SimpleCommand | None:
    """The one simple command `command` is, when its shell words are exactly its program and arguments.

    None for a command the parser rejects, for several commands, and for any word the parser consumes
    or the shell expands at run time: a wrapper, an assignment, a redirection, a comment, `$NAME`, a
    glob or `~`. What is left can be rewritten as `shlex.join` of its words without changing what runs.
    """
    try:
        parsed = parse(command)
        words = shlex.split(command)
    except (Unsupported, ValueError):
        return None
    if len(parsed) != 1:
        return None
    (simple,) = parsed
    if simple.uncertain or words != [simple.program, *simple.args]:
        return None
    return simple


def plain_text(simple: SimpleCommand) -> str:
    return shlex.join((simple.program, *simple.args))


def quote_only_fix(failed: str, passing: str) -> bool:
    """Whether quoting bare glob characters makes `failed` the same plain command as `passing`.

    Quote only those characters, then let `plain_command` reject every other shell expansion or syntax.
    Comparing parsed words keeps a change to an argument or an added word from becoming a quote fix.
    """
    fix = plain_command(passing)
    if fix is None:
        return False
    quoted: list[str] = []
    quote: str | None = None
    escaped = False
    changed = False
    for char in failed:
        if escaped:
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif quote is not None:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char in "?*[":
            quoted.append(f"'{char}'")
            changed = True
            continue
        quoted.append(char)
    return changed and plain_command("".join(quoted)) == fix


def _canonical(text: str) -> SimpleCommand | None:
    simple = plain_command(text)
    return simple if simple is not None and plain_text(simple) == text else None


def _check_mined(values: Mapping[str, str]) -> None:
    """Stage 5. `instead` already matched one of `INSTEAD_TEMPLATES`, so it holds exactly one code span."""
    avoid = _canonical(values["avoid"])
    command = values["instead"].split("`")[1]
    if avoid is None and quote_only_fix(values["avoid"], command):
        avoid = plain_command(command)
    if avoid is None:
        raise RejectedText("mined.avoid_not_one_command", "avoid")
    fix = _canonical(command)
    if fix is None:
        raise RejectedText("mined.instead_not_one_command", "instead")
    if fix.program != avoid.program:
        raise RejectedText("mined.program_mismatch", "instead")
    if values["reason"] != MINED_REASON:
        raise RejectedText("mined.reason_template", "reason")


# ---------------------------------------------------------------- structured tool lessons

TOOL_VALUE_CAP = 100
"""The longest string value a structured tool lesson may name; longer ones are prose, not a setting."""
TOOL_MAX_FIELDS = 2
_TOOL_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,127}")
_FIELD_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,63}")
_JSON = json.JSONDecoder()


@dataclass(frozen=True)
class FieldChange:
    """One top-level input field that differs between a failed non-shell tool call and its fix.

    `failed` and `passing` are the field's value as compact JSON, or None when that call has no such
    field. Values are JSON scalars (string, number, boolean, null); a string holds only printable ASCII
    and at most `TOOL_VALUE_CAP` characters (`tool_value` builds them).

    The lesson text is `tool_text(tool, changes)`, sorted by key, at most `TOOL_MAX_FIELDS` changes:
    `avoid` is `<tool> with <k>=<v>` per change (`<k> missing` when the failed call had no such field),
    joined by `, `; `instead` is `<k>=<v>` for a changed value, `add <k>=<v>` for an added field and
    `omit <k>` for a removed one, joined by `, `.
    """

    key: str
    failed: str | None
    passing: str | None


def tool_value(value: object) -> str | None:
    """`value` as compact JSON when a structured tool lesson may name it, else None."""
    if isinstance(value, str):
        if len(value) > TOOL_VALUE_CAP or any(not 0x20 <= ord(ch) <= 0x7E for ch in value):
            return None
    elif value is not None and not isinstance(value, bool | int | float):
        return None
    try:
        return json.dumps(value, allow_nan=False)
    except ValueError:
        return None


def tool_name_ok(tool: str) -> bool:
    return _TOOL_NAME.fullmatch(tool) is not None


def field_key_ok(key: str) -> bool:
    return _FIELD_KEY.fullmatch(key) is not None


def secret_name(key: str) -> bool:
    """Whether a field or option name says it holds a secret (`api_key`, `auth_token`)."""
    return _SECRET_WORD.search(key) is not None


def tool_text(tool: str, changes: Sequence[FieldChange]) -> tuple[str, str]:
    """`avoid` and `instead` for a structured tool lesson, in the one shape `FieldChange` describes."""
    avoid = ", ".join(f"{c.key}={c.failed}" if c.failed is not None else f"{c.key} missing" for c in changes)
    instead = ", ".join(
        f"omit {c.key}"
        if c.passing is None
        else f"add {c.key}={c.passing}"
        if c.failed is None
        else f"{c.key}={c.passing}"
        for c in changes
    )
    return f"{tool} with {avoid}", instead


def _value(text: str, pos: int) -> tuple[str, int] | None:
    """The canonical JSON scalar at `pos` and where it ends, or None."""
    try:
        value, end = _JSON.raw_decode(text, pos)
    except ValueError:
        return None
    raw = text[pos:end]
    return (raw, end) if tool_value(value) == raw else None


def _items(text: str, parse_one: Callable[[str, int], tuple[tuple[str, str | None, str], int] | None]):
    items, pos = [], 0
    while True:
        item = parse_one(text, pos)
        if item is None:
            return None
        items.append(item[0])
        pos = item[1]
        if pos == len(text):
            return items
        if not text.startswith(", ", pos) or len(items) >= TOOL_MAX_FIELDS:
            return None
        pos += 2


def _avoid_item(text: str, pos: int):
    key = _FIELD_KEY.match(text, pos)
    if key is None:
        return None
    end = key.end()
    if text.startswith(" missing", end):
        return (key.group(), None, "missing"), end + len(" missing")
    if not text.startswith("=", end) or (value := _value(text, end + 1)) is None:
        return None
    return (key.group(), value[0], "set"), value[1]


def _instead_item(text: str, pos: int):
    op = next((o for o in ("omit ", "add ") if text.startswith(o, pos)), "")
    key = _FIELD_KEY.match(text, pos + len(op))
    if key is None:
        return None
    end = key.end()
    if op == "omit ":
        return (key.group(), None, "omit"), end
    if not text.startswith("=", end) or (value := _value(text, end + 1)) is None:
        return None
    return (key.group(), value[0], "add" if op else "set"), value[1]


def parse_instead_fields(instead: str) -> list[tuple[str, str | None, str]] | None:
    return _items(instead, _instead_item)


def parse_tool_text(avoid: str, instead: str) -> tuple[str, tuple[FieldChange, ...]] | None:
    """The tool and field changes `avoid` and `instead` spell, or None when either is not that shape.
    Consistency between the two fields is `_consistent`'s job."""
    tool = _TOOL_NAME.match(avoid)
    if tool is None or not avoid.startswith(" with ", tool.end()):
        return None
    failed = _items(avoid[tool.end() + len(" with ") :], _avoid_item)
    passing = parse_instead_fields(instead)
    if failed is None or passing is None:
        return None
    if [k for k, _, _ in failed] != [k for k, _, _ in passing]:
        return tool.group(), ()
    changes = tuple(
        FieldChange(key, failed_value, None if op == "omit" else passing_value)
        for (key, failed_value, _), (_, passing_value, op) in zip(failed, passing, strict=True)
    )
    ops = tuple(op for _, _, op in passing)
    return tool.group(), changes if all(_op_fits(c, op) for c, op in zip(changes, ops, strict=True)) else ()


def _op_fits(change: FieldChange, op: str) -> bool:
    if change.failed is None:
        return op == "add"
    return op == ("omit" if change.passing is None else "set")


def _consistent(changes: Sequence[FieldChange]) -> bool:
    keys = [c.key for c in changes]
    return (
        1 <= len(changes) <= TOOL_MAX_FIELDS
        and keys == sorted(set(keys))
        and all(c.failed != c.passing for c in changes)
    )


def _is_tool_shape(values: Mapping[str, str], trust_class: TrustClass) -> bool:
    return (
        trust_class is not TrustClass.HUMAN
        and "`" not in values["instead"]
        and parse_instead_fields(values["instead"]) is not None
    )


# ---------------------------------------------------------------- check


def _run(rules: Sequence[Rule], values: Mapping[str, str]) -> None:
    for field in FIELDS:
        for rule in rules:
            if rule.hits(values[field]):
                raise RejectedText(rule.name, field)


def _check_values(
    values: Mapping[str, str],
    scope: Scope,
    known_commands: frozenset[str],
    trust_class: TrustClass,
    registered_repos: Sequence[str],
    writing: bool,
) -> None:
    _run(SANITIZER_RULES, values)
    _run(IMPERATIVE_RULES, values)
    tool_shape = _is_tool_shape(values, trust_class)
    if tool_shape:
        _check_tool_shape(values, known_commands)
    else:
        _check_shape(values, known_commands, trust_class)
    _run(SECRET_RULES, values)
    if isinstance(scope, MachineScope):
        _check_machine_scope(values, registered_repos, writing and trust_class is TrustClass.HUMAN)
    if tool_shape and values["reason"] != MINED_REASON:
        raise RejectedText("mined.reason_template", "reason")
    if not tool_shape and trust_class is not TrustClass.HUMAN:
        _check_mined(values)


def check(
    text: LessonText,
    scope: Scope,
    *,
    known_commands: frozenset[str],
    trust_class: TrustClass,
    registered_repos: Sequence[str] = (),
    writing: bool = False,
) -> None:
    """Raise `RejectedText` unless every field passes every stage.

    `known_commands` holds full commands that ran successfully on this machine: the miner passes the
    passing inputs it observed, the hook passes the lesson's own observed passing inputs. It has no
    default, so no caller can skip the observed-command rule by accident. `trust_class` is the lesson's;
    only `human` exempts a lesson from that rule and from the fixed shape (`INSTEAD_TEMPLATES` for
    `instead`, no code span in `avoid` or `reason`). `registered_repos` holds the display names
    of every registered repo. `writing` is for a path that stores text or scope: it adds the rules that
    hold only for new human machine lessons (see `_check_machine_scope`).
    """
    values = {field: getattr(text, field) for field in FIELDS}
    _check_values(values, scope, known_commands, trust_class, registered_repos, writing)


def check_fields(
    avoid: str,
    instead: str,
    reason: str,
    scope: Scope,
    *,
    known_commands: frozenset[str],
    trust_class: TrustClass,
    registered_repos: Sequence[str] = (),
) -> LessonText:
    """Check raw field strings and return them as `LessonText`.

    Unlike constructing `LessonText` first, an empty or over-cap field comes back as `RejectedText`
    (`sanitizer.empty`, `sanitizer.over_cap`) naming the field, so the miner can drop the candidate the
    same way it drops any other rejection.
    """
    values = {"avoid": avoid, "instead": instead, "reason": reason}
    _check_values(values, scope, known_commands, trust_class, registered_repos, True)
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
    seen = (
        f"{lesson.sessions_seen}" if lesson.sessions_seen <= MAX_SHOWN_SESSIONS else f"{MAX_SHOWN_SESSIONS}+"
    )
    label = scope_label(lesson.scope)
    return f"{HEADER_PREFIX}{lesson.id} {lesson.level} | {label} | seen {seen}x | {reviewed}]"


MAX_SHOWN_SESSIONS = 999
"""Higher counts show as `999+`, so the header has a longest form for budget checks to preview."""


def _recheck(lesson: Lesson, known_commands: frozenset[str], registered_repos: Sequence[str]) -> None:
    _check_header(lesson)
    check(
        lesson.text,
        lesson.scope,
        known_commands=known_commands,
        trust_class=lesson.trust_class,
        registered_repos=registered_repos,
    )


def render(
    lesson: Lesson,
    *,
    known_commands: frozenset[str],
    reviewed_on: date | None = None,
    registered_repos: Sequence[str] = (),
) -> str:
    """Render one lesson as quoted, advisory data with its provenance.

    The lesson record has no review date, so the caller passes `reviewed_on`, the date of the lesson's
    latest promotion event. Only a promoted lesson with a date says `reviewed <date>`; every other lesson
    says `unreviewed`. Every check runs again first and its `RejectedText` propagates, so stale or
    tampered text is never shown. `known_commands` has no default; the hook passes the lesson's own
    observed passing inputs. The trust class is read from the lesson itself, never from the caller, so a
    caller cannot lift a mined lesson into the `human` exemption.
    """
    _recheck(lesson, known_commands, registered_repos)
    return "\n".join(
        (
            _header(lesson, reviewed_on),
            f"AVOID: {lesson.text.avoid}",
            f"INSTEAD: {lesson.text.instead}",
            f"WHY: {lesson.text.reason}",
            f"(advisory; scars why {lesson.id})",
        )
    )


def render_problem(
    lesson: Lesson, *, known_commands: frozenset[str], registered_repos: Sequence[str] = ()
) -> RejectedText | None:
    """The rule that keeps `lesson` from rendering, or None when `render` would show it."""
    try:
        _recheck(lesson, known_commands, registered_repos)
    except RejectedText as exc:
        return exc
    return None


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


STAGE_BUDGETS = {Stage.PLAN: PROMPT_BUDGET, Stage.REPAIR: REPAIR_BUDGET}
"""The budget a lesson must fit alone at each stage it is delivered at. The gate stage has none:
`render_deny` always fits `DENY_CAP` by dropping fields."""
_LONGEST_LEVEL = max(Level, key=lambda level: len(level.value))
_LONGEST_REVIEW = date(9999, 12, 31)


def check_budgets(
    lesson: Lesson,
    *,
    known_commands: frozenset[str],
    registered_repos: Sequence[str] = (),
) -> None:
    """Raise `RejectedText` (`budget.<stage>`, field `lesson`) unless the lesson fits alone in the budget
    of every stage its triggers deliver at, and in the session-start budget for when it is pinned.

    The lesson renders with the longest header it can reach (promoted with a review date, the longest
    level, a session count shown as `999+`), so a lesson that fits when written never stops rendering
    after a later promotion or sighting. Call it on every path that writes lesson text or triggers. Every
    `check` rule runs too, with `writing`, and its own `RejectedText` propagates.
    """
    check(
        lesson.text,
        lesson.scope,
        known_commands=known_commands,
        trust_class=lesson.trust_class,
        registered_repos=registered_repos,
        writing=True,
    )
    longest = replace(
        lesson, status=Status.PROMOTED, level=_LONGEST_LEVEL, sessions_seen=MAX_SHOWN_SESSIONS + 1
    )
    text = render(
        longest, known_commands=known_commands, reviewed_on=_LONGEST_REVIEW, registered_repos=registered_repos
    )
    stages = sorted({t.stage for t in lesson.triggers if t.stage in STAGE_BUDGETS})
    budgets = [(str(stage), STAGE_BUDGETS[stage]) for stage in stages]
    for name, budget in [*budgets, ("session_start", SESSION_START_BUDGET)]:
        if len(text) > budget.chars:
            raise RejectedText(f"budget.{name}", "lesson")


def render_deny(
    lesson: Lesson,
    *,
    known_commands: frozenset[str],
    registered_repos: Sequence[str] = (),
) -> str:
    """A PreToolUse deny reason of at most `DENY_CAP` characters, without cutting any field.

    Candidates go from richest to barest, and the first that fits wins: header, AVOID and INSTEAD; then
    header and AVOID; then a bare line naming the lesson. The bare line is at most 94 characters because
    the lesson id is at most 18, so some candidate always fits. The final length check guards that bound.
    """
    _recheck(lesson, known_commands, registered_repos)
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
