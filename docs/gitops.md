# GitOps and pull-request environments

GitOps mode renders the canonical Kubernetes resources without contacting a
cluster. Argo CD, Flux, or another reconciler can consume the output.

```yaml
gitops:
  enabled: true
  output_path: deploy/gitops
  preview_base_domain: preview.example.com
  preview_source_environment: staging
  preview_ttl_hours: 24
  initializer_image: registry.example.com/platform/odooctl:0.3.0b1
  preview_image_template: "registry.example.com/acme/odoo:pr-{revision}"
```

The initializer image must contain `odooctl`, the same Odoo addons used by the
target image, PostgreSQL client tools, and any configured native neutralization
support.

Pin this image to the odooctl release documented by the snapshot you are
reading. Development documentation intentionally does not imply that a moving
`latest` image exists or is compatible with a released configuration.

## Environment overlays

Render a declared environment:

```console
$ odooctl gitops render --env production
deploy/gitops/environments/production
```

The directory contains `resources.yaml` and `kustomization.yaml`. Rendering is
purely declarative: it does not invoke kubectl or apply resources.

## Pull-request overlays

```console
$ odooctl gitops preview --pr 123 --sha abcdef0123456789
deploy/gitops/previews/pr-123-abcdef01
```

The identity is deterministic from the PR number and first eight hexadecimal
revision characters. Each preview receives a separate namespace, domain,
database name, filestore PVC, and expiry annotation. The overlay also contains
a PostSync initialization Job that runs the normal fail-closed sanitized clone
path. Its generated ConfigMap contains environment-variable and Kubernetes
Secret references, never secret values.

`metadata.yaml` records the source, PR revision, created/expiry timestamps,
database, filestore, domain, sanitization requirement, and native
neutralization policy.

`preview_image_template` may use `{revision}` and `{pull_request}`. Publish the
corresponding immutable PR image before allowing Argo CD to sync the overlay.

## Expiry and cleanup

Planning is the default:

```console
$ odooctl gitops cleanup
expired: pr-123-abcdef01
```

Direct teardown is explicit:

```console
$ odooctl gitops cleanup --apply --yes
deleted: pr-123-abcdef01
```

Before deleting a namespace, odooctl verifies the same project, environment,
component, and managed-by labels used during creation. An absent namespace is
an idempotent no-op; a mismatched namespace fails closed.

In repository-driven Argo CD flows, removing an expired preview directory lets
the ApplicationSet prune the corresponding Application and owned resources.
The example workflow uses a short-lived GitHub App token and a protected
`preview` environment; it does not store a kubeconfig or cluster credential.

See `examples/gitops/` for GitHub Actions and Argo CD examples.
