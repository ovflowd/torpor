"""`python -m torpor agent …` runs torpord, `python -m torpor ctl …` runs torporctl. The zipapp's entry."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["agent"]:
        from torpor.agent import main as agent

        return agent(args[1:])
    if args[:1] == ["ctl"]:
        from torpor.cli import main as ctl

        return ctl(args[1:])
    print("usage: python -m torpor {agent,ctl} …", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
