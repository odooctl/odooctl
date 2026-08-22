# Installation

`odooctl` is packaged as a normal Python CLI. The recommended operator install is an
isolated tool environment, not a checkout-specific virtualenv.

## Release channels

Stable installs stay on the stable channel unless you explicitly request a
prerelease. Do not use an unpinned beta in automation: pin the exact version
and record it with the deployment or drill report.

### Stable `0.2.0`

```bash
python3 -m pip install --user pipx
python3 -m pipx ensurepath
pipx install 'odooctl==0.2.0'
odooctl --version
```

With uv instead:

```bash
uv tool install 'odooctl==0.2.0'
odooctl --version
```

### Explicit beta `0.3.0b1`

```bash
# pipx: replace an existing stable tool environment with the beta
pipx install --force 'odooctl==0.3.0b1'

# uv: replace an existing stable tool environment with the beta
uv tool install --force 'odooctl==0.3.0b1'

odooctl --version
```

`0.3.0b1` is prerelease documentation. Commands and configuration can change
before `0.3.0` final; consult the beta docs selector rather than assuming a
stable command is unchanged.

### Return from beta to stable

```bash
pipx install --force 'odooctl==0.2.0'
# or: uv tool install --force 'odooctl==0.2.0'
odooctl --version
```

For later stable maintenance upgrades, use `pipx upgrade odooctl` or
`uv tool upgrade odooctl`; their normal resolver behavior does not select a
prerelease unless you opt in.

## Release tags and release checklist

Release tags must be valid PEP 440 versions prefixed with `v`: for example,
`v0.3.0`, `v0.3.0b1`, `v0.3.0a1`, or `v0.3.0rc1`. The release workflow parses
the package version, rejects a tag mismatch, and treats every PEP 440
prerelease as a prerelease GitHub release.

Before publishing a release, verify the exact artifact with each installation
command documented for its channel, run `odooctl --version`, and confirm the
immutable documentation snapshot displays that same package version.

## Development install from a checkout

```bash
uv venv
uv pip install -e '.[dev]'
pytest -q
```

## Optional S3 dependencies

The core package intentionally avoids cloud SDK dependencies. Install the optional
S3 extra only on hosts that use a future real S3 remote adapter:

```bash
pipx inject odooctl 'odooctl[s3]'
# or for uv tool installs:
uv tool install 'odooctl[s3]'
```

## Runtime prerequisites

A Python package manager only installs the `odooctl` CLI. The deployment host still
needs the platform tools used to operate Odoo.

Required for Docker Compose projects:

- Docker Engine with the Compose plugin (`docker compose version`).
- Access to the project repo that contains `odooctl.yml` and `docker-compose.yml`.
- `tar` for plain `filestore.tar` filestore archives.

Database tooling depends on `runtime.execution_mode`:

- `execution_mode: docker` (recommended default for Docker Compose stacks): PostgreSQL
  tools run inside the configured database service, so the host does not need
  `pg_dump`, `pg_restore`, `psql`, `createdb`, or `dropdb`.
- `execution_mode: host`: install host PostgreSQL client tools (`pg_dump`,
  `pg_restore`, `psql`, `createdb`, `dropdb`) and ensure the configured database is
  reachable from the operator host.

After installation, register or select a project and run doctor:

```bash
odooctl project add acme --path /srv/odoo/acme
odooctl -p acme doctor
```

For an unregistered checkout:

```bash
odooctl -C /srv/odoo/acme doctor
```
