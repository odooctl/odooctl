# Progressive deployment and rollback

Each environment selects a rollout strategy:

```yaml
runtime:
  type: kubernetes
  canary_provider: nginx

environments:
  production:
    rollout_strategy: blue_green
    auto_rollback: true
  staging:
    rollout_strategy: canary
    canary_percent: 10
```

## Capability matrix

| Runtime | `recreate` | `rolling` | `blue_green` | `canary` |
| --- | --- | --- | --- | --- |
| Docker Compose | yes | no | no | no |
| Kubernetes | yes | yes | yes | with `canary_provider: nginx` |

Unsupported combinations fail during configuration validation, before a
deployment mutates data or workloads.

`rolling` uses Kubernetes Deployment readiness and native `rollout undo`.
`blue_green` creates a revision-named candidate Deployment and Service, waits
for the Odoo HTTP readiness probe, runs module updates in the candidate,
switches the stable Service, performs the public health check, and only then
converges the canonical workload and removes the candidate.

`canary` adds a temporary NGINX canary Ingress at `canary_percent`, verifies
readiness and public health, then follows the same stable-Service promotion.
Candidate resources carry the normal ownership labels plus an immutable
revision label. Cleanup and rollback refuse resources with mismatched labels.

On rollout-status, candidate, or public-health failure, `auto_rollback: true`
restores the prior Service selector or runs native Deployment undo. Protected
environments also restore their pre-deploy portable backup when a database
mutation may have occurred. Deployment metadata records the selected strategy
and workload rollback result.

## Database migration limitation

Odoo module updates mutate a database shared by old and new application
workloads. Keeping the old workload available does not make an incompatible
schema migration zero-downtime, and application rollback alone cannot reverse
that schema. odooctl prints this warning for every progressive rollout with
module updates.

Use backward-compatible expand/migrate/contract changes across separate
deployments where uninterrupted service matters. For protected environments,
the pre-deploy database and filestore backup remains the authoritative rollback
boundary.
