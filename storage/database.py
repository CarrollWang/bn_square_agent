from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator

from ..models.schemas import ContentReview, PostAnalysis, StyleProfile


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
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
                cookie TEXT NOT NULL DEFAULT '',
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
                source_type TEXT NOT NULL CHECK(source_type IN ('binance_square')),
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
            """
        )
        account_columns = self._columns(connection, "accounts")
        if "cookie" not in account_columns:
            connection.execute(
                "ALTER TABLE accounts ADD COLUMN cookie TEXT NOT NULL DEFAULT ''"
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

    def pending_material_items_for_tagging(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return self.list_material_items(status="new", tag_status="pending", limit=limit)

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
                    error = 'expired_after_2h',
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

    def upsert_account(self, *, account_key: str, name: str, cookie: str = "") -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO accounts (account_key, name, cookie, enabled, created_at)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(account_key) DO UPDATE SET
                    name = excluded.name,
                    cookie = CASE
                        WHEN excluded.cookie = '' THEN accounts.cookie
                        ELSE excluded.cookie
                    END,
                    enabled = 1
                """,
                (account_key, name, cookie, utc_now()),
            )

    def list_accounts(self, *, include_disabled: bool = False) -> list[dict[str, Any]]:
        query = """
            SELECT account_key, name, cookie, signature_key, check_status,
                checked_at, check_error, enabled, created_at
            FROM accounts
        """
        params: tuple[Any, ...] = ()
        if not include_disabled:
            query += " WHERE enabled = 1"
        query += " ORDER BY created_at, account_key"
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

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
                (signature_key, status, utc_now(), error, account_key),
            )

    def get_app_settings(self) -> dict[str, str]:
        with self.connect() as connection:
            rows = connection.execute("SELECT key, value FROM app_settings").fetchall()
        return {str(row["key"]): str(row["value"]) for row in rows}

    def set_app_settings(self, values: dict[str, str]) -> None:
        now = utc_now()
        with self.connect() as connection:
            for key, value in values.items():
                connection.execute(
                    """
                    INSERT INTO app_settings (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at
                    """,
                    (key, value, now),
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
