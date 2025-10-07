import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from telegram import commands

print(commands.holdings())
print(commands.totals())
print(commands.daily())
