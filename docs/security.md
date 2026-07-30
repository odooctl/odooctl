# Security Notes

- Do not commit passwords, API tokens, SMTP credentials, payment credentials, OAuth secrets, webhook secrets, S3 keys, or Odoo admin passwords.
- Use environment variables and `*_env` references.
- Logs redact sensitive environment values whose variable names look secret-bearing (`PASSWORD`, `SECRET`, `TOKEN`, `KEY`, `PASSWD`).
- Redaction intentionally skips short/common values from `redaction.ignore_values` such as `odoo`; otherwise logs become unreadable in local Odoo stacks. Use strong, unique production secrets.
- Run `odooctl doctor` after exporting env vars. It warns when referenced secrets are shorter than `redaction.min_secret_length` or ignored by the redaction policy.
- Protect local backup directories with host filesystem permissions.
- Install `odooctl[s3]`, configure real S3 credentials, and choose
  `backups.remote.policy` explicitly for off-host backup copies. Remote
  failures never change the configured destination.
- Never clone production into staging without sanitization unless you fully understand the risk.
- Staging sanitization disables mail servers, fetchmail, crons, payment providers, queue jobs, and pending outbound mail by default.

## Trust model

The audits of 2026-05-31 asked one load-bearing question: *who is trusted to
write `odooctl.yml`?* The answer defines the severity of every config-driven
finding, so it is stated here explicitly.

### `odooctl.yml` is operator-trusted

- Anyone who can write the project config (or the files it references, such as
  the compose file) is considered an **operator** of that project. The config
  is not a security boundary: an actor who controls it already controls what
  `docker compose` runs and can therefore execute arbitrary commands with the
  runner's privileges by design.
- Consequently, protect `odooctl.yml` like you protect the compose file:
  repository write access, host filesystem permissions, and code review are
  the controls. `odooctl` will never mitigate a hostile config author.
- Defense in depth still applies: config values that flow into subprocess
  arguments, container paths, volume names, or reverse-proxy rules are
  validated at load time (charset/length/hostname rules), and no code path
  builds `sh -c` command strings from config values. These measures limit the
  blast radius of *mistakes* (typos, malformed generated configs, copy-paste)
  — they are not a sandbox for malicious operators.

### Boundaries that ARE enforced

- **Web/API tier vs runner**: the API process never mounts the Docker socket
  and never imports privileged modules (enforced by `security/runner_contract`).
  API clients act under RBAC roles; destructive operations require capability
  tokens minted with the runner key, verified with single-use nonces.
  Capability tokens default to a 300-second TTL; consumed nonces are retained
  for 2 hours (2 × the maximum token TTL) and then purged, so replay stays
  blocked for every token's validity window without unbounded growth.
- **API key strength**: `ODOOCTL_API_KEY` must be at least 32 characters;
  `odooctl serve` and `odooctl runner` refuse shorter keys at startup.
  Cancelling an operation is a write action (`cancel`, operator-or-higher) —
  viewer tokens cannot cancel — and `/operations/{id}` reads/cancel are
  restricted to the project named in the token's `proj` claim when it is not
  `"*"`.
- **Roles**: viewers cannot mutate; operators cannot bypass protected-environment
  floors; protected environments (`is_protected()`: the `production` name or
  any env with `tier: production`) require elevated confirmation paths.
- **Secrets**: referenced by env-var name in config, resolved only at execution
  time in the privileged process, redacted from logs, errors, and streamed
  operation events.

### Remote-backup boundary

- `required` makes upload, immediate byte verification, and retention failures
  fail backup creation after the validated local backup has been safely
  published. `best_effort` keeps local backup creation successful but records
  and warns about a degraded remote result. `disabled` creates no remote
  client. Explicit `backup-remote verify` always exits non-zero on a retention
  reconciliation alert, under either active policy, so monitors cannot miss
  cleanup failures.
