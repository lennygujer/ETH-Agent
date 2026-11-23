"""Repariert Dateinamen wie '#U00dcbungsstunden' -> 'Übungsstunden' nach dem Entpacken.

Aufruf: python scripts/fix_zip_names.py ~/eth-agent/material/"Physik 2"
"""

import os
import re
import sys


def fix(name: str) -> str:
    return re.sub(r"#U([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), name)


def main(root: str) -> None:
    for d, dirs, files in os.walk(root, topdown=False):
        for n in files + dirs:
            new = fix(n)
            if new != n:
                os.rename(os.path.join(d, n), os.path.join(d, new))
                print(f"{n} -> {new}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
