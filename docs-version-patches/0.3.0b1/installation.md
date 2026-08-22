# Installation

<!-- Explicit documentation backport for v0.3.0b1. It corrects the stale
     "coming soon" PyPI text without changing the release tag. -->

`odooctl` is installed in an isolated tool environment. This snapshot is for
the `0.3.0b1` prerelease; commands and configuration may change before `0.3.0`
final.

## Install this beta explicitly

```bash
pipx install --force 'odooctl==0.3.0b1'
# or: uv tool install --force 'odooctl==0.3.0b1'
odooctl --version
```

This beta is never an implicit upgrade target for a stable installation.

## Upgrade from stable and return to stable

```bash
# Upgrade an existing 0.2.0 tool installation to this beta.
pipx install --force 'odooctl==0.3.0b1'
# or: uv tool install --force 'odooctl==0.3.0b1'

# Return to the supported stable snapshot.
pipx install --force 'odooctl==0.2.0'
# or: uv tool install --force 'odooctl==0.2.0'
```

Check the installed version with `odooctl --version`. Ordinary
`pipx upgrade odooctl` and `uv tool upgrade odooctl` remain on stable unless
you explicitly opt into a prerelease.

## Runtime prerequisites

The deployment host still needs Docker Engine with the Compose plugin, access
to the project repository, and `tar` for plain filestore archives. With
`execution_mode: docker`, PostgreSQL client tools run inside the configured DB
container; `execution_mode: host` requires local PostgreSQL client tools.
