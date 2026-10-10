"""Spots on the command line: ``--anchor -3096.573,-848.458,67.991,-41.65``.

argparse takes a value that starts with a minus sign for an option of its own (only a plain
negative number passes), so a spot west or south of Night City's centre was refused with
"expected one argument" -- the map deploy then left Sea Dome at its first spot instead of the one
it had been moved to.  ``glue_vectors`` turns ``--anchor <list>`` into ``--anchor=<list>`` before
argparse sees it."""
from __future__ import annotations

import re
import sys

VECTOR_OPTIONS = ("--anchor", "--place", "--coords")
_NUMBERS = re.compile(r"^-\.?\d[\d.eE]*(,\s*[-+]?\.?\d[\d.eE+-]*)*$")


def glue_vectors(argv=None, names=VECTOR_OPTIONS) -> list:
    argv = list(sys.argv[1:] if argv is None else argv)
    out, i = [], 0
    while i < len(argv):
        a = argv[i]
        if a in names and i + 1 < len(argv) and _NUMBERS.match(argv[i + 1]):
            out.append(f"{a}={argv[i + 1]}")
            i += 2
            continue
        out.append(a)
        i += 1
    return out
