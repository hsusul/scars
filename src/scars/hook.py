"""Entry point for `scars hook <harness> <event>`. See ARCHITECTURE "Hook contract".

The hook fails open. Every path returns exit code 0, and stdout carries either nothing or one valid
JSON object. A fault in scars must never block or slow an agent's tool call.
"""

import os
import sys
import traceback
from typing import TextIO

from scars import paths
from scars.model import Harness


def run(harness: Harness, event_name: str, stdin: TextIO, stdout: TextIO) -> None:
    raise NotImplementedError("B4 hook")


def main(harness: str, event_name: str) -> int:
    if paths.is_off():
        return 0
    try:
        run(Harness(harness), event_name, sys.stdin, sys.stdout)
    except Exception:
        if os.environ.get("SCARS_DEBUG"):
            log = paths.home() / "logs" / "hook.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a") as f:
                f.write(traceback.format_exc())
    return 0
