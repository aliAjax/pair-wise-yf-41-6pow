import json
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

from .domain import ConflictError, NotFoundError
from .rules import magnitude_from_reports, merge_reports, post_event_status


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
                CREATE TABLE IF NOT EXISTS entity_versions (
                    entity_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    action TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(entity_id, version)
                );
                CREATE INDEX IF NOT EXISTS idx_entity_versions_entity
                    ON entity_versions(entity_id, version);
            """)

    @staticmethod
    def _entity_from_row(row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _insert_version(connection, entity_id, kind, status, version, data, created_by, action):
        connection.execute(
            "INSERT OR REPLACE INTO entity_versions"
            "(entity_id, version, kind, status, data, created_by, action, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entity_id,
                int(version),
                kind,
                status,
                json.dumps(data, ensure_ascii=False, sort_keys=True),
                created_by,
                action,
                utcnow(),
            ),
        )

    def create_entity(self, entity_id, kind, status, data, actor_id, action="create"):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now),
            )
            self._insert_version(connection, entity_id, kind, status, 1, data, actor_id, action)
        return self.get_entity(entity_id)

    def get_entity(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return self._entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
            ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def find_entities(self, kind, field, value):
        return [
            entity
            for entity in self.list_entities(kind=kind)
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(self, entity_id, expected_version, status, data, action="update"):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT kind, version, created_by FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            new_version = current_version + 1
            connection.execute(
                "UPDATE entities SET status = ?, version = ?, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (status, new_version, payload, now, entity_id, current_version),
            )
            self._insert_version(
                connection,
                entity_id,
                row["kind"],
                status,
                new_version,
                data,
                row["created_by"],
                action,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    def list_versions(self, entity_id):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entity_versions WHERE entity_id = ? ORDER BY version",
                (entity_id,),
            ).fetchall()
        return [
            {
                "entity_id": row["entity_id"],
                "version": int(row["version"]),
                "kind": row["kind"],
                "status": row["status"],
                "data": json.loads(row["data"]),
                "created_by": row["created_by"],
                "action": row["action"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_version(self, entity_id, version):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entity_versions WHERE entity_id = ? AND version = ?",
                (entity_id, int(version)),
            ).fetchone()
        if not row:
            return None
        return {
            "entity_id": row["entity_id"],
            "version": int(row["version"]),
            "kind": row["kind"],
            "status": row["status"],
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "action": row["action"],
            "created_at": row["created_at"],
        }

    def merge_events(self, target_id, source_id, combined_reports, magnitude, actor_id, expected_version=None):
        """Atomically absorb source event into target.

        The source is marked merged with a pointer to the target so a concurrent
        merge that loses the race can report where the event went.
        """
        now = utcnow()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            target = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (target_id,)
            ).fetchone()
            source = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (source_id,)
            ).fetchone()
            if not target:
                raise NotFoundError("entity not found: " + target_id)
            if not source:
                raise NotFoundError("entity not found: " + source_id)
            tdata = json.loads(target["data"])
            sdata = json.loads(source["data"])
            if source["status"] == "merged" or sdata.get("merged_into"):
                raise ConflictError(
                    "source event %s already merged into %s"
                    % (source_id, sdata.get("merged_into"))
                )
            if target["status"] == "merged" or tdata.get("merged_into"):
                raise ConflictError(
                    "target event %s already merged into %s"
                    % (target_id, tdata.get("merged_into"))
                )
            if expected_version is not None and int(target["version"]) != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, target["version"])
                )
            tdata["reports"] = combined_reports
            if magnitude is not None:
                tdata["magnitude"] = magnitude
            target_status = post_event_status(target["status"])
            target_version = int(target["version"]) + 1
            connection.execute(
                "UPDATE entities SET status = ?, version = ?, data = ?, updated_at = ? WHERE id = ?",
                (
                    target_status,
                    target_version,
                    json.dumps(tdata, ensure_ascii=False, sort_keys=True),
                    now,
                    target_id,
                ),
            )
            self._insert_version(
                connection, target_id, target["kind"], target_status,
                target_version, tdata, target["created_by"], "merge",
            )
            # Reports now belong to the target; the source is a merged
            # tombstone. Its pre-merge data is retained in version history.
            sdata = {"merged_into": target_id, "merged_at": now}
            source_version = int(source["version"]) + 1
            connection.execute(
                "UPDATE entities SET status = 'merged', version = ?, data = ?, updated_at = ? WHERE id = ?",
                (
                    source_version,
                    json.dumps(sdata, ensure_ascii=False, sort_keys=True),
                    now,
                    source_id,
                ),
            )
            self._insert_version(
                connection, source_id, source["kind"], "merged",
                source_version, sdata, source["created_by"], "merge",
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(target_id), self.get_entity(source_id)

    def split_event(self, event_id, keep_reports, moved_reports, target_id, new_event_data, magnitude, actor_id, expected_version=None):
        """Atomically move selected reports out of an event into another event."""
        now = utcnow()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            event = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (event_id,)
            ).fetchone()
            if not event:
                raise NotFoundError("entity not found: " + event_id)
            if event["status"] == "merged" or json.loads(event["data"]).get("merged_into"):
                raise ConflictError("event %s already merged" % event_id)
            data = json.loads(event["data"])
            data["reports"] = keep_reports
            if magnitude is not None:
                data["magnitude"] = magnitude
            event_status = post_event_status(event["status"])
            event_version = int(event["version"]) + 1
            connection.execute(
                "UPDATE entities SET status = ?, version = ?, data = ?, updated_at = ? WHERE id = ?",
                (
                    event_status,
                    event_version,
                    json.dumps(data, ensure_ascii=False, sort_keys=True),
                    now,
                    event_id,
                ),
            )
            self._insert_version(
                connection, event_id, event["kind"], event_status,
                event_version, data, event["created_by"], "split",
            )

            if target_id:
                target = connection.execute(
                    "SELECT * FROM entities WHERE id = ?", (target_id,)
                ).fetchone()
                if not target:
                    raise NotFoundError("entity not found: " + target_id)
                if target["status"] == "merged" or json.loads(target["data"]).get("merged_into"):
                    raise ConflictError("target event %s already merged" % target_id)
                tdata = json.loads(target["data"])
                tdata["reports"] = merge_reports(tdata.get("reports", []), moved_reports)
                target_magnitude = magnitude_from_reports(tdata["reports"])
                if target_magnitude is not None:
                    tdata["magnitude"] = target_magnitude
                target_status = post_event_status(target["status"])
                target_version = int(target["version"]) + 1
                connection.execute(
                    "UPDATE entities SET status = ?, version = ?, data = ?, updated_at = ? WHERE id = ?",
                    (
                        target_status,
                        target_version,
                        json.dumps(tdata, ensure_ascii=False, sort_keys=True),
                        now,
                        target_id,
                    ),
                )
                self._insert_version(
                    connection, target_id, target["kind"], target_status,
                    target_version, tdata, target["created_by"], "split",
                )
                result_target = target_id
            else:
                new_id = (new_event_data or {}).get("id") or str(uuid4())
                tdata = {
                    "title": (new_event_data or {}).get("title"),
                    "origin_time": (new_event_data or {}).get("origin_time"),
                    "location": (new_event_data or {}).get("location"),
                    "reports": moved_reports,
                }
                target_magnitude = magnitude_from_reports(moved_reports)
                if target_magnitude is not None:
                    tdata["magnitude"] = target_magnitude
                connection.execute(
                    "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                    "VALUES (?, 'event', 'candidate', 1, ?, ?, ?, ?)",
                    (
                        new_id,
                        json.dumps(tdata, ensure_ascii=False, sort_keys=True),
                        actor_id,
                        now,
                        now,
                    ),
                )
                self._insert_version(
                    connection, new_id, "event", "candidate", 1, tdata, actor_id, "split",
                )
                result_target = new_id
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(event_id), self.get_entity(result_target)

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity_id,
                    actor_id,
                    actor_role,
                    action,
                    from_status,
                    to_status,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                    utcnow(),
                ),
            )

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_idempotency(self, actor_id, idem_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
