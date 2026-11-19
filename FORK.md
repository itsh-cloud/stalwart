# ITSH fork of Stalwart

This fork tracks upstream [stalwartlabs/stalwart](https://github.com/stalwartlabs/stalwart)
and carries one behavioural patch plus build changes. Everything else is
upstream.

Base: **v1.1.1**. Branch: **`itsh/v1.1.1`**.

Previous bases: v0.16.27 on `itsh/v0.16.27`, v0.16.26 on `itsh/v0.16.26`,
v0.16.25 on `itsh/v0.16.25`, v0.16.19 on `itsh/v0.16.19` and v0.15.5 on
`itsh/v0.15.5`, kept for the rollback images.

1.0.0 changed the database layout, so a 0.16 store does not open on this build.
Upstream's `stalwart-migrate` copies a 0.16.27 store into a new one. Use its
`-oss` build, since this image is built without `enterprise`.

## The authenticated recipient failure patch

`crates/smtp/src/inbound/rcpt.rs` limits failed recipients of an authenticated
session per login instead of counting them towards the IP ban.

Upstream counts every failed `RCPT TO` against the remote IP under the abuse
ban rate and blocks the IP once it is exceeded, authenticated or not. When many
authenticated clients share one egress IP, behind NAT or a cluster's nodes, one
client sending to non-existent local recipients gets the shared IP banned and
cuts off every other client behind it.

An authenticated session now counts its failures against its login
(`Server::is_rcpt_fail_limited` in `crates/common/src/network/security.rs`): the
abuse ban rate's count, over a window of one hour instead of the abuse ban
period. A login over that rate is refused with `451 4.3.0` and disconnected,
and every later `RCPT TO` from that login is refused the same way before any
lookup until the window ends. No IP is blocked on this path.

A user's JMAP submissions run as local SMTP sessions and count against the same
login. Each session carries its origin (`SessionOrigin` in
`crates/smtp/src/inbound/login_limit.rs`, which holds the fork's session-side
code): a network client, a user's JMAP submission, or a message the server
sends on its own, such as an iMIP reply or an alarm, which never counts.

The counter is keyed by a hash of the login under its own prefix,
`KV_RATE_LIMIT_RCPT_AUTH` (200). The unauthenticated counters' keys derive from
recipients the client chooses, so a shared prefix would let an unauthenticated
sender add to a login's count.

Upstream's `is_rate_allowed` has no read-only mode at 1.x, so the check before
the lookup increments the counter by zero instead.

### Scope and cost

- Unauthenticated sessions keep the IP and per-recipient ban unchanged.
- Network clients from an allowlisted IP are exempt, as they are from the IP
  ban. JMAP submissions connect from loopback, which is always allowlisted, so
  their exemption follows their origin instead.
- The limit belongs to the account: signing in by an alias or in another case
  counts against the same login.
- The per-session `maxFailures` limit (`MtaStageRcpt`) applies to both paths
  unchanged.
- Resetting the rate limiters
  (`crates/services/src/task_manager/tasks/maintenance.rs`) clears the per-login
  counters too.

### Residual behaviour

There is no IP-level backstop on this path. An attacker holding K logins behind
one IP gets K times the rate in failed lookups per window.

The one-hour window is a trade. Anyone who can sign in as the login, such as a
misconfigured client holding one of its app passwords, can stop it from sending
for up to an hour. In return a login can probe more addresses per day than the
abuse ban rate allows an IP: with the default one-day period, 24 windows of the
same count.

Both checks fail open on a rate-store error, as upstream's IP check does.

### Accepting the change

Unit tests next to the counter cover the limit, a login reconnecting over it,
a second login, a check that does not count, recipients that spell the login or
its key, the reset by prefix and the one-hour window; those next to
`SessionOrigin` cover which sessions count:

```sh
cargo test -p common --lib network::security::rcpt_auth_tests
cargo test -p smtp --lib inbound::login_limit
```

`tests/itsh/rcpt_fail_limit.py` checks the session end to end: the
unauthenticated ban, the authenticated refusal without a ban, the refusal of a
reconnecting login, an unaffected second login on the same IP, an allowlisted
IP whose failures do not count afterwards, an account signing in by its alias
and in upper case, and failed JMAP submissions refusing the login over JMAP and
SMTP. It fails on stock 1.1.1, where the first authenticated login over the
rate gets the shared IP banned.

## Patches upstream absorbed

These were carried on 0.16 and are not on this branch.

**Header search.** On 0.16 a `HEADER` search unioned the value's tokens, so
Message-IDs sharing a domain all matched each other, and draft clients that
relocate a draft by Message-ID found every revision. 1.x indexes headers
differently and intersects the tokens (`crates/store/src/search/query.rs:613` at
1.1.1). `tests/itsh/header_search.py` passes on stock 1.1.1.

Header search is still opt-in. `Search.indexEmailFields` must include `headers`,
or no header terms are written and every `HEADER` search returns nothing.

**S3 transport retries.** Upstream retries transport errors, such as timeouts,
up to `maxRetries` since 1.1.1 (`crates/s3/src/retry.rs:82`), for writes as well
as reads.

**OAuth sign-in with app passwords and API keys.** Upstream refuses both at
interactive sign-in and at device-code approval since 1.0.2
(`crates/http/src/auth/oauth/auth.rs:720` at 1.1.1, the same
`is_secondary_credential` check the 0.16 patch added).
`tests/itsh/oauth_scoped_credentials.py` passes on stock 1.1.1 for both. An API
key is a bearer credential that the password sign-in never accepts, so its
refusal there does not reach that check.

## Licensing and the build

The tree is dual licensed. Most files are `AGPL-3.0-only OR LicenseRef-SEL`, and
a small set is `LicenseRef-SEL` only. At v1.1.1 there are 66 such files under
`crates/`. 33 sit behind a cargo feature this build does not enable, all of
them behind `enterprise`. The other 33 are the `scim` and `scim-proto` crates,
which upstream depends on unconditionally. Every use of `scim` is behind
`enterprise`, so this fork makes it an optional dependency of `http` and `main`
that only `enterprise` turns on, and neither crate is compiled here.

Dual-licensed files in the compiled crates also hold 2067 SEL-only snippets
between `SPDX-SnippetBegin` and `SPDX-SnippetEnd`. All but two are already
excluded from this build: `DOMAIN_FLAG_SCIM_PROVISIONING` and
`DomainCache::allows_scim_provisioning` in `crates/common/src/auth/mod.rs`.
Every reader of the flag is behind `enterprise`, so this fork gates those two as
well, with the attribute above the `SnippetBegin` marker.

`Dockerfile.itsh` therefore builds **without** the `enterprise` feature. The
published image contains no SEL-licensed code and is AGPL-3.0-only, which is what
makes it redistributable. `.github/scripts/check_sel_gating.py` runs before every
image build and fails if upstream ever adds an SEL-licensed file or snippet that
is reachable in this build.

The check asks `cargo tree` which workspace crates this build compiles and with
which features, fails unless the ones left out are exactly `scim` and
`scim-proto`, and skips their files. It fails outright if `enterprise` or
`dev_mode` is on in any compiled crate, which a dependency edge can do without
touching `Dockerfile.itsh`. It then resolves each SEL-only file's real parent
module chain and accepts a gate only when its `cfg` is false for the features
of the crate that holds the file, since features are per crate. A snippet needs
such a gate on every item or statement at its own indentation, either above it
or leading its line, and a gate may span lines; a snippet that opens by
continuing a statement from outside it counts as ungated. Gating on a feature
the crate has on excludes nothing. A line naming `SPDX-SnippetBegin` or
`LicenseRef-SEL` in any form other than the file headers and snippet markers
upstream uses fails the check, so a marker cannot pass unseen.

The feature set is otherwise identical to upstream's image:
`sqlite postgres mysql rocks s3 redis keep azure nats`. The runtime stage
mirrors the musl stage of upstream's `Dockerfile.build`, so the image is a
drop-in for `stalwartlabs/stalwart:<version>-alpine`: it runs as the
`stalwart` user (2000), reads `/etc/stalwart/config.json`, keeps its data in
`/var/lib/stalwart` and has upstream's health check. The 0.16 images ran as root
with no default arguments; the 1.x layout follows upstream instead, because
deployments and the migration are written against upstream's image. The gated
features are inert without a licence key in any case.

Upstream's `Dockerfile` and `Dockerfile.build` are left untouched so they do not
conflict on rebase. `Dockerfile.itsh` drops the sccache and FoundationDB
machinery from `Dockerfile.build`, neither of which is used here. Its toolchain
follows `Dockerfile.build` at v1.1.1 with every version pinned: the Rust image
upstream leaves at `rust:slim-trixie` is pinned to the release that tag pointed
to when 1.1.1 shipped, Zig is checked against the digests published for it,
and cargo-chef and cargo-zigbuild are installed from crates.io with `--locked`.
Upstream builds cargo-zigbuild from a git commit, which release 0.24.1 contains.
Both base images are pinned by digest as well as tag, the dependency build and
the server build run with `--locked`, and `itsh-image.yml` pins every action to
a commit SHA.

The two git dependencies in `[patch.crates-io]`, hickory-dns and rust-rocksdb,
are mirrored at the locked SHAs in `itsh-cloud/hickory-dns` and
`itsh-cloud/rust-rocksdb`. If upstream's repositories become unavailable, point
those entries at the mirrors with `rev = "<sha>"`.

Publishing this fork also satisfies AGPL-3.0 section 13, which requires offering
the corresponding source of a modified version that users interact with over a
network.

Patched files are `AGPL-3.0-only OR LicenseRef-SEL`; the modifications here are
taken under AGPL-3.0-only.

## Acceptance scripts

Upstream removed its test suites from this repository in 1.0.0, including the
`tests` crate the 0.16 patches were tested in. `tests/itsh/` holds
scripts that check the patch and the absorbed fixes against a running server;
its `README.md` shows how to run them.

## Versioning

Tags are `v<upstream-version>-itsh.<n>`, for example `v1.1.1-itsh.1`. The
counter restarts at 1 for each new upstream base. Pushing such a tag builds and
publishes `ghcr.io/itsh-cloud/stalwart:<upstream-version>-itsh.<n>`, along with
a `sha-<commit>` tag.

`1.1.1-itsh.1` is a valid semantic version whose prerelease segment sorts our
builds correctly among themselves. It sorts below a bare `1.1.1`, which is
harmless because this image repository only ever holds builds from this fork.

## Rebasing onto a new upstream release

Rebase onto each upstream patch release within two weeks of its release, and
within three days when it fixes a security issue.

```sh
git fetch upstream --tags
git switch -c itsh/vX.Y.Z vX.Y.Z
git cherry-pick <every patch commit from the previous itsh branch>
```

Then run `python3 .github/scripts/check_sel_gating.py`, the unit tests above and
the scripts in `tests/itsh/`, and tag `vX.Y.Z-itsh.1`.

Two build changes are carried as code: `scim` as an optional dependency in
`crates/http/Cargo.toml` and `crates/main/Cargo.toml`, and the two `enterprise`
gates on the SCIM flag snippets. Drop either once upstream gates the same code
itself. The SEL check fails the image build if a new base needs more of the
same.

The recipient failure patch touches `handle_rcpt_to` and `rcpt_error` in
`crates/smtp/src/inbound/rcpt.rs`, adds `crates/smtp/src/inbound/login_limit.rs`
and an `origin` field to `SessionData` in `crates/smtp/src/core/mod.rs`, marks
the JMAP submission session in `crates/jmap/src/submission/set.rs`, adds
`is_rcpt_fail_limited` next to `is_rcpt_fail2banned` with its own
`KV_RATE_LIMIT_RCPT_AUTH` prefix in `crates/common/src/lib.rs`, and adds that
prefix to the rate limiter reset in
`crates/services/src/task_manager/tasks/maintenance.rs`. After rebasing, check
that `rcpt_error` is still the only caller of `is_rcpt_fail2banned`, that every
caller of `SessionData::local` other than the JMAP submission sends on the
server's own behalf, that upstream has not taken prefix 200, and that the reset
still lists every `KV_RATE_LIMIT_*` prefix it should. For each new locked SHA of
the two git dependencies, push a branch to its mirror.

When upstream changes `Dockerfile.build`, carry its toolchain versions into
`Dockerfile.itsh` and keep them pinned: a fixed Rust image tag, Zig digests from
`https://ziglang.org/download/index.json`, and released versions of cargo-chef
and cargo-zigbuild.

Upstream workflows removed on this branch: `ci.yml`, `scorecard.yml`,
`trivy.yml`, `ci-retry.yml`, `auto-close-issues.yml`, `auto-close-prs.yml` and
`auto-redirect-discussions.yml`. `ci.yml` triggers on `v*.*.*`, which our tags
match, and expects signing and registry credentials this fork does not have. The
three `auto-*` workflows would close every issue and pull request opened on this
fork and point every discussion at upstream's support portal. Upstream removed
`test.yml` in 1.0.0 together with the tests.

## Contributing upstream

The recipient failure patch changes upstream's abuse policy for authenticated
sessions rather than fixing a defect, so it is not offered upstream as is.
Upstream `CONTRIBUTING.md` requires vouched contributor status and a signed
licensing agreement in any case, so the fork is maintained here instead.
