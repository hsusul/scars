"""The run-wide guard in conftest notices a created, changed or removed file."""

import os

from conftest import changed, real_home_state

pytest_plugins = ("cli_fixtures",)


def test_the_guard_sees_files_created_changed_and_removed(tmp_path):
    tree = tmp_path / ".scars"
    tree.mkdir()
    (tree / "lessons.db").write_text("a")
    hooks = tmp_path / ".codex" / "hooks.json"
    watched = {"trees": (tree,), "files": (hooks,)}
    before = real_home_state(**watched)
    assert changed(before, real_home_state(**watched)) == []
    hooks.parent.mkdir()
    hooks.write_text("{}")
    (tree / "OFF").touch()
    os.utime(tree / "lessons.db", ns=(1, 1))
    after = real_home_state(**watched)
    assert changed(before, after) == [str(hooks), str(tree), str(tree / "OFF"), str(tree / "lessons.db")]
    (tree / "OFF").unlink()
    os.utime(tree / "lessons.db", ns=(1, 1))
    assert str(tree / "OFF") in changed(after, real_home_state(**watched))


def test_the_guard_ignores_what_a_live_install_writes_but_not_the_store(tmp_path):
    tree = tmp_path / ".scars"
    (tree / "logs").mkdir(parents=True)
    (tree / "lessons.db").write_text("a")
    (tree / "telemetry.db").write_text("t")
    watched = {"trees": (tree,), "files": ()}
    before = real_home_state(**watched)
    (tree / "telemetry.db").write_text("tt")
    for name in ("telemetry.db-wal", "telemetry.db-shm", "lessons.db-shm", "lessons.db-wal"):
        (tree / name).write_text("x")
    (tree / "logs" / "hook.log").write_text("line")
    (tree / "lessons.db-shm").unlink()
    assert changed(before, real_home_state(**watched)) == []
    (tree / "lessons.db").write_text("ab")
    (tree / "OFF").touch()
    after = real_home_state(**watched)
    assert changed(before, after) == [str(tree), str(tree / "OFF"), str(tree / "lessons.db")]


def test_home_is_a_temp_dir_in_every_cli_test(tmp_path):
    from conftest import REAL_HOME

    assert os.environ["HOME"] == str(tmp_path / "user")
    assert not os.environ["HOME"].startswith(str(REAL_HOME))
