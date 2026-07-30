"""Durable operation, event, and audit models."""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class OperationKind(str, Enum):
    DEPLOY = "deploy"
    BACKUP = "backup"
    RESTORE = "restore"
    CLONE = "clone"
    ENV_CREATE = "env_create"
    ENV_DESTROY = "env_destroy"
    UPDATE_MODULES = "update_modules"
    ROLLBACK = "rollback"
    PROMOTE = "promote"
    DR_DRILL = "dr_drill"
    SNAPSHOT_CREATE = "snapshot_create"
    SNAPSHOT_RECONCILE = "snapshot_reconcile"
    SNAPSHOT_RESTORE = "snapshot_restore"
    PITR_BASE_CREATE = "pitr_base_create"
    PITR_RESTORE = "pitr_restore"
    PITR_CUTOVER = "pitr_cutover"
    PITR_RECONCILE = "pitr_reconcile"
    FILESTORE_MIGRATE = "filestore_migrate"
    MIGRATE_REHEARSAL = "migrate_rehearsal"


class OperationStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Operation:
    id: str
    kind: OperationKind
    project: str
    environment: str
    status: OperationStatus
    actor: str
    params_redacted: dict
    created_at: str
    updated_at: str = ""
    result_ref: str | None = None
    error: str | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "id": self.id,
                "kind": self.kind.value if isinstance(self.kind, OperationKind) else self.kind,
                "project": self.project,
                "environment": self.environment,
                "status": self.status.value if isinstance(self.status, OperationStatus) else self.status,
                "actor": self.actor,
                "params_redacted": self.params_redacted,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "result_ref": self.result_ref,
                "error": self.error,
            },
            indent=2,
        )

    @classmethod
    def from_dict(cls, d: dict) -> "Operation":
        return cls(
            id=d["id"],
            kind=OperationKind(d["kind"]),
            project=d["project"],
            environment=d["environment"],
            status=OperationStatus(d["status"]),
            actor=d["actor"],
            params_redacted=d.get("params_redacted", {}),
            created_at=d["created_at"],
            updated_at=d.get("updated_at", ""),
            result_ref=d.get("result_ref"),
            error=d.get("error"),
        )

    @classmethod
    def from_json(cls, text: str) -> "Operation":
        return cls.from_dict(json.loads(text))

    @classmethod
    def create(
        cls,
        kind: OperationKind,
        project: str,
        environment: str,
        actor: str,
        params_redacted: dict,
    ) -> "Operation":
        now = _utcnow()
        return cls(
            id=uuid.uuid4().hex[:12],
            kind=kind,
            project=project,
            environment=environment,
            status=OperationStatus.QUEUED,
            actor=actor,
            params_redacted=params_redacted,
            created_at=now,
            updated_at=now,
        )


@dataclass
class Event:
    op_id: str
    seq: int
    timestamp: str
    level: str
    phase: str
    message: str
    data: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {
                "op_id": self.op_id,
                "seq": self.seq,
                "timestamp": self.timestamp,
                "level": self.level,
                "phase": self.phase,
                "message": self.message,
                "data": self.data,
            }
        )

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        return cls(
            op_id=d["op_id"],
            seq=d["seq"],
            timestamp=d["timestamp"],
            level=d.get("level", "info"),
            phase=d.get("phase", ""),
            message=d["message"],
            data=d.get("data", {}),
        )

    @classmethod
    def from_json(cls, text: str) -> "Event":
        return cls.from_dict(json.loads(text))


@dataclass
class AuditEntry:
    actor: str
    action: str
    target: str
    params_redacted: dict
    outcome: str
    op_id: str
    timestamp: str
    prev_hash: str = ""
    current_hash: str = ""

    def to_dict(self) -> dict:
        return {
            "prev_hash": self.prev_hash,
            "actor": self.actor,
            "action": self.action,
            "target": self.target,
            "params_redacted": self.params_redacted,
            "outcome": self.outcome,
            "op_id": self.op_id,
            "timestamp": self.timestamp,
        }

    def to_json(self) -> str:
        d = self.to_dict()
        d["current_hash"] = self.current_hash
        return json.dumps(d)

    @classmethod
    def from_dict(cls, d: dict) -> "AuditEntry":
        return cls(
            prev_hash=d.get("prev_hash", ""),
            current_hash=d.get("current_hash", ""),
            actor=d["actor"],
            action=d["action"],
            target=d["target"],
            params_redacted=d.get("params_redacted", {}),
            outcome=d["outcome"],
            op_id=d.get("op_id", ""),
            timestamp=d["timestamp"],
        )
