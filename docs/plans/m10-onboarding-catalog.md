# M10 — Onboarding Catalog and Templates

## Goal

Turn the useful `community-sh` template idea into an Odoo ecosystem catalog for stacks, addons, and companion services.

## Files to create

- `odooctl/catalog/__init__.py`
- `odooctl/catalog/schema.py`
- `odooctl/catalog/registry.py`
- `odooctl/catalog/render.py`
- `odooctl/catalog/manifests/odoo-19-community.yaml`
- `odooctl/catalog/manifests/odoo-18-community.yaml`
- `odooctl/catalog/manifests/oca-web.yaml`
- `odooctl/catalog/manifests/companions.yaml`
- `odooctl/commands/catalog.py`
- `docs/catalog.md`

## Manifest types

- `StackTemplate`: Odoo image, Postgres image, compose defaults, ports, volumes.
- `AddonSource`: OCA/private/Enterprise repo, ref, subpath, auth env.
- `AddonPack`: grouped addon sources.
- `CompanionService`: pgAdmin, MailHog, Metabase, n8n, MinIO, monitoring.

## Rules

- Prefer pinned image digests/versions.
- No floating `latest` in bundled production templates.
- Private credentials are env/secret references only.
- Catalog is declarative, not a package manager.

## CLI

- `odooctl catalog list`
- `odooctl catalog show <id>`
- `odooctl catalog add <manifest>`

## Acceptance criteria

- Catalog entries validate.
- Setup wizard consumes catalog.
- User manifests can extend bundled catalog.
- A catalog-generated project validates and deploys.
