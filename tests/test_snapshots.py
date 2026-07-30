from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from odooctl.adapters.snapshots import (
    AwsEbsSnapshotProvider,
    HetznerCloudSnapshotProvider,
    NoSnapshotProvider,
    ProviderRestoreResult,
    ProviderSnapshot,
    SnapshotCreateRequest,
    SnapshotRestorePlan,
    make_snapshot_provider,
)
from odooctl.config import (
    AwsEbsSnapshotConfig,
    HetznerSnapshotConfig,
    OdooCtlConfig,
    example_config,
)
from odooctl.metadata.models import SnapshotManifest, SnapshotResource
from odooctl.metadata.store import MetadataStore
from odooctl.main import app
from odooctl.services.context import ServiceContext
from odooctl.services.snapshots import (
    list_snapshots,
    run_snapshot_create,
    run_snapshot_restore,
)
from odooctl.utils.shell import CommandResult


def _command_result(args: list[str], stdout: str = "") -> CommandResult:
    return CommandResult(args, 0, stdout, "")


def _base_config() -> dict:
    data = yaml.safe_load(example_config())
    data["backups"].pop("remote", None)
    data["sanitization"]["sql_files"] = []
    return data


def _write_snapshot_config(tmp_path: Path, snapshots: dict) -> Path:
    data = _base_config()
    data["snapshots"] = snapshots
    path = tmp_path / "odooctl.yml"
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


def test_snapshot_config_defaults_to_explicit_no_provider_mode():
    cfg = OdooCtlConfig.model_validate(_base_config())
    assert cfg.snapshots.provider == "none"
    assert cfg.snapshots.pre_deploy == "disabled"
    assert isinstance(make_snapshot_provider(cfg.snapshots), NoSnapshotProvider)


def test_no_provider_rejects_enabled_pre_deploy_policy():
    data = _base_config()
    data["snapshots"] = {"provider": "none", "pre_deploy": "required"}
    with pytest.raises(ValidationError, match="must be disabled"):
        OdooCtlConfig.model_validate(data)


def test_selected_provider_requires_matching_settings():
    data = _base_config()
    data["snapshots"] = {"provider": "aws_ebs"}
    with pytest.raises(ValidationError, match="snapshots.aws_ebs is required"):
        OdooCtlConfig.model_validate(data)


def test_snapshot_provider_environment_must_exist():
    data = _base_config()
    data["snapshots"] = {
        "provider": "aws_ebs",
        "environment": "missing",
        "aws_ebs": {
            "instance_id": "i-0123456789abcdef0",
            "region": "us-east-1",
            "recovery_availability_zone": "us-east-1a",
        },
    }
    with pytest.raises(ValidationError, match="snapshots.environment"):
        OdooCtlConfig.model_validate(data)


def test_pre_deploy_snapshot_binding_must_be_protected():
    data = _base_config()
    data["snapshots"] = {
        "provider": "aws_ebs",
        "environment": "staging",
        "pre_deploy": "required",
        "aws_ebs": {
            "instance_id": "i-0123456789abcdef0",
            "region": "us-east-1",
            "recovery_availability_zone": "us-east-1a",
        },
    }
    with pytest.raises(ValidationError, match="to be a protected environment"):
        OdooCtlConfig.model_validate(data)


