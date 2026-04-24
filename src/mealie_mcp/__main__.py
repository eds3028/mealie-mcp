"""CLI entry point: ``python -m mealie_mcp`` or the ``mealie-mcp`` script."""

from __future__ import annotations

from dotenv import load_dotenv

from .server import run


def main() -> None:
    load_dotenv()
    run()


if __name__ == "__main__":
    main()
