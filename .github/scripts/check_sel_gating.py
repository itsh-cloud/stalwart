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
such a cfg among the attributes directly above it or leading its line. Match
arm alternatives and operators that open a line continue the item above. For
the first item those attributes may sit above the SnippetBegin marker, so a gate
can be added without editing the SEL-licensed text. A snippet that only
continues a statement begun outside it cannot be gated and fails.

Cargo features are per crate, so every gate is evaluated against the features
`cargo tree` resolves for the crate that owns the file, and the check fails
outright if any compiled crate has `enterprise` or `dev_mode` on. A line naming
`SPDX-SnippetBegin` or `LicenseRef-SEL` in a form the check does not parse fails
as well, so a marker cannot slip past it by being written differently.

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

# Dockerfile.itsh builds the stalwart crate --no-default-features with these.
IMAGE_FEATURES = set(re.search(
    r'^ARG FEATURES="([^"]+)"', pathlib.Path("Dockerfile.itsh").read_text(), re.M,
).group(1).split())
# Features that must stay off in every compiled crate.
FORBIDDEN_FEATURES = {"enterprise", "dev_mode"}

ATTR_RE = re.compile(r"#\[cfg\((.*)\)\]")
# Workspace members that -p stalwart does not compile with IMAGE_FEATURES.
UNBUILT_CRATES = {"crates/scim", "crates/scim-proto"}

# A package line of `cargo tree --format "{p} {f}"`: name, version, an optional
# proc-macro marker, the `(/abs/path)` of a path dependency, then its features.
PACKAGE_RE = re.compile(r"^\S+ v\S+(?: \(proc-macro\))? \((/[^)]+)\)(?: \(\*\))?(?: (\S*))?")

# The marker lines the check understands; any other line holding one of
# MARKERS fails. File headers sit in the leading block comment.
MARKERS = ("SPDX-SnippetBegin", "LicenseRef-SEL")
SNIPPET_BEGIN = "// SPDX-SnippetBegin"
SNIPPET_LICENSE = "// SPDX-License-Identifier: LicenseRef-SEL"
HEADER_LICENSES = (f"* {SEL}", f"* SPDX-License-Identifier: {DUAL}")

# Features of each compiled crate directory, one set per resolved instance.
CrateFeatures = dict[pathlib.Path, list[frozenset[str]]]


def built_crates(root: pathlib.Path) -> CrateFeatures:
    """Crate directories, relative to root, that `-p stalwart` compiles here,
    with the features cargo resolves for each."""
    tree = subprocess.run(
        ["cargo", "tree", "--locked", "-p", "stalwart", "--no-default-features",
         "--features", ",".join(sorted(IMAGE_FEATURES)),
         "--edges", "normal,build,features", "--target", "all",
         "--prefix", "none", "--format", "{p} {f}"],
        cwd=root, check=True, stdout=subprocess.PIPE, text=True,
    ).stdout
    crates: CrateFeatures = {}
    for line in tree.splitlines():
        match = PACKAGE_RE.match(line)
        if not match or not pathlib.Path(match.group(1)).is_relative_to(root):
            continue
        features = frozenset(filter(None, (match.group(2) or "").split(",")))
        sets = crates.setdefault(pathlib.Path(match.group(1)).relative_to(root), [])
        if features not in sets:
            sets.append(features)
    built = set(crates)
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
    forbidden = sorted(f"{crate}: {feature}" for crate, sets in crates.items()
                       for features in sets for feature in features & FORBIDDEN_FEATURES)
    if forbidden:
        sys.exit("features this build must not enable are on: " + ", ".join(forbidden))
    return crates


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


def eval_cfg(expr: str, features: frozenset[str]) -> tuple[bool | None, str]:
    """Evaluate a cfg predicate for a release build of a crate with these
    features: True, False or None (unknown).

    Returns the value and the unparsed remainder of expr.
    """
    expr = expr.lstrip()
    op = re.match(r"(all|any|not)\s*\(", expr)
    if op:
        rest, values = expr[op.end():], []
        while not rest.lstrip().startswith(")"):
            value, rest = eval_cfg(rest, features)
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
        return feature.group(1) in features, expr[feature.end():]
    other = re.match(r'[A-Za-z_]\w*(\s*=\s*"[^"]*")?', expr)
    if not other:
        raise ValueError(f"cannot parse cfg: {expr!r}")
    known = {"test": False, "debug_assertions": False}
    return known.get(other.group(0)), expr[other.end():]


def gate_excludes(line: str, feature_sets: list[frozenset[str]]) -> bool:
    """True if line holds a #[cfg(...)] that is definitely false for every
    resolved feature set of its crate."""
    attr = ATTR_RE.search(line)
    return attr is not None and all(
        eval_cfg(attr.group(1), features)[0] is False for features in feature_sets)


