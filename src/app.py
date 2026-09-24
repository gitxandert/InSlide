"""
This script implements a Flask web application designed for the manual review and correction
of data extracted from whole-slide images (WSIs). It provides a user interface for operators
to verify and amend information like Accession IDs and Stain types that have been processed
by an automated pipeline (e.g., OCR).

The application features:
- User authentication (login/logout) with role-based access (standard user vs. admin).
- An admin panel for user management (adding new users).
- A robust queuing system that "leases" data rows to users for a fixed duration to prevent
  simultaneous edits. Expired leases are automatically returned to the queue.
- Dynamic loading and saving of data from/to a central CSV file.
- Automatic creation of backups before saving any changes.
- A user-friendly interface displaying slide images (macro/label) and form fields for data entry.
- Logic to pre-fill information based on other slides from the same patient/case.
- A command-line interface (CLI) for initializing the database and user accounts.
"""

# ==============================================================================
# 1. IMPORTS
# ==============================================================================
import csv
import codecs
import contextlib
import base64
import datetime
import functools
import hmac
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path, PureWindowsPath
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union
from urllib.parse import urlsplit

import click
from batch_catalog import catalog as batch_catalog, normalize_relative_path
from container_paths import runtime_path
import deidentify_anonymize
import renaming

# Flask and its extensions for web framework, user management
from flask import (
    Flask,
    abort,
    flash,
    get_flashed_messages,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    session,
    url_for,
)
from flask.cli import with_appcontext
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from openpyxl import load_workbook
from openpyxl.worksheet.worksheet import Worksheet


PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
os.umask(0o077)


def _environment_paths(name: str) -> Tuple[str, ...]:
    return tuple(
        item.strip()
        for item in os.environ.get(name, "").split(os.pathsep)
        if item.strip()
    )


# ==============================================================================
# 2. CONFIGURATION
# ==============================================================================
class Config:
    """Central configuration class for the Flask application."""

    # A secret key is required for session management and security.
    SECRET_KEY = os.environ.get("SECRET_KEY")

    # --- Path Configuration ---
    # Robustly determine the project root directory.
    # We assume this file (app.py) is in <project_root>/src
    BASE_DIR = os.path.abspath(os.path.dirname(__file__))
    PROJECT_ROOT = os.path.dirname(BASE_DIR)

    # The base directory where all data (images, CSV) is located.
    # Using absolute path ensures we can run the app from anywhere.
    IMAGE_BASE_DIR = os.environ.get("IMAGE_BASE_DIR", PROJECT_ROOT)

    # The full path to the primary CSV file.
    CSV_FILE_PATH = os.path.join(IMAGE_BASE_DIR, "enriched.csv")
    
    # Directory to store timestamped backups.
    BACKUP_DIR = os.environ.get("BACKUP_DIR", os.path.join(BASE_DIR, "csv_backups"))

    # Instance directory for local data persistence
    INSTANCE_DIR = os.environ.get("INSTANCE_DIR", os.path.join(BASE_DIR, "instance"))
    
    # CSV persistence files
    USERS_CSV_PATH = os.path.join(INSTANCE_DIR, "users.csv")
    QUEUE_CSV_PATH = os.path.join(INSTANCE_DIR, "queue.csv")
    API_DB_PATH = os.path.join(INSTANCE_DIR, "api.sqlite3")
    STATS_DB_PATH = os.path.join(INSTANCE_DIR, "statistics.sqlite3")
    USER_STATS_ROOT = os.path.join(INSTANCE_DIR, "users")
    API_JOB_OUTPUT_DIR = os.path.join(INSTANCE_DIR, "pipeline_job_output")
    BATCH_CATALOG_RECONCILE_SECONDS = int(
        os.environ.get("BATCH_CATALOG_RECONCILE_SECONDS", "60")
    )
    API_REQUIRE_HTTPS = os.environ.get("API_REQUIRE_HTTPS", "true").lower() == "true"
    API_TRUST_PROXY_HEADERS = os.environ.get("API_TRUST_PROXY_HEADERS", "false").lower() == "true"
    API_SUBMIT_RATE_LIMIT = int(os.environ.get("API_SUBMIT_RATE_LIMIT", "5"))
    API_READ_RATE_LIMIT = int(os.environ.get("API_READ_RATE_LIMIT", "60"))
    API_RATE_WINDOW_SECONDS = 60
    API_DEFAULT_TOKEN_DAYS = 90
    API_OUTPUT_DEFAULT_LIMIT = 16 * 1024
    API_OUTPUT_MAX_LIMIT = 64 * 1024
    CSRF_ENABLED = os.environ.get("CSRF_ENABLED", "true").lower() == "true"
    SESSION_COOKIE_SECURE = (
        os.environ.get("SESSION_COOKIE_SECURE", "true").strip().lower() != "false"
    )
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    PIPELINE_INPUT_ROOTS = _environment_paths("PIPELINE_INPUT_ROOTS")
    PIPELINE_OUTPUT_ROOTS = _environment_paths("PIPELINE_OUTPUT_ROOTS")
    PIPELINE_MAX_WORKERS = int(os.environ.get("PIPELINE_MAX_WORKERS", "8"))
    PIPELINE_MAX_THUMBNAIL_DIMENSION = int(
        os.environ.get("PIPELINE_MAX_THUMBNAIL_DIMENSION", "4096")
    )
    LOGIN_PAIR_ATTEMPT_LIMIT = int(os.environ.get("LOGIN_PAIR_ATTEMPT_LIMIT", "5"))
    LOGIN_ACCOUNT_ATTEMPT_LIMIT = int(
        os.environ.get("LOGIN_ACCOUNT_ATTEMPT_LIMIT", "10")
    )
    LOGIN_RATE_WINDOW_SECONDS = int(
        os.environ.get("LOGIN_RATE_WINDOW_SECONDS", "900")
    )
    APP_LOG_DIR = os.environ.get("APP_LOG_DIR", os.path.join(BASE_DIR, "logs"))

    # Slide Digitization Log workbook configuration.
    SDL_FILE_PATH = os.environ.get(
        "SDL_FILE_PATH",
        os.path.join(BASE_DIR, "logs", "Slide_Digitization_Log.xlsx"),
    )
    SDL_SHEET_NAME = "general"
    SDL_ORGANS = ("BRAIN", "BREAST", "TESTIS", "OTHER", "UNKNOWN", "CYTO")
    SDL_SCANNERS = ("-----", "RSCH1 (SS12797)", "CLIN1 (SS12602)")
    TQ_EXECUTABLE = os.environ.get(
        "TQ_EXECUTABLE", "tq.exe" if os.name == "nt" else "tq"
    )
    TQ_HOME_DIR = os.environ.get("TQ_HOME_DIR", str(Path.home() / ".tq"))
    TQ_TRANSFER_LOG_DIR = os.environ.get("TQ_TRANSFER_LOG_DIR", "")
    IMAGE_STAGING_ROOT = os.environ.get(
        "IMAGE_STAGING_ROOT",
        r"D:\image_staging" if os.name == "nt" else "/data/image-staging",
    )
    IMAGE_STAGING_HOST_DISPLAY = os.environ.get(
        "IMAGE_STAGING_HOST_DISPLAY", IMAGE_STAGING_ROOT
    )


    # Path to scanner inventories
    SCANNER_INVENTORIES = os.environ.get(
        "SCANNER_INVENTORIES", "D:\\scanner_inventories"
    )
    # Path to batches of new slides to process in InSlide
    INSLIDE_BATCHES = os.environ.get(
        "INSLIDE_BATCHES", "D:\\label_check_batches"
    )
    COPATH_CLONE = os.environ.get("COPATH_CLONE", "D:\\copath_clone")

    # Default password for the initial 'admin' user.
    ADMIN_DEFAULT_PASSWORD = os.environ.get("ADMIN_DEFAULT_PASSWORD")

    # --- Queue Settings ---
    # The duration (in seconds) a user can hold a "lease" on a queue item before it's
    # automatically returned to the pool for others.
    LEASE_DURATION_SECONDS = 300  # 5 minutes


def _make_private_directory(path: Union[str, Path]) -> Path:
    directory = Path(path)
    if directory.is_symlink():
        raise RuntimeError(f"Sensitive runtime directory cannot be a symbolic link: {directory}")
    directory.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIRECTORY_MODE)
    _set_private_mode(directory, PRIVATE_DIRECTORY_MODE)
    return directory


def _set_private_mode(path: Path, expected_mode: int) -> bool:
    """Apply POSIX mode, tolerating unsupported Docker Desktop bind mounts."""
    try:
        os.chmod(path, expected_mode)
        return True
    except PermissionError:
        containerized = os.environ.get("INSLIDE_CONTAINER", "false").lower() == "true"
        required_access = os.R_OK | os.W_OK
        if path.is_dir():
            required_access |= os.X_OK
        if not containerized or not os.access(path, required_access):
            raise
        logging.getLogger(__name__).warning(
            "POSIX permissions are unsupported for bind-mounted runtime path %s; "
            "enforce access through Windows ACLs",
            path,
        )
        return False


def _verify_private_mode(path: Path, expected_mode: int) -> None:
    if os.name == "nt":
        return
    actual_mode = stat.S_IMODE(path.stat().st_mode)
    if actual_mode & 0o077 or actual_mode != expected_mode:
        raise RuntimeError(
            f"Sensitive runtime path has insecure permissions: {path} ({actual_mode:o})"
        )


def harden_runtime_permissions(
    instance_dir: Union[str, Path], log_dir: Union[str, Path]
) -> None:
    """Create and repair private application state without following symlinks."""
    instance = _make_private_directory(instance_dir)
    logs = _make_private_directory(log_dir)

    for root, directories, files in os.walk(instance, followlinks=False):
        root_path = Path(root)
        if root_path.is_symlink():
            raise RuntimeError(
                f"Sensitive runtime directory cannot be a symbolic link: {root_path}"
            )
        if _set_private_mode(root_path, PRIVATE_DIRECTORY_MODE):
            _verify_private_mode(root_path, PRIVATE_DIRECTORY_MODE)
        for name in directories:
            child = root_path / name
            if child.is_symlink():
                raise RuntimeError(
                    f"Sensitive runtime path cannot be a symbolic link: {child}"
                )
        for name in files:
            child = root_path / name
            if child.is_symlink():
                raise RuntimeError(
                    f"Sensitive runtime path cannot be a symbolic link: {child}"
                )
            if _set_private_mode(child, PRIVATE_FILE_MODE):
                _verify_private_mode(child, PRIVATE_FILE_MODE)

    for child in logs.glob("app.log*"):
        if child.is_symlink():
            raise RuntimeError(f"Log file cannot be a symbolic link: {child}")
        if child.is_file():
            if _set_private_mode(child, PRIVATE_FILE_MODE):
                _verify_private_mode(child, PRIVATE_FILE_MODE)
    if _set_private_mode(logs, PRIVATE_DIRECTORY_MODE):
        _verify_private_mode(logs, PRIVATE_DIRECTORY_MODE)


# ==============================================================================
# 3. LOGGING SETUP
# ==============================================================================
def setup_logging(app: Flask) -> None:
    """Configures comprehensive logging for the application."""
    log_dir = _make_private_directory(app.config["APP_LOG_DIR"])
    log_path = log_dir / "app.log"
    file_handler = RotatingFileHandler(log_path, maxBytes=102400, backupCount=10)
    _set_private_mode(log_path, PRIVATE_FILE_MODE)
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]"
        )
    )
    file_handler.setLevel(logging.INFO)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(
        logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    )
    console_handler.setLevel(logging.INFO)

    app.logger.addHandler(file_handler)
    app.logger.addHandler(console_handler)
    app.logger.setLevel(logging.INFO)
    app.logger.info("Application startup")


# ==============================================================================
# 4. APPLICATION & EXTENSIONS INITIALIZATION
# ==============================================================================
base_dir = os.path.abspath(os.path.dirname(__file__))
template_dir = os.path.join(base_dir, "templates")

app = Flask(__name__, template_folder=template_dir, instance_path=Config.INSTANCE_DIR)
app.config.from_object(Config)
if app.config["API_TRUST_PROXY_HEADERS"]:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
harden_runtime_permissions(app.config["INSTANCE_DIR"], app.config["APP_LOG_DIR"])