- New objects are written below a deterministic project-scoped namespace, and
  completion manifests are validated against the configured project and
  environment before list/latest/download/delete operations. GFS retention
  re-checks ownership immediately before deletion. These checks prevent
  accidental cross-project deletion; S3 IAM and bucket policy remain the
  security boundary against a hostile bucket writer who could forge object
  contents.
- Local manifest filesystem time and remote completion-marker provider time
  enforce `retention.grace_hours` before deletion. Together with explicit
  protection of the caller's new ID, this prevents concurrent publishers from
  mutually deleting just-completed backups; a later pass applies the
  deterministic GFS result.
- A payload-only prefix is not a backup: the final manifest is the completion
  marker. `orphan_grace_hours` only delays cleanup. Automatic deletion also
  requires a valid identity-bound abandonment marker from the publisher and a
  second inventory check. An old prefix without that marker is preserved and
  raises a manual-review alert, because age is not proof that another host has
  stopped uploading.
- `verify_after_upload: true` reads and hashes the stored bytes before marking
  an upload complete. `backup-remote verify` performs the same byte-level
  verification on demand, and `backup-remote download` verifies before staging
  and atomically publishing a local directory.
- Encryption key values, S3 access keys, endpoint credentials, and generated
  drill database passwords stay out of argv and manifests. Only non-secret
  encryption metadata and environment-variable references are recorded.

### DR-drill boundary

- A drill restores unsanitized backup data only into a disposable PostgreSQL
  container on tmpfs and a dedicated filestore volume. An internal Docker
  network gives isolated Odoo access to that PostgreSQL peer but no external
  egress; PostgreSQL has no published port and Odoo exposes only an ephemeral
  loopback healthcheck port.
- The selected backup must belong to the configured project and requested
  environment. A newer foreign-project backup in a shared root is skipped,
  then the selected backup's identity and checksums are validated before any
  Docker mutation.
- The live Compose database/Odoo services, networks, and data volumes are
  never joined or used as restore targets. Cleanup attempts every drill
  container, volume, network, temporary config, and in-memory credential even
  after partial setup; an incomplete cleanup fails the drill.
- The isolated Odoo container deliberately does not inherit live Compose bind
  mounts. Custom addons must exist inside the configured image at
  `odoo.addons_paths`. Treat a missing-addon drill failure as an image
  packaging issue rather than attaching live mounts and weakening isolation.
- This isolates the drill from the application stack, not from the Docker
  host. Anyone who controls the Docker daemon or the operator-trusted config
  remains privileged under the trust model above.

### Audit-trail integrity (optional HMAC keying)

The audit trail (`.odooctl/audit.jsonl`) chains entries with a hash so
in-place tampering is detectable. By default the chain uses an **unkeyed**
SHA-256 — sufficient against accidental corruption and naive edits, but an
attacker with write access to the file can truncate the chain, alter entries,
and recompute the hashes.

Set the `ODOOCTL_AUDIT_KEY` environment variable (in the runner/CLI process
that writes audit entries) to switch the chain to
`HMAC-SHA256(key, prev_hash || entry)`. Without the key, forged or rehashed
chains fail verification (`odooctl.operations.audit.verify_chain`, which reads
the same env var or accepts an explicit `key=` argument). Notes:

- Unkeyed remains the default for backward compatibility; existing unkeyed
  chains keep verifying as long as `ODOOCTL_AUDIT_KEY` is unset.
- Enabling the key starts keying **new** entries only; verification of a chain
  written partly unkeyed and partly keyed will fail across the boundary, so
  rotate/archive `audit.jsonl` when enabling the key.
- Keep the key outside the state directory (host secret manager or service
  environment), or an attacker who can read the state dir can re-key the chain.

If your deployment needs a lower-trust config-authoring role (e.g. developers
may edit addon lists but not volumes or compose paths), put that policy in
your VCS review process — odooctl deliberately does not implement partial
config trust in v1.
