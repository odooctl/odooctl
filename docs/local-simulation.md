# k3d + Tilt local production simulation

The local simulation creates one disposable k3d cluster whose name combines
the configured project name with a hash of the absolute project root. Two
checkouts of the same project therefore cannot collide.

## Configuration

```yaml
runtime:
  type: kubernetes
  namespace_template: "{project}-{environment}"

local_simulation:
  enabled: true
  environment: development
  output_path: .odooctl/local
  cluster_prefix: odooctl
  k3s_image: rancher/k3s:v1.31.5-k3s1
  http_port: 18069
  postgres_port: 15432
  postgres_image: postgres:16
  build_context: .
  dockerfile: Dockerfile
  live_update_paths: [addons]
  rollout_timeout_seconds: 90
```

Pin the k3s, PostgreSQL, base Odoo, and project image versions to keep the
simulation reproducible.

## Render and run

```console
$ odooctl local render
.odooctl/local/odooctl-acme-1a2b3c4d

$ odooctl local up
cluster: odooctl-acme-1a2b3c4d
tilt: tilt up -f .../Tiltfile
```

Rendering reuses the canonical Namespace, filestore PVC, Odoo Deployment,
Service, and Ingress used by the production Kubernetes runtime. A disposable
PostgreSQL Deployment and Service are added for local simulation. PostgreSQL
uses trust authentication only inside this disposable project namespace, so no
fake password or Secret value is written into YAML.

The generated Tiltfile:

- builds the configured Odoo image from the project Dockerfile;
- live-syncs configured addon paths;
- groups Odoo, PostgreSQL, and ingress resources;
- streams workload logs in the Tilt UI;
- exposes Odoo and PostgreSQL through local port forwards.

## Lifecycle smoke

After `local up`, run:

```console
$ odooctl local smoke
```

The smoke workflow builds and imports the Odoo image, applies the canonical
resources, waits for PostgreSQL and Odoo readiness, initializes an Odoo
database, executes native Odoo neutralization, captures a custom-format
PostgreSQL backup, restores and verifies it in a new database, starts an
intentionally invalid rolling update, and verifies native rollout undo.

The real integration test is opt-in because it pulls container images:

```console
$ ODOOCTL_RUN_K3D=1 uv run pytest -m integration \
    tests/integration/test_k3d_lifecycle.py -q
```

## Ownership-safe teardown

```console
$ odooctl local down --yes
```

An `ownership.json` record is written only after k3d reports successful cluster
creation. Teardown validates its schema, exact cluster name, project,
environment, and absolute project root before calling `k3d cluster delete`
with that literal name. It refuses a missing, symlinked, malformed, or
mismatched record and never performs label-wide or prefix-wide deletion.

See `examples/k3d/` for a ready-to-run fixture.
