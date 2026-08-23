from __future__ import annotations

from pathlib import Path

import typer

from odooctl.config import load_config
from odooctl.utils.logging import success

WORKFLOW_FILENAME = "odooctl-deploy.yml"


def render_workflow(environments: dict[str, str] | None = None, default_branch: str = "main", config_path: str = "odooctl.yml") -> str:
    environments = environments or {"staging": "staging", "production": "main"}
    options = "\n".join(f"          - {name}" for name in environments)
    branch_cases = "\n".join(f'            {name}) expected_branch="{branch}" ;;' for name, branch in environments.items())
    return f"""name: odooctl deploy

# Push-based deploys are the SECONDARY model. The primary CI/CD model is
# pull-based: `odooctl sync` on a systemd timer on the server itself
# (`odooctl schedule sync --env <env>`), which needs no inbound access and no
# runner. This workflow requires a SELF-HOSTED runner installed on the server
# (or with SSH/Docker access to it) — GitHub-hosted runners cannot reach your
# VPS Docker daemon.

on:
  workflow_dispatch:
    inputs:
      environment:
        description: Target environment
        required: true
        type: choice
        options:
{options}
      branch:
        description: Git branch to deploy
        required: true
        default: {default_branch}
        type: string

jobs:
  deploy:
    runs-on: [self-hosted]
    steps:
      - name: Enforce branch/environment mapping
        run: |
          case "${{{{ inputs.environment }}}}" in
{branch_cases}
            *) echo "::error::Unknown environment '${{{{ inputs.environment }}}}'"; exit 1 ;;
          esac
          if [ "${{{{ inputs.branch }}}}" != "$expected_branch" ]; then
            echo "::error::Branch '${{{{ inputs.branch }}}}' is not allowed for environment '${{{{ inputs.environment }}}}' (expected '$expected_branch')"
            exit 1
          fi
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - name: Install odooctl
        run: pip install .
      - name: Validate config
        run: odooctl validate --config {config_path}
      - name: Deploy
        env:
          ODOO_DB_PASSWORD: ${{{{ secrets.ODOO_DB_PASSWORD }}}}
        run: |
          odooctl deploy ${{{{ inputs.environment }}}} --branch ${{{{ inputs.branch }}}} --config {config_path}
"""


def run(config: str = "odooctl.yml", output: str = f".github/workflows/{WORKFLOW_FILENAME}", dry_run: bool = False, force: bool = False) -> str:
    parsed = load_config(config)
    environments = {name: parsed.environments[name].branch for name in sorted(parsed.environments)}
    default_branch = parsed.environments.get("production", next(iter(parsed.environments.values()))).branch
    content = render_workflow(environments, default_branch, config_path=config)
    path = Path(output)
    if dry_run:
        return content
    if path.exists() and not force:
        raise typer.BadParameter(f"{output} already exists; pass --force to overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    success(f"Created {output}")
    return content
