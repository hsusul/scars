import pytest

from scars import hook, shell
from scars.adapters import claude

HOME_REASON = (
    "scars: this command names the scars home directory and is not a plain read (cat, ls, grep and the "
    "like, with no `>`). Lessons change only through scars commands a human runs at a terminal."
)


def run(subcommand):
    return (
        f"scars: agents may not run `scars {subcommand}`. "
        "It changes which lessons reach agents, so a human runs it at a terminal."
    )


@pytest.fixture
def user(tmp_path, monkeypatch):
    """A user home holding the scars home `~/.scars` and a repo, with `HOME` pointing at it."""
    user = tmp_path / "user"
    (user / ".scars").mkdir(parents=True)
    (user / "repo").mkdir()
    monkeypatch.setenv("HOME", str(user))
    monkeypatch.setenv("SCARS_HOME", str(user / ".scars"))
    return user


def reason(user, command, cwd="repo"):
    payload = {
        "session_id": "s1",
        "cwd": str(user / cwd),
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_use_id": "tu1",
    }
    event = claude.parse("PreToolUse", payload)
    return hook.agent_deny_reason(event, user / ".scars", command=command)


ALLOWED = [
    "printf '%s\\n' 'scars off'",
    "git commit -m 'document scars off'",
    "git commit -m 'scars promote'",
    "echo scars off is the kill switch",
    "grep -rn 'scars off' guide/",
    "printf '%s\\n' 'see ~/.scars/OFF'",
    "echo ~/.scars/OFF",
    "scars ls",
    "scars why L-1",
    "scars --version",
    "scars hook claude x",
    "scars doctor",
    "npm run scarsify off",
    "grep scars $(printf README.md)",
    "echo $(date)",
    "cat ~/.scars/lessons.db",
    "cat ~/.scars/lessons.db > /tmp/copy",
    "ls -la ~/.scars | head -n 3",
    "rg -n retire ~/.scars/logs",
    "find ~/.scars -name '*.db'",
    "touch notes.txt",
    "touch ~/.scarsrc",
    "rm -rf ../build",
    "cd /tmp && rm -rf x",
]


@pytest.mark.parametrize("command", ALLOWED)
def test_commands_that_only_mention_or_read_scars_are_allowed(user, command):
    assert reason(user, command) is None


DENIED = [
    ("scars off", run("off")),
    ("scars promote L-1a2b3c4d", run("promote")),
    ("SCARS off", run("off")),
    ("scars -v off", run("off")),
    ("scars $WHAT", run("$WHAT")),
    ("uv run scars pin L-1", run("pin")),
    ("env FOO=1 scars mute L-1", run("mute")),
    ("sudo scars off", run("off")),
    ("python3 -m scars level L-1 warn", run("level")),
    ("python -m scars.cli off", run("off")),
    ("~/.scars/versions/0.1/bin/scars inbox", run("inbox")),
    ("./.venv/bin/scars unpin L-1", run("unpin")),
    ("'sc''ars' off", run("off")),
    ("cd /tmp && bash -c 'scars off'", run("off")),
    ("bash -c \"sh -c 'scars off'\"", run("off")),
    ("zsh -c 'scars retire L-1'", run("retire")),
    ("echo $(scars promote L-1)", run("promote")),
    ("echo " + "a" * 4096 + " && scars promote L-1", run("promote")),
    ("cd ~ && touch .scars/OFF", HOME_REASON),
    ("touch ~/.sc*/OFF", HOME_REASON),
    ("touch ~/*/OFF", HOME_REASON),
    ("touch ~/.scars/OFF", HOME_REASON),
    ("touch ~/.SCARS/OFF", HOME_REASON),
    ('touch ~/".scars"/OFF', HOME_REASON),
    ("cp evil.db $HOME/.scars/lessons.db", HOME_REASON),
    ("rm ${HOME}/.scars/OFF", HOME_REASON),
    ("rm $SCARS_HOME/OFF", HOME_REASON),
    ("D=~/.scars; touch $D/OFF", HOME_REASON),
    ("dd if=/dev/zero of=~/.scars/lessons.db", HOME_REASON),
    ("touch ../.scars/OFF", HOME_REASON),
    ("echo a > ~/.scars/x", HOME_REASON),
    ('echo a > "$HOME/.scars/OFF"', HOME_REASON),
    ("echo a >> ../.scars/OFF", HOME_REASON),
    ("(echo a; echo b) > ../.scars/OFF", HOME_REASON),
    ("ls > ../.scars/OFF", HOME_REASON),
    ("scars why L-1 > ~/.scars/x", HOME_REASON),
    ("tee -a ~/.scars/logs/x", HOME_REASON),
    ("sqlite3 ~/.scars/lessons.db 'delete from lessons'", HOME_REASON),
    ("find ~/.scars -name '*.db' -delete", HOME_REASON),
    ("find ~/.scars -exec rm {} +", HOME_REASON),
    ("cat $(echo ~/.scars/lessons.db)", HOME_REASON),
    ("rg --pre cat x ~/.scars/logs", HOME_REASON),
    ("rg x * ~/.scars/logs", HOME_REASON),
    ("find ~/.scars $ACTION", HOME_REASON),
]


@pytest.mark.parametrize(("command", "expected"), DENIED)
def test_commands_that_run_scars_or_write_its_home_are_denied(user, command, expected):
    assert reason(user, command) == expected


@pytest.mark.parametrize(
    ("command", "cwd"),
    [
        ("cd .scars && touch OFF", "."),
        ("cd /nowhere; touch .scars/OFF", "."),
        ("true; cd .scars; touch OFF", "."),
        ("touch OFF", ".scars"),
        ("rm -rf .", ".scars"),
    ],
)
def test_relative_paths_resolve_against_the_event_cwd_and_a_leading_cd(user, command, cwd):
    assert reason(user, command, cwd=cwd) == HOME_REASON


def test_a_custom_scars_home_is_protected_along_with_the_default(user, tmp_path, monkeypatch):
    custom = tmp_path / "elsewhere" / "state"
    custom.mkdir(parents=True)
    monkeypatch.setenv("SCARS_HOME", str(custom))
    payload = {
        "session_id": "s1",
        "cwd": str(user / "repo"),
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_use_id": "tu1",
    }

    def deny(command):
        event = claude.parse("PreToolUse", {**payload, "tool_input": {"command": command}})
        return hook.agent_deny_reason(event, custom)

    assert deny(f"touch {custom}/OFF") == HOME_REASON
    assert deny("rm $SCARS_HOME/OFF") == HOME_REASON
    assert deny("touch ~/.scars/OFF") == HOME_REASON
    assert deny(f"ls {custom}") is None
    assert deny(f"touch {custom}2/OFF") is None


def parsed(command):
    (c,) = shell.parse(command)
    return c


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("rg -n x logs", True),
        ("rg --pre cat x logs", False),
        ("rg --pre=cat x logs", False),
        ("rg x $OPTS logs", False),
        ("rg x * logs", False),
        ("rg x ?x logs", False),
        ("rg x [a] logs", False),
        ("rg x ~/logs", True),
        ("find logs -name x", True),
        ("find logs $ACTION", False),
        ("find logs *", False),
        ("find logs -delete", False),
        ("find logs -exec rm {} +", False),
        ("grep -r x $DIR", True),
        ("echo x", True),
        ("touch x", False),
    ],
)
def test_provably_read_only(command, expected):
    assert hook.provably_read_only(parsed(command)) is expected
