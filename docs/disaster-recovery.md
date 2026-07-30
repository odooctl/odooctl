# Disaster Recovery

odooctl provides four layers of backup/restore UX for production safety:

1. **Backup verification** — confirm a backup's integrity after creation.
2. **Restore-point browser** — list and audit all local backups with checksum integrity.
3. **DR drills** — automated drill that restores a backup into a throwaway database, healthchecks it, and cleans up.
4. **Provider snapshots** — coarse VM/volume recovery points kept separate
   from portable Odoo backups.

## Provider snapshots

Provider snapshots supplement database + filestore backups. They are
infrastructure recovery points, not portable Odoo backups and not an hourly
backup policy. Their manifests live under
`.odooctl/snapshots/`; portable backup manifests remain under
`.odooctl/backups/`.

Snapshot manifests are durable state machines: `requested`, `pending`,
`complete`, or `failed`. odooctl writes the requested intent before calling a
cloud mutation, then records provider IDs as soon as they are observed.
Interrupted and long-running requests can therefore be resumed with
`odooctl dr snapshot reconcile SNAPSHOT_ID`.

One provider configuration is deliberately bound to one
`snapshots.environment` and one infrastructure source. The environment is an
authorization and locking boundary, not a caller-supplied label: create,
reconcile, and restore refuse any other environment even if the caller has
access to it.

The default is an explicit no-provider mode:

```yaml
snapshots:
  provider: none
  pre_deploy: disabled
```

### AWS EBS multi-volume snapshots

AWS mode uses the official `aws ec2 create-snapshots` operation, which creates
a crash-consistent set across the EBS volumes attached to one EC2 instance:

```yaml
snapshots:
  provider: aws_ebs
  environment: production
  pre_deploy: required
  aws_ebs:
    instance_id: i-0123456789abcdef0
    region: eu-central-1
    recovery_availability_zone: eu-central-1a
    profile: production       # optional; normal AWS credential chain is used
    include_root_volume: true
    completion_timeout_seconds: 600
    poll_interval_seconds: 15
```

Before creation, odooctl records the source Availability Zone, root/device
mapping, volume type, provisioned IOPS and throughput, encryption/KMS identity,
and AWS account/region. Status checks are always owner-scoped. AWS snapshot
sets are labelled `crash_consistent`: odooctl does not claim PostgreSQL
application consistency because it does not stop or quiesce the instance.

Install the AWS CLI on the host that runs the CLI/runner and configure the
selected profile or normal AWS credential chain. The principal needs access to
the configured instance, volumes, snapshots, and recovery Availability Zone.
The commands used require, at minimum, permissions corresponding to
`sts:GetCallerIdentity`, `ec2:DescribeInstances`, `ec2:DescribeVolumes`,
`ec2:DescribeSnapshots`, `ec2:CreateSnapshots`, `ec2:CreateVolume`, and
snapshot/volume tagging (`ec2:CreateTags`). Encrypted snapshots also require
the applicable KMS key policy and KMS permissions for volume creation. Scope
the policy to the configured account, Region, source resources, recovery
resources, and odooctl tags where AWS supports that restriction.

