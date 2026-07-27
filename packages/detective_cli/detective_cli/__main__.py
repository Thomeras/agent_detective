"""``python -m detective_cli`` — same entry point as the ``detective`` script."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
