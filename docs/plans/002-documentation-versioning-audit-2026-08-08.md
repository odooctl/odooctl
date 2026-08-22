# Documentation audit and version-preservation plan

Date: 2026-08-08
Status: COMPLETE
Branch: docs/versioned-documentation

Audit date: 2026-08-08

Versions currently supported as published documentation targets:

- Stable: `0.2.0` (`v0.2.0`)
- Beta: `0.3.0b1` (`v0.3.0b1`, user-facing 0.3.0 beta line)

## Executive finding

The repository has broad feature documentation, but the published site is not
versioned. The docs workflow always builds the `master` branch into the same
`site/docs` destination. Consequently, publishing beta or later development
documentation can silently replace the documentation users of stable `0.2.0`
need. The existing Git tags preserve source history, but there is no immutable
documentation URL, stable/beta selector, release-channel banner, or CI check
that a release's docs match its artifact.

The documentation must be versioned before `0.3.0` becomes stable. Until then,
the site should expose `0.2.0` and `0.3.0b1` independently and identify the
second as prerelease documentation.

## Required versioned-documentation design

1. Publish immutable snapshots from release tags, not from the current branch:
   - `/docs/0.2.0/` built from `v0.2.0`
   - `/docs/0.3.0b1/` built from `v0.3.0b1`
2. Publish channel aliases:
   - `/docs/stable/` -> `0.2.0`
   - `/docs/beta/` -> `0.3.0b1`
   - `/docs/dev/` -> current `master`, clearly marked unreleased
3. Add a visible version selector and a banner showing exact package version
   and channel on every page. Beta pages must warn that commands and config may
   change before `0.3.0` final.
4. Keep prior immutable version directories during deployment. The current
   workflow publishes one replacement tree and has no preservation contract.
5. Generate a machine-readable version manifest (for example
   `/docs/versions.json`) containing version, channel, Git tag/commit, publish
   date, and canonical URL.
6. Build release docs from the same checked-out tag and in the same release
   workflow that validates/builds the wheel. A tag/package-version mismatch
   must fail both artifact and documentation publication.
7. Add link checking and strict MkDocs builds for every retained supported
   version. Do not rebuild `0.2.0` from `master`; fixes to old documentation
   require an explicit `0.2.x` documentation patch or a clearly recorded docs
   backport.
8. Define a retention policy. At minimum, retain every supported stable minor,
   every advertised beta, and an end-of-support tombstone page after a version
   leaves support.

`mike` can implement versions and aliases, but the existing external Pages
repository and root landing page require deliberate integration. An equivalent
tag-to-directory publisher is acceptable if it provides immutable directories,
atomic alias updates, and preservation tests.

## Installation and release-channel documentation gaps

- `README.md` and `docs/getting-started.md` still say PyPI publication is
  coming soon, while `0.2.0` and `0.3.0b1` are published and installable.
- `docs/installation.md` documents only an unpinned stable install and upgrade.
  Add tested commands for:
  - installing stable `0.2.0` exactly;
  - installing prerelease `0.3.0b1` explicitly;
  - upgrading an existing `0.2.0` pipx/uv installation to the beta;
  - returning from beta to stable;
  - checking the installed version and release channel.
- Explain that ordinary package upgrades should remain on stable unless the
  operator explicitly opts into a prerelease. Never make a beta the implicit
  upgrade path for stable installations.
- The release workflow recognizes prereleases only by the presence of `b` in
  the tag. Document the accepted version/tag format and consider deriving the
  prerelease flag with a PEP 440 parser so alpha and release-candidate tags are
  also handled correctly.
- Add a release checklist item requiring installation commands to be tested
  against the artifact produced by the tag.

## Accuracy gaps found in current content

### Staging clone and neutralization

The `0.3.0b1` sanitizer clears every `ir_config_parameter` key containing
`secret`, which also clears Odoo's required `database.secret`. On Odoo 17, 18,
and 19 this makes the staging login page fail with HTTP 500:

```text
ValueError: CSRF protection requires a configured database secret
```

