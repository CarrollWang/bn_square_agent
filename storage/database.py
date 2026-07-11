from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Iterator

from ..core.secret_store import SecretStore
from ..models.schemas import ContentReview, PostAnalysis, StyleProfile


ACCOUNT_SECRET_COLUMNS = frozenset(
    {
        "square_openapi_key",
        "proxy_url",
        "mcp_auth_token",
        "signature_key",
    }
)
SECRET_APP_SETTING_KEYS = frozenset(
    {
        "LLM_API_KEY",
        "DASHSCOPE_API_KEY",
        "EMBEDDING_API_KEY",
        "MCP_AUTH_TOKEN",
        "SMTP_PASSWORD",
    }
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path | str, *, secret_store: SecretStore):
        self.path = Path(path)
        self.secret_store = secret_store
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def init_schema(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS source_posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    author TEXT,
                    title TEXT,
                    content TEXT NOT NULL,
                    url TEXT,
                    source_created_at TEXT,
                    role TEXT NOT NULL CHECK(role IN ('reference', 'material')),
                    hash TEXT NOT NULL UNIQUE,
                    analysis_status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS post_analysis (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    post_id INTEGER NOT NULL UNIQUE,
                    analysis_json TEXT,
                    status TEXT NOT NULL,
                    error TEXT,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(post_id) REFERENCES source_posts(id)
                );

                CREATE TABLE IF NOT EXISTS author_profiles (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    profile_json TEXT NOT NULL,
                    source_count INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS generated_posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_post_id INTEGER NOT NULL,
                    candidate_index INTEGER NOT NULL,
                    original_content TEXT NOT NULL,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(
                        status IN ('pending', 'approved', 'rejected', 'failed')
                    ),
                    review_json TEXT,
                    rewrite_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source_post_id, candidate_index),
                    FOREIGN KEY(source_post_id) REFERENCES source_posts(id)
                );

                CREATE TABLE IF NOT EXISTS job_locks (
                    job_name TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            self._migrate_schema(connection)

    @staticmethod
    def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
        return {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }

    def _migrate_schema(self, connection: sqlite3.Connection) -> None:
        source_columns = self._columns(connection, "source_posts")
        if "account_key" not in source_columns:
            connection.execute(
                "ALTER TABLE source_posts ADD COLUMN account_key TEXT NOT NULL DEFAULT 'default'"
            )

        generated_columns = self._columns(connection, "generated_posts")
        if "account_key" not in generated_columns:
            connection.execute(
                "ALTER TABLE generated_posts ADD COLUMN account_key TEXT NOT NULL DEFAULT 'default'"
            )
        if "publish_status" not in generated_columns:
            connection.execute(
                "ALTER TABLE generated_posts ADD COLUMN publish_status TEXT NOT NULL DEFAULT 'not_published'"
            )
        if "publish_json" not in generated_columns:
            connection.execute("ALTER TABLE generated_posts ADD COLUMN publish_json TEXT")
        if "published_at" not in generated_columns:
            connection.execute("ALTER TABLE generated_posts ADD COLUMN published_at TEXT")

        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                account_key TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                square_openapi_key TEXT NOT NULL DEFAULT '',
                proxy_url TEXT NOT NULL DEFAULT '',
                mcp_url TEXT NOT NULL DEFAULT '',
                mcp_auth_token TEXT NOT NULL DEFAULT '',
                signature_key TEXT,
                check_status TEXT NOT NULL DEFAULT 'unchecked',
                checked_at TEXT,
                check_error TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS style_profiles (
                account_key TEXT PRIMARY KEY,
                profile_json TEXT NOT NULL,
                source_count INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS material_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                source_type TEXT NOT NULL CHECK(
                    source_type IN ('binance_square', 'techflow_newsletter')
                ),
                url TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_checked_at TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(source_type, url)
            );

            CREATE TABLE IF NOT EXISTS material_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER,
                external_id TEXT,
                author TEXT,
                title TEXT,
                content TEXT NOT NULL,
                url TEXT,
                source_created_at TEXT,
                hash TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'new' CHECK(
                    status IN ('new', 'used', 'ignored', 'failed')
                ),
                tag_status TEXT NOT NULL DEFAULT 'pending',
                tag_json TEXT,
                tag_error TEXT,
                tagged_at TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(source_id) REFERENCES material_sources(id)
            );

            CREATE TABLE IF NOT EXISTS material_account_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                material_item_id INTEGER NOT NULL,
                account_key TEXT NOT NULL,
                status TEXT NOT NULL CHECK(
                    status IN ('published', 'failed', 'skipped')
                ),
                generated_id INTEGER,
                publish_json TEXT,
                error TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_attempted_at TEXT,
                published_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(material_item_id, account_key),
                FOREIGN KEY(material_item_id) REFERENCES material_items(id),
                FOREIGN KEY(account_key) REFERENCES accounts(account_key),
                FOREIGN KEY(generated_id) REFERENCES generated_posts(id)
            );

            CREATE INDEX IF NOT EXISTS idx_material_items_status_tag_created
                ON material_items(status, tag_status, created_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_generated_posts_account_status_created
                ON generated_posts(account_key, status, created_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_generated_posts_publish_status
                ON generated_posts(publish_status, published_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_material_account_runs_material_status
                ON material_account_runs(material_item_id, status, account_key);
            """
        )
        account_columns = self._columns(connection, "accounts")
        if "square_openapi_key" not in account_columns:
            connection.execute(
                "ALTER TABLE accounts ADD COLUMN square_openapi_key TEXT NOT NULL DEFAULT ''"
            )
        if "proxy_url" not in account_columns:
            connection.execute(
                "ALTER TABLE accounts ADD COLUMN proxy_url TEXT NOT NULL DEFAULT ''"
            )
        if "mcp_url" not in account_columns:
            connection.execute(
                "ALTER TABLE accounts ADD COLUMN mcp_url TEXT NOT NULL DEFAULT ''"
            )
        if "mcp_auth_token" not in account_columns:
            connection.execute(
                "ALTER TABLE accounts ADD COLUMN mcp_auth_token TEXT NOT NULL DEFAULT ''"
            )
        if "signature_key" not in account_columns:
            connection.execute("ALTER TABLE accounts ADD COLUMN signature_key TEXT")
        if "check_status" not in account_columns:
            connection.execute(
                "ALTER TABLE accounts ADD COLUMN check_status TEXT NOT NULL DEFAULT 'unchecked'"
            )
        if "checked_at" not in account_columns:
            connection.execute("ALTER TABLE accounts ADD COLUMN checked_at TEXT")
        if "check_error" not in account_columns:
            connection.execute("ALTER TABLE accounts ADD COLUMN check_error TEXT")
        material_columns = self._columns(connection, "material_items")
        self._ensure_material_source_types(connection)
        self._ensure_material_items_source_fk(connection)
        if "tag_status" not in material_columns:
            connection.execute(
                "ALTER TABLE material_items ADD COLUMN tag_status TEXT NOT NULL DEFAULT 'pending'"
            )
        if "tag_json" not in material_columns:
            connection.execute("ALTER TABLE material_items ADD COLUMN tag_json TEXT")
        if "tag_error" not in material_columns:
            connection.execute("ALTER TABLE material_items ADD COLUMN tag_error TEXT")
        if "tagged_at" not in material_columns:
            connection.execute("ALTER TABLE material_items ADD COLUMN tagged_at TEXT")
        self._migrate_secret_storage(connection)

    def _migrate_secret_storage(self, connection: sqlite3.Connection) -> None:
        for column in ACCOUNT_SECRET_COLUMNS:
            rows = connection.execute(
                f"SELECT account_key, {column} FROM accounts"
            ).fetchall()
            for row in rows:
                current = row[column]
                if current is None:
                    continue
                value = str(current)
                if not value or self.secret_store.is_encrypted(value):
                    continue
                connection.execute(
                    f"UPDATE accounts SET {column} = ? WHERE account_key = ?",
                    (self.secret_store.encrypt(value), row["account_key"]),
                )

        rows = connection.execute("SELECT key, value FROM app_settings").fetchall()
        for row in rows:
            key = str(row["key"])
            value = str(row["value"])
            if (
                key not in SECRET_APP_SETTING_KEYS
                or not value
                or self.secret_store.is_encrypted(value)
            ):
                continue
            connection.execute(
                """
                UPDATE app_settings
                SET value = ?, updated_at = ?
                WHERE key = ?
                """,
                (self.secret_store.encrypt(value), utc_now(), key),
            )

    def _encrypt_secret(self, value: str | None) -> str | None:
        if value is None:
            return None
        return self.secret_store.encrypt(value)

    def _decrypt_secret(self, value: Any, *, label: str) -> str | None:
        if value is None:
            return None
        return self.secret_store.decrypt(str(value), label=label)

    def _decode_account_row(self, row: sqlite3.Row) -> dict[str, Any]:
        account = dict(row)
        account_key = str(account.get("account_key") or "")
        for column in ACCOUNT_SECRET_COLUMNS:
            account[column] = self._decrypt_secret(
                account.get(column),
                label=f"accounts.{account_key}.{column}",
            )
        return account

    def try_acquire_job_lock(
        self,
        job_name: str,
        *,
        owner_id: str,
        lease_seconds: int,
    ) -> bool:
        expires_at = int(time.time()) + max(1, lease_seconds)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT owner_id, expires_at FROM job_locks WHERE job_name = ?",
                (job_name,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO job_locks (job_name, owner_id, expires_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (job_name, owner_id, expires_at, utc_now()),
                )
                return True

            if int(row["expires_at"] or 0) <= int(time.time()):
                connection.execute(
                    """
                    UPDATE job_locks
                    SET owner_id = ?, expires_at = ?, updated_at = ?
                    WHERE job_name = ?
                    """,
                    (owner_id, expires_at, utc_now(), job_name),
                )
                return True
        return False

    def release_job_lock(self, job_name: str, *, owner_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM job_locks WHERE job_name = ? AND owner_id = ?",
                (job_name, owner_id),
            )

    def renew_job_lock(
        self,
        job_name: str,
        *,
        owner_id: str,
        lease_seconds: int,
    ) -> bool:
        expires_at = int(time.time()) + max(1, lease_seconds)
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE job_locks
                SET expires_at = ?, updated_at = ?
                WHERE job_name = ? AND owner_id = ?
                """,
                (expires_at, utc_now(), job_name, owner_id),
            )
            return cursor.rowcount == 1

    def _ensure_material_items_source_fk(self, connection: sqlite3.Connection) -> None:
        foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(material_items)"
        ).fetchall()
        if not any(str(row["table"]) == "material_sources_old" for row in foreign_keys):
            return
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.executescript(
            """
            ALTER TABLE material_items RENAME TO material_items_old;
            CREATE TABLE material_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER,
                external_id TEXT,
                author TEXT,
                title TEXT,
                content TEXT NOT NULL,
                url TEXT,
                source_created_at TEXT,
                hash TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'new' CHECK(
                    status IN ('new', 'used', 'ignored', 'failed')
                ),
                tag_status TEXT NOT NULL DEFAULT 'pending',
                tag_json TEXT,
                tag_error TEXT,
                tagged_at TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(source_id) REFERENCES material_sources(id)
            );
            INSERT INTO material_items (
                id, source_id, external_id, author, title, content, url,
                source_created_at, hash, status, tag_status, tag_json,
                tag_error, tagged_at, error, created_at, updated_at
            )
            SELECT
                id, source_id, external_id, author, title, content, url,
                source_created_at, hash, status, tag_status, tag_json,
                tag_error, tagged_at, error, created_at, updated_at
            FROM material_items_old;
            DROP TABLE material_items_old;
            PRAGMA foreign_keys = ON;
            """
        )

    def _ensure_material_source_types(self, connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'material_sources'"
        ).fetchone()
        table_sql = str(row["sql"] if row else "")
        if "techflow_newsletter" in table_sql:
            return
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.executescript(
            """
            ALTER TABLE material_sources RENAME TO material_sources_old;
            CREATE TABLE material_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                source_type TEXT NOT NULL CHECK(
                    source_type IN ('binance_square', 'techflow_newsletter')
                ),
                url TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_checked_at TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(source_type, url)
            );
            INSERT INTO material_sources (
                id, name, source_type, url, enabled, last_checked_at,
                last_error, created_at, updated_at
            )
            SELECT
                id, name, source_type, url, enabled, last_checked_at,
                last_error, created_at, updated_at
            FROM material_sources_old;
            DROP TABLE material_sources_old;
            PRAGMA foreign_keys = ON;
            """
        )

    def upsert_material_source(
        self,
        *,
        name: str,
        source_type: str,
        url: str,
        enabled: bool = True,
    ) -> int:
        now = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO material_sources (
                    name, source_type, url, enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_type, url) DO UPDATE SET
                    name = excluded.name,
                    enabled = excluded.enabled,
                    updated_at = excluded.updated_at
                RETURNING id
                """,
                (name, source_type, url, 1 if enabled else 0, now, now),
            )
            return int(cursor.fetchone()["id"])

    def list_material_sources(
        self,
        *,
        include_disabled: bool = False,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM material_sources"
        if not include_disabled:
            query += " WHERE enabled = 1"
        query += " ORDER BY created_at DESC, id DESC"
        with self.connect() as connection:
            rows = connection.execute(query).fetchall()
        return [dict(row) for row in rows]

    def update_material_source_check(
        self,
        source_id: int,
        *,
        error: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE material_sources
                SET last_checked_at = ?, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (utc_now(), error, utc_now(), source_id),
            )

    def disable_material_source(self, source_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE material_sources
                SET enabled = 0, updated_at = ?
                WHERE id = ?
                """,
                (utc_now(), source_id),
            )

    def add_material_item(
        self,
        *,
        content: str,
        source_id: int | None = None,
        external_id: str | None = None,
        author: str | None = None,
        title: str | None = None,
        url: str | None = None,
        source_created_at: str | None = None,
    ) -> tuple[int, bool]:
        digest = self.content_hash(content)
        now = utc_now()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id FROM material_items WHERE hash = ?",
                (digest,),
            ).fetchone()
            if row:
                return int(row["id"]), False
            cursor = connection.execute(
                """
                INSERT INTO material_items (
                    source_id, external_id, author, title, content, url,
                    source_created_at, hash, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)
                """,
                (
                    source_id,
                    external_id,
                    author,
                    title,
                    content,
                    url,
                    source_created_at,
                    digest,
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid), True

    def list_material_items(
        self,
        *,
        status: str | None = "new",
        tag_status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT i.*, s.name AS source_name, s.source_type
            FROM material_items i
            LEFT JOIN material_sources s ON s.id = i.source_id
        """
        params: list[Any] = []
        if status:
            query += " WHERE i.status = ?"
            params.append(status)
        if tag_status:
            query += " AND" if params else " WHERE"
            query += " i.tag_status = ?"
            params.append(tag_status)
        query += " ORDER BY i.created_at DESC, i.id DESC LIMIT ?"
        params.append(limit)
        with self.connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def get_material_item(self, item_id: int) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT i.*, s.name AS source_name, s.source_type
                FROM material_items i
                LEFT JOIN material_sources s ON s.id = i.source_id
                WHERE i.id = ?
                """,
                (item_id,),
            ).fetchone()
        if not row:
            raise KeyError(f"素材不存在: {item_id}")
        return dict(row)

    def mark_material_item(
        self,
        item_id: int,
        *,
        status: str,
        error: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE material_items
                SET status = ?, error = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, error, utc_now(), item_id),
            )

    def pending_material_items_for_tagging(
        self,
        *,
        limit: int = 50,
        strategy: str | None = None,
    ) -> list[dict[str, Any]]:
        if not strategy:
            return self.list_material_items(
                status="new",
                tag_status="pending",
                limit=limit,
            )
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT i.*, s.name AS source_name, s.source_type
                FROM material_items i
                LEFT JOIN material_sources s ON s.id = i.source_id
                WHERE i.status = 'new'
                    AND (
                        i.tag_status = 'pending'
                        OR i.tag_json IS NULL
                        OR i.tag_json NOT LIKE ?
                    )
                ORDER BY i.created_at DESC, i.id DESC
                LIMIT ?
                """,
                (f'%"strategy": "{strategy}"%', limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_material_account_run(
        self,
        material_item_id: int,
        account_key: str,
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM material_account_runs
                WHERE material_item_id = ? AND account_key = ?
                """,
                (material_item_id, account_key),
            ).fetchone()
        return dict(row) if row else None

    def list_material_account_runs(self, material_item_id: int) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM material_account_runs
                WHERE material_item_id = ?
                ORDER BY created_at, account_key
                """,
                (material_item_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_material_queue_for_account(
        self,
        account_key: str,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    i.*,
                    s.name AS source_name,
                    s.source_type,
                    r.status AS run_status,
                    r.error AS run_error,
                    r.attempt_count
                FROM material_items i
                LEFT JOIN material_sources s ON s.id = i.source_id
                LEFT JOIN material_account_runs r
                    ON r.material_item_id = i.id AND r.account_key = ?
                WHERE i.status = 'new'
                    AND i.tag_status = 'accepted'
                    AND (
                        r.status IS NULL
                        OR (
                            r.status = 'failed'
                            AND COALESCE(r.error, '') NOT LIKE 'publish_outcome_unknown:%'
                        )
                    )
                ORDER BY
                    COALESCE(i.source_created_at, i.created_at) ASC,
                    i.id ASC
                LIMIT ?
                """,
                (account_key, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_material_account_run(
        self,
        material_item_id: int,
        *,
        account_key: str,
        status: str,
        generated_id: int | None = None,
        publish_result: dict[str, Any] | None = None,
        error: str | None = None,
        increment_attempts: bool = False,
    ) -> None:
        now = utc_now()
        attempt_delta = 1 if increment_attempts else 0
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO material_account_runs (
                    material_item_id,
                    account_key,
                    status,
                    generated_id,
                    publish_json,
                    error,
                    attempt_count,
                    last_attempted_at,
                    published_at,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(material_item_id, account_key) DO UPDATE SET
                    status = excluded.status,
                    generated_id = CASE
                        WHEN excluded.generated_id IS NULL
                        THEN material_account_runs.generated_id
                        ELSE excluded.generated_id
                    END,
                    publish_json = excluded.publish_json,
                    error = excluded.error,
                    attempt_count = material_account_runs.attempt_count + ?,
                    last_attempted_at = CASE
                        WHEN excluded.last_attempted_at IS NULL
                        THEN material_account_runs.last_attempted_at
                        ELSE excluded.last_attempted_at
                    END,
                    published_at = excluded.published_at,
                    updated_at = excluded.updated_at
                """,
                (
                    material_item_id,
                    account_key,
                    status,
                    generated_id,
                    json.dumps(publish_result, ensure_ascii=False)
                    if publish_result is not None
                    else None,
                    error,
                    attempt_delta,
                    now if increment_attempts else None,
                    now if status == "published" else None,
                    now,
                    now,
                    attempt_delta,
                ),
            )

    def save_material_tag(
        self,
        item_id: int,
        *,
        tag_status: str,
        tag: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE material_items
                SET tag_status = ?,
                    tag_json = ?,
                    tag_error = ?,
                    tagged_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    tag_status,
                    json.dumps(tag, ensure_ascii=False) if tag is not None else None,
                    error,
                    utc_now(),
                    utc_now(),
                    item_id,
                ),
            )

    def expire_stale_material_items(self, *, ttl_seconds: int) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE material_items
                SET status = 'ignored',
                    error = 'expired_after_ttl',
                    updated_at = ?
                WHERE status = 'new'
                    AND datetime(created_at) <= datetime('now', ?)
                """,
                (utc_now(), f"-{ttl_seconds} seconds"),
            )
            return int(cursor.rowcount)

    @staticmethod
    def content_hash(
        content: str,
        *,
        account_key: str | None = None,
        role: str | None = None,
    ) -> str:
        normalized = " ".join(content.split())
        if account_key or role:
            normalized = f"{account_key or 'default'}\0{role or ''}\0{normalized}"
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def upsert_account(
        self,
        *,
        account_key: str,
        name: str,
        square_openapi_key: str | None = None,
        proxy_url: str | None = None,
        mcp_url: str | None = None,
        mcp_auth_token: str | None = None,
    ) -> None:
        encrypted_square_openapi_key = self._encrypt_secret(square_openapi_key)
        encrypted_proxy_url = self._encrypt_secret(proxy_url)
        encrypted_mcp_auth_token = self._encrypt_secret(mcp_auth_token)
        new_check_status = (
            None
            if square_openapi_key is None
            else ("configured" if square_openapi_key.strip() else "missing")
        )
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO accounts (
                    account_key, name, square_openapi_key, proxy_url, mcp_url, mcp_auth_token,
                    check_status, enabled, created_at
                )
                VALUES (?, ?, COALESCE(?, ''), COALESCE(?, ''), COALESCE(?, ''), COALESCE(?, ''), COALESCE(?, 'unchecked'), 1, ?)
                ON CONFLICT(account_key) DO UPDATE SET
                    name = excluded.name,
                    square_openapi_key = CASE
                        WHEN ? IS NULL THEN accounts.square_openapi_key
                        ELSE excluded.square_openapi_key
                    END,
                    proxy_url = CASE
                        WHEN ? IS NULL THEN accounts.proxy_url
                        ELSE excluded.proxy_url
                    END,
                    mcp_url = CASE
                        WHEN ? IS NULL THEN accounts.mcp_url
                        ELSE excluded.mcp_url
                    END,
                    mcp_auth_token = CASE
                        WHEN ? IS NULL THEN accounts.mcp_auth_token
                        ELSE excluded.mcp_auth_token
                    END,
                    check_status = CASE
                        WHEN ? IS NULL THEN accounts.check_status
                        WHEN excluded.square_openapi_key = '' THEN 'missing'
                        ELSE 'configured'
                    END,
                    checked_at = CASE
                        WHEN ? IS NULL THEN accounts.checked_at
                        ELSE NULL
                    END,
                    check_error = CASE
                        WHEN ? IS NULL THEN accounts.check_error
                        ELSE NULL
                    END,
                    enabled = 1
                """,
                (
                    account_key,
                    name,
                    encrypted_square_openapi_key,
                    encrypted_proxy_url,
                    mcp_url,
                    encrypted_mcp_auth_token,
                    new_check_status,
                    utc_now(),
                    encrypted_square_openapi_key,
                    encrypted_proxy_url,
                    mcp_url,
                    encrypted_mcp_auth_token,
                    encrypted_square_openapi_key,
                    encrypted_square_openapi_key,
                    encrypted_square_openapi_key,
                ),
            )

    def list_accounts(self, *, include_disabled: bool = False) -> list[dict[str, Any]]:
        query = """
            SELECT account_key, name, square_openapi_key, proxy_url, mcp_url, mcp_auth_token,
                signature_key, check_status, checked_at, check_error, enabled, created_at
            FROM accounts
        """
        params: tuple[Any, ...] = ()
        if not include_disabled:
            query += " WHERE enabled = 1"
        query += " ORDER BY created_at, account_key"
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._decode_account_row(row) for row in rows]

    def disable_account(self, account_key: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE accounts SET enabled = 0 WHERE account_key = ?",
                (account_key,),
            )

    def update_account_check(
        self,
        account_key: str,
        *,
        signature_key: str | None,
        status: str,
        error: str | None = None,
    ) -> None:
        encrypted_signature_key = self._encrypt_secret(signature_key)
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE accounts
                SET signature_key = ?,
                    check_status = ?,
                    checked_at = ?,
                    check_error = ?
                WHERE account_key = ?
                """,
                (encrypted_signature_key, status, utc_now(), error, account_key),
            )

    def get_app_settings(self) -> dict[str, str]:
        with self.connect() as connection:
            rows = connection.execute("SELECT key, value FROM app_settings").fetchall()
        values: dict[str, str] = {}
        for row in rows:
            key = str(row["key"])
            value = str(row["value"])
            if key in SECRET_APP_SETTING_KEYS:
                value = self._decrypt_secret(
                    value,
                    label=f"app_settings.{key}",
                ) or ""
            values[key] = value
        return values

    def set_app_settings(self, values: dict[str, str]) -> None:
        now = utc_now()
        with self.connect() as connection:
            for key, value in values.items():
                stored_value = (
                    self._encrypt_secret(value)
                    if key in SECRET_APP_SETTING_KEYS
                    else value
                )
                connection.execute(
                    """
                    INSERT INTO app_settings (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at
                    """,
                    (key, stored_value, now),
                )

    def add_source_post(
        self,
        *,
        content: str,
        role: str,
        author: str | None = None,
        title: str | None = None,
        url: str | None = None,
        created_at: str | None = None,
        account_key: str = "default",
    ) -> tuple[int, bool]:
        digest = self.content_hash(content, account_key=account_key, role=role)
        legacy_digest = self.content_hash(content)
        with self.connect() as connection:
            existing = connection.execute(
                """
                SELECT id FROM source_posts
                WHERE hash IN (?, ?) AND account_key = ? AND role = ?
                """,
                (digest, legacy_digest, account_key, role),
            ).fetchone()
            if existing:
                return int(existing["id"]), False
            cursor = connection.execute(
                """
                INSERT INTO source_posts (
                    account_key, author, title, content, url, source_created_at, role,
                    hash, analysis_status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_key,
                    author,
                    title,
                    content,
                    url,
                    created_at,
                    role,
                    digest,
                    "pending" if role == "reference" else "not_required",
                    utc_now(),
                ),
            )
            return int(cursor.lastrowid), True

    def get_post(self, post_id: int) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM source_posts WHERE id = ?", (post_id,)
            ).fetchone()
        if not row:
            raise KeyError(f"文章不存在: {post_id}")
        return dict(row)

    def pending_reference_posts(self, account_key: str = "default") -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM source_posts
                WHERE account_key = ?
                    AND role = 'reference'
                    AND analysis_status IN ('pending', 'failed')
                ORDER BY id
                """,
                (account_key,),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_analysis(
        self, post_id: int, analysis: PostAnalysis | None, error: str | None = None
    ) -> None:
        status = "success" if analysis else "failed"
        payload = analysis.model_dump_json() if analysis else None
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO post_analysis (
                    post_id, analysis_json, status, error, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(post_id) DO UPDATE SET
                    analysis_json = excluded.analysis_json,
                    status = excluded.status,
                    error = excluded.error,
                    updated_at = excluded.updated_at
                """,
                (post_id, payload, status, error, utc_now()),
            )
            connection.execute(
                "UPDATE source_posts SET analysis_status = ? WHERE id = ?",
                (status, post_id),
            )

    def successful_analyses(self, account_key: str = "default") -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT s.id AS post_id, s.author, s.content, p.analysis_json
                FROM source_posts s
                JOIN post_analysis p ON p.post_id = s.id
                WHERE s.account_key = ? AND s.role = 'reference' AND p.status = 'success'
                ORDER BY s.id
                """,
                (account_key,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["analysis"] = json.loads(item.pop("analysis_json"))
            result.append(item)
        return result

    def save_profile(
        self,
        profile: StyleProfile,
        source_count: int,
        account_key: str = "default",
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO style_profiles (
                    account_key, profile_json, source_count, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(account_key) DO UPDATE SET
                    profile_json = excluded.profile_json,
                    source_count = excluded.source_count,
                    updated_at = excluded.updated_at
                """,
                (account_key, profile.model_dump_json(), source_count, utc_now()),
            )
            if account_key != "default":
                return
            connection.execute(
                """
                INSERT INTO author_profiles (
                    id, profile_json, source_count, updated_at
                ) VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    profile_json = excluded.profile_json,
                    source_count = excluded.source_count,
                    updated_at = excluded.updated_at
                """,
                (profile.model_dump_json(), source_count, utc_now()),
            )

    def get_profile(self, account_key: str = "default") -> StyleProfile | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT profile_json FROM style_profiles WHERE account_key = ?",
                (account_key,),
            ).fetchone()
            if row:
                return StyleProfile.model_validate_json(row["profile_json"])
            if account_key != "default":
                return None
            row = connection.execute(
                "SELECT profile_json FROM author_profiles WHERE id = 1"
            ).fetchone()
        return StyleProfile.model_validate_json(row["profile_json"]) if row else None

    def save_generated(
        self,
        *,
        source_post_id: int,
        candidate_index: int,
        original_content: str,
        content: str,
        status: str,
        review: ContentReview,
        rewrite_count: int,
        account_key: str = "default",
    ) -> int:
        now = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO generated_posts (
                    account_key, source_post_id, candidate_index, original_content, content,
                    status, review_json, rewrite_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_post_id, candidate_index) DO UPDATE SET
                    account_key = excluded.account_key,
                    original_content = excluded.original_content,
                    content = excluded.content,
                    status = excluded.status,
                    review_json = excluded.review_json,
                    rewrite_count = excluded.rewrite_count,
                    updated_at = excluded.updated_at
                RETURNING id
                """,
                (
                    account_key,
                    source_post_id,
                    candidate_index,
                    original_content,
                    content,
                    status,
                    review.model_dump_json(),
                    rewrite_count,
                    now,
                    now,
                ),
            )
            return int(cursor.fetchone()["id"])

    def list_generated(
        self,
        status: str | None = None,
        account_key: str | None = None,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT g.*, s.title AS source_title, s.content AS source_content
            FROM generated_posts g
            JOIN source_posts s ON s.id = g.source_post_id
        """
        clauses = []
        params: list[Any] = []
        if status:
            clauses.append("g.status = ?")
            params.append(status)
        if account_key:
            clauses.append("g.account_key = ?")
            params.append(account_key)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY g.created_at DESC, g.candidate_index"
        with self.connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def list_publish_history(
        self,
        *,
        limit: int | None = 100,
        account_key: str | None = None,
        status: str | None = None,
        days: int | None = None,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT
                r.*,
                a.name AS account_name,
                a.check_status AS account_check_status,
                m.title AS material_title,
                m.content AS material_content,
                m.url AS material_url,
                m.source_created_at,
                ms.name AS source_name,
                ms.source_type,
                g.content AS generated_content,
                g.publish_status AS generated_publish_status,
                g.published_at AS generated_published_at
            FROM material_account_runs r
            JOIN accounts a ON a.account_key = r.account_key
            JOIN material_items m ON m.id = r.material_item_id
            LEFT JOIN material_sources ms ON ms.id = m.source_id
            LEFT JOIN generated_posts g ON g.id = r.generated_id
        """
        clauses = []
        params: list[Any] = []
        if account_key:
            clauses.append("r.account_key = ?")
            params.append(account_key)
        if status:
            clauses.append("r.status = ?")
            params.append(status)
        if days is not None and days > 0:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            clauses.append("COALESCE(r.published_at, r.last_attempted_at, r.updated_at) >= ?")
            params.append(cutoff)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += """
            ORDER BY
                COALESCE(r.published_at, r.last_attempted_at, r.updated_at) DESC,
                r.id DESC
        """
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        with self.connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def publish_account_summaries(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    a.account_key,
                    a.name,
                    a.enabled,
                    a.check_status,
                    a.checked_at,
                    COALESCE(SUM(CASE WHEN r.status = 'published' THEN 1 ELSE 0 END), 0) AS published_count,
                    COALESCE(SUM(CASE WHEN r.status = 'failed' THEN 1 ELSE 0 END), 0) AS failed_count,
                    COALESCE(SUM(CASE WHEN r.status = 'skipped' THEN 1 ELSE 0 END), 0) AS skipped_count,
                    MAX(r.published_at) AS last_published_at,
                    MAX(COALESCE(r.last_attempted_at, r.updated_at)) AS last_activity_at
                FROM accounts a
                LEFT JOIN material_account_runs r ON r.account_key = a.account_key
                WHERE a.enabled = 1
                GROUP BY
                    a.account_key,
                    a.name,
                    a.enabled,
                    a.check_status,
                    a.checked_at
                ORDER BY published_count DESC, a.created_at, a.account_key
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def get_generated(self, generated_id: int) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT g.*, s.title AS source_title, s.url AS source_url,
                    s.content AS source_content
                FROM generated_posts g
                JOIN source_posts s ON s.id = g.source_post_id
                WHERE g.id = ?
                """,
                (generated_id,),
            ).fetchone()
        if not row:
            raise KeyError(f"候选稿不存在: {generated_id}")
        return dict(row)

    def mark_published(
        self,
        generated_id: int,
        *,
        result: dict[str, Any],
        success: bool,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE generated_posts
                SET publish_status = ?,
                    publish_json = ?,
                    published_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    "published" if success else "publish_failed",
                    json.dumps(result, ensure_ascii=False),
                    utc_now() if success else None,
                    utc_now(),
                    generated_id,
                ),
            )

    def update_generated_content(self, generated_id: int, content: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE generated_posts
                SET content = ?, updated_at = ?
                WHERE id = ?
                """,
                (content, utc_now(), generated_id),
            )

    def approve_generated(self, generated_id: int, final_content: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM generated_posts WHERE id = ?", (generated_id,)
            ).fetchone()
            if not row:
                raise KeyError(f"候选稿不存在: {generated_id}")
            if row["status"] != "pending":
                raise ValueError("只有待审核候选稿可以批准")
            now = utc_now()
            connection.execute(
                """
                UPDATE generated_posts
                SET content = ?, status = 'approved', updated_at = ?
                WHERE id = ?
                """,
                (final_content, now, generated_id),
            )
            connection.execute(
                """
                UPDATE generated_posts
                SET status = 'rejected', updated_at = ?
                WHERE source_post_id = ? AND id != ? AND status = 'pending'
                """,
                (now, row["source_post_id"], generated_id),
            )
            approved = connection.execute(
                "SELECT * FROM generated_posts WHERE id = ?", (generated_id,)
            ).fetchone()
        return dict(approved)

    def reject_generated(self, generated_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE generated_posts
                SET status = 'rejected', updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (utc_now(), generated_id),
            )
