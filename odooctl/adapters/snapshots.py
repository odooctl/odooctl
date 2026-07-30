"""Provider-native disaster-recovery snapshot adapters.

Every provider command is passed as an argv array. Credentials are supplied
through provider-native environment/configuration chains and are never placed
in command previews or manifests.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field, replace
from typing import Callable, Protocol

from odooctl.config import (
    AwsEbsSnapshotConfig,
    HetznerSnapshotConfig,
    SnapshotsConfig,
)
from odooctl.metadata.models import SnapshotManifest, SnapshotResource
from odooctl.utils.shell import CommandResult, run


@dataclass(frozen=True)
class SnapshotCreateRequest:
    snapshot_id: str
    project: str
    environment: str
    description: str
    progress: Callable[[ProviderSnapshot], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )


@dataclass(frozen=True)
class ProviderSnapshot:
    source_resource_id: str
    resources: tuple[SnapshotResource, ...]
    scope: tuple[str, ...]
    consistency: str
    recovery_notes: tuple[str, ...]
    status: str = "complete"
    provider_scope: dict[str, str] = field(default_factory=dict)
    provider_metadata: dict[
        str,
        str | int | float | bool | None,
    ] = field(default_factory=dict)


class SnapshotCreateProviderError(RuntimeError):
    """A create failure that may already have provider-side resources."""

    def __init__(
        self,
        message: str,
        *,
        snapshot: ProviderSnapshot | None = None,
    ):
        super().__init__(message)
        self.snapshot = snapshot


@dataclass(frozen=True)
class SnapshotRestorePlan:
    provider: str
    snapshot_id: str
    source_resource_id: str
    commands: tuple[tuple[str, ...], ...]
    destructive: bool
    notes: tuple[str, ...]


@dataclass(frozen=True)
class ProviderRestoreResult:
    restored_resource_ids: tuple[str, ...]
    message: str
    status: str = "complete"


class SnapshotRestoreProviderError(RuntimeError):
    """A restore failure that preserves every resource already created."""

    def __init__(
        self,
        message: str,
        *,
        restored_resource_ids: tuple[str, ...] = (),
    ):
        super().__init__(message)
        self.restored_resource_ids = restored_resource_ids


class SnapshotProvider(Protocol):
    name: str

    def create(self, request: SnapshotCreateRequest) -> ProviderSnapshot: ...
    def reconcile(self, manifest: SnapshotManifest) -> ProviderSnapshot: ...
    def plan_restore(self, manifest: SnapshotManifest) -> SnapshotRestorePlan: ...
    def restore(
        self,
        manifest: SnapshotManifest,
        *,
        progress: Callable[[ProviderRestoreResult], None] | None = None,
    ) -> ProviderRestoreResult: ...


class NoSnapshotProvider:
    name = "none"

    @staticmethod
    def _raise() -> None:
        raise RuntimeError(
            "No snapshot provider is configured. Set snapshots.provider to "
            "aws_ebs or hetzner_cloud."
        )

    def create(self, request: SnapshotCreateRequest) -> ProviderSnapshot:
        self._raise()
        raise AssertionError("unreachable")

    def reconcile(self, manifest: SnapshotManifest) -> ProviderSnapshot:
        self._raise()
        raise AssertionError("unreachable")

    def plan_restore(self, manifest: SnapshotManifest) -> SnapshotRestorePlan:
        self._raise()
        raise AssertionError("unreachable")

    def restore(
        self,
        manifest: SnapshotManifest,
        *,
        progress: Callable[[ProviderRestoreResult], None] | None = None,
    ) -> ProviderRestoreResult:
        self._raise()
        raise AssertionError("unreachable")


class AwsEbsSnapshotProvider:
    name = "aws_ebs"

    def __init__(self, config: AwsEbsSnapshotConfig, *, runner=run):
        self.config = config
        self._run = runner

    def _global_args(self) -> list[str]:
        args = ["--region", self.config.region]
        if self.config.profile:
            args.extend(["--profile", self.config.profile])
        args.extend(["--output", "json", "--no-cli-pager"])
        return args

    def _command(self, *args: str) -> list[str]:
        return [self.config.cli_command, "ec2", *args, *self._global_args()]

    def _identity_command(self) -> list[str]:
        return [
            self.config.cli_command,
            "sts",
            "get-caller-identity",
            *self._global_args(),
        ]

    def _probe(self) -> None:
        self._run([self.config.cli_command, "--version"], stream=False)

    @staticmethod
    def _json(result: CommandResult, operation: str) -> dict:
        try:
            data = json.loads(result.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"AWS CLI returned invalid JSON for {operation}"
            ) from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"AWS CLI returned an invalid result for {operation}")
        return data

    def _describe_source(
        self,
    ) -> tuple[
        list[dict[str, str | int | bool | None]],
        dict[str, str],
        dict[str, str | int | bool | None],
    ]:
        instance_result = self._run(
            self._command(
                "describe-instances",
                "--instance-ids",
                self.config.instance_id,
            ),
            stream=False,
        )
        data = self._json(instance_result, "describe-instances")
        reservations = data.get("Reservations")
        if not isinstance(reservations, list):
            raise RuntimeError("AWS did not return EC2 instance metadata")

        instance: dict | None = None
        owner_id: str | None = None
        for reservation in reservations:
            if not isinstance(reservation, dict):
                continue
            instances = reservation.get("Instances")
            if not isinstance(instances, list):
                continue
            for candidate in instances:
                if (
                    isinstance(candidate, dict)
                    and candidate.get("InstanceId") == self.config.instance_id
                ):
                    instance = candidate
                    if reservation.get("OwnerId"):
                        owner_id = str(reservation["OwnerId"])
                    break
        if instance is None:
            raise RuntimeError(
                f"AWS could not describe EC2 instance {self.config.instance_id}"
            )

        root_device_name = str(instance.get("RootDeviceName") or "")
        placement = instance.get("Placement")
        source_az = (
            str(placement.get("AvailabilityZone"))
            if isinstance(placement, dict) and placement.get("AvailabilityZone")
            else ""
        )
        mappings = instance.get("BlockDeviceMappings")
        if not isinstance(mappings, list):
            raise RuntimeError("AWS returned invalid EC2 block-device mappings")

        included: list[dict[str, str | bool]] = []
        for mapping in mappings:
            if not isinstance(mapping, dict):
                continue
            ebs = mapping.get("Ebs")
            if not isinstance(ebs, dict) or not ebs.get("VolumeId"):
                continue
            device_name = str(mapping.get("DeviceName") or "")
            is_root = bool(root_device_name and device_name == root_device_name)
            if is_root and not self.config.include_root_volume:
                continue
            included.append(
                {
                    "volume_id": str(ebs["VolumeId"]),
                    "device_name": device_name,
                    "root_device": is_root,
                }
            )
        if not included:
            raise RuntimeError(
                "The configured EC2 instance has no EBS volumes in snapshot scope"
            )

        volume_ids = [str(item["volume_id"]) for item in included]
        volumes_result = self._run(
            self._command("describe-volumes", "--volume-ids", *volume_ids),
            stream=False,
        )
        volumes = self._json(volumes_result, "describe-volumes").get("Volumes")
        if not isinstance(volumes, list):
            raise RuntimeError("AWS did not return EBS volume metadata")
        by_id = {
            str(item["VolumeId"]): item
            for item in volumes
            if isinstance(item, dict) and item.get("VolumeId")
        }
        if set(by_id) != set(volume_ids):
            raise RuntimeError("AWS did not describe every EBS volume in scope")

        metadata: list[dict[str, str | int | bool | None]] = []
        for mapping in included:
            volume_id = str(mapping["volume_id"])
            volume = by_id[volume_id]
            metadata.append(
                {
                    "volume_id": volume_id,
                    "device_name": str(mapping["device_name"]),
                    "root_device": bool(mapping["root_device"]),
                    "availability_zone": str(
                        volume.get("AvailabilityZone") or source_az
                    ),
                    "size_gib": volume.get("Size"),
                    "volume_type": (
                        str(volume["VolumeType"])
                        if volume.get("VolumeType")
                        else None
                    ),
                    "iops": volume.get("Iops"),
                    "throughput_mibps": volume.get("Throughput"),
                    "encrypted": volume.get("Encrypted"),
                    "kms_key_id": (
                        str(volume["KmsKeyId"])
                        if volume.get("KmsKeyId")
                        else None
                    ),
                }
            )

        provider_scope = {"region": self.config.region}
        if owner_id:
            provider_scope["account_id"] = owner_id
        provider_metadata: dict[str, str | int | bool | None] = {
            "source_availability_zone": source_az or None,
            "recovery_availability_zone": (
                self.config.recovery_availability_zone
            ),
            "source_volume_count": len(metadata),
            "include_root_volume": self.config.include_root_volume,
        }
        return metadata, provider_scope, provider_metadata

    def _snapshot_tags(
        self,
        request: SnapshotCreateRequest,
    ) -> str:
        return json.dumps(
            [
                {
                    "ResourceType": "snapshot",
                    "Tags": [
                        {"Key": "odooctl:snapshot", "Value": request.snapshot_id},
                        {"Key": "odooctl:project", "Value": request.project},
                        {
                            "Key": "odooctl:environment",
                            "Value": request.environment,
                        },
                    ],
                }
            ],
            separators=(",", ":"),
        )

    def _describe_snapshots(
        self,
        *,
        snapshot_ids: list[str] | None = None,
        snapshot_id_tag: str | None = None,
    ) -> list[dict]:
        args = ["describe-snapshots", "--owner-ids", "self"]
        if snapshot_ids:
            args.extend(["--snapshot-ids", *snapshot_ids])
        if snapshot_id_tag:
            args.extend(
                [
                    "--filters",
                    f"Name=tag:odooctl:snapshot,Values={snapshot_id_tag}",
                ]
            )
        result = self._run(self._command(*args), stream=False)
        snapshots = self._json(result, "describe-snapshots").get("Snapshots")
        if not isinstance(snapshots, list) or not all(
            isinstance(item, dict) for item in snapshots
        ):
            raise RuntimeError("AWS returned invalid EBS snapshot metadata")
        return snapshots

    def _create_snapshot_records(
        self,
        request: SnapshotCreateRequest,
    ) -> list[dict]:
        instance_spec = json.dumps(
            {
                "InstanceId": self.config.instance_id,
                "ExcludeBootVolume": not self.config.include_root_volume,
            },
            separators=(",", ":"),
        )
        command = self._command(
            "create-snapshots",
            "--instance-specification",
            instance_spec,
            "--description",
            request.description[:255],
            "--tag-specifications",
            self._snapshot_tags(request),
        )
        result: CommandResult | None = None
        create_error: Exception | None = None
        try:
            result = self._run(command, stream=False, check=False)
        except Exception as exc:  # provider transport may fail after acceptance
            create_error = exc

        created: object = None
        if result is not None and result.returncode == 0:
            try:
                created = self._json(result, "create-snapshots").get("Snapshots")
            except RuntimeError as exc:
                create_error = exc
        if isinstance(created, list) and created:
            valid = [
                item
                for item in created
                if isinstance(item, dict) and item.get("SnapshotId")
            ]
            if len(valid) == len(created):
                return valid
            create_error = RuntimeError(
                "AWS returned an EBS snapshot without a SnapshotId"
            )

        # CreateSnapshots has no client token. Reconcile the unique local set
        # tag before considering a retry, otherwise a transport timeout can
        # silently create duplicate billable snapshot sets.
        reconcile_deadline = time.monotonic() + min(
            self.config.completion_timeout_seconds,
            60,
        )
        reconcile_error: Exception | None = None
        while True:
            try:
                reconciled = self._describe_snapshots(
                    snapshot_id_tag=request.snapshot_id,
                )
                if reconciled:
                    return reconciled
            except Exception as exc:
                reconcile_error = exc
            remaining = reconcile_deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(self.config.poll_interval_seconds, remaining))

        detail = create_error
        if detail is None:
            detail = reconcile_error
        if detail is None and result is not None:
            detail = RuntimeError(
                f"AWS create-snapshots exited with status {result.returncode}"
            )
        raise RuntimeError(
            "AWS did not return or reconcile any EBS snapshots"
            + (f": {detail}" if detail else "")
        )

    def _provider_snapshot(
        self,
        *,
        described: list[dict],
        volume_metadata: list[dict[str, str | int | bool | None]],
        provider_scope: dict[str, str],
        provider_metadata: dict[str, str | int | bool | None],
    ) -> ProviderSnapshot:
        expected = {str(item["volume_id"]): item for item in volume_metadata}
        candidates = [
            item
            for item in described
            if item.get("VolumeId") and item.get("SnapshotId")
        ]
        candidate_volume_ids = [
            str(item["VolumeId"]) for item in candidates
        ]
        candidate_snapshot_ids = [
            str(item["SnapshotId"]) for item in candidates
        ]
        if (
            len(candidates) != len(expected)
            or len(set(candidate_volume_ids)) != len(candidate_volume_ids)
            or len(set(candidate_snapshot_ids)) != len(candidate_snapshot_ids)
            or set(candidate_volume_ids) != set(expected)
        ):
            discovered = ", ".join(candidate_snapshot_ids) or "none"
            raise RuntimeError(
                "AWS did not return exactly one unique snapshot per EBS volume "
                f"in scope; discovered snapshot IDs: {discovered}"
            )
        by_volume = {
            str(item["VolumeId"]): item for item in candidates
        }

        resources: list[SnapshotResource] = []
        states: list[str] = []
        for volume_id, source in expected.items():
            item = by_volume[volume_id]
            state = str(item.get("State") or "unknown").lower()
            states.append(state)
            resources.append(
                SnapshotResource(
                    snapshot_resource_id=str(item["SnapshotId"]),
                    source_resource_id=volume_id,
                    kind="ebs_volume",
                    state=state,
                    location=(
                        str(source["availability_zone"])
                        if source.get("availability_zone")
                        else None
                    ),
                    size_gib=(
                        int(source["size_gib"])
                        if source.get("size_gib") is not None
                        else None
                    ),
                    device_name=(
                        str(source["device_name"])
                        if source.get("device_name")
                        else None
                    ),
                    root_device=bool(source["root_device"]),
                    volume_type=(
                        str(source["volume_type"])
                        if source.get("volume_type")
                        else None
                    ),
                    iops=(
                        int(source["iops"])
                        if source.get("iops") is not None
                        else None
                    ),
                    throughput_mibps=(
                        int(source["throughput_mibps"])
                        if source.get("throughput_mibps") is not None
                        else None
                    ),
                    encrypted=(
                        bool(source["encrypted"])
                        if source.get("encrypted") is not None
                        else None
                    ),
                    kms_key_id=(
                        str(source["kms_key_id"])
                        if source.get("kms_key_id")
                        else None
                    ),
                )
            )

        if all(state == "completed" for state in states):
            status = "complete"
        elif any(state == "error" for state in states):
            status = "failed"
        else:
            status = "pending"
        scope = (
            "ec2_instance_all_attached_ebs_volumes"
            if self.config.include_root_volume
            else "ec2_instance_non_root_ebs_volumes"
        )
        return ProviderSnapshot(
            source_resource_id=self.config.instance_id,
            resources=tuple(resources),
            scope=(scope,),
            consistency="crash_consistent",
            recovery_notes=(
                "AWS multi-volume snapshots are crash-consistent; application "
                "and operating-system buffers were not quiesced by odooctl.",
                "Recovery creates new, unattached EBS volumes in the configured "
                "recovery availability zone and never overwrites live volumes.",
                "Use the recorded device/root mapping when attaching volumes to "
                "an isolated recovery instance.",
            ),
            status=status,
            provider_scope=provider_scope,
            provider_metadata=provider_metadata,
        )

    def _requested_provider_snapshot(
        self,
        *,
        volume_metadata: list[dict[str, str | int | bool | None]],
        provider_scope: dict[str, str],
        provider_metadata: dict[str, str | int | bool | None],
    ) -> ProviderSnapshot:
        resources = tuple(
            SnapshotResource(
                snapshot_resource_id=None,
                source_resource_id=str(source["volume_id"]),
                kind="ebs_volume",
                state="requested",
                location=(
                    str(source["availability_zone"])
                    if source.get("availability_zone")
                    else None
                ),
                size_gib=(
                    int(source["size_gib"])
                    if source.get("size_gib") is not None
                    else None
                ),
                device_name=(
                    str(source["device_name"])
                    if source.get("device_name")
                    else None
                ),
                root_device=bool(source["root_device"]),
                volume_type=(
                    str(source["volume_type"])
                    if source.get("volume_type")
                    else None
                ),
                iops=(
                    int(source["iops"])
                    if source.get("iops") is not None
                    else None
                ),
                throughput_mibps=(
                    int(source["throughput_mibps"])
                    if source.get("throughput_mibps") is not None
                    else None
                ),
                encrypted=(
                    bool(source["encrypted"])
                    if source.get("encrypted") is not None
                    else None
                ),
                kms_key_id=(
                    str(source["kms_key_id"])
                    if source.get("kms_key_id")
                    else None
                ),
            )
            for source in volume_metadata
        )
        scope = (
            "ec2_instance_all_attached_ebs_volumes"
            if self.config.include_root_volume
            else "ec2_instance_non_root_ebs_volumes"
        )
        return ProviderSnapshot(
            source_resource_id=self.config.instance_id,
            resources=resources,
            scope=(scope,),
            consistency="crash_consistent",
            recovery_notes=(
                "AWS multi-volume snapshots are crash-consistent; application "
                "and operating-system buffers were not quiesced by odooctl.",
                "Recovery creates new, unattached EBS volumes in the configured "
                "recovery availability zone and never overwrites live volumes.",
                "Use the recorded device/root mapping when attaching volumes to "
                "an isolated recovery instance.",
            ),
            status="requested",
            provider_scope=provider_scope,
            provider_metadata=provider_metadata,
        )

    def create(self, request: SnapshotCreateRequest) -> ProviderSnapshot:
        self._probe()
        (
            volume_metadata,
            provider_scope,
            provider_metadata,
        ) = self._describe_source()
        if request.progress:
            request.progress(
                self._requested_provider_snapshot(
                    volume_metadata=volume_metadata,
                    provider_scope=provider_scope,
                    provider_metadata=provider_metadata,
                )
            )
        created_records = self._create_snapshot_records(request)
        snapshot_ids = [str(item["SnapshotId"]) for item in created_records]
        try:
            initial = self._provider_snapshot(
                described=created_records,
                volume_metadata=volume_metadata,
                provider_scope=provider_scope,
                provider_metadata=provider_metadata,
            )
        except Exception as exc:
            raise SnapshotCreateProviderError(
                "AWS returned provider snapshot IDs but not enough source-volume "
                f"metadata to persist them safely ({', '.join(snapshot_ids)}): {exc}"
            ) from exc
        if request.progress:
            request.progress(initial)
        deadline = time.monotonic() + self.config.completion_timeout_seconds

        while True:
            try:
                described = self._describe_snapshots(snapshot_ids=snapshot_ids)
                snapshot = self._provider_snapshot(
                    described=described,
                    volume_metadata=volume_metadata,
                    provider_scope=provider_scope,
                    provider_metadata=provider_metadata,
                )
            except Exception as exc:
                raise SnapshotCreateProviderError(
                    "AWS created or reconciled snapshot IDs "
                    f"{', '.join(snapshot_ids)}, but status reconciliation failed: "
                    f"{exc}"
                ) from exc

            if request.progress:
                request.progress(snapshot)
            if snapshot.status == "complete":
                return snapshot
            if snapshot.status == "failed":
                raise SnapshotCreateProviderError(
                    "One or more EBS snapshots entered the error state",
                    snapshot=snapshot,
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return snapshot
            time.sleep(min(self.config.poll_interval_seconds, remaining))

    @staticmethod
    def _manifest_volume_metadata(
        manifest: SnapshotManifest,
    ) -> list[dict[str, str | int | bool | None]]:
        return [
            {
                "volume_id": resource.source_resource_id,
                "device_name": resource.device_name or "",
                "root_device": resource.root_device,
                "availability_zone": resource.location,
                "size_gib": resource.size_gib,
                "volume_type": resource.volume_type,
                "iops": resource.iops,
                "throughput_mibps": resource.throughput_mibps,
                "encrypted": resource.encrypted,
                "kms_key_id": resource.kms_key_id,
            }
            for resource in manifest.resources
        ]

    def reconcile(self, manifest: SnapshotManifest) -> ProviderSnapshot:
        self._probe()
        self._validate_manifest_scope(manifest)
        self._validate_account_scope(manifest)
        provider_ids = [
            str(resource.snapshot_resource_id)
            for resource in manifest.resources
            if resource.snapshot_resource_id
        ]
        if provider_ids:
            described = self._describe_snapshots(
                snapshot_ids=provider_ids
            )
            volume_metadata = self._manifest_volume_metadata(manifest)
            provider_scope = dict(manifest.provider_scope)
            provider_metadata = dict(manifest.provider_metadata)
        else:
            described = self._describe_snapshots(
                snapshot_id_tag=manifest.snapshot_id,
            )
            if not described:
                raise RuntimeError(
                    f"AWS found no snapshots tagged for {manifest.snapshot_id}"
                )
            if manifest.resources:
                volume_metadata = self._manifest_volume_metadata(manifest)
                provider_scope = dict(manifest.provider_scope)
                provider_metadata = dict(manifest.provider_metadata)
            else:
                (
                    volume_metadata,
                    provider_scope,
                    provider_metadata,
                ) = self._describe_source()
        refreshed = self._provider_snapshot(
            described=described,
            volume_metadata=volume_metadata,
            provider_scope=provider_scope,
            provider_metadata=provider_metadata,
        )
        return ProviderSnapshot(
            source_resource_id=manifest.source_resource_id,
            resources=refreshed.resources,
            scope=tuple(manifest.scope),
            consistency=manifest.consistency,
            recovery_notes=tuple(manifest.recovery_notes),
            status=refreshed.status,
            provider_scope=provider_scope,
            provider_metadata=provider_metadata,
        )

    def _validate_manifest_scope(self, manifest: SnapshotManifest) -> None:
        region = manifest.provider_scope.get("region")
        if region and region != self.config.region:
            raise RuntimeError(
                f"Snapshot {manifest.snapshot_id} belongs to AWS region {region}, "
                f"not configured region {self.config.region}"
            )

    def _validate_account_scope(self, manifest: SnapshotManifest) -> None:
        expected = manifest.provider_scope.get("account_id")
        if not expected:
            return
        result = self._run(self._identity_command(), stream=False)
        actual = self._json(result, "get-caller-identity").get("Account")
        if not actual or str(actual) != expected:
            raise RuntimeError(
                f"Snapshot {manifest.snapshot_id} belongs to AWS account "
                f"{expected}, not the currently authenticated account"
            )

    @staticmethod
    def _restore_marker(manifest: SnapshotManifest) -> str:
        return hashlib.sha256(manifest.snapshot_id.encode()).hexdigest()[:20]

    def _restore_command(
        self,
        manifest: SnapshotManifest,
        resource: SnapshotResource,
    ) -> list[str]:
        if not resource.snapshot_resource_id:
            raise RuntimeError(
                "EBS snapshot resource is missing its provider SnapshotId"
            )
        marker = self._restore_marker(manifest)
        token_digest = hashlib.sha256(
            (
                f"{manifest.snapshot_id}:"
                f"{resource.snapshot_resource_id}:"
                f"{self.config.recovery_availability_zone}"
            ).encode()
        ).hexdigest()
        client_token = f"odooctl-{token_digest[:48]}"
        tags = json.dumps(
            [
                {
                    "ResourceType": "volume",
                    "Tags": [
                        {
                            "Key": "odooctl:restored-from",
                            "Value": manifest.snapshot_id,
                        },
                        {"Key": "odooctl:restore-set", "Value": marker},
                        {"Key": "odooctl:restore", "Value": client_token},
                        {"Key": "odooctl:project", "Value": manifest.project},
                        {
                            "Key": "odooctl:environment",
                            "Value": manifest.environment,
                        },
                    ],
                }
            ],
            separators=(",", ":"),
        )
        args = [
            "create-volume",
            "--snapshot-id",
            resource.snapshot_resource_id,
            "--availability-zone",
            self.config.recovery_availability_zone,
            "--client-token",
            client_token,
            "--tag-specifications",
            tags,
        ]
        if resource.volume_type:
            args.extend(["--volume-type", resource.volume_type])
        if (
            resource.iops is not None
            and resource.volume_type in {"gp3", "io1", "io2"}
        ):
            args.extend(["--iops", str(resource.iops)])
        if (
            resource.throughput_mibps is not None
            and resource.volume_type == "gp3"
        ):
            args.extend(
                ["--throughput", str(resource.throughput_mibps)]
            )
        return self._command(*args)

    def plan_restore(self, manifest: SnapshotManifest) -> SnapshotRestorePlan:
        if manifest.provider != self.name:
            raise RuntimeError(
                f"Snapshot {manifest.snapshot_id} belongs to {manifest.provider}, "
                f"not {self.name}"
            )
        if manifest.status != "complete":
            raise RuntimeError(
                f"Snapshot {manifest.snapshot_id} is {manifest.status}, not complete"
            )
        if not manifest.resources or any(
            resource.state != "completed" or not resource.snapshot_resource_id
            for resource in manifest.resources
        ):
            raise RuntimeError(
                f"Snapshot {manifest.snapshot_id} does not have a fully completed "
                "EBS resource set"
            )
        self._validate_manifest_scope(manifest)
        return SnapshotRestorePlan(
            provider=self.name,
            snapshot_id=manifest.snapshot_id,
            source_resource_id=manifest.source_resource_id,
            commands=tuple(
                tuple(self._restore_command(manifest, resource))
                for resource in manifest.resources
            ),
            destructive=False,
            notes=(
                "The plan creates replacement EBS volumes but does not attach them.",
                "Volume type, provisioned IOPS/throughput, source device name, and "
                "root-device mapping are preserved in the manifest.",
                "Attach and mount the volumes from an external recovery host after "
                "validating the recorded mapping.",
            ),
        )

    def _describe_restored_volumes(self, volume_ids: list[str]) -> list[dict]:
        result = self._run(
            self._command("describe-volumes", "--volume-ids", *volume_ids),
            stream=False,
        )
        volumes = self._json(result, "describe-volumes").get("Volumes")
        if not isinstance(volumes, list) or not all(
            isinstance(item, dict) for item in volumes
        ):
            raise RuntimeError("AWS returned invalid restored-volume metadata")
        returned = {
            str(item["VolumeId"])
            for item in volumes
            if item.get("VolumeId")
        }
        if returned != set(volume_ids):
            raise RuntimeError("AWS did not describe every restored EBS volume")
        return volumes

    def _find_restored_volume(self, client_token: str) -> dict | None:
        result = self._run(
            self._command(
                "describe-volumes",
                "--filters",
                f"Name=tag:odooctl:restore,Values={client_token}",
            ),
            stream=False,
        )
        volumes = self._json(result, "describe-volumes").get("Volumes")
        if not isinstance(volumes, list) or not all(
            isinstance(item, dict) for item in volumes
        ):
            raise RuntimeError("AWS returned invalid restore reconciliation data")
        identified = [item for item in volumes if item.get("VolumeId")]
        if len(identified) > 1:
            raise RuntimeError(
                "AWS returned multiple volumes for one odooctl restore token"
            )
        return identified[0] if identified else None

    def _reconcile_uncertain_restored_volume(
        self,
        client_token: str,
    ) -> dict | None:
        deadline = time.monotonic() + min(
            self.config.completion_timeout_seconds,
            60,
        )
        while True:
            try:
                found = self._find_restored_volume(client_token)
                if found is not None:
                    return found
            except Exception:
                # A provider timeout can affect both create and the first
                # reconciliation read. Retry within the bounded window.
                pass
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            time.sleep(min(self.config.poll_interval_seconds, remaining))

    def _validate_restored_volume(
        self,
        volume: dict,
        *,
        manifest: SnapshotManifest,
        resource: SnapshotResource,
        client_token: str,
    ) -> None:
        volume_id = str(volume.get("VolumeId") or "<unknown>")
        expected_snapshot_id = str(resource.snapshot_resource_id)
        if str(volume.get("SnapshotId") or "") != expected_snapshot_id:
            raise RuntimeError(
                f"Reconciled EBS volume {volume_id} was not created from "
                f"snapshot {expected_snapshot_id}"
            )
        if (
            str(volume.get("AvailabilityZone") or "")
            != self.config.recovery_availability_zone
        ):
            raise RuntimeError(
                f"Reconciled EBS volume {volume_id} is not in recovery zone "
                f"{self.config.recovery_availability_zone}"
            )
        attachments = volume.get("Attachments")
        if not isinstance(attachments, list) or attachments:
            raise RuntimeError(
                f"Reconciled EBS volume {volume_id} is not confirmed unattached"
            )

        expected_fields = {
            "Size": resource.size_gib,
            "VolumeType": resource.volume_type,
            "Iops": resource.iops,
            "Throughput": resource.throughput_mibps,
            "Encrypted": resource.encrypted,
            "KmsKeyId": resource.kms_key_id,
        }
        for field_name, expected in expected_fields.items():
            if expected is None:
                continue
            actual = volume.get(field_name)
            if actual != expected:
                raise RuntimeError(
                    f"Reconciled EBS volume {volume_id} has unexpected "
                    f"{field_name}: {actual!r}, expected {expected!r}"
                )

        raw_tags = volume.get("Tags")
        if not isinstance(raw_tags, list):
            raise RuntimeError(
                f"Reconciled EBS volume {volume_id} has no verifiable tags"
            )
        tags = {
            str(item["Key"]): str(item["Value"])
            for item in raw_tags
            if isinstance(item, dict)
            and item.get("Key") is not None
            and item.get("Value") is not None
        }
        expected_tags = {
            "odooctl:restored-from": manifest.snapshot_id,
            "odooctl:restore-set": self._restore_marker(manifest),
            "odooctl:restore": client_token,
            "odooctl:project": manifest.project,
            "odooctl:environment": manifest.environment,
        }
        if any(tags.get(key) != value for key, value in expected_tags.items()):
            raise RuntimeError(
                f"Reconciled EBS volume {volume_id} does not carry the exact "
                "odooctl recovery identity"
            )

    def _assert_remote_snapshot_complete(
        self,
        manifest: SnapshotManifest,
    ) -> None:
        described = self._describe_snapshots(
            snapshot_ids=[
                str(resource.snapshot_resource_id)
                for resource in manifest.resources
                if resource.snapshot_resource_id
            ]
        )
        expected = {
            (
                str(resource.snapshot_resource_id),
                resource.source_resource_id,
            )
            for resource in manifest.resources
        }
        actual = {
            (str(item.get("SnapshotId")), str(item.get("VolumeId")))
            for item in described
            if item.get("SnapshotId") and item.get("VolumeId")
        }
        if actual != expected or any(
            str(item.get("State") or "").lower() != "completed"
            for item in described
        ):
            raise RuntimeError(
                f"AWS snapshot {manifest.snapshot_id} is no longer a complete "
                "provider resource set"
            )

    def restore(
        self,
        manifest: SnapshotManifest,
        *,
        progress: Callable[[ProviderRestoreResult], None] | None = None,
    ) -> ProviderRestoreResult:
        self._probe()
        self._validate_account_scope(manifest)
        plan = self.plan_restore(manifest)
        self._assert_remote_snapshot_complete(manifest)
        volume_ids: list[str] = []
        try:
            restore_specs = list(
                zip(plan.commands, manifest.resources, strict=True)
            )
            client_tokens: list[str] = []
            for command, resource in restore_specs:
                client_token = command[command.index("--client-token") + 1]
                client_tokens.append(client_token)
                reconciled = self._find_restored_volume(client_token)
                if reconciled is None:
                    create_error: Exception | None = None
                    try:
                        result = self._run(list(command), stream=False)
                        volume_id = self._json(
                            result,
                            "create-volume",
                        ).get("VolumeId")
                        if not volume_id:
                            raise RuntimeError(
                                "AWS did not return a restored EBS VolumeId"
                            )
                        volume_id = str(volume_id)
                    except Exception as exc:
                        create_error = exc
                        reconciled = (
                            self._reconcile_uncertain_restored_volume(
                                client_token
                            )
                        )
                        if reconciled is None:
                            raise RuntimeError(
                                "AWS create-volume outcome is indeterminate "
                                f"after reconciliation: {create_error}"
                            ) from create_error
                        volume_id = str(reconciled["VolumeId"])
                else:
                    volume_id = str(reconciled["VolumeId"])
                volume_ids.append(volume_id)
                if reconciled is not None:
                    self._validate_restored_volume(
                        reconciled,
                        manifest=manifest,
                        resource=resource,
                        client_token=client_token,
                    )
                if progress:
                    progress(
                        ProviderRestoreResult(
                            restored_resource_ids=tuple(volume_ids),
                            status="pending",
                            message=(
                                "Replacement EBS volumes are being created; "
                                "provider IDs have been recorded."
                            ),
                        )
                    )

            deadline = time.monotonic() + self.config.completion_timeout_seconds
            while True:
                volumes = self._describe_restored_volumes(volume_ids)
                by_id = {
                    str(item["VolumeId"]): item
                    for item in volumes
                    if item.get("VolumeId")
                }
                for volume_id, resource, client_token in zip(
                    volume_ids,
                    manifest.resources,
                    client_tokens,
                    strict=True,
                ):
                    self._validate_restored_volume(
                        by_id[volume_id],
                        manifest=manifest,
                        resource=resource,
                        client_token=client_token,
                    )
                states = [
                    str(item.get("State") or "unknown").lower()
                    for item in volumes
                ]
                if all(state == "available" for state in states):
                    completed = ProviderRestoreResult(
                        restored_resource_ids=tuple(volume_ids),
                        message=(
                            "Replacement EBS volumes are available and remain "
                            "unattached; validate them from an external recovery "
                            "host before cutover."
                        ),
                    )
                    if progress:
                        progress(completed)
                    return completed
                if any(state in {"deleted", "deleting", "error"} for state in states):
                    raise RuntimeError(
                        "One or more replacement EBS volumes entered a failure state"
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    pending = ProviderRestoreResult(
                        restored_resource_ids=tuple(volume_ids),
                        status="pending",
                        message=(
                            "Replacement EBS volumes were created and remain "
                            "pending. Their IDs are recorded; verify they become "
                            "available before attaching them."
                        ),
                    )
                    if progress:
                        progress(pending)
                    return pending
                time.sleep(min(self.config.poll_interval_seconds, remaining))
        except Exception as exc:
            if isinstance(exc, SnapshotRestoreProviderError):
                raise
            raise SnapshotRestoreProviderError(
                f"AWS snapshot restore failed: {exc}",
                restored_resource_ids=tuple(volume_ids),
            ) from exc


class HetznerCloudSnapshotProvider:
    name = "hetzner_cloud"

    def __init__(self, config: HetznerSnapshotConfig, *, runner=run):
        self.config = config
        self._run = runner

    def _base(self) -> list[str]:
        args = [self.config.cli_command]
        if self.config.context:
            args.extend(["--context", self.config.context])
        return args

    def _credential_env(self) -> dict[str, str] | None:
        if self.config.context:
            return None
        token = os.getenv(self.config.token_env)
        if token:
            # hcloud only consumes HCLOUD_TOKEN. token_env selects the source
            # environment variable; it is deliberately remapped off argv.
            return {"HCLOUD_TOKEN": token}
        raise RuntimeError(
            f"Missing required environment variable: {self.config.token_env}"
        )

    def _run_hcloud(
        self,
        args: list[str],
        *,
        check: bool = True,
    ) -> CommandResult:
        return self._run(
            args,
            stream=False,
            check=check,
            env=self._credential_env(),
            unset_env=(
                ("HCLOUD_TOKEN",)
                if self.config.context
                else ()
            ),
        )

    def _probe(self) -> None:
        self._run_hcloud([*self._base(), "version"])

    @staticmethod
    def _json_list(result: CommandResult, operation: str) -> list[dict]:
        try:
            data = json.loads(result.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"hcloud returned invalid JSON for {operation}"
            ) from exc
        if not isinstance(data, list) or not all(
            isinstance(item, dict) for item in data
        ):
            raise RuntimeError(f"hcloud returned an invalid result for {operation}")
        return data

    @staticmethod
    def _json_object(result: CommandResult, operation: str) -> dict:
        try:
            data = json.loads(result.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"hcloud returned invalid JSON for {operation}"
            ) from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"hcloud returned an invalid result for {operation}")
        return data

    @staticmethod
    def _nested_name(value: object) -> str | None:
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            for key in ("name", "id"):
                if value.get(key) is not None:
                    return str(value[key])
        return None

    def _provider_snapshot(
        self,
        *,
        server: dict,
        image: dict,
    ) -> ProviderSnapshot:
        source_id = str(server.get("id") or self.config.server)
        source_status = str(server.get("status") or "unknown").lower()
        consistency = (
            "powered_off_consistent"
            if source_status in {"off", "powered_off"}
            else "live_unverified"
        )
        image_status = str(image.get("status") or "creating").lower()
        status = "complete" if image_status == "available" else "pending"
        server_type = self._nested_name(server.get("server_type"))
        location = self._nested_name(server.get("location"))
        if not location:
            datacenter = server.get("datacenter")
            if isinstance(datacenter, dict):
                location = self._nested_name(datacenter.get("location"))
        architecture = (
            str(server["architecture"])
            if server.get("architecture")
            else None
        )
        if architecture is None and isinstance(server.get("server_type"), dict):
            raw = server["server_type"].get("architecture")
            architecture = str(raw) if raw else None

        metadata: dict[str, str | int | float | bool | None] = {
            "source_server_id": source_id,
            "source_server_name": (
                str(server["name"]) if server.get("name") else None
            ),
            "source_power_status": source_status,
            "source_server_type": server_type,
            "source_location": location,
            "source_architecture": architecture,
            "network_reconstruction_required": True,
        }
        context = self.config.context or "environment-credentials"
        return ProviderSnapshot(
            source_resource_id=source_id,
            resources=(
                SnapshotResource(
                    snapshot_resource_id=str(image["id"]),
                    source_resource_id=source_id,
                    kind="server_root_disk",
                    state=image_status,
                    location=location,
                    size_gib=(
                        int(image["disk_size"])
                        if image.get("disk_size") is not None
                        else None
                    ),
                    root_device=True,
                ),
            ),
            scope=("hetzner_server_local_root_disk",),
            consistency=consistency,
            recovery_notes=(
                "Hetzner does not guarantee consistency for a running-server "
                "snapshot; only a source observed as powered off is labelled "
                "powered-off consistent.",
                "Hetzner server snapshots exclude separately attached Volumes.",
                "Recovery creates a stopped isolated server without public IPs; "
                "networking, firewalls, Primary IPs, and placement must be "
                "reconstructed from IaC.",
            ),
            status=status,
            provider_scope={"context": context},
            provider_metadata=metadata,
        )

    @staticmethod
    def _image_source_id(image: dict) -> str | None:
        created_from = image.get("created_from")
        if isinstance(created_from, dict) and created_from.get("id") is not None:
            return str(created_from["id"])
        return None

    def _assert_image_source(
        self,
        *,
        image: dict,
        source_server_id: str,
    ) -> None:
        created_from_id = self._image_source_id(image)
        if created_from_id != source_server_id:
            raise RuntimeError(
                f"Hetzner image {image.get('id')!r} records source server "
                f"{created_from_id!r}, not the canonical source "
                f"{source_server_id!r}"
            )

    def create(self, request: SnapshotCreateRequest) -> ProviderSnapshot:
        self._probe()
        described = self._run_hcloud(
            [
                *self._base(),
                "server",
                "describe",
                self.config.server,
                "--output",
                "json",
            ]
        )
        server = self._json_object(described, "server describe")
        if server.get("id") is None:
            raise RuntimeError(
                "hcloud did not return a canonical source server id"
            )
        source_server_id = str(server["id"])
        volumes = server.get("volumes")
        if not isinstance(volumes, list):
            raise RuntimeError(
                "hcloud did not return a complete attached-volume list"
            )
        if volumes:
            raise RuntimeError(
                "Hetzner server snapshots exclude attached Volumes; refusing to "
                "create an incomplete Odoo DR snapshot. Use portable backups or "
                "AWS EBS multi-volume snapshots for this topology."
            )
        if request.progress:
            requested = self._provider_snapshot(
                server=server,
                image={"id": "requested", "status": "creating"},
            )
            request.progress(
                replace(
                    requested,
                    resources=(
                        requested.resources[0].model_copy(
                            update={
                                "snapshot_resource_id": None,
                                "state": "requested",
                            }
                        ),
                    ),
                    status="requested",
                )
            )

        marker = hashlib.sha256(request.snapshot_id.encode()).hexdigest()[:20]
        label = f"odooctl-marker={marker}"
        create_result: CommandResult | None = None
        create_error: Exception | None = None
        try:
            create_result = self._run_hcloud(
                [
                    *self._base(),
                    "server",
                    "create-image",
                    "--type",
                    "snapshot",
                    "--description",
                    request.description[:255],
                    "--label",
                    label,
                    source_server_id,
                ],
                check=False,
            )
        except Exception as exc:
            # The request may have reached Hetzner even when the local
            # transport failed. Continue with label-based discovery.
            create_error = exc
        # create-image has no JSON output or client token. Whether it succeeds
        # or the transport becomes uncertain, reconcile by the unique label.
        list_command = [
            *self._base(),
            "image",
            "list",
            "--type",
            "snapshot",
            "--selector",
            label,
            "--output",
            "json",
        ]
        discovery_deadline = time.monotonic() + min(
            self.config.completion_timeout_seconds,
            60,
        )
        images: list[dict] = []
        while True:
            listed = self._run_hcloud(list_command)
            images = self._json_list(listed, "image list")
            if images:
                break
            remaining = discovery_deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(self.config.poll_interval_seconds, remaining))
        if len(images) != 1 or not images[0].get("id"):
            if create_error is not None:
                detail = f" ({create_error})"
            elif create_result is not None and create_result.returncode != 0:
                detail = (
                    f" (create-image exited {create_result.returncode})"
                )
            else:
                detail = ""
            raise RuntimeError(
                "hcloud could not identify the uniquely labelled server "
                f"snapshot{detail}"
            )

        image = images[0]
        deadline = time.monotonic() + self.config.completion_timeout_seconds
        while True:
            try:
                self._assert_image_source(
                    image=image,
                    source_server_id=source_server_id,
                )
            except Exception as exc:
                failed = replace(
                    self._provider_snapshot(server=server, image=image),
                    status="failed",
                )
                raise SnapshotCreateProviderError(
                    f"Hetzner snapshot source verification failed: {exc}",
                    snapshot=failed,
                ) from exc
            snapshot = self._provider_snapshot(server=server, image=image)
            if request.progress:
                request.progress(snapshot)
            if snapshot.status == "complete":
                return snapshot
            image_status = str(image.get("status") or "creating").lower()
            if image_status not in {"creating", "pending"}:
                failed = replace(snapshot, status="failed")
                raise SnapshotCreateProviderError(
                    f"Hetzner image entered unexpected state {image_status!r}",
                    snapshot=failed,
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return snapshot
            time.sleep(min(self.config.poll_interval_seconds, remaining))
            image_result = self._run_hcloud(
                [
                    *self._base(),
                    "image",
                    "describe",
                    str(image["id"]),
                    "--output",
                    "json",
                ]
            )
            image = self._json_object(image_result, "image describe")

    def reconcile(self, manifest: SnapshotManifest) -> ProviderSnapshot:
        self._probe()
        provider_image_id = next(
            (
                str(resource.snapshot_resource_id)
                for resource in manifest.resources
                if resource.snapshot_resource_id
            ),
            None,
        )
        if provider_image_id:
            image_result = self._run_hcloud(
                [
                    *self._base(),
                    "image",
                    "describe",
                    provider_image_id,
                    "--output",
                    "json",
                ]
            )
            image = self._json_object(image_result, "image describe")
            canonical_source = manifest.provider_metadata.get(
                "source_server_id"
            )
            if (
                canonical_source
                and str(canonical_source) != manifest.source_resource_id
            ):
                raise RuntimeError(
                    f"Snapshot {manifest.snapshot_id} has inconsistent canonical "
                    "Hetzner source-server metadata"
                )
            server = {
                "id": manifest.source_resource_id,
                "name": manifest.provider_metadata.get("source_server_name"),
                "status": manifest.provider_metadata.get("source_power_status"),
                "server_type": manifest.provider_metadata.get(
                    "source_server_type"
                ),
                "location": manifest.provider_metadata.get("source_location"),
                "architecture": manifest.provider_metadata.get(
                    "source_architecture"
                ),
            }
        else:
            marker = hashlib.sha256(manifest.snapshot_id.encode()).hexdigest()[
                :20
            ]
            listed = self._run_hcloud(
                [
                    *self._base(),
                    "image",
                    "list",
                    "--type",
                    "snapshot",
                    "--selector",
                    f"odooctl-marker={marker}",
                    "--output",
                    "json",
                ]
            )
            images = self._json_list(listed, "image list")
            if len(images) != 1 or not images[0].get("id"):
                raise RuntimeError(
                    f"hcloud found no unique image for {manifest.snapshot_id}"
                )
            image = images[0]
            if manifest.provider_metadata.get("source_server_id"):
                server = {
                    "id": manifest.source_resource_id,
                    "name": manifest.provider_metadata.get(
                        "source_server_name"
                    ),
                    "status": manifest.provider_metadata.get(
                        "source_power_status"
                    ),
                    "server_type": manifest.provider_metadata.get(
                        "source_server_type"
                    ),
                    "location": manifest.provider_metadata.get(
                        "source_location"
                    ),
                    "architecture": manifest.provider_metadata.get(
                        "source_architecture"
                    ),
                }
            else:
                described = self._run_hcloud(
                    [
                        *self._base(),
                        "server",
                        "describe",
                        self.config.server,
                        "--output",
                        "json",
                    ]
                )
                server = self._json_object(described, "server describe")
        created_from = image.get("created_from")
        created_from_id = (
            str(created_from.get("id"))
            if isinstance(created_from, dict) and created_from.get("id")
            else None
        )
        if (
            created_from_id
            and created_from_id != str(server.get("id"))
        ):
            raise RuntimeError(
                f"Hetzner image source {created_from_id} does not match "
                f"snapshot source {server.get('id')}"
            )
        refreshed = self._provider_snapshot(server=server, image=image)
        return ProviderSnapshot(
            source_resource_id=manifest.source_resource_id,
            resources=refreshed.resources,
            scope=tuple(manifest.scope),
            consistency=manifest.consistency,
            recovery_notes=tuple(manifest.recovery_notes),
            status=refreshed.status,
            provider_scope=(
                dict(manifest.provider_scope)
                if manifest.provider_scope
                else dict(refreshed.provider_scope)
            ),
            provider_metadata=(
                dict(manifest.provider_metadata)
                if manifest.provider_metadata
                else dict(refreshed.provider_metadata)
            ),
        )

    def plan_restore(self, manifest: SnapshotManifest) -> SnapshotRestorePlan:
        if manifest.provider != self.name:
            raise RuntimeError(
                f"Snapshot {manifest.snapshot_id} belongs to {manifest.provider}, "
                f"not {self.name}"
            )
        if manifest.status != "complete":
            raise RuntimeError(
                f"Snapshot {manifest.snapshot_id} is {manifest.status}, not complete"
            )
        if (
            len(manifest.resources) != 1
            or manifest.resources[0].state != "available"
            or not manifest.resources[0].snapshot_resource_id
        ):
            raise RuntimeError(
                f"Snapshot {manifest.snapshot_id} does not have an available "
                "Hetzner image"
            )
        image_id = manifest.resources[0].snapshot_resource_id
        recovery_marker = hashlib.sha256(manifest.snapshot_id.encode()).hexdigest()[
            :12
        ]
        recovery_name = f"odooctl-recovery-{recovery_marker}"
        command = (
            *self._base(),
            "server",
            "create",
            "--name",
            recovery_name,
            "--type",
            self.config.recovery_server_type,
            "--image",
            image_id,
            "--location",
            self.config.recovery_location,
            "--network",
            self.config.recovery_network,
            "--start-after-create=false",
            "--without-ipv4",
            "--without-ipv6",
            "--label",
            f"odooctl-restored-from={recovery_marker}",
            "--output",
            "json",
        )
        return SnapshotRestorePlan(
            provider=self.name,
            snapshot_id=manifest.snapshot_id,
            source_resource_id=manifest.source_resource_id,
            commands=(tuple(command),),
            destructive=False,
            notes=(
                "This creates a stopped recovery server and never rebuilds the "
                "configured source server.",
                "The recovery server has no public IPs; inspect its disk from an "
                "isolated recovery network before any cutover.",
                "Separately attached Hetzner Volumes and network configuration "
                "are outside this snapshot.",
            ),
        )

    def _reconcile_recovery_server(
        self,
        marker_label: str,
    ) -> dict | None:
        listed = self._run_hcloud(
            [
                *self._base(),
                "server",
                "list",
                "--selector",
                marker_label,
                "--output",
                "json",
            ]
        )
        servers = self._json_list(listed, "server list")
        if len(servers) == 1:
            return servers[0]
        return None

    def _reconcile_recovery_server_bounded(
        self,
        marker_label: str,
    ) -> dict | None:
        deadline = time.monotonic() + min(
            self.config.completion_timeout_seconds,
            60,
        )
        while True:
            try:
                server = self._reconcile_recovery_server(marker_label)
                if server is not None:
                    return server
            except Exception:
                pass
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            time.sleep(min(self.config.poll_interval_seconds, remaining))

    def _recovery_network_id(self) -> str:
        result = self._run_hcloud(
            [
                *self._base(),
                "network",
                "describe",
                self.config.recovery_network,
                "--output",
                "json",
            ]
        )
        network = self._json_object(result, "network describe")
        if not network.get("id"):
            raise RuntimeError(
                "hcloud did not return the configured recovery network id"
            )
        return str(network["id"])

    @staticmethod
    def _public_address_present(value: object) -> bool:
        if value in (None, False, "", {}):
            return False
        if isinstance(value, dict):
            return bool(value.get("ip") or value.get("enabled"))
        return True

    def _validate_recovery_server(
        self,
        *,
        resource_id: str,
        image_id: str,
        network_id: str,
    ) -> dict:
        result = self._run_hcloud(
            [
                *self._base(),
                "server",
                "describe",
                resource_id,
                "--output",
                "json",
            ]
        )
        server = self._json_object(result, "recovery server describe")
        if str(server.get("id") or "") != resource_id:
            raise RuntimeError(
                "hcloud recovery-server identity does not match the created id"
            )
        if str(server.get("status") or "").lower() != "off":
            raise RuntimeError(
                f"Hetzner recovery server {resource_id} is not powered off"
            )
        server_image = server.get("image")
        server_image_id = (
            str(server_image.get("id"))
            if isinstance(server_image, dict) and server_image.get("id")
            else str(server_image) if server_image is not None else ""
        )
        if server_image_id != image_id:
            raise RuntimeError(
                f"Hetzner recovery server {resource_id} was not created from "
                f"snapshot image {image_id}"
            )
        public_net = server.get("public_net")
        if not isinstance(public_net, dict) or any(
            self._public_address_present(public_net.get(version))
            for version in ("ipv4", "ipv6")
        ):
            raise RuntimeError(
                f"Hetzner recovery server {resource_id} has an unexpected "
                "public network address"
            )
        private_net = server.get("private_net")
        if not isinstance(private_net, list):
            raise RuntimeError(
                f"Hetzner recovery server {resource_id} has no private network"
            )
        attached_networks = {
            str(
                item["network"].get("id")
                if isinstance(item.get("network"), dict)
                else item.get("network")
            )
            for item in private_net
            if isinstance(item, dict) and item.get("network") is not None
        }
        if network_id not in attached_networks:
            raise RuntimeError(
                f"Hetzner recovery server {resource_id} is not attached to "
                "the configured recovery network"
            )
        return server

    def restore(
        self,
        manifest: SnapshotManifest,
        *,
        progress: Callable[[ProviderRestoreResult], None] | None = None,
    ) -> ProviderRestoreResult:
        self._probe()
        plan = self.plan_restore(manifest)
        image_id = manifest.resources[0].snapshot_resource_id
        image_result = self._run_hcloud(
            [
                *self._base(),
                "image",
                "describe",
                image_id,
                "--output",
                "json",
            ]
        )
        image = self._json_object(image_result, "image describe")
        returned_image_id = image.get("id")
        if returned_image_id is None or str(returned_image_id) != str(image_id):
            raise RuntimeError(
                f"hcloud returned image {returned_image_id}, not {image_id}"
            )
        image_type = str(image.get("type") or "").lower()
        if image_type != "snapshot":
            raise RuntimeError(
                f"Hetzner image {image_id} is type {image_type!r}, not snapshot"
            )
        if str(image.get("status") or "").lower() != "available":
            raise RuntimeError(
                f"Hetzner snapshot image {image_id} is not available"
            )
        self._assert_image_source(
            image=image,
            source_server_id=manifest.source_resource_id,
        )
        network_id = self._recovery_network_id()

        command = list(plan.commands[0])
        marker_label = command[command.index("--label") + 1]
        result: CommandResult | None = None
        create_error: Exception | None = None
        try:
            result = self._run_hcloud(command, check=False)
        except Exception as exc:
            create_error = exc
        server: dict | None = None
        if result is not None and result.returncode == 0:
            try:
                data = self._json_object(result, "server create")
                raw_server = data.get("server", data)
                if isinstance(raw_server, dict):
                    server = raw_server
            except RuntimeError:
                server = None
        if server is None:
            server = self._reconcile_recovery_server_bounded(marker_label)
        if server is None:
            detail = f": {create_error}" if create_error is not None else ""
            raise SnapshotRestoreProviderError(
                "hcloud did not return or reconcile the recovery server"
                + detail
            )
        resource_id = server.get("id") or server.get("name")
        if not resource_id:
            raise SnapshotRestoreProviderError(
                "hcloud did not return a recovery server id"
            )
        resource_id = str(resource_id)
        pending = ProviderRestoreResult(
            restored_resource_ids=(resource_id,),
            message=(
                "Hetzner recovery server exists; validating its image, power, "
                "and network isolation."
            ),
            status="pending",
        )
        if progress:
            progress(pending)
        try:
            self._validate_recovery_server(
                resource_id=resource_id,
                image_id=str(image_id),
                network_id=network_id,
            )
        except Exception as exc:
            raise SnapshotRestoreProviderError(
                f"Hetzner recovery server {resource_id} failed safety "
                f"validation: {exc}",
                restored_resource_ids=(resource_id,),
            ) from exc
        completed = ProviderRestoreResult(
            restored_resource_ids=(resource_id,),
            message=(
                "A stopped Hetzner recovery server was created without public "
                "IPs; inspect it before any manual cutover."
            ),
        )
        if progress:
            progress(completed)
        return completed


def make_snapshot_provider(config: SnapshotsConfig) -> SnapshotProvider:
    if config.provider == "none":
        return NoSnapshotProvider()
    if config.provider == "aws_ebs":
        if config.aws_ebs is None:
            raise RuntimeError("Missing snapshots.aws_ebs configuration")
        return AwsEbsSnapshotProvider(config.aws_ebs)
    if config.provider == "hetzner_cloud":
        if config.hetzner_cloud is None:
            raise RuntimeError("Missing snapshots.hetzner_cloud configuration")
        return HetznerCloudSnapshotProvider(config.hetzner_cloud)
    raise RuntimeError(f"Unsupported snapshot provider: {config.provider}")
