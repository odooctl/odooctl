from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from odooctl.adapters.snapshots import (
    AwsEbsSnapshotProvider,
    HetznerCloudSnapshotProvider,
    ProviderSnapshot,
    SnapshotCreateRequest,
    SnapshotCreateProviderError,
    SnapshotRestoreProviderError,
)
from odooctl.config import (
    AwsEbsSnapshotConfig,
    HetznerSnapshotConfig,
    example_config,
)
from odooctl.metadata.models import (
    SnapshotManifest,
    SnapshotResource,
    SnapshotRestoreMetadata,
)
from odooctl.metadata.store import MetadataStore
from odooctl.services.context import ServiceContext
from odooctl.services.snapshots import (
    SnapshotCreateFailed,
    run_snapshot_create,
    run_snapshot_reconcile,
    run_snapshot_restore,
)
from odooctl.utils.shell import CommandError, CommandResult


SNAPSHOT_ID = "production-20260730-deadbeef"
INSTANCE_ID = "i-0123456789abcdef0"


def _result(args: list[str], stdout: str = "") -> CommandResult:
    return CommandResult(args, 0, stdout, "")


def _request() -> SnapshotCreateRequest:
    return SnapshotCreateRequest(
        snapshot_id=SNAPSHOT_ID,
        project="demo",
        environment="production",
        description="DR snapshot",
    )


def _aws_config(
    *,
    completion_timeout_seconds: int = 600,
) -> AwsEbsSnapshotConfig:
    return AwsEbsSnapshotConfig(
        instance_id=INSTANCE_ID,
        region="us-east-1",
        availability_zone="us-east-1a",
        completion_timeout_seconds=completion_timeout_seconds,
        poll_interval_seconds=0.01,
    )


def _aws_instance_response() -> str:
    return json.dumps(
        {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": INSTANCE_ID,
                            "RootDeviceName": "/dev/sda1",
                            "Placement": {"AvailabilityZone": "us-east-1a"},
                            "BlockDeviceMappings": [
                                {
                                    "DeviceName": "/dev/sda1",
                                    "Ebs": {
                                        "VolumeId": "vol-root",
                                        "DeleteOnTermination": True,
                                    },
                                },
                                {
                                    "DeviceName": "/dev/sdf",
                                    "Ebs": {
                                        "VolumeId": "vol-data",
                                        "DeleteOnTermination": False,
                                    },
                                },
                            ],
                        }
                    ]
                }
            ]
        }
    )


def _aws_volumes_response() -> str:
    return json.dumps(
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
                    "KmsKeyId": "arn:aws:kms:us-east-1:123:key/root",
                },
                {
                    "VolumeId": "vol-data",
                    "AvailabilityZone": "us-east-1a",
                    "Size": 100,
                    "VolumeType": "io2",
                    "Iops": 12000,
                    "Encrypted": False,
                },
            ]
        }
    )


def _aws_created_response() -> str:
    return json.dumps(
        {
            "Snapshots": [
                {"SnapshotId": "snap-root", "VolumeId": "vol-root"},
                {"SnapshotId": "snap-data", "VolumeId": "vol-data"},
            ]
        }
    )


def _aws_completed_response() -> str:
    return json.dumps(
        {
            "Snapshots": [
                {
                    "SnapshotId": "snap-root",
                    "VolumeId": "vol-root",
                    "State": "completed",
                    "VolumeSize": 20,
                },
                {
                    "SnapshotId": "snap-data",
                    "VolumeId": "vol-data",
                    "State": "completed",
                    "VolumeSize": 100,
                },
            ]
        }
    )


def _reconstruction_runner(calls: list[list[str]]):
    def runner(args, **kwargs):
        command = list(args)
        calls.append(command)
        if command[-1] == "--version":
            return _result(command, "aws-cli/2")
        if "describe-instances" in command:
            return _result(command, _aws_instance_response())
        if "describe-volumes" in command:
            return _result(command, _aws_volumes_response())
        if "create-snapshots" in command:
            return _result(command, _aws_created_response())
        if "describe-snapshots" in command:
            return _result(command, _aws_completed_response())
        return _result(command)

    return runner


def test_aws_pre_describes_storage_and_persists_reconstruction_metadata():
    calls: list[list[str]] = []
    provider = AwsEbsSnapshotProvider(
        _aws_config(),
        runner=_reconstruction_runner(calls),
    )

    created = provider.create(_request())

    assert created.status == "complete"
    operations = [
        next(i for i, call in enumerate(calls) if operation in call)
        for operation in (
            "describe-instances",
            "describe-volumes",
            "create-snapshots",
        )
    ]
    assert operations == sorted(operations)
    describe_instance = next(call for call in calls if "describe-instances" in call)
    assert describe_instance[describe_instance.index("--instance-ids") + 1] == INSTANCE_ID
    describe_volumes = next(call for call in calls if "describe-volumes" in call)
    assert set(
        describe_volumes[
            describe_volumes.index("--volume-ids") + 1 : describe_volumes.index("--region")
        ]
    ) == {
        "vol-root",
        "vol-data",
    }

    by_source = {
        resource.source_resource_id: resource.model_dump() for resource in created.resources
    }
    assert {
        "device_name": "/dev/sda1",
        "volume_type": "gp3",
        "iops": 3000,
        "throughput_mibps": 125,
        "encrypted": True,
        "kms_key_id": "arn:aws:kms:us-east-1:123:key/root",
        "root_device": True,
        "location": "us-east-1a",
    }.items() <= by_source["vol-root"].items()
    assert {
        "device_name": "/dev/sdf",
        "volume_type": "io2",
        "iops": 12000,
        "encrypted": False,
        "root_device": False,
        "location": "us-east-1a",
    }.items() <= by_source["vol-data"].items()