If the configured wait expires while AWS still reports `pending`, creation
returns a durable pending manifest instead of falsely declaring failure.
`dr snapshot reconcile` refreshes that manifest later. Recovery recreates the
source volume type/performance in `recovery_availability_zone`, uses
deterministic per-volume client tokens and tags for safe retries, and leaves
every new volume unattached. It never replaces or attaches over live volumes.
The legacy input key `availability_zone` is accepted, but new configuration
should use `recovery_availability_zone`. See the
[AWS `create-snapshots` reference](https://docs.aws.amazon.com/cli/latest/reference/ec2/create-snapshots.html).

### Hetzner Cloud server snapshots

Hetzner mode uses `hcloud server create-image --type snapshot`:

```yaml
snapshots:
  provider: hetzner_cloud
  environment: production
  pre_deploy: preferred
  hetzner_cloud:
    server: odoo-production
    context: production       # optional hcloud context
    token_env: HCLOUD_TOKEN
    recovery_server_type: cx23
    recovery_location: nbg1
    recovery_network: odoo-recovery
```

`token_env` names the source variable; odooctl passes its value to hcloud as
`HCLOUD_TOKEN`, never on argv or in a manifest. When `context` is configured,
that hcloud context may supply credentials without a token environment
variable. A context name is only a local credential selector: it may be renamed
or replaced by token credentials for recovery, provided those credentials
still address the same Hetzner project and recorded provider resources. Before
creating an image, odooctl describes the server and refuses
the operation if it has attached Hetzner Volumes:
server images cover the root disk only, so accepting that topology would
produce an incomplete Odoo recovery point.

Install the `hcloud` CLI on the host that runs the CLI/runner. The selected
context or API token must belong to the source server's Hetzner project and
have Read & Write access: odooctl reads servers, images, and networks, creates a
snapshot image, and creates a recovery server. Create
`recovery_network` in that project before recovery and restrict it as an
isolated private network for inspection hosts and recovery systems.

Hetzner does not guarantee consistency for a running-server snapshot. odooctl
therefore records `live_unverified` unless the source was observed as powered
off, in which case it records `powered_off_consistent`. Images still in
`creating` remain pending and cannot be restored until reconciliation observes
`available`.

Recovery uses `hcloud server create` to materialize a separate stopped server
from the image. It attaches the server to `recovery_network`, assigns no public
IPv4 or IPv6, and verifies the image, powered-off state, and private-network
attachment after creation. The source server is never rebuilt. See the official
[`create-image` command](https://github.com/hetznercloud/cli/blob/main/docs/reference/manual/hcloud_server_create-image.md)
and [`server create` options](https://github.com/hetznercloud/cli/blob/main/docs/reference/manual/hcloud_server_create.md).

### Create, list, and recover

```sh
# Create an explicit provider snapshot
odooctl dr snapshot create production

# Read odooctl's separate snapshot index
odooctl dr snapshot list
odooctl dr snapshot list --environment production --json

# Refresh a requested/pending snapshot without creating another one
odooctl dr snapshot reconcile production-20260730T120000Z-deadbeef

# Safe default: print the recovery commands without changing provider state
odooctl dr snapshot restore production-20260730T120000Z-deadbeef

# Execute only after typing both exact identities
odooctl dr snapshot restore production-20260730T120000Z-deadbeef --execute
```

Planning is local and side-effect free: it reads the complete manifest and
configured recovery destination, but does not invoke AWS/hcloud or require
provider credentials. This lets an operator review the exact commands and
identities before arranging privileged recovery access. Create, reconcile, and
`--execute` do invoke the provider CLI and require the prerequisites above.

There is deliberately no `--yes` bypass. Interactive execution prompts for
the exact odooctl snapshot ID and exact source resource ID. Automation must
provide both explicitly:

```sh
odooctl dr snapshot restore "$SNAPSHOT_ID" \
  --execute \
  --confirm-snapshot "$SNAPSHOT_ID" \
  --confirm-resource "$SOURCE_RESOURCE_ID"
```

Only a `complete` manifest whose individual resources are all complete or
available can enter the restore path. A provider timeout is not automatically a
failure:

- Snapshot creation may complete as `pending`; run `dr snapshot reconcile`
  until it becomes `complete` or `failed`.
- If creation raises after the provider may have accepted the request, odooctl
  preserves the last `requested`/`pending` manifest and its discovery marker.
  Reconcile that snapshot ID before starting another create. AWS
  `create-snapshots` and Hetzner `create-image` do not provide a client token,
  so blindly retrying can create duplicate billable artifacts.
- Recovery execution may also return `pending` after recording newly created
  volume/server IDs. Re-running the same typed execution is retry-safe:
  deterministic AWS client tokens/tags and Hetzner labels discover the existing
  recovery resources instead of intentionally creating a second set. Always
  inspect the recorded provider IDs before retrying or cleaning up.

Explicit CLI/API create, reconcile, plan, and execute actions each receive an
operation record, events, and an audit entry. An automatic pre-deploy snapshot
is part of the enclosing `deploy` operation rather than a second
`snapshot_create` operation; its manifest ID/status are recorded in deployment
metadata. Restore metadata is written after each provider mutation, so a later
failure does not hide already-created resources.

> **Cost and retention:** EBS snapshots, replacement EBS volumes, Hetzner
> snapshot images, and stopped recovery servers can all remain billable.
> odooctl does not currently prune or delete provider snapshots or recovery
> resources, and portable-backup retention settings do not apply to them.
> Configure provider lifecycle policy where appropriate and remove artifacts
> manually only after checking their manifest/restore IDs.

The local API exposes the same plan-first flow and exact identities; see
[Snapshot operations through the API](api.md#snapshot-operations).

### Protected pre-deploy policy

For the protected environment named by `snapshots.environment`,
`pre_deploy: preferred` or `required` runs the provider snapshot after the
portable database + filestore backup succeeds and before any rollout:

- `required`: a provider failure or still-pending snapshot stops the deploy
  before code or database mutation.
- `preferred`: a failure or pending status is recorded in deployment metadata
  and the deploy continues.
- `disabled`: no automatic snapshot; explicit snapshot commands still work.

Configuration validation requires the bound environment to be protected when
automatic snapshots are enabled. A single provider block never snapshots a
different protected environment: that environment still gets its normal
portable pre-deploy backup, but needs a separate project/provider binding for
provider-native snapshots.

The portable backup is always retained as the primary application-level
rollback artifact.

## Backup verification

Run a backup and immediately verify its checksums:

```sh
odooctl backup production --verify
```

The `--verify` flag calls `validate_backup_dir` against the just-created backup and emits a `backup verified` operation event if checksums match. Use this as a post-backup sanity check.

You can also verify any existing backup by backup ID:

```sh
# Python API
from odooctl.services.backup import verify_backup
result = verify_backup(backups_root, "production_2026-05-31_100000")
print(result.ok, result.error)
```

## Restore-point browser

List all local restore points with integrity status:

```sh
# CLI (via the web UI or API)
GET /projects/{project}/restore-points
GET /projects/{project}/restore-points?environment=staging
```

Each restore point reports:

| Field | Description |
|-------|-------------|
| `backup_id` | Unique backup identifier |
| `environment` | Source environment |
| `timestamp` | Creation timestamp |
| `integrity` | `ok` / `failed` / `unknown` |

Integrity is verified by re-checking SHA-256 checksums against the stored manifest. A `failed` status means files are corrupt or missing.

The **Restore Points** tab in the web UI (`odooctl serve`) shows this list for each environment.

## Restore production backup to staging

Restore a production backup into staging without touching production:

```sh
odooctl restore production --to staging
odooctl restore production --to staging --backup production_2026-05-31_100000
```

Safety rules:
- The **target** environment must not be protected (production is always protected).
- The source backup is validated (checksums) but the environment mismatch check is intentionally skipped so cross-environment restores work.
- The DB dump is restored into a temporary `{target_db}{temp_db_suffix}` database first, then swapped into the target DB name before healthcheck. The target filestore is restored as part of the staging flow.
- A healthcheck is run against the target after restore.
- **Sanitization is mandatory when the source environment is protected (production).** The target environment must have `sanitize: true`; if it does not, the restore is refused before any DB work begins. Sanitization runs on the temp DB before the atomic swap — production PII, credentials, and live integrations are scrubbed before the data is promoted into the target.

To enable cross-env restore from production, set `sanitize: true` on the target environment in `odooctl.yml`:

```yaml
environments:
  staging:
    branch: staging
    domain: staging.example.com
    db_name: odoo_staging
    filestore_path: /var/lib/odoo/filestore/odoo_staging
    sanitize: true   # required for production→staging restore
```

## DR drills

A DR drill restores the latest backup into a **throwaway database**, runs a healthcheck, then drops the throwaway DB. The live database is never touched.

```sh
odooctl dr drill production
```

Steps:
1. Resolve the latest backup for the source environment.
2. Validate backup checksums.
3. Restore the DB dump into `{source_db}_dr_drill` (a throwaway DB name).
4. Run a healthcheck.
5. Drop the throwaway DB (always, even on failure).
6. Report `success` or `failed`.

Protected environments (production by default) cannot be passed as drill *targets* — the drill reads from their backups but never writes to the live DB. Since the throwaway DB is always dropped, drills are safe to run repeatedly.

### Python API (injectable fakes for testing)

```python
from odooctl.services.dr import run_dr_drill

result = run_dr_drill(
    environment="production",
    backups_root=project.backups_dir,
    db_adapter=my_db_adapter,
    fs_adapter=my_fs_adapter,
    healthcheck_fn=lambda url: True,
    is_protected_fn=config.is_protected,
)
print(result.status, result.backup_id)
```

All dependencies are injectable, making unit tests use fakes without touching real databases.

## Web UI

The **Restore Points** tab in `odooctl serve` shows restore points with integrity badges for each environment. Admin users see a **DR Drill** button that enqueues a drill operation via the API.

## Encrypted off-site backup metadata

When remote backups use S3, configure server-side encryption metadata on the remote backup block:

```yaml
backups:
  remote:
    type: s3
    bucket: demo-odoo-backups
    encryption_algorithm: aws:kms   # or AES256 for S3-managed keys
    encryption_key_env: ODOO_BACKUP_KMS_KEY_ID
```

`odooctl` records only non-secret manifest metadata:

```json
"encryption": {
  "algorithm": "aws:kms",
  "key_ref": "env:ODOO_BACKUP_KMS_KEY_ID"
}
```

For real S3 uploads, the adapter passes the matching `ServerSideEncryption` and optional `SSEKMSKeyId` `ExtraArgs` to `boto3.upload_file()`. The key ID is read from the named environment variable and is not written to the manifest or logs. If boto3 is unavailable and the adapter falls back to the local mirror used by tests/offline runs, the mirror is a local copy and should not be treated as an encrypted off-site destination.

## Safety invariants

- Production is never used as a restore *target* (enforced in `restore_to_env`).
- Cross-env restore from a protected source (production) is refused if the target environment has `sanitize: false` — the refusal happens before any DB or filesystem work.
- When source is protected, the temp DB is sanitized (mail servers, crons, payment providers, API keys, webhooks) before the atomic swap promotes it into the target DB name.
- Restore-to-staging restores the DB into a temporary incoming DB, sanitizes it, and swaps before target healthcheck.
- DR drill throwaway DB is always dropped in a `finally` block.
- Backup checksums are verified before any restore or drill.
- Remote S3 encryption metadata is recorded in the backup manifest and real boto3 uploads request S3 server-side encryption; no key material is stored.
