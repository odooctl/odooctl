# Documentation versions

The documentation site publishes immutable snapshots alongside channel aliases:

| Channel | Alias | Current snapshot |
| --- | --- | --- |
| Stable | `/docs/` and `/docs/stable/` | [`0.2.0`](/docs/0.2.0/) |
| Beta | `/docs/beta/` | [`0.3.0b1`](/docs/0.3.0b1/) |
| Development | `/docs/dev/` | current `master` checkout |

Use the version picker on every documentation page to change channel. A beta
page is intentionally marked as prerelease: its commands and configuration may
change before the final release. Development documentation is unreleased and
may not match a package on PyPI. The root `/docs/` URL remains a complete copy
of the stable snapshot so existing bookmarks continue to resolve.

## Retention policy

We retain every supported stable minor and every advertised prerelease. An
immutable tree is built from its Git tag, never from `master`. When support
ends, its URL remains and serves an end-of-support tombstone directing operators
to supported documentation. Fixing an old snapshot requires a documented patch
release/backport; it is not silently rewritten during a newer publish.

The machine-readable [versions manifest](/docs/versions.json) records each
version, release channel, Git ref and commit, publication date, canonical URL,
and current aliases. Release docs are built from the same checked-out tag that
is used to validate and package the wheel.
