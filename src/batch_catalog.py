"""SQLite catalog for Label-Check pipeline batches and their QC queues."""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import sqlite3
import threading
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, Iterator, Mapping, Optional, Sequence


SCHEMA_VERSION = 4
QUEUE_STATUSES = {"pending", "leased", "completed"}
DOCUMENT_KINDS = {"enriched", "name_mapping"}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_relative_path(value: str) -> str:
    """Return a safe, portable scanner/batch catalog key."""
    normalized = str(PurePosixPath(str(value).replace("\\", "/")))
    path = PurePosixPath(normalized)
    if normalized in {"", "."} or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"invalid relative batch path: {value}")
    if len(path.parts) != 2 or not path.parts[0].startswith("SS"):
        raise ValueError(f"batch path must match SS*/batch: {value}")
    return normalized


def public_batch_id(relative_path: str) -> str:
    key = normalize_relative_path(relative_path).casefold()
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


class BatchCatalog:
    """Thread-safe SQLite access with a dynamically configurable instance root."""

    def __init__(self) -> None:
        self._schema_path: Optional[Path] = None
        self._schema_lock = threading.Lock()

    @staticmethod
    def database_path(instance_dir: str | Path) -> Path:
        return Path(instance_dir) / "batch_catalog.sqlite3"

    def reset(self) -> None:
        self._schema_path = None

    def _connect(self, instance_dir: str | Path) -> sqlite3.Connection:
        path = self.database_path(instance_dir)
        self._ensure_schema(path)
        connection = sqlite3.connect(path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextlib.contextmanager
    def connection(self, instance_dir: str | Path) -> Iterator[sqlite3.Connection]:
        connection = self._connect(instance_dir)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _ensure_schema(self, path: Path) -> None:
        if self._schema_path == path and path.exists():
            return
        with self._schema_lock:
            if self._schema_path == path and path.exists():
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_symlink():
                raise RuntimeError(f"Batch catalog cannot be a symbolic link: {path}")
            connection = sqlite3.connect(path, timeout=30)
            try:
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA busy_timeout=30000")
                connection.execute("PRAGMA journal_mode=WAL")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS catalog_metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS batches (
                        id INTEGER PRIMARY KEY,
                        public_id TEXT NOT NULL UNIQUE,
                        relative_path TEXT NOT NULL COLLATE NOCASE UNIQUE,
                        scanner_name TEXT NOT NULL,
                        batch_name TEXT NOT NULL,
                        run_type TEXT NOT NULL DEFAULT 'nightly'
                            CHECK(run_type IN ('nightly','on_demand')),
                        qc_complete INTEGER NOT NULL DEFAULT 0 CHECK(qc_complete IN (0,1)),
                        renamed_complete INTEGER NOT NULL DEFAULT 0 CHECK(renamed_complete IN (0,1)),
                        validity TEXT NOT NULL DEFAULT 'ready'
                            CHECK(validity IN ('ready','invalid','missing')),
                        validation_error TEXT NOT NULL DEFAULT '',
                        slide_count INTEGER NOT NULL DEFAULT 0 CHECK(slide_count >= 0),
                        pending_count INTEGER NOT NULL DEFAULT 0 CHECK(pending_count >= 0),
                        leased_count INTEGER NOT NULL DEFAULT 0 CHECK(leased_count >= 0),
                        completed_count INTEGER NOT NULL DEFAULT 0 CHECK(completed_count >= 0),
                        enriched_mtime_ns INTEGER,
                        mapping_mtime_ns INTEGER,
                        history_mtime_ns INTEGER,
                        renaming_status TEXT NOT NULL DEFAULT 'missing',
                        history_status TEXT NOT NULL DEFAULT 'not_needed',
                        first_seen_at TEXT NOT NULL,
                        last_seen_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS queue_items (
                        batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
                        original_index INTEGER NOT NULL CHECK(original_index >= 0),
                        status TEXT NOT NULL CHECK(status IN ('pending','leased','completed')),
                        leased_by_id TEXT,
                        leased_at TEXT,
                        completed_by_id TEXT,
                        completed_at TEXT,
                        PRIMARY KEY(batch_id, original_index)
                    );
                    CREATE INDEX IF NOT EXISTS queue_batch_status
                        ON queue_items(batch_id, status);
                    CREATE INDEX IF NOT EXISTS queue_lease_owner
                        ON queue_items(leased_by_id, status);
                    CREATE INDEX IF NOT EXISTS queue_completion_owner
                        ON queue_items(completed_by_id, completed_at);
                    CREATE INDEX IF NOT EXISTS queue_batch_completion_owner
                        ON queue_items(batch_id, completed_by_id, completed_at DESC);
                    CREATE TABLE IF NOT EXISTS batch_documents (
                        batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
                        kind TEXT NOT NULL CHECK(kind IN ('enriched','name_mapping')),
                        fields_json TEXT NOT NULL,
                        source_hash TEXT NOT NULL,
                        desired_version INTEGER NOT NULL DEFAULT 1,
                        exported_version INTEGER NOT NULL DEFAULT 1,
                        status TEXT NOT NULL DEFAULT 'current'
                            CHECK(status IN ('current','pending','exporting','failed','conflict')),
                        error TEXT NOT NULL DEFAULT '',
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY(batch_id,kind)
                    );
                    CREATE TABLE IF NOT EXISTS batch_document_rows (
                        batch_id INTEGER NOT NULL,
                        kind TEXT NOT NULL,
                        row_index INTEGER NOT NULL CHECK(row_index >= 0),
                        data_json TEXT NOT NULL,
                        PRIMARY KEY(batch_id,kind,row_index),
                        FOREIGN KEY(batch_id,kind) REFERENCES batch_documents(batch_id,kind)
                            ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS file_exports (
                        batch_id INTEGER NOT NULL,
                        kind TEXT NOT NULL,
                        desired_version INTEGER NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending'
                            CHECK(status IN ('pending','exporting','failed')),
                        leased_at TEXT,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        last_error TEXT NOT NULL DEFAULT '',
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY(batch_id,kind),
                        FOREIGN KEY(batch_id,kind) REFERENCES batch_documents(batch_id,kind)
                            ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS copath_sources (
                        source_key TEXT PRIMARY KEY,
                        path TEXT NOT NULL,
                        size INTEGER,
                        mtime_ns INTEGER,
                        indexed_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS copath_report_rows (
                        source_key TEXT NOT NULL REFERENCES copath_sources(source_key)
                            ON DELETE CASCADE,
                        accession_key TEXT NOT NULL,
                        data_json TEXT NOT NULL,
                        PRIMARY KEY(source_key,accession_key)
                    );
                    CREATE INDEX IF NOT EXISTS copath_reports_accession
                        ON copath_report_rows(accession_key);
                    CREATE TABLE IF NOT EXISTS transfer_sources (
                        source_key TEXT PRIMARY KEY,
                        source_kind TEXT NOT NULL CHECK(source_kind IN ('sdl','mapping')),
                        batch_id INTEGER REFERENCES batches(id) ON DELETE CASCADE,
                        path TEXT NOT NULL,
                        size INTEGER,
                        mtime_ns INTEGER,
                        status TEXT NOT NULL CHECK(status IN ('ready','warning','error')),
                        error TEXT NOT NULL DEFAULT '',
                        indexed_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS transfer_slides (
                        slide_id TEXT PRIMARY KEY,
                        batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
                        source_row INTEGER NOT NULL CHECK(source_row >= 0),
                        raw_original_path TEXT NOT NULL,
                        original_path TEXT NOT NULL,
                        accession TEXT NOT NULL,
                        accession_key TEXT NOT NULL,
                        organ TEXT NOT NULL,
                        pid TEXT NOT NULL,
                        accession_date TEXT NOT NULL,
                        stain TEXT NOT NULL,
                        image_type TEXT NOT NULL,
                        samp_acq_type TEXT NOT NULL,
                        block_number TEXT NOT NULL,
                        section_count TEXT NOT NULL,
                        destination_name TEXT NOT NULL,
                        digitization_date TEXT NOT NULL DEFAULT '',
                        UNIQUE(batch_id, source_row)
                    );
                    CREATE TABLE IF NOT EXISTS transfer_slide_types (
                        slide_id TEXT NOT NULL REFERENCES transfer_slides(slide_id) ON DELETE CASCADE,
                        slide_type TEXT NOT NULL,
                        PRIMARY KEY(slide_id, slide_type)
                    );
                    CREATE INDEX IF NOT EXISTS transfer_slides_batch
                        ON transfer_slides(batch_id);
                    CREATE INDEX IF NOT EXISTS transfer_slides_accession
                        ON transfer_slides(accession_key);
                    CREATE INDEX IF NOT EXISTS transfer_slides_pid
                        ON transfer_slides(pid);
                    CREATE INDEX IF NOT EXISTS transfer_slides_organ
                        ON transfer_slides(organ);
                    CREATE INDEX IF NOT EXISTS transfer_slide_types_type
                        ON transfer_slide_types(slide_type, slide_id);
                    """
                )
                connection.execute(
                    "INSERT INTO catalog_metadata(key,value) VALUES('schema_version',?) "
                    "ON CONFLICT(key) DO NOTHING",
                    (str(SCHEMA_VERSION),),
                )
                version = connection.execute(
                    "SELECT value FROM catalog_metadata WHERE key='schema_version'"
                ).fetchone()[0]
                if int(version) < SCHEMA_VERSION:
                    connection.commit()
                    backup_path = path.with_name(
                        f"{path.name}.pre-v{SCHEMA_VERSION}.backup"
                    )
                    if not backup_path.exists():
                        backup = sqlite3.connect(backup_path)
                        try:
                            connection.backup(backup)
                        finally:
                            backup.close()
                if int(version) == 1:
                    connection.execute(
                        "UPDATE catalog_metadata SET value='2' WHERE key='schema_version'",
                    )
                    version = "2"
                if int(version) == 2:
                    columns = {
                        row[1] for row in connection.execute("PRAGMA table_info(batches)")
                    }
                    if "run_type" not in columns:
                        connection.execute(
                            "ALTER TABLE batches ADD COLUMN run_type TEXT NOT NULL "
                            "DEFAULT 'nightly' CHECK(run_type IN ('nightly','on_demand'))"
                        )
                    connection.execute(
                        "UPDATE catalog_metadata SET value=? WHERE key='schema_version'",
                        ("3",),
                    )
                    version = "3"
                if int(version) == 3:
                    columns = {
                        row[1] for row in connection.execute("PRAGMA table_info(batches)")
                    }
                    for column in ("pending_count", "leased_count", "completed_count"):
                        if column not in columns:
                            connection.execute(
                                f"ALTER TABLE batches ADD COLUMN {column} INTEGER NOT NULL "
                                f"DEFAULT 0 CHECK({column} >= 0)"
                            )
                    connection.execute(
                        """
                        UPDATE batches SET
                            pending_count=(SELECT COUNT(*) FROM queue_items q WHERE q.batch_id=batches.id AND q.status='pending'),
                            leased_count=(SELECT COUNT(*) FROM queue_items q WHERE q.batch_id=batches.id AND q.status='leased'),
                            completed_count=(SELECT COUNT(*) FROM queue_items q WHERE q.batch_id=batches.id AND q.status='completed')
                        """
                    )
                    connection.execute(
                        "UPDATE catalog_metadata SET value=? WHERE key='schema_version'",
                        (str(SCHEMA_VERSION),),
                    )
                    version = str(SCHEMA_VERSION)
                if int(version) != SCHEMA_VERSION:
                    raise RuntimeError(f"Unsupported batch catalog schema version: {version}")
                connection.executescript(
                    """
                    CREATE TRIGGER IF NOT EXISTS queue_count_insert
                    AFTER INSERT ON queue_items BEGIN
                        UPDATE batches SET
                            pending_count=pending_count+(NEW.status='pending'),
                            leased_count=leased_count+(NEW.status='leased'),
                            completed_count=completed_count+(NEW.status='completed')
                        WHERE id=NEW.batch_id;
                    END;
                    CREATE TRIGGER IF NOT EXISTS queue_count_delete
                    AFTER DELETE ON queue_items BEGIN
                        UPDATE batches SET
                            pending_count=pending_count-(OLD.status='pending'),
                            leased_count=leased_count-(OLD.status='leased'),
                            completed_count=completed_count-(OLD.status='completed')
                        WHERE id=OLD.batch_id;
                    END;
                    CREATE TRIGGER IF NOT EXISTS queue_count_update
                    AFTER UPDATE OF status ON queue_items WHEN OLD.status<>NEW.status BEGIN
                        UPDATE batches SET
                            pending_count=pending_count-(OLD.status='pending')+(NEW.status='pending'),
                            leased_count=leased_count-(OLD.status='leased')+(NEW.status='leased'),
                            completed_count=completed_count-(OLD.status='completed')+(NEW.status='completed')
                        WHERE id=NEW.batch_id;
                    END;
                    """
                )
                connection.commit()
                if os.name != "nt":
                    try:
                        os.chmod(path, 0o600)
                    except PermissionError:
                        containerized = os.environ.get(
                            "INSLIDE_CONTAINER", "false"
                        ).lower() == "true"
                        if not containerized or not os.access(path, os.R_OK | os.W_OK):
                            raise
            finally:
                connection.close()
            self._schema_path = path

    @staticmethod
    def _batch_row(connection: sqlite3.Connection, public_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM batches WHERE public_id=?", (public_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown batch: {public_id}")
        return row

    def list_batches(self, instance_dir: str | Path) -> list[dict]:
        with self.connection(instance_dir) as connection:
            rows = connection.execute(
                """
                SELECT b.*, pending_count+leased_count+completed_count AS queue_total
                FROM batches b ORDER BY b.relative_path COLLATE NOCASE
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def get_batch(self, instance_dir: str | Path, public_id: str) -> Optional[dict]:
        with self.connection(instance_dir) as connection:
            row = connection.execute(
                """
                SELECT b.*, pending_count+leased_count+completed_count AS queue_total
                FROM batches b WHERE b.public_id=?
                """,
                (public_id,),
            ).fetchone()
            return dict(row) if row is not None else None

    @staticmethod
    def _document_kind(kind: str) -> str:
        if kind not in DOCUMENT_KINDS:
            raise ValueError(f"unsupported batch document: {kind}")
        return kind

    @staticmethod
    def _document_values(
        fields: Sequence[str], rows: Iterable[Mapping[str, object]]
    ) -> tuple[list[str], list[dict[str, str]]]:
        headers = list(dict.fromkeys(str(field) for field in fields))
        if not headers:
            raise ValueError("batch document must have at least one field")
        values = [
            {field: str(row.get(field) or "") for field in headers}
            for row in rows
        ]
        return headers, values

    def import_document(
        self,
        instance_dir: str | Path,
        public_id: str,
        kind: str,
        fields: Sequence[str],
        rows: Iterable[Mapping[str, object]],
        source_hash: str,
    ) -> bool:
        """Import one existing file once; never replace central edits."""
        kind = self._document_kind(kind)
        headers, values = self._document_values(fields, rows)
        with self.connection(instance_dir) as connection:
            batch = self._batch_row(connection, public_id)
            existing = connection.execute(
                "SELECT 1 FROM batch_documents WHERE batch_id=? AND kind=?",
                (batch["id"], kind),
            ).fetchone()
            if existing is not None:
                return False
            now = utc_now()
            connection.execute(
                """
                INSERT INTO batch_documents(
                    batch_id,kind,fields_json,source_hash,desired_version,
                    exported_version,status,error,updated_at
                ) VALUES(?,?,?,?,1,1,'current','',?)
                """,
                (batch["id"], kind, json.dumps(headers), source_hash, now),
            )
            connection.executemany(
                "INSERT INTO batch_document_rows(batch_id,kind,row_index,data_json) "
                "VALUES(?,?,?,?)",
                [
                    (batch["id"], kind, index, json.dumps(row, separators=(",", ":")))
                    for index, row in enumerate(values)
                ],
            )
            return True

    def document_state(
        self, instance_dir: str | Path, public_id: str, kind: str
    ) -> Optional[dict]:
        kind = self._document_kind(kind)
        with self.connection(instance_dir) as connection:
            batch = self._batch_row(connection, public_id)
            row = connection.execute(
                "SELECT * FROM batch_documents WHERE batch_id=? AND kind=?",
                (batch["id"], kind),
            ).fetchone()
            return dict(row) if row is not None else None

    def load_document(
        self, instance_dir: str | Path, public_id: str, kind: str
    ) -> tuple[list[str], list[dict[str, str]], dict]:
        kind = self._document_kind(kind)
        with self.connection(instance_dir) as connection:
            batch = self._batch_row(connection, public_id)
            state = connection.execute(
                "SELECT * FROM batch_documents WHERE batch_id=? AND kind=?",
                (batch["id"], kind),
            ).fetchone()
            if state is None:
                raise KeyError(f"{kind} is not imported for batch {public_id}")
            rows = connection.execute(
                "SELECT data_json FROM batch_document_rows "
                "WHERE batch_id=? AND kind=? ORDER BY row_index",
                (batch["id"], kind),
            ).fetchall()
            return (
                list(json.loads(state["fields_json"])),
                [dict(json.loads(row[0])) for row in rows],
                dict(state),
            )

    def replace_document(
        self,
        instance_dir: str | Path,
        public_id: str,
        kind: str,
        fields: Sequence[str],
        rows: Iterable[Mapping[str, object]],
    ) -> int:
        """Replace central rows and durably request one latest-version export."""
        kind = self._document_kind(kind)
        headers, values = self._document_values(fields, rows)
        with self.connection(instance_dir) as connection:
            batch = self._batch_row(connection, public_id)
            state = connection.execute(
                "SELECT desired_version FROM batch_documents WHERE batch_id=? AND kind=?",
                (batch["id"], kind),
            ).fetchone()
            if state is None:
                raise KeyError(f"{kind} is not imported for batch {public_id}")
            version = int(state[0]) + 1
            now = utc_now()
            connection.execute(
                "DELETE FROM batch_document_rows WHERE batch_id=? AND kind=?",
                (batch["id"], kind),
            )
            connection.executemany(
                "INSERT INTO batch_document_rows(batch_id,kind,row_index,data_json) "
                "VALUES(?,?,?,?)",
                [
                    (batch["id"], kind, index, json.dumps(row, separators=(",", ":")))
                    for index, row in enumerate(values)
                ],
            )
            connection.execute(
                """
                UPDATE batch_documents SET fields_json=?,desired_version=?,status='pending',
                    error='',updated_at=? WHERE batch_id=? AND kind=?
                """,
                (json.dumps(headers), version, now, batch["id"], kind),
            )
            connection.execute(
                """
                INSERT INTO file_exports(
                    batch_id,kind,desired_version,status,leased_at,attempts,last_error,updated_at
                ) VALUES(?,?,?,'pending',NULL,0,'',?)
                ON CONFLICT(batch_id,kind) DO UPDATE SET
                    desired_version=excluded.desired_version,status='pending',leased_at=NULL,
                    last_error='',updated_at=excluded.updated_at
                """,
                (batch["id"], kind, version, now),
            )
            return version

    def update_document_rows(
        self,
        instance_dir: str | Path,
        public_id: str,
        kind: str,
        updates: Mapping[int, Mapping[str, object]],
    ) -> int:
        """Update selected central rows and enqueue one coalesced export."""
        kind = self._document_kind(kind)
        with self.connection(instance_dir) as connection:
            batch = self._batch_row(connection, public_id)
            state = connection.execute(
                "SELECT fields_json,desired_version,status,error FROM batch_documents "
                "WHERE batch_id=? AND kind=?",
                (batch["id"], kind),
            ).fetchone()
            if state is None:
                raise KeyError(f"{kind} is not imported for batch {public_id}")
            if state["status"] == "conflict":
                raise RuntimeError(str(state["error"]))
            fields = list(json.loads(state["fields_json"]))
            for index, changes in updates.items():
                stored = connection.execute(
                    "SELECT data_json FROM batch_document_rows "
                    "WHERE batch_id=? AND kind=? AND row_index=?",
                    (batch["id"], kind, int(index)),
                ).fetchone()
                if stored is None:
                    raise KeyError(f"unknown {kind} row: {index}")
                row = dict(json.loads(stored[0]))
                for field, value in changes.items():
                    if field not in fields:
                        raise ValueError(f"unknown {kind} field: {field}")
                    row[field] = str(value or "")
                connection.execute(
                    "UPDATE batch_document_rows SET data_json=? "
                    "WHERE batch_id=? AND kind=? AND row_index=?",
                    (
                        json.dumps(row, separators=(",", ":")),
                        batch["id"], kind, int(index),
                    ),
                )
            version = int(state["desired_version"]) + 1
            now = utc_now()
            connection.execute(
                "UPDATE batch_documents SET desired_version=?,status='pending',error='',updated_at=? "
                "WHERE batch_id=? AND kind=?",
                (version, now, batch["id"], kind),
            )
            connection.execute(
                """
                INSERT INTO file_exports(
                    batch_id,kind,desired_version,status,leased_at,attempts,last_error,updated_at
                ) VALUES(?,?,?,'pending',NULL,0,'',?)
                ON CONFLICT(batch_id,kind) DO UPDATE SET
                    desired_version=excluded.desired_version,status='pending',leased_at=NULL,
                    last_error='',updated_at=excluded.updated_at
                """,
                (batch["id"], kind, version, now),
            )
            return version

    def adopt_document(
        self,
        instance_dir: str | Path,
        public_id: str,
        kind: str,
        fields: Sequence[str],
        rows: Iterable[Mapping[str, object]],
        source_hash: str,
    ) -> int:
        """Adopt a file written by an internal pipeline job as current central data."""
        kind = self._document_kind(kind)
        headers, values = self._document_values(fields, rows)
        with self.connection(instance_dir) as connection:
            batch = self._batch_row(connection, public_id)
            state = connection.execute(
                "SELECT desired_version FROM batch_documents WHERE batch_id=? AND kind=?",
                (batch["id"], kind),
            ).fetchone()
            if state is None:
                version = 1
                connection.execute(
                    """
                    INSERT INTO batch_documents(
                        batch_id,kind,fields_json,source_hash,desired_version,
                        exported_version,status,error,updated_at
                    ) VALUES(?,?,?,?,?,?,'current','',?)
                    """,
                    (
                        batch["id"], kind, json.dumps(headers), source_hash,
                        version, version, utc_now(),
                    ),
                )
            else:
                version = int(state[0]) + 1
                connection.execute(
                    """
                    UPDATE batch_documents SET fields_json=?,source_hash=?,desired_version=?,
                        exported_version=?,status='current',error='',updated_at=?
                    WHERE batch_id=? AND kind=?
                    """,
                    (
                        json.dumps(headers), source_hash, version, version, utc_now(),
                        batch["id"], kind,
                    ),
                )
                connection.execute(
                    "DELETE FROM batch_document_rows WHERE batch_id=? AND kind=?",
                    (batch["id"], kind),
                )
            connection.executemany(
                "INSERT INTO batch_document_rows(batch_id,kind,row_index,data_json) "
                "VALUES(?,?,?,?)",
                [
                    (batch["id"], kind, index, json.dumps(row, separators=(",", ":")))
                    for index, row in enumerate(values)
                ],
            )
            connection.execute(
                "DELETE FROM file_exports WHERE batch_id=? AND kind=?",
                (batch["id"], kind),
            )
            return version

    def claim_export(
        self, instance_dir: str | Path, stale_before: str
    ) -> Optional[dict]:
        connection = self._connect(instance_dir)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT e.batch_id,e.kind,e.desired_version,b.public_id,b.relative_path
                FROM file_exports e JOIN batches b ON b.id=e.batch_id
                WHERE e.status='pending'
                   OR (e.status='failed' AND e.updated_at<?)
                   OR (e.status='exporting' AND e.leased_at<?)
                ORDER BY e.updated_at LIMIT 1
                """,
                (stale_before, stale_before),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            now = utc_now()
            connection.execute(
                "UPDATE file_exports SET status='exporting',leased_at=?,attempts=attempts+1,updated_at=? "
                "WHERE batch_id=? AND kind=?",
                (now, now, row["batch_id"], row["kind"]),
            )
            connection.execute(
                "UPDATE batch_documents SET status='exporting',updated_at=? "
                "WHERE batch_id=? AND kind=?",
                (now, row["batch_id"], row["kind"]),
            )
            connection.commit()
            return dict(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def export_snapshot(
        self, instance_dir: str | Path, public_id: str, kind: str, version: int
    ) -> Optional[tuple[list[str], list[dict[str, str]]]]:
        fields, rows, state = self.load_document(instance_dir, public_id, kind)
        if int(state["desired_version"]) != int(version) or state["status"] == "conflict":
            return None
        return fields, rows

    def finish_export(
        self,
        instance_dir: str | Path,
        public_id: str,
        kind: str,
        version: int,
        source_hash: str,
    ) -> bool:
        kind = self._document_kind(kind)
        with self.connection(instance_dir) as connection:
            batch = self._batch_row(connection, public_id)
            state = connection.execute(
                "SELECT desired_version FROM batch_documents WHERE batch_id=? AND kind=?",
                (batch["id"], kind),
            ).fetchone()
            if state is None or int(state[0]) != int(version):
                connection.execute(
                    "UPDATE file_exports SET status='pending',leased_at=NULL,updated_at=? "
                    "WHERE batch_id=? AND kind=?",
                    (utc_now(), batch["id"], kind),
                )
                return False
            now = utc_now()
            connection.execute(
                """
                UPDATE batch_documents SET source_hash=?,exported_version=?,status='current',
                    error='',updated_at=? WHERE batch_id=? AND kind=?
                """,
                (source_hash, version, now, batch["id"], kind),
            )
            connection.execute(
                "DELETE FROM file_exports WHERE batch_id=? AND kind=?",
                (batch["id"], kind),
            )
            return True

    def fail_export(
        self, instance_dir: str | Path, public_id: str, kind: str, error: str
    ) -> None:
        kind = self._document_kind(kind)
        with self.connection(instance_dir) as connection:
            batch = self._batch_row(connection, public_id)
            now = utc_now()
            connection.execute(
                "UPDATE file_exports SET status='failed',leased_at=NULL,last_error=?,updated_at=? "
                "WHERE batch_id=? AND kind=?",
                (error, now, batch["id"], kind),
            )
            connection.execute(
                "UPDATE batch_documents SET status='failed',error=?,updated_at=? "
                "WHERE batch_id=? AND kind=?",
                (error, now, batch["id"], kind),
            )

    def mark_document_conflict(
        self, instance_dir: str | Path, public_id: str, kind: str, error: str
    ) -> None:
        kind = self._document_kind(kind)
        with self.connection(instance_dir) as connection:
            batch = self._batch_row(connection, public_id)
            connection.execute(
                "UPDATE batch_documents SET status='conflict',error=?,updated_at=? "
                "WHERE batch_id=? AND kind=?",
                (error, utc_now(), batch["id"], kind),
            )
            connection.execute(
                "DELETE FROM file_exports WHERE batch_id=? AND kind=?",
                (batch["id"], kind),
            )

    def copath_source_state(
        self, instance_dir: str | Path, source_key: str
    ) -> Optional[dict]:
        with self.connection(instance_dir) as connection:
            row = connection.execute(
                "SELECT * FROM copath_sources WHERE source_key=?", (source_key,)
            ).fetchone()
            return dict(row) if row is not None else None

    def replace_copath_source(
        self,
        instance_dir: str | Path,
        source_key: str,
        path: str,
        size: Optional[int],
        mtime_ns: Optional[int],
        rows: Iterable[Mapping[str, object]],
    ) -> None:
        values = []
        for row in rows:
            accession = str(row.get("accession_id") or row.get("AccessionID") or "")
            key = accession.strip().casefold()
            if key:
                values.append(
                    (source_key, key, json.dumps(dict(row), separators=(",", ":")))
                )
        with self.connection(instance_dir) as connection:
            connection.execute(
                """
                INSERT INTO copath_sources(source_key,path,size,mtime_ns,indexed_at)
                VALUES(?,?,?,?,?) ON CONFLICT(source_key) DO UPDATE SET
                    path=excluded.path,size=excluded.size,mtime_ns=excluded.mtime_ns,
                    indexed_at=excluded.indexed_at
                """,
                (source_key, path, size, mtime_ns, utc_now()),
            )
            connection.execute(
                "DELETE FROM copath_report_rows WHERE source_key=?", (source_key,)
            )
            connection.executemany(
                "INSERT INTO copath_report_rows(source_key,accession_key,data_json) "
                "VALUES(?,?,?)",
                values,
            )

    def copath_reports(
        self, instance_dir: str | Path, accession_keys: Sequence[str]
    ) -> dict[str, dict]:
        keys = list(dict.fromkeys(str(key) for key in accession_keys if key))
        if not keys:
            return {}
        placeholders = ",".join("?" for _ in keys)
        with self.connection(instance_dir) as connection:
            rows = connection.execute(
                f"SELECT accession_key,data_json FROM copath_report_rows "
                f"WHERE accession_key IN ({placeholders})",
                keys,
            ).fetchall()
            return {str(row["accession_key"]): dict(json.loads(row["data_json"])) for row in rows}

    def upsert_batch(
        self,
        instance_dir: str | Path,
        relative_path: str,
        *,
        run_type: str = "nightly",
        qc_complete: bool = False,
        renamed_complete: bool = False,
        validity: str = "ready",
        validation_error: str = "",
        slide_count: int = 0,
        enriched_mtime_ns: Optional[int] = None,
        mapping_mtime_ns: Optional[int] = None,
        history_mtime_ns: Optional[int] = None,
        renaming_status: str = "missing",
        history_status: str = "not_needed",
        preserve_stages: bool = True,
    ) -> str:
        relative_path = normalize_relative_path(relative_path)
        if run_type not in {"nightly", "on_demand"}:
            raise ValueError(f"invalid run type: {run_type}")
        scanner_name, batch_name = PurePosixPath(relative_path).parts
        public_id = public_batch_id(relative_path)
        now = utc_now()
        with self.connection(instance_dir) as connection:
            existing = connection.execute(
                "SELECT public_id,qc_complete,renamed_complete FROM batches WHERE relative_path=?",
                (relative_path,),
            ).fetchone()
            if existing is not None:
                public_id = str(existing[0])
            if existing is not None and preserve_stages:
                qc_complete = bool(existing[1])
                renamed_complete = bool(existing[2])
            connection.execute(
                """
                INSERT INTO batches(
                    public_id,relative_path,scanner_name,batch_name,run_type,qc_complete,
                    renamed_complete,validity,validation_error,slide_count,
                    enriched_mtime_ns,mapping_mtime_ns,history_mtime_ns,renaming_status,history_status,
                    first_seen_at,last_seen_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(relative_path) DO UPDATE SET
                    public_id=excluded.public_id,
                    scanner_name=excluded.scanner_name,
                    batch_name=excluded.batch_name,
                    run_type=excluded.run_type,
                    qc_complete=excluded.qc_complete,
                    renamed_complete=excluded.renamed_complete,
                    validity=excluded.validity,
                    validation_error=excluded.validation_error,
                    slide_count=excluded.slide_count,
                    enriched_mtime_ns=excluded.enriched_mtime_ns,
                    mapping_mtime_ns=excluded.mapping_mtime_ns,
                    history_mtime_ns=excluded.history_mtime_ns,
                    renaming_status=excluded.renaming_status,
                    history_status=excluded.history_status,
                    last_seen_at=excluded.last_seen_at,
                    updated_at=excluded.updated_at
                """,
                (
                    public_id, relative_path, scanner_name, batch_name,
                    run_type, int(qc_complete), int(renamed_complete), validity,
                    validation_error, int(slide_count), enriched_mtime_ns,
                    mapping_mtime_ns, history_mtime_ns, renaming_status, history_status,
                    now, now, now,
                ),
            )
        return public_id

    def mark_unseen_missing(
        self, instance_dir: str | Path, seen_relative_paths: Sequence[str]
    ) -> None:
        normalized = [normalize_relative_path(path) for path in seen_relative_paths]
        with self.connection(instance_dir) as connection:
            if not normalized:
                connection.execute(
                    "UPDATE batches SET validity='missing',validation_error='batch directory is missing',updated_at=?",
                    (utc_now(),),
                )
                return
            placeholders = ",".join("?" for _ in normalized)
            connection.execute(
                f"UPDATE batches SET validity='missing',validation_error='batch directory is missing',updated_at=? "
                f"WHERE relative_path NOT IN ({placeholders})",
                (utc_now(), *normalized),
            )

    def replace_queue(
        self,
        instance_dir: str | Path,
        public_id: str,
        rows: Iterable[Mapping[str, object]],
    ) -> None:
        values = []
        seen: set[int] = set()
        for row in rows:
            index = int(row["original_index"])
            status = str(row.get("status") or "pending")
            if index < 0 or index in seen or status not in QUEUE_STATUSES:
                raise ValueError(f"invalid queue row for {public_id}: {row}")
            seen.add(index)
            values.append(
                (
                    index, status, row.get("leased_by_id") or None,
                    row.get("leased_at") or None, row.get("completed_by_id") or None,
                    row.get("completed_at") or None,
                )
            )
        with self.connection(instance_dir) as connection:
            batch = self._batch_row(connection, public_id)
            connection.execute("DELETE FROM queue_items WHERE batch_id=?", (batch["id"],))
            connection.executemany(
                """
                INSERT INTO queue_items(
                    batch_id,original_index,status,leased_by_id,leased_at,
                    completed_by_id,completed_at
                ) VALUES(?,?,?,?,?,?,?)
                """,
                [(batch["id"], *value) for value in values],
            )
            connection.execute(
                "UPDATE batches SET slide_count=?,updated_at=? WHERE id=?",
                (len(values), utc_now(), batch["id"]),
            )

    def apply_queue_changes(
        self,
        instance_dir: str | Path,
        public_id: str,
        rows: Iterable[Mapping[str, object]],
        deleted_indices: Iterable[int] = (),
    ) -> None:
        """Persist only changed queue rows so unrelated concurrent leases survive."""
        values = []
        for row in rows:
            index = int(row["original_index"])
            status = str(row.get("status") or "pending")
            if index < 0 or status not in QUEUE_STATUSES:
                raise ValueError(f"invalid queue row for {public_id}: {row}")
            values.append(
                (
                    index, status, row.get("leased_by_id") or None,
                    row.get("leased_at") or None, row.get("completed_by_id") or None,
                    row.get("completed_at") or None,
                )
            )
        deleted = [int(index) for index in deleted_indices]
        with self.connection(instance_dir) as connection:
            batch = self._batch_row(connection, public_id)
            connection.executemany(
                """
                INSERT INTO queue_items(
                    batch_id,original_index,status,leased_by_id,leased_at,
                    completed_by_id,completed_at
                ) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(batch_id,original_index) DO UPDATE SET
                    status=excluded.status,
                    leased_by_id=excluded.leased_by_id,
                    leased_at=excluded.leased_at,
                    completed_by_id=excluded.completed_by_id,
                    completed_at=excluded.completed_at
                """,
                [(batch["id"], *value) for value in values],
            )
            if deleted:
                placeholders = ",".join("?" for _ in deleted)
                connection.execute(
                    f"DELETE FROM queue_items WHERE batch_id=? AND original_index IN ({placeholders})",
                    (batch["id"], *deleted),
                )
            counts = connection.execute(
                "SELECT pending_count+leased_count+completed_count FROM batches WHERE id=?",
                (batch["id"],),
            ).fetchone()
            connection.execute(
                "UPDATE batches SET slide_count=?,updated_at=? WHERE id=?",
                (int(counts[0]), utc_now(), batch["id"]),
            )

    def load_queue(self, instance_dir: str | Path, public_id: str) -> list[dict]:
        with self.connection(instance_dir) as connection:
            batch = self._batch_row(connection, public_id)
            rows = connection.execute(
                "SELECT original_index,status,leased_by_id,leased_at,completed_by_id,completed_at "
                "FROM queue_items WHERE batch_id=? ORDER BY original_index",
                (batch["id"],),
            ).fetchall()
            return [dict(row) for row in rows]

    def queue_dashboard(
        self, instance_dir: str | Path, public_id: str, user_id: str, limit: int = 5
    ) -> tuple[dict[str, int], list[dict]]:
        with self.connection(instance_dir) as connection:
            batch = self._batch_row(connection, public_id)
            counts = {
                "pending": int(batch["pending_count"]),
                "leased": int(batch["leased_count"]),
                "completed": int(batch["completed_count"]),
            }
            rows = connection.execute(
                """
                SELECT original_index,status,leased_by_id,leased_at,completed_by_id,completed_at
                FROM queue_items
                WHERE batch_id=? AND completed_by_id=?
                ORDER BY completed_at DESC LIMIT ?
                """,
                (batch["id"], user_id, max(0, int(limit))),
            ).fetchall()
            return counts, [dict(row) for row in rows]

    def claim_item(
        self,
        instance_dir: str | Path,
        public_id: str,
        user_id: str,
        leased_at: str,
        original_index: Optional[int] = None,
    ) -> Optional[dict]:
        """Atomically retain/acquire one lease for user, optionally by index."""
        connection = self._connect(instance_dir)
        try:
            connection.execute("BEGIN IMMEDIATE")
            batch = self._batch_row(connection, public_id)
            if original_index is not None:
                connection.execute(
                    """
                    UPDATE queue_items SET status='pending',leased_by_id=NULL,leased_at=NULL
                    WHERE batch_id=? AND status='leased' AND leased_by_id=?
                      AND original_index<>?
                    """,
                    (batch["id"], user_id, original_index),
                )
                row = connection.execute(
                    "SELECT * FROM queue_items WHERE batch_id=? AND original_index=?",
                    (batch["id"], original_index),
                ).fetchone()
                if row is None:
                    connection.rollback()
                    return None
                if row["status"] not in {"completed", "leased"} or row["leased_by_id"] == user_id:
                    connection.execute(
                        """
                        UPDATE queue_items SET status='leased',leased_by_id=?,leased_at=?
                        WHERE batch_id=? AND original_index=? AND status<>'completed'
                          AND (status<>'leased' OR leased_by_id=?)
                        """,
                        (user_id, leased_at, batch["id"], original_index, user_id),
                    )
            else:
                row = connection.execute(
                    """
                    SELECT * FROM queue_items
                    WHERE batch_id=? AND status='leased' AND leased_by_id=?
                    ORDER BY original_index LIMIT 1
                    """,
                    (batch["id"], user_id),
                ).fetchone()
                if row is None:
                    row = connection.execute(
                        """
                        SELECT * FROM queue_items WHERE batch_id=? AND status='pending'
                        ORDER BY original_index LIMIT 1
                        """,
                        (batch["id"],),
                    ).fetchone()
                    if row is not None:
                        connection.execute(
                            """
                            UPDATE queue_items SET status='leased',leased_by_id=?,leased_at=?
                            WHERE batch_id=? AND original_index=? AND status='pending'
                            """,
                            (user_id, leased_at, batch["id"], row["original_index"]),
                        )
            if row is None:
                connection.commit()
                return None
            result = connection.execute(
                "SELECT original_index,status,leased_by_id,leased_at,completed_by_id,completed_at "
                "FROM queue_items WHERE batch_id=? AND original_index=?",
                (batch["id"], row["original_index"]),
            ).fetchone()
            connection.commit()
            return dict(result)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def release_expired(
        self, instance_dir: str | Path, public_id: str, before_iso: str
    ) -> int:
        with self.connection(instance_dir) as connection:
            batch = self._batch_row(connection, public_id)
            cursor = connection.execute(
                """
                UPDATE queue_items SET status='pending',leased_by_id=NULL,leased_at=NULL
                WHERE batch_id=? AND status='leased' AND leased_at IS NOT NULL AND leased_at<?
                """,
                (batch["id"], before_iso),
            )
            return cursor.rowcount

    def release_user(
        self, instance_dir: str | Path, public_id: str, user_id: str
    ) -> int:
        with self.connection(instance_dir) as connection:
            batch = self._batch_row(connection, public_id)
            cursor = connection.execute(
                """
                UPDATE queue_items SET status='pending',leased_by_id=NULL,leased_at=NULL
                WHERE batch_id=? AND status='leased' AND leased_by_id=?
                """,
                (batch["id"], user_id),
            )
            return cursor.rowcount

    def update_stages(
        self,
        instance_dir: str | Path,
        public_id: str,
        *,
        qc_complete: Optional[bool] = None,
        renamed_complete: Optional[bool] = None,
    ) -> dict:
        assignments = ["updated_at=?"]
        values: list[object] = [utc_now()]
        if qc_complete is not None:
            assignments.append("qc_complete=?")
            values.append(int(qc_complete))
        if renamed_complete is not None:
            assignments.append("renamed_complete=?")
            values.append(int(renamed_complete))
        values.append(public_id)
        with self.connection(instance_dir) as connection:
            cursor = connection.execute(
                f"UPDATE batches SET {','.join(assignments)} WHERE public_id=?", values
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown batch: {public_id}")
            row = self._batch_row(connection, public_id)
            return dict(row)

    def mark_qc_complete_if_queue_complete(
        self, instance_dir: str | Path, public_id: str
    ) -> bool:
        """Set QC complete only when queue has rows and none remain unfinished."""
        connection = self._connect(instance_dir)
        try:
            connection.execute("BEGIN IMMEDIATE")
            batch = self._batch_row(connection, public_id)
            counts = connection.execute(
                "SELECT pending_count,leased_count,completed_count FROM batches WHERE id=?",
                (batch["id"],),
            ).fetchone()
            if counts["completed_count"] == 0 or (
                counts["pending_count"] + counts["leased_count"]
            ) != 0:
                connection.rollback()
                return False
            connection.execute(
                "UPDATE batches SET qc_complete=1,updated_at=? WHERE id=?",
                (utc_now(), batch["id"]),
            )
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def set_metadata(self, instance_dir: str | Path, key: str, value: str) -> None:
        with self.connection(instance_dir) as connection:
            connection.execute(
                "INSERT INTO catalog_metadata(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def get_metadata(self, instance_dir: str | Path, key: str) -> Optional[str]:
        with self.connection(instance_dir) as connection:
            row = connection.execute(
                "SELECT value FROM catalog_metadata WHERE key=?", (key,)
            ).fetchone()
            return str(row[0]) if row is not None else None

    def replace_transfer_catalog(
        self,
        instance_dir: str | Path,
        slides: Iterable[Mapping[str, object]],
        sources: Iterable[Mapping[str, object]],
        signature: str,
    ) -> None:
        """Atomically replace derived transfer rows and their source state."""
        slide_rows = [dict(row) for row in slides]
        source_rows = [dict(row) for row in sources]
        with self.connection(instance_dir) as connection:
            batch_ids = {
                str(row["public_id"]): int(row["id"])
                for row in connection.execute("SELECT id,public_id FROM batches")
            }
            connection.execute("DELETE FROM transfer_sources")
            connection.execute("DELETE FROM transfer_slides")
            connection.executemany(
                """
                INSERT INTO transfer_sources(
                    source_key,source_kind,batch_id,path,size,mtime_ns,status,error,indexed_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        row["source_key"], row["source_kind"],
                        batch_ids.get(str(row.get("batch_id") or "")), row["path"],
                        row.get("size"), row.get("mtime_ns"), row["status"],
                        row.get("error", ""), utc_now(),
                    )
                    for row in source_rows
                ],
            )
            connection.executemany(
                """
                INSERT INTO transfer_slides(
                    slide_id,batch_id,source_row,raw_original_path,original_path,
                    accession,accession_key,organ,pid,accession_date,stain,image_type,
                    samp_acq_type,block_number,section_count,destination_name,digitization_date
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        row["id"], batch_ids[str(row["batch_id"])], row["source_row"],
                        row["raw_original_path"], row["original_path"], row["accession"],
                        row["accession_key"], row["organ"], row["pid"],
                        row["accession_date"], row["stain"], row["image_type"],
                        row["samp_acq_type"], row["block_number"], row["section_count"],
                        row["destination_name"], row["digitization_date"],
                    )
                    for row in slide_rows
                ],
            )
            type_rows = [
                (row["id"], slide_type)
                for row in slide_rows
                for slide_type in row.get("sdl_types", ["NONE"])
            ]
            connection.executemany(
                "INSERT INTO transfer_slide_types(slide_id,slide_type) VALUES(?,?)",
                type_rows,
            )
            connection.execute(
                "INSERT INTO catalog_metadata(key,value) VALUES('transfer_signature',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (signature,),
            )

    def list_transfer_slides(self, instance_dir: str | Path) -> list[dict]:
        """Return transfer rows belonging to healthy, finalized batches."""
        with self.connection(instance_dir) as connection:
            rows = connection.execute(
                """
                SELECT s.*, b.public_id AS batch_public_id,
                       b.scanner_name || '/' || b.batch_name AS batch_name,
                       GROUP_CONCAT(t.slide_type, char(31)) AS sdl_types
                FROM transfer_slides s
                JOIN batches b ON b.id=s.batch_id
                JOIN transfer_sources source
                  ON source.batch_id=b.id AND source.source_kind='mapping'
                LEFT JOIN transfer_slide_types t ON t.slide_id=s.slide_id
                WHERE b.renamed_complete=1 AND b.validity='ready'
                  AND source.status='ready'
                GROUP BY s.slide_id
                ORDER BY s.rowid
                """
            ).fetchall()
        result = []
        for raw in rows:
            row = dict(raw)
            row["id"] = row.pop("slide_id")
            row["batch_id"] = row.pop("batch_public_id")
            row["sdl_types"] = (row.get("sdl_types") or "NONE").split(chr(31))
            row.pop("source_row", None)
            row.pop("accession_key", None)
            row.pop("raw_original_path", None)
            result.append(row)
        return result

    def list_transfer_warnings(self, instance_dir: str | Path) -> list[str]:
        """Return persistent warnings for transfer sources that are not healthy."""
        with self.connection(instance_dir) as connection:
            rows = connection.execute(
                """
                SELECT source.source_kind, source.error,
                       b.scanner_name, b.batch_name
                FROM transfer_sources source
                LEFT JOIN batches b ON b.id=source.batch_id
                WHERE source.status<>'ready'
                  AND (source.source_kind='sdl' OR b.renamed_complete=1)
                ORDER BY source.source_key
                """
            ).fetchall()
        warnings = []
        for row in rows:
            if row["source_kind"] == "sdl":
                warnings.append(str(row["error"]))
            else:
                warnings.append(
                    f"Skipped {row['scanner_name']}/{row['batch_name']}: "
                    f"name_mapping.csv {row['error']}."
                )
        return warnings

    def acquire_reconcile_lease(
        self, instance_dir: str | Path, owner: str, lease_seconds: int
    ) -> bool:
        connection = self._connect(instance_dir)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT value FROM catalog_metadata WHERE key='reconcile_lease'"
            ).fetchone()
            now = dt.datetime.now(dt.timezone.utc).timestamp()
            if row is not None:
                try:
                    _, expires = str(row[0]).rsplit(":", 1)
                    if float(expires) > now:
                        connection.rollback()
                        return False
                except (TypeError, ValueError):
                    pass
            value = f"{owner}:{now + max(1, lease_seconds)}"
            connection.execute(
                "INSERT INTO catalog_metadata(key,value) VALUES('reconcile_lease',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (value,),
            )
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def release_reconcile_lease(self, instance_dir: str | Path, owner: str) -> None:
        with self.connection(instance_dir) as connection:
            connection.execute(
                "DELETE FROM catalog_metadata WHERE key='reconcile_lease' AND value LIKE ?",
                (f"{owner}:%",),
            )


catalog = BatchCatalog()
