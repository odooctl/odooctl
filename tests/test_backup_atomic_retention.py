from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from odooctl.metadata.models import BackupManifest
from odooctl.metadata.store import MetadataStore
from odooctl.services import backup as backup_svc


def _service_context(tmp_path: Path):
    environment = SimpleNamespace(
        db_name="production_db",
        filestore_path="./filestore",
        filestore_volume=None,
    )
    config = SimpleNamespace(
        project=SimpleNamespace(name="demo", odoo_version="19.0"),
        runtime=SimpleNamespace(execution_mode="host"),
        postgres=SimpleNamespace(),
        odoo=SimpleNamespace(image="odoo:19"),
        backups=SimpleNamespace(
            remote=None,
            retention=SimpleNamespace(daily=7, weekly=4, monthly=6),
        ),
        env=lambda name: environment,
    )
    project = SimpleNamespace(
        config=config,
        root=tmp_path,
        backups_dir=tmp_path / "backups",
        state_dir=tmp_path / ".odooctl",
        odoo_config_path=tmp_path / "missing-odoo.conf",
        resolve_path=lambda value: (tmp_path / value).resolve(),
    )
    return SimpleNamespace(project=project)


def _install_backup_fakes(monkeypatch, *, archive: bool = True) -> None:
    class FakePostgres:
        def __init__(self, config):
            pass

        def dump(self, database, path):
            assert Path(path).parent.name.startswith(".partial-")
            Path(path).write_bytes(b"database dump")

    class FakeFilestore:
        def archive(self, source, path):
            assert Path(path).parent.name.startswith(".partial-")
            if archive:
                Path(path).write_bytes(b"filestore archive")

    monkeypatch.setattr(backup_svc, "PostgresAdapter", FakePostgres)
    monkeypatch.setattr(backup_svc, "FilestoreAdapter", FakeFilestore)
    monkeypatch.setattr(
        backup_svc,
        "git_commit",
        lambda cwd=None: "abc123",
    )


def _published_backup(
    root: Path,
    *,
    environment: str,
    timestamp: str,
    suffix: str,
    project: str = "demo",
) -> tuple[Path, BackupManifest]:
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    backup_id = (
        f"{environment}_{parsed.astimezone(timezone.utc):%Y-%m-%d_%H%M%S}"
        f"_{suffix}"
    )
    path = root / backup_id
    path.mkdir(parents=True)
    manifest = BackupManifest(
        backup_id=backup_id,
        project=project,
        environment=environment,
        timestamp=timestamp,
        db_name=f"{environment}_db",
        odoo_version="19.0",
        checksums={"db_dump": "db", "filestore": "fs"},
    )
    (path / "manifest.json").write_text(
        manifest.model_dump_json(indent=2)
    )
    return path, manifest


def test_run_backup_publishes_only_after_staged_artifacts_validate(
    tmp_path,
    monkeypatch,
):
    ctx = _service_context(tmp_path)
    _install_backup_fakes(monkeypatch)

    result = backup_svc.run_backup(ctx, "production")

    published = ctx.project.backups_dir / result.backup_id
    assert published.is_dir()
    assert not list(ctx.project.backups_dir.glob(".partial-*"))
    verified = backup_svc.verify_backup(
        ctx.project.backups_dir,
        result.backup_id,
    )
    assert verified.ok is True
    latest = MetadataStore(ctx.project.state_dir).latest_backup(
        "production"
    )
    assert latest is not None
    assert latest["backup_id"] == result.backup_id


def test_failed_staging_never_publishes_or_updates_latest(
    tmp_path,
    monkeypatch,
):
    ctx = _service_context(tmp_path)
    _install_backup_fakes(monkeypatch, archive=False)

    with pytest.raises(FileNotFoundError):
        backup_svc.run_backup(ctx, "production")

    assert not list(ctx.project.backups_dir.glob("production_*"))
    assert not list(ctx.project.backups_dir.glob(".partial-*"))
    assert (
        MetadataStore(ctx.project.state_dir).latest_backup("production")
        is None
    )


