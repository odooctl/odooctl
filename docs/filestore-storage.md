# Object-storage filestores

`odooctl filestore` inventories, copies, verifies, and explicitly cuts over an
Odoo filestore without silently removing the source. It supports four
deployment shapes:

- a local host path or Docker named volume (the existing default);
- a verified S3-compatible object mirror;
- a POSIX-mounted object-store filesystem;
- an operator-selected Odoo object-storage module backed by the same neutral
  S3 contract.

Install the S3 extra for `object_mirror` or `odoo_module`:

```bash
pipx install 'odooctl[s3]'
# or
uv tool install 'odooctl[s3]'
```

## Backend contract

Existing configurations remain valid. Without `filestore_backend`, odooctl
infers `docker_volume` when `filestore_volume` is set and `local` otherwise.
An explicit local backend is:

```yaml
environments:
  production:
    db_name: odoo_prod
    filestore_path: /srv/odoo/filestore/odoo_prod
    filestore_backend:
      type: local
```

For a Docker named volume:

```yaml
environments:
  production:
    db_name: odoo_prod
    filestore_path: /var/lib/odoo/filestore/odoo_prod
    filestore_volume: odoo-data
    filestore_backend:
      type: docker_volume
```

`filestore_path` or `filestore_volume` remains the migration source. An
optional `source_path` can name a different local source during a migration.
Source and target locations are bound into the plan; changing either requires a
new plan. Keep those values unchanged through optional source cleanup, then
update the steady-state configuration after the migration is complete. The
target/integration mode is configured as follows.

### S3-compatible object mirror

```yaml
environments:
  production:
    db_name: odoo_prod
    filestore_path: /srv/odoo/filestore/odoo_prod
    filestore_backend:
      type: object_mirror
      object_store:
        bucket: acme-odoo-filestore
        prefix: acme
        region: eu-central-1
        endpoint_env: ODOO_FILESTORE_S3_ENDPOINT
        access_key_env: ODOO_FILESTORE_S3_ACCESS_KEY
        secret_key_env: ODOO_FILESTORE_S3_SECRET_KEY
        session_token_env: ODOO_FILESTORE_S3_SESSION_TOKEN
        encryption_algorithm: aws:kms
        encryption_key_env: ODOO_FILESTORE_KMS_KEY_ID
```

The endpoint, credentials, session token, and KMS key are environment-variable
references, never secret values in YAML. Omit the explicit credentials to use
the normal AWS credential chain. `endpoint_env` makes the same contract work
with S3-compatible providers.

Objects are content-addressed by SHA-256 below a
project-and-environment-scoped prefix. Each migration publishes an immutable
manifest only after every object is uploaded and read back for checksum
verification. Cutover updates `active.json` with an ETag compare-and-swap, so a
concurrent publisher cannot silently replace a generation planned against a
different active version.

### POSIX-mounted object storage

Use this for an object-storage gateway that already exposes normal POSIX file
semantics:

```yaml
environments:
  production:
    db_name: odoo_prod
    filestore_path: /srv/odoo/filestore/odoo_prod
    filestore_backend:
      type: posix_object_mount
      mount_path: /mnt/object-store/odoo_prod
```

Sync writes a private partial directory, verifies every copied byte, fsyncs the
result, and atomically renames it into `mount_path`. It refuses to overwrite a
different existing target. Odoo or the container mount must be changed to use
that verified path as part of the operator's deployment cutover; odooctl does
not rewrite Compose files or mount tables.

### Odoo module integration

`odoo_module` keeps the object format provider-neutral and records which module
the operator chose:

```yaml
environments:
  production:
    db_name: odoo_prod
    filestore_path: /srv/odoo/filestore/odoo_prod
    filestore_backend:
      type: odoo_module
      module_name: my_object_storage_connector
      object_store:
        bucket: acme-odoo-filestore
        prefix: acme
        region: eu-central-1
```

odooctl uploads, downloads, verifies, and publishes an odooctl active
generation. That marker is control-plane metadata, not an assumed module API.
odooctl does not install the module, assume proprietary models or RPC methods,
map the neutral object layout into a module-specific schema, or write module
credentials. Installing/configuring the named module and performing any
module-specific import are explicit operator steps.

## Migration workflow

First inspect the effective backend:

```bash
odooctl filestore status production
```

Then create and execute a migration:

```bash
# Immutable source inventory; prints the migration id.
odooctl filestore migrate plan production

# Copy/upload exactly that inventory. The source is retained.
odooctl filestore migrate sync production \
  --migration production_filestore_0123456789abcdef01234567

# Read and checksum every target file/object.
odooctl filestore migrate verify production \
  --migration production_filestore_0123456789abcdef01234567

# Record/publish the verified generation as active. The source is still kept.
odooctl filestore migrate cutover production \
  --migration production_filestore_0123456789abcdef01234567 \
  --confirm-environment production \
  --confirm-source-retained
```

Planning fails on symbolic links, special files, unsafe paths, and files that
change while being hashed. Sync refuses a source that differs from its plan.
Cutover re-verifies the target and the unchanged source. Every mutating command
uses the normal environment lock, operation event stream, RBAC action, and
audit trail.

Download a verified generation into a new directory:

```bash
odooctl filestore migrate download production \
  --migration production_filestore_0123456789abcdef01234567 \
  --destination ./recovered-filestore
```

The destination and partial path must not already exist.

## Deletion controls

Cutover never deletes the source. Source cleanup is a separate command and
requires all three acknowledgements:

```bash
odooctl filestore migrate delete-source production \
  --migration production_filestore_0123456789abcdef01234567 \
  --confirm-environment production \
  --confirm-migration production_filestore_0123456789abcdef01234567 \
  --delete-source
```

Before deletion, odooctl again verifies the active target and confirms that the
source still matches the planned inventory. It refuses deletion before
cutover, with mismatched confirmations, or when a POSIX source equals the
target. `object_mirror` is intentionally ineligible for source deletion: it is
a retained copy, not an Odoo serving backend. For `posix_object_mount` and
`odoo_module`, switch and validate the actual Odoo deployment first, while
keeping the odooctl source/target configuration used by the migration unchanged
through this cleanup command. A durable deletion-start checkpoint makes an
interrupted cleanup retryable; a surviving source must still match the original
inventory before a retry removes it.

Remote content objects are deliberately not garbage-collected by this
workflow because multiple generations and paths may share one digest. You may
delete only an inactive migration marker with exact confirmation:

```bash
odooctl filestore migrate delete-remote-marker production \
  --migration production_filestore_0123456789abcdef01234567 \
  --confirm-migration production_filestore_0123456789abcdef01234567
```

The active marker cannot be deleted through this command, and shared content
objects remain available.

## Consistency boundary

A filestore migration verifies storage bytes; it does not create a
transactionally consistent database/filestore snapshot while Odoo is writing
attachments. Quiesce Odoo or otherwise stop attachment writes before the final
plan/sync/verify/cutover sequence. Portable backups remain the recovery
primitive that binds a PostgreSQL dump and matching filestore under one backup
identity.