def test_aws_persists_source_shape_before_cloud_mutation():
    calls: list[list[str]] = []
    progress: list[ProviderSnapshot] = []
    base_runner = _reconstruction_runner(calls)

    def runner(args, **kwargs):
        if "create-snapshots" in args:
            assert progress
            assert progress[-1].status == "requested"
            assert all(
                resource.snapshot_resource_id is None
                for resource in progress[-1].resources
            )
            assert {
                resource.source_resource_id
                for resource in progress[-1].resources
            } == {"vol-root", "vol-data"}
        return base_runner(args, **kwargs)

    provider = AwsEbsSnapshotProvider(
        _aws_config(completion_timeout_seconds=0),
        runner=runner,
    )
    request = SnapshotCreateRequest(
        snapshot_id=SNAPSHOT_ID,
        project="demo",
        environment="production",
        description="DR snapshot",
        progress=progress.append,
    )

    provider.create(request)

    assert progress[0].status == "requested"
    assert progress[-1].status == "complete"


def test_aws_describe_snapshots_is_scoped_to_current_account():
    calls: list[list[str]] = []
    provider = AwsEbsSnapshotProvider(
        _aws_config(),
        runner=_reconstruction_runner(calls),
    )

    provider.create(_request())

    descriptions = [call for call in calls if "describe-snapshots" in call]
    assert descriptions
    assert all(call[call.index("--owner-ids") + 1] == "self" for call in descriptions)


def test_aws_wait_timeout_reconciles_pending_snapshot_by_durable_marker():
    calls: list[list[str]] = []

    def runner(args, **kwargs):
        command = list(args)
        calls.append(command)
        if command[-1] == "--version":
            return _result(command, "aws-cli/2")
        if "describe-instances" in command:
            return _result(command, _aws_instance_response())
        if "describe-volumes" in command:
            return _result(command, _aws_volumes_response())
        if "create-snapshots" in command:
            return _result(
                command,
                json.dumps(
                    {
                        "Snapshots": [
                            {
                                "SnapshotId": "snap-pending-root",
                                "VolumeId": "vol-root",
                            },
                            {
                                "SnapshotId": "snap-pending-data",
                                "VolumeId": "vol-data",
                            },
                        ]
                    }
                ),
            )
        if "snapshot-completed" in command:
            failed = CommandResult(command, 255, "", "Waiter timed out")
            raise CommandError(failed)
        if "describe-snapshots" in command:
            return _result(
                command,
                json.dumps(
                    {
                        "Snapshots": [
                            {
                                "SnapshotId": "snap-pending-root",
                                "VolumeId": "vol-root",
                                "State": "pending",
                                "VolumeSize": 20,
                            },
                            {
                                "SnapshotId": "snap-pending-data",
                                "VolumeId": "vol-data",
                                "State": "pending",
                                "VolumeSize": 100,
                            },
                        ]
                    }
                ),
            )
        raise AssertionError(f"Unexpected command: {command}")

    provider = AwsEbsSnapshotProvider(
        _aws_config(completion_timeout_seconds=0),
        runner=runner,
    )

    created = provider.create(_request())

    assert created.status == "pending"
    assert {resource.snapshot_resource_id for resource in created.resources} == {
        "snap-pending-root",
        "snap-pending-data",
    }
    assert all(resource.state == "pending" for resource in created.resources)
    assert sum("create-snapshots" in call for call in calls) == 1
    create = next(call for call in calls if "create-snapshots" in call)
    tag_specifications = json.loads(create[create.index("--tag-specifications") + 1])
    assert {
        "Key": "odooctl:snapshot",
        "Value": SNAPSHOT_ID,
    } in tag_specifications[0]["Tags"]
    reconciliations = [call for call in calls if "describe-snapshots" in call]
    assert reconciliations
    assert all("--owner-ids" in call and "self" in call for call in reconciliations)
    assert any("snap-pending-root" in call for call in reconciliations)
    assert any("snap-pending-data" in call for call in reconciliations)


