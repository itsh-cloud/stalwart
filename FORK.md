# ITSH fork of Stalwart

This fork tracks upstream [stalwartlabs/stalwart](https://github.com/stalwartlabs/stalwart)
and carries two behavioural patches plus a build change. Everything else is
upstream.

Base: **v0.16.19**. Branch: **`itsh/v0.16.19`**.

Previous base: v0.15.5 on `itsh/v0.15.5`, kept for the rollback image.

## The header search patch

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

Header search is opt-in. `storage.search-index.email.fields` must be set and
must include `email-headers`, because the indexing path has no "empty means index
everything" fallback for headers. Without it no header terms are written at all
and every `HEADER` search returns nothing, which looks like a broken patch rather
than a missing setting. Note the `storage.` prefix: `search-index.email.fields`
is silently accepted and never read.

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

The client-shaped version of the same check: append several messages whose
Message-IDs differ only in the local part, as a draft client does on each
autosave. Before the patch every one of them matches all of the others; after it
each resolves to exactly one message.

`SEARCH HEADER Message-ID aaa` still matches both `<alpha@aaa.example>` and
`<beta@aaa.example>`, which is correct: both headers do contain that token.

## The S3 retry patch

`crates/store/src/backend/s3/mod.rs` gives transport failures the same retry
budget that HTTP 5xx responses already had.

Upstream retries a 5xx status up to `maxRetries` times with exponential backoff,
but a transport failure (connect timeout, total-request timeout, a reset
mid-upload) leaves through `.map_err(into_error)?` on the first attempt, so the
budget only ever applied to requests that reached a server-side decision. A blob
write sits on the SMTP DATA path, so one timed-out PUT refuses the message with
`451 4.3.5` and it is never queued: the sending client keeps it and nothing
retries server-side.

All four call sites are covered, `get_blob`, `put_blob`, the `verifyAfterWrite`
HEAD and `delete_blob`, via two helpers: `consume_retry` for the shared backoff
and `retry_or_fail` for the budget decision.

### Scope and cost

Only transport-class errors are retried, matched positively as
`S3Error::Reqwest | S3Error::Io | S3Error::Http`. A deterministic fault such as a
signing, URL or encoding error surfaces immediately rather than spending the
budget asleep before returning the identical error. `S3Error` is
`#[non_exhaustive]`, so a positive list is the safe shape: a transport variant
added upstream falls back to today's fail-fast behaviour instead of silently
retrying something deterministic.

Every retried operation is idempotent. Object keys are content-derived, so a
repeated PUT writes identical bytes.

`rust-s3` runs its own retry underneath this one. `RETRIES` defaults to 1 and
Stalwart never calls `set_retries`, so each request already makes up to two HTTP
attempts with a 1s sleep between them. Worst-case wall time is therefore driven
by the configured request `timeout` and multiplies with it, not by the backoff,
which totals 7s at `maxRetries: 3`.

### Residual behaviour

A transport failure that is retried and then succeeds emits no event, so
`store_s3_error` counts terminal failures only. That is the useful definition for
alerting, at the cost of no signal for transient trouble that was absorbed. The
histograms that would show it as latency, `STORE_BLOB_WRITE_TIME` and
`STORE_BLOB_READ_TIME`, sit in the enterprise-only set in
`crates/trc/src/ipc/metrics.rs` and are unavailable on this build. Adding a
counter would mean editing `crates/trc/src/event/enums.rs` and `enums_impl.rs`,
both marked auto-generated, and bumping `TOTAL_EVENT_COUNT`, which sizes fixed
arrays and bitsets. That is why the blind spot is recorded here rather than
closed.

Nothing cancels the retry loop when a client disconnects: `handle_conn` wraps
only the socket read in a timeout, not `ingest`. A client that gives up mid-write
leaves the server to finish and queue the message anyway, so a resend can deliver
twice. Keeping the request `timeout` low bounds this.

## Licensing and the build

The tree is dual licensed. Most files are `AGPL-3.0-only OR LicenseRef-SEL`, and
a small set is `LicenseRef-SEL` only. At v0.16.19 there are 19 such files under
`crates/`, and each sits behind a cargo feature this build does not enable:
`enterprise` for all but one, and `dev_mode`/`test_mode` for
`crates/common/src/telemetry/metrics/test_data.rs`.

`Dockerfile.itsh` therefore builds **without** the `enterprise` feature. The
published image contains no SEL-licensed code and is AGPL-3.0-only, which is what
makes it redistributable. `.github/scripts/check_sel_gating.py` runs before every
image build and fails if upstream ever adds an SEL-licensed file that is
reachable in this build.

The check resolves each SEL-only file's real parent module chain and accepts a
gate only when it names at least one feature and none of the features this build
enables. Gating on a feature we *do* enable excludes nothing: at v0.16.19 the
three `crates/store/src/backend/composite/*.rs` files carry an inner
`#[cfg(any(feature = "postgres", feature = "mysql"))]`, and both are enabled
here, so they are safe solely because their parent `composite` module is gated.
The check is negative-tested against a planted ungated file, a correctly gated
one, and one gated on an enabled feature.

`tests/` is skipped: it is a separate workspace member and not a dependency of
the `stalwart` package, which is the only one `Dockerfile.itsh` builds.

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

Tags are `v<upstream-version>-itsh.<n>`, for example `v0.16.19-itsh.1`. The
counter restarts at 1 for each new upstream base. Pushing such a tag builds and
publishes `ghcr.io/itsh-cloud/stalwart:<upstream-version>-itsh.<n>`, along with
a `sha-<commit>` tag.

`0.16.19-itsh.1` is a valid semantic version whose prerelease segment sorts our
builds correctly among themselves. It sorts below a bare `0.16.19`, which is
harmless because this image repository only ever holds builds from this fork.

## Rebasing onto a new upstream release

```sh
git fetch upstream --tags
git switch -c itsh/vX.Y.Z vX.Y.Z
git cherry-pick <both patch commits from the previous itsh branch>
```

The patched hunk in `crates/store/src/search/query.rs` was byte-identical
between v0.15.5 and v0.16.19 (blob `171ca4a6`), so the cherry-pick applied
cleanly. Re-run the acceptance check above afterwards, then tag `vX.Y.Z-itsh.1`.

The S3 retry patch touches `crates/store/src/backend/s3/mod.rs` only. Check after
rebasing that upstream has not adopted its own transport retry, in which case the
patch should be dropped rather than merged, and that `S3Error` has gained no new
transport-class variant the positive match in `retry_or_fail` would miss.

The patch is **more** necessary at 0.16 than at 0.15. v0.16.19 removed the
`ContentType`/`Received` gate in `crates/email/src/message/index/search.rs`, so
every non-address header is now raw-tokenised into the `Headers` field and the
union bug over-matches across more headers than it did at 0.15.5.

It does **not** survive into 1.0: on `upstream/v1.0.0`,
`crates/store/src/search/query.rs` contains neither `is_json` nor
`merge_bitmaps`, and the directory gains `codec.rs`/`tokenize.rs` while losing
`bm_u32.rs`/`bm_u64.rs`. The fix has to be re-derived against that structure.

Upstream workflows removed on this branch: `ci.yml`, `scorecard.yml`,
`trivy.yml`, `ci-retry.yml`, `auto-close-issues.yml`, `auto-close-prs.yml` and
`auto-redirect-discussions.yml`. `ci.yml` triggers on `v*.*.*`, which our tags
match, and expects signing and registry credentials this fork does not have. The
three `auto-*` workflows hardcode `allowedAuthors = ['mdecimus']` and would close
every issue, pull request and discussion opened on this fork. `test.yml` is kept:
it is `workflow_dispatch` only and is useful for exercising the patch.

`Dockerfile.itsh` changes at 0.16: the `stalwart-cli` build steps are gone (the
CLI moved to `stalwartlabs/cli`), `resources/docker/entrypoint.sh` no longer
exists so the server binary is the entrypoint directly, and `WORKDIR`/`VOLUME`
on `/opt/stalwart` are dropped since the data directory is supplied by the
deployment. `git` plus `CARGO_NET_GIT_FETCH_WITH_CLI`, `CARGO_NET_RETRY` and
`AWS_LC_SYS_PREBUILT_NASM` are added, matching what upstream's own CI sets for
the `[patch.crates-io]` git pins introduced in this release.

## Contributing upstream

This patch is not offered upstream. Upstream `CONTRIBUTING.md` requires vouched
contributor status and a signed licensing agreement, so the fork is maintained
here instead.
