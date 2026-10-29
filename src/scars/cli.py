import argparse

from scars import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scars", description="Lessons from past agent sessions.")
    parser.add_argument("--version", action="version", version=f"scars {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    build_parser().parse_args(argv)
    return 0