def test_aws_create_rejects_ambiguous_duplicate_snapshot_candidates():
    calls: list[list[str]] = []

    def runner(args, **kwargs):
        command = list(args)
        calls.append(command)
        if command[-1] == "--version":
            return _result(command, "aws-cli/2")
        if "describe-instances" in command:
            return _result(command, _aws_instance_response())
        if "describe-volumes" in command:
            return _result(command, _aws_volumes_response())
        if "create-snapshots" in command:
            return CommandResult(
                command,
                255,
                "",
                "transport outcome unknown",
            )
        if "describe-snapshots" in command:
            return _result(
                command,
                json.dumps(
                    {
                        "Snapshots": [
                            {
                                "SnapshotId": "snap-root-a",
                                "VolumeId": "vol-root",
                                "State": "completed",
                            },
                            {
                                "SnapshotId": "snap-root-b",
                                "VolumeId": "vol-root",
                                "State": "completed",
                            },
                            {
                                "SnapshotId": "snap-data",
                                "VolumeId": "vol-data",
                                "State": "completed",
                            },
                        ]
                    }
                ),
            )
        raise AssertionError(f"Unexpected command: {command}")

    provider = AwsEbsSnapshotProvider(
        _aws_config(completion_timeout_seconds=0),
        runner=runner,
    )

    with pytest.raises(
        SnapshotCreateProviderError,
        match="snap-root-a, snap-root-b, snap-data",
    ):
        provider.create(_request())


def _aws_restore_manifest() -> SnapshotManifest:
    return SnapshotManifest(
        snapshot_id=SNAPSHOT_ID,
        project="demo",
        environment="production",
        provider="aws_ebs",
        source_resource_id=INSTANCE_ID,
        resources=[
            SnapshotResource(
                snapshot_resource_id="snap-root",
                source_resource_id="vol-root",
                kind="ebs_volume",
                location="us-east-1a",
                device_name="/dev/sda1",
                volume_type="gp3",
                iops=3000,
                throughput_mibps=125,
                encrypted=True,
                root_device=True,
            ),
            SnapshotResource(
                snapshot_resource_id="snap-data",
                source_resource_id="vol-data",
                kind="ebs_volume",
                location="us-east-1a",
                device_name="/dev/sdf",
                volume_type="io2",
                iops=12000,
                encrypted=False,
                root_device=False,
            ),
        ],
        scope=["ec2_instance_all_attached_ebs_volumes"],
        consistency="crash_consistent",
    )


def test_aws_restore_replays_storage_shape_with_per_volume_idempotency():
    provider = AwsEbsSnapshotProvider(_aws_config(), runner=lambda *args, **kwargs: None)
    manifest = _aws_restore_manifest()

    first_plan = provider.plan_restore(manifest)
    second_plan = provider.plan_restore(manifest)

    assert first_plan.commands == second_plan.commands
    assert len(first_plan.commands) == 2
    client_tokens: list[str] = []
    for command, expected_type, expected_iops in zip(
        first_plan.commands,
        ("gp3", "io2"),
        ("3000", "12000"),
        strict=True,
    ):
        assert command[command.index("--volume-type") + 1] == expected_type
        assert command[command.index("--iops") + 1] == expected_iops
        token = command[command.index("--client-token") + 1]
        assert token
        assert len(token) <= 64
        client_tokens.append(token)
        tags = json.loads(command[command.index("--tag-specifications") + 1])
        tag_values = {item["Key"]: item["Value"] for item in tags[0]["Tags"]}
        restore_markers = {key: value for key, value in tag_values.items() if "restore" in key}
        assert token in restore_markers.values()
    assert first_plan.commands[0][first_plan.commands[0].index("--throughput") + 1] == "125"
    assert "--throughput" not in first_plan.commands[1]
    assert len(set(client_tokens)) == 2


def test_aws_historical_snapshot_survives_source_instance_replacement():
    provider = AwsEbsSnapshotProvider(
        AwsEbsSnapshotConfig(
            instance_id="i-0fedcba9876543210",
            region="us-east-1",
            recovery_availability_zone="us-east-1b",
        ),
        runner=lambda *args, **kwargs: None,
    )
    manifest = _aws_restore_manifest().model_copy(
        update={"provider_scope": {"region": "us-east-1"}}
    )

    plan = provider.plan_restore(manifest)

    assert plan.source_resource_id == INSTANCE_ID
    assert all("us-east-1b" in command for command in plan.commands)


def test_aws_restore_rechecks_remote_snapshot_state_before_creating_volumes():
    calls: list[list[str]] = []

    def runner(args, **kwargs):
        command = list(args)
        calls.append(command)
        if command[-1] == "--version":
            return _result(command, "aws-cli/2")
        if "describe-snapshots" in command:
            pending = json.loads(_aws_completed_response())
            pending["Snapshots"][1]["State"] = "pending"
            return _result(command, json.dumps(pending))
        raise AssertionError(f"Unexpected command: {command}")

    provider = AwsEbsSnapshotProvider(_aws_config(), runner=runner)

    with pytest.raises(RuntimeError, match="no longer a complete"):
        provider.restore(_aws_restore_manifest())

    assert not any("create-volume" in call for call in calls)


