# M13 — Web UI MVP

## Goal

Build a web dashboard on top of the API that gives users Odoo.sh-like workflows without duplicating backend logic.

## Locked UI technology

Use a **static SPA served by `odooctl serve`**. The backend API is FastAPI from M12.

Rules:

- UI talks only to API endpoints.
- UI does not shell out to CLI.
- UI does not import service/adapters directly.
- No Django-style monolith.
- No server-rendered app logic that duplicates services.
- Built SPA assets are packaged and served by FastAPI.


## UX lessons to borrow

From `community-sh`, borrow:

- friendly dashboard
- New Instance button
- instance/environment detail page
- tabs for status/logs/backups/domains/modules/operations
- one-click clone/duplicate concept
- visible URLs and copy buttons
- logs and operation feedback

Do not borrow:

- Docker socket in web tier
- arbitrary console as a casual feature
- weak defaults
- synchronous HTTP deploys

## Pages

1. Projects page
2. Import existing project flow
3. New project/setup flow
4. Environments page
5. Environment detail page
6. Operations page
7. Backups page/tab
8. Doctor page/tab
9. Domains page/tab
10. Settings/security page

## Environment detail tabs

- Overview
- Doctor
- Operations
- Logs
- Backups
- Clone/Promote
- Modules
- Domains/SSL
- Schedule

## Files to create

- `odooctl/web/README.md`
- `odooctl/web/` app structure
- packaged built assets under `odooctl/web/dist/`
- `docs/web-ui.md`

## Acceptance criteria

- `odooctl serve` serves the FastAPI API and packaged static SPA.
- UI reads projects/environments through API.
- Clone production → staging enqueues operation and streams events.
- Backups and operations render live data.
- Destructive actions require typed confirmation.
- UI hides/disables actions based on RBAC.
- UI has no direct Docker/Compose access.