def test_hetzner_token_env_is_preflighted_unless_context_supplies_credentials():
    data = _base_config()
    data["snapshots"] = {
        "provider": "hetzner_cloud",
        "environment": "production",
        "hetzner_cloud": {
            "server": "odoo-prod",
            "recovery_server_type": "cx23",
            "recovery_location": "nbg1",
            "recovery_network": "odoo-recovery",
            "token_env": "ODOOCTL_HCLOUD_TOKEN",
        },
    }
    cfg = OdooCtlConfig.model_validate(data)
    assert "ODOOCTL_HCLOUD_TOKEN" not in cfg.referenced_env_vars()
    assert "ODOOCTL_HCLOUD_TOKEN" in cfg.referenced_env_vars(
        include_snapshot=True
    )
    assert cfg.snapshot_referenced_env_vars() == ["ODOOCTL_HCLOUD_TOKEN"]

    data["snapshots"]["hetzner_cloud"]["context"] = "production"
    cfg = OdooCtlConfig.model_validate(data)
    assert "ODOOCTL_HCLOUD_TOKEN" not in cfg.referenced_env_vars(
        include_snapshot=True
    )
    assert cfg.snapshot_referenced_env_vars() == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("instance_id", "i-$(touch-pwned)"),
        ("region", "us-east-1;whoami"),
        ("availability_zone", "../../tmp"),
        ("cli_command", "aws --debug"),
    ],
)
def test_aws_snapshot_config_rejects_unsafe_argv_values(field: str, value: str):
    values = {
        "instance_id": "i-0123456789abcdef0",
        "region": "us-east-1",
        "availability_zone": "us-east-1a",
        field: value,
    }
    with pytest.raises(ValidationError):
        AwsEbsSnapshotConfig.model_validate(values)


def test_aws_recovery_zone_must_match_region_boundary():
    with pytest.raises(ValidationError, match="must belong"):
        AwsEbsSnapshotConfig.model_validate(
            {
                "instance_id": "i-0123456789abcdef0",
                "region": "us-east-1",
                "recovery_availability_zone": "us-east-10a",
            }
        )


def test_aws_ebs_create_waits_and_verifies_all_snapshots(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "never-on-argv")

    def runner(args, **kwargs):
        calls.append(list(args))
        if args[-1] == "--version":
            return _command_result(list(args), "aws-cli/2")
        if "describe-instances" in args:
            return _command_result(
                list(args),
                json.dumps(
                    {
                        "Reservations": [
                            {
                                "OwnerId": "123456789012",
                                "Instances": [
                                    {
                                        "InstanceId": "i-0123456789abcdef0",
                                        "RootDeviceName": "/dev/sda1",
                                        "Placement": {
                                            "AvailabilityZone": "us-east-1a"
                                        },
                                        "BlockDeviceMappings": [
                                            {
                                                "DeviceName": "/dev/sda1",
                                                "Ebs": {"VolumeId": "vol-root"},
                                            },
                                            {
                                                "DeviceName": "/dev/sdf",
                                                "Ebs": {"VolumeId": "vol-data"},
                                            },
                                        ],
                                    }
                                ],
                            }
                        ]
                    }
                ),
            )
        if "describe-volumes" in args:
            return _command_result(
                list(args),
                json.dumps(
                    {
                        "Volumes": [
                            {
                                "VolumeId": "vol-root",
                                "AvailabilityZone": "us-east-1a",
                                "Size": 20,
                                "VolumeType": "gp3",
                                "Iops": 3000,
                                "Throughput": 125,
                                "Encrypted": True,
                            },
                            {
                                "VolumeId": "vol-data",
                                "AvailabilityZone": "us-east-1a",
                                "Size": 100,
                                "VolumeType": "gp3",
                                "Iops": 3000,
                                "Throughput": 125,
                                "Encrypted": True,
                            },
                        ]
                    }
                ),
            )
        if "create-snapshots" in args:
            return _command_result(
                list(args),
                json.dumps(
                    {
                        "Snapshots": [
                            {
                                "SnapshotId": "snap-aaa",
                                "VolumeId": "vol-root",
                                "State": "pending",
                            },
                            {
                                "SnapshotId": "snap-bbb",
                                "VolumeId": "vol-data",
                                "State": "pending",
                            },
                        ]
                    }
                ),
            )
        if "describe-snapshots" in args:
            return _command_result(
                list(args),
                json.dumps(
                    {
                        "Snapshots": [
                            {
                                "SnapshotId": "snap-aaa",
                                "VolumeId": "vol-root",
                                "State": "completed",
                                "VolumeSize": 20,
                            },
                            {
                                "SnapshotId": "snap-bbb",
                                "VolumeId": "vol-data",
                                "State": "completed",
                                "VolumeSize": 100,
                            },
                        ]
                    }
                ),
            )
        return _command_result(list(args))

    provider = AwsEbsSnapshotProvider(
        AwsEbsSnapshotConfig(
            instance_id="i-0123456789abcdef0",
            region="us-east-1",
            availability_zone="us-east-1a",
            profile="odooctl",
        ),
        runner=runner,
    )
    result = provider.create(
        SnapshotCreateRequest(
            snapshot_id="production-20260730-deadbeef",
            project="demo",
            environment="production",
            description="DR snapshot",
        )
    )

    assert result.consistency == "crash_consistent"
    assert result.source_resource_id == "i-0123456789abcdef0"
    assert [item.snapshot_resource_id for item in result.resources] == [
        "snap-aaa",
        "snap-bbb",
    ]
    create = next(call for call in calls if "create-snapshots" in call)
    instance_spec = json.loads(create[create.index("--instance-specification") + 1])
    assert instance_spec == {
        "InstanceId": "i-0123456789abcdef0",
        "ExcludeBootVolume": False,
    }
    describe = next(call for call in calls if "describe-snapshots" in call)
    assert describe[describe.index("--owner-ids") + 1] == "self"
    assert not any("snapshot-completed" in call for call in calls)
    assert all("never-on-argv" not in item for call in calls for item in call)