def is_excluded(declarer: pathlib.Path, name: str,
                feature_sets: list[frozenset[str]]) -> bool:
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
            if gate_excludes(stripped, feature_sets):
                return True
            if not stripped.startswith("#["):
                break
    return False


def split_attribute(text: str) -> tuple[str, str] | None:
    """Split text starting with `#[` into that attribute and the rest of it.

    None if the attribute's brackets do not close within text.
    """
    depth = 0
    for pos, char in enumerate(text):
        if char in "[]":
            depth += 1 if char == "[" else -1
            if depth == 0:
                return text[:pos + 1], text[pos + 1:]
    return None


def unrecognised_markers(text: str) -> list[int]:
    """Line numbers of marker lines in a form the check does not parse."""
    lines = text.splitlines()
    header_end = next((n for n, line in enumerate(lines) if line.strip() == "*/"), -1)
    bad = []
    for n, line in enumerate(lines):
        stripped = line.strip()
        if not any(marker in stripped for marker in MARKERS):
            continue
        if stripped == SNIPPET_BEGIN:
            license_line = next((l.strip() for l in lines[n + 1:n + 4]
                                 if "SPDX-License-Identifier:" in l), "")
            if license_line.startswith("// SPDX-License-Identifier: "):
                continue
        elif stripped == SNIPPET_LICENSE:
            if any(l.strip() == SNIPPET_BEGIN for l in lines[max(n - 3, 0):n]):
                continue
        elif stripped in HEADER_LICENSES and n < header_end:
            continue
        bad.append(n + 1)
    return bad


def ungated_snippets(text: str,
                     feature_sets: list[frozenset[str]]) -> tuple[int, list[int]]:
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
        complete = True
        i = start
        while i < end:
            line = lines[i]
            i += 1
            stripped = line.strip()
            if not stripped or stripped.startswith("//"):
                continue
            if len(line) - len(line.lstrip()) > indent:
                # Nested in an item above it, or, before the first item, in a
                # block opened outside the snippet that no gate here reaches.
                if not items:
                    complete = False
                    break
                continue
            # Attributes, which may span lines or share one with the item.
            while stripped.startswith("#["):
                while (split := split_attribute(stripped)) is None and i < end:
                    stripped += " " + lines[i].strip()
                    i += 1
                if split is None:
                    break
                gated = gated or gate_excludes(split[0], feature_sets)
                stripped = split[1].strip()
            if not stripped or stripped.startswith(("//", "#[")):
                continue
            # A method-chain link, the brace or operator after a multi-line
            # condition or match pattern, or a closing bracket followed by more
            # code (`} else {`) continues the statement above it. Before the
            # snippet's first item that statement began outside the snippet,
            # where no gate inside it can reach.
            closes = stripped[0] in "})]"
            continues = stripped[0] == "." or (
                open_expr and stripped.startswith(("{", "|", "&&"))) or (
                closes and stripped.lstrip("})]; ,") != "")
            open_expr = not stripped.endswith((";", "}", ","))
            if continues and not items:
                complete = False
                break
            if continues:
                continue
            if not closes:
                if not gated:
                    complete = False
                    break
                items += 1
            gated = False
        # Code that only continues a statement outside the snippet cannot
        # take a cfg of its own.
        if complete and (items or all(
                not l.strip() or l.strip().startswith("//") for l in region)):
            continue
        ungated.append(begin + 1)
    return count, ungated


def main() -> int:
    root = pathlib.Path(".")
    crates = built_crates(root.resolve())
    # Nested crates, such as crates/trc/event-macro, own their own files.
    crate_dirs = sorted(crates, key=lambda d: len(d.parts), reverse=True)
    ungated: list[str] = []
    unrecognised: list[str] = []
    checked = snippets = 0

    for path in sorted(root.rglob("*.rs")):
        # `target` and the unbuilt crates fall outside every crate the image compiles.
        crate = next((d for d in crate_dirs if path.is_relative_to(d)), None)
        if crate is None:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        unrecognised.extend(f"{path}:{n}" for n in unrecognised_markers(text))
        if SEL not in text:
            continue
        if DUAL not in text:
            checked += 1
            found = [str(path)]
        else:
            count, lines = ungated_snippets(text, crates[crate])
            snippets += count
            found = [f"{path}:{n}" for n in lines]
        if found and not any(is_excluded(d, n, crates[crate])
                             for d, n in declaring_files(path)):
            ungated.extend(found)

    if unrecognised:
        print("SPDX marker lines in a form this check does not parse:")
        for p in unrecognised:
            print(f"  {p}")
    if ungated:
        print("SEL-licensed files reachable in the ITSH build:")
        for p in ungated:
            print(f"  {p}")
    if unrecognised or ungated:
        print(f"\nchecked {checked} SEL-only files and {snippets} SEL-only "
              f"snippets; image features: {' '.join(sorted(IMAGE_FEATURES))}")
        return 1

    print(f"All {checked} SEL-only files and {snippets} SEL-only snippets are "
          f"gated behind features this build does not enable.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
