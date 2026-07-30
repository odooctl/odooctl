from __future__ import annotations

from pathlib import Path


def test_example_config_and_docs_are_checked_in():
    root = Path(__file__).resolve().parents[1]
    example_config = root / "examples" / "odooctl.yml"
    examples_doc = root / "docs" / "examples.md"

    assert example_config.exists(), "expected example config template to be checked in"
    assert examples_doc.exists(), "expected workflow docs to be checked in"

    config_text = example_config.read_text()
    doc_text = examples_doc.read_text()

    assert "project:" in config_text
    assert "environments:" in config_text
    assert "odooctl clone production staging --sanitize" in doc_text
    assert "odooctl status --config odooctl.yml --environment production --json" in doc_text


def test_remote_backup_and_dr_docs_match_the_fail_closed_contract():
    root = Path(__file__).resolve().parents[1]
    markdown_paths = [
        root / "README.md",
        root / "CHANGELOG.md",
        *(root / "docs").rglob("*.md"),
    ]
    all_markdown = "\n".join(path.read_text() for path in markdown_paths).lower()
    for stale_claim in (
        "local mirror",
        "mirrored locally",
        ".odooctl/remote-backups/",
    ):
        assert stale_claim not in all_markdown

    backup_docs = (root / "docs" / "backup-restore.md").read_text()
    for required_text in (
        "`required`",
        "`best_effort`",
        "`disabled`",
        "`verify_after_upload`",
        "`grace_hours`",
        "`orphan_grace_hours`",
        "manual review",
        "24-hex suffix",
        "odooctl backup-remote list",
        "odooctl backup-remote verify",
        "odooctl backup-remote download",
        "EnvironmentFile=",
    ):
        assert required_text in backup_docs

    dr_docs = (root / "docs" / "disaster-recovery.md").read_text()
    for required_text in (
        "DockerComposeDrillRuntime",
        "expected_project=config.project.name",
        "prepare_runtime_fn=runtime.prepare",
        "restore_database_fn=runtime.restore_database",
        "restore_filestore_fn=runtime.restore_filestore",
        "start_runtime_fn=runtime.start",
        "stop_runtime_fn=runtime.stop",
        "Custom-addon prerequisite",
    ):
        assert required_text in dr_docs