def test_aws_restore_creates_unattached_replacement_volumes():
    calls: list[list[str]] = []
    created = iter(["vol-new-root", "vol-new-data"])
    created_metadata: dict[str, dict] = {}

    def runner(args, **kwargs):
        calls.append(list(args))
        if args[-1] == "--version":
            return _command_result(list(args), "aws-cli/2")
        if "describe-snapshots" in args:
            return _command_result(
                list(args),
                json.dumps(
                    {
                        "Snapshots": [
                            {
                                "SnapshotId": "snap-aaa",
                                "VolumeId": "vol-root",
                                "State": "completed",
                            },
                            {
                                "SnapshotId": "snap-bbb",
                                "VolumeId": "vol-data",
                                "State": "completed",
                            },
                        ]
                    }
                ),
            )
        if "create-volume" in args:
            volume_id = next(created)
            tag_spec = json.loads(
                args[args.index("--tag-specifications") + 1]
            )
            created_metadata[volume_id] = {
                "SnapshotId": args[args.index("--snapshot-id") + 1],
                "AvailabilityZone": args[
                    args.index("--availability-zone") + 1
                ],
                "Tags": tag_spec[0]["Tags"],
            }
            return _command_result(
                list(args),
                json.dumps({"VolumeId": volume_id}),
            )
        if "describe-volumes" in args:
            if "--filters" in args:
                return _command_result(
                    list(args),
                    json.dumps({"Volumes": []}),
                )
            volume_ids = args[
                args.index("--volume-ids") + 1 : args.index("--region")
            ]
            return _command_result(
                list(args),
                json.dumps(
                    {
                        "Volumes": [
                            {
                                "VolumeId": volume_id,
                                "State": "available",
                                "Attachments": [],
                                **created_metadata[volume_id],
                            }
                            for volume_id in volume_ids
                        ]
                    }
                ),
            )
        return _command_result(list(args))

    provider = AwsEbsSnapshotProvider(
        AwsEbsSnapshotConfig(
            instance_id="i-0123456789abcdef0",
            region="us-east-1",
            availability_zone="us-east-1a",
        ),
        runner=runner,
    )
    manifest = SnapshotManifest(
        snapshot_id="production-20260730-deadbeef",
        project="demo",
        environment="production",
        provider="aws_ebs",
        source_resource_id="i-0123456789abcdef0",
        resources=[
            SnapshotResource(
                snapshot_resource_id="snap-aaa",
                source_resource_id="vol-root",
                kind="ebs_volume",
            ),
            SnapshotResource(
                snapshot_resource_id="snap-bbb",
                source_resource_id="vol-data",
                kind="ebs_volume",
            ),
        ],
        scope=["ec2_instance_all_attached_ebs_volumes"],
        consistency="crash_consistent",
    )

    plan = provider.plan_restore(manifest)
    assert plan.destructive is False
    assert all("create-volume" in command for command in plan.commands)
    restored = provider.restore(manifest)
    assert restored.restored_resource_ids == ("vol-new-root", "vol-new-data")
    described = next(
        call
        for call in calls
        if "describe-volumes" in call and "vol-new-root" in call
    )
    assert "vol-new-root" in described and "vol-new-data" in described


