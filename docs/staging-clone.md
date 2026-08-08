# Staging Clone

`odooctl clone production staging --sanitize` probes the configured Odoo
runtime for its native `neutralize` command before changing data, restores
production into a temporary database, runs native neutralization plus
odooctl's extension checks, copies the filestore, swaps the verified database,
updates configured modules, restarts Odoo, and checks the configured health
path (default `/web/health`).

Sanitization is enabled by default because staging must not send real emails, call live payment APIs, or trigger production webhooks.

## Native neutralization policy

```yaml
sanitization:
  native_neutralize: preferred  # required | preferred | disabled
```

- `required` refuses the clone when the Odoo image does not expose the native
  command.
- `preferred` uses the native command when available and falls back to the
  odooctl extension pass only when the command is explicitly unsupported.
- `disabled` runs only the odooctl extension pass.

Connection, configuration, and execution errors are fatal under both
`required` and `preferred`; they are never mistaken for an unsupported
command. Odoo runs inside the configured Compose service so its installed
modules and their `neutralize.sql` files participate. Database credentials are
supplied through `PGPASSWORD`, never process arguments.

The result is written under `.odooctl/sanitizations/` after the verified
temporary database is promoted. It records the policy, whether native
neutralization executed, the extension profile, and the completed verification
checks without recording credentials.

## Built-in sanitization coverage

All built-in statements are guarded (`to_regclass` / `information_schema.columns`) so they no-op on Odoo versions where a table or column does not exist.

Every profile (`minimal`, `normal`, `strict`) applies the mandatory baseline:

- Disables real outgoing mail servers (`ir_mail_server`) while keeping exactly
  one active `invalid` neutralization sink. The sink prevents Odoo from falling
  back to SMTP values passed on its command line. Incoming mail
  (`fetchmail_server`) is disabled.
- Disables all crons (`ir_cron`) — including under `minimal`; a cloned production database with live crons can send mail and charge cards.
- Disables payment providers in both the modern `payment_provider` table (Odoo 16+) and the legacy `payment_acquirer` table (pre-16).
- Cancels pending `queue_job` records and disables `base_automation` rules.
- Purges the unsent mail queue (`mail_mail`).
- Scrubs webhook/callback/endpoint URLs and `api_key`/`secret`/`token`/`password` system parameters.
- Rewrites `web.base.url` to the staging domain and sets `web.base.url.freeze = True` (inserting the parameter if missing) so Odoo cannot rewrite the URL back to production on the next admin login.
- Sets and verifies Odoo's standard `database.is_neutralized` flag so the
  database displays the familiar neutralized-state warning.

`normal` (the default) and `strict` additionally scrub credential material carried over from production:

- Disables OAuth providers (`auth_oauth_provider`: `enabled`/`active` set to false) and clears `client_secret` where the column exists.
- Clears IAP/SMS tokens (`iap_account.account_token`).
- Deletes WebAuthn passkeys (`auth_passkey_key`, Odoo 19 `auth_passkey` module) so production passkeys cannot unlock staging; no-ops on older versions.

`strict` also blanks every `auth_%` system parameter.

Before the database swap, odooctl verifies the SMTP sink, incoming mail,
scheduled actions, payment providers, base URL/freeze state, and the standard
neutralized flag. Any failed check leaves the previous target database in
place.