The implementation fix rotates `database.secret` after credential scrubbing
and verifies a non-empty replacement before database promotion. Update:

- `docs/staging-clone.md` to distinguish external credentials from Odoo's
  functional CSRF/session secret and state that each clone receives a newly
  rotated value;
- `docs/security.md` to explain why the production value is not retained and
  why an empty value is invalid;
- `CHANGELOG.md` under `Unreleased` with the cross-version staging-login fix;
- integration tests and `docs/operations/integration-testing.md` so the real
  matrix loads the staging login form after neutralization, rather than only
  checking `/web/health` and SQL safety predicates;
- `docs/operations/integration-status.md` with the repaired 2026-08-08 run and
  exact package version tested.

### Supported Odoo versions

- `README.md` claims Odoo 17, 18, and 19 are integration-tested, but
  `docs/odoo-versions.md` describes only an Odoo 19 production baseline and
  says other recent images are merely “intended” to work.
- Replace that ambiguity with a support matrix keyed by both odooctl release
  and Odoo major. Each cell should state tested image digest/tag, PostgreSQL
  major, tested operations, date, and known limitations.
- Record `0.2.0` results separately from `0.3.0b1`; a successful beta test must
  not retroactively expand stable-version guarantees.
- `docs/operations/integration-status.md` currently records only the
  2026-07-19 run and does not identify the odooctl package version or commit.
- Add the staging UI/CSRF check to the support gate for every Odoo version.

### Health-check behavior

`docs/odoo-versions.md` says 2xx and 3xx responses are healthy, then later says
redirects are unhealthy. Current code and the `0.2.0` changelog require 2xx and
reject redirects. Remove the 2xx/3xx claim and consistently document
`/web/health` as the default machine health endpoint. Browser login-page checks
must be a separate integration assertion with cookies and redirect handling.

### Web UI and remote access

- The CLI exposes `odooctl serve --host`, but `TrustedHostMiddleware` accepts
  only `127.0.0.1` and `localhost`; therefore using `--host 0.0.0.0` alone
  produces `Invalid host header`. Either remove the misleading non-loopback
  capability or add an explicit, narrow allowed-host option intended for a
  TLS/authenticating reverse proxy.
- Keep localhost as the secure default. Document SSH tunneling and a supported
  reverse-proxy/Tailscale deployment with TLS, allowed hosts, firewall scope,
  bearer-token handling, and persistent service setup.
- Add troubleshooting entries for `Invalid host header`, missing/expired UI
  tokens (`401`), and the difference between the dashboard port and Odoo
  environment ports.
- The live beta acceptance lab required a runtime allowlist workaround; that
  workaround must not become the undocumented production procedure.

### General drift

- `README.md` says “700+ unit tests”; the current suite contains substantially
  more. Prefer a CI-generated badge/result or wording that does not become
  stale after normal test growth.
- `docs/gitops.md` hard-codes an `odooctl:0.2.0` initializer image. This is
  correct only in the `0.2.0` snapshot. Beta docs should use `0.3.0b1` (or an
  explicit beta placeholder), and development docs should explain pinning
  rather than silently showing an old release.
- Add an exact `odooctl` version/commit column to operational test records,
  DR drill reports, and examples whose behavior changed between releases.
- Add a documentation matrix test that executes or at least parses every
  documented CLI command against the matching release's `--help` output.

## Acceptance criteria

- A `0.2.0` user can open an immutable stable URL containing only commands and
  configuration available in `0.2.0`.
- A beta user can open an immutable `0.3.0b1` URL with a prerelease banner and
  correct beta installation/upgrade instructions.
- Publishing `master` or a new beta cannot remove or alter the `0.2.0` tree.
- The version selector clearly distinguishes stable, beta, and development.
- CI builds and link-checks every supported version and confirms the displayed
  version matches the source tag/package metadata.
- Odoo 17, 18, and 19 staging login forms are included in the real integration
  acceptance gate after sanitization.
- Documentation explicitly states that staging rotates a usable
  `database.secret`, while external integration secrets remain scrubbed.
