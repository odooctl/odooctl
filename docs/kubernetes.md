# Kubernetes runtime

The Kubernetes runtime uses the same deploy, clone, backup, restore, log, and
Odoo-command service paths as Docker Compose. Set `runtime.type` to
`kubernetes`; existing projects that omit `type` continue to use Compose.

## Production configuration

```yaml
runtime:
  type: kubernetes
  context: production
  namespace_template: "{project}-{environment}"
  manifests_path: .odooctl/rendered/kubernetes
  replicas: 2
  image_pull_policy: IfNotPresent
  postgres_mode: external
  secret_refs:
    PGPASSWORD:
      name: odoo-database
      key: password

postgres:
  host: postgres-rw.database.svc.cluster.local
  user: odoo
  password_env: ODOO_DB_PASSWORD
```

`postgres_mode: external` is the production default. The runner still needs
the configured local password environment variable for direct backup, restore,
and verification connections. The Odoo pod receives credentials only through
the named Kubernetes Secret key. odooctl never copies the local secret value
into argv or generated YAML and does not create Secret resources.

Each environment receives its own namespace by default. Namespace, Deployment,
Service, Ingress, PersistentVolumeClaim, and pod template carry:

- `app.kubernetes.io/managed-by: odooctl`
- `odooctl.dev/project`
- `odooctl.dev/environment`
- `app.kubernetes.io/component`

Before apply, restart, exec, logs, or deletion, odooctl reads existing resource
labels and fails closed if any identity differs. RBAC for the runner should
limit it to the configured namespace prefix and the resource kinds above.

## CloudNativePG

CloudNativePG is supported as an externally managed database integration:

1. Install and operate the CloudNativePG operator independently of odooctl.
2. Create a `Cluster` in a database namespace with its own backup and recovery
   policy.
3. Set `runtime.postgres_mode: cloudnativepg`.
4. Point `postgres.host` and `odoo.db_host` at the cluster's read/write Service,
   such as `erp-rw.database.svc.cluster.local`.
5. Reference the operator-managed application Secret from
   `runtime.secret_refs`.

odooctl deliberately does not apply, delete, or take ownership of the
CloudNativePG `Cluster`. Portable backups, restore drills, and PITR metadata
remain explicit odooctl operations; operator-native backups are an additional
database recovery layer.

## Generated resources and lifecycle

The runtime writes the canonical multi-document manifest to:

```text
.odooctl/rendered/kubernetes/<environment>/resources.yaml
```

`deploy` applies that file and waits for Deployment rollout status. `logs`,
module updates, native neutralization, and filestore streaming use
`kubectl logs` or `kubectl exec`. `status` reports owned Deployment
availability and identifies externally managed PostgreSQL separately.
