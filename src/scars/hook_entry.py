"""The `scars-hook <harness> <event>` console script that agent hooks run.

It is separate from `scars.cli` so a hook run never imports `argparse` or the CLI, and it checks the kill
switch before importing the hook pipeline, so a disabled hook costs little more than starting Python.
Whatever happens, it exits 0 with nothing on stdout or stderr, even when a module of a broken install
fails to import.
"""

import sys
import time


def main() -> None:
    started = time.perf_counter()
    try:
        from scars import paths

        args = sys.argv[1:]
        if len(args) == 2 and not paths.is_off():
            from scars import hook

            hook.main(args[0], args[1], started=started)
    except Exception:
        pass
    sys.exit(0)
