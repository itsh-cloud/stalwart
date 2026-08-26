# ITSH fork of Stalwart

This fork tracks upstream [stalwartlabs/stalwart](https://github.com/stalwartlabs/stalwart)
and carries one behavioural patch plus a build change. Everything else is upstream.

Base: **v0.15.5**. Branch: **`itsh/v0.15.5`**.

## The patch

`crates/store/src/search/query.rs` makes an IMAP/JMAP header search **intersect**
the query's tokens instead of unioning them.

Both the write and query sides tokenise a header value with `SpaceTokenizer`,
which splits on every non-alphanumeric character, and index one term per token
as `"<header-name> <token>"`. The query side then merged those term bitmaps with
`is_union = true`, so a message matched when it shared **any single token** with
the query. `SearchOperator::Equal` was accepted from the caller and then
discarded in that branch, so it had no effect.

The practical consequence is that Message-IDs sharing a domain all collide.
`<alpha@example.com>` tokenises to `[alpha, example, com]`, and any other
Message-ID ending in `@example.com` shares two of those three tokens, so it
matches. A value that was never indexed at all, such as
`<nonexistent@example.com>`, also matches.

This breaks mail clients that re-append a draft on every autosave under a new
Message-ID and then relocate it with `UID SEARCH HEADER Message-ID`. All
revisions share the client's Message-ID domain, so the search returns every
revision still in the folder instead of exactly one. With a single revision the
result is accidentally correct, which is why short messages appear to work and
longer compose sessions, where revisions accumulate, fail reliably.

RFC 3501 defines `HEADER <field> <value>` as matching when the header *contains*
the value, so requiring every token of the value to be present is both a closer
reading of the spec and a strict improvement on a union.

### Scope and cost

- `SearchField::is_json()` matches only `EmailSearchField::Headers`, so nothing
  outside header search changes.
- Only the internal store backend is touched. The Elasticsearch, Meilisearch,
  PostgreSQL and MySQL backends have separate implementations and are untouched.
- Indexed terms are unchanged, so **no reindex is required**. The patch changes
  how existing terms are combined at query time, not what is written.
- `merge_bitmaps` already implemented intersection and is used that way
  elsewhere, so this selects an existing code path rather than adding one.

### Residual behaviour

Term indexing carries no position information, so a header whose token set is a
superset of the query's still matches. Two values built from the same tokens in
a different order are also indistinguishable. Both are far narrower than the
previous union and neither affects Message-ID relocation, where revisions differ
in the local part.

### Accepting the change

Against a scratch mailbox, append three messages with Message-IDs
`<alpha@aaa.example>`, `<beta@aaa.example>` and `<gamma@zzz.invalid>`, then:

```
SEARCH HEADER Message-ID <alpha@aaa.example>        -> 1
SEARCH HEADER Message-ID <beta@aaa.example>         -> 2
SEARCH HEADER Message-ID <gamma@zzz.invalid>        -> 3
SEARCH HEADER Message-ID <nonexistent@aaa.example>  -> (no results)
```

Before the patch the first, second and fourth each returned both message 1 and
message 2.

## Licensing and the build

The tree is dual licensed. Most files are `AGPL-3.0-only OR LicenseRef-SEL`, and
a small set is `LicenseRef-SEL` only. Every SEL-only file sits behind the
`enterprise` cargo feature.

`Dockerfile.itsh` therefore builds **without** the `enterprise` feature. The
published image contains no SEL-licensed code and is AGPL-3.0-only, which is what
makes it redistributable. `.github/scripts/check_sel_gating.py` runs before every
image build and fails if upstream ever adds an SEL-licensed file that is
reachable without the feature.

The feature set is otherwise identical to upstream's image, and the runtime stage
mirrors upstream's musl/alpine one, so the result is a drop-in replacement for
`stalwartlabs/stalwart:<version>-alpine`. The gated features are inert without a
licence key in any case.

Upstream's `Dockerfile` and `Dockerfile.build` are left untouched so they do not
conflict on rebase. `Dockerfile.itsh` drops the sccache and FoundationDB
machinery from `Dockerfile.build`, neither of which is used here.

Publishing this fork also satisfies AGPL-3.0 section 13, which requires offering
the corresponding source of a modified version that users interact with over a
network.

Patched files are `AGPL-3.0-only OR LicenseRef-SEL`; the modifications here are
taken under AGPL-3.0-only.

## Versioning

Tags are `v<upstream-version>-itsh.<n>`, for example `v0.15.5-itsh.1`. The
counter restarts at 1 for each new upstream base. Pushing such a tag builds and
publishes `ghcr.io/itsh-cloud/stalwart:<upstream-version>-itsh.<n>`, along with
a `sha-<commit>` tag.

`0.15.5-itsh.1` is a valid semantic version whose prerelease segment sorts our
builds correctly among themselves. It sorts below a bare `0.15.5`, which is
harmless because this image repository only ever holds builds from this fork.

## Rebasing onto a new upstream release

```sh
git fetch upstream --tags
git switch -c itsh/vX.Y.Z vX.Y.Z
git cherry-pick <patch commit from the previous itsh branch>
```

The patched hunk in `crates/store/src/search/query.rs` is unchanged between
v0.15.5 and v0.16.19, so the patch is expected to apply cleanly. Re-run the
acceptance check above afterwards, then tag `vX.Y.Z-itsh.1`.

Upstream's `ci.yml`, `scorecard.yml` and `trivy.yml` are removed on this branch.
`ci.yml` triggers on `v*.*.*`, which our tags match, and it expects signing and
registry credentials this fork does not have.

## Contributing upstream

This patch is not offered upstream. Upstream `CONTRIBUTING.md` requires vouched
contributor status and a signed licensing agreement, so the fork is maintained
here instead.