def test_aws_restore_rejects_retagged_unrelated_volume_and_retains_its_id():
    manifest = _aws_restore_manifest().model_copy(
        update={"resources": [_aws_restore_manifest().resources[0]]}
    )
    calls: list[list[str]] = []
    provider: AwsEbsSnapshotProvider
    client_token = ""

    def runner(args, **kwargs):
        command = list(args)
        calls.append(command)
        if command[-1] == "--version":
            return _result(command, "aws-cli/2")
        if "describe-snapshots" in command:
            return _result(
                command,
                json.dumps(
                    {
                        "Snapshots": [
                            {
                                "SnapshotId": "snap-root",
                                "VolumeId": "vol-root",
                                "State": "completed",
                            }
                        ]
                    }
                ),
            )
        if "describe-volumes" in command and "--filters" in command:
            return _result(
                command,
                json.dumps(
                    {
                        "Volumes": [
                            {
                                "VolumeId": "vol-retagged",
                                "SnapshotId": "snap-unrelated",
                                "AvailabilityZone": "us-east-1a",
                                "State": "available",
                                "Attachments": [],
                                "VolumeType": "gp3",
                                "Iops": 3000,
                                "Throughput": 125,
                                "Encrypted": True,
                                "Tags": [
                                    {
                                        "Key": "odooctl:restored-from",
                                        "Value": manifest.snapshot_id,
                                    },
                                    {
                                        "Key": "odooctl:restore-set",
                                        "Value": provider._restore_marker(manifest),
                                    },
                                    {
                                        "Key": "odooctl:restore",
                                        "Value": client_token,
                                    },
                                    {
                                        "Key": "odooctl:project",
                                        "Value": manifest.project,
                                    },
                                    {
                                        "Key": "odooctl:environment",
                                        "Value": manifest.environment,
                                    },
                                ],
                            }
                        ]
                    }
                ),
            )
        raise AssertionError(f"Unexpected command: {command}")

    provider = AwsEbsSnapshotProvider(_aws_config(), runner=runner)
    client_token = provider.plan_restore(manifest).commands[0][
        provider.plan_restore(manifest).commands[0].index("--client-token") + 1
    ]

    with pytest.raises(SnapshotRestoreProviderError) as caught:
        provider.restore(manifest)

    assert caught.value.restored_resource_ids == ("vol-retagged",)
    assert "was not created from snapshot snap-root" in str(caught.value)
    assert not any("create-volume" in call for call in calls)


def test_aws_restore_reconciles_volume_when_create_response_is_lost():
    manifest = _aws_restore_manifest()
    config = _aws_config(completion_timeout_seconds=0)
    planner = AwsEbsSnapshotProvider(config, runner=lambda *args, **kwargs: None)
    resource_by_snapshot = {
        str(resource.snapshot_resource_id): resource
        for resource in manifest.resources
    }
    accepted: dict[str, dict] = {}
    create_calls = 0

    def volume_payload(
        volume_id: str,
        snapshot_id: str,
        client_token: str,
    ) -> dict:
        resource = resource_by_snapshot[snapshot_id]
        payload = {
            "VolumeId": volume_id,
            "SnapshotId": snapshot_id,
            "AvailabilityZone": "us-east-1a",
            "State": "available",
            "Attachments": [],
            "VolumeType": resource.volume_type,
            "Iops": resource.iops,
            "Encrypted": resource.encrypted,
            "Tags": [
                {
                    "Key": "odooctl:restored-from",
                    "Value": manifest.snapshot_id,
                },
                {
                    "Key": "odooctl:restore-set",
                    "Value": planner._restore_marker(manifest),
                },
                {"Key": "odooctl:restore", "Value": client_token},
                {"Key": "odooctl:project", "Value": manifest.project},
                {
                    "Key": "odooctl:environment",
                    "Value": manifest.environment,
                },
            ],
        }
        if resource.throughput_mibps is not None:
            payload["Throughput"] = resource.throughput_mibps
        return payload

    def runner(args, **kwargs):
        nonlocal create_calls
        command = list(args)
        if command[-1] == "--version":
            return _result(command, "aws-cli/2")
        if "describe-snapshots" in command:
            return _result(command, _aws_completed_response())
        if "describe-volumes" in command and "--filters" in command:
            filter_value = command[command.index("--filters") + 1]
            client_token = filter_value.rsplit("=", 1)[-1]
            found = accepted.get(client_token)
            return _result(
                command,
                json.dumps({"Volumes": [found] if found else []}),
            )
        if "create-volume" in command:
            create_calls += 1
            snapshot_id = command[command.index("--snapshot-id") + 1]
            client_token = command[command.index("--client-token") + 1]
            volume_id = (
                "vol-restored-root"
                if snapshot_id == "snap-root"
                else "vol-restored-data"
            )
            accepted[client_token] = volume_payload(
                volume_id,
                snapshot_id,
                client_token,
            )
            if create_calls == 2:
                raise RuntimeError(
                    "transport closed after AWS accepted create-volume"
                )
            return _result(command, json.dumps({"VolumeId": volume_id}))
        if "describe-volumes" in command and "--volume-ids" in command:
            volume_ids = command[
                command.index("--volume-ids") + 1 : command.index("--region")
            ]
            by_id = {
                volume["VolumeId"]: volume for volume in accepted.values()
            }
            return _result(
                command,
                json.dumps(
                    {"Volumes": [by_id[volume_id] for volume_id in volume_ids]}
                ),
            )
        raise AssertionError(f"Unexpected command: {command}")

    provider = AwsEbsSnapshotProvider(config, runner=runner)
    progress = []

    restored = provider.restore(manifest, progress=progress.append)

    assert restored.status == "complete"
    assert restored.restored_resource_ids == (
        "vol-restored-root",
        "vol-restored-data",
    )
    assert progress[-1].status == "complete"
    assert create_calls == 2


