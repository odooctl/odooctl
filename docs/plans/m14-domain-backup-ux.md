# M14 — Domain/SSL and Backup/Restore UX

## Goal

Give operators a product-grade domain/TLS and backup/restore experience.

## Locked reverse proxy support

V1 supports **Traefik only**, but implementation must go through an explicit reverse proxy abstraction.

Required shape:

- `ReverseProxyAdapter` protocol/interface.
- `TraefikAdapter` implementation.
- Domain/SSL services call the interface, not Traefik directly.
- Nginx/Caddy are future adapters and must not be implemented in v1.

## Domain/SSL scope

- Attach domain to environment.
- Verify DNS points to host.
- Configure reverse proxy route.
- Use one certificate lifecycle path.
- Prefer Traefik ACME; support DNS-01 for wildcard domains.
- Never stop the global proxy for a single cert operation.

## Backup/restore UX scope

- Restore-point browser.
- Verify backup integrity.
- Restore production backup to staging safely.
- Run DR drills.
- Optional encrypted off-site backups.

## Files to create

- `odooctl/domains/__init__.py`
- `odooctl/domains/base.py`
- `odooctl/domains/traefik.py`
- `odooctl/services/domain.py`
- `odooctl/services/restore_points.py`
- `odooctl/services/dr.py`
- `odooctl/commands/domain.py`
- `odooctl/commands/dr.py`
- `docs/domains-ssl.md`
- `docs/disaster-recovery.md`

## Commands

- `odooctl domain attach <env> <domain>`
- `odooctl domain verify <env>`
- `odooctl domain detach <env> <domain>`
- `odooctl backup --verify <env>`
- `odooctl restore --to staging --backup latest`
- `odooctl dr drill production`

## Acceptance criteria

- Domain verify reports DNS/cert/proxy status through the reverse proxy abstraction.
- Restore-to-staging uses temp DB swap and never touches production.
- DR drill restores latest backup into throwaway DB, healthchecks, then cleans up.
- Encrypted remote backup manifests record encryption metadata.
- UI exposes restore points and drill status.
