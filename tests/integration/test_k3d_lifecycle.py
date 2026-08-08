from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from odooctl.services.context import ServiceContext
from odooctl.services.local_cluster import (
    create_local_cluster,
    delete_local_cluster,
    run_local_smoke,
)


pytestmark = [pytest.mark.integration, pytest.mark.docker]


@pytest.mark.skipif(
    os.getenv("ODOOCTL_RUN_K3D") != "1",
    reason="set ODOOCTL_RUN_K3D=1 to run the disposable k3d lifecycle",
)
def test_disposable_k3d_lifecycle(tmp_path: Path):
    for command in ("docker", "k3d", "kubectl"):
        if shutil.which(command) is None:
            pytest.skip(f"{command} is not installed")
    source = Path(__file__).parents[2] / "examples/k3d"
    project = tmp_path / "k3d-project"
    shutil.copytree(source, project)
    ctx = ServiceContext.from_config_path(project / "odooctl.yml")

    created = create_local_cluster(ctx)
    try:
        try:
            smoke = run_local_smoke(ctx)
        except Exception:
            cfg = ctx.project.config
            namespace = (
                f"{cfg.project.name}-{cfg.local_simulation.environment}".lower()
            )
            context = f"k3d-{created.cluster_name}"
            for args in (
                ["get", "pods", "-o", "wide"],
                ["get", "events", "--sort-by=.lastTimestamp"],
                ["describe", "deployment/odoo"],
                ["logs", "deployment/odoo", "--tail=100"],
            ):
                subprocess.run(
                    [
                        "kubectl",
                        "--context",
                        context,
                        "--namespace",
                        namespace,
                        *args,
                    ],
                    check=False,
                    text=True,
                )
            raise
        assert smoke.cluster_name == created.cluster_name
        assert smoke.backup_path.is_file()
        assert smoke.rollback_verified is True
    finally:
        delete_local_cluster(ctx)