def _write_aws_project(tmp_path: Path) -> ServiceContext:
    data = yaml.safe_load(example_config())
    data["backups"].pop("remote", None)
    data["sanitization"]["sql_files"] = []
    data["snapshots"] = {
        "provider": "aws_ebs",
        "pre_deploy": "disabled",
        "aws_ebs": {
            "instance_id": INSTANCE_ID,
            "region": "us-east-1",
            "availability_zone": "us-east-1a",
        },
    }
    path = tmp_path / "odooctl.yml"
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return ServiceContext.from_config_path(path)


def test_snapshot_service_persists_pending_provider_state(tmp_path: Path):
    ctx = _write_aws_project(tmp_path)

    class PendingProvider:
        name = "aws_ebs"

        def create(self, request):
            return ProviderSnapshot(
                source_resource_id=INSTANCE_ID,
                resources=(
                    SnapshotResource(
                        snapshot_resource_id="snap-pending-data",
                        source_resource_id="vol-data",
                        kind="ebs_volume",
                        state="pending",
                    ),
                ),
                scope=("ec2_instance_non_root_ebs_volumes",),
                consistency="crash_consistent",
                recovery_notes=("Provider completion is still pending.",),
                status="pending",
                provider_scope={
                    "account_id": "123456789012",
                    "region": "us-east-1",
                },
                provider_metadata={"source_instance_id": INSTANCE_ID},
            )

    manifest = run_snapshot_create(
        ctx,
        "production",
        provider=PendingProvider(),
    )
    persisted = MetadataStore(ctx.project.state_dir).get_snapshot(manifest.snapshot_id)

    assert manifest.status == "pending"
    assert persisted.status == "pending"
    assert persisted.resources[0].state == "pending"
    assert persisted.provider_scope == {
        "account_id": "123456789012",
        "region": "us-east-1",
    }
    assert persisted.provider_metadata["source_instance_id"] == INSTANCE_ID


def test_snapshot_create_transport_failure_preserves_last_known_pending_state(
    tmp_path: Path,
):
    ctx = _write_aws_project(tmp_path)

    class AmbiguousProvider:
        name = "aws_ebs"

        def create(self, request):
            request.progress(
                ProviderSnapshot(
                    source_resource_id=INSTANCE_ID,
                    resources=(
                        SnapshotResource(
                            snapshot_resource_id="snap-ambiguous",
                            source_resource_id="vol-data",
                            kind="ebs_volume",
                            state="pending",
                        ),
                    ),
                    scope=("ec2_instance_non_root_ebs_volumes",),
                    consistency="crash_consistent",
                    recovery_notes=("Reconcile before retrying creation.",),
                    status="pending",
                )
            )
            raise RuntimeError("provider response was lost")

    with pytest.raises(SnapshotCreateFailed) as caught:
        run_snapshot_create(
            ctx,
            "production",
            provider=AmbiguousProvider(),
        )

    failed = caught.value.manifest
    persisted = MetadataStore(ctx.project.state_dir).get_snapshot(
        failed.snapshot_id
    )
    assert failed.status == "pending"
    assert persisted.status == "pending"
    assert persisted.resources[0].snapshot_resource_id == "snap-ambiguous"
    assert persisted.last_error == "provider response was lost"


def test_snapshot_reconcile_promotes_pending_manifest_and_honours_env_lock(
    tmp_path: Path,
):
    ctx = _write_aws_project(tmp_path)
    pending = _aws_restore_manifest().model_copy(
        update={
            "project": ctx.project.config.project.name,
            "status": "pending",
            "resources": [
                resource.model_copy(update={"state": "pending"})
                for resource in _aws_restore_manifest().resources
            ],
            "provider_scope": {"region": "us-east-1"},
        }
    )
    MetadataStore(ctx.project.state_dir).save_snapshot_manifest(pending)

    class ReconcileProvider:
        name = "aws_ebs"
        calls = 0

        def reconcile(self, manifest):
            self.calls += 1
            return ProviderSnapshot(
                source_resource_id=manifest.source_resource_id,
                resources=tuple(
                    resource.model_copy(update={"state": "completed"})
                    for resource in manifest.resources
                ),
                scope=tuple(manifest.scope),
                consistency=manifest.consistency,
                recovery_notes=tuple(manifest.recovery_notes),
                status="complete",
                provider_scope=dict(manifest.provider_scope),
                provider_metadata=dict(manifest.provider_metadata),
            )

    provider = ReconcileProvider()
    with pytest.raises(RuntimeError, match="not locked environment"):
        run_snapshot_reconcile(
            ctx,
            pending.snapshot_id,
            expected_environment="staging",
            provider=provider,
        )
    assert provider.calls == 0

    complete = run_snapshot_reconcile(
        ctx,
        pending.snapshot_id,
        expected_environment="production",
        provider=provider,
    )
    assert complete.status == "complete"
    assert complete.completed_at
    assert all(resource.state == "completed" for resource in complete.resources)
    assert (
        MetadataStore(ctx.project.state_dir).get_snapshot(pending.snapshot_id).status
        == "complete"
    )