def test_hetzner_create_uses_unique_label_and_never_passes_token(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setenv("HCLOUD_TOKEN", "never-on-argv")

    def runner(args, **kwargs):
        calls.append(list(args))
        if "server" in args and "describe" in args:
            return _command_result(
                list(args),
                json.dumps({"id": 42, "name": "odoo-prod", "volumes": []}),
            )
        if "image" in args and "list" in args:
            return _command_result(
                list(args),
                json.dumps(
                    [
                        {
                            "id": 12345,
                            "status": "available",
                            "disk_size": 40,
                            "created_from": {"id": 42},
                        }
                    ]
                ),
            )
        return _command_result(list(args), "ok")

    provider = HetznerCloudSnapshotProvider(
        HetznerSnapshotConfig(
            server="odoo-prod",
            recovery_server_type="cx23",
            recovery_location="nbg1",
            recovery_network="odoo-recovery",
            context="production",
        ),
        runner=runner,
    )
    result = provider.create(
        SnapshotCreateRequest(
            snapshot_id="production-20260730-deadbeef",
            project="demo",
            environment="production",
            description="DR snapshot",
        )
    )

    create = next(call for call in calls if "create-image" in call)
    listing = next(call for call in calls if "image" in call and "list" in call)
    label = create[create.index("--label") + 1]
    assert label.startswith("odooctl-marker=")
    assert listing[listing.index("--selector") + 1] == label
    assert "--output" in listing and "json" in listing
    assert create[-1] == "42"
    assert result.resources[0].snapshot_resource_id == "12345"
    assert result.scope == ("hetzner_server_local_root_disk",)
    assert all("never-on-argv" not in item for call in calls for item in call)


def test_hetzner_refuses_incomplete_snapshot_when_server_has_attached_volumes(
    monkeypatch,
):
    calls: list[list[str]] = []
    monkeypatch.setenv("HCLOUD_TOKEN", "token")

    def runner(args, **kwargs):
        calls.append(list(args))
        if "describe" in args:
            return _command_result(
                list(args),
                json.dumps({"id": 42, "volumes": [9001]}),
            )
        return _command_result(list(args), "ok")

    provider = HetznerCloudSnapshotProvider(
        HetznerSnapshotConfig(
            server="odoo-prod",
            recovery_server_type="cx23",
            recovery_location="nbg1",
            recovery_network="odoo-recovery",
        ),
        runner=runner,
    )

    with pytest.raises(RuntimeError, match="exclude attached Volumes"):
        provider.create(
            SnapshotCreateRequest(
                snapshot_id="production-20260730-deadbeef",
                project="demo",
                environment="production",
                description="DR snapshot",
            )
        )

    assert not any("create-image" in call for call in calls)


def test_hetzner_restore_creates_stopped_isolated_server(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setenv("HCLOUD_TOKEN", "token")

    def runner(args, **kwargs):
        calls.append(list(args))
        if "image" in args and "describe" in args:
            return _command_result(
                list(args),
                json.dumps(
                    {
                        "id": 12345,
                        "type": "snapshot",
                        "status": "available",
                        "created_from": {"id": 42},
                    }
                ),
            )
        if "network" in args and "describe" in args:
            return _command_result(
                list(args),
                json.dumps({"id": 777, "name": "odoo-recovery"}),
            )
        if "server" in args and "create" in args:
            return _command_result(
                list(args),
                json.dumps({"server": {"id": 67890, "name": "recovery"}}),
            )
        if "server" in args and "describe" in args:
            return _command_result(
                list(args),
                json.dumps(
                    {
                        "id": 67890,
                        "status": "off",
                        "image": {"id": 12345},
                        "public_net": {"ipv4": None, "ipv6": None},
                        "private_net": [{"network": 777}],
                    }
                ),
            )
        return _command_result(list(args), "ok")

    provider = HetznerCloudSnapshotProvider(
        HetznerSnapshotConfig(
            server="odoo-prod",
            recovery_server_type="cx23",
            recovery_location="nbg1",
            recovery_network="odoo-recovery",
        ),
        runner=runner,
    )
    manifest = SnapshotManifest(
        snapshot_id="production-20260730-deadbeef",
        project="demo",
        environment="production",
        provider="hetzner_cloud",
        source_resource_id="42",
        resources=[
            SnapshotResource(
                snapshot_resource_id="12345",
                source_resource_id="42",
                kind="server_root_disk",
                state="available",
            )
        ],
        scope=["hetzner_server_root_disk"],
        consistency="crash_consistent",
    )
    plan = provider.plan_restore(manifest)
    assert plan.destructive is False
    command = plan.commands[0]
    assert command[:3] == ("hcloud", "server", "create")
    assert command[command.index("--image") + 1] == "12345"
    assert command[command.index("--type") + 1] == "cx23"
    assert command[command.index("--location") + 1] == "nbg1"
    assert command[command.index("--network") + 1] == "odoo-recovery"
    assert "--start-after-create=false" in command
    assert "--without-ipv4" in command
    assert "--without-ipv6" in command
    result = provider.restore(manifest)
    assert result.restored_resource_ids == ("67890",)
    assert any("create" in call for call in calls)


class FakeSnapshotProvider:
    name = "hetzner_cloud"

    def __init__(self):
        self.create_calls = 0
        self.restore_calls = 0

    def create(self, request):
        self.create_calls += 1
        return ProviderSnapshot(
            source_resource_id="odoo-prod",
            resources=(
                SnapshotResource(
                    snapshot_resource_id="image-123",
                    source_resource_id="odoo-prod",
                    kind="server_root_disk",
                ),
            ),
            scope=("hetzner_server_root_disk",),
            consistency="crash_consistent",
            recovery_notes=("external recovery only",),
        )

    def plan_restore(self, manifest):
        return SnapshotRestorePlan(
            provider="hetzner_cloud",
            snapshot_id=manifest.snapshot_id,
            source_resource_id=manifest.source_resource_id,
            commands=(("hcloud", "server", "create"),),
            destructive=False,
            notes=("isolated recovery",),
        )

    def reconcile(self, manifest):
        return ProviderSnapshot(
            source_resource_id=manifest.source_resource_id,
            resources=tuple(manifest.resources),
            scope=tuple(manifest.scope),
            consistency=manifest.consistency,
            recovery_notes=tuple(manifest.recovery_notes),
            status=manifest.status,
            provider_scope=dict(manifest.provider_scope),
            provider_metadata=dict(manifest.provider_metadata),
        )

    def restore(self, manifest, *, progress=None):
        self.restore_calls += 1
        result = ProviderRestoreResult(
            restored_resource_ids=("odoo-prod",),
            message="accepted",
        )
        if progress:
            progress(result)
        return result


def test_snapshot_create_rejects_environment_outside_provider_binding(
    tmp_path: Path,
):
    config = _write_snapshot_config(
        tmp_path,
        {
            "provider": "hetzner_cloud",
            "environment": "production",
            "pre_deploy": "disabled",
            "hetzner_cloud": {
                "server": "odoo-prod",
                "recovery_server_type": "cx23",
                "recovery_location": "nbg1",
                "recovery_network": "odoo-recovery",
            },
        },
    )
    ctx = ServiceContext.from_config_path(config)
    provider = FakeSnapshotProvider()

    with pytest.raises(RuntimeError, match="bound to environment 'production'"):
        run_snapshot_create(ctx, "staging", provider=provider)

    assert provider.create_calls == 0
    assert MetadataStore(ctx.project.state_dir).list_snapshots() == []


def test_snapshot_service_persists_separate_manifest_and_lists_it(tmp_path: Path):
    config = _write_snapshot_config(
        tmp_path,
        {
            "provider": "hetzner_cloud",
            "pre_deploy": "disabled",
            "hetzner_cloud": {
                "server": "odoo-prod",
                "recovery_server_type": "cx23",
                "recovery_location": "nbg1",
                "recovery_network": "odoo-recovery",
            },
        },
    )
    ctx = ServiceContext.from_config_path(config)
    manifest = run_snapshot_create(
        ctx,
        "production",
        portable_backup_id="production-portable-1",
        provider=FakeSnapshotProvider(),
    )

    assert (ctx.project.state_dir / "snapshots" / f"{manifest.snapshot_id}.json").exists()
    assert not (ctx.project.state_dir / "backups" / f"{manifest.snapshot_id}.json").exists()
    assert manifest.portable_backup_id == "production-portable-1"
    assert list_snapshots(ctx, "production")[0].snapshot_id == manifest.snapshot_id


def test_snapshot_restore_requires_exact_snapshot_and_resource_confirmation(
    tmp_path: Path,
):
    config = _write_snapshot_config(
        tmp_path,
        {
            "provider": "hetzner_cloud",
            "pre_deploy": "disabled",
            "hetzner_cloud": {
                "server": "odoo-prod",
                "recovery_server_type": "cx23",
                "recovery_location": "nbg1",
                "recovery_network": "odoo-recovery",
            },
        },
    )
    ctx = ServiceContext.from_config_path(config)
    provider = FakeSnapshotProvider()
    manifest = run_snapshot_create(
        ctx,
        "production",
        provider=provider,
    )

    with pytest.raises(RuntimeError, match="Snapshot confirmation"):
        run_snapshot_restore(
            ctx,
            manifest.snapshot_id,
            execute=True,
            confirm_snapshot_id="wrong",
            confirm_resource_id="odoo-prod",
            provider=provider,
        )
    with pytest.raises(RuntimeError, match="Source-resource confirmation"):
        run_snapshot_restore(
            ctx,
            manifest.snapshot_id,
            execute=True,
            confirm_snapshot_id=manifest.snapshot_id,
            confirm_resource_id="wrong",
            provider=provider,
        )
    assert provider.restore_calls == 0

    outcome = run_snapshot_restore(
        ctx,
        manifest.snapshot_id,
        execute=True,
        confirm_snapshot_id=manifest.snapshot_id,
        confirm_resource_id="odoo-prod",
        provider=provider,
    )
    assert outcome.executed is True
    assert provider.restore_calls == 1


def test_snapshot_plan_does_not_call_provider_restore(tmp_path: Path):
    config = _write_snapshot_config(
        tmp_path,
        {
            "provider": "hetzner_cloud",
            "pre_deploy": "disabled",
            "hetzner_cloud": {
                "server": "odoo-prod",
                "recovery_server_type": "cx23",
                "recovery_location": "nbg1",
                "recovery_network": "odoo-recovery",
            },
        },
    )
    ctx = ServiceContext.from_config_path(config)
    provider = FakeSnapshotProvider()
    manifest = run_snapshot_create(ctx, "production", provider=provider)

    outcome = run_snapshot_restore(
        ctx,
        manifest.snapshot_id,
        execute=False,
        provider=provider,
    )

    assert outcome.executed is False
    assert provider.restore_calls == 0
    restore_files = list(
        (ctx.project.state_dir / "snapshots" / "restores").glob("*.json")
    )
    assert restore_files


def test_queued_snapshot_restore_must_match_locked_environment(tmp_path: Path):
    config = _write_snapshot_config(
        tmp_path,
        {
            "provider": "hetzner_cloud",
            "pre_deploy": "disabled",
            "hetzner_cloud": {
                "server": "odoo-prod",
                "recovery_server_type": "cx23",
                "recovery_location": "nbg1",
                "recovery_network": "odoo-recovery",
            },
        },
    )
    ctx = ServiceContext.from_config_path(config)
    provider = FakeSnapshotProvider()
    manifest = run_snapshot_create(ctx, "production", provider=provider)

    with pytest.raises(RuntimeError, match="not locked environment"):
        run_snapshot_restore(
            ctx,
            manifest.snapshot_id,
            execute=True,
            confirm_snapshot_id=manifest.snapshot_id,
            confirm_resource_id=manifest.source_resource_id,
            expected_environment="staging",
            provider=provider,
        )

    assert provider.restore_calls == 0


def test_snapshot_store_rejects_path_traversal(tmp_path: Path):
    store = MetadataStore(tmp_path / ".odooctl")
    with pytest.raises(ValueError, match="snapshot_id"):
        store.get_snapshot("../outside")


def test_snapshot_cli_create_list_plan_and_typed_restore(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("HCLOUD_TOKEN", "test-token")
    _write_snapshot_config(
        tmp_path,
        {
            "provider": "hetzner_cloud",
            "pre_deploy": "disabled",
            "hetzner_cloud": {
                "server": "odoo-prod",
                "recovery_server_type": "cx23",
                "recovery_location": "nbg1",
                "recovery_network": "odoo-recovery",
            },
        },
    )
    provider = FakeSnapshotProvider()
    monkeypatch.setattr(
        "odooctl.services.snapshots.make_snapshot_provider",
        lambda config: provider,
    )
    runner = CliRunner()

    created = runner.invoke(
        app,
        ["-C", str(tmp_path), "dr", "snapshot", "create", "production"],
    )
    assert created.exit_code == 0, created.output
    manifest = MetadataStore(tmp_path / ".odooctl").list_snapshots()[0]
    assert manifest.snapshot_id in created.output

    listed = runner.invoke(
        app,
        ["-C", str(tmp_path), "dr", "snapshot", "list", "--json"],
    )
    assert listed.exit_code == 0, listed.output
    assert json.loads(listed.output)[0]["snapshot_id"] == manifest.snapshot_id

    reconciled = runner.invoke(
        app,
        [
            "-C",
            str(tmp_path),
            "dr",
            "snapshot",
            "reconcile",
            manifest.snapshot_id,
        ],
    )
    assert reconciled.exit_code == 0, reconciled.output
    assert f"{manifest.snapshot_id}: complete" in reconciled.output

    planned = runner.invoke(
        app,
        [
            "-C",
            str(tmp_path),
            "dr",
            "snapshot",
            "restore",
            manifest.snapshot_id,
        ],
    )
    assert planned.exit_code == 0, planned.output
    assert "Provider recovery commands" in planned.output
    assert provider.restore_calls == 0

    rejected = runner.invoke(
        app,
        [
            "-C",
            str(tmp_path),
            "dr",
            "snapshot",
            "restore",
            manifest.snapshot_id,
            "--execute",
        ],
        input=f"wrong\n{manifest.source_resource_id}\n",
    )
    assert rejected.exit_code != 0
    assert provider.restore_calls == 0

    restored = runner.invoke(
        app,
        [
            "-C",
            str(tmp_path),
            "dr",
            "snapshot",
            "restore",
            manifest.snapshot_id,
            "--execute",
            "--confirm-snapshot",
            manifest.snapshot_id,
            "--confirm-resource",
            manifest.source_resource_id,
        ],
    )
    assert restored.exit_code == 0, restored.output
    assert "Restored provider resources" in restored.output
    assert provider.restore_calls == 1
