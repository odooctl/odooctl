# k3d + Tilt production simulation

Prerequisites: Docker, k3d, kubectl, Tilt, and ports 18069/15432.

```console
odooctl local render --config odooctl.yml
odooctl local up --config odooctl.yml
tilt up -f .odooctl/local/<generated-cluster-name>/Tiltfile
odooctl local smoke --config odooctl.yml
odooctl local down --config odooctl.yml --yes
```

`local smoke` builds and imports the configured Odoo image, applies the same
canonical Namespace/PVC/Deployment/Service/Ingress resources used in
production plus a disposable PostgreSQL overlay, executes native
neutralization, captures and restores a PostgreSQL backup into an isolated
database, deliberately starts a bad rolling update, and verifies native undo.

The cluster name includes a hash of the absolute project root. Teardown requires
the matching `ownership.json` written only after successful cluster creation
and deletes that one exact k3d cluster.
