# Integration testing against real Odoo

The unit suite uses fakes for Docker, PostgreSQL, and Odoo. The
integration harness in `tests/integration/` complements it by exercising the
full operator lifecycle against disposable, real Odoo stacks built from the
official images.

## Running

```bash
# Default: Odoo 19 only
pytest -m integration tests/integration

# Full version matrix (serial — one stack at a time)
ODOOCTL_IT_VERSIONS=17.0,18.0,19.0 pytest -m integration tests/integration
```

Requirements: Docker with the compose plugin, ~4 GB free RAM, and the
`odoo:<version>` / `postgres:16-alpine` images (pulled automatically on first
run). A full three-version matrix takes roughly 10–20 minutes on a 4-core
host.

## What it covers

Per version: `validate`, `doctor --json`, `status --json`,
`backup --verify` (manifest + checksums asserted on disk),
`clone production staging` (sanitization asserted via SQL: crons and mail
servers disabled, a rotated non-empty `database.secret`, and the
CSRF-protected staging login form loads with normal cookies/redirects),
`restore production --to staging`
(temp-DB + swap path), and **API/runner parity**: an operation enqueued
through the authenticated local API is executed by `odooctl runner --once`,
which must exit 0 and mark the operation `succeeded` (regression for the
2026-05-31 audit finding C2, where the runner exited 0 on failed operations).

## Isolation guarantees

- Each stack lives in a pytest temp directory whose (unique) name becomes the
  docker compose project name, so containers, volumes, and networks are all
  namespaced — the harness cannot see or touch any other compose project on
  the host. A dedicated test asserts this.
- Teardown always runs `docker compose down -v --remove-orphans` for that
  project only.
- The global odooctl registry is redirected via `XDG_CONFIG_HOME` into the
  temp directory, so `project add` in tests never touches the operator's real
  registry.

## Adding a new Odoo version

1. Add the version to `ODOOCTL_IT_VERSIONS` and run the matrix.
2. Review sanitization coverage against the new version's credential surfaces
   (the Odoo 19 example: the new `auth_passkey` module stores WebAuthn
   credentials that a naive clone would carry into staging — sanitization now
   deletes them).
3. Update `docs/odoo-versions.md` with findings before claiming support.
4. Record the exact odooctl package version and Git commit in
   `docs/operations/integration-status.md`.