def test_aws_partial_restore_ids_are_retained_in_failure_metadata(tmp_path: Path):
    ctx = _write_aws_project(tmp_path)
    manifest = _aws_restore_manifest().model_copy(
        update={"project": ctx.project.config.project.name}
    )
    MetadataStore(ctx.project.state_dir).save_snapshot_manifest(manifest)
    create_count = 0

    def runner(args, **kwargs):
        nonlocal create_count
        command = list(args)
        if command[-1] == "--version":
            return _result(command, "aws-cli/2")
        if "describe-snapshots" in command:
            return _result(command, _aws_completed_response())
        if "describe-volumes" in command and "--filters" in command:
            return _result(command, json.dumps({"Volumes": []}))
        if "create-volume" in command:
            create_count += 1
            if create_count == 1:
                return _result(command, json.dumps({"VolumeId": "vol-restored-root"}))
            raise RuntimeError("second volume could not be created")
        raise AssertionError(f"Unexpected command: {command}")

    provider = AwsEbsSnapshotProvider(
        _aws_config(completion_timeout_seconds=0),
        runner=runner,
    )

    with pytest.raises(RuntimeError, match="second volume"):
        run_snapshot_restore(
            ctx,
            manifest.snapshot_id,
            execute=True,
            confirm_snapshot_id=manifest.snapshot_id,
            confirm_resource_id=manifest.source_resource_id,
            provider=provider,
        )

    restore_files = list((ctx.project.state_dir / "snapshots" / "restores").glob("*.json"))
    history = [
        SnapshotRestoreMetadata.model_validate_json(path.read_text()) for path in restore_files
    ]
    assert {item.status for item in history} == {"pending", "failed"}
    failure = next(item for item in history if item.status == "failed")
    assert failure.status == "failed"
    assert failure.restored_resource_ids == ["vol-restored-root"]


def _hetzner_runner(
    calls: list[tuple[list[str], dict]],
    *,
    server_status: str = "running",
    image_status: str = "available",
):
    def runner(args, **kwargs):
        command = list(args)
        calls.append((command, kwargs))
        if "server" in command and "describe" in command:
            return _result(
                command,
                json.dumps(
                    {
                        "id": 42,
                        "name": "odoo-prod",
                        "status": server_status,
                        "volumes": [],
                    }
                ),
            )
        if "image" in command and "list" in command:
            return _result(
                command,
                json.dumps(
                    [
                        {
                            "id": 12345,
                            "status": image_status,
                            "disk_size": 40,
                            "created_from": {"id": 42},
                        }
                    ]
                ),
            )
        return _result(command, "ok")

    return runner


@pytest.mark.parametrize(
    "server",
    [
        {"id": 42, "name": "odoo-prod", "status": "running"},
        {
            "id": 42,
            "name": "odoo-prod",
            "status": "running",
            "volumes": None,
        },
    ],
)
def test_hetzner_create_requires_explicit_attached_volume_inventory(
    monkeypatch,
    server: dict,
):
    monkeypatch.setenv("HCLOUD_TOKEN", "token-for-test")
    calls: list[list[str]] = []

    def runner(args, **kwargs):
        command = list(args)
        calls.append(command)
        if "server" in command and "describe" in command:
            return _result(command, json.dumps(server))
        return _result(command, "ok")

    provider = HetznerCloudSnapshotProvider(
        HetznerSnapshotConfig(
            server="odoo-prod",
            recovery_server_type="cx23",
            recovery_location="nbg1",
            recovery_network="odoo-recovery",
        ),
        runner=runner,
    )

    with pytest.raises(RuntimeError, match="attached-volume list"):
        provider.create(_request())

    assert not any("create-image" in command for command in calls)


def test_hetzner_custom_token_env_is_mapped_without_secret_argv(monkeypatch):
    calls: list[tuple[list[str], dict]] = []
    monkeypatch.delenv("HCLOUD_TOKEN", raising=False)
    monkeypatch.setenv("ODOOCTL_HCLOUD_TOKEN", "custom-secret-value")
    provider = HetznerCloudSnapshotProvider(
        HetznerSnapshotConfig(
            server="odoo-prod",
            recovery_server_type="cx23",
            recovery_location="nbg1",
            recovery_network="odoo-recovery",
            token_env="ODOOCTL_HCLOUD_TOKEN",
        ),
        runner=_hetzner_runner(calls),
    )

    provider.create(_request())

    assert calls
    assert all(kwargs["env"]["HCLOUD_TOKEN"] == "custom-secret-value" for _, kwargs in calls)
    assert all(
        "custom-secret-value" not in argument for command, _ in calls for argument in command
    )