def test_same_second_staging_reservations_are_unique(tmp_path):
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    created_at = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)

    def reserve():
        return backup_svc._reserve_backup_staging(
            backup_root,
            "production",
            created_at=created_at,
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        reservations = list(executor.map(lambda _: reserve(), range(16)))

    backup_ids = [item[0] for item in reservations]
    assert len(set(backup_ids)) == len(backup_ids)
    base_id = "production_2026-07-30_120000"
    assert all(backup_id.startswith(f"{base_id}_") for backup_id in backup_ids)
    assert all(len(backup_id.removeprefix(f"{base_id}_")) == 24 for backup_id in backup_ids)
    for _, staging_path, _ in reservations:
        backup_svc._remove_tree(staging_path)


def test_same_second_reservations_are_unique_across_independent_hosts(tmp_path):
    created_at = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
    roots = [tmp_path / "host-a", tmp_path / "host-b"]
    for root in roots:
        root.mkdir()

    reservations = [
        backup_svc._reserve_backup_staging(
            root,
            "production",
            created_at=created_at,
        )
        for root in roots
    ]

    assert reservations[0][0] != reservations[1][0]
    assert all(
        backup_id.startswith("production_2026-07-30_120000_")
        for backup_id, _, _ in reservations
    )


def test_gfs_selection_and_pruning_use_utc_periods_and_sync_metadata(
    tmp_path,
):
    backup_root = tmp_path / "backups"
    store = MetadataStore(tmp_path / ".odooctl")
    fixtures = [
        ("2026-07-30T18:00:00Z", "jul30-late"),
        ("2026-07-30T09:00:00Z", "jul30-early"),
        ("2026-07-29T12:00:00Z", "jul29"),
        ("2026-07-20T12:00:00Z", "jul20"),
        ("2026-07-05T12:00:00Z", "jul05"),
        ("2026-06-30T23:00:00Z", "jun30"),
        ("2026-06-15T12:00:00Z", "jun15"),
        ("2026-05-31T12:00:00Z", "may31"),
    ]
    by_suffix: dict[str, Path] = {}
    manifests: list[BackupManifest] = []
    for timestamp, suffix in fixtures:
        path, manifest = _published_backup(
            backup_root,
            environment="production",
            timestamp=timestamp,
            suffix=suffix,
        )
        by_suffix[suffix] = path
        manifests.append(manifest)
        store.save_backup_manifest(manifest.backup_id, manifest)

    other_path, other_manifest = _published_backup(
        backup_root,
        environment="staging",
        timestamp="2026-01-01T00:00:00Z",
        suffix="other-env",
    )
    store.save_backup_manifest(
        other_manifest.backup_id,
        other_manifest,
    )
    partial = backup_root / ".partial-production_incomplete"
    partial.mkdir()
    (partial / "manifest.json").write_text(
        manifests[0].model_dump_json()
    )

    selected = backup_svc.select_gfs_backups(
        backup_root,
        environment="production",
        daily=2,
        weekly=2,
        monthly=3,
    )
    expected_suffixes = {
        "jul30-late",
        "jul29",
        "jul20",
        "jun30",
        "may31",
    }
    assert set(selected) == {
        by_suffix[suffix] for suffix in expected_suffixes
    }

    removed = backup_svc.prune_backups_gfs(
        backup_root,
        environment="production",
        daily=2,
        weekly=2,
        monthly=3,
        project="demo",
        metadata_store=store,
    )

    assert set(removed) == {
        by_suffix["jul30-early"],
        by_suffix["jul05"],
        by_suffix["jun15"],
    }
    assert partial.exists()
    assert other_path.exists()
    for path in selected:
        assert path.exists()
        assert (
            store.root / "backups" / f"{path.name}.json"
        ).exists()
    for path in removed:
        assert not path.exists()
        assert not (
            store.root / "backups" / f"{path.name}.json"
        ).exists()
    latest = store.latest_backup("production")
    assert latest is not None
    assert latest["backup_id"] == by_suffix["jul30-late"].name
    assert (
        store.latest_backup("staging")["backup_id"]
        == other_manifest.backup_id
    )


def test_local_retention_grace_prevents_cross_publisher_mutual_deletion(
    tmp_path,
):
    backup_root = tmp_path / "backups"
    first, first_manifest = _published_backup(
        backup_root,
        environment="production",
        timestamp="2026-07-30T12:00:00Z",
        suffix="publisher-a",
    )
    second, second_manifest = _published_backup(
        backup_root,
        environment="production",
        timestamp="2026-07-30T12:00:01Z",
        suffix="publisher-b",
    )
    now = datetime.now(timezone.utc)

    first_pass = backup_svc.prune_backups_gfs(
        backup_root,
        environment="production",
        daily=0,
        weekly=0,
        monthly=0,
        project="demo",
        protected_backup_ids=(first_manifest.backup_id,),
        grace_hours=1,
        now=now,
    )
    second_pass = backup_svc.prune_backups_gfs(
        backup_root,
        environment="production",
        daily=0,
        weekly=0,
        monthly=0,
        project="demo",
        protected_backup_ids=(second_manifest.backup_id,),
        grace_hours=1,
        now=now,
    )

    assert first_pass == []
    assert second_pass == []
    assert first.exists()
    assert second.exists()

    reconciled = backup_svc.prune_backups_gfs(
        backup_root,
        environment="production",
        daily=0,
        weekly=0,
        monthly=0,
        project="demo",
        grace_hours=1,
        now=now + timedelta(hours=2),
    )

    assert reconciled == [first]
    assert not first.exists()
    assert second.exists()


def test_gfs_zero_tiers_still_keep_newest_restore_point(tmp_path):
    backup_root = tmp_path / "backups"
    old, _ = _published_backup(
        backup_root,
        environment="production",
        timestamp="2026-07-29T12:00:00Z",
        suffix="old",
    )
    newest, _ = _published_backup(
        backup_root,
        environment="production",
        timestamp="2026-07-30T12:00:00Z",
        suffix="new",
    )

    selected = backup_svc.select_gfs_backups(
        backup_root,
        environment="production",
        daily=0,
        weekly=0,
        monthly=0,
    )

    assert selected == [newest]
    assert old not in selected


def test_gfs_pruning_never_deletes_another_projects_shared_root_backup(
    tmp_path,
):
    backup_root = tmp_path / "shared-backups"
    owned_new, _ = _published_backup(
        backup_root,
        environment="production",
        timestamp="2026-07-30T12:00:00Z",
        suffix="owned-new",
        project="demo",
    )
    owned_old, _ = _published_backup(
        backup_root,
        environment="production",
        timestamp="2026-07-28T12:00:00Z",
        suffix="owned-old",
        project="demo",
    )
    foreign, _ = _published_backup(
        backup_root,
        environment="production",
        timestamp="2026-07-29T12:00:00Z",
        suffix="foreign",
        project="another-project",
    )

    removed = backup_svc.prune_backups_gfs(
        backup_root,
        environment="production",
        daily=0,
        weekly=0,
        monthly=0,
        project="demo",
    )

    assert removed == [owned_old]
    assert owned_new.exists()
    assert not owned_old.exists()
    assert foreign.exists()
