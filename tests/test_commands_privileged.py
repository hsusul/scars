"""The human-only check that privileged commands run before they change anything."""

import os

import pytest

from cli_helpers import make_ctx, run
from scars import commands, paths
from scars.commands import process_ancestors, refusal

pytest_plugins = ("cli_fixtures",)

UNKNOWN = "refused: cannot tell which processes started it; run it in your own terminal"


def test_ancestors_come_from_an_absolute_ps_whatever_path_says(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    found = process_ancestors(os.getpid())
    assert found is not None
    assert "pytest" in found[0]


def test_off_refuses_when_ps_cannot_run(home, user_home, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setattr(commands, "PS", "/nonexistent/ps", raising=False)
    ctx = make_ctx(user_home)
    ctx.ancestors = process_ancestors
    assert run(ctx, "off") == (1, "", f"scars off: {UNKNOWN}\n")
    assert not paths.kill_switch().exists()


def test_ps_that_fails_gives_no_ancestry(monkeypatch, tmp_path):
    fake = tmp_path / "ps"
    fake.write_text("#!/bin/sh\necho '  1 /bin/zsh'\nexit 1\n")
    fake.chmod(0o755)
    monkeypatch.setattr(commands, "PS", str(fake), raising=False)
    assert process_ancestors(os.getpid()) is None


def test_ps_with_empty_output_gives_no_ancestry(monkeypatch, tmp_path):
    fake = tmp_path / "ps"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setattr(commands, "PS", str(fake), raising=False)
    assert process_ancestors(os.getpid()) is None


@pytest.mark.parametrize("found", [None, []], ids=["unknown", "empty"])
def test_refusal_when_ancestry_is_unknown_or_empty(user_home, found):
    ctx = make_ctx(user_home)
    ctx.ancestors = lambda: found
    assert refusal(ctx) == UNKNOWN.removeprefix("refused: ")


@pytest.mark.parametrize("variable", ["CLAUDECODE", "CODEX_SANDBOX"])
def test_an_agent_variable_set_to_empty_still_refuses(home, user_home, variable):
    ctx = make_ctx(user_home, env={variable: ""})
    assert run(ctx, "off") == (
        1,
        "",
        f"scars off: refused: {variable} is set, so this looks like an agent session; "
        "run it in your own terminal\n",
    )
    assert not paths.kill_switch().exists()
