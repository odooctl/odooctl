# Runner architecture — web/API vs. privileged runner

This document describes the privilege split that the security model
(`docs/rbac.md`) depends on. The split is enforced structurally by
`odooctl/security/runner_contract.py` and checked in CI/tests.

## V1 security boundary

V1 is a single-host Docker Compose deployment. Two trust zones run on that one
host:

```
                 RBAC + capability token
  ┌────────────┐  ───────────────────────▶  ┌─────────────────────────┐
  │  web/API   │      enqueue operation       │   privileged runner     │
  │  process   │ ◀───────────────────────     │   (Docker socket, etc.) │
  └────────────┘      events / state          └─────────────────────────┘
   no Docker socket                              Docker / Postgres / git / tar
```

- The **web/API process never mounts the Docker socket** and never runs Docker,
  Postgres, git, or tar work.
- The **privileged runner** performs all of that, on the same host, behind the
  durable operation queue.

Remote runners and hosted multi-tenant worker pools are explicit future work.

## What each side may do

| web/API may | runner may |
| --- | --- |
| read state | access Docker / Compose |
| enqueue operations | run Postgres commands |
| stream operation events | manage filestore archives |
| read the audit trail (per RBAC) | run git operations |

These capability lists are encoded as `API_ALLOWED_CAPABILITIES` and
`RUNNER_ALLOWED_CAPABILITIES` in `runner_contract.py`.

## How the contract is enforced

The privileged adapters live under:

- `odooctl.adapters.*` — `docker_compose`, `postgres`, `db`, `filestore`, `s3`,
  `reverse_proxy`
- `odooctl.odoo.*` — `db_swap`, `module_update`, `sanitize`, `healthcheck`

The future API/web packages (`odooctl.api`, `odooctl.web`) **must not import any
of these directly**. Instead they go through the service/operation layer, which
enqueues work the runner executes.

`runner_contract.py` parses the source of each API/web package (via `ast`,
without importing it) and reports any direct absolute or relative import of a
privileged module. This static check is a guardrail, not a sandbox; dynamic
imports with computed names remain out of scope, and the process boundary is the
real privilege separation:

```python
from odooctl.security.runner_contract import (
    assert_api_does_not_import_privileged,
    find_violations,
)

assert_api_does_not_import_privileged()   # raises RunnerContractViolation if dirty
```

The API and web packages (`odooctl/api/`, `odooctl/web/`) exist and are
scanned; `find_violations()` returns an empty list only while they stay clean.
The moment a module in either package gains a forbidden import, the test in
`tests/test_security.py` fails. Run the check manually with:

```console
$ odooctl security runner-check
```

## Operation kinds the runner executes

The API enqueue endpoint accepts every operation kind in its whitelist
(`odooctl/api/routes_operations.py`), but the runner's dispatcher
(`odooctl/runner/worker.py::_dispatch`) only executes a subset. The remaining
kinds are CLI-only by design — they involve interactive confirmation and
host-level judgment — and the runner marks them `failed` at dispatch time
(`Unsupported operation kind in runner`):

| Operation kind      | Runner-supported | How to run it otherwise        |
|---------------------|------------------|--------------------------------|
| `backup`            | yes              | —                              |
| `clone`             | yes              | —                              |
| `dr_drill`          | yes              | —                              |
| `snapshot_create`   | yes              | —                              |
| `snapshot_reconcile`| yes              | —                              |
| `snapshot_restore`  | yes              | —                              |
| `pitr_base_create`  | yes              | —                              |
| `pitr_reconcile`    | yes              | —                              |
| `pitr_restore`      | yes              | —                              |
| `pitr_cutover`      | yes              | —                              |
| `filestore_migrate` | yes              | —                              |
| `migrate_rehearsal` | yes              | —                              |
| `restore`           | no (CLI-only)    | `odooctl restore`              |
| `deploy`            | no (CLI-only)    | `odooctl deploy`               |
| `promote`           | no (CLI-only)    | `odooctl promote`              |
| `env_create`        | no (CLI-only)    | `odooctl env create`           |
| `env_destroy`       | no (CLI-only)    | `odooctl env destroy`          |
| `update_modules`    | no (CLI-only)    | `odooctl update-modules`       |
| `rollback`          | no (CLI-only)    | `odooctl rollback`             |

## Capability tokens across the boundary

Because the API cannot act directly, it authorizes the runner per operation
with a signed [capability token](rbac.md#capability-tokens). The token binds the
work to a single action, environment, and project, with a short TTL. A leaked
token cannot be used against a different target or after expiry, and the
runner records consumed token nonces (`consumed_nonces.json`) and rejects
repeats, so a token cannot be replayed for the same scope either.

Signing-key note: the runner verifies capability tokens with the key from
`ODOOCTL_API_KEY` — the same key the API server uses to mint them, and the
default `--key-env` of `odooctl security token mint`, so hand-minted tokens
verify without extra flags (see `docs/rbac.md`).

## Why a stdlib-only crypto core

The secret store and capability tokens use only Python's standard library
(`hmac`, `hashlib`, `secrets`, `base64`, `json`, `time`). For a single-host v1
this avoids adding a `cryptography` dependency while still providing
authenticated encryption (encrypt-then-MAC) for secrets at rest and HMAC-signed,
scope-limited, expiring tokens for runner authorization. If a future milestone
introduces remote runners or at-rest requirements that need AES-GCM or
asymmetric signatures, the `secrets`/`tokens` modules are the contained seams to
upgrade.