def test_hetzner_create_pins_canonical_server_and_rejects_wrong_image_source(
    monkeypatch,
):
    monkeypatch.setenv("HCLOUD_TOKEN", "token-for-test")
    calls: list[list[str]] = []
    progress: list[ProviderSnapshot] = []

    def runner(args, **kwargs):
        command = list(args)
        calls.append(command)
        if "server" in command and "describe" in command:
            return _result(
                command,
                json.dumps(
                    {
                        "id": 42,
                        "name": "odoo-prod",
                        "status": "running",
                        "volumes": [],
                    }
                ),
            )
        if "image" in command and "list" in command:
            return _result(
                command,
                json.dumps(
                    [
                        {
                            "id": 12345,
                            "status": "available",
                            "created_from": {"id": 99},
                        }
                    ]
                ),
            )
        return _result(command, "ok")

    provider = HetznerCloudSnapshotProvider(
        HetznerSnapshotConfig(
            server="odoo-prod",
            recovery_server_type="cx23",
            recovery_location="nbg1",
            recovery_network="odoo-recovery",
        ),
        runner=runner,
    )

    with pytest.raises(SnapshotCreateProviderError) as caught:
        provider.create(
            SnapshotCreateRequest(
                snapshot_id=SNAPSHOT_ID,
                project="demo",
                environment="production",
                description="DR snapshot",
                progress=progress.append,
            )
        )

    create = next(call for call in calls if "create-image" in call)
    assert create[-1] == "42"
    assert caught.value.snapshot is not None
    assert caught.value.snapshot.status == "failed"
    assert caught.value.snapshot.resources[0].snapshot_resource_id == "12345"
    assert [item.status for item in progress] == ["requested"]


def test_hetzner_historical_snapshot_survives_context_rename():
    provider = HetznerCloudSnapshotProvider(
        HetznerSnapshotConfig(
            server="replacement-source",
            recovery_server_type="cx23",
            recovery_location="nbg1",
            recovery_network="odoo-recovery",
            context="renamed-controller-context",
        ),
        runner=lambda *args, **kwargs: None,
    )
    manifest = SnapshotManifest(
        snapshot_id=SNAPSHOT_ID,
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
        scope=["hetzner_server_local_root_disk"],
        consistency="live_unverified",
        provider_scope={"context": "old-local-context"},
    )

    plan = provider.plan_restore(manifest)

    assert plan.source_resource_id == "42"
    assert plan.commands[0][0:3] == (
        "hcloud",
        "--context",
        "renamed-controller-context",
    )


def test_hetzner_context_can_supply_credentials_without_environment_token(
    monkeypatch,
):
    calls: list[tuple[list[str], dict]] = []
    monkeypatch.delenv("HCLOUD_TOKEN", raising=False)
    monkeypatch.delenv("ODOOCTL_HCLOUD_TOKEN", raising=False)
    provider = HetznerCloudSnapshotProvider(
        HetznerSnapshotConfig(
            server="odoo-prod",
            recovery_server_type="cx23",
            recovery_location="nbg1",
            recovery_network="odoo-recovery",
            context="production",
            token_env="ODOOCTL_HCLOUD_TOKEN",
        ),
        runner=_hetzner_runner(calls),
    )

    provider.create(_request())

    assert calls
    assert all(command[1:3] == ["--context", "production"] for command, _ in calls)
    assert all(
        kwargs["unset_env"] == ("HCLOUD_TOKEN",)
        for _, kwargs in calls
    )


def test_hetzner_context_removes_ambient_token_that_would_override_it(
    monkeypatch,
):
    calls: list[tuple[list[str], dict]] = []
    monkeypatch.setenv("HCLOUD_TOKEN", "wrong-project-token")
    provider = HetznerCloudSnapshotProvider(
        HetznerSnapshotConfig(
            server="odoo-prod",
            recovery_server_type="cx23",
            recovery_location="nbg1",
            recovery_network="odoo-recovery",
            context="production",
        ),
        runner=_hetzner_runner(calls),
    )

    provider.create(_request())

    assert calls
    assert all(kwargs["env"] is None for _, kwargs in calls)
    assert all(
        kwargs["unset_env"] == ("HCLOUD_TOKEN",)
        for _, kwargs in calls
    )


@pytest.mark.parametrize(
    ("server_status", "expected_consistency"),
    [
        ("running", "live_unverified"),
        ("off", "powered_off_consistent"),
    ],
)
def test_hetzner_consistency_reflects_source_power_state(
    monkeypatch,
    server_status: str,
    expected_consistency: str,
):
    calls: list[tuple[list[str], dict]] = []
    monkeypatch.setenv("HCLOUD_TOKEN", "token-for-test")
    provider = HetznerCloudSnapshotProvider(
        HetznerSnapshotConfig(
            server="odoo-prod",
            recovery_server_type="cx23",
            recovery_location="nbg1",
            recovery_network="odoo-recovery",
            completion_timeout_seconds=0,
            poll_interval_seconds=0.01,
        ),
        runner=_hetzner_runner(calls, server_status=server_status),
    )

    created = provider.create(_request())

    assert created.consistency == expected_consistency


def test_hetzner_does_not_accept_image_that_is_still_being_created(monkeypatch):
    calls: list[tuple[list[str], dict]] = []
    monkeypatch.setenv("HCLOUD_TOKEN", "token-for-test")
    provider = HetznerCloudSnapshotProvider(
        HetznerSnapshotConfig(
            server="odoo-prod",
            recovery_server_type="cx23",
            recovery_location="nbg1",
            recovery_network="odoo-recovery",
            completion_timeout_seconds=0,
            poll_interval_seconds=0.01,
        ),
        runner=_hetzner_runner(calls, image_status="creating"),
    )

    created = provider.create(_request())

    assert created.status == "pending"
    assert created.resources[0].state == "creating"


