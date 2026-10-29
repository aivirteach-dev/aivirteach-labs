from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from .models import GatewayObservation, ProgressEvent, ProgressTarget


@dataclass(frozen=True)
class Frontier:
    checkpoint_id: str | None
    due: bool
    unknown_streak: int = 0


@dataclass(frozen=True)
class PendingEvent:
    event: ProgressEvent
    attempts: int


class ProgressStore:
    def __init__(
        self,
        path: Path,
        *,
        now: Callable[[], float] = time.time,
        new_id: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self._path = path
        self._now = now
        self._new_id = new_id
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(path.parent, 0o700)
        except PermissionError:
            pass
        self._connection = sqlite3.connect(path, timeout=5, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        if path.exists():
            try:
                os.chmod(path, 0o600)
            except PermissionError:
                pass
        self._configure()
        self._migrate()

    def close(self) -> None:
        self._connection.close()

    def _configure(self) -> None:
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA busy_timeout=5000")

    def _migrate(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS checkpoint_state (
                target_id TEXT NOT NULL,
                target_revision INTEGER NOT NULL,
                vm_instance_id TEXT NOT NULL,
                checkpoint_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                lab_id TEXT NOT NULL,
                course_id TEXT NOT NULL,
                runtime_course_id TEXT NOT NULL,
                course_version INTEGER NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('passed','failed','unknown')),
                achieved INTEGER NOT NULL CHECK (achieved IN (0,1)),
                observed_at TEXT NOT NULL,
                evidence_hash TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                unknown_streak INTEGER NOT NULL,
                next_probe_at REAL NOT NULL,
                last_event_at REAL NOT NULL,
                PRIMARY KEY (target_id, target_revision, vm_instance_id, checkpoint_id)
            );

            CREATE TABLE IF NOT EXISTS outbox (
                event_id TEXT PRIMARY KEY,
                target_id TEXT NOT NULL,
                target_revision INTEGER NOT NULL,
                vm_instance_id TEXT NOT NULL,
                checkpoint_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL,
                delivered_at REAL,
                dead_at REAL,
                last_error TEXT,
                created_at REAL NOT NULL,
                UNIQUE (target_id, target_revision, vm_instance_id, checkpoint_id, sequence)
            );

            CREATE INDEX IF NOT EXISTS outbox_due_idx
              ON outbox (next_attempt_at, created_at)
              WHERE delivered_at IS NULL AND dead_at IS NULL;
            """
        )
        self._connection.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(1, ?)",
            (self._now(),),
        )

    def frontier(self, target: ProgressTarget, now: float | None = None) -> Frontier:
        current_time = self._now() if now is None else now
        rows = self._connection.execute(
            """
            SELECT checkpoint_id, achieved, next_probe_at, unknown_streak
            FROM checkpoint_state
            WHERE target_id=? AND target_revision=? AND vm_instance_id=?
            """,
            (target.target_id, target.target_revision, target.vm_instance_id),
        ).fetchall()
        by_checkpoint = {row["checkpoint_id"]: row for row in rows}
        for checkpoint_id in target.checkpoints:
            row = by_checkpoint.get(checkpoint_id)
            if row is None:
                return Frontier(checkpoint_id, True)
            if not bool(row["achieved"]):
                return Frontier(
                    checkpoint_id,
                    float(row["next_probe_at"]) <= current_time,
                    int(row["unknown_streak"]),
                )
        return Frontier(None, False)

    def record_observation(
        self,
        *,
        worker_id: str,
        target: ProgressTarget,
        observation: GatewayObservation,
        observed_at: datetime,
        poll_seconds: float,
        unknown_backoff_max_seconds: int,
        heartbeat_seconds: int,
    ) -> str | None:
        now = self._now()
        key = (
            target.target_id,
            target.target_revision,
            target.vm_instance_id,
            observation.checkpoint_id,
        )
        evidence = {
            "evidence_type": observation.evidence_type,
            "summary": observation.summary,
            "facts": observation.facts,
        }
        evidence_json = _canonical_json(evidence)
        evidence_hash = hashlib.sha256(evidence_json.encode()).hexdigest()
        observed_iso = _iso(observed_at)
        ordinal = target.checkpoints.index(observation.checkpoint_id) + 1

        self._connection.execute("BEGIN IMMEDIATE")
        try:
            previous = self._connection.execute(
                """
                SELECT * FROM checkpoint_state
                WHERE target_id=? AND target_revision=? AND vm_instance_id=?
                  AND checkpoint_id=?
                """,
                key,
            ).fetchone()
            previous_sequence = int(previous["sequence"]) if previous else 0
            previous_achieved = bool(previous["achieved"]) if previous else False
            previous_state = str(previous["state"]) if previous else None
            last_event_at = float(previous["last_event_at"]) if previous else 0.0
            has_pending_event = bool(
                previous
                and self._connection.execute(
                    """
                    SELECT 1 FROM outbox
                    WHERE target_id=? AND target_revision=? AND vm_instance_id=?
                      AND checkpoint_id=? AND delivered_at IS NULL AND dead_at IS NULL
                    LIMIT 1
                    """,
                    key,
                ).fetchone()
            )
            unknown_streak = (
                int(previous["unknown_streak"]) + 1
                if observation.state == "unknown" and previous_state == "unknown"
                else (1 if observation.state == "unknown" else 0)
            )
            if observation.state == "unknown":
                delay = min(
                    unknown_backoff_max_seconds,
                    poll_seconds * (2 ** min(unknown_streak - 1, 10)),
                )
            elif observation.state == "failed":
                delay = poll_seconds
            else:
                delay = 0.0

            should_enqueue = (
                previous is None
                or previous_state != observation.state
                or (
                    now - last_event_at >= heartbeat_seconds
                    and not has_pending_event
                )
            )
            sequence = previous_sequence + (1 if should_enqueue else 0)
            achieved = previous_achieved or observation.state == "passed"
            event_id: str | None = None
            if should_enqueue:
                event_id = self._new_id()
                event = _event(
                    event_id=event_id,
                    worker_id=worker_id,
                    target=target,
                    checkpoint_id=observation.checkpoint_id,
                    sequence=sequence,
                    state=observation.state,
                    observed_at=observed_at,
                    evidence=evidence,
                )
                self._connection.execute(
                    """
                    INSERT INTO outbox(
                      event_id,target_id,target_revision,vm_instance_id,
                      checkpoint_id,sequence,payload_json,next_attempt_at,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        event_id,
                        target.target_id,
                        target.target_revision,
                        target.vm_instance_id,
                        observation.checkpoint_id,
                        sequence,
                        _canonical_json(event.model_dump(mode="json")),
                        now,
                        now,
                    ),
                )
                last_event_at = now

            self._connection.execute(
                """
                INSERT INTO checkpoint_state(
                  target_id,target_revision,vm_instance_id,checkpoint_id,ordinal,
                  lab_id,course_id,runtime_course_id,course_version,state,achieved,
                  observed_at,evidence_hash,evidence_json,sequence,unknown_streak,
                  next_probe_at,last_event_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(target_id,target_revision,vm_instance_id,checkpoint_id)
                DO UPDATE SET
                  ordinal=excluded.ordinal, lab_id=excluded.lab_id,
                  course_id=excluded.course_id,
                  runtime_course_id=excluded.runtime_course_id,
                  course_version=excluded.course_version, state=excluded.state,
                  achieved=excluded.achieved, observed_at=excluded.observed_at,
                  evidence_hash=excluded.evidence_hash,
                  evidence_json=excluded.evidence_json, sequence=excluded.sequence,
                  unknown_streak=excluded.unknown_streak,
                  next_probe_at=excluded.next_probe_at,
                  last_event_at=excluded.last_event_at
                """,
                (
                    *key,
                    ordinal,
                    target.lab_id,
                    target.course_id,
                    target.runtime_course_id,
                    target.course_version,
                    observation.state,
                    int(achieved),
                    observed_iso,
                    evidence_hash,
                    evidence_json,
                    sequence,
                    unknown_streak,
                    now + delay,
                    last_event_at,
                ),
            )
            self._connection.execute("COMMIT")
            return event_id
        except Exception:
            self._connection.execute("ROLLBACK")
            raise

    def due_events(self, *, limit: int, now: float | None = None) -> list[PendingEvent]:
        current_time = self._now() if now is None else now
        rows = self._connection.execute(
            """
            SELECT payload_json, attempts FROM outbox
            WHERE delivered_at IS NULL AND dead_at IS NULL AND next_attempt_at <= ?
            ORDER BY created_at, event_id LIMIT ?
            """,
            (current_time, limit),
        ).fetchall()
        return [
            PendingEvent(ProgressEvent.model_validate_json(row["payload_json"]), int(row["attempts"]))
            for row in rows
        ]

    def mark_delivered(self, event_ids: set[str]) -> None:
        if not event_ids:
            return
        now = self._now()
        self._connection.executemany(
            "UPDATE outbox SET delivered_at=?, last_error=NULL WHERE event_id=?",
            [(now, event_id) for event_id in event_ids],
        )

    def mark_retry(self, events: list[PendingEvent], *, error: str, delay: float) -> None:
        if not events:
            return
        next_attempt = self._now() + max(0.1, delay)
        safe_error = error[:256]
        self._connection.executemany(
            """
            UPDATE outbox
            SET attempts=attempts+1, next_attempt_at=?, last_error=?
            WHERE event_id=? AND delivered_at IS NULL AND dead_at IS NULL
            """,
            [(next_attempt, safe_error, item.event.event_id) for item in events],
        )

    def mark_dead(self, events: list[PendingEvent], *, error: str) -> None:
        if not events:
            return
        now = self._now()
        self._connection.executemany(
            """
            UPDATE outbox SET attempts=attempts+1, dead_at=?, last_error=?
            WHERE event_id=? AND delivered_at IS NULL
            """,
            [(now, error[:256], item.event.event_id) for item in events],
        )

    def outbox_status(self, event_id: str) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT * FROM outbox WHERE event_id=?", (event_id,)
        ).fetchone()


def _event(
    *,
    event_id: str,
    worker_id: str,
    target: ProgressTarget,
    checkpoint_id: str,
    sequence: int,
    state: str,
    observed_at: datetime,
    evidence: dict[str, object],
) -> ProgressEvent:
    return ProgressEvent(
        event_id=event_id,
        worker_id=worker_id,
        target_id=target.target_id,
        target_revision=target.target_revision,
        lab_id=target.lab_id,
        vm_instance_id=target.vm_instance_id,
        course_id=target.course_id,
        runtime_course_id=target.runtime_course_id,
        course_version=target.course_version,
        checkpoint_id=checkpoint_id,
        sequence=sequence,
        state=state,
        observed_at=observed_at,
        evidence_type=str(evidence["evidence_type"]),
        evidence_summary=str(evidence["summary"]),
        facts=evidence["facts"],
        checker_schema_version=1,
    )


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
