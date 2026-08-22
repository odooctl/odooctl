# Odoo version notes

`odooctl` is version-aware where Odoo CLI behavior or Docker images differ, but it intentionally keeps configuration explicit instead of guessing too much from an image tag.

## Support matrix

Only cells recorded below are integration-tested support claims. A successful
beta run never retroactively changes the guarantees for an earlier stable
release. The matrix uses the official image tags and `postgres:16-alpine` in a
disposable Docker Compose stack (`odoo` and `db` services).

| odooctl release | Odoo major / image | PostgreSQL | Tested operations | Date | Known limitations |
| --- | --- | --- | --- | --- | --- |
| `0.2.0` (`1b482535f060054d98efc258ce4cc61384a465e4`) | 17.0 / `odoo:17.0` | 16 | validate, doctor, status, backup verify, sanitized clone, cross-env restore, API/runner parity | 2026-07-19 | The gate did not render the staging login form; the released sanitizer can clear `database.secret`. |
| `0.2.0` (`1b482535f060054d98efc258ce4cc61384a465e4`) | 18.0 / `odoo:18.0` | 16 | validate, doctor, status, backup verify, sanitized clone, cross-env restore, API/runner parity | 2026-07-19 | The gate did not render the staging login form; the released sanitizer can clear `database.secret`. |
| `0.2.0` (`1b482535f060054d98efc258ce4cc61384a465e4`) | 19.0 / `odoo:19.0` | 16 | validate, doctor, status, backup verify, sanitized clone, cross-env restore, API/runner parity | 2026-07-19 | The gate did not render the staging login form; the released sanitizer can clear `database.secret`. |
| `0.3.0b1` (`ea14df128ad4377f52ded427156e0b7383408f5e`) | 17.0 / `odoo:17.0` | 16 | then-current lifecycle; no login-form assertion | 2026-08-08 | Beta; the released sanitizer can clear `database.secret` and break staging login. |
| `0.3.0b1` (`ea14df128ad4377f52ded427156e0b7383408f5e`) | 18.0 / `odoo:18.0` | 16 | then-current lifecycle; no login-form assertion | 2026-08-08 | Beta; the released sanitizer can clear `database.secret` and break staging login. |
| `0.3.0b1` (`ea14df128ad4377f52ded427156e0b7383408f5e`) | 19.0 / `odoo:19.0` | 16 | then-current lifecycle; no login-form assertion | 2026-08-08 | Beta; the released sanitizer can clear `database.secret` and break staging login. |

The current unreleased support gate verifies `/web/health` as the machine
endpoint, then separately loads the cookie/redirect-aware staging login form
and checks its CSRF token after sanitization. That assertion and the
`database.secret` rotation fix are not part of the tagged releases above.

## Config fields to review per version

### `odoo.image`

Set the image tag you actually deploy:

```yaml
odoo:
  image: odoo:19.0
```

Verify tag availability before using a new major version in production.

### `odoo.without_demo`

Odoo 19 warns when older examples use `--without-demo=all` and treats it as true. Prefer:

```yaml
odoo:
  without_demo: "True"
```

Older Odoo deployments that still require `all` can override this value explicitly.

### Module updates

Docker module updates are invoked with explicit DB connection flags:

```text
odoo -d <db> -u <modules> --stop-after-init --db_host=<host> --db_user=<user> --db_password=<password>
```

This avoids relying on official-image entrypoint environment handling, which is not always applied to direct `docker compose exec odoo odoo ...` calls.

### Health checks

`odooctl` requires a 2xx response. Redirects are unhealthy because they may
hide an error page or route to the wrong database. Use `/web/health` (the
default on Odoo 15+) as the machine health endpoint.

For local or shared-stack multi-database setups, use:

```yaml
environments:
  staging:
    db_selector: true
```

This makes health checks include `?db=<db_name>`.

## Upgrade checklist

When moving a project to a new Odoo major version:

1. Pull and start the target image in a staging stack.
2. Run `odooctl doctor` with the new config.
3. Run `odooctl backup production` in the old known-good stack.
4. Restore or clone into a staging database on the new image.
5. Run `odooctl update-modules staging --modules <your modules>`.
6. Confirm `/web/health` and any custom health route return HTTP 200 (redirects count as unhealthy).
7. Review Odoo logs for deprecated CLI flags or addon migration warnings.

## Integration coverage

Browser login checks are not health checks: they use cookies and normal redirect
handling only as an integration assertion that Odoo can render the login form.
The checked-in integration harness is the reference fixture for service names,
Docker-native DB access, filestore named volumes, and local multi-DB
`db_selector` health checks.
