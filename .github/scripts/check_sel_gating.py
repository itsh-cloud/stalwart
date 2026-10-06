#!/usr/bin/env python3
"""Fail if any SEL-licensed source file is reachable in the ITSH build.

The ITSH image is built without the "enterprise" cargo feature so that the
resulting binary is AGPL-3.0-only and redistributable. That only holds while
every file licensed solely under LicenseRef-SEL stays behind a cfg gate whose
features this build does not enable. Upstream can add an ungated one during a
rebase, so check it at build time.

A SEL-only file is excluded from the build when its own `mod <name>;`
declaration, or that of any module above it in its real parent chain, carries a
`#[cfg(...)]` that is false for this build. Gating on a feature we DO enable
(for example `postgres`) excludes nothing and is treated as ungated.

SEL-only snippets (SPDX-SnippetBegin/End) inside dual-licensed files are held
to the same rule: every item or statement at the snippet's own indentation needs
such a cfg among the attributes directly above it. For the first one those may
sit above the SnippetBegin marker, so a gate can be added without editing the
SEL-licensed text. A snippet that only continues a statement begun outside it
cannot be gated and fails.

Files of a workspace crate this build never compiles, such as an optional
dependency that only `enterprise` turns on, cannot reach the image and are
skipped. `cargo tree` decides which crates are compiled, and the crates it
leaves out must be exactly UNBUILT_CRATES.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

SEL = "SPDX-License-Identifier: LicenseRef-SEL"
DUAL = "AGPL-3.0-only OR LicenseRef-SEL"

# Dockerfile.itsh builds --no-default-features with exactly these.
ENABLED_FEATURES = set(re.search(
    r'^ARG FEATURES="([^"]+)"', pathlib.Path("Dockerfile.itsh").read_text(), re.M,
).group(1).split())

ATTR_RE = re.compile(r"#\[cfg\((.*)\)\]")
# Workspace members that -p stalwart does not compile with ENABLED_FEATURES.
UNBUILT_CRATES = {"tests", "crates/scim", "crates/scim-proto"}

# cargo tree's `(/abs/path)` suffix on path dependencies.
CRATE_PATH_RE = re.compile(r"\((/[^)]+)\)")


def built_crate_dirs(root: pathlib.Path) -> list[pathlib.Path]:
    """Crate directories, relative to root, that `-p stalwart` compiles here."""
    tree = subprocess.run(
        ["cargo", "tree", "--locked", "-p", "stalwart", "--no-default-features",
         "--features", ",".join(sorted(ENABLED_FEATURES)),
         "--edges", "normal,build", "--target", "all",
         "--prefix", "none", "--format", "{p}"],
        cwd=root, check=True, stdout=subprocess.PIPE, text=True,
    ).stdout
    dirs = {pathlib.Path(m) for m in CRATE_PATH_RE.findall(tree)}
    built = {d.relative_to(root) for d in dirs if d.is_relative_to(root)}
    metadata = json.loads(subprocess.run(
        ["cargo", "metadata", "--locked", "--no-deps", "--format-version", "1"],
        cwd=root, check=True, stdout=subprocess.PIPE, text=True,
    ).stdout)
    members = {pathlib.Path(p["manifest_path"]).parent.relative_to(root)
               for p in metadata["packages"]}
    unbuilt = {str(m) for m in members - built}
    if unbuilt != UNBUILT_CRATES:
        sys.exit(f"crates not compiled by this build: {sorted(unbuilt)}, "
                 f"expected {sorted(UNBUILT_CRATES)}")
    return sorted(built)


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


def eval_cfg(expr: str) -> tuple[bool | None, str]:
    """Evaluate a cfg predicate for this release build: True, False or None (unknown).

    Returns the value and the unparsed remainder of expr.
    """
    expr = expr.lstrip()
    op = re.match(r"(all|any|not)\s*\(", expr)
    if op:
        rest, values = expr[op.end():], []
        while not rest.lstrip().startswith(")"):
            value, rest = eval_cfg(rest)
            values.append(value)
            rest = rest.lstrip().removeprefix(",")
        rest = rest.lstrip()[1:]
        if op.group(1) == "not":
            return (None if values[0] is None else not values[0]), rest
        decisive = op.group(1) == "any"  # one True settles any(), one False all()
        if decisive in values:
            return decisive, rest
        if None in values:
            return None, rest
        return not decisive, rest
    feature = re.match(r'feature\s*=\s*"([^"]+)"', expr)
    if feature:
        return feature.group(1) in ENABLED_FEATURES, expr[feature.end():]
    other = re.match(r'[A-Za-z_]\w*(\s*=\s*"[^"]*")?', expr)
    if not other:
        raise ValueError(f"cannot parse cfg: {expr!r}")
    known = {"test": False, "debug_assertions": False}
    return known.get(other.group(0)), expr[other.end():]


def gate_excludes(line: str) -> bool:
    """True if line holds a #[cfg(...)] that is definitely false for this build."""
    attr = ATTR_RE.search(line)
    return attr is not None and eval_cfg(attr.group(1))[0] is False


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
            if gate_excludes(stripped):
                return True
            if not stripped.startswith("#["):
                break
    return False


