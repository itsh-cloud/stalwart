#!/usr/bin/env python3
"""Fail if any SEL-licensed source file is reachable in the ITSH build.

The ITSH image is built without the "enterprise" cargo feature so that the
resulting binary is AGPL-3.0-only and redistributable. That only holds while
every file licensed solely under LicenseRef-SEL stays behind a cfg gate whose
features this build does not enable. Upstream can add an ungated one during a
rebase, so check it at build time.

A SEL-only file is excluded from the build when its own `mod <name>;`
declaration, or that of any module above it in its real parent chain, carries a
`#[cfg(...)]` that references at least one feature and no feature this build
enables. Gating on a feature we DO enable (for example `postgres`) excludes
nothing and is treated as ungated.

ENABLED_FEATURES must match the FEATURES build arg in Dockerfile.itsh.
"""

from __future__ import annotations

import pathlib
import re
import sys

SEL = "SPDX-License-Identifier: LicenseRef-SEL"
DUAL = "AGPL-3.0-only OR LicenseRef-SEL"

# Mirrors ARG FEATURES in Dockerfile.itsh, which is built --no-default-features.
ENABLED_FEATURES = {
    "sqlite", "postgres", "mysql", "rocks", "s3", "redis", "azure", "nats",
}

FEATURE_RE = re.compile(r'feature\s*=\s*"([^"]+)"')
ATTR_RE = re.compile(r"#\[cfg\((.*)\)\]")


def declaring_files(path: pathlib.Path) -> list[tuple[pathlib.Path, str]]:
    """The (module file, module name) pairs that make up this file's parent chain.

    For a/b/c/foo.rs the declarer is a/b/c/mod.rs or a/b/c.rs declaring `mod foo;`,
    then that file's own declarer, and so on up to the crate root.
    """
    chain: list[tuple[pathlib.Path, str]] = []
    current = path
    while True:
        name = current.stem
        parent = current.parent
        if name == "mod":
            # a/b/mod.rs is declared as `mod b;` by a/b's parent
            name = parent.name
            parent = parent.parent
        if name in ("lib", "main") or parent.name == "" or not parent.parts:
            break
        candidates = [parent / "mod.rs", parent.with_suffix(".rs"),
                      parent / "lib.rs", parent / "main.rs"]
        declarer = next((c for c in candidates if c.is_file() and c != current), None)
        if declarer is None:
            break
        chain.append((declarer, name))
        if declarer.stem in ("lib", "main"):
            break
        current = declarer
    return chain


def gate_excludes(attr_body: str) -> bool:
    """True if this cfg references features and none of them are enabled here."""
    features = FEATURE_RE.findall(attr_body)
    if not features:
        return False
    return not any(f in ENABLED_FEATURES for f in features)


def is_excluded(declarer: pathlib.Path, name: str) -> bool:
    """True if `mod <name>;` in declarer is behind a cfg this build disables."""
    body = declarer.read_text(encoding="utf-8", errors="replace")
    pattern = re.compile(
        rf"^\s*(?:pub(?:\([^)]*\))?\s+)?mod\s+{re.escape(name)}\s*;", re.M
    )
    for match in pattern.finditer(body):
        preceding = body[: match.start()].splitlines()
        # Attributes directly above the declaration, skipping blank/comment lines.
        for line in reversed(preceding):
            stripped = line.strip()
            if not stripped or stripped.startswith("//"):
                continue
            attr = ATTR_RE.search(stripped)
            if attr and gate_excludes(attr.group(1)):
                return True
            if not stripped.startswith("#["):
                break
    return False


def main() -> int:
    root = pathlib.Path(".")
    ungated: list[str] = []
    checked = 0

    for path in sorted(root.rglob("*.rs")):
        # `tests` is its own workspace member and is not a dependency of the
        # `stalwart` package, which is the only one Dockerfile.itsh builds
        # (-p stalwart), so nothing under it reaches the image.
        if "target" in path.parts or path.parts[0] == "tests":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if SEL not in text or DUAL in text:
            continue  # dual-licensed or not SEL at all
        checked += 1
        if not any(is_excluded(d, n) for d, n in declaring_files(path)):
            ungated.append(str(path))

    if ungated:
        print("SEL-licensed files reachable in the ITSH build:")
        for p in ungated:
            print(f"  {p}")
        print(f"\nchecked {checked} SEL-only files; enabled features: "
              f"{' '.join(sorted(ENABLED_FEATURES))}")
        return 1

    print(f"All {checked} SEL-only files are gated behind features this build "
          f"does not enable.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