setup_logging(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"
login_manager.login_message = "Please log in to access this page."
login_manager.login_message_category = "warning"


# ==============================================================================
# 5. CUSTOM EXCEPTIONS
# ==============================================================================
class DataLoadError(Exception):
    pass

class DataSaveError(Exception):
    pass

class BackupError(Exception):
    pass

class SDLWorkbookError(Exception):
    pass

class SDLValidationError(Exception):
    pass


class InventoryReadError(Exception):
    pass


class TQError(Exception):
    pass


class SecurityConfigurationError(Exception):
    pass


_INSECURE_SECRET_KEYS = {
    "a-super-secret-key-that-you-should-change",
    "replace-with-a-long-random-value",
}
_INSECURE_ADMIN_PASSWORDS = {
    "change_this_password",
    "replace-before-first-start",
}
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 128


def password_policy_error(password: Any) -> Optional[str]:
    if not isinstance(password, str):
        return "Password must be text."
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must contain at least {MIN_PASSWORD_LENGTH} characters."
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"Password must contain at most {MAX_PASSWORD_LENGTH} characters."
    if password in _INSECURE_ADMIN_PASSWORDS:
        return "Password must not use a documented placeholder."
    return None


def validate_security_config(configuration: Optional[Dict[str, Any]] = None) -> None:
    """Reject missing, weak, or documented-placeholder production credentials."""
    if configuration is None:
        configuration = app.config
    errors = []
    secret_key = configuration.get("SECRET_KEY")
    admin_password = configuration.get("ADMIN_DEFAULT_PASSWORD")

    if not isinstance(secret_key, str) or len(secret_key) < 32:
        errors.append("SECRET_KEY must contain at least 32 characters")
    elif secret_key in _INSECURE_SECRET_KEYS:
        errors.append("SECRET_KEY must not use a documented placeholder or legacy default")

    password_error = password_policy_error(admin_password)
    if password_error:
        errors.append(f"ADMIN_DEFAULT_PASSWORD: {password_error}")

    for key in ("PIPELINE_INPUT_ROOTS", "PIPELINE_OUTPUT_ROOTS"):
        roots = configuration.get(key)
        if not roots:
            errors.append(f"{key} must contain at least one directory")

    for key in (
        "PIPELINE_MAX_WORKERS",
        "PIPELINE_MAX_THUMBNAIL_DIMENSION",
        "LOGIN_PAIR_ATTEMPT_LIMIT",
        "LOGIN_ACCOUNT_ATTEMPT_LIMIT",
        "LOGIN_RATE_WINDOW_SECONDS",
    ):
        value = configuration.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            errors.append(f"{key} must be a positive integer")

    if errors:
        raise SecurityConfigurationError("; ".join(errors))


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _iso_utc(value: Optional[datetime.datetime] = None) -> str:
    return (value or _utcnow()).isoformat().replace("+00:00", "Z")


class APIStore:
    """SQLite-backed API credentials, durable job metadata, and rate counters."""

    def __init__(self, db_path: str, output_dir: str):
        self.db_path = db_path
        self.output_dir = output_dir
        self._initialized_path: Optional[str] = None
        self._init_lock = threading.Lock()

    def configure(self, db_path: str, output_dir: str) -> None:
        self.db_path = db_path
        self.output_dir = output_dir
        self._initialized_path = None

    def _connect(self) -> sqlite3.Connection:
        self._ensure_schema()
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    @contextlib.contextmanager
    def connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _ensure_schema(self) -> None:
        if self._initialized_path == self.db_path:
            return
        with self._init_lock:
            if self._initialized_path == self.db_path:
                return
            database_path = Path(self.db_path)
            if database_path.is_symlink():
                raise RuntimeError(
                    f"Sensitive runtime database cannot be a symbolic link: {database_path}"
                )
            database_directory = _make_private_directory(database_path.parent)
            output_directory = _make_private_directory(self.output_dir)
            connection = sqlite3.connect(self.db_path, timeout=30)
            try:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS api_tokens (
                        token_id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        label TEXT NOT NULL,
                        secret_hash TEXT NOT NULL,
                        scopes TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        revoked_at TEXT,
                        last_used_at TEXT
                    );
                    CREATE TABLE IF NOT EXISTS pipeline_jobs (
                        job_id TEXT PRIMARY KEY,
                        owner_id TEXT NOT NULL,
                        token_id TEXT,
                        idempotency_key TEXT,
                        payload_hash TEXT,
                        request_json TEXT NOT NULL,
                        command_json TEXT NOT NULL,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        started_at TEXT,
                        completed_at TEXT,
                        return_code INTEGER,
                        output_path TEXT NOT NULL,
                        launcher_pid INTEGER,
                        UNIQUE(token_id, idempotency_key)
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS one_active_pipeline_job
                    ON pipeline_jobs((1)) WHERE status IN ('starting', 'running');
                    CREATE TABLE IF NOT EXISTS api_rate_limits (
                        token_id TEXT NOT NULL,
                        bucket TEXT NOT NULL,
                        window_start INTEGER NOT NULL,
                        request_count INTEGER NOT NULL,
                        PRIMARY KEY(token_id, bucket)
                    );
                    CREATE TABLE IF NOT EXISTS login_rate_limits (
                        scope TEXT NOT NULL,
                        identifier_hash TEXT NOT NULL,
                        window_start INTEGER NOT NULL,
                        request_count INTEGER NOT NULL,
                        PRIMARY KEY(scope, identifier_hash)
                    );
                    """
                )
                connection.commit()
            finally:
                connection.close()
            os.chmod(database_path, PRIVATE_FILE_MODE)
            _verify_private_mode(database_directory, PRIVATE_DIRECTORY_MODE)
            _verify_private_mode(output_directory, PRIVATE_DIRECTORY_MODE)
            _verify_private_mode(database_path, PRIVATE_FILE_MODE)
            self._initialized_path = self.db_path

    def create_token(
        self, user_id: str, label: str, scopes: List[str], expires_days: int
    ) -> Tuple[str, Dict[str, Any]]:
        token_id = uuid.uuid4().hex[:16]
        raw_token = f"lc_pat_{token_id}.{secrets.token_urlsafe(32)}"
        created = _utcnow()
        expires = created + datetime.timedelta(days=expires_days)
        record = {
            "token_id": token_id,
            "user_id": user_id,
            "label": label,
            "scopes": sorted(set(scopes)),
            "created_at": _iso_utc(created),
            "expires_at": _iso_utc(expires),
            "revoked_at": None,
            "last_used_at": None,
        }
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO api_tokens
                   (token_id, user_id, label, secret_hash, scopes, created_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    token_id,
                    user_id,
                    label,
                    hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
                    " ".join(record["scopes"]),
                    record["created_at"],
                    record["expires_at"],
                ),
            )
        return raw_token, record

    def list_tokens(self, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        query = "SELECT * FROM api_tokens"
        params: Tuple[Any, ...] = ()
        if user_id:
            query += " WHERE user_id = ?"
            params = (user_id,)
        query += " ORDER BY created_at DESC"
        with self.connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._token_record(row) for row in rows]

    @staticmethod
    def _token_record(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "token_id": row["token_id"],
            "user_id": row["user_id"],
            "label": row["label"],
            "scopes": row["scopes"].split(),
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "revoked_at": row["revoked_at"],
            "last_used_at": row["last_used_at"],
        }

    def authenticate_token(self, raw_token: str) -> Optional[Dict[str, Any]]:
        match = re.fullmatch(r"lc_pat_([0-9a-f]{16})\.[A-Za-z0-9_-]+", raw_token)
        if not match:
            return None
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM api_tokens WHERE token_id = ?", (match.group(1),)
            ).fetchone()
            if row is None or row["revoked_at"]:
                return None
            expected = row["secret_hash"]
            actual = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
            if not hmac.compare_digest(expected, actual):
                return None
            expires = datetime.datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
            if expires <= _utcnow():
                return None
            used_at = _iso_utc()
            connection.execute(
                "UPDATE api_tokens SET last_used_at = ? WHERE token_id = ?",
                (used_at, row["token_id"]),
            )
            record = self._token_record(row)
            record["last_used_at"] = used_at
            return record

    def revoke_token(self, token_id: str) -> bool:
        with self.connection() as connection:
            cursor = connection.execute(
                "UPDATE api_tokens SET revoked_at = ? WHERE token_id = ? AND revoked_at IS NULL",
                (_iso_utc(), token_id),
            )
            return cursor.rowcount == 1

    def rate_limit(self, token_id: str, bucket: str, limit: int, window: int) -> Tuple[bool, int, int]:
        now = int(time.time())
        window_start = now - (now % window)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT window_start, request_count FROM api_rate_limits WHERE token_id=? AND bucket=?",
                (token_id, bucket),
            ).fetchone()
            count = 0 if row is None or row["window_start"] != window_start else row["request_count"]
            allowed = count < limit
            if allowed:
                count += 1
                connection.execute(
                    """INSERT INTO api_rate_limits(token_id, bucket, window_start, request_count)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(token_id, bucket) DO UPDATE SET
                       window_start=excluded.window_start, request_count=excluded.request_count""",
                    (token_id, bucket, window_start, count),
                )
        return allowed, max(0, limit - count), window_start + window - now

    @staticmethod
    def _login_limit_keys(username: str, client_address: str) -> Dict[str, str]:
        normalized_username = username.casefold()
        return {
            "pair": hashlib.sha256(
                f"{normalized_username}\0{client_address}".encode("utf-8")
            ).hexdigest(),
            "account": hashlib.sha256(normalized_username.encode("utf-8")).hexdigest(),
        }

    def login_rate_limit(
        self,
        username: str,
        client_address: str,
        pair_limit: int,
        account_limit: int,
        window: int,
    ) -> Tuple[bool, int]:
        now = int(time.time())
        window_start = now - (now % window)
        retry_after = window_start + window - now
        keys = self._login_limit_keys(username, client_address)
        limits = {"pair": pair_limit, "account": account_limit}
        with self.connection() as connection:
            for scope, identifier_hash in keys.items():
                row = connection.execute(
                    """SELECT window_start, request_count FROM login_rate_limits
                       WHERE scope=? AND identifier_hash=?""",
                    (scope, identifier_hash),
                ).fetchone()
                if (
                    row is not None
                    and row["window_start"] == window_start
                    and row["request_count"] >= limits[scope]
                ):
                    return False, retry_after
        return True, retry_after

    def record_login_failure(
        self, username: str, client_address: str, window: int
    ) -> None:
        now = int(time.time())
        window_start = now - (now % window)
        keys = self._login_limit_keys(username, client_address)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM login_rate_limits WHERE window_start < ?",
                (window_start,),
            )
            for scope, identifier_hash in keys.items():
                connection.execute(
                    """INSERT INTO login_rate_limits
                       (scope, identifier_hash, window_start, request_count)
                       VALUES (?, ?, ?, 1)
                       ON CONFLICT(scope, identifier_hash) DO UPDATE SET
                         window_start=excluded.window_start,
                         request_count=CASE
                           WHEN login_rate_limits.window_start=excluded.window_start
                           THEN login_rate_limits.request_count + 1
                           ELSE 1
                         END""",
                    (scope, identifier_hash, window_start),
                )

    def clear_login_failures(self, username: str, client_address: str) -> None:
        keys = self._login_limit_keys(username, client_address)
        with self.connection() as connection:
            connection.executemany(
                "DELETE FROM login_rate_limits WHERE scope=? AND identifier_hash=?",
                keys.items(),
            )

    def find_idempotent(self, token_id: str, key: str) -> Optional[sqlite3.Row]:
        with self.connection() as connection:
            return connection.execute(
                "SELECT * FROM pipeline_jobs WHERE token_id=? AND idempotency_key=?",
                (token_id, key),
            ).fetchone()

    def reserve_job(
        self,
        job_id: str,
        owner_id: str,
        values: Dict[str, Any],
        command: List[str],
        token_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        payload_hash: Optional[str] = None,
    ) -> str:
        self._ensure_schema()
        output_path = str(Path(self.output_dir) / f"{job_id}.log")
        descriptor = os.open(
            output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, PRIVATE_FILE_MODE
        )
        os.close(descriptor)
        try:
            with self.connection() as connection:
                connection.execute(
                    """INSERT INTO pipeline_jobs
                       (job_id, owner_id, token_id, idempotency_key, payload_hash,
                        request_json, command_json, status, created_at, output_path)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'starting', ?, ?)""",
                    (
                        job_id, owner_id, token_id, idempotency_key, payload_hash,
                        json.dumps(values, sort_keys=True), json.dumps(command), _iso_utc(), output_path,
                    ),
                )
        except Exception:
            os.remove(output_path)
            raise
        return output_path

    def update_job(self, job_id: str, **fields: Any) -> None:
        allowed = {"status", "started_at", "completed_at", "return_code", "launcher_pid"}
        selected = {key: value for key, value in fields.items() if key in allowed}
        if not selected:
            return
        assignments = ", ".join(f"{key}=?" for key in selected)
        with self.connection() as connection:
            connection.execute(
                f"UPDATE pipeline_jobs SET {assignments} WHERE job_id=?",
                (*selected.values(), job_id),
            )

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM pipeline_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        return dict(row) if row else None

    def mark_stale_jobs_interrupted(self) -> None:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT job_id, launcher_pid FROM pipeline_jobs WHERE status IN ('starting', 'running')"
            ).fetchall()
            for row in rows:
                launcher_pid = row["launcher_pid"]
                if launcher_pid:
                    try:
                        os.kill(launcher_pid, 0)
                        continue
                    except (OSError, ProcessLookupError):
                        pass
                connection.execute(
                    """UPDATE pipeline_jobs SET status='interrupted', completed_at=?
                       WHERE job_id=? AND status IN ('starting', 'running')""",
                    (_iso_utc(), row["job_id"]),
                )


class StatisticsStore:
    """Durable per-user activity counters and nightly CSV materialization."""

    METRICS = {"slides_completed", "accessions_logged"}

    def __init__(self, db_path: str, user_root: str):
        self.db_path = db_path
        self.user_root = user_root
        self._initialized_path: Optional[str] = None
        self._init_lock = threading.Lock()

    def configure(self, db_path: str, user_root: str) -> None:
        self.db_path = db_path
        self.user_root = user_root
        self._initialized_path = None

    def _ensure_schema(self) -> None:
        if self._initialized_path == self.db_path:
            return
        with self._init_lock:
            if self._initialized_path == self.db_path:
                return
            parent = Path(self.db_path).parent
            _make_private_directory(parent)
            connection = sqlite3.connect(self.db_path, timeout=30)
            try:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS user_days (
                        user_id TEXT NOT NULL,
                        activity_date TEXT NOT NULL,
                        PRIMARY KEY (user_id, activity_date)
                    );
                    CREATE TABLE IF NOT EXISTS daily_statistics (
                        user_id TEXT NOT NULL,
                        activity_date TEXT NOT NULL,
                        slides_completed INTEGER NOT NULL DEFAULT 0,
                        accessions_logged INTEGER NOT NULL DEFAULT 0,
                        active_minutes INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (user_id, activity_date)
                    );
                    CREATE TABLE IF NOT EXISTS heartbeat_state (
                        user_id TEXT PRIMARY KEY,
                        last_minute_bucket INTEGER NOT NULL
                    );
                    """
                )
                connection.commit()
            finally:
                connection.close()
            os.chmod(self.db_path, PRIVATE_FILE_MODE)
            self._initialized_path = self.db_path

    @contextlib.contextmanager
    def connection(self):
        self._ensure_schema()
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def local_date() -> datetime.date:
        return datetime.date.today()

    def note_presence(
        self, user_id: str, activity_date: Optional[datetime.date] = None
    ) -> None:
        date_text = (activity_date or self.local_date()).isoformat()
        with self.connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO user_days (user_id, activity_date) VALUES (?, ?)",
                (str(user_id), date_text),
            )

    def increment(
        self,
        user_id: str,
        metric: str,
        amount: int = 1,
        activity_date: Optional[datetime.date] = None,
    ) -> None:
        if metric not in self.METRICS:
            raise ValueError(f"Unknown statistics metric: {metric}")
        date_text = (activity_date or self.local_date()).isoformat()
        with self.connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO user_days (user_id, activity_date) VALUES (?, ?)",
                (str(user_id), date_text),
            )
            connection.execute(
                f"""INSERT INTO daily_statistics
                    (user_id, activity_date, {metric}) VALUES (?, ?, ?)
                    ON CONFLICT(user_id, activity_date) DO UPDATE SET
                    {metric}={metric}+excluded.{metric}""",
                (str(user_id), date_text, int(amount)),
            )

    def heartbeat(
        self, user_id: str, now: Optional[datetime.datetime] = None
    ) -> bool:
        current = now or datetime.datetime.now()
        date_text = current.date().isoformat()
        minute_bucket = int(current.timestamp() // 60)
        credited = False
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT OR IGNORE INTO user_days (user_id, activity_date) VALUES (?, ?)",
                (str(user_id), date_text),
            )
            state = connection.execute(
                "SELECT last_minute_bucket FROM heartbeat_state WHERE user_id=?",
                (str(user_id),),
            ).fetchone()
            if state is None or minute_bucket > state["last_minute_bucket"]:
                connection.execute(
                    """INSERT INTO heartbeat_state (user_id, last_minute_bucket)
                       VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET
                       last_minute_bucket=excluded.last_minute_bucket""",
                    (str(user_id), minute_bucket),
                )
                connection.execute(
                    """INSERT INTO daily_statistics
                       (user_id, activity_date, active_minutes) VALUES (?, ?, 1)
                       ON CONFLICT(user_id, activity_date) DO UPDATE SET
                       active_minutes=active_minutes+1""",
                    (str(user_id), date_text),
                )
                credited = True
        return credited

    @staticmethod
    def rounded_hours(active_minutes: int) -> int:
        return (int(active_minutes) + 30) // 60

    @staticmethod
    def _storage_key(user_id: str) -> str:
        encoded = base64.urlsafe_b64encode(str(user_id).encode("utf-8"))
        return "u_" + encoded.decode("ascii").rstrip("=")

    def csv_path(self, user_id: str) -> Path:
        return Path(self.user_root) / self._storage_key(user_id) / "logs" / "lifetime_stats.csv"

    def _database_rows(self, user_id: str) -> Dict[str, Dict[str, int]]:
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT u.activity_date,
                          COALESCE(d.slides_completed, 0) AS slides_completed,
                          COALESCE(d.accessions_logged, 0) AS accessions_logged,
                          COALESCE(d.active_minutes, 0) AS active_minutes
                   FROM user_days AS u
                   LEFT JOIN daily_statistics AS d
                     ON d.user_id=u.user_id AND d.activity_date=u.activity_date
                   WHERE u.user_id=? ORDER BY u.activity_date""",
                (str(user_id),),
            ).fetchall()
        values = {str(row["activity_date"]): dict(row) for row in rows if row["activity_date"]}
        return values

    def read_csv(self, user_id: str) -> List[Dict[str, Any]]:
        path = self.csv_path(user_id)
        if not path.is_file():
            return []
        with open(path, newline="", encoding="utf-8") as handle:
            return [
                {
                    "date": row["date"],
                    "slides_completed": int(row["slides_completed"]),
                    "accessions_logged": int(row["accessions_logged"]),
                    "hours": int(row["hours"]),
                }
                for row in csv.DictReader(handle)
            ]

    def rollup_user(
        self, user_id: str, through_date: Optional[datetime.date] = None
    ) -> Path:
        cutoff = through_date or (self.local_date() - datetime.timedelta(days=1))
        rows = self._database_rows(user_id)
        completed = []
        for date_text, row in sorted(rows.items()):
            if datetime.date.fromisoformat(date_text) > cutoff:
                continue
            completed.append(
                {
                    "date": date_text,
                    "slides_completed": int(row.get("slides_completed") or 0),
                    "accessions_logged": int(row.get("accessions_logged") or 0),
                    "hours": self.rounded_hours(int(row.get("active_minutes") or 0)),
                }
            )
        path = self.csv_path(user_id)
        _make_private_directory(path.parent.parent)
        _make_private_directory(path.parent)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with open(temporary, "w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=("date", "slides_completed", "accessions_logged", "hours"),
                )
                writer.writeheader()
                writer.writerows(completed)
            os.chmod(temporary, PRIVATE_FILE_MODE)
            os.replace(temporary, path)
            os.chmod(path, PRIVATE_FILE_MODE)
        finally:
            if temporary.exists():
                temporary.unlink()
        return path

    def dashboard(self, user_id: str) -> Dict[str, Any]:
        today = self.local_date()
        csv_rows = {row["date"]: row for row in self.read_csv(user_id)}
        database_rows = self._database_rows(user_id)
        today_values = database_rows.get(today.isoformat(), {})
        csv_rows[today.isoformat()] = {
            "date": today.isoformat(),
            "slides_completed": int(today_values.get("slides_completed") or 0),
            "accessions_logged": int(today_values.get("accessions_logged") or 0),
            "hours": self.rounded_hours(int(today_values.get("active_minutes") or 0)),
        }
        week = []
        for offset in range(6, -1, -1):
            date_text = (today - datetime.timedelta(days=offset)).isoformat()
            week.append(
                csv_rows.get(
                    date_text,
                    {"date": date_text, "slides_completed": 0, "accessions_logged": 0, "hours": 0},
                )
            )
        totals = {
            metric: sum(int(row[metric]) for row in csv_rows.values())
            for metric in ("slides_completed", "accessions_logged", "hours")
        }
        return {"week": week, "totals": totals}


api_store = APIStore(Config.API_DB_PATH, Config.API_JOB_OUTPUT_DIR)
if os.environ.get("INSLIDE_STATS_SCHEDULER") != "true":
    api_store.mark_stale_jobs_interrupted()
stats_store = StatisticsStore(Config.STATS_DB_PATH, Config.USER_STATS_ROOT)


# ==============================================================================
# 6. PERSISTENCE MODELS (CSV BASED)
# ==============================================================================
class User(UserMixin):
    """Represents a user account."""
    def __init__(self, id: str, password_hash: str, correction_count: int = 0, is_admin: bool = False):
        self.id = id
        self.password_hash = password_hash
        # correction_count remains an accepted argument for legacy callers only.
        # Handle string 'True'/'False' from CSV loading
        if isinstance(is_admin, str):
            self.is_admin = is_admin.lower() == 'true'
        else:
            self.is_admin = bool(is_admin)

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def verify_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    @property
    def slides_completed(self) -> int:
        return int(stats_store.dashboard(str(self.id))["totals"]["slides_completed"])

    def to_dict(self) -> Dict[str, str]:
        return {
            "id": self.id,
            "password_hash": self.password_hash,
            "is_admin": str(self.is_admin)
        }

    def __repr__(self) -> str:
        return f"<User {self.id}>"


class QueueItem:
    """Represents a single row from the CSV in the processing queue."""
    def __init__(self, original_index: int, status: str = "pending", 
                 leased_by_id: Optional[str] = None, leased_at: Optional[Union[str, datetime.datetime]] = None,
                 completed_by_id: Optional[str] = None, completed_at: Optional[Union[str, datetime.datetime]] = None,
                 row_id: Optional[int] = None):
        self.id = row_id # ID is strictly internal/optional for QueueItem in this CSV context, but we keep track if needed.
        self.original_index = int(original_index)
        self.status = status
        self.leased_by_id = leased_by_id if leased_by_id != "" else None
        
        # Date parsing logic
        self.leased_at = self._parse_date(leased_at)
        self.completed_by_id = completed_by_id if completed_by_id != "" else None
        self.completed_at = self._parse_date(completed_at)

    def _parse_date(self, date_val: Union[str, datetime.datetime, None]) -> Optional[datetime.datetime]:
        if not date_val:
            return None
        if isinstance(date_val, datetime.datetime):
            return date_val
        try:
            return datetime.datetime.fromisoformat(date_val)
        except ValueError:
            return None

    def _format_date(self, date_val: Optional[datetime.datetime]) -> str:
        return date_val.isoformat() if date_val else ""

    def to_dict(self) -> Dict[str, str]:
        return {
            "original_index": str(self.original_index),
            "status": self.status,
            "leased_by_id": self.leased_by_id if self.leased_by_id else "",
            "leased_at": self._format_date(self.leased_at),
            "completed_by_id": self.completed_by_id if self.completed_by_id else "",
            "completed_at": self._format_date(self.completed_at)
        }

    @property
    def leased_by(self):
        """Helper to resolve user object for template compatibility."""
        if self.leased_by_id:
            # Access global user_manager
            return user_manager.get(self.leased_by_id)
        return None

    def __repr__(self) -> str:
        return f"<QueueItem {self.original_index} - {self.status}>"


class CSVManager:
    """Generic CSV persistence manager."""
    def __init__(self, filepath: str, fieldnames: List[str]):
        self.filepath = filepath
        self.fieldnames = fieldnames
        self._lock = threading.Lock()

    def _ensure_file(self):
        if not os.path.exists(self.filepath):
            descriptor = os.open(
                self.filepath, os.O_WRONLY | os.O_CREAT | os.O_EXCL, PRIVATE_FILE_MODE
            )
            with os.fdopen(descriptor, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=self.fieldnames)
                writer.writeheader()

    def read_all(self) -> List[Dict[str, str]]:
        self._ensure_file()
        with self._lock:
            try:
                with open(self.filepath, 'r', newline='', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    return list(reader)
            except Exception as e:
                app.logger.error(f"Error reading {self.filepath}: {e}")
                return []

    def write_all(self, data: List[Dict[str, str]]) -> None:
        with self._lock:
            try:
                # write atomic
                temp_path = self.filepath + ".tmp"
                with open(temp_path, 'w', newline='', encoding='utf-8') as f:
                    writer = csv.DictWriter(f, fieldnames=self.fieldnames)
                    writer.writeheader()
                    writer.writerows(data)
                os.chmod(temp_path, PRIVATE_FILE_MODE)
                os.replace(temp_path, self.filepath)
                os.chmod(self.filepath, PRIVATE_FILE_MODE)
            except Exception as e:
                app.logger.error(f"Error writing {self.filepath}: {e}")
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                raise DataSaveError(f"Failed to save CSV {self.filepath}: {e}")


class UserManager(CSVManager):
    def __init__(self):
        super().__init__(Config.USERS_CSV_PATH, ["id", "password_hash", "is_admin"])
        # Cache users in memory for performance, similar to DB
        self.users: Dict[str, User] = {}
        self.load()

    def load(self):
        rows = self.read_all()
        self.users = {}
        for row in rows:
            u = User(
                id=row["id"],
                password_hash=row["password_hash"],
                is_admin=row["is_admin"]
            )
            self.users[u.id] = u

    def save(self):
        data = [u.to_dict() for u in self.users.values()]
        self.write_all(data)

    def get(self, user_id: str) -> Optional[User]:
        return self.users.get(user_id)

    def add(self, user: User):
        self.users[user.id] = user
        self.save()  # Auto-save on add due to simple architecture

    def update(self, user: User):
        self.users[user.id] = user
        self.save()
    
    def get_all(self) -> List[User]:
        return list(self.users.values())


class QueueManager:
    """In-memory queue adapter persisted in the central batch catalog."""

    def __init__(self, batch_id: str):
        self.batch_id = batch_id
        self.items: Dict[int, QueueItem] = {}
        self._snapshot: Dict[int, Dict[str, str]] = {}

    def load(self):
        rows = batch_catalog.load_queue(Config.INSTANCE_DIR, self.batch_id)
        self.items = {}
        for row in rows:
            try:
                idx = int(row["original_index"])
                item = QueueItem(
                    original_index=idx,
                    status=row["status"],
                    leased_by_id=row.get("leased_by_id"),
                    leased_at=row.get("leased_at"),
                    completed_by_id=row.get("completed_by_id"),
                    completed_at=row.get("completed_at"),
                )
                self.items[idx] = item
            except ValueError:
                continue
        self._snapshot = {index: item.to_dict() for index, item in self.items.items()}

    def save(self):
        current = {index: item.to_dict() for index, item in self.items.items()}
        changed = [row for index, row in current.items() if self._snapshot.get(index) != row]
        deleted = set(self._snapshot).difference(current)
        batch_catalog.apply_queue_changes(
            Config.INSTANCE_DIR,
            self.batch_id,
            changed,
            deleted,
        )
        self._snapshot = current

    def get(self, original_index: int) -> Optional[QueueItem]:
        return self.items.get(original_index)
    
    def add(self, item: QueueItem):
        self.items[item.original_index] = item
        # Batch add usually calls save manually, but for single integrity:
        # self.save() 
    
    def get_all(self) -> List[QueueItem]:
        return list(self.items.values())

    def update(self):
        """Persist current state."""
        self.save()

    def claim(self, user_id: str, original_index: Optional[int] = None) -> Optional[QueueItem]:
        row = batch_catalog.claim_item(
            Config.INSTANCE_DIR,
            self.batch_id,
            user_id,
            datetime.datetime.utcnow().isoformat(),
            original_index,
        )
        self.load()
        return self.items.get(int(row["original_index"])) if row else None

    def release_expired(self, before: datetime.datetime) -> int:
        count = batch_catalog.release_expired(
            Config.INSTANCE_DIR, self.batch_id, before.isoformat()
        )
        if count:
            self.load()
        return count

    def release_user(self, user_id: str) -> int:
        count = batch_catalog.release_user(Config.INSTANCE_DIR, self.batch_id, user_id)
        self.load()
        return count


# Initialize Managers
user_manager = UserManager()


@login_manager.user_loader
def load_user(user_id: str) -> Optional[User]:
    return user_manager.get(user_id)


# ==============================================================================
# 7. DATA MANAGER
# ==============================================================================
class DataManager:
    """Manages the in-memory CSV data state, loading, and saving."""
    def __init__(self, batch_root: Optional[Path] = None, csv_path: Optional[Path] = None):
        self.data: List[Dict[str, Any]] = []
        self.headers: List[str] = []
        self.batch_root = batch_root
        self.csv_path = csv_path
        self._lock = threading.Lock() # Ensure thread safety for data access
        self.critical_headers = ["AccessionID", "Stain", "ParsingQCPassed", "original_slide_path"]

    def load_data(self, file_path: Optional[Union[str, Path]] = None) -> None:
        """Loads CSV data into memory safely."""
        file_path = str(file_path or self.csv_path or Config.CSV_FILE_PATH)
        with self._lock:
            app.logger.info(f"Loading CSV data from: {file_path}")
            if not os.path.exists(file_path):
                raise DataLoadError(f"CSV file not found: {file_path}")

            _data: List[Dict[str, Any]] = []
            try:
                with open(file_path, "r", newline="", encoding="utf-8") as csvfile:
                    reader = csv.DictReader(csvfile, delimiter=",")
                    _headers = reader.fieldnames

                    if not _headers:
                        raise DataLoadError("CSV file is empty or has no header.")
                    
                    missing = [h for h in self.critical_headers if h not in _headers]
                    if missing:
                        app.logger.warning(
                            f"CSV is missing expected headers: {missing}. Functionality may be limited."
                        )

                    for i, row in enumerate(reader):
                        row["_original_index"] = i
                        orig_path = row.get("original_slide_location")
                        row["_identifier"] = Path(orig_path).stem if orig_path else f"Unknown_{i}"
                        row["_label_text"] = row.get("label_text", "N/A")
                        row["_macro_text"] = row.get("macro_text", "N/A")
                        row["_label_path"] = row.get("label_path")
                        row["_macro_path"] = row.get("macro_path")
                        
                        # Preserve QC field contents exactly as stored. Validation must
                        # catch whitespace and unsafe characters instead of silently
                        # repairing them while loading the CSV.
                        row["AccessionID"] = row.get("AccessionID", "")
                        row["Stain"] = row.get("Stain", "")
                        row["BlockNumber"] = row.get("BlockNumber", "")
                        
                        qc_passed_str = row.get("ParsingQCPassed", "").strip()
                        row["_is_complete"] = bool(
                            qc_passed_str and qc_passed_str.lower() != "false"
                        )
                        _data.append(row)

                # Post-processing: Calculate per-patient file statistics
                patient_slide_ids = defaultdict(list)
                for i, row in enumerate(_data):
                    patient_slide_ids[row["_identifier"]].append(i)
                
                for _, original_indices in patient_slide_ids.items():
                    total = len(original_indices)
                    for j, original_idx in enumerate(sorted(original_indices)):
                        _data[original_idx]["_total_patient_files"] = total
                        _data[original_idx]["_patient_file_number"] = j + 1

                self.data = _data
                self.headers = _headers
                self._recalculate_accession_counts()
                app.logger.info(f"Loaded {len(self.data)} rows.")

            except Exception as e:
                self.data, self.headers = [], []
                raise DataLoadError(f"Error reading CSV: {e}")

    def save_data(self, target_path: Optional[Union[str, Path]] = None) -> None:
        """Saves current data to CSV atomically."""
        target_path = str(target_path or self.csv_path or Config.CSV_FILE_PATH)
        with self._lock:
            if not self.data or not self.headers:
                app.logger.warning("Save aborted: No data in memory.")
                return

            app.logger.info(f"Saving {len(self.data)} rows to {target_path}")

            priority_fields = ["AccessionID", "Stain", "BlockNumber", "ParsingQCPassed"]
            pipeline_fields = [h for h in self.headers if h not in priority_fields]
            fieldnames = list(dict.fromkeys(priority_fields + pipeline_fields))
            
            temp_path = target_path + ".tmp"
            try:
                with open(temp_path, "w", newline="", encoding="utf-8") as csvfile:
                    writer = csv.DictWriter(
                        csvfile,
                        fieldnames=fieldnames,
                        delimiter=",",
                        extrasaction="ignore",
                        quoting=csv.QUOTE_MINIMAL,
                    )
                    writer.writeheader()

                    for row in self.data:
                        write_row = row.copy()
                        write_row["ParsingQCPassed"] = "TRUE" if row.get("_is_complete") else ""
                        writer.writerow(write_row)

                # Atomic replace
                if os.path.exists(target_path):
                    os.replace(temp_path, target_path)
                else:
                    os.rename(temp_path, target_path)
                    
                session["last_loaded_csv_mod_time"] = os.path.getmtime(target_path)
                app.logger.info("Save successful.")
            except Exception as e:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                raise DataSaveError(f"Failed to save CSV: {e}")

    def _recalculate_accession_counts(self) -> None:
        """Internal helper to count AccessionID occurrences."""
        if not self.data:
            return
        id_counts = Counter(
            renaming.accession_key(row.get("AccessionID", ""))
            for row in self.data
            if row.get("AccessionID", "").strip()
        )
        for row in self.data:
            current_id = renaming.accession_key(row.get("AccessionID", ""))
            row["_accession_id_count"] = id_counts[current_id] if current_id else 0
    
    def get_row(self, index: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            if 0 <= index < len(self.data):
                return self.data[index]
            return None

    def update_row(self, index: int, updates: Dict[str, Any]) -> bool:
        """Updates a row and triggers recalculations if needed."""
        with self._lock:
            if not (0 <= index < len(self.data)):
                return False
            
            row = self.data[index]
            has_changed = False
            recalc_counts = False

            for key, value in updates.items():
                if row.get(key) != value:
                    row[key] = value
                    has_changed = True
                    if key == "AccessionID":
                        recalc_counts = True
            
            if recalc_counts:
                self._recalculate_accession_counts()
                
            return has_changed

    def clear(self):
        with self._lock:
            self.data = []
            self.headers = []

    def get_absolute_path(self, relative_path: str) -> Optional[str]:
        """Resolve a CSV image path, constrained to the active batch directory."""
        if not relative_path:
            return None

        root = (self.batch_root or Path(Config.IMAGE_BASE_DIR)).resolve()
        cleaned_path = str(relative_path).replace("\\", os.sep)
        candidate = Path(cleaned_path)
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            resolved = candidate.resolve()
            if os.path.commonpath([str(root), str(resolved)]) != str(root):
                return None
        except (OSError, ValueError):
            return None
        return str(resolved)

    def check_paths(self) -> List[str]:
        """Checks if all image paths in the loaded data exist and are readable."""
        missing_or_unreadable = []
        with self._lock:
            for i, row in enumerate(self.data):
                # Check Label and Macro paths
                for key in ["_label_path", "_macro_path"]:
                    rel_path = row.get(key)
                    if rel_path:
                        abs_path = self.get_absolute_path(rel_path)
                        if not abs_path or not os.path.exists(abs_path):
                             missing_or_unreadable.append(f"Row {i+1} ({key}): Path not found -> {abs_path}")
                        elif not os.access(abs_path, os.R_OK):
                             missing_or_unreadable.append(f"Row {i+1} ({key}): Path not readable -> {abs_path}")
        return missing_or_unreadable

class BatchContext:
    """Loaded data and persistent queue belonging to one discovered batch."""

    def __init__(self, batch_id: str, root: Path, catalog_row: Optional[Dict[str, Any]] = None):
        self.id = batch_id
        self.root = root
        self.name = root.name
        self.display_name = f"{root.parent.name}/{root.name}"
        self.csv_path = root / "enriched.csv"
        self.data_manager = DataManager(root, self.csv_path)
        self.queue_manager = QueueManager(batch_id)
        self.csv_mod_time: Optional[float] = None
        row = catalog_row or {}
        self.completed_stages = {
            "QC": bool(row.get("qc_complete", False)),
            "Renamed": bool(row.get("renamed_complete", False)),
        }
        self._pending_count = int(row.get("pending_count", 0))
        self._total_count = int(row.get("queue_total", row.get("slide_count", 0)))

    def load_completed_stages(self, create_if_missing: bool = False) -> None:
        """Refresh stage flags from the central catalog."""
        row = batch_catalog.get_batch(Config.INSTANCE_DIR, self.id)
        if row is None:
            raise DataLoadError("batch is missing from batch_catalog.sqlite3")
        self.completed_stages = {
            "QC": bool(row["qc_complete"]),
            "Renamed": bool(row["renamed_complete"]),
        }

    def mark_qc_complete(self) -> None:
        """Atomically mark the catalog QC stage complete."""
        try:
            if not batch_catalog.mark_qc_complete_if_queue_complete(
                Config.INSTANCE_DIR, self.id
            ):
                raise DataSaveError("queue still contains unfinished slides")
            self.completed_stages["QC"] = True
        except DataSaveError:
            raise
        except (OSError, sqlite3.Error, KeyError) as exc:
            raise DataSaveError(f"could not update batch catalog: {exc}") from exc

    def mark_renamed_complete(self) -> None:
        """Atomically mark both catalog workflow stages complete."""
        try:
            batch_catalog.update_stages(
                Config.INSTANCE_DIR, self.id, qc_complete=True, renamed_complete=True
            )
            self.completed_stages = {"QC": True, "Renamed": True}
        except (OSError, sqlite3.Error, KeyError) as exc:
            raise DataSaveError(f"could not update batch catalog: {exc}") from exc

    @property
    def qc_complete(self) -> bool:
        return self.completed_stages["QC"]

    def refresh(self) -> None:
        mod_time = self.csv_path.stat().st_mtime
        if not self.data_manager.data or mod_time != self.csv_mod_time:
            self.data_manager.load_data(self.csv_path)
            self.csv_mod_time = mod_time
        self.queue_manager.load()
        valid_indices = set(range(len(self.data_manager.data)))
        changed = False
        for index in list(self.queue_manager.items):
            if index not in valid_indices:
                del self.queue_manager.items[index]
                changed = True
        for row in self.data_manager.data:
            index = row["_original_index"]
            if index not in self.queue_manager.items:
                status = "completed" if row["_is_complete"] else "pending"
                self.queue_manager.add(QueueItem(original_index=index, status=status))
                changed = True
            elif row["_is_complete"] and self.queue_manager.items[index].status != "completed":
                item = self.queue_manager.items[index]
                item.status = "completed"
                item.leased_by_id = None
                item.leased_at = None
                changed = True
            elif not row["_is_complete"] and self.queue_manager.items[index].status == "completed":
                item = self.queue_manager.items[index]
                item.status = "pending"
                item.completed_by_id = None
                item.completed_at = None
                changed = True
        if changed:
            self.queue_manager.save()
        self._pending_count = sum(
            item.status == "pending" for item in self.queue_manager.get_all()
        )
        self._total_count = len(self.queue_manager.items)

    @property
    def is_complete(self) -> bool:
        items = self.queue_manager.get_all()
        return bool(items) and all(item.status == "completed" for item in items)

    @property
    def pending_count(self) -> int:
        return self._pending_count

    @property
    def total_count(self) -> int:
        return self._total_count


batch_contexts: Dict[str, BatchContext] = {}
batch_contexts_lock = threading.Lock()
_catalog_reconcile_lock = threading.Lock()
_catalog_reconciled_target: Optional[Tuple[str, str]] = None
_catalog_reconciler_started = False
_catalog_reconcile_owner = uuid.uuid4().hex
_renaming_jobs: Dict[str, Dict[str, Any]] = {}
_renaming_jobs_lock = threading.Lock()
_renaming_clone_lock = threading.Lock()
_longitudinal_active: set = set()
_longitudinal_lock = threading.Lock()


def _batch_relative_path(root: Path) -> str:
    return normalize_relative_path(root.relative_to(Path(Config.INSLIDE_BATCHES)).as_posix())


def _reconcile_queue_rows(public_id: str, slide_rows: Sequence[Dict[str, str]]) -> None:
    current = {
        int(row["original_index"]): row
        for row in batch_catalog.load_queue(Config.INSTANCE_DIR, public_id)
    }
    reconciled = []
    for index, slide in enumerate(slide_rows):
        complete_value = (slide.get("ParsingQCPassed") or "").strip()
        complete = bool(complete_value and complete_value.lower() != "false")
        row = dict(current.get(index, {"original_index": index, "status": "pending"}))
        if complete and row.get("status") != "completed":
            row.update({"status": "completed", "leased_by_id": None, "leased_at": None})
        elif not complete and row.get("status") == "completed":
            row.update(
                {
                    "status": "pending", "completed_by_id": None,
                    "completed_at": None,
                }
            )
        reconciled.append(row)
    batch_catalog.replace_queue(Config.INSTANCE_DIR, public_id, reconciled)


def reconcile_batch_catalog() -> List[str]:
    """Refresh catalog from batch directory; never read legacy stage/queue files."""
    base = Path(Config.INSLIDE_BATCHES)
    warnings: List[str] = []
    seen: List[str] = []
    try:
        scanner_dirs = sorted(
            (path for path in base.iterdir() if path.is_dir() and path.name.startswith("SS")),
            key=lambda path: path.name.lower(),
        )
    except OSError as exc:
        app.logger.warning("Label-Check pipeline batch directory unavailable: %s", exc)
        return [f"Batch directory is unavailable: {base}"]

    candidates: List[Path] = []
    for scanner_dir in scanner_dirs:
        try:
            candidates.extend(
                sorted(
                    (path for path in scanner_dir.iterdir() if path.is_dir()),
                    key=lambda path: path.name.lower(),
                )
            )
        except OSError as exc:
            app.logger.warning("Scanner directory unavailable: %s", exc)
            warnings.append(f"Skipped {scanner_dir.name}: directory is unavailable.")

    existing_batches = {
        str(row["relative_path"]).casefold(): row
        for row in batch_catalog.list_batches(Config.INSTANCE_DIR)
    }

    for root in candidates:
        display_name = f"{root.parent.name}/{root.name}"
        relative_path = normalize_relative_path(f"{root.parent.name}/{root.name}")
        seen.append(relative_path)
        missing = [
            name for name in ("label", "macro")
            if not (root / name).is_dir() or not os.access(root / name, os.R_OK | os.X_OK)
        ]
        csv_path = root / "enriched.csv"
        if not csv_path.is_file() or not os.access(csv_path, os.R_OK):
            missing.append("enriched.csv")
        try:
            if missing:
                message = f"missing {', '.join(missing)}"
                batch_catalog.upsert_batch(
                    Config.INSTANCE_DIR, relative_path, validity="invalid",
                    validation_error=message,
                )
                warnings.append(f"Skipped {display_name}: {message}.")
                continue
            mapping_path = root / "name_mapping.csv"
            history_path = root / "copath_history_job.json"
            enriched_mtime = csv_path.stat().st_mtime_ns
            mapping_mtime = mapping_path.stat().st_mtime_ns if mapping_path.exists() else None
            history_mtime = history_path.stat().st_mtime_ns if history_path.exists() else None
            existing = existing_batches.get(relative_path.casefold())
            if (
                existing is not None
                and existing["validity"] == "ready"
                and existing["enriched_mtime_ns"] == enriched_mtime
                and existing["mapping_mtime_ns"] == mapping_mtime
                and existing["history_mtime_ns"] == history_mtime
            ):
                batch_catalog.upsert_batch(
                    Config.INSTANCE_DIR,
                    relative_path,
                    validity="ready",
                    slide_count=int(existing["slide_count"]),
                    enriched_mtime_ns=enriched_mtime,
                    mapping_mtime_ns=mapping_mtime,
                    history_mtime_ns=history_mtime,
                    renaming_status=str(existing["renaming_status"]),
                    history_status=str(existing["history_status"]),
                )
                continue
            with csv_path.open("r", newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                if not reader.fieldnames or "ParsingQCPassed" not in reader.fieldnames:
                    raise DataLoadError("enriched.csv is missing ParsingQCPassed")
                slide_rows = list(reader)
            if not slide_rows:
                raise DataLoadError("enriched.csv has no slide rows")
            renaming_status = "missing"
            if mapping_path.exists():
                _, mapping_rows = renaming.read_csv(mapping_path)
                renaming_status = (
                    "approved"
                    if mapping_rows and all(renaming.parse_bool(row["Approved"]) for row in mapping_rows)
                    else "ready"
                )
            history_status = str(renaming.read_history_job(root).get("status", "not_needed"))
            public_id = batch_catalog.upsert_batch(
                Config.INSTANCE_DIR,
                relative_path,
                validity="ready",
                validation_error="",
                slide_count=len(slide_rows),
                enriched_mtime_ns=enriched_mtime,
                mapping_mtime_ns=mapping_mtime,
                history_mtime_ns=history_mtime,
                renaming_status=renaming_status,
                history_status=history_status,
            )
            _reconcile_queue_rows(public_id, slide_rows)
        except (DataLoadError, OSError, ValueError, csv.Error, renaming.RenamingError) as exc:
            app.logger.warning("Skipping invalid batch %s: %s", root, exc)
            warnings.append(f"Skipped {display_name}: {exc}")
            batch_catalog.upsert_batch(
                Config.INSTANCE_DIR, relative_path, validity="invalid",
                validation_error=str(exc),
            )

    batch_catalog.mark_unseen_missing(Config.INSTANCE_DIR, seen)
    batch_catalog.set_metadata(Config.INSTANCE_DIR, "last_reconciled_at", _iso_utc())
    batch_catalog.set_metadata(Config.INSTANCE_DIR, "last_reconcile_warnings", json.dumps(warnings))
    return warnings


def _catalog_reconciler() -> None:
    while True:
        time.sleep(max(1, Config.BATCH_CATALOG_RECONCILE_SECONDS))
        try:
            with _catalog_reconcile_lock:
                lease_seconds = max(60, Config.BATCH_CATALOG_RECONCILE_SECONDS * 2)
                if batch_catalog.acquire_reconcile_lease(
                    Config.INSTANCE_DIR, _catalog_reconcile_owner, lease_seconds
                ):
                    try:
                        reconcile_batch_catalog()
                    finally:
                        batch_catalog.release_reconcile_lease(
                            Config.INSTANCE_DIR, _catalog_reconcile_owner
                        )
        except Exception:
            app.logger.exception("Background batch catalog reconciliation failed")


def _ensure_catalog_reconciled() -> List[str]:
    global _catalog_reconciled_target, _catalog_reconciler_started
    target = (str(Path(Config.INSTANCE_DIR)), str(Path(Config.INSLIDE_BATCHES)))
    warnings: List[str] = []
    with _catalog_reconcile_lock:
        if _catalog_reconciled_target != target:
            batch_catalog.reset()
            lease_seconds = max(60, Config.BATCH_CATALOG_RECONCILE_SECONDS * 2)
            if batch_catalog.acquire_reconcile_lease(
                Config.INSTANCE_DIR, _catalog_reconcile_owner, lease_seconds
            ):
                try:
                    warnings = reconcile_batch_catalog()
                finally:
                    batch_catalog.release_reconcile_lease(
                        Config.INSTANCE_DIR, _catalog_reconcile_owner
                    )
            _catalog_reconciled_target = target
    if not app.config.get("TESTING") and not _catalog_reconciler_started:
        threading.Thread(target=_catalog_reconciler, daemon=True).start()
        _catalog_reconciler_started = True
    return warnings


def discover_batches() -> Tuple[List[BatchContext], List[str]]:
    """Return lightweight valid batch contexts from central catalog."""
    warnings = _ensure_catalog_reconciled()
    if not warnings:
        raw_warnings = batch_catalog.get_metadata(
            Config.INSTANCE_DIR, "last_reconcile_warnings"
        )
        if raw_warnings:
            try:
                warnings = list(json.loads(raw_warnings))
            except (TypeError, ValueError):
                warnings = []
    discovered: List[BatchContext] = []
    for row in batch_catalog.list_batches(Config.INSTANCE_DIR):
        if row["validity"] != "ready":
            continue
        root = Path(Config.INSLIDE_BATCHES) / Path(row["relative_path"])
        batch_id = str(row["public_id"])
        with batch_contexts_lock:
            context = batch_contexts.get(batch_id)
            if context is None or context.root != root:
                context = BatchContext(batch_id, root, row)
                batch_contexts[batch_id] = context
            else:
                context.completed_stages = {
                    "QC": bool(row["qc_complete"]),
                    "Renamed": bool(row["renamed_complete"]),
                }
                context._pending_count = int(row["pending_count"])
                context._total_count = int(row["queue_total"])
        discovered.append(context)

    return discovered, warnings


def _selected_batch(allow_completed: bool = False) -> Tuple[Optional[BatchContext], List[BatchContext], List[str]]:
    batches, warnings = discover_batches()
    available = [batch for batch in batches if not batch.qc_complete]
    if request.args.get("choose") == "1":
        session.pop("qc_batch_id", None)
        return None, available, warnings
    requested_id = request.values.get("batch") or session.get("qc_batch_id")
    selected = next((batch for batch in batches if batch.id == requested_id), None)
    if selected and (allow_completed or not selected.qc_complete):
        selected.refresh()
        session["qc_batch_id"] = selected.id
        return selected, available, warnings
    if requested_id:
        session.pop("qc_batch_id", None)
    return None, available, warnings


def _renaming_batches() -> Tuple[List[BatchContext], List[str]]:
    batches, warnings = discover_batches()
    return [
        batch for batch in batches
        if batch.completed_stages["QC"] and not batch.completed_stages["Renamed"]
    ], warnings


def _renaming_context(batch_id: str) -> Optional[BatchContext]:
    batches, _ = discover_batches()
    return next(
        (
            batch for batch in batches
            if batch.id == batch_id
            and batch.completed_stages["QC"]
            and not batch.completed_stages["Renamed"]
        ),
        None,
    )


def _renaming_job_state(batch_id: str) -> Dict[str, Any]:
    with _renaming_jobs_lock:
        return dict(_renaming_jobs.get(batch_id, {"status": "idle", "error": ""}))


def _start_longitudinal_job(context: BatchContext, *, force: bool = False) -> bool:
    try:
        job = renaming.read_history_job(context.root)
    except renaming.RenamingError:
        app.logger.exception("Invalid longitudinal job for batch %s", context.id)
        return False
    status = str(job.get("status", "not_needed"))
    if status not in ({"failed", "pending", "running"} if force else {"pending", "running"}):
        return False
    with _longitudinal_lock:
        if context.id in _longitudinal_active:
            return False
        _longitudinal_active.add(context.id)

    def worker() -> None:
        try:
            with _renaming_clone_lock:
                renaming.stage_longitudinal_history(
                    context.root,
                    Path(Config.COPATH_CLONE),
                    Path(Config.INSLIDE_BATCHES),
                )
            mapping_path = context.root / "name_mapping.csv"
            if mapping_path.exists():
                _, rows = renaming.read_csv(mapping_path)
                if rows and all(renaming.parse_bool(row["Approved"]) for row in rows):
                    with _renaming_clone_lock:
                        renaming.finalize_batch(context.root, Path(Config.COPATH_CLONE))
        except Exception:
            app.logger.exception("Longitudinal CoPath job failed for batch %s", context.id)
        finally:
            with _longitudinal_lock:
                _longitudinal_active.discard(context.id)

    threading.Thread(target=worker, daemon=True).start()
    return True


def _resume_longitudinal_jobs(batches: Sequence[BatchContext]) -> None:
    for context in batches:
        _start_longitudinal_job(context)


def _start_renaming_job(
    context: BatchContext,
    *,
    old_accession: Optional[str] = None,
    new_accession: Optional[str] = None,
    force: bool = False,
) -> bool:
    """Start one background preparation or accession retry for a batch."""
    with _renaming_jobs_lock:
        existing = _renaming_jobs.get(context.id, {})
        if existing.get("status") in {"preparing", "retrying"}:
            return False
        if (
            not force
            and old_accession is None
            and (context.root / "name_mapping.csv").exists()
        ):
            _renaming_jobs[context.id] = {"status": "ready", "error": ""}
            return False
        _renaming_jobs[context.id] = {
            "status": "retrying" if old_accession else "preparing",
            "error": "",
        }

    def worker() -> None:
        try:
            with _renaming_clone_lock:
                if old_accession is not None and new_accession is not None:
                    renaming.retry_group(
                        context.root,
                        Path(Config.COPATH_CLONE),
                        Path(Config.INSLIDE_BATCHES),
                        old_accession,
                        new_accession,
                    )
                    _replace_sdl_accession(old_accession, new_accession)
                    context.data_manager.load_data(context.csv_path)
                    context.csv_mod_time = context.csv_path.stat().st_mtime
                else:
                    renaming.prepare_batch(
                        context.root,
                        Path(Config.COPATH_CLONE),
                        Path(Config.INSLIDE_BATCHES),
                    )
            state = {"status": "ready", "error": ""}
        except Exception as exc:
            app.logger.exception("Renaming preparation failed for batch %s", context.id)
            state = {"status": "failed", "error": str(exc)}
        with _renaming_jobs_lock:
            _renaming_jobs[context.id] = state
        if state["status"] == "ready":
            _start_longitudinal_job(context)

    threading.Thread(target=worker, daemon=True).start()
    return True

# ==============================================================================
# 8. HELPER FUNCTIONS
# ==============================================================================
def _release_expired_leases(context: BatchContext):
    """Scans for and releases any item leases that have expired."""
    queue_manager = context.queue_manager
    lease_duration = datetime.timedelta(seconds=app.config["LEASE_DURATION_SECONDS"])
    expired_time = datetime.datetime.utcnow() - lease_duration
    count = queue_manager.release_expired(expired_time)
    if count:
        app.logger.info("Released %d expired lease(s) for batch %s", count, context.id)
        flash(
            f"{count} item(s) had expired leases and were returned to the queue.",
            "warning",
        )


def _create_backup(context: BatchContext, suffix: str = "") -> None:
    """Creates a timestamped backup of the current CSV file."""
    source_path = str(context.csv_path)
    if not os.path.exists(source_path):
        return
    try:
        os.makedirs(Config.BACKUP_DIR, exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        name_part = f"{context.name}_{context.id}_enriched.csv_{timestamp}"
        if suffix:
            name_part += f"_{suffix}"
        backup_path = os.path.join(Config.BACKUP_DIR, f"{name_part}.bak")
        shutil.copy2(source_path, backup_path)
    except Exception as e:
        raise BackupError(f"Backup failed: {e}")


def _is_row_incomplete(row_dict: Dict[str, Any]) -> bool:
    return not row_dict.get("_is_complete", False)


_qc_filename_component_pattern = re.compile(r"^[A-Z0-9-]+$")


def _normalize_qc_values(values: Dict[str, Any]) -> Dict[str, Any]:
    """Return QC values in their canonical form without hiding invalid input."""
    normalized = dict(values)
    normalized["AccessionID"] = str(values.get("AccessionID") or "").strip()
    for field in ("Stain", "BlockNumber"):
        value = str(values.get(field) or "").upper()
        if field == "Stain":
            value = value.replace("_", "-")
        normalized[field] = value
    return normalized


def _qc_row_validation_errors(row: Dict[str, Any]) -> List[str]:
    """Return the QC fields that are missing or invalid for a completed row."""
    errors = []
    accession_id = str(row.get("AccessionID") or "")
    if not accession_id.strip():
        errors.append("Accession ID is required")

    for field, label in (("Stain", "Stain"), ("BlockNumber", "Block Number")):
        value = str(row.get(field) or "")
        if not value.strip():
            errors.append(f"{label} is required")
        elif not _qc_filename_component_pattern.fullmatch(value):
            errors.append(
                f"{label} may contain only uppercase letters, numbers, and hyphens"
            )
    return errors


def _requeue_invalid_qc_rows(context: BatchContext) -> List[int]:
    """Return invalid completed rows to the pending queue."""
    invalid_indices = []
    for row in context.data_manager.data:
        if not _qc_row_validation_errors(row):
            continue

        index = row["_original_index"]
        invalid_indices.append(index)
        row["_is_complete"] = False

        item = context.queue_manager.get(index)
        if item is None:
            item = QueueItem(original_index=index)
            context.queue_manager.add(item)
        item.status = "pending"
        item.leased_by_id = None
        item.leased_at = None
        item.completed_by_id = None
        item.completed_at = None

    if invalid_indices:
        context.queue_manager.save()
    return invalid_indices


def flash_messages() -> List[Dict[str, str]]:
    return [
        {"category": category, "message": message}
        for category, message in get_flashed_messages(with_categories=True)
    ]


# ==============================================================================
# SLIDE DIGITIZATION LOG HELPERS
# ==============================================================================
SDL_HEADERS = (
    "Accession ID",
    "Organ",
    "Type",
    "Slides Count",
    "Scanner",
    "Carousel Rack",
    "Date Loaded",
    "Time Loaded",
    "Date Unloaded",
    "Time Unloaded",
    "Ran Label-Check",
    "Finished QC",
    "Collected CoPath Data",
    "Renamed",
    "Pushed to SFTP Server",
    "Notes",
)

SDL_STATUS_HEADERS = (
    "Ran Label-Check",
    "Finished QC",
    "Collected CoPath Data",
    "Renamed",
    "Pushed to SFTP Server",
)

SDL_FORM_FIELDS = {
    "Accession ID": "accession_id",
    "Organ": "organ",
    "Type": "type",
    "Slides Count": "slides_count",
    "Scanner": "scanner",
    "Carousel Rack": "carousel_rack",
    "Date Loaded": "date_loaded",
    "Time Loaded": "time_loaded",
    "Date Unloaded": "date_unloaded",
    "Time Unloaded": "time_unloaded",
    "Notes": "notes",
}

SDL_UNKNOWN_DATE = "----------"

_sdl_workbook_lock = threading.Lock()
_accession_pattern = re.compile(r"^[A-Z]{1,3}[0-9]{2}-[0-9]+$")
_rack_pattern = re.compile(r"^[0-9]+(?:\s*,\s*[0-9]+)*$")
_time_pattern = re.compile(r"^[0-9]{2}:[0-9]{2}$")


def _save_sdl_workbook(workbook) -> None:
    """Atomically replaces the SDL workbook with the supplied workbook."""
    workbook_path = Config.SDL_FILE_PATH
    workbook_dir = os.path.dirname(workbook_path)
    file_mode = stat.S_IMODE(os.stat(workbook_path).st_mode)
    file_descriptor, temporary_path = tempfile.mkstemp(
        prefix=".Slide_Digitization_Log.", suffix=".xlsx", dir=workbook_dir
    )
    os.close(file_descriptor)
    try:
        workbook.save(temporary_path)
        os.chmod(temporary_path, file_mode)
        os.replace(temporary_path, workbook_path)
    finally:
        if os.path.exists(temporary_path):
            os.remove(temporary_path)


def _sdl_header_columns(worksheet: Worksheet) -> Dict[str, int]:
    """Map managed SDL headers to their worksheet columns."""
    header_locations: Dict[str, List[int]] = defaultdict(list)
    for column in range(1, worksheet.max_column + 1):
        value = worksheet.cell(row=1, column=column).value
        if value is not None:
            header_locations[str(value).strip()].append(column)

    missing = [header for header in SDL_HEADERS if not header_locations[header]]
    duplicates = [
        header for header in SDL_HEADERS if len(header_locations[header]) > 1
    ]
    if missing or duplicates:
        details = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if duplicates:
            details.append(f"duplicated: {', '.join(duplicates)}")
        raise SDLWorkbookError(
            "The SDL worksheet does not contain exactly one of each required "
            f"header ({'; '.join(details)})."
        )
    return {header: header_locations[header][0] for header in SDL_HEADERS}


def _load_sdl_workbook():
    """Loads and validates the configured SDL workbook and worksheet."""
    workbook_path = Config.SDL_FILE_PATH
    if not os.path.isfile(workbook_path):
        raise SDLWorkbookError(
            f"Slide Digitization Log not found at {workbook_path}."
        )

    try:
        workbook = load_workbook(workbook_path)
    except Exception as exc:
        raise SDLWorkbookError(f"The Slide Digitization Log could not be opened: {exc}") from exc

    if Config.SDL_SHEET_NAME not in workbook.sheetnames:
        workbook.close()
        raise SDLWorkbookError(
            f"The workbook must contain a worksheet named '{Config.SDL_SHEET_NAME}'."
        )

    worksheet = workbook[Config.SDL_SHEET_NAME]
    has_any_value = any(
        cell.value is not None
        for row in worksheet.iter_rows()
        for cell in row
    )
    initialized_headers = False
    if not has_any_value:
        worksheet.append(SDL_HEADERS)
        initialized_headers = True
    try:
        _sdl_header_columns(worksheet)
    except SDLWorkbookError:
        workbook.close()
        raise

    return workbook, worksheet, initialized_headers


def _coerce_sdl_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "y", "1"}
    return False


def _format_sdl_value(header: str, value: Any) -> str:
    if value is None:
        return ""
    if header.startswith("Date ") and isinstance(value, (datetime.datetime, datetime.date)):
        return value.strftime("%Y-%m-%d")
    if header.startswith("Time ") and isinstance(value, (datetime.datetime, datetime.time)):
        return value.strftime("%H:%M")
    return str(value)


def _sdl_row_signature(worksheet: Worksheet, row_number: int) -> str:
    header_columns = _sdl_header_columns(worksheet)
    values = tuple(
        worksheet.cell(row=row_number, column=header_columns[header]).value
        for header in SDL_HEADERS
    )
    return hashlib.sha256(repr(values).encode("utf-8")).hexdigest()


def _read_sdl_rows(worksheet: Worksheet) -> List[Dict[str, Any]]:
    header_columns = _sdl_header_columns(worksheet)
    rows = []
    for row_number in range(2, worksheet.max_row + 1):
        raw_values = {
            header: worksheet.cell(
                row=row_number, column=header_columns[header]
            ).value
            for header in SDL_HEADERS
        }
        if all(value is None for value in raw_values.values()):
            continue
        rows.append(
            {
                "worksheet_row": row_number,
                "values": {
                    header: _format_sdl_value(header, value)
                    for header, value in raw_values.items()
                },
                "statuses": {
                    header: _coerce_sdl_bool(raw_values[header])
                    for header in SDL_STATUS_HEADERS
                },
                "signature": _sdl_row_signature(worksheet, row_number),
            }
        )
    return rows


TABLE_QUERY_MAX_LENGTH = 200


def _clean_table_query(value: Optional[str]) -> str:
    return (value or "").strip()[:TABLE_QUERY_MAX_LENGTH]


def _natural_sort_key(value: str) -> Tuple[Tuple[int, Any], ...]:
    """Build a case-insensitive key that orders embedded numbers numerically."""
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in re.split(r"(\d+)", value.strip().casefold())
        if part
    )


def _filter_sort_records(
    records: List[Any],
    values_for_record,
    global_query: str = "",
    column_filters: Optional[Dict[int, str]] = None,
    sort_column: Optional[int] = None,
    sort_direction: str = "asc",
) -> List[Any]:
    """Filter displayed row values, then apply a stable natural sort."""
    global_term = _clean_table_query(global_query).casefold()
    filters = {
        column: cleaned.casefold()
        for column, value in (column_filters or {}).items()
        if (cleaned := _clean_table_query(value))
    }
    matched = []
    for record in records:
        values = [str(value or "") for value in values_for_record(record)]
        folded_values = [value.casefold() for value in values]
        if global_term and not any(global_term in value for value in folded_values):
            continue
        if any(
            column >= len(folded_values) or term not in folded_values[column]
            for column, term in filters.items()
        ):
            continue
        matched.append(record)

    if sort_column is None:
        return matched

    nonblank = []
    blank = []
    for record in matched:
        values = values_for_record(record)
        value = str(values[sort_column] or "") if sort_column < len(values) else ""
        (blank if not value.strip() else nonblank).append(record)
    nonblank.sort(
        key=lambda record: _natural_sort_key(
            str(values_for_record(record)[sort_column] or "")
        ),
        reverse=sort_direction == "desc",
    )
    return nonblank + blank


def _request_table_state(
    column_count: int,
) -> Tuple[str, Dict[int, str], Optional[int], str]:
    global_query = _clean_table_query(request.args.get("q"))
    column_filters = {
        column: value
        for column in range(column_count)
        if (value := _clean_table_query(request.args.get(f"filter_{column}")))
    }
    try:
        sort_column = int(request.args.get("sort", ""))
    except (TypeError, ValueError):
        sort_column = None
    if sort_column is not None and not 0 <= sort_column < column_count:
        sort_column = None
    sort_direction = request.args.get("direction", "asc").casefold()
    if sort_direction not in {"asc", "desc"}:
        sort_direction = "asc"
    return global_query, column_filters, sort_column, sort_direction


def _table_query_params(
    global_query: str,
    column_filters: Dict[int, str],
    sort_column: Optional[int],
    sort_direction: str,
) -> Dict[str, str]:
    params: Dict[str, str] = {}
    if global_query:
        params["q"] = global_query
    params.update(
        {f"filter_{column}": value for column, value in column_filters.items()}
    )
    if sort_column is not None:
        params["sort"] = str(sort_column)
        params["direction"] = sort_direction
    return params


def _submitted_sdl_form() -> Dict[str, str]:
    return {
        header: request.form.get(field_name, "").strip()
        for header, field_name in SDL_FORM_FIELDS.items()
    }


def _validate_sdl_form(values: Dict[str, str]) -> Dict[str, Any]:
    accession_id = values["Accession ID"]
    if not _accession_pattern.fullmatch(accession_id):
        raise SDLValidationError(
            "Accession ID must match the format A12-123 (1-3 uppercase letters, "
            "2 digits, a hyphen, and one or more digits)."
        )
    if values["Organ"] not in Config.SDL_ORGANS:
        raise SDLValidationError("Select a valid Organ.")
    if not values["Type"]:
        raise SDLValidationError("Type is required.")
    try:
        slides_count = int(values["Slides Count"])
    except ValueError as exc:
        raise SDLValidationError("Slides Count must be an integer.") from exc
    if slides_count < 1:
        raise SDLValidationError("Slides Count must be at least 1.")
    if values["Scanner"] not in Config.SDL_SCANNERS:
        raise SDLValidationError("Select a valid Scanner.")

    carousel_rack = values["Carousel Rack"]
    if not _rack_pattern.fullmatch(carousel_rack):
        raise SDLValidationError(
            "Carousel Rack must contain positive integers separated by commas."
        )
    rack_numbers = [int(value.strip()) for value in carousel_rack.split(",")]
    if any(value < 1 for value in rack_numbers):
        raise SDLValidationError("Carousel Rack numbers must be at least 1.")

    if values["Date Loaded"] == SDL_UNKNOWN_DATE:
        date_loaded: Union[datetime.date, str] = SDL_UNKNOWN_DATE
    else:
        try:
            date_loaded = datetime.date.fromisoformat(values["Date Loaded"])
        except ValueError as exc:
            raise SDLValidationError(
                f"Date Loaded must use YYYY-MM-DD format or {SDL_UNKNOWN_DATE}."
            ) from exc
    if not _time_pattern.fullmatch(values["Time Loaded"]):
        raise SDLValidationError("Time Loaded must use HH:MM 24-hour format.")
    try:
        time_loaded = datetime.time.fromisoformat(values["Time Loaded"])
    except ValueError as exc:
        raise SDLValidationError("Time Loaded must be a valid 24-hour time.") from exc

    date_unloaded_value = values["Date Unloaded"]
    time_unloaded_value = values["Time Unloaded"]
    if bool(date_unloaded_value) != bool(time_unloaded_value):
        raise SDLValidationError(
            "Date Unloaded and Time Unloaded must either both be supplied or both be blank."
        )
    date_unloaded = None
    time_unloaded = None
    if date_unloaded_value:
        if date_unloaded_value == SDL_UNKNOWN_DATE:
            date_unloaded = SDL_UNKNOWN_DATE
        else:
            try:
                date_unloaded = datetime.date.fromisoformat(date_unloaded_value)
            except ValueError as exc:
                raise SDLValidationError(
                    f"Date Unloaded must use YYYY-MM-DD format or {SDL_UNKNOWN_DATE}."
                ) from exc
        if not _time_pattern.fullmatch(time_unloaded_value):
            raise SDLValidationError("Time Unloaded must use HH:MM 24-hour format.")
        try:
            time_unloaded = datetime.time.fromisoformat(time_unloaded_value)
        except ValueError as exc:
            raise SDLValidationError("Time Unloaded must be a valid 24-hour time.") from exc
        if (
            isinstance(date_loaded, datetime.date)
            and isinstance(date_unloaded, datetime.date)
            and datetime.datetime.combine(date_unloaded, time_unloaded)
            < datetime.datetime.combine(date_loaded, time_loaded)
        ):
            raise SDLValidationError("The unloaded timestamp cannot precede the loaded timestamp.")

    return {
        "Accession ID": accession_id,
        "Organ": values["Organ"],
        "Type": values["Type"],
        "Slides Count": slides_count,
        "Scanner": values["Scanner"],
        "Carousel Rack": ", ".join(str(value) for value in rack_numbers),
        "Date Loaded": date_loaded,
        "Time Loaded": time_loaded,
        "Date Unloaded": date_unloaded,
        "Time Unloaded": time_unloaded,
        "Notes": values["Notes"],
    }


def _strict_sdl_date(value: str) -> Optional[datetime.date]:
    """Return a calendar date only for canonical YYYY-MM-DD values."""
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        return None


def _original_path_parent_name(value: str) -> str:
    """Return the immediate parent for native or Windows-formatted paths."""
    if "\\" in value:
        return PureWindowsPath(value).parent.name
    return Path(value).parent.name


def _sdl_scanner_for_batch(batch_root: Path) -> str:
    scanner_directory = batch_root.parent.name
    if not scanner_directory.upper().startswith("SS"):
        return "-----"
    marker = f"({scanner_directory})".upper()
    return next(
        (
            option
            for option in Config.SDL_SCANNERS
            if marker in option.upper()
        ),
        "-----",
    )


def _post_qc_sdl_rows(
    batch_root: Path,
    worksheet: Worksheet,
) -> List[Dict[str, Any]]:
    """Build new SDL rows for accessions absent from the workbook."""
    _, mapping = renaming.read_csv(batch_root / "name_mapping.csv")
    header_columns = _sdl_header_columns(worksheet)
    accession_column = header_columns["Accession ID"]
    existing_accessions = {
        renaming.accession_key(
            worksheet.cell(row=row_number, column=accession_column).value
        )
        for row_number in range(2, worksheet.max_row + 1)
        if worksheet.cell(row=row_number, column=accession_column).value is not None
    }

    slides_by_accession: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    mapping_organs: Dict[str, str] = {}
    for slide in mapping:
        accession = renaming.row_accession(slide)
        if not accession:
            continue
        key = renaming.accession_key(accession)
        slides_by_accession[key].append(slide)
        organ = slide.get("Organ", "").strip().upper()
        if organ and key not in mapping_organs:
            mapping_organs[key] = organ

    clone_organs: Dict[str, str] = {}
    renaming.initialize_clone(Path(Config.COPATH_CLONE))
    clone_index = Path(Config.COPATH_CLONE) / "all_iuh_identifiers.csv"
    if clone_index.exists():
        _, clone_rows = renaming.read_csv(clone_index)
        clone_organs = {
            renaming.row_accession_key(row): row.get("Organ", "").strip().upper()
            for row in clone_rows
            if renaming.row_accession(row) and row.get("Organ", "").strip()
        }

    batch_date = _strict_sdl_date(batch_root.name)
    scanner = _sdl_scanner_for_batch(batch_root)
    new_rows: List[Dict[str, Any]] = []
    for key, slides in slides_by_accession.items():
        if key in existing_accessions:
            continue
        accession = renaming.row_accession(slides[0])
        if batch_date is not None:
            date_groups: Dict[Union[datetime.date, str], List[Dict[str, str]]] = {
                batch_date: slides
            }
        else:
            date_groups = {}
            for slide in slides:
                parent_name = _original_path_parent_name(
                    slide.get("OriginalPath", "")
                )
                date_key: Union[datetime.date, str] = (
                    _strict_sdl_date(parent_name) or SDL_UNKNOWN_DATE
                )
                date_groups.setdefault(date_key, []).append(slide)

        organ = mapping_organs.get(key) or clone_organs.get(key) or "UNKNOWN"
        for loaded_date, grouped_slides in date_groups.items():
            row = {header: None for header in SDL_HEADERS}
            row.update(
                {
                    "Accession ID": accession,
                    "Organ": organ,
                    "Slides Count": len(grouped_slides),
                    "Scanner": scanner,
                    "Date Loaded": loaded_date,
                    "Ran Label-Check": True,
                    "Finished QC": True,
                    "Collected CoPath Data": True,
                    "Renamed": True,
                    "Pushed to SFTP Server": False,
                }
            )
            new_rows.append(row)
    return new_rows


def _update_sdl_after_renaming(batch_root: Path) -> int:
    """Append missing post-QC rows and return the number added."""
    with _sdl_workbook_lock:
        workbook, worksheet, initialized_headers = _load_sdl_workbook()
        try:
            header_columns = _sdl_header_columns(worksheet)
            new_rows = _post_qc_sdl_rows(batch_root, worksheet)
            for values in new_rows:
                row_number = worksheet.max_row + 1
                for header, value in values.items():
                    worksheet.cell(
                        row=row_number, column=header_columns[header]
                    ).value = value
                loaded_date = values["Date Loaded"]
                if isinstance(loaded_date, datetime.date):
                    worksheet.cell(
                        row=row_number,
                        column=header_columns["Date Loaded"],
                    ).number_format = "yyyy-mm-dd"
            if initialized_headers or new_rows:
                _save_sdl_workbook(workbook)
            return len(new_rows)
        finally:
            workbook.close()


def _replace_sdl_accession(old_accession: str, new_accession: str) -> int:
    """Replace an accession in existing SDL rows and return rows changed."""
    if old_accession == new_accession:
        return 0
    with _sdl_workbook_lock:
        workbook, worksheet, _ = _load_sdl_workbook()
        try:
            accession_column = _sdl_header_columns(worksheet)["Accession ID"]
            changed = 0
            for row_number in range(2, worksheet.max_row + 1):
                cell = worksheet.cell(row=row_number, column=accession_column)
                if renaming.same_accession(cell.value, old_accession):
                    cell.value = new_accession
                    changed += 1
            if changed:
                _save_sdl_workbook(workbook)
            return changed
        finally:
            workbook.close()


def _render_sdl_page(
    form_values: Optional[Dict[str, str]] = None,
    edit_row: Optional[int] = None,
    edit_signature: str = "",
):
    workbook = None
    try:
        with _sdl_workbook_lock:
            workbook, worksheet, initialized_headers = _load_sdl_workbook()
            if initialized_headers:
                _save_sdl_workbook(workbook)
            rows = _read_sdl_rows(worksheet)
    except SDLWorkbookError as exc:
        return render_template(
            "sdl.html",
            workbook_available=False,
            workbook_error=str(exc),
            messages=flash_messages(),
        )
    except Exception as exc:
        app.logger.exception("Unexpected error while reading the SDL workbook")
        return render_template(
            "sdl.html",
            workbook_available=False,
            workbook_error=f"The Slide Digitization Log could not be read: {exc}",
            messages=flash_messages(),
        )
    finally:
        if workbook is not None:
            workbook.close()

    if edit_row is not None and form_values is None:
        selected_row = next(
            (row for row in rows if row["worksheet_row"] == edit_row), None
        )
        if selected_row is None:
            flash("The selected SDL row no longer exists.", "error")
            return redirect(url_for("sdl"))
        form_values = {
            header: selected_row["values"][header]
            for header in SDL_FORM_FIELDS
        }
        edit_signature = selected_row["signature"]

    total_rows = len(rows)
    row_sort_column = len(SDL_HEADERS)
    global_query, column_filters, sort_column, sort_direction = (
        _request_table_state(len(SDL_HEADERS) + 1)
    )
    column_filters = {
        column: value
        for column, value in column_filters.items()
        if column < len(SDL_HEADERS)
    }

    def displayed_values(row):
        values = []
        for header in SDL_HEADERS:
            if header in SDL_STATUS_HEADERS:
                values.append("Yes" if row["statuses"][header] else "No")
            else:
                values.append(row["values"][header])
        return values

    if sort_column == row_sort_column:
        rows = _filter_sort_records(
            rows, displayed_values, global_query, column_filters
        )
        rows.sort(
            key=lambda row: row["worksheet_row"],
            reverse=sort_direction == "desc",
        )
    elif sort_column is None:
        rows.reverse()
        rows = _filter_sort_records(
            rows, displayed_values, global_query, column_filters
        )
    else:
        rows = _filter_sort_records(
            rows,
            displayed_values,
            global_query,
            column_filters,
            sort_column,
            sort_direction,
        )
    query_params = _table_query_params(
        global_query, column_filters, sort_column, sort_direction
    )
    row_sort_url = url_for(
        "sdl",
        **{
            **query_params,
            "sort": row_sort_column,
            "direction": (
                "desc"
                if sort_column == row_sort_column and sort_direction == "asc"
                else "asc"
            ),
        },
    )
    sort_urls = []
    for column in range(len(SDL_HEADERS)):
        next_direction = (
            "desc"
            if sort_column == column and sort_direction == "asc"
            else "asc"
        )
        sort_urls.append(
            url_for(
                "sdl",
                **{
                    **query_params,
                    "sort": column,
                    "direction": next_direction,
                },
            )
        )
    edit_urls = {
        row["worksheet_row"]: url_for(
            "sdl",
            **{**query_params, "edit_row": row["worksheet_row"]},
        )
        + "#entry-form"
        for row in rows
    }

    return render_template(
        "sdl.html",
        workbook_available=True,
        rows=rows,
        total_rows=total_rows,
        headers=SDL_HEADERS,
        status_headers=SDL_STATUS_HEADERS,
        form_fields=SDL_FORM_FIELDS,
        form_values=form_values or {header: "" for header in SDL_FORM_FIELDS},
        edit_row=edit_row,
        edit_signature=edit_signature,
        organ_options=Config.SDL_ORGANS,
        scanner_options=Config.SDL_SCANNERS,
        global_query=global_query,
        column_filters=column_filters,
        sort_column=sort_column,
        sort_direction=sort_direction,
        row_sort_column=row_sort_column,
        row_sort_url=row_sort_url,
        query_params=query_params,
        sort_urls=sort_urls,
        edit_urls=edit_urls,
        messages=flash_messages(),
    )


def _read_inventory_page(
    inventory_path: Path,
    requested_page: int,
    rows_per_page: int = 100,
    global_query: str = "",
    column_filters: Optional[Dict[int, str]] = None,
    sort_column: Optional[int] = None,
    sort_direction: str = "asc",
) -> Tuple[List[str], List[List[str]], int, int, int, int]:
    """Filter and sort a headered inventory CSV before returning one page."""
    headers: List[str] = []
    matched_rows: List[List[str]] = []
    page_rows: List[List[str]] = []
    total_rows = 0
    matching_rows = 0

    try:
        with inventory_path.open("r", encoding="utf-8-sig", newline="") as inventory_file:
            reader = csv.reader(inventory_file, strict=True)
            try:
                headers = next(reader)
            except StopIteration:
                raise InventoryReadError("This inventory is empty and has no header row.")
            if not headers:
                raise InventoryReadError("This inventory does not contain a usable header row.")

            column_filters = {
                column: value
                for column, value in (column_filters or {}).items()
                if 0 <= column < len(headers)
            }
            if sort_column is not None and not 0 <= sort_column < len(headers):
                sort_column = None
            if sort_direction not in {"asc", "desc"}:
                sort_direction = "asc"
            page_start = (requested_page - 1) * rows_per_page
            page_end = page_start + rows_per_page
            for row in reader:
                total_rows += 1
                normalized_row = (row + [""] * len(headers))[:len(headers)]
                if not _filter_sort_records(
                    [normalized_row],
                    lambda candidate: candidate,
                    global_query,
                    column_filters,
                ):
                    continue
                if sort_column is not None:
                    matched_rows.append(normalized_row)
                elif page_start <= matching_rows < page_end:
                    page_rows.append(normalized_row)
                matching_rows += 1
    except InventoryReadError:
        raise
    except UnicodeDecodeError as exc:
        raise InventoryReadError("This inventory is not valid UTF-8 text.") from exc
    except csv.Error as exc:
        raise InventoryReadError(f"This inventory contains invalid CSV data: {exc}") from exc
    except OSError as exc:
        raise InventoryReadError(f"This inventory could not be read: {exc}") from exc

    total_pages = max(1, (matching_rows + rows_per_page - 1) // rows_per_page)
    current_page = min(requested_page, total_pages)
    if current_page != requested_page:
        return _read_inventory_page(
            inventory_path,
            current_page,
            rows_per_page,
            global_query,
            column_filters,
            sort_column,
            sort_direction,
        )
    page_start = (current_page - 1) * rows_per_page
    if sort_column is not None:
        matched_rows = _filter_sort_records(
            matched_rows,
            lambda row: row,
            sort_column=sort_column,
            sort_direction=sort_direction,
        )
        page_rows = matched_rows[page_start:page_start + rows_per_page]

    return (
        headers,
        page_rows,
        total_rows,
        matching_rows,
        current_page,
        total_pages,
    )


# ==============================================================================
# TQ TRANSFER HELPERS
# ==============================================================================
TQ_LOG_FIELDS = (
    "original_path",
    "destination_dir",
    "destination_name",
    "organ",
    "pid",
    "digitization_date",
    "status",
)
TQ_METADATA_FIELDS = ("accession_id", "pid", "num_slides")
TQ_FILTER_FIELDS = (
    "None",
    "Organ",
    "PID",
    "AccessionDate",
    "Stain",
    "ImageType",
    "SampAcqType",
    "BlockNumber",
    "SectionCount",
    "OriginalPath",
    "NewName",
)
_tq_pid_pattern = re.compile(r"^[A-Z]{6}$")
_tq_section_pattern = re.compile(r"^[0-9]{3}$")
_tq_state_lock = threading.Lock()
_tq_config_lock = threading.Lock()
_tq_drafts: Dict[str, Dict[str, Any]] = {}
_tq_jobs: Dict[str, "TQJob"] = {}
_tq_active_job_id: Optional[str] = None


@dataclass(frozen=True)
class TQConfigResult:
    values: Dict[str, Any]
    repaired_lines: Tuple[int, ...] = ()
    repaired_keys: Tuple[str, ...] = ()
    backup_path: Optional[Path] = None


class TQJob:
    def __init__(
        self,
        job_id: str,
        owner_id: str,
        process: Optional[subprocess.Popen],
        slides: List[Dict[str, str]],
        all_slides: List[Dict[str, str]],
        manifest_path: Optional[Path] = None,
        metadata_path: Optional[Path] = None,
        copath_path: Optional[Path] = None,
        missing_copath_accessions: Sequence[str] = (),
    ):
        self.id = job_id
        self.owner_id = owner_id
        self.process = process
        self.slides = slides
        self.all_slides = all_slides
        self.manifest_path = manifest_path
        self.metadata_path = metadata_path
        self.copath_path = copath_path
        self.include_copath = copath_path is not None
        self.missing_copath_accessions = tuple(missing_copath_accessions)
        self.status = "running"
        self.return_code: Optional[int] = None
        self.output = ""
        self.results: Dict[str, Dict[str, Any]] = {}
        self.result_errors: Dict[str, str] = {}
        self.started_at = datetime.datetime.now().astimezone()
        self.log_path: Optional[Path] = None
        self.config_result: Optional[TQConfigResult] = None


def _tq_append_output(job: TQJob, message: str) -> None:
    with _tq_state_lock:
        job.output += message


def _tq_staging_root() -> Path:
    return Path(Config.IMAGE_STAGING_ROOT).expanduser().resolve()


def _tq_staging_display_path(path: Path) -> str:
    try:
        relative = path.relative_to(_tq_staging_root())
    except ValueError:
        return str(path)
    if re.match(r"^[A-Za-z]:[\\/]", Config.IMAGE_STAGING_HOST_DISPLAY):
        return str(PureWindowsPath(Config.IMAGE_STAGING_HOST_DISPLAY).joinpath(*relative.parts))
    return str(Path(Config.IMAGE_STAGING_HOST_DISPLAY).joinpath(*relative.parts))


def _tq_windows_safe_component(value: str) -> bool:
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }
    stem = value.split(".", 1)[0].upper()
    return (
        not any(character in value for character in '<>:"|?*')
        and not any(ord(character) < 32 for character in value)
        and not value.endswith((" ", "."))
        and stem not in reserved
    )


def _tq_staging_path(slide: Dict[str, str]) -> Path:
    staging_dir = slide.get("staging_dir", "")
    parts = staging_dir.split("/")
    if (
        not staging_dir
        or "\\" in staging_dir
        or any(not part or part in {".", ".."} for part in parts)
        or any(not _tq_windows_safe_component(part) for part in parts)
    ):
        raise TQError("Destination directory cannot be used for staging.")

    destination_name = slide.get("destination_name", "")
    if (
        not destination_name
        or destination_name in {".", ".."}
        or Path(destination_name).name != destination_name
        or any(separator in destination_name for separator in ("/", "\\"))
        or Path(destination_name).suffix.casefold() != ".svs"
        or not _tq_windows_safe_component(destination_name)
    ):
        raise TQError("Destination name must be one .svs filename.")

    root = _tq_staging_root()
    target = root.joinpath(*parts, destination_name)
    resolved = target.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise TQError("Staging path is outside the configured staging root.") from exc

    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise TQError(f"Staging path cannot contain a symbolic link: {current}")
    if target.is_symlink():
        raise TQError(f"Staging file cannot be a symbolic link: {target}")
    return target


def _tq_prepare_staging_paths(slides: List[Dict[str, str]]) -> None:
    seen = set()
    staging_directories = set()
    for slide in slides:
        target = _tq_staging_path(slide)
        normalized = str(target).casefold()
        if normalized in seen:
            raise TQError(f"Duplicate staging destination: {target}")
        seen.add(normalized)
        staging_directories.add(str(target.parent).casefold())
        slide["staged_path"] = str(target)
    if len(staging_directories) != 1:
        raise TQError("All selected slides must use one staging batch directory.")


def _tq_transfer_log_root() -> Path:
    configured = str(Config.TQ_TRANSFER_LOG_DIR or "").strip()
    return (
        Path(configured).expanduser()
        if configured
        else Path(Config.INSLIDE_BATCHES) / "transfer_logs"
    )


def _tq_slide_id(batch_id: str, original_path: str) -> str:
    value = f"{batch_id}\0{original_path}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()[:24]


def _tq_sdl_accession_metadata(
) -> Tuple[Dict[str, Dict[str, List[str]]], Optional[str]]:
    workbook = None
    try:
        with _sdl_workbook_lock:
            workbook, worksheet, _ = _load_sdl_workbook()
            dates: Dict[str, set] = defaultdict(set)
            types: Dict[str, set] = defaultdict(set)
            for row in _read_sdl_rows(worksheet):
                accession = renaming.accession_key(row["values"]["Accession ID"])
                loaded = row["values"]["Date Loaded"].strip()
                slide_type = row["values"]["Type"].strip()
                if accession and _strict_sdl_date(loaded):
                    dates[accession].add(loaded)
                if accession and slide_type:
                    types[accession].add(slide_type)
            return {
                accession: {
                    "dates": sorted(dates.get(accession, set())),
                    "types": sorted(values, key=str.casefold),
                }
                for accession, values in types.items()
            }, None
    except (SDLWorkbookError, OSError) as exc:
        return {}, f"Slide Digitization Log transfer metadata is unavailable: {exc}"
    finally:
        if workbook is not None:
            workbook.close()


def _tq_digitization_date(
    original_path: str,
    batch_root: Path,
    accession: str,
    sdl_dates: Dict[str, List[str]],
) -> str:
    original_parent = _strict_sdl_date(_original_path_parent_name(original_path))
    if original_parent:
        return original_parent.isoformat()
    batch_date = _strict_sdl_date(batch_root.name)
    if batch_date:
        return batch_date.isoformat()
    candidates = sdl_dates.get(renaming.accession_key(accession), [])
    return candidates[0] if len(candidates) == 1 else ""


def _tq_catalog() -> Tuple[List[Dict[str, str]], List[str]]:
    batches, warnings = discover_batches()
    sdl_metadata, sdl_warning = _tq_sdl_accession_metadata()
    if sdl_warning:
        warnings.append(sdl_warning)
    slides: List[Dict[str, str]] = []
    required = set(renaming.MAPPING_FIELDS)
    for context in batches:
        if not (
            context.completed_stages["QC"]
            and context.completed_stages["Renamed"]
        ):
            continue
        mapping_path = context.root / "name_mapping.csv"
        try:
            fields, rows = renaming.read_csv(mapping_path)
            missing = required.difference(fields)
            if missing:
                raise renaming.RenamingError(
                    f"missing columns: {', '.join(sorted(missing))}"
                )
            for row in rows:
                original_path = str(runtime_path(row["OriginalPath"].strip()))
                accession = renaming.row_accession(row)
                organ = row["Organ"].strip().upper()
                pid = row["PID"].strip().upper()
                destination_name = row["NewName"].strip()
                if not all((original_path, accession, organ, pid, destination_name)):
                    raise renaming.RenamingError(
                        "contains a row missing OriginalPath, AccessionID, "
                        "Organ, PID, or NewName"
                    )
                accession_metadata = sdl_metadata.get(renaming.accession_key(accession))
                if not accession_metadata:
                    continue
                slides.append(
                    {
                        "id": _tq_slide_id(context.id, original_path),
                        "batch_id": context.id,
                        "batch_name": context.display_name,
                        "batch_root": str(context.root),
                        "accession": accession,
                        "organ": organ,
                        "pid": pid,
                        "accession_date": row["AccessionDate"].strip(),
                        "stain": row["Stain"].strip(),
                        "image_type": row["ImageType"].strip().upper(),
                        "samp_acq_type": row["SampAcqType"].strip().upper(),
                        "block_number": row["BlockNumber"].strip(),
                        "section_count": row["SectionCount"].strip(),
                        "original_path": original_path,
                        "destination_name": destination_name,
                        "sdl_types": accession_metadata["types"],
                        "digitization_date": _tq_digitization_date(
                            original_path,
                            context.root,
                            accession,
                            {renaming.accession_key(accession): accession_metadata["dates"]},
                        ),
                    }
                )
        except renaming.RenamingError as exc:
            warnings.append(
                f"Skipped {context.display_name}: name_mapping.csv {exc}."
            )
    return slides, warnings


def _tq_validate_filter(
    field: str,
    value: str,
    start: str,
    end: str,
) -> Optional[str]:
    if field not in TQ_FILTER_FIELDS:
        return "Choose a valid filter field."
    if field == "PID" and value and not _tq_pid_pattern.fullmatch(value.upper()):
        return "PID must contain exactly six uppercase letters."
    if field == "SectionCount" and value and not _tq_section_pattern.fullmatch(value):
        return "Section Count must contain exactly three digits."
    if field == "AccessionDate":
        parsed = []
        for label, candidate in (("Start", start), ("End", end)):
            if not candidate:
                parsed.append(None)
                continue
            try:
                parsed.append(datetime.date.fromisoformat(candidate))
            except ValueError:
                return f"{label} date must use YYYY-MM-DD."
        if parsed[0] and parsed[1] and parsed[1] < parsed[0]:
            return "End date cannot precede Start date."
    return None


def _tq_filtered_slides(
    slides: List[Dict[str, str]],
    field: str,
    value: str,
    start: str,
    end: str,
    sort_order: str,
) -> List[Dict[str, str]]:
    attribute = {
        "Organ": "organ",
        "PID": "pid",
        "AccessionDate": "accession_date",
        "Stain": "stain",
        "ImageType": "image_type",
        "SampAcqType": "samp_acq_type",
        "BlockNumber": "block_number",
        "SectionCount": "section_count",
        "OriginalPath": "original_path",
        "NewName": "destination_name",
    }.get(field)
    matched = []
    for slide in slides:
        candidate = slide.get(attribute, "") if attribute else ""
        include = True
        if field == "AccessionDate":
            try:
                candidate_date = datetime.datetime.strptime(
                    candidate, "%Y%m%d"
                ).date()
            except ValueError:
                include = False
            else:
                start_date = datetime.date.fromisoformat(start) if start else None
                end_date = datetime.date.fromisoformat(end) if end else None
                include = not (
                    (start_date and candidate_date < start_date)
                    or (end_date and candidate_date > end_date)
                )
        elif field in {"Organ", "PID", "ImageType", "SampAcqType", "SectionCount"}:
            include = not value or candidate.casefold() == value.casefold()
        elif attribute:
            include = not value or value.casefold() in candidate.casefold()
        if include:
            matched.append(slide)

    if sort_order in {"az", "za"}:
        matched.sort(
            key=lambda item: (
                item["destination_name"].casefold(),
                item["original_path"].casefold(),
            ),
            reverse=sort_order == "za",
        )
    elif sort_order in {"date", "date_reverse"}:
        dated = [item for item in matched if item["digitization_date"]]
        undated = [item for item in matched if not item["digitization_date"]]
        dated.sort(
            key=lambda item: (
                item["digitization_date"],
                item["destination_name"].casefold(),
            ),
            reverse=sort_order == "date_reverse",
        )
        matched = dated + undated
    return matched


def _tq_date_summary(slides: List[Dict[str, str]]) -> str:
    values = sorted(
        {slide["digitization_date"] for slide in slides if slide["digitization_date"]}
    )
    if not values:
        return "Unknown"
    return values[0] if len(values) == 1 else f"{values[0]} – {values[-1]}"


def _tq_grouped_rows(
    slides: List[Dict[str, str]], selection_type: str
) -> List[Dict[str, Any]]:
    if selection_type == "Slide":
        return [{"type": "slide", "slide": slide} for slide in slides]
    if selection_type == "Accession":
        grouped: Dict[Tuple[str, str], List[Dict[str, str]]] = defaultdict(list)
        for slide in slides:
            grouped[(slide["batch_name"], slide["accession"])].append(slide)
        return [
            {
                "type": "accession",
                "name": accession,
                "batch_name": batch_name,
                "slides": values,
                "date": _tq_date_summary(values),
            }
            for (batch_name, accession), values in grouped.items()
        ]
    if selection_type == "Type":
        grouped_types: Dict[str, List[Dict[str, str]]] = defaultdict(list)
        for slide in slides:
            for slide_type in slide["sdl_types"]:
                grouped_types[slide_type].append(slide)
        rows = []
        for slide_type, type_slides in grouped_types.items():
            accessions: Dict[
                Tuple[str, str], List[Dict[str, str]]
            ] = defaultdict(list)
            for slide in type_slides:
                accessions[(slide["batch_name"], slide["accession"])].append(slide)
            rows.append(
                {
                    "type": "sdl_type",
                    "name": slide_type,
                    "slides": type_slides,
                    "date": _tq_date_summary(type_slides),
                    "accessions": [
                        {
                            "name": accession,
                            "batch_name": batch_name,
                            "slides": values,
                            "date": _tq_date_summary(values),
                        }
                        for (batch_name, accession), values in accessions.items()
                    ],
                }
            )
        return rows

    batch_groups: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for slide in slides:
        batch_groups[slide["batch_name"]].append(slide)
    rows = []
    for batch_name, batch_slides in batch_groups.items():
        accessions: Dict[str, List[Dict[str, str]]] = defaultdict(list)
        for slide in batch_slides:
            accessions[slide["accession"]].append(slide)
        rows.append(
            {
                "type": "batch",
                "name": batch_name,
                "slides": batch_slides,
                "date": _tq_date_summary(batch_slides),
                "accessions": [
                    {
                        "name": accession,
                        "slides": values,
                        "date": _tq_date_summary(values),
                    }
                    for accession, values in accessions.items()
                ],
            }
        )
    return rows


def _tq_sort_grouped_rows(
    rows: List[Dict[str, Any]], sort_order: str
) -> List[Dict[str, Any]]:
    if sort_order not in {"az", "za", "date", "date_reverse"}:
        return rows
    if sort_order in {"az", "za"}:
        rows.sort(
            key=lambda row: (
                (
                    row["slide"]["destination_name"]
                    if row["type"] == "slide"
                    else row["name"]
                ).casefold()
            ),
            reverse=sort_order == "za",
        )
        for row in rows:
            if "accessions" in row:
                row["accessions"].sort(
                    key=lambda accession: accession["name"].casefold(),
                    reverse=sort_order == "za",
                )
        return rows
    dated = []
    undated = []
    for row in rows:
        date_value = (
            row["slide"]["digitization_date"]
            if row["type"] == "slide"
            else next(
                (
                    slide["digitization_date"]
                    for slide in row["slides"]
                    if slide["digitization_date"]
                ),
                "",
            )
        )
        (dated if date_value else undated).append((date_value, row))
        if "accessions" in row:
            nested_dated = []
            nested_undated = []
            for accession in row["accessions"]:
                accession_date = next(
                    (
                        slide["digitization_date"]
                        for slide in accession["slides"]
                        if slide["digitization_date"]
                    ),
                    "",
                )
                (nested_dated if accession_date else nested_undated).append(
                    (accession_date, accession)
                )
            nested_dated.sort(
                key=lambda item: item[0],
                reverse=sort_order == "date_reverse",
            )
            row["accessions"] = [
                accession for _, accession in nested_dated
            ] + [accession for _, accession in nested_undated]
    dated.sort(
        key=lambda item: item[0],
        reverse=sort_order == "date_reverse",
    )
    return [row for _, row in dated] + [row for _, row in undated]


def _tq_safe_prefix(value: str) -> str:
    cleaned = value.strip().replace("\\", "/").strip("/")
    if not cleaned:
        raise TQError("Destination prefix is required.")
    if re.match(r"^[A-Za-z]:", value.strip()) or value.strip().startswith(("/", "\\")):
        raise TQError("Destination prefix must be relative to ftp_dir.")
    parts = cleaned.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise TQError("Destination prefix contains an unsafe path component.")
    if any("\0" in part for part in parts):
        raise TQError("Destination prefix contains an invalid character.")
    return "/".join(parts)


def _tq_destination_dir(prefix: str, slide: Dict[str, str]) -> str:
    return f"{_tq_safe_prefix(prefix)}/{slide['organ']}/{slide['pid']}"


_TQ_LOCAL_PATH_ASSIGNMENT = re.compile(
    r'^(?P<prefix>\s*(?P<key>source|destination)\s*=\s*)"'
    r'(?P<value>.*)"(?P<suffix>\s*(?:#.*)?)$'
)


def _tq_windows_path_is_unambiguously_literal(value: str) -> bool:
    if '"' in value or any(character in value for character in "\r\n\0"):
        return False
    runs = [len(match.group()) for match in re.finditer(r"\\+", value)]
    if re.match(r"^[A-Za-z]:\\", value):
        return bool(runs) and all(length == 1 for length in runs)
    if re.match(r"^\\\\[^\\]", value):
        return runs[:1] == [2] and all(length == 1 for length in runs[1:])
    return False


def _tq_windows_path_is_correctly_escaped(value: str) -> bool:
    runs = [len(match.group()) for match in re.finditer(r"\\+", value)]
    if re.match(r"^[A-Za-z]:\\", value):
        return bool(runs) and all(length == 2 for length in runs)
    if re.match(r"^\\\\", value):
        return runs[:1] == [4] and all(length == 2 for length in runs[1:])
    return False


def _tq_literal_path(value: str) -> str:
    if "'" not in value:
        return f"'{value}'"
    return '"' + value.replace("\\", "\\\\") + '"'


def _tq_repair_windows_paths(
    contents: str,
) -> Tuple[str, Tuple[Tuple[int, str], ...]]:
    lines = contents.splitlines(keepends=True)
    section = ""
    repairs: List[Tuple[int, str]] = []
    for index, original in enumerate(lines):
        newline = ""
        line = original
        if line.endswith("\n"):
            newline = "\n"
            line = line[:-1]
            if line.endswith("\r"):
                line = line[:-1]
                newline = "\r\n"
        header = re.match(r"^\s*\[([^]]+)]\s*(?:#.*)?$", line)
        if header:
            section = header.group(1).strip()
            continue
        match = _TQ_LOCAL_PATH_ASSIGNMENT.match(line)
        if not match:
            continue
        key = match.group("key")
        if (key, section) not in {("source", "pusher"), ("destination", "puller")}:
            continue
        value = match.group("value")
        looks_like_windows_path = bool(
            re.match(r"^(?:[A-Za-z]:\\|\\\\)", value)
        )
        if not looks_like_windows_path:
            continue
        if not _tq_windows_path_is_unambiguously_literal(value):
            if _tq_windows_path_is_correctly_escaped(value):
                continue
            column = line.find("\\", match.start("value")) + 1
            raise TQError(
                f"TQ configuration line {index + 1}, column {column} uses "
                f"ambiguous mixed backslash escaping in {key!r}. Use a "
                "single-quoted Windows path or double every backslash. "
                "An administrator can correct it at /tq/config."
            )
        lines[index] = (
            match.group("prefix")
            + _tq_literal_path(value)
            + match.group("suffix")
            + newline
        )
        repairs.append((index + 1, key))
    return "".join(lines), tuple(repairs)


def _tq_toml_error(contents: str, exc: tomllib.TOMLDecodeError) -> TQError:
    detail = str(exc)
    location = re.search(r"\(at line (\d+), column (\d+)\)$", detail)
    if not location:
        return TQError(f"TQ configuration is not valid TOML: {detail}")
    line_number = int(location.group(1))
    column = int(location.group(2))
    lines = contents.splitlines()
    excerpt = (
        lines[line_number - 1].strip()[:160]
        if line_number <= len(lines)
        else ""
    )
    hint = ""
    if "Unescaped '\\' in a string" in detail:
        hint = (
            " Windows paths must use single quotes, such as "
            "source = 'D:\\folder', or double each backslash."
        )
    return TQError(
        f"TQ configuration line {line_number}, column {column} is invalid: "
        f"{detail}. Source: {excerpt!r}.{hint} "
        "An administrator can correct it at /tq/config."
    )


def _tq_parse_config(
    contents: str,
) -> Tuple[Dict[str, Any], str, Tuple[Tuple[int, str], ...]]:
    repaired, repairs = _tq_repair_windows_paths(contents)
    try:
        values = tomllib.loads(repaired)
    except tomllib.TOMLDecodeError as exc:
        raise _tq_toml_error(repaired, exc) from exc
    missing = [
        key for key in ("username", "ftp_addr", "ftp_dir")
        if not str(values.get(key, "")).strip()
    ]
    if missing:
        raise TQError(
            f"TQ configuration requires values for: {', '.join(missing)}."
        )
    return values, repaired, repairs


def _tq_backup_name(target: Path) -> Path:
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y%m%dT%H%M%S%fZ"
    )
    return target.with_name(f"{target.name}.bak-{timestamp}")


def _tq_replace_config(
    target: Path,
    contents: str,
    backup_contents: Optional[str] = None,
) -> Optional[Path]:
    root = target.parent
    root.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise TQError("config.toml cannot be edited through a symbolic link.")
    mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else None
    backup_path = None
    if backup_contents is not None:
        backup_path = _tq_backup_name(target)
        try:
            descriptor = os.open(
                backup_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                mode if mode is not None else PRIVATE_FILE_MODE,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                handle.write(backup_contents)
                handle.flush()
                os.fsync(handle.fileno())
            if mode is not None:
                os.chmod(backup_path, mode)
        except Exception:
            backup_path.unlink(missing_ok=True)
            raise
    descriptor, temporary_path = tempfile.mkstemp(
        prefix=".config.", suffix=".toml.tmp", dir=root
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temporary_path, mode)
        os.replace(temporary_path, target)
    finally:
        if os.path.exists(temporary_path):
            os.remove(temporary_path)
    return backup_path


def _tq_config() -> TQConfigResult:
    path = Path(Config.TQ_HOME_DIR).expanduser() / "config.toml"
    with _tq_config_lock:
        try:
            if path.is_symlink():
                raise TQError("config.toml cannot be opened through a symbolic link.")
            contents = path.read_text(encoding="utf-8")
            values, repaired, repairs = _tq_parse_config(contents)
            backup_path = None
            if repairs:
                backup_path = _tq_replace_config(path, repaired, contents)
        except FileNotFoundError as exc:
            raise TQError(f"TQ configuration was not found at {path}.") from exc
        except TQError:
            raise
        except (OSError, UnicodeError) as exc:
            raise TQError(f"TQ configuration could not be read: {exc}") from exc
    return TQConfigResult(
        values=values,
        repaired_lines=tuple(line for line, _ in repairs),
        repaired_keys=tuple(key for _, key in repairs),
        backup_path=backup_path,
    )


def _tq_write_metadata_csv(slides: List[Dict[str, str]]) -> Path:
    directory = Path(Config.INSTANCE_DIR) / "tq_manifests"
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, path_value = tempfile.mkstemp(
        prefix="metadata-", suffix=".csv", dir=directory
    )
    path = Path(path_value)
    accessions: Dict[str, Dict[str, Any]] = {}
    try:
        for slide in slides:
            accession = slide.get("accession", "").strip()
            accession_identity = renaming.accession_key(accession)
            pid = slide.get("pid", "").strip().upper()
            if not accession or not pid:
                raise TQError(
                    "Transfer metadata requires an accession ID and PID for every slide."
                )
            summary = accessions.setdefault(
                accession_identity,
                {"accession_id": accession, "pid": pid, "num_slides": 0},
            )
            if summary["pid"] != pid:
                raise TQError(
                    f"Transfer metadata found multiple PIDs for accession {accession}."
                )
            summary["num_slides"] += 1

        with os.fdopen(descriptor, "w", newline="", encoding="utf-8") as handle:
            descriptor = -1
            writer = csv.DictWriter(handle, fieldnames=TQ_METADATA_FIELDS)
            writer.writeheader()
            writer.writerows(accessions[key] for key in sorted(accessions))
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        if descriptor != -1:
            os.close(descriptor)
        path.unlink(missing_ok=True)
        raise
    return path


def _tq_copath_rows(
    slides: List[Dict[str, str]],
) -> Tuple[List[Dict[str, str]], List[str]]:
    requested: Dict[str, Dict[str, Any]] = {}
    for slide in slides:
        accession = slide.get("accession", "").strip()
        key = renaming.accession_key(accession)
        if not key:
            continue
        entry = requested.setdefault(
            key,
            {"accession": accession, "batch_roots": set()},
        )
        batch_root = slide.get("batch_root", "").strip()
        if batch_root:
            entry["batch_roots"].add(batch_root)

    reports_by_root: Dict[str, Dict[str, Dict[str, str]]] = {}
    exported: Dict[str, Dict[str, str]] = {}
    missing: List[str] = []
    for key in sorted(requested):
        entry = requested[key]
        candidates: List[Dict[str, str]] = []
        try:
            for batch_root in sorted(entry["batch_roots"]):
                if batch_root not in reports_by_root:
                    reports_by_root[batch_root] = renaming.report_rows(
                        Path(batch_root), Path(Config.COPATH_CLONE)
                    )
                reports = reports_by_root[batch_root]
                if key in reports:
                    candidate = {
                        field: reports[key].get(field, "")
                        for field in renaming.COPATH_FIELDS
                    }
                    if candidate not in candidates:
                        candidates.append(candidate)
        except renaming.RenamingError as exc:
            raise TQError(f"CoPath transfer data could not be read: {exc}") from exc
        if not candidates:
            missing.append(entry["accession"])
            continue
        if len(candidates) > 1:
            raise TQError(
                "Conflicting CoPath records were found for accession "
                f"{entry['accession']}."
            )
        exported[key] = candidates[0]
    return [exported[key] for key in sorted(exported)], missing


def _tq_write_copath_csv(
    slides: List[Dict[str, str]],
) -> Tuple[Path, List[str]]:
    directory = Path(Config.INSTANCE_DIR) / "tq_manifests"
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, path_value = tempfile.mkstemp(
        prefix="copath-", suffix=".csv", dir=directory
    )
    path = Path(path_value)
    try:
        rows, missing = _tq_copath_rows(slides)
        with os.fdopen(descriptor, "w", newline="", encoding="utf-8") as handle:
            descriptor = -1
            writer = csv.DictWriter(handle, fieldnames=renaming.COPATH_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        if descriptor != -1:
            os.close(descriptor)
        path.unlink(missing_ok=True)
        raise
    return path, missing


def _tq_write_manifest(
    slides: List[Dict[str, str]],
    metadata_path: Path,
    copath_path: Optional[Path] = None,
) -> Path:
    directory = Path(Config.INSTANCE_DIR) / "tq_manifests"
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, path_value = tempfile.mkstemp(
        prefix="transfer-", suffix=".csv", dir=directory
    )
    try:
        with os.fdopen(descriptor, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=("original_path", "destination_dir", "destination_name"),
            )
            writer.writeheader()
            writer.writerow(
                {
                    "original_path": str(metadata_path),
                    "destination_dir": slides[0]["staging_dir"],
                    "destination_name": "metadata.csv",
                }
            )
            if copath_path is not None:
                writer.writerow(
                    {
                        "original_path": str(copath_path),
                        "destination_dir": slides[0]["staging_dir"],
                        "destination_name": "copath_data.csv",
                    }
                )
            writer.writerows(
                {
                    "original_path": slide.get(
                        "staged_path", slide["original_path"]
                    ),
                    "destination_dir": slide["destination_dir"],
                    "destination_name": slide["destination_name"],
                }
                for slide in slides
            )
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        Path(path_value).unlink(missing_ok=True)
        raise
    return Path(path_value)


def _tq_history_successes() -> set:
    successes = set()
    root = _tq_transfer_log_root()
    if not root.is_dir():
        return successes
    for path in root.glob("*/*.csv"):
        try:
            with path.open("r", newline="", encoding="utf-8-sig") as handle:
                reader = csv.DictReader(handle)
                for row in reader:
                    if row.get("status") == "SUCCESS" and row.get("original_path"):
                        successes.add(row["original_path"])
        except (OSError, UnicodeError, csv.Error):
            app.logger.warning("Could not read TQ transfer history %s", path)
    return successes


def _tq_update_sdl_push_status(all_slides: List[Dict[str, str]]) -> int:
    successes = _tq_history_successes()
    grouped: Dict[Tuple[str, str], set] = defaultdict(set)
    for slide in all_slides:
        if slide["digitization_date"]:
            grouped[(renaming.accession_key(slide["accession"]), slide["digitization_date"])].add(
                slide["original_path"]
            )
    workbook = None
    changed = 0
    with _sdl_workbook_lock:
        workbook, worksheet, _ = _load_sdl_workbook()
        try:
            columns = _sdl_header_columns(worksheet)
            for row_number in range(2, worksheet.max_row + 1):
                accession = str(
                    worksheet.cell(
                        row=row_number, column=columns["Accession ID"]
                    ).value or ""
                ).strip()
                loaded = _format_sdl_value(
                    "Date Loaded",
                    worksheet.cell(
                        row=row_number, column=columns["Date Loaded"]
                    ).value,
                )
                paths = grouped.get((renaming.accession_key(accession), loaded), set())
                if not paths or not paths.issubset(successes):
                    continue
                status_cell = worksheet.cell(
                    row=row_number, column=columns["Pushed to SFTP Server"]
                )
                if not _coerce_sdl_bool(status_cell.value):
                    status_cell.value = True
                    changed += 1
            if changed:
                _save_sdl_workbook(workbook)
        finally:
            workbook.close()
    return changed


def _tq_write_transfer_log(job: TQJob) -> Path:
    root = _tq_transfer_log_root()
    date_directory = root / job.started_at.strftime("%Y-%m-%d")
    date_directory.mkdir(parents=True, exist_ok=True)
    base_name = job.started_at.strftime("%H-%M-%S.%f")
    target = date_directory / f"{base_name}.csv"
    counter = 1
    while target.exists():
        target = date_directory / f"{base_name}-{counter}.csv"
        counter += 1
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=TQ_LOG_FIELDS)
            writer.writeheader()
            for slide in job.slides:
                result = job.results.get(slide["original_path"])
                result_error = job.result_errors.get(slide["original_path"])
                if result_error:
                    status = result_error
                elif result is None:
                    status = (
                        "tq did not report a result "
                        f"(exit code {job.return_code})."
                    )
                elif result["success"]:
                    status = "SUCCESS"
                else:
                    status = result["error"]
                writer.writerow(
                    {
                        "original_path": slide["original_path"],
                        "destination_dir": slide["destination_dir"],
                        "destination_name": slide["destination_name"],
                        "organ": slide["organ"],
                        "pid": slide["pid"],
                        "digitization_date": slide["digitization_date"],
                        "status": status,
                    }
                )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def _tq_parse_result_line(job: TQJob, line: str) -> None:
    try:
        result = json.loads(line)
    except json.JSONDecodeError:
        return
    if not isinstance(result, dict) or "original_path" not in result:
        return
    reported_path = result.get("original_path")
    success = result.get("success")
    error = result.get("error")
    expected = {
        slide.get("staged_path", slide["original_path"]): slide["original_path"]
        for slide in job.slides
    }
    original_path = expected.get(reported_path) if isinstance(reported_path, str) else None
    if original_path is not None:
        if original_path in job.results or original_path in job.result_errors:
            job.results.pop(original_path, None)
            job.result_errors[original_path] = (
                "tq reported more than one result for this slide."
            )
            return
        if not isinstance(success, bool) or (
            not success and not isinstance(error, str)
        ):
            job.result_errors[original_path] = (
                "tq reported a malformed result for this slide."
            )
            return
    if (
        original_path is None
        or not isinstance(success, bool)
        or (not success and not isinstance(error, str))
    ):
        return
    job.results[original_path] = {
        "success": success,
        "error": "" if success else error.strip() or "tq reported an unspecified error.",
    }


def _tq_prune_empty_staging_directories(path: Path) -> None:
    root = _tq_staging_root()
    current = path.parent
    while current != root:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def _tq_cleanup_successful_staging(job: TQJob) -> None:
    for slide in job.slides:
        result = job.results.get(slide["original_path"])
        staged_value = slide.get("staged_path")
        if not staged_value or not result or not result["success"]:
            continue
        staged_path = Path(staged_value)
        try:
            staged_path.unlink(missing_ok=True)
            _tq_prune_empty_staging_directories(staged_path)
            _tq_append_output(job, f"\nRemoved staged file {staged_path}.\n")
        except OSError as exc:
            app.logger.warning("Could not remove staged slide %s: %s", staged_path, exc)
            _tq_append_output(
                job, f"\nUploaded successfully, but staged file could not be removed: {exc}\n"
            )


def _tq_download_slides(job: TQJob) -> None:
    for index, slide in enumerate(job.slides, start=1):
        source = Path(slide["original_path"])
        target = Path(slide["staged_path"])
        try:
            if not source.is_file():
                raise TQError(f"Source slide is not a regular file: {source}")
            target.parent.mkdir(parents=True, exist_ok=True)
            _tq_staging_path(slide)
            temporary = target.with_name(
                f".{target.stem}.{uuid.uuid4().hex}.copying{target.suffix}"
            )
            _tq_append_output(
                job,
                f"[{index}/{len(job.slides)}] Downloading {source} to "
                f"{_tq_staging_display_path(target)}.\n",
            )
            try:
                # Do not preserve GT450 mode bits. Scanner files can be read-only,
                # while deidentify_anonymize.py must reopen staged copies with r+b.
                shutil.copyfile(source, temporary)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
            _tq_append_output(
                job,
                f"[{index}/{len(job.slides)}] Downloaded "
                f"{_tq_staging_display_path(target)}.\n",
            )
        except Exception as exc:
            message = f"Staging failed: {exc}"
            job.result_errors[slide["original_path"]] = message
            raise TQError(message) from exc

    staging_directory = Path(job.slides[0]["staged_path"]).parent
    selected_paths = {
        Path(slide["staged_path"]).resolve() for slide in job.slides
    }
    unexpected = sorted(
        path for path in staging_directory.rglob("*.svs")
        if path.resolve() not in selected_paths
    )
    if unexpected:
        raise TQError(
            "Staging batch folder contains an unselected slide: "
            f"{_tq_staging_display_path(unexpected[0])}"
        )


def _tq_temporary_csv(prefix: str) -> Path:
    directory = Path(Config.INSTANCE_DIR) / "tq_manifests"
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, path_value = tempfile.mkstemp(
        prefix=prefix, suffix=".csv", dir=directory
    )
    os.close(descriptor)
    return Path(path_value)


def _tq_stream_process_output(job: TQJob, process: subprocess.Popen) -> None:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    if process.stdout is not None:
        while True:
            chunk = process.stdout.read(4096)
            if not chunk:
                break
            _tq_append_output(job, decoder.decode(chunk))
    trailing = decoder.decode(b"", final=True)
    if trailing:
        _tq_append_output(job, trailing)


def _tq_deidentify_slides(job: TQJob) -> None:
    output_log = _tq_temporary_csv("deidentify-results-")
    try:
        staging_directory = Path(job.slides[0]["staged_path"]).parent
        _tq_append_output(
            job,
            f"All {len(job.slides)} selected slide(s) downloaded.\n"
            "Starting deidentify_anonymize.py on "
            f"{_tq_staging_display_path(staging_directory)}.\n",
        )
        command = [
            sys.executable,
            "-u",
            str(Path(deidentify_anonymize.__file__).resolve()),
            str(staging_directory),
            "--output-log",
            str(output_log),
        ]
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )
        _tq_stream_process_output(job, process)
        return_code = process.wait()
        if return_code != 0:
            if output_log.is_file():
                with output_log.open("r", newline="", encoding="utf-8-sig") as handle:
                    results = {
                        row.get("original_path", ""): row.get("status", "")
                        for row in csv.DictReader(handle)
                    }
                for slide in job.slides:
                    if results.get(slide["staged_path"]) == "FAILURE":
                        job.result_errors[slide["original_path"]] = (
                            f"Deidentification failed for {slide['staged_path']}"
                        )
            raise TQError(
                f"deidentify_anonymize.py exited with code {return_code}"
            )
        _tq_append_output(job, "deidentify_anonymize.py completed successfully.\n")
    finally:
        output_log.unlink(missing_ok=True)


def _tq_fail_before_upload(job: TQJob, error: Exception) -> None:
    for slide in job.slides:
        job.result_errors.setdefault(
            slide["original_path"],
            f"Upload not started: {error}",
        )
    _tq_append_output(job, f"\nTransfer stopped before upload: {error}\n")
    try:
        job.log_path = _tq_write_transfer_log(job)
    except Exception:
        app.logger.exception("Could not write failed TQ staging log")
    job.status = "failed"


def _run_tq_job(job: TQJob) -> None:
    global _tq_active_job_id
    try:
        _tq_download_slides(job)
        _tq_deidentify_slides(job)
        _tq_append_output(job, "Starting TQ upload.\n")
        job.metadata_path = _tq_write_metadata_csv(job.slides)
        if job.missing_copath_accessions:
            _tq_append_output(
                job,
                "CoPath data was unavailable for accession(s): "
                + ", ".join(job.missing_copath_accessions)
                + ". Continuing with available CoPath data.\n",
            )
        job.manifest_path = _tq_write_manifest(
            job.slides, job.metadata_path, job.copath_path
        )
        command = [
            Config.TQ_EXECUTABLE,
            "pusher",
            "--paths",
            str(job.manifest_path),
        ]
        job.process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )
    except Exception as exc:
        _tq_fail_before_upload(job, exc)
        _tq_cleanup_job_files(job)
        with _tq_state_lock:
            if _tq_active_job_id == job.id:
                _tq_active_job_id = None
        return
    _read_tq_output(job)


def _tq_cleanup_job_files(job: TQJob) -> None:
    for path in (job.manifest_path, job.metadata_path, job.copath_path):
        if path is not None:
            path.unlink(missing_ok=True)


def _read_tq_output(job: TQJob) -> None:
    global _tq_active_job_id
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pending = ""
    try:
        if job.process is None:
            raise TQError("The TQ process was not started.")
        if job.process.stdout is not None:
            while True:
                chunk = job.process.stdout.read(4096)
                if not chunk:
                    break
                text = decoder.decode(chunk)
                with _tq_state_lock:
                    job.output += text
                pending += text
                lines = pending.splitlines(keepends=True)
                pending = ""
                if lines and not lines[-1].endswith(("\n", "\r")):
                    pending = lines.pop()
                for line in lines:
                    _tq_parse_result_line(job, line.strip())
        trailing = pending + decoder.decode(b"", final=True)
        if trailing:
            _tq_parse_result_line(job, trailing.strip())
        job.return_code = job.process.wait()
        _tq_cleanup_successful_staging(job)
        job.log_path = _tq_write_transfer_log(job)
        try:
            updated_rows = _tq_update_sdl_push_status(job.all_slides)
            if updated_rows:
                with _tq_state_lock:
                    job.output += (
                        f"\nUpdated {updated_rows} Slide Digitization Log row(s).\n"
                    )
        except Exception as exc:
            app.logger.exception("Could not update SDL after TQ transfer")
            with _tq_state_lock:
                job.output += (
                    "\nTransfer results were logged, but the Slide Digitization "
                    f"Log could not be updated: {exc}\n"
                )
        complete = (
            job.return_code == 0
            and len(job.results) == len(job.slides)
            and not job.result_errors
            and all(result["success"] for result in job.results.values())
        )
        job.status = "succeeded" if complete else "failed"
    except Exception as exc:
        app.logger.exception("TQ transfer finalization failed")
        with _tq_state_lock:
            job.output += f"\nLauncher error: {exc}\n"
        job.status = "failed"
        job.return_code = job.process.poll() if job.process else None
    finally:
        _tq_cleanup_job_files(job)
        with _tq_state_lock:
            if _tq_active_job_id == job.id:
                _tq_active_job_id = None


def _start_tq_job(
    owner_id: str,
    slides: List[Dict[str, str]],
    all_slides: List[Dict[str, str]],
    copath_path: Optional[Path] = None,
    missing_copath_accessions: Sequence[str] = (),
) -> TQJob:
    global _tq_active_job_id
    config_result = _tq_config()
    _tq_prepare_staging_paths(slides)
    with _tq_state_lock:
        if _tq_active_job_id:
            active = _tq_jobs.get(_tq_active_job_id)
            if active and active.status == "running":
                raise TQError("Another transfer is already running.")
            _tq_active_job_id = None
        job = TQJob(
            uuid.uuid4().hex,
            owner_id,
            None,
            slides,
            all_slides,
            copath_path=copath_path,
            missing_copath_accessions=missing_copath_accessions,
        )
        job.config_result = config_result
        _tq_jobs[job.id] = job
        _tq_active_job_id = job.id
    threading.Thread(target=_run_tq_job, args=(job,), daemon=True).start()
    return job


def _tq_job_for_user(job_id: Optional[str]) -> Optional[TQJob]:
    if not job_id:
        return None
    with _tq_state_lock:
        job = _tq_jobs.get(job_id)
        if job is None:
            return None
        if job.owner_id != str(current_user.id) and not current_user.is_admin:
            return None
        return job


def _tq_safe_path(relative_path: str, expected: str = "any") -> Path:
    root = Path(Config.TQ_HOME_DIR).expanduser().resolve()
    normalized = relative_path.replace("\\", "/").strip("/")
    if not normalized:
        if expected == "file":
            raise TQError("The requested TQ file is unavailable.")
        return root
    lexical = Path(normalized)
    if lexical.is_absolute() or any(part in {"", ".", ".."} for part in lexical.parts):
        raise TQError("The requested TQ path is outside the TQ directory.")
    current = root
    for part in lexical.parts:
        current = current / part
        if current.is_symlink():
            raise TQError("Symbolic links cannot be opened from the TQ log browser.")
    try:
        relative = current.resolve().relative_to(root)
    except (OSError, ValueError) as exc:
        raise TQError("The requested TQ path is outside the TQ directory.") from exc
    resolved = root / relative
    if expected == "file" and not resolved.is_file():
        raise TQError("The requested TQ file is unavailable.")
    if expected == "directory" and not resolved.is_dir():
        raise TQError("The requested TQ folder is unavailable.")
    if expected == "any" and not resolved.exists():
        raise TQError("The requested TQ path is unavailable.")
    return resolved


def _tq_relative_path(path: Path) -> str:
    root = Path(Config.TQ_HOME_DIR).expanduser().resolve()
    return str(path.resolve().relative_to(root)).replace(os.sep, "/")


def _save_tq_config(contents: str) -> TQConfigResult:
    if len(contents.encode("utf-8")) > 1024 * 1024:
        raise TQError("config.toml cannot exceed 1 MiB.")
    values, repaired, repairs = _tq_parse_config(contents)
    target = Path(Config.TQ_HOME_DIR).expanduser() / "config.toml"
    with _tq_config_lock:
        if target.is_symlink():
            raise TQError("config.toml cannot be edited through a symbolic link.")
        previous = None
        if repairs:
            try:
                previous = target.read_text(encoding="utf-8")
            except FileNotFoundError:
                previous = contents
            except UnicodeError as exc:
                raise TQError(
                    f"Existing config.toml could not be read: {exc}"
                ) from exc
        backup_path = _tq_replace_config(target, repaired, previous)
    return TQConfigResult(
        values=values,
        repaired_lines=tuple(line for line, _ in repairs),
        repaired_keys=tuple(key for _, key in repairs),
        backup_path=backup_path,
    )


# ==============================================================================
# 9. PIPELINE LAUNCHER
# ==============================================================================
PIPELINE_FORM_DEFAULTS = {
    "input_dir": "",
    "output_dir": "",
    "start_from": "1",
    "end_at": "3",
    "input_mode": "auto",
    "macro_workers": "4",
    "macro_extensions": "svs",
    "macro_image_extensions": "png, jpg, jpeg, tif, tiff, bmp",
    "thumbnail_width": "300",
    "thumbnail_height": "300",
    "ocr_workers": "4",
    "ocr_use_cpu": "",
    "naming_accession_pattern": r"\b([A-Za-z]{1,3}\s*\d{2}\s*[ -/]\s*\d+)\b",
    "naming_workers": "4",
}


class PipelineJob:
    """In-memory state for one pipeline child process."""

    def __init__(
        self,
        job_id: str,
        owner_id: str,
        process: subprocess.Popen,
        output_path: Optional[str] = None,
    ):
        self.id = job_id
        self.owner_id = owner_id
        self.process = process
        self.status = "running"
        self.return_code: Optional[int] = None
        self.output = ""
        self.output_path = output_path


_pipeline_jobs: Dict[str, PipelineJob] = {}
_pipeline_jobs_lock = threading.Lock()
_pipeline_active_job_id: Optional[str] = None


def _pipeline_form_values(source=None) -> Dict[str, str]:
    values = dict(PIPELINE_FORM_DEFAULTS)
    if source is not None:
        for key in values:
            submitted = source.get(key)
            if submitted is not None:
                values[key] = submitted
    return values


def _positive_pipeline_integer(
    values: Dict[str, str],
    field: str,
    label: str,
    errors: List[str],
    maximum: int,
) -> Optional[int]:
    try:
        value = int(values[field])
        if value <= 0:
            raise ValueError
        if value > maximum:
            errors.append(f"{label} must not exceed {maximum}.")
        return value
    except (TypeError, ValueError):
        errors.append(f"{label} must be a positive whole number.")
        return None


def _pipeline_allowed_roots(config_key: str) -> List[Path]:
    configured = app.config.get(config_key, ())
    if isinstance(configured, str):
        configured = [item for item in configured.split(os.pathsep) if item]
    return [runtime_path(str(item)).expanduser().resolve() for item in configured]


def _pipeline_path_is_allowed(candidate: Path, roots: List[Path]) -> bool:
    for root in roots:
        try:
            candidate.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _pipeline_extensions(value: str, label: str, errors: List[str]) -> List[str]:
    extensions = [item.lstrip(".") for item in re.split(r"[\s,]+", value.strip()) if item]
    if not extensions:
        errors.append(f"{label} must contain at least one extension.")
    elif any(not re.fullmatch(r"[A-Za-z0-9.]+", item) for item in extensions):
        errors.append(f"{label} may contain only letters, numbers, and periods.")
    return extensions


def _pipeline_command(values: Dict[str, str]) -> Tuple[Optional[List[str]], List[str]]:
    errors: List[str] = []
    input_text = values["input_dir"].strip()
    output_text = values["output_dir"].strip()
    if not input_text:
        errors.append("Input directory is required.")
    if not output_text:
        errors.append("Output directory is required.")

    input_dir = runtime_path(input_text).expanduser().resolve() if input_text else None
    output_dir = runtime_path(output_text).expanduser().resolve() if output_text else None
    input_roots = _pipeline_allowed_roots("PIPELINE_INPUT_ROOTS")
    output_roots = _pipeline_allowed_roots("PIPELINE_OUTPUT_ROOTS")
    if not input_roots:
        errors.append("Pipeline input roots are not configured.")
    elif any(not root.is_dir() for root in input_roots):
        errors.append("Every configured pipeline input root must be an existing directory.")
    if not output_roots:
        errors.append("Pipeline output roots are not configured.")
    elif any(not root.is_dir() for root in output_roots):
        errors.append("Every configured pipeline output root must be an existing directory.")
    if input_dir is not None and not input_dir.is_dir():
        errors.append("Input directory must be an existing directory on the server.")
    elif input_dir is not None and input_roots and not _pipeline_path_is_allowed(
        input_dir, input_roots
    ):
        errors.append("Input directory is outside the configured pipeline input roots.")
    if output_dir is not None and output_dir.exists() and not output_dir.is_dir():
        errors.append("Output directory points to a file, not a directory.")
    elif output_dir is not None and output_roots and not _pipeline_path_is_allowed(
        output_dir, output_roots
    ):
        errors.append("Output directory is outside the configured pipeline output roots.")

    try:
        start_stage = int(values["start_from"])
        end_stage = int(values["end_at"])
        if start_stage not in (1, 2, 3) or end_stage not in (1, 2, 3):
            raise ValueError
        if end_stage < start_stage:
            errors.append("End at cannot be earlier than Start from.")
    except (TypeError, ValueError):
        start_stage = end_stage = 0
        errors.append("Choose valid starting and ending stages.")

    macro_workers = _positive_pipeline_integer(
        values, "macro_workers", "Get macro workers", errors,
        app.config["PIPELINE_MAX_WORKERS"],
    )
    thumbnail_width = _positive_pipeline_integer(
        values, "thumbnail_width", "Thumbnail width", errors,
        app.config["PIPELINE_MAX_THUMBNAIL_DIMENSION"],
    )
    thumbnail_height = _positive_pipeline_integer(
        values, "thumbnail_height", "Thumbnail height", errors,
        app.config["PIPELINE_MAX_THUMBNAIL_DIMENSION"],
    )
    ocr_workers = _positive_pipeline_integer(
        values, "ocr_workers", "Dual OCR workers", errors,
        app.config["PIPELINE_MAX_WORKERS"],
    )
    naming_workers = _positive_pipeline_integer(
        values, "naming_workers", "Name files workers", errors,
        app.config["PIPELINE_MAX_WORKERS"],
    )
    macro_extensions = _pipeline_extensions(
        values["macro_extensions"], "Slide extensions", errors
    )
    image_extensions = _pipeline_extensions(
        values["macro_image_extensions"], "Image extensions", errors
    )

    if values["input_mode"] not in ("auto", "slides", "images"):
        errors.append("Choose a valid input mode.")
    try:
        re.compile(values["naming_accession_pattern"])
    except re.error as exc:
        errors.append(f"Accession pattern is not a valid regular expression: {exc}")

    if output_dir is not None and start_stage == 2:
        if not (output_dir / "slide_mapping.csv").is_file():
            errors.append(
                "Starting from Dual OCR requires slide_mapping.csv in the output directory."
            )
    if output_dir is not None and start_stage == 3:
        if not (output_dir / "ocr.csv").is_file():
            errors.append(
                "Starting from Name files requires ocr.csv in the output directory."
            )

    pipeline_script = Path(__file__).resolve().with_name("pipeline.py")
    if not pipeline_script.is_file():
        errors.append(f"Pipeline script is unavailable: {pipeline_script}")
    if errors:
        return None, errors

    command = [
        sys.executable,
        "-u",
        str(pipeline_script),
        "--input-dir",
        str(input_dir),
        "--output-dir",
        str(output_dir),
        "--start-from",
        str(start_stage),
        "--end-at",
        str(end_stage),
        "--input-mode",
        values["input_mode"],
        "--macro-workers",
        str(macro_workers),
        "--macro-extensions",
        *macro_extensions,
        "--macro-image-extensions",
        *image_extensions,
        "--macro-thumbnail-size",
        str(thumbnail_width),
        str(thumbnail_height),
        "--ocr-workers",
        str(ocr_workers),
        "--naming-accession-pattern",
        values["naming_accession_pattern"],
        "--naming-workers",
        str(naming_workers),
    ]
    if values["ocr_use_cpu"] == "on":
        command.append("--ocr-use-cpu")
    return command, []


def _read_pipeline_output(job: PipelineJob) -> None:
    """Drain merged child output and finalize job state."""
    global _pipeline_active_job_id
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    try:
        if job.process.stdout is not None:
            while True:
                chunk = job.process.stdout.read(4096)
                if not chunk:
                    break
                text = decoder.decode(chunk)
                if text:
                    with _pipeline_jobs_lock:
                        job.output += text
                    if job.output_path:
                        with open(job.output_path, "a", encoding="utf-8") as output_file:
                            output_file.write(text)
            trailing_text = decoder.decode(b"", final=True)
            if trailing_text:
                with _pipeline_jobs_lock:
                    job.output += trailing_text
                if job.output_path:
                    with open(job.output_path, "a", encoding="utf-8") as output_file:
                        output_file.write(trailing_text)
        return_code = job.process.wait()
        with _pipeline_jobs_lock:
            job.return_code = return_code
            job.status = "succeeded" if return_code == 0 else "failed"
        if job.output_path:
            api_store.update_job(
                job.id,
                status=job.status,
                return_code=return_code,
                completed_at=_iso_utc(),
            )
    except Exception as exc:
        app.logger.exception("Failed while reading pipeline output")
        with _pipeline_jobs_lock:
            job.output += f"\nLauncher error while reading output: {exc}\n"
            job.return_code = job.process.poll()
            job.status = "failed"
        if job.output_path:
            with open(job.output_path, "a", encoding="utf-8") as output_file:
                output_file.write(f"\nLauncher error while reading output: {exc}\n")
            api_store.update_job(
                job.id,
                status="failed",
                return_code=job.return_code,
                completed_at=_iso_utc(),
            )
    finally:
        with _pipeline_jobs_lock:
            if _pipeline_active_job_id == job.id:
                _pipeline_active_job_id = None


def _start_pipeline_job(
    command: List[str],
    owner_id: str,
    *,
    job_id: Optional[str] = None,
    request_values: Optional[Dict[str, Any]] = None,
    token_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    payload_hash: Optional[str] = None,
) -> PipelineJob:
    global _pipeline_active_job_id
    with _pipeline_jobs_lock:
        if _pipeline_active_job_id is not None:
            active_job = _pipeline_jobs.get(_pipeline_active_job_id)
            if active_job is not None and active_job.status == "running":
                raise RuntimeError("Another Label-Check pipeline is already running.")
            _pipeline_active_job_id = None

        resolved_job_id = job_id or str(uuid.uuid4())
        try:
            output_path = api_store.reserve_job(
                resolved_job_id,
                owner_id,
                request_values or {},
                command,
                token_id,
                idempotency_key,
                payload_hash,
            )
        except sqlite3.IntegrityError as exc:
            raise RuntimeError("Another Label-Check pipeline is already running.") from exc

        try:
            process = subprocess.Popen(
                command,
                cwd=Path(__file__).resolve().parent.parent,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
        except OSError:
            api_store.update_job(
                resolved_job_id, status="failed", completed_at=_iso_utc()
            )
            raise
        job = PipelineJob(resolved_job_id, owner_id, process, output_path)
        _pipeline_jobs[job.id] = job
        _pipeline_active_job_id = job.id
        api_store.update_job(
            job.id,
            status="running",
            started_at=_iso_utc(),
            launcher_pid=os.getpid(),
        )

    reader = threading.Thread(target=_read_pipeline_output, args=(job,), daemon=True)
    reader.start()
    return job


def _pipeline_job_for_user(job_id: str) -> Optional[PipelineJob]:
    with _pipeline_jobs_lock:
        job = _pipeline_jobs.get(job_id)
        if job is None:
            return None
        if job.owner_id != str(current_user.id) and not current_user.is_admin:
            return None
        return job


def _pipeline_is_busy() -> bool:
    with _pipeline_jobs_lock:
        if _pipeline_active_job_id is None:
            return False
        job = _pipeline_jobs.get(_pipeline_active_job_id)
        return job is not None and job.status == "running"


def csrf_token() -> str:
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


app.jinja_env.globals["csrf_token"] = csrf_token


def csp_nonce() -> str:
    """Return the per-request nonce used by inline application assets."""
    nonce = getattr(g, "csp_nonce", None)
    if nonce is None:
        nonce = secrets.token_urlsafe(16)
        g.csp_nonce = nonce
    return nonce


app.jinja_env.globals["csp_nonce"] = csp_nonce


def safe_login_redirect(value: Optional[str]) -> Optional[str]:
    """Accept only root-relative paths on this application."""
    if not value or not value.startswith("/") or value.startswith("//"):
        return None
    if "\\" in value or any(ord(character) < 0x20 for character in value):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme or parsed.netloc:
        return None
    return value


def admin_required(view):
    """Require an authenticated administrator for a browser route."""
    @functools.wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def _api_problem(status: int, code: str, title: str, detail: str):
    response = jsonify(
        {
            "type": f"https://inslide.invalid/problems/{code}",
            "status": status,
            "code": code,
            "title": title,
            "detail": detail,
            "request_id": getattr(g, "request_id", uuid.uuid4().hex),
        }
    )
    response.status_code = status
    response.content_type = "application/problem+json"
    if status == 401:
        response.headers["WWW-Authenticate"] = 'Bearer realm="inslide-api"'
    return response


def _require_api_scope(scope: str, bucket: str = "read"):
    def decorator(view):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            if app.config.get("API_REQUIRE_HTTPS", True) and not app.testing and not request.is_secure:
                return _api_problem(400, "https_required", "HTTPS required", "The API is available only over HTTPS.")
            header = request.headers.get("Authorization", "")
            if not header.startswith("Bearer ") or header.count(" ") != 1:
                return _api_problem(401, "invalid_token", "Authentication required", "Provide a valid bearer token.")
            token = api_store.authenticate_token(header[7:])
            user = user_manager.get(token["user_id"]) if token else None
            if token is None or user is None:
                return _api_problem(401, "invalid_token", "Authentication required", "The bearer token is invalid, expired, or revoked.")
            g.api_token = token
            g.api_user = user
            if scope not in token["scopes"]:
                return _api_problem(403, "insufficient_scope", "Insufficient scope", f"This endpoint requires the {scope} scope.")
            limit = (
                app.config["API_SUBMIT_RATE_LIMIT"]
                if bucket == "submit"
                else app.config["API_READ_RATE_LIMIT"]
            )
            allowed, remaining, retry_after = api_store.rate_limit(
                token["token_id"], bucket, limit, app.config["API_RATE_WINDOW_SECONDS"]
            )
            g.rate_limit = limit
            g.rate_remaining = remaining
            g.rate_retry_after = retry_after
            if not allowed:
                response = _api_problem(429, "rate_limit_exceeded", "Rate limit exceeded", "Retry after the current rate-limit window.")
                response.headers["Retry-After"] = str(retry_after)
                return response
            return view(*args, **kwargs)
        return wrapped
    return decorator


def _api_job_document(record: Dict[str, Any]) -> Dict[str, Any]:
    job_id = record["job_id"]
    return {
        "id": job_id,
        "status": record["status"],
        "created_at": record["created_at"],
        "started_at": record.get("started_at"),
        "completed_at": record.get("completed_at"),
        "return_code": record.get("return_code"),
        "links": {
            "self": url_for("api_pipeline_job", job_id=job_id, _external=True),
            "output": url_for("api_pipeline_job_output", job_id=job_id, _external=True),
        },
    }


@app.after_request
def response_security_metadata(response):
    nonce = csp_nonce()
    response.headers["Content-Security-Policy"] = "; ".join(
        (
            "default-src 'self'",
            f"script-src 'self' 'nonce-{nonce}'",
            f"style-src 'self' 'nonce-{nonce}'",
            "img-src 'self' data:",
            "font-src 'self'",
            "connect-src 'self'",
            "object-src 'none'",
            "base-uri 'self'",
            "frame-ancestors 'none'",
            "form-action 'self'",
        )
    )
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if request.is_secure:
        response.headers["Strict-Transport-Security"] = "max-age=31536000"

    if request.path.startswith("/api/v1/"):
        request_id = getattr(g, "request_id", uuid.uuid4().hex)
        response.headers["X-Request-ID"] = request_id
        if hasattr(g, "rate_limit"):
            response.headers["X-RateLimit-Limit"] = str(g.rate_limit)
            response.headers["X-RateLimit-Remaining"] = str(g.rate_remaining)
        token = getattr(g, "api_token", None)
        app.logger.info(
            "API_AUDIT %s",
            json.dumps(
                {
                    "request_id": request_id,
                    "token_id": token.get("token_id") if token else None,
                    "user_id": token.get("user_id") if token else None,
                    "method": request.method,
                    "path": request.path,
                    "status": response.status_code,
                    "remote_addr": request.remote_addr,
                    "idempotency_key": request.headers.get("Idempotency-Key"),
                },
                sort_keys=True,
            ),
        )
    return response


@app.errorhandler(404)
def api_not_found(error):
    if request.path.startswith("/api/v1/"):
        return _api_problem(404, "not_found", "Not found", "The requested API resource does not exist.")
    return error


@app.errorhandler(405)
def api_method_not_allowed(error):
    if request.path.startswith("/api/v1/"):
        return _api_problem(405, "method_not_allowed", "Method not allowed", "This API resource does not support the requested method.")
    return error


@app.errorhandler(500)
def api_internal_error(error):
    if request.path.startswith("/api/v1/"):
        return _api_problem(500, "internal_error", "Internal server error", "The request could not be completed.")
    return error


# ==============================================================================
# 10. FLASK ROUTES
# ==============================================================================
@app.before_request
def before_request_handler():
    g.csp_nonce = secrets.token_urlsafe(16)
    if not app.testing:
        try:
            validate_security_config()
        except SecurityConfigurationError as exc:
            app.logger.critical("Invalid security configuration: %s", exc)
            return "Server security configuration is invalid.", 500

    if request.path.startswith("/api/v1/"):
        supplied_request_id = request.headers.get("X-Request-ID", "")
        g.request_id = (
            supplied_request_id
            if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", supplied_request_id)
            else uuid.uuid4().hex
        )
        return None

    if current_user.is_authenticated:
        try:
            stats_store.note_presence(str(current_user.id))
        except Exception:
            app.logger.exception("Could not record user presence")

    if (
        request.method in {"POST", "PUT", "PATCH", "DELETE"}
        and app.config.get("CSRF_ENABLED", True)
        and not app.testing
    ):
        expected = session.get("_csrf_token", "")
        supplied = request.form.get("csrf_token", "") or request.headers.get("X-CSRF-Token", "")
        if not expected or not supplied or not hmac.compare_digest(expected, supplied):
            return "Invalid or missing CSRF token.", 400

    if request.endpoint in [
        "static",
        "serve_relative_image",
        "login",
        "logout",
        "sdl",
        "inventories",
    ]:
        return

    session.setdefault("show_only_incomplete", False)


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    next_url = safe_login_redirect(
        request.form.get("next") or request.args.get("next")
    )
    
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        client_address = request.remote_addr or "unknown"
        limit_arguments = (
            username,
            client_address,
            app.config["LOGIN_PAIR_ATTEMPT_LIMIT"],
            app.config["LOGIN_ACCOUNT_ATTEMPT_LIMIT"],
            app.config["LOGIN_RATE_WINDOW_SECONDS"],
        )
        allowed, retry_after = api_store.login_rate_limit(*limit_arguments)
        if not allowed:
            flash("Too many login attempts. Try again later.", "error")
            return (
                render_template(
                    "login.html", messages=flash_messages(), next_url=next_url
                ),
                429,
                {"Retry-After": str(retry_after)},
            )

        user = user_manager.get(username)
        password_is_bounded = len(password) <= MAX_PASSWORD_LENGTH
        if user and password_is_bounded and user.verify_password(password):
            api_store.clear_login_failures(username, client_address)
            login_user(user)
            try:
                stats_store.note_presence(user.id)
            except Exception:
                app.logger.exception("Could not record login presence")
            app.logger.info(f"User '{username}' logged in successfully.")
            return redirect(next_url or url_for("index"))

        api_store.record_login_failure(
            username, client_address, app.config["LOGIN_RATE_WINDOW_SECONDS"]
        )
        allowed, retry_after = api_store.login_rate_limit(*limit_arguments)
        if not allowed:
            flash("Too many login attempts. Try again later.", "error")
            return (
                render_template(
                    "login.html", messages=flash_messages(), next_url=next_url
                ),
                429,
                {"Retry-After": str(retry_after)},
            )
        flash("Invalid username or password.", "error")
        
    return render_template(
        "login.html",
        messages=flash_messages(),
        next_url=next_url,
    )


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


@app.route("/users")
@login_required
def users_management():
    if not current_user.is_admin:
        flash("You do not have permission to access this page.", "error")
        return redirect(url_for("index"))
        
    users = user_manager.get_all()
    return render_template("users.html", users=users, messages=flash_messages())


@app.route("/add_user", methods=["POST"])
@login_required
def add_user():
    if not current_user.is_admin:
        return redirect(url_for("index"))
        
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    
    if not username:
        flash("Username is required.", "error")
        return redirect(url_for("users_management"))
    password_error = password_policy_error(password)
    if password_error:
        flash(password_error, "error")
        return redirect(url_for("users_management"))
        
    if user_manager.get(username):
        flash(f"User '{username}' already exists.", "error")
        return redirect(url_for("users_management"))
        
    try:
        u = User(id=username, password_hash="", is_admin=(request.form.get("is_admin") == "on"))
        u.set_password(password)
        user_manager.add(u)
        flash(f"User '{username}' created successfully.", "success")
    except Exception as e:
        flash(f"An error occurred while adding the user: {e}", "error")
        
    return redirect(url_for("users_management"))


@app.route("/", methods=["GET"])
@login_required
def index():
    requested_index = request.args.get("index")
    if requested_index is not None:
        return redirect(url_for("qc", index=requested_index))

    statistics = _statistics_dashboard(str(current_user.id))
    return render_template(
        "index.html",
        statistics=statistics,
        statistics_heading="Your activity",
        lifetime_url=url_for("lifetime_statistics"),
        statistics_live=True,
        messages=flash_messages(),
    )


def _statistics_dashboard(user_id: str) -> Dict[str, Any]:
    statistics = stats_store.dashboard(user_id)
    statistics["chart_max"] = max(
        1,
        *(
            int(day[metric])
            for day in statistics["week"]
            for metric in ("slides_completed", "accessions_logged", "hours")
        ),
    )
    return statistics


def _admin_statistics_user() -> User:
    user_id = request.args.get("user_id", "")
    user = user_manager.get(user_id) if user_id else None
    if user is None:
        abort(404)
    return user


@app.route("/admin/statistics")
@admin_required
def admin_user_statistics():
    user = _admin_statistics_user()
    return render_template(
        "index.html",
        statistics=_statistics_dashboard(str(user.id)),
        statistics_heading=f"{user.id} activity",
        lifetime_url=url_for(
            "admin_user_lifetime_statistics", user_id=user.id
        ),
        statistics_live=False,
        messages=flash_messages(),
    )


@app.route("/statistics/heartbeat", methods=["POST"])
@login_required
def statistics_heartbeat():
    credited = stats_store.heartbeat(str(current_user.id))
    return jsonify(
        {
            "credited": credited,
            "statistics": stats_store.dashboard(str(current_user.id)),
        }
    )


@app.route("/statistics/lifetime")
@login_required
def lifetime_statistics():
    rows = stats_store.read_csv(str(current_user.id))
    return render_template(
        "lifetime_statistics.html",
        rows=rows,
        statistics_user=current_user,
        download_url=url_for("download_lifetime_statistics"),
        messages=flash_messages(),
    )


@app.route("/statistics/lifetime.csv")
@login_required
def download_lifetime_statistics():
    path = stats_store.csv_path(str(current_user.id))
    if not path.is_file():
        stats_store.rollup_user(str(current_user.id))
    return send_file(
        path,
        mimetype="text/csv",
        as_attachment=True,
        download_name="lifetime_stats.csv",
    )


@app.route("/admin/statistics/lifetime")
@admin_required
def admin_user_lifetime_statistics():
    user = _admin_statistics_user()
    rows = stats_store.read_csv(str(user.id))
    return render_template(
        "lifetime_statistics.html",
        rows=rows,
        statistics_user=user,
        download_url=url_for(
            "admin_download_lifetime_statistics", user_id=user.id
        ),
        messages=flash_messages(),
    )


@app.route("/admin/statistics/lifetime.csv")
@admin_required
def admin_download_lifetime_statistics():
    user = _admin_statistics_user()
    path = stats_store.csv_path(str(user.id))
    if not path.is_file():
        stats_store.rollup_user(str(user.id))
    return send_file(
        path,
        mimetype="text/csv",
        as_attachment=True,
        download_name="lifetime_stats.csv",
    )


@app.route("/tq", methods=["GET"])
@login_required
def tq_page():
    all_slides, discovery_warnings = _tq_catalog()
    selection_type = request.args.get("select", "Batch")
    if selection_type not in {"Batch", "Accession", "Slide", "Type"}:
        selection_type = "Batch"
    filter_field = request.args.get("filter", "None")
    filter_value = request.args.get("filter_value", "").strip()
    start_date = request.args.get("start_date", "").strip()
    end_date = request.args.get("end_date", "").strip()
    sort_order = request.args.get("sort", "none")
    if sort_order not in {"none", "az", "za", "date", "date_reverse"}:
        sort_order = "none"
    filter_error = _tq_validate_filter(
        filter_field, filter_value, start_date, end_date
    )
    filtered_slides = (
        []
        if filter_error
        else _tq_filtered_slides(
            all_slides,
            filter_field,
            filter_value,
            start_date,
            end_date,
            sort_order,
        )
    )
    rows = _tq_sort_grouped_rows(
        _tq_grouped_rows(filtered_slides, selection_type), sort_order
    )
    owner_id = str(current_user.id)
    with _tq_state_lock:
        draft = _tq_drafts.setdefault(
            owner_id,
            {
                "selected_ids": set(),
                "destination_dir": "",
                "include_copath": False,
                "phase": "select",
            },
        )
        draft_snapshot = {
            "selected_ids": set(draft["selected_ids"]),
            "destination_dir": str(draft.get("destination_dir", "")),
            "include_copath": bool(draft.get("include_copath", False)),
            "phase": draft["phase"],
        }
    catalog_by_id = {slide["id"]: slide for slide in all_slides}
    selected_slides = [
        catalog_by_id[slide_id]
        for slide_id in draft_snapshot["selected_ids"]
        if slide_id in catalog_by_id
    ]
    requested_view = request.args.get("view")
    review = requested_view == "review" or (
        requested_view is None and draft_snapshot["phase"] == "review"
    )
    job = _tq_job_for_user(session.get("tq_job_id"))
    return render_template(
        "tq.html",
        rows=rows,
        all_slides=all_slides,
        filtered_count=len(filtered_slides),
        selection_type=selection_type,
        filter_fields=TQ_FILTER_FIELDS,
        filter_field=filter_field,
        filter_value=filter_value,
        start_date=start_date,
        end_date=end_date,
        sort_order=sort_order,
        filter_error=filter_error,
        selected_ids=draft_snapshot["selected_ids"],
        selected_slides=selected_slides,
        destination_dir=draft_snapshot["destination_dir"],
        include_copath=draft_snapshot["include_copath"],
        review=review,
        job=job,
        discovery_warnings=discovery_warnings,
        messages=flash_messages(),
        organ_options=renaming.ORGANS,
    )


@app.route("/tq/review", methods=["POST"])
@login_required
def tq_review():
    requested_ids = set(request.form.getlist("slide_id"))
    all_slides, _ = _tq_catalog()
    valid_ids = {slide["id"] for slide in all_slides}
    selected_ids = requested_ids.intersection(valid_ids)
    if not selected_ids:
        flash("Select at least one slide to review for transfer.", "warning")
        return redirect(url_for("tq_page"))
    with _tq_state_lock:
        draft = _tq_drafts.setdefault(
            str(current_user.id),
            {
                "selected_ids": set(),
                "destination_dir": "",
                "include_copath": False,
                "phase": "select",
            },
        )
        draft["selected_ids"] = selected_ids
        draft["phase"] = "review"
    return redirect(url_for("tq_page", view="review"))


@app.route("/tq/draft", methods=["POST"])
@login_required
def tq_save_draft():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"success": False, "message": "Invalid draft request."}), 400
    selected_ids = payload.get("selected_ids")
    destination_dir = payload.get("destination_dir")
    include_copath = payload.get("include_copath")
    phase = payload.get("phase")
    with _tq_state_lock:
        draft = _tq_drafts.setdefault(
            str(current_user.id),
            {
                "selected_ids": set(),
                "destination_dir": "",
                "include_copath": False,
                "phase": "select",
            },
        )
        if selected_ids is not None:
            if not isinstance(selected_ids, list) or any(
                not isinstance(value, str)
                or not re.fullmatch(r"[a-f0-9]{24}", value)
                for value in selected_ids
            ):
                return jsonify(
                    {"success": False, "message": "Invalid slide selection."}
                ), 400
            draft["selected_ids"] = set(selected_ids)
        if destination_dir is not None:
            if not isinstance(destination_dir, str):
                return jsonify(
                    {"success": False, "message": "Invalid destination draft."}
                ), 400
            draft["destination_dir"] = destination_dir[:500]
        if include_copath is not None:
            if not isinstance(include_copath, bool):
                return jsonify(
                    {"success": False, "message": "Invalid CoPath draft option."}
                ), 400
            draft["include_copath"] = include_copath
        if phase in {"select", "review"}:
            draft["phase"] = phase
    return jsonify({"success": True})


@app.route("/tq/reset", methods=["POST"])
@login_required
def tq_reset():
    with _tq_state_lock:
        _tq_drafts.pop(str(current_user.id), None)
    flash("The transfer draft was cleared.", "success")
    return redirect(url_for("tq_page"))


@app.route("/tq/transfer", methods=["POST"])
@login_required
def tq_transfer():
    owner_id = str(current_user.id)
    with _tq_state_lock:
        draft = _tq_drafts.get(owner_id)
        selected_ids = set(draft["selected_ids"]) if draft else set()
    all_slides, _ = _tq_catalog()
    by_id = {slide["id"]: slide for slide in all_slides}
    selected = [
        dict(by_id[slide_id])
        for slide_id in selected_ids
        if slide_id in by_id
    ]
    if not selected:
        flash("The transfer draft no longer contains any available slides.", "error")
        return redirect(url_for("tq_page"))
    destination_value = request.form.get("destination_dir", "")
    include_copath = request.form.get("include_copath") == "1"
    copath_path = None
    try:
        staging_dir = _tq_safe_prefix(destination_value)
        for slide in selected:
            slide["staging_dir"] = staging_dir
            slide["destination_dir"] = _tq_destination_dir(staging_dir, slide)
        missing_copath_accessions: List[str] = []
        if include_copath:
            copath_path, missing_copath_accessions = _tq_write_copath_csv(selected)
        job = _start_tq_job(
            owner_id,
            selected,
            all_slides,
            copath_path=copath_path,
            missing_copath_accessions=missing_copath_accessions,
        )
    except TQError as exc:
        if copath_path is not None:
            copath_path.unlink(missing_ok=True)
        app.logger.warning(
            "TQ_TRANSFER_PREFLIGHT user_id=%s status=failed",
            current_user.id,
        )
        flash(str(exc), "error")
        with _tq_state_lock:
            if draft:
                draft["destination_dir"] = destination_value[:500]
                draft["include_copath"] = include_copath
                draft["phase"] = "review"
        return redirect(url_for("tq_page", view="review"))
    session["tq_job_id"] = job.id
    with _tq_state_lock:
        _tq_drafts.pop(owner_id, None)
    config_result = getattr(job, "config_result", None)
    if config_result and config_result.repaired_lines:
        lines = ", ".join(str(line) for line in config_result.repaired_lines)
        backup_name = config_result.backup_path.name
        app.logger.info(
            "TQ_CONFIG_REPAIR user_id=%s status=succeeded keys=%s lines=%s backup=%s",
            current_user.id,
            ",".join(config_result.repaired_keys),
            lines,
            backup_name,
        )
        flash(
            f"TQ configuration Windows path escaping was repaired on line(s) "
            f"{lines}. Original saved as {backup_name}.",
            "success",
        )
    if missing_copath_accessions:
        flash(
            "CoPath data was unavailable for accession(s): "
            + ", ".join(missing_copath_accessions)
            + ". The transfer will continue with available CoPath data.",
            "warning",
        )
    flash(f"Transfer started for {len(selected)} slide(s).", "success")
    return redirect(url_for("tq_page"))


@app.route("/tq/jobs/<job_id>/output", methods=["GET"])
@login_required
def tq_job_output(job_id: str):
    job = _tq_job_for_user(job_id)
    if job is None:
        return jsonify({"error": "Transfer job not found."}), 404
    try:
        offset = max(0, int(request.args.get("offset", "0")))
    except ValueError:
        offset = 0
    with _tq_state_lock:
        output = job.output[offset:]
        next_offset = len(job.output)
        status = job.status
        log_path = str(job.log_path) if job.log_path else None
    return jsonify(
        {
            "output": output,
            "next_offset": next_offset,
            "status": status,
            "return_code": job.return_code,
            "log_path": log_path,
        }
    )


@app.route("/tq/logs", methods=["GET"])
@login_required
def tq_logs():
    root = Path(Config.TQ_HOME_DIR).expanduser().resolve()
    entries: List[Dict[str, Any]] = []
    breadcrumbs = [{"name": ".tq", "path": ""}]
    error = None
    current_name = request.args.get("path", "").strip()
    selected_name = request.args.get("file", "").strip()
    content = None
    truncated = False
    try:
        if not root.is_dir():
            raise TQError(f"The TQ directory is unavailable: {root}")
        current_path = _tq_safe_path(current_name, "directory")
        accumulated = []
        for part in Path(current_name.replace("\\", "/")).parts:
            if part in {"", "."}:
                continue
            accumulated.append(part)
            breadcrumbs.append(
                {"name": part, "path": "/".join(accumulated)}
            )
        children = []
        for path in current_path.iterdir():
            if path.is_symlink():
                continue
            if not current_name and not path.is_dir():
                continue
            if path.is_dir() or path.is_file():
                children.append(path)
        children.sort(key=lambda path: (not path.is_dir(), path.name.casefold()))
        entries = [
            {
                "name": path.name,
                "path": _tq_relative_path(path),
                "is_dir": path.is_dir(),
            }
            for path in children
        ]
        if selected_name:
            selected_path = _tq_safe_path(selected_name, "file")
            with selected_path.open("r", encoding="utf-8", errors="replace") as handle:
                content = handle.read(1024 * 1024 + 1)
            if len(content) > 1024 * 1024:
                content = content[: 1024 * 1024]
                truncated = True
    except TQError as exc:
        error = str(exc)
    except OSError as exc:
        error = f"The TQ file could not be read: {exc}"
    return render_template(
        "tq_logs.html",
        root=root,
        entries=entries,
        breadcrumbs=breadcrumbs,
        current_name=current_name,
        selected_name=selected_name,
        content=content,
        truncated=truncated,
        error=error,
        messages=flash_messages(),
    )


@app.route("/tq/config", methods=["GET", "POST"])
@admin_required
def tq_edit_config():
    config_path = Path(Config.TQ_HOME_DIR).expanduser() / "config.toml"
    if request.method == "POST":
        contents = request.form.get("config_text", "")
        try:
            result = _save_tq_config(contents)
        except (TQError, OSError) as exc:
            app.logger.warning(
                "TQ_CONFIG_UPDATE user_id=%s status=failed", current_user.id
            )
            flash(str(exc), "error")
            return render_template(
                "tq_config.html",
                config_path=config_path,
                config_text=contents,
                messages=flash_messages(),
            )
        app.logger.info(
            "TQ_CONFIG_UPDATE user_id=%s status=succeeded repaired_keys=%s "
            "repaired_lines=%s",
            current_user.id,
            ",".join(result.repaired_keys),
            ",".join(str(line) for line in result.repaired_lines),
        )
        if result.repaired_lines:
            lines = ", ".join(str(line) for line in result.repaired_lines)
            backup_name = result.backup_path.name
            flash(
                f"Windows path escaping was repaired on line(s) {lines}. "
                f"Original saved as {backup_name}.",
                "success",
            )
        else:
            flash("config.toml was saved successfully.", "success")
        return redirect(url_for("tq_edit_config"))

    try:
        if config_path.is_symlink():
            raise TQError("config.toml cannot be opened through a symbolic link.")
        config_text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        config_text = ""
        flash(
            "config.toml does not exist yet. Saving will create it.",
            "warning",
        )
    except (TQError, OSError, UnicodeError) as exc:
        config_text = ""
        flash(f"config.toml could not be read: {exc}", "error")
    return render_template(
        "tq_config.html",
        config_path=config_path,
        config_text=config_text,
        messages=flash_messages(),
    )


@app.route("/renaming", methods=["GET"])
@login_required
def renaming_page():
    all_batches, discovery_warnings = discover_batches()
    _resume_longitudinal_jobs(all_batches)
    batches = [
        batch for batch in all_batches
        if batch.completed_stages["QC"] and not batch.completed_stages["Renamed"]
    ]
    history_failures = []
    for batch in all_batches:
        try:
            history = renaming.read_history_job(batch.root)
        except renaming.RenamingError as exc:
            history = {"status": "failed", "error": str(exc)}
        if history.get("status") == "failed":
            history_failures.append((batch, history))
    if request.args.get("choose") == "1":
        session.pop("renaming_batch_id", None)
    requested = request.args.get("batch") or session.get("renaming_batch_id")
    context = next((batch for batch in batches if batch.id == requested), None)
    if context is None:
        if requested:
            session.pop("renaming_batch_id", None)
        for batch in batches:
            if not (batch.root / "name_mapping.csv").exists():
                _start_renaming_job(batch)
        return render_template(
            "renaming.html",
            batches=batches,
            context=None,
            discovery_warnings=discovery_warnings,
            messages=flash_messages(),
            job_states={batch.id: _renaming_job_state(batch.id) for batch in batches},
            history_failures=history_failures,
        )

    session["renaming_batch_id"] = context.id
    mapping_path = context.root / "name_mapping.csv"
    if not mapping_path.exists():
        _start_renaming_job(context)
        return render_template(
            "renaming.html", batches=batches, context=context, groups=[], signature="",
            discovery_warnings=discovery_warnings, messages=flash_messages(),
            job_state=_renaming_job_state(context.id),
        )
    try:
        with _renaming_clone_lock:
            renaming.repair_staged_pid_assignments(
                Path(Config.COPATH_CLONE), Path(Config.INSLIDE_BATCHES)
            )
            _, rows = renaming.read_csv(mapping_path)
            reports = renaming.report_rows(context.root, Path(Config.COPATH_CLONE))
        groups = _renaming_groups_with_image_links(context, rows, reports)
        signature = renaming.mapping_signature(rows)
    except renaming.RenamingError as exc:
        flash(str(exc), "error")
        groups, signature = [], ""
    history_job = renaming.read_history_job(context.root)
    return render_template(
        "renaming.html", batches=batches, context=context, groups=groups,
        signature=signature, discovery_warnings=discovery_warnings,
        messages=flash_messages(), job_state=_renaming_job_state(context.id),
        history_job=history_job, history_failures=history_failures,
    )


@app.route("/renaming/status/<batch_id>", methods=["GET"])
@login_required
def renaming_status(batch_id: str):
    context = _renaming_context(batch_id)
    if context is None:
        return jsonify({"status": "unavailable", "error": "Batch not found."}), 404
    state = _renaming_job_state(batch_id)
    state["ready"] = (context.root / "name_mapping.csv").exists()
    state["history"] = renaming.read_history_job(context.root)
    return jsonify(state)


@app.route("/renaming/history/retry/<batch_id>", methods=["POST"])
@login_required
def renaming_history_retry(batch_id: str):
    batches, _ = discover_batches()
    context = next((batch for batch in batches if batch.id == batch_id), None)
    if context is None or not context.completed_stages["QC"]:
        flash("Batch history job is unavailable.", "warning")
    elif _start_longitudinal_job(context, force=True):
        flash("Longitudinal CoPath retry started.", "info")
    else:
        flash("Longitudinal CoPath job is already running or has no work.", "warning")
    return redirect(url_for("renaming_page", batch=batch_id))


@app.route("/renaming/prepare/<batch_id>", methods=["POST"])
@login_required
def renaming_prepare(batch_id: str):
    context = _renaming_context(batch_id)
    if context is None:
        flash("The batch is no longer available for renaming.", "warning")
        return redirect(url_for("renaming_page"))
    _start_renaming_job(context, force=True)
    flash("CoPath preparation started.", "info")
    return redirect(url_for("renaming_page", batch=batch_id))


@app.route("/renaming/retry/<batch_id>", methods=["POST"])
@login_required
def renaming_retry(batch_id: str):
    context = _renaming_context(batch_id)
    if context is None:
        flash("The batch is no longer available for renaming.", "warning")
        return redirect(url_for("renaming_page"))
    old_accession = request.form.get("old_accession", "").strip()
    new_accession = request.form.get("accession_id", "").strip()
    if not new_accession:
        flash("Accession ID is required.", "error")
    elif _start_renaming_job(
        context, old_accession=old_accession, new_accession=new_accession, force=True
    ):
        flash(f"CoPath retry started for {new_accession}.", "info")
    else:
        flash("A CoPath job is already running for this batch.", "warning")
    return redirect(url_for("renaming_page", batch=batch_id))


def _renaming_group_html(
    context: BatchContext,
    rows: List[Dict[str, str]],
    accession: str,
    signature: str,
) -> str:
    """Render one current mapping group for an in-page approval update."""
    reports = renaming.report_rows(context.root, Path(Config.COPATH_CLONE))
    group = next(
        (
            candidate
            for candidate in _renaming_groups_with_image_links(context, rows, reports)
            if candidate["accession"] == accession
        ),
        None,
    )
    if group is None:
        raise renaming.RenamingError(
            f"The updated accession group {accession} could not be found"
        )
    return render_template(
        "_renaming_group.html",
        context=context,
        group=group,
        signature=signature,
        group_key=f"updated-{uuid.uuid4().hex}",
        job_state=_renaming_job_state(context.id),
    )


def _renaming_groups_with_image_links(
    context: BatchContext,
    rows: List[Dict[str, str]],
    reports: Dict[str, Dict[str, str]],
) -> List[Dict[str, object]]:
    """Group mappings and attach safe, existing label/macro image URLs."""
    _, enriched_rows = renaming.read_csv(context.root / "enriched.csv")
    images_by_slide = {
        row.get("original_slide_path", ""): row
        for row in enriched_rows
        if row.get("original_slide_path", "")
    }
    groups = renaming.group_mapping(rows, reports)
    for group in groups:
        for slide in group["slides"]:
            source = images_by_slide.get(slide["OriginalPath"], {})
            for source_key, destination_key in (
                ("label_path", "LabelImageURL"),
                ("macro_path", "MacroImageURL"),
            ):
                image_path = context.data_manager.get_absolute_path(
                    source.get(source_key, "")
                )
                if image_path and os.path.isfile(image_path):
                    relative_path = os.path.relpath(
                        image_path, context.root
                    ).replace(os.sep, "/")
                    slide[destination_key] = url_for(
                        "serve_relative_image",
                        batch=context.id,
                        filepath=relative_path,
                    )
    return groups


@app.route("/renaming/pid/<batch_id>", methods=["GET"])
@login_required
def renaming_pid(batch_id: str):
    context = _renaming_context(batch_id)
    if context is None:
        return jsonify({
            "success": False,
            "message": "The batch is no longer available for renaming.",
        }), 404
    accession = request.args.get("accession", "").strip()
    organ = request.args.get("organ", "").strip().upper()
    reserved_pids = request.args.getlist("reserved_pid")
    expected_signature = request.args.get("mapping_signature", "")
    try:
        with _renaming_clone_lock:
            _, rows = renaming.read_csv(context.root / "name_mapping.csv")
            if renaming.mapping_signature(rows) != expected_signature:
                raise renaming.RenamingError(
                    "The mapping changed in another session; reload and try again"
                )
            pid = renaming.pid_after_organ_change(
                context.root,
                Path(Config.COPATH_CLONE),
                Path(Config.INSLIDE_BATCHES),
                accession,
                organ,
                reserved_pids,
            )
            _, rows = renaming.read_csv(context.root / "name_mapping.csv")
            signature = renaming.mapping_signature(rows)
        payload = {"success": True, "pid": pid}
        if signature != expected_signature:
            payload["mapping_signature"] = signature
        return jsonify(payload)
    except renaming.RenamingError as exc:
        status = 409 if "changed in another session" in str(exc) else 400
        return jsonify({"success": False, "message": str(exc)}), status


@app.route("/renaming/approve/<batch_id>", methods=["POST"])
@login_required
def renaming_approve(batch_id: str):
    wants_json = request.accept_mimetypes.best == "application/json"
    context = _renaming_context(batch_id)
    if context is None:
        if wants_json:
            return jsonify({
                "success": False,
                "message": "The batch is no longer available for renaming.",
            }), 404
        flash("The batch is no longer available for renaming.", "warning")
        return redirect(url_for("renaming_page"))
    mapping_path = context.root / "name_mapping.csv"
    old_accession = request.form.get("old_accession", "").strip()
    values = {
        "AccessionID": request.form.get("accession_id", "").strip(),
        "Organ": request.form.get("organ", "").strip().upper(),
        "AccessionDate": request.form.get("accession_date", "").strip().upper(),
        "Timepoint": request.form.get("timepoint", "").strip().upper(),
        "ImageType": request.form.get("image_type", "").strip().upper(),
        "SampAcqType": request.form.get("samp_acq_type", "").strip().upper(),
    }
    updated: Optional[List[Dict[str, str]]] = None
    merged = False
    try:
        if not values["AccessionID"]:
            raise renaming.RenamingError("Accession ID is required")
        if _renaming_job_state(batch_id).get("status") in {"preparing", "retrying"}:
            raise renaming.RenamingError(
                "Wait for the active CoPath job to finish before approving names"
            )
        slide_values: Dict[str, Dict[str, str]] = {}
        try:
            slide_count = int(request.form.get("slide_count", "0"))
        except ValueError as exc:
            raise renaming.RenamingError("Invalid slide submission") from exc
        for index in range(slide_count):
            path = request.form.get(f"original_path_{index}", "")
            slide_values[path] = {
                "Stain": request.form.get(f"stain_{index}", "").strip(),
                "BlockNumber": request.form.get(f"block_number_{index}", "").strip(),
                "SectionCount": request.form.get(f"section_count_{index}", "").strip(),
            }
        with _renaming_clone_lock:
            _, current_rows = renaming.read_csv(mapping_path)
            submitted_signature = request.form.get("mapping_signature", "")
            if renaming.mapping_signature(current_rows) != submitted_signature:
                raise renaming.RenamingError(
                    "The mapping changed in another session; reload and try again"
                )
            renaming.repair_staged_pid_assignments(
                Path(Config.COPATH_CLONE), Path(Config.INSLIDE_BATCHES)
            )
            _, current_rows = renaming.read_csv(mapping_path)
            repaired_signature = renaming.mapping_signature(current_rows)
            old_key = renaming.accession_key(old_accession)
            new_key = renaming.accession_key(values["AccessionID"])
            target_exists = any(
                renaming.accession_key(row["AccessionID"]) == new_key
                for row in current_rows
                if renaming.accession_key(row["AccessionID"]) != old_key
            )
            if new_key != old_key and not target_exists:
                raise renaming.RenamingError(
                    "Retry CoPath after changing an accession ID before approving it"
                )
            current = next(
                (
                    row for row in current_rows
                    if renaming.accession_key(row["AccessionID"]) == old_key
                ),
                None,
            )
            if current is None:
                raise renaming.RenamingError(
                    "The accession is no longer available in this batch"
                )
            if not target_exists:
                values["PID"] = renaming.pid_after_organ_change(
                    context.root,
                    Path(Config.COPATH_CLONE),
                    Path(Config.INSLIDE_BATCHES),
                    old_accession,
                    values["Organ"],
                )
            updated, merged = renaming.update_group(
                mapping_path, old_accession, values, slide_values,
                repaired_signature,
            )
        if (
            values["AccessionID"] != old_accession
            and renaming.same_accession(values["AccessionID"], old_accession)
        ):
            _replace_sdl_accession(old_accession, values["AccessionID"])
        if merged:
            message = "Accessions were merged. Review and approve the combined group."
            category = "info"
        else:
            message = f"Approved names for {values['AccessionID']}."
            category = "success"
        if updated and all(renaming.parse_bool(row["Approved"]) for row in updated):
            with _renaming_clone_lock:
                renaming.finalize_batch(context.root, Path(Config.COPATH_CLONE))
                _update_sdl_after_renaming(context.root)
                context.mark_renamed_complete()
            message = "All names are approved and the batch has been finalized."
            if wants_json:
                flash(message, "success")
                return jsonify({
                    "success": True,
                    "finalized": True,
                    "message": message,
                    "redirect_url": url_for("renaming_page"),
                })
            flash(message, "success")
            return redirect(url_for("renaming_page"))
        signature = renaming.mapping_signature(updated)
        if wants_json:
            return jsonify({
                "success": True,
                "finalized": False,
                "message": message,
                "category": category,
                "old_accession": old_accession,
                "accession": values["AccessionID"],
                "merged": merged,
                "signature": signature,
                "group_html": _renaming_group_html(
                    context, updated, values["AccessionID"], signature
                ),
            })
        flash(message, category)
    except (renaming.RenamingError, DataSaveError, SDLWorkbookError) as exc:
        app.logger.warning("Renaming approval failed for %s: %s", batch_id, exc)
        if wants_json:
            payload: Dict[str, Any] = {
                "success": False,
                "message": str(exc),
                "saved": updated is not None,
            }
            if updated is not None:
                signature = renaming.mapping_signature(updated)
                payload.update({
                    "old_accession": old_accession,
                    "accession": values["AccessionID"],
                    "merged": merged,
                    "signature": signature,
                    "group_html": _renaming_group_html(
                        context, updated, values["AccessionID"], signature
                    ),
                })
            if "changed in another session" in str(exc):
                status = 409
            elif isinstance(exc, renaming.RenamingError):
                status = 400
            else:
                status = 500
            return jsonify(payload), status
        flash(str(exc), "error")
    except Exception as exc:
        app.logger.exception("Renaming finalization failed for %s", batch_id)
        message = f"Renaming finalization failed: {exc}"
        if wants_json:
            payload = {
                "success": False,
                "message": message,
                "saved": updated is not None,
            }
            if updated is not None:
                signature = renaming.mapping_signature(updated)
                payload.update({
                    "old_accession": old_accession,
                    "accession": values["AccessionID"],
                    "merged": merged,
                    "signature": signature,
                    "group_html": _renaming_group_html(
                        context, updated, values["AccessionID"], signature
                    ),
                })
            return jsonify(payload), 500
        flash(message, "error")
    return redirect(url_for("renaming_page", batch=batch_id))


@app.route("/pipeline", methods=["GET"])
@login_required
def pipeline_launcher():
    job = None
    job_id = session.get("pipeline_job_id")
    if job_id:
        job = _pipeline_job_for_user(job_id)
    return render_template(
        "pipeline.html",
        form_values=_pipeline_form_values(),
        job=job,
        pipeline_busy=_pipeline_is_busy(),
        messages=flash_messages(),
    )


@app.route("/pipeline/run", methods=["POST"])
@login_required
def run_pipeline():
    values = _pipeline_form_values(request.form)
    command, errors = _pipeline_command(values)
    if errors:
        for error in errors:
            flash(error, "error")
        return (
            render_template(
                "pipeline.html",
                form_values=values,
                job=None,
                pipeline_busy=_pipeline_is_busy(),
                messages=flash_messages(),
            ),
            400,
        )

    try:
        job = _start_pipeline_job(command, str(current_user.id))
    except RuntimeError as exc:
        flash(str(exc), "warning")
        return (
            render_template(
                "pipeline.html",
                form_values=values,
                job=None,
                pipeline_busy=True,
                messages=flash_messages(),
            ),
            409,
        )
    except OSError as exc:
        app.logger.exception("Could not start the Label-Check pipeline")
        flash(f"The pipeline process could not be started: {exc}", "error")
        return (
            render_template(
                "pipeline.html",
                form_values=values,
                job=None,
                pipeline_busy=False,
                messages=flash_messages(),
            ),
            500,
        )

    session["pipeline_job_id"] = job.id
    return redirect(url_for("pipeline_launcher"))


@app.route("/pipeline/jobs/<job_id>/output", methods=["GET"])
@login_required
def pipeline_job_output(job_id: str):
    job = _pipeline_job_for_user(job_id)
    if job is None:
        return jsonify({"error": "Pipeline job not found."}), 404
    try:
        offset = max(0, int(request.args.get("offset", "0")))
    except ValueError:
        return jsonify({"error": "Output offset must be a whole number."}), 400

    with _pipeline_jobs_lock:
        offset = min(offset, len(job.output))
        output = job.output[offset:]
        next_offset = len(job.output)
        status = job.status
        return_code = job.return_code
    return jsonify(
        {
            "output": output,
            "next_offset": next_offset,
            "status": status,
            "return_code": return_code,
        }
    )


@app.route("/inventories", methods=["GET"])
@login_required
def inventories():
    inventory_directory = Path(Config.SCANNER_INVENTORIES)
    inventory_files: List[Path] = []
    directory_error = None

    try:
        if not inventory_directory.is_dir():
            directory_error = (
                f"The scanner inventory directory is unavailable: {inventory_directory}"
            )
        else:
            inventory_files = sorted(
                (
                    path
                    for path in inventory_directory.iterdir()
                    if path.is_file()
                    and not path.is_symlink()
                    and path.suffix.lower() == ".csv"
                ),
                key=lambda path: path.name.casefold(),
            )
    except OSError as exc:
        directory_error = f"The scanner inventory directory could not be read: {exc}"

    selected_name = request.args.get("file", "").strip()
    selected_path = None
    headers: List[str] = []
    rows: List[List[str]] = []
    total_rows = 0
    matching_rows = 0
    current_page = 1
    total_pages = 1
    inventory_error = None
    global_query = ""
    column_filters: Dict[int, str] = {}
    sort_column = None
    sort_direction = "asc"
    query_params: Dict[str, str] = {}
    sort_urls: List[str] = []
    previous_url = None
    next_url = None
    inventory_file_urls: Dict[str, str] = {}

    if selected_name:
        files_by_name = {path.name: path for path in inventory_files}
        selected_path = files_by_name.get(selected_name)
        if selected_path is None:
            flash("The selected scanner inventory is unavailable.", "warning")
            return redirect(url_for("inventories"))

        try:
            requested_page = max(1, int(request.args.get("page", "1")))
        except (TypeError, ValueError):
            requested_page = 1

        try:
            with selected_path.open(
                "r", encoding="utf-8-sig", newline=""
            ) as inventory_file:
                preview_reader = csv.reader(inventory_file, strict=True)
                headers = next(preview_reader)
            if not headers:
                raise InventoryReadError(
                    "This inventory does not contain a usable header row."
                )
            global_query, column_filters, sort_column, sort_direction = (
                _request_table_state(len(headers))
            )
            (
                headers,
                rows,
                total_rows,
                matching_rows,
                current_page,
                total_pages,
            ) = _read_inventory_page(
                selected_path,
                requested_page,
                global_query=global_query,
                column_filters=column_filters,
                sort_column=sort_column,
                sort_direction=sort_direction,
            )
        except StopIteration:
            inventory_error = "This inventory is empty and has no header row."
        except UnicodeDecodeError:
            inventory_error = "This inventory is not valid UTF-8 text."
        except csv.Error as exc:
            inventory_error = f"This inventory contains invalid CSV data: {exc}"
        except OSError as exc:
            inventory_error = f"This inventory could not be read: {exc}"
        except InventoryReadError as exc:
            inventory_error = str(exc)

        if not inventory_error:
            query_params = _table_query_params(
                global_query, column_filters, sort_column, sort_direction
            )
            for column in range(len(headers)):
                next_direction = (
                    "desc"
                    if sort_column == column and sort_direction == "asc"
                    else "asc"
                )
                sort_urls.append(
                    url_for(
                        "inventories",
                        **{
                            **query_params,
                            "file": selected_path.name,
                            "sort": column,
                            "direction": next_direction,
                        },
                    )
                )
            if current_page > 1:
                previous_url = url_for(
                    "inventories",
                    **{
                        **query_params,
                        "file": selected_path.name,
                        "page": current_page - 1,
                    },
                )
            if current_page < total_pages:
                next_url = url_for(
                    "inventories",
                    **{
                        **query_params,
                        "file": selected_path.name,
                        "page": current_page + 1,
                    },
                )

    inventory_file_urls = {
        path.name: url_for(
            "inventories", **{**query_params, "file": path.name}
        )
        for path in inventory_files
    }

    return render_template(
        "inventories.html",
        inventory_files=inventory_files,
        selected_name=selected_path.name if selected_path else None,
        headers=headers,
        rows=rows,
        total_rows=total_rows,
        matching_rows=matching_rows,
        current_page=current_page,
        total_pages=total_pages,
        directory_error=directory_error,
        inventory_error=inventory_error,
        global_query=global_query,
        column_filters=column_filters,
        sort_column=sort_column,
        sort_direction=sort_direction,
        query_params=query_params,
        sort_urls=sort_urls,
        previous_url=previous_url,
        next_url=next_url,
        inventory_file_urls=inventory_file_urls,
        messages=flash_messages(),
    )


@app.route("/sdl", methods=["GET", "POST"])
@login_required
def sdl():
    if request.method == "GET":
        requested_row = request.args.get("edit_row", "").strip()
        if not requested_row:
            return _render_sdl_page()
        try:
            edit_row = int(requested_row)
        except ValueError:
            flash("Invalid SDL row selected.", "error")
            return redirect(url_for("sdl"))
        return _render_sdl_page(edit_row=edit_row)

    action = (
        "delete"
        if request.form.get("delete") == "1"
        else request.form.get("action", "add")
    )
    if action == "delete":
        workbook = None
        try:
            try:
                row_number = int(request.form.get("worksheet_row", ""))
            except ValueError as exc:
                raise SDLValidationError("Invalid SDL row selected.") from exc

            with _sdl_workbook_lock:
                workbook, worksheet, _ = _load_sdl_workbook()
                header_columns = _sdl_header_columns(worksheet)
                if row_number < 2 or row_number > worksheet.max_row:
                    raise SDLValidationError("The selected SDL row no longer exists.")
                if all(
                    worksheet.cell(
                        row=row_number, column=header_columns[header]
                    ).value is None
                    for header in SDL_HEADERS
                ):
                    raise SDLValidationError("The selected SDL row no longer exists.")

                expected_signature = request.form.get("row_signature", "")
                if not expected_signature or expected_signature != _sdl_row_signature(
                    worksheet, row_number
                ):
                    raise SDLValidationError(
                        "This SDL row changed after it was opened. Reload it before deleting."
                    )

                worksheet.delete_rows(row_number, 1)
                _save_sdl_workbook(workbook)
        except (SDLWorkbookError, SDLValidationError) as exc:
            flash(str(exc), "error")
            app.logger.warning("SDL delete rejected: %s", exc)
        except Exception as exc:
            app.logger.exception("Unexpected error while deleting an SDL row")
            flash(f"The Slide Digitization Log row could not be deleted: {exc}", "error")
        else:
            flash("Slide Digitization Log row deleted successfully.", "success")
        finally:
            if workbook is not None:
                workbook.close()
        return redirect(url_for("sdl"))

    submitted_values = _submitted_sdl_form()
    try:
        normalized_values = _validate_sdl_form(submitted_values)
    except SDLValidationError as exc:
        flash(str(exc), "error")
        edit_row = None
        if action == "update":
            try:
                edit_row = int(request.form.get("worksheet_row", ""))
            except ValueError:
                pass
        return _render_sdl_page(
            form_values=submitted_values,
            edit_row=edit_row,
            edit_signature=request.form.get("row_signature", ""),
        )

    workbook = None
    try:
        with _sdl_workbook_lock:
            workbook, worksheet, initialized_headers = _load_sdl_workbook()
            header_columns = _sdl_header_columns(worksheet)
            if action == "update":
                try:
                    row_number = int(request.form.get("worksheet_row", ""))
                except ValueError as exc:
                    raise SDLValidationError("Invalid SDL row selected.") from exc
                if row_number < 2 or row_number > worksheet.max_row:
                    raise SDLValidationError("The selected SDL row no longer exists.")
                if all(
                    worksheet.cell(
                        row=row_number, column=header_columns[header]
                    ).value is None
                    for header in SDL_HEADERS
                ):
                    raise SDLValidationError("The selected SDL row no longer exists.")
                expected_signature = request.form.get("row_signature", "")
                if not expected_signature or expected_signature != _sdl_row_signature(
                    worksheet, row_number
                ):
                    raise SDLValidationError(
                        "This SDL row changed after it was opened. Reload it before saving."
                    )
            elif action == "add":
                row_number = worksheet.max_row + 1
            else:
                raise SDLValidationError("Invalid SDL action.")

            for header in SDL_HEADERS:
                column = header_columns[header]
                if header in normalized_values:
                    worksheet.cell(row=row_number, column=column).value = normalized_values[header]
                elif action == "add" and header in SDL_STATUS_HEADERS:
                    worksheet.cell(row=row_number, column=column).value = False

            for header in ("Date Loaded", "Date Unloaded"):
                worksheet.cell(
                    row=row_number, column=header_columns[header]
                ).number_format = "yyyy-mm-dd"
            for header in ("Time Loaded", "Time Unloaded"):
                worksheet.cell(
                    row=row_number, column=header_columns[header]
                ).number_format = "hh:mm"

            _save_sdl_workbook(workbook)
    except (SDLWorkbookError, SDLValidationError) as exc:
        flash(str(exc), "error")
        app.logger.warning("SDL save rejected: %s", exc)
        return _render_sdl_page(
            form_values=submitted_values,
            edit_row=(row_number if action == "update" and "row_number" in locals() else None),
            edit_signature=request.form.get("row_signature", ""),
        )
    except Exception as exc:
        app.logger.exception("Unexpected error while saving the SDL workbook")
        flash(f"The Slide Digitization Log could not be saved: {exc}", "error")
        return _render_sdl_page(
            form_values=submitted_values,
            edit_row=None,
        )
    finally:
        if workbook is not None:
            workbook.close()

    if action == "add":
        try:
            stats_store.increment(str(current_user.id), "accessions_logged")
        except Exception:
            app.logger.exception("Could not record accession activity")
            flash(
                "The row was saved, but its statistics entry could not be recorded.",
                "warning",
            )

    flash(
        "Slide Digitization Log row updated successfully."
        if action == "update"
        else "Slide Digitization Log row added successfully.",
        "success",
    )
    return redirect(url_for("sdl"))


@app.route("/qc", methods=["GET"])
@login_required
def qc():
    context, available_batches, discovery_warnings = _selected_batch()
    if context is None:
        return render_template(
            "batches.html",
            batches=available_batches,
            discovery_warnings=discovery_warnings,
            messages=flash_messages(),
        )

    data_manager = context.data_manager
    queue_manager = context.queue_manager

    _release_expired_leases(context)
    item_to_display = None
    requested_index_str = request.args.get("index")

    # 1. User requested specific index
    if requested_index_str:
        try:
            idx = int(requested_index_str)
            if 0 <= idx < len(data_manager.data):
                qi = queue_manager.claim(str(current_user.id), idx)
                if qi is None:
                    raise ValueError("queue item is missing")
                if qi.status == "leased" and qi.leased_by_id != current_user.id:
                    flash("This item is currently leased by another user. Viewing in read-only mode.", "warning")
                item_to_display = qi
        except (ValueError, TypeError):
            flash("Invalid index provided in URL.", "error")

    # 2. Check active lease
    if not item_to_display:
        item_to_display = queue_manager.claim(str(current_user.id))
        if item_to_display is None:
                # 4. Every unfinished item is currently leased by another user.
                total = len(queue_manager.items)
                done = len([i for i in queue_manager.get_all() if i.status == "completed"])
                return render_template(
                    "qc.html",
                    no_items_available=True,
                    completed_count=done,
                    total_count=total,
                    messages=flash_messages(),
                    batch_id=context.id,
                    batch_name=context.display_name,
                    discovery_warnings=discovery_warnings,
                )

    current_index = item_to_display.original_index
    row_data = data_manager.get_row(current_index)
    if not row_data:
        flash("Error: Database index mismatch with CSV. Reloading data...", "error")
        context.refresh()
        return redirect(url_for("qc", batch=context.id))

    display_row_data = row_data.copy()

    # Pre-fill logic safe lookup
    identifier = display_row_data.get("_identifier")
    if not display_row_data.get("AccessionID") and identifier:
        for r in data_manager.data:
            if r.get("_identifier") == identifier and r.get("AccessionID"):
                display_row_data["AccessionID"] = r["AccessionID"]
                flash(f"Auto-filled Accession ID '{r['AccessionID']}' from a related file.", "info")
                if not display_row_data.get("Stain") and r.get("Stain"):
                    display_row_data["Stain"] = r["Stain"]
                break

    # Image Paths
    label_image_url, macro_image_url = None, None
    label_image_exists, macro_image_exists = False, False

    def resolve_image_path(csv_path_key):
        csv_path = display_row_data.get(csv_path_key)
        if csv_path:
            full_path = data_manager.get_absolute_path(csv_path)
            if full_path and os.path.exists(full_path):
                relative_path = os.path.relpath(full_path, context.root).replace(os.sep, "/")
                return url_for("serve_relative_image", batch=context.id, filepath=relative_path), True
        return None, False

    label_image_url, label_image_exists = resolve_image_path("_label_path")
    macro_image_url, macro_image_exists = resolve_image_path("_macro_path")

    queue_stats = {
        "pending": len([i for i in queue_manager.get_all() if i.status == "pending"]),
        "leased": len([i for i in queue_manager.get_all() if i.status == "leased"]),
        "completed": len([i for i in queue_manager.get_all() if i.status == "completed"]),
    }
    
    recently_completed_items = sorted(
        [i for i in queue_manager.get_all() if i.completed_by_id == current_user.id],
        key=lambda x: x.completed_at if x.completed_at else datetime.datetime.min,
        reverse=True
    )[:5]
    
    # Enrich for template (needs accession_id)
    recently_completed = []
    for r in recently_completed_items:
        rr = data_manager.get_row(r.original_index)
        # Create a proxy object or dict for template
        r_dict = r.to_dict()
        r_dict['accession_id'] = rr.get("AccessionID", "N/A") if rr else "N/A"
        # Overwrite string date with object for strftime support in template
        r_dict['completed_at'] = r.completed_at 
        recently_completed.append(r_dict)

    return render_template(
        "qc.html",
        row=display_row_data,
        original_index=current_index,
        total_original_rows=len(data_manager.data),
        label_img_path=label_image_url,
        macro_img_path=macro_image_url,
        label_img_exists=label_image_exists,
        macro_img_exists=macro_image_exists,
        messages=flash_messages(),
        data_loaded=True,
        queue_stats=queue_stats,
        lease_info=item_to_display,
        datetime=datetime.datetime,
        timedelta=datetime.timedelta,
        recently_completed=recently_completed,
        batch_id=context.id,
        batch_name=context.display_name,
        discovery_warnings=discovery_warnings,
    )


@app.route("/update", methods=["POST"])
@login_required
def update():
    """Handles the form submission for saving corrections."""
    context, _, _ = _selected_batch(allow_completed=True)
    if context is None:
        flash("The selected batch is no longer available.", "warning")
        return redirect(url_for("qc"))
    data_manager = context.data_manager
    queue_manager = context.queue_manager
    if not data_manager.data:
        return redirect(url_for("qc"))
        
    try:
        idx = int(request.form.get("original_index", -1))
        if idx < 0:
            raise ValueError("Invalid index")

        qi = queue_manager.get(idx)

        if not qi:
            flash("Error: Item not found in queue.", "error")
            return redirect(url_for("qc"))

        # --- SAFETY CHECK: LEASE VALIDATION ---
        is_forced_save = False
        
        # Case A: Item is completed.
        if qi.status == "completed":
            flash("Cannot save changes: This item has already been completed.", "error")
            return redirect(url_for("qc"))

        # Case B: I hold the lease.
        if qi.leased_by_id == current_user.id:
            pass # Valid save

        # Case C: Leased by SOMEONE ELSE.
        elif qi.status == "leased" and qi.leased_by_id != current_user.id:
            # Check for lease expiry just in case
            _release_expired_leases(context)
            # Reload queue just to be sure
            qi = queue_manager.get(idx)
            if qi.status == "leased" and qi.leased_by_id != current_user.id:
                flash("SAVE BLOCKED: This item is currently currently leased by another user.", "error")
                return redirect(url_for("qc"))
            # If after refresh it's effectively pending, we fall through to Case D.
            is_forced_save = True

        # Case D: Item is pending (lease expired or never leased).
        elif qi.status == "pending":
            is_forced_save = True # Allowed to pick up

        # --- Update Data ---
        new_values = _normalize_qc_values({
            "AccessionID": request.form.get("accession_id", ""),
            "Stain": request.form.get("stain", ""),
            "BlockNumber": request.form.get("block_number", ""),
            "_is_complete": request.form.get("complete") == "on"
        })
        
        # Validation for completion
        if new_values["_is_complete"]:
            validation_errors = _qc_row_validation_errors(new_values)
            if validation_errors:
                flash(
                    "Cannot mark as complete: " + "; ".join(validation_errors) + ".",
                    "warning",
                )
                new_values["_is_complete"] = False

        # Apply updates
        has_changed = data_manager.update_row(idx, new_values)

        if has_changed:
            completed_now = (
                request.form.get("action") == "next" and new_values["_is_complete"]
            )

            if completed_now:
                qi.status = "completed"
                qi.completed_by_id = current_user.id
                qi.completed_at = datetime.datetime.utcnow()
            elif is_forced_save:
                qi.status = "leased"
                qi.leased_by_id = current_user.id
                qi.leased_at = datetime.datetime.utcnow()
            
            queue_manager.save()

            try:
                _create_backup(context)
                data_manager.save_data(context.csv_path)
                context.csv_mod_time = context.csv_path.stat().st_mtime

                if completed_now:
                    try:
                        stats_store.increment(
                            str(current_user.id), "slides_completed"
                        )
                    except Exception:
                        app.logger.exception("Could not record slide completion activity")
                        flash(
                            "The slide was completed, but its statistics entry could not be recorded.",
                            "warning",
                        )

                # --- CHECK IF LIST IS DONE ---
                remaining = len([i for i in queue_manager.get_all() if i.status != "completed"])

                if remaining == 0:
                    invalid_indices = _requeue_invalid_qc_rows(context)
                    if invalid_indices:
                        data_manager.save_data(context.csv_path)
                        context.csv_mod_time = context.csv_path.stat().st_mtime
                        app.logger.warning(
                            "Final QC validation returned %d row(s) to the queue for batch %s.",
                            len(invalid_indices),
                            context.id,
                        )
                        flash(
                            f"Final validation returned {len(invalid_indices)} slide(s) "
                            "to the QC queue because required values were missing or invalid.",
                            "warning",
                        )
                    else:
                        app.logger.info("All items passed final validation. Creating final backup.")
                        _create_backup(context, suffix="FINAL_COMPLETED")
                        try:
                            context.mark_qc_complete()
                        except DataSaveError as exc:
                            app.logger.error("QC status update failed: %s", exc)
                            flash(
                                "Slide changes were saved, but the batch could not be marked as QC complete.",
                                "error",
                            )
                            return redirect(url_for("qc", batch=context.id))
                        _start_renaming_job(context)
                        flash("🎉 ALL ITEMS COMPLETED! A final comprehensive backup has been created.", "success")
                else:
                    flash("Changes saved successfully.", "success")

            except Exception as e:
                app.logger.error(f"Save operation failed: {e}")
                flash("CRITICAL: Error saving changes to the CSV file.", "error")

        return redirect(url_for("qc", batch=context.id))

    except Exception as e:
        app.logger.error(f"Update failed: {e}")
        flash("An error occurred during the update.", "error")
        return redirect(url_for("qc"))


@app.route("/history")
@login_required
def history():
    context, _, _ = _selected_batch(allow_completed=True)
    if context is None:
        flash("Choose a batch to view its history.", "warning")
        return redirect(url_for("qc"))
    data_manager = context.data_manager
    queue_manager = context.queue_manager
    history_items = sorted(
        [i for i in queue_manager.get_all() if i.completed_by_id == current_user.id],
        key=lambda x: x.completed_at if x.completed_at else datetime.datetime.min,
        reverse=True
    )
    
    # Enhance for template
    display_history = []
    for item in history_items:
        d = item.to_dict()
        row = data_manager.get_row(item.original_index)
        d['accession_id'] = row.get("AccessionID", "N/A") if row else "N/A"
        d['completed_at'] = item.completed_at
        display_history.append(d)

    return render_template(
        "history.html", completed_items=display_history, messages=flash_messages(),
        batch_id=context.id, batch_name=context.display_name,
    )


@app.route("/release", methods=["POST"])
@login_required
def release_lease():
    context, _, _ = _selected_batch(allow_completed=True)
    if context is None:
        flash("The selected batch is no longer available.", "warning")
        return redirect(url_for("qc"))
    queue_manager = context.queue_manager
    released = queue_manager.release_user(str(current_user.id))
    if released:
        flash(f"Successfully released {released} item(s) back to the queue.", "info")
        
    return redirect(url_for("qc", batch=context.id))


@app.route("/search", methods=["POST"])
@login_required
def search():
    context, _, _ = _selected_batch()
    if context is None:
        return redirect(url_for("qc"))
    data_manager = context.data_manager
    if not data_manager.data:
        return redirect(url_for("qc"))
        
    search_term = request.form.get("search_term", "").strip().lower()
    if not search_term:
        return redirect(url_for("qc"))

    for i, row in enumerate(data_manager.data):
        if (
            search_term in row.get("AccessionID", "").lower() or
            search_term in row.get("_identifier", "").lower() or
            search_term == row.get("BlockNumber", "").lower()
        ):
            return redirect(url_for("qc", batch=context.id, index=i))

    flash(f"No item found matching '{search_term}'.", "warning")
    return redirect(url_for("qc", batch=context.id))


@app.route("/data_images/<batch>/<path:filepath>")
@login_required
def serve_relative_image(batch: str, filepath: str):
    batches, _ = discover_batches()
    context = next((item for item in batches if item.id == batch), None)
    if context is None:
        return "Batch not found.", 404
    context.refresh()
    abs_file_path = context.data_manager.get_absolute_path(filepath)
    if not abs_file_path:
        app.logger.warning("Blocked invalid image path for batch %s: %s", batch, filepath)
        return "Access denied: Invalid file path.", 403
    if not os.path.isfile(abs_file_path):
        return "Image not found on server.", 404

    directory, filename = os.path.split(abs_file_path)
    return send_from_directory(directory, filename)


# ==============================================================================
# 11. VERSIONED PIPELINE API
# ==============================================================================
API_PIPELINE_FIELDS = {
    "input_dir", "output_dir", "start_from", "end_at", "input_mode",
    "macro_workers", "macro_extensions", "macro_image_extensions",
    "thumbnail_width", "thumbnail_height", "ocr_workers", "ocr_use_cpu",
    "naming_accession_pattern", "naming_workers",
}


def _api_pipeline_values(payload: Any) -> Tuple[Optional[Dict[str, str]], List[str]]:
    if not isinstance(payload, dict):
        return None, ["The request body must be a JSON object."]
    errors = []
    unknown = sorted(set(payload) - API_PIPELINE_FIELDS)
    if unknown:
        errors.append(f"Unknown fields: {', '.join(unknown)}.")
    for required in ("input_dir", "output_dir"):
        if required not in payload:
            errors.append(f"{required} is required.")

    integer_fields = {
        "start_from", "end_at", "macro_workers", "thumbnail_width",
        "thumbnail_height", "ocr_workers", "naming_workers",
    }
    string_fields = {"input_dir", "output_dir", "input_mode", "naming_accession_pattern"}
    extension_fields = {"macro_extensions", "macro_image_extensions"}
    for field in integer_fields & payload.keys():
        if isinstance(payload[field], bool) or not isinstance(payload[field], int):
            errors.append(f"{field} must be an integer.")
    for field in string_fields & payload.keys():
        if not isinstance(payload[field], str):
            errors.append(f"{field} must be a string.")
    for field in extension_fields & payload.keys():
        value = payload[field]
        if not isinstance(value, list) or not value or any(not isinstance(item, str) for item in value):
            errors.append(f"{field} must be a non-empty array of strings.")
    if "ocr_use_cpu" in payload and not isinstance(payload["ocr_use_cpu"], bool):
        errors.append("ocr_use_cpu must be a boolean.")
    if errors:
        return None, errors

    values = dict(PIPELINE_FORM_DEFAULTS)
    for key, value in payload.items():
        if key in integer_fields:
            values[key] = str(value)
        elif key in extension_fields:
            values[key] = ", ".join(value)
        elif key == "ocr_use_cpu":
            values[key] = "on" if value else ""
        else:
            values[key] = value
    return values, []


@app.route("/api/v1/pipeline/jobs", methods=["POST"])
@_require_api_scope("pipeline:run", "submit")
def api_create_pipeline_job():
    if not request.is_json:
        return _api_problem(415, "unsupported_media_type", "JSON required", "Use Content-Type: application/json.")
    idempotency_key = request.headers.get("Idempotency-Key", "")
    if not re.fullmatch(r"[\x21-\x7E]{1,128}", idempotency_key):
        return _api_problem(422, "invalid_idempotency_key", "Invalid idempotency key", "Idempotency-Key must contain 1–128 printable non-space ASCII characters.")
    payload = request.get_json(silent=True)
    values, shape_errors = _api_pipeline_values(payload)
    if shape_errors:
        return _api_problem(422, "validation_error", "Invalid pipeline request", " ".join(shape_errors))
    assert values is not None
    payload_hash = hashlib.sha256(
        json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    existing = api_store.find_idempotent(g.api_token["token_id"], idempotency_key)
    if existing is not None:
        if existing["payload_hash"] != payload_hash:
            return _api_problem(409, "idempotency_conflict", "Idempotency conflict", "This key was already used with a different request.")
        response = jsonify({"data": _api_job_document(dict(existing))})
        response.status_code = 202
        response.headers["Location"] = url_for("api_pipeline_job", job_id=existing["job_id"], _external=True)
        response.headers["Idempotency-Replayed"] = "true"
        return response

    command, validation_errors = _pipeline_command(values)
    if validation_errors:
        return _api_problem(422, "validation_error", "Invalid pipeline request", " ".join(validation_errors))
    assert command is not None
    job_id = str(uuid.uuid4())
    try:
        job = _start_pipeline_job(
            command,
            str(g.api_user.id),
            job_id=job_id,
            request_values=values,
            token_id=g.api_token["token_id"],
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
        )
    except RuntimeError as exc:
        raced_job = api_store.find_idempotent(g.api_token["token_id"], idempotency_key)
        if raced_job is not None and raced_job["payload_hash"] == payload_hash:
            response = jsonify({"data": _api_job_document(dict(raced_job))})
            response.status_code = 202
            response.headers["Location"] = url_for(
                "api_pipeline_job", job_id=raced_job["job_id"], _external=True
            )
            response.headers["Idempotency-Replayed"] = "true"
            return response
        return _api_problem(409, "pipeline_busy", "Pipeline busy", str(exc))
    except OSError:
        app.logger.exception("API could not start the Label-Check pipeline")
        return _api_problem(500, "pipeline_launch_failed", "Pipeline launch failed", "The pipeline process could not be started.")
    record = api_store.get_job(job.id)
    response = jsonify({"data": _api_job_document(record)})
    response.status_code = 202
    response.headers["Location"] = url_for("api_pipeline_job", job_id=job.id, _external=True)
    return response


def _authorized_api_job(job_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[Any]]:
    record = api_store.get_job(job_id)
    if record is None or (
        record["owner_id"] != str(g.api_user.id) and not g.api_user.is_admin
    ):
        return None, _api_problem(404, "job_not_found", "Job not found", "The requested pipeline job was not found.")
    return record, None


@app.route("/api/v1/pipeline/jobs/<job_id>", methods=["GET"])
@_require_api_scope("pipeline:read")
def api_pipeline_job(job_id: str):
    record, error = _authorized_api_job(job_id)
    if error is not None:
        return error
    return jsonify({"data": _api_job_document(record)})


@app.route("/api/v1/pipeline/jobs/<job_id>/output", methods=["GET"])
@_require_api_scope("pipeline:read")
def api_pipeline_job_output(job_id: str):
    record, error = _authorized_api_job(job_id)
    if error is not None:
        return error
    try:
        offset = int(request.args.get("offset", "0"))
        limit = int(request.args.get("limit", str(app.config["API_OUTPUT_DEFAULT_LIMIT"])))
    except ValueError:
        return _api_problem(422, "invalid_pagination", "Invalid pagination", "offset and limit must be integers.")
    if offset < 0 or limit < 1 or limit > app.config["API_OUTPUT_MAX_LIMIT"]:
        return _api_problem(422, "invalid_pagination", "Invalid pagination", f"offset must be non-negative and limit must be 1–{app.config['API_OUTPUT_MAX_LIMIT']}.")
    output_path = record["output_path"]
    try:
        size = os.path.getsize(output_path)
        offset = min(offset, size)
        with open(output_path, "rb") as output_file:
            output_file.seek(offset)
            raw = output_file.read(limit)
    except OSError:
        return _api_problem(500, "output_unavailable", "Output unavailable", "The pipeline output could not be read.")
    while raw:
        try:
            output = raw.decode("utf-8")
            break
        except UnicodeDecodeError as exc:
            if exc.reason == "unexpected end of data":
                raw = raw[:exc.start]
            else:
                output = raw.decode("utf-8", errors="replace")
                break
    else:
        output = ""
    next_offset = offset + len(raw)
    return jsonify(
        {
            "data": {
                "job_id": job_id,
                "output": output,
                "offset": offset,
                "next_offset": next_offset,
                "eof": next_offset >= size,
                "status": record["status"],
            }
        }
    )


@app.route("/api/v1/openapi.json", methods=["GET"])
@_require_api_scope("pipeline:read")
def api_openapi_document():
    contract_path = Path(__file__).resolve().with_name("openapi.json")
    try:
        with contract_path.open("r", encoding="utf-8") as contract_file:
            contract = json.load(contract_file)
        properties = contract["components"]["schemas"]["PipelineRequest"]["properties"]
        for field in ("macro_workers", "ocr_workers", "naming_workers"):
            properties[field]["maximum"] = app.config["PIPELINE_MAX_WORKERS"]
        for field in ("thumbnail_width", "thumbnail_height"):
            properties[field]["maximum"] = app.config[
                "PIPELINE_MAX_THUMBNAIL_DIMENSION"
            ]
        return jsonify(contract)
    except (OSError, json.JSONDecodeError):
        app.logger.exception("OpenAPI contract is unavailable")
        return _api_problem(500, "contract_unavailable", "Contract unavailable", "The OpenAPI contract could not be loaded.")


# ==============================================================================
# 12. CLI COMMANDS
# ==============================================================================
@app.cli.command("validate-security")
def validate_security_command():
    """Validate required production credentials without printing their values."""
    try:
        validate_security_config()
    except SecurityConfigurationError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo("Security configuration is valid.")


@app.cli.group("api-token")
def api_token_cli():
    """Manage scoped personal access tokens for the pipeline API."""


@api_token_cli.command("create")
@click.argument("user_id")
@click.option("--label", required=True, help="Human-readable credential label.")
@click.option(
    "--scope",
    "scopes",
    multiple=True,
    type=click.Choice(["pipeline:read", "pipeline:run"]),
    default=("pipeline:read", "pipeline:run"),
    show_default=True,
)
@click.option("--expires-days", type=click.IntRange(min=1), default=90, show_default=True)
def create_api_token(user_id: str, label: str, scopes: Tuple[str, ...], expires_days: int):
    if user_manager.get(user_id) is None:
        raise click.ClickException(f"Unknown user: {user_id}")
    raw_token, record = api_store.create_token(user_id, label, list(scopes), expires_days)
    click.echo(f"Token ID: {record['token_id']}")
    click.echo(f"Expires: {record['expires_at']}")
    click.echo("Token (shown once):")
    click.echo(raw_token)


@api_token_cli.command("list")
@click.option("--user", "user_id")
def list_api_tokens(user_id: Optional[str]):
    records = api_store.list_tokens(user_id)
    if not records:
        click.echo("No API tokens found.")
        return
    for record in records:
        state = "revoked" if record["revoked_at"] else "active"
        click.echo(
            f"{record['token_id']}\t{record['user_id']}\t{record['label']}\t"
            f"{','.join(record['scopes'])}\t{record['expires_at']}\t{state}"
        )


@api_token_cli.command("revoke")
@click.argument("token_id")
def revoke_api_token(token_id: str):
    if not api_store.revoke_token(token_id):
        raise click.ClickException("Active token not found.")
    click.echo(f"Revoked token {token_id}.")


@api_token_cli.command("rotate")
@click.argument("token_id")
@click.option("--expires-days", type=click.IntRange(min=1), default=90, show_default=True)
def rotate_api_token(token_id: str, expires_days: int):
    record = next((item for item in api_store.list_tokens() if item["token_id"] == token_id), None)
    if record is None or record["revoked_at"]:
        raise click.ClickException("Active token not found.")
    raw_token, replacement = api_store.create_token(
        record["user_id"], record["label"], record["scopes"], expires_days
    )
    api_store.revoke_token(token_id)
    click.echo(f"Revoked token {token_id}; replacement ID: {replacement['token_id']}")
    click.echo("Token (shown once):")
    click.echo(raw_token)


@app.cli.command("init-db")
@with_appcontext
def init_db_command():
    try:
        validate_security_config()
    except SecurityConfigurationError as exc:
        raise click.ClickException(str(exc)) from exc
    print("--- Initializing App Persistence ---")
    
    # Init Users
    if not user_manager.get("admin"):
        u = User(id="admin", password_hash="", is_admin=True)
        u.set_password(app.config["ADMIN_DEFAULT_PASSWORD"])
        user_manager.add(u)
        print(f"Created default 'admin' user in {Config.USERS_CSV_PATH}")
    else:
        print("'admin' user already exists.")

    batches, warnings = discover_batches()
    print(f"Discovered and initialized {len(batches)} valid batch(es).")
    for warning in warnings:
        print(f"WARNING: {warning}")

    print("--- Initialization complete. ---")


if __name__ == "__main__":
    try:
        validate_security_config()
    except SecurityConfigurationError as exc:
        print(f"Invalid security configuration: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    app.run(debug=True, host="0.0.0.0")