def ungated_snippets(text: str) -> tuple[int, list[int]]:
    """Count SEL-only snippets and return the begin lines of ungated ones."""
    lines = text.splitlines()
    count, ungated = 0, []
    for begin, marker in enumerate(lines):
        if marker.strip() != "// SPDX-SnippetBegin":
            continue
        end = next((j for j in range(begin + 1, len(lines))
                    if "SPDX-SnippetEnd" in lines[j]), len(lines))
        region = lines[begin + 1:end]
        if not any(SEL in line for line in region) or any(DUAL in line for line in region):
            continue
        count += 1
        # Start at the attributes directly above the marker; the marker itself
        # is skipped as a comment.
        start = begin
        while start > 0 and lines[start - 1].strip().startswith(("#[", "//")):
            start -= 1
        indent = len(marker) - len(marker.lstrip())
        gated = open_expr = False
        items = 0
        for line in lines[start:end]:
            stripped = line.strip()
            if (not stripped or stripped.startswith("//")
                    or len(line) - len(line.lstrip()) > indent):
                continue
            if stripped.startswith("#["):
                gated = gated or gate_excludes(stripped)
                continue
            # A method-chain link, or the brace after a multi-line condition,
            # continues the statement above it.
            continues = stripped[0] == "." or (stripped[0] == "{" and open_expr)
            open_expr = not stripped.endswith((";", "}", ","))
            if continues:
                continue
            if stripped[0] not in "})]":
                if not gated:
                    break
                items += 1
            gated = False
        else:
            # Code that only continues a statement outside the snippet cannot
            # take a cfg of its own.
            if items or all(not l.strip() or l.strip().startswith("//") for l in region):
                continue
        ungated.append(begin + 1)
    return count, ungated


def main() -> int:
    root = pathlib.Path(".")
    built = built_crate_dirs(root.resolve())
    ungated: list[str] = []
    checked = snippets = 0

    for path in sorted(root.rglob("*.rs")):
        # `tests` and `target` fall outside every crate the image compiles.
        if not any(path.is_relative_to(d) for d in built):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if SEL not in text:
            continue
        if DUAL not in text:
            checked += 1
            found = [str(path)]
        else:
            count, lines = ungated_snippets(text)
            snippets += count
            found = [f"{path}:{n}" for n in lines]
        if found and not any(is_excluded(d, n) for d, n in declaring_files(path)):
            ungated.extend(found)

    if ungated:
        print("SEL-licensed files reachable in the ITSH build:")
        for p in ungated:
            print(f"  {p}")
        print(f"\nchecked {checked} SEL-only files and {snippets} SEL-only "
              f"snippets; enabled features: "
              f"{' '.join(sorted(ENABLED_FEATURES))}")
        return 1

    print(f"All {checked} SEL-only files and {snippets} SEL-only snippets are "
          f"gated behind features this build does not enable.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
