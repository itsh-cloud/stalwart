#!/usr/bin/env python3
"""Fail if any SEL-licensed source file is reachable without the enterprise feature.

The ITSH image is built without the "enterprise" cargo feature so that the
resulting binary is AGPL-3.0-only and redistributable. That only holds while
every file licensed solely under LicenseRef-SEL stays behind the feature gate.
Upstream could add an ungated one during a rebase, so check it at build time.

A file counts as gated when its own `mod <name>;` declaration, or that of any
directory module containing it, is preceded by #[cfg(feature = "enterprise")].
"""

import pathlib
import re
import sys

GATE = '#[cfg(feature = "enterprise")]'
SEL = "SPDX-License-Identifier: LicenseRef-SEL"
DUAL = "AGPL-3.0-only OR LicenseRef-SEL"


def is_gated(name: str, crate: pathlib.Path) -> bool:
    """True if some `mod <name>;` inside this crate carries the enterprise gate."""
    pattern = re.compile(rf"^\s*(pub(\([^)]*\))?\s+)?mod\s+{re.escape(name)}\s*;", re.M)
    for cand in crate.rglob("*.rs"):
        body = cand.read_text(encoding="utf-8", errors="replace")
        for match in pattern.finditer(body):
            preceding = body[: match.start()].splitlines()[-3:]
            if any(GATE in line for line in preceding):
                return True
    return False


ungated = []
for path in sorted(pathlib.Path("crates").rglob("*.rs")):
    text = path.read_text(encoding="utf-8", errors="replace")
    if SEL not in text or DUAL in text:
        continue  # not SEL-only

    # crates/<crate>/src/... -> the crate root we search for module declarations
    parts = path.parts
    crate = pathlib.Path(*parts[:2])

    # The file's own module name, plus every directory module above it up to src/.
    names = [path.stem]
    for parent in path.parents:
        if parent.name == "src" or parent == crate:
            break
        names.append(parent.name)

    if not any(is_gated(name, crate) for name in names):
        ungated.append(str(path))

if ungated:
    print("SEL-licensed files reachable without the enterprise feature:")
    for path in ungated:
        print(f"  {path}")
    sys.exit(1)

print("All SEL-licensed files are gated behind the enterprise feature.")
