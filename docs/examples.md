# odooctl command workflows

This page collects common MVP workflows for self-hosted Odoo teams.

## 1) Start from a config template

```bash
odooctl init --dry-run
```

Then save the generated YAML to `odooctl.yml` and fill in environment-specific values.

## 2) Validate your project config

```bash
odooctl validate --config odooctl.yml
```

Validation checks required project fields, environment definitions, backup settings, and secret references.

## 3) Inspect project state

```bash
odooctl status --config odooctl.yml
odooctl status --config odooctl.yml --environment production --json
```

Use `status` to review the current git commit, image reference, compose service state, backup metadata, and deployment result.

## 4) Back up production before changes

```bash
odooctl backup production --config odooctl.yml
```

The backup manifest captures the database dump, filestore archive, git commit, image reference, and timestamp.

## 5) Clone production to staging and sanitize it

```bash
odooctl clone production staging --sanitize --config odooctl.yml
```

This is the key MVP workflow: dump production, restore into staging, copy the filestore, sanitize unsafe data, update modules, restart staging, and print the staging URL.

## 6) Update modules directly

```bash
odooctl update-modules staging --modules sale,stock,custom_module --config odooctl.yml
```

The command streams the Odoo update run and exits non-zero if module updates fail.

## 7) Deployment and rollback

```bash
odooctl deploy staging --branch staging --config odooctl.yml
odooctl rollback production --mode code --config odooctl.yml
odooctl rollback production --mode full --backup production_2026-05-24_1600 --config odooctl.yml
```

Production deploys back up data first, while full rollback restores the backed-up database and filestore.

## Additional platform examples

- `examples/kubernetes/odooctl.yml` — production Kubernetes, GitOps previews,
  and progressive rollout policies.
- `examples/gitops/` — protected GitHub Actions publishing and an Argo CD
  ApplicationSet.
- `examples/k3d/` — Dockerfile, addons directory, and configuration for the
  owned k3d + Tilt production simulation.