@pytest.mark.parametrize(
    ("image", "message"),
    [
        (
            {
                "type": "snapshot",
                "status": "available",
                "created_from": {"id": 42},
            },
            "returned image None",
        ),
        (
            {
                "id": 12345,
                "status": "available",
                "created_from": {"id": 42},
            },
            "not snapshot",
        ),
        (
            {"id": 12345, "type": "snapshot", "status": "available"},
            "records source server None",
        ),
        (
            {
                "id": 12345,
                "type": "snapshot",
                "status": "available",
                "created_from": {"id": 99},
            },
            "not the canonical source '42'",
        ),
    ],
)
def test_hetzner_restore_rejects_unbound_or_incomplete_image_before_mutation(
    monkeypatch,
    image: dict,
    message: str,
):
    monkeypatch.setenv("HCLOUD_TOKEN", "token-for-test")
    calls: list[list[str]] = []

    def runner(args, **kwargs):
        command = list(args)
        calls.append(command)
        if command[-1] == "version":
            return _result(command, "hcloud 1.50")
        if "image" in command and "describe" in command:
            return _result(command, json.dumps(image))
        raise AssertionError(f"Unexpected command: {command}")

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
        snapshot_id=SNAPSHOT_ID,
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
        scope=["hetzner_server_local_root_disk"],
        consistency="live_unverified",
    )

    with pytest.raises(RuntimeError, match=message):
        provider.restore(manifest)

    assert not any(
        "server" in command and "create" in command for command in calls
    )


def test_hetzner_unsafe_recovery_server_is_reported_with_created_id(
    monkeypatch,
):
    monkeypatch.setenv("HCLOUD_TOKEN", "token-for-test")
    progress = []

    def runner(args, **kwargs):
        command = list(args)
        if command[-1] == "version":
            return _result(command, "hcloud 1.50")
        if "image" in command and "describe" in command:
            return _result(
                command,
                json.dumps(
                    {
                        "id": 12345,
                        "type": "snapshot",
                        "status": "available",
                        "created_from": {"id": 42},
                    }
                ),
            )
        if "network" in command and "describe" in command:
            return _result(command, json.dumps({"id": 777}))
        if "server" in command and "create" in command:
            return _result(command, json.dumps({"server": {"id": 67890}}))
        if "server" in command and "describe" in command:
            return _result(
                command,
                json.dumps(
                    {
                        "id": 67890,
                        "status": "running",
                        "image": {"id": 12345},
                        "public_net": {
                            "ipv4": {"ip": "203.0.113.8"},
                            "ipv6": None,
                        },
                        "private_net": [{"network": 777}],
                    }
                ),
            )
        raise AssertionError(f"Unexpected command: {command}")

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
        snapshot_id=SNAPSHOT_ID,
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
        consistency="live_unverified",
    )

    with pytest.raises(SnapshotRestoreProviderError) as caught:
        provider.restore(manifest, progress=progress.append)

    assert caught.value.restored_resource_ids == ("67890",)
    assert progress[-1].status == "pending"
    assert progress[-1].restored_resource_ids == ("67890",)
    assert "failed safety validation" in str(caught.value)


def test_hetzner_restore_reconciles_server_when_create_response_is_lost(
    monkeypatch,
):
    monkeypatch.setenv("HCLOUD_TOKEN", "token-for-test")
    create_attempted = False

    def runner(args, **kwargs):
        nonlocal create_attempted
        command = list(args)
        if command[-1] == "version":
            return _result(command, "hcloud 1.50")
        if "image" in command and "describe" in command:
            return _result(
                command,
                json.dumps(
                    {
                        "id": 12345,
                        "type": "snapshot",
                        "status": "available",
                        "created_from": {"id": 42},
                    }
                ),
            )
        if "network" in command and "describe" in command:
            return _result(command, json.dumps({"id": 777}))
        if "server" in command and "create" in command:
            create_attempted = True
            raise RuntimeError(
                "transport closed after Hetzner accepted server create"
            )
        if "server" in command and "list" in command:
            assert create_attempted
            return _result(command, json.dumps([{"id": 67890}]))
        if "server" in command and "describe" in command:
            return _result(
                command,
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
        raise AssertionError(f"Unexpected command: {command}")

    provider = HetznerCloudSnapshotProvider(
        HetznerSnapshotConfig(
            server="odoo-prod",
            recovery_server_type="cx23",
            recovery_location="nbg1",
            recovery_network="odoo-recovery",
            completion_timeout_seconds=0,
        ),
        runner=runner,
    )
    manifest = SnapshotManifest(
        snapshot_id=SNAPSHOT_ID,
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
        scope=["hetzner_server_local_root_disk"],
        consistency="live_unverified",
    )
    progress = []

    restored = provider.restore(manifest, progress=progress.append)

    assert restored.status == "complete"
    assert restored.restored_resource_ids == ("67890",)
    assert progress[0].status == "pending"
    assert progress[-1].status == "complete"
