from __future__ import annotations

"""Local smoke check for Telegram command outputs."""

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from telegram import commands


def main() -> None:
    print(commands.holdings())
    print()
    print(commands.totals())
    print()
    print(commands.daily())


if __name__ == "__main__":
    main()
