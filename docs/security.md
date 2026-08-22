# Security Policy

`odooctl` operates production Odoo databases, filestores, and Docker
infrastructure. We take security reports seriously and ask reporters to follow
the coordinated disclosure process below.

## Supported versions

`odooctl` is pre-1.0. Security fixes are issued against the latest released
version on the `master` branch only.

| Version | Supported            |
| ------- | -------------------- |
| 0.2.x   | :white_check_mark:   |
| < 0.2   | :x:                  |

## Reporting a vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Report privately via GitHub's
[private vulnerability reporting](https://github.com/odooctl/odooctl/security/advisories/new)
on the `odooctl/odooctl` repository. This is the coordinated channel — it keeps
the report confidential until a fix is released and lets us credit you in the
advisory.

Please include:

- A description of the issue and the impact you observed.
- Steps to reproduce or a minimal proof of concept.
- The `odooctl` version, Python version, host OS, and execution mode
  (`host` or `docker`) you used.
- Any relevant logs, redacted of real secrets.

We aim to acknowledge new reports within five business days and to provide an
initial assessment within ten business days. Coordinated disclosure timelines
are agreed with the reporter once the issue has been triaged.

## Scope

In scope:

- Issues in the `odooctl` CLI, adapters, sanitization SQL, backup/restore
  pipeline, schedule generation, project/env registry, and the optional S3
  adapter.
- Issues in shipped documentation that would cause an operator to expose
  secrets or run destructive commands against production.

Out of scope:

- Vulnerabilities in third-party software (Odoo itself, PostgreSQL, Docker,
  the host OS). Report those upstream.
- Issues that require an attacker to already have shell or Docker socket
  access on the host running `odooctl`.

## Secret handling

`odooctl` is designed so that secrets never live in the repository or in
checked-in configuration files:

- Secrets are referenced via environment variables and `*_env` config fields
  (for example `password_env: ODOO_DB_PASSWORD`).
- Logs redact environment values whose variable names look secret-bearing
  (`PASSWORD`, `SECRET`, `TOKEN`, `KEY`, `PASSWD`). The redaction policy and
  ignored-value list are configurable; see `docs/security.md`.
- `odooctl doctor` warns when referenced secrets are shorter than the
  configured minimum or fall on the redaction ignore list.

If you report a vulnerability that involves a captured log or trace, please
redact any real production secrets before sending it.
