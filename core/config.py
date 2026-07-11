from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
from functools import lru_cache
import json
from pathlib import Path
import os
from urllib.parse import unquote, urlsplit, urlunsplit

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SETTING_INTEGER_BOUNDS: dict[str, tuple[int, int]] = {
    "MATERIAL_POLL_INTERVAL_SECONDS": (10, 86_400),
    "MATERIAL_SUCCESS_INTERVAL_SECONDS": (10, 86_400),
    "MATERIAL_FAILURE_INTERVAL_SECONDS": (10, 86_400),
    "MATERIAL_TTL_SECONDS": (60, 604_800),
    "MATERIAL_CONSUME_BATCH_SIZE": (1, 20),
    "PUBLISH_FAILURE_ALERT_THRESHOLD": (1, 100),
    "MAX_POSTS_PER_ACCOUNT_PER_HOUR": (1, 5),
    "MAX_POSTS_PER_ACCOUNT_PER_DAY": (1, 80),
    "SMTP_PORT": (1, 65_535),
}


def _bounded_integer(name: str, value: str | int, default: int) -> int:
    raw = str(value).strip()
    if not raw:
        parsed = default
    else:
        try:
            parsed = int(raw)
        except ValueError as exc:
            raise ValueError(f"{name} 必须是整数") from exc
    minimum, maximum = SETTING_INTEGER_BOUNDS[name]
    return max(minimum, min(parsed, maximum))


def add_no_proxy_host(host: str) -> None:
    current = os.getenv("NO_PROXY", "")
    hosts = [item.strip() for item in current.split(",") if item.strip()]
    if host not in hosts:
        hosts.append(host)
    value = ",".join(hosts)
    os.environ["NO_PROXY"] = value
    os.environ["no_proxy"] = value


def normalize_proxy_url(value: str) -> str:
    proxy = value.strip()
    if not proxy:
        return ""
    if "://" not in proxy:
        proxy = f"http://{proxy}"
    parts = urlsplit(proxy)
    if not parts.scheme or not parts.netloc or not parts.hostname:
        raise ValueError("代理地址格式不正确，请使用 http://host:port 这类格式")
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def mask_url_credentials(value: str) -> str:
    url = value.strip()
    if not url or "://" not in url:
        return url
    parts = urlsplit(url)
    if not parts.username and not parts.password:
        return urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, f"****@{host}", "", "", ""))


def playwright_proxy_settings(proxy_url: str) -> dict[str, str] | None:
    proxy = normalize_proxy_url(proxy_url)
    if not proxy:
        return None
    parts = urlsplit(proxy)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    settings = {
        "server": urlunsplit((parts.scheme, host, "", "", "")),
    }
    if parts.username:
        settings["username"] = unquote(parts.username)
    if parts.password:
        settings["password"] = unquote(parts.password)
    return settings


def normalize_openai_base_url(value: str) -> str:
    url = value.strip()
    if not url:
        return ""
    if "://" not in url:
        url = f"https://{url}"

    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    for suffix in ("/chat/completions", "/responses", "/models"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


@dataclass(frozen=True)
class AccountConfig:
    key: str
    name: str
    square_openapi_key: str
    proxy_url: str = ""
    mcp_url: str = ""
    mcp_auth_token: str = ""
    check_status: str = "unchecked"
    enabled: bool = True


def _load_accounts(value: str) -> tuple[AccountConfig, ...]:
    value = value.strip()
    if not value:
        return ()
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return tuple(
            AccountConfig(
                key=f"account_{index}",
                name=f"account_{index}",
                square_openapi_key=item.strip(),
            )
            for index, item in enumerate(value.split(","), start=1)
            if item.strip()
        )
    if not isinstance(payload, list):
        raise ValueError("AGENT_ACCOUNTS 必须是 JSON 数组或逗号分隔账号列表")
    accounts = []
    for item in payload:
        if isinstance(item, str):
            key = f"account_{len(accounts) + 1}"
            accounts.append(AccountConfig(key=key, name=key, square_openapi_key=item))
            continue
        if not isinstance(item, dict):
            raise ValueError("AGENT_ACCOUNTS 中的账号必须是字符串或对象")
        key = str(item.get("key") or item.get("account_key") or "").strip()
        if not key:
            raise ValueError("AGENT_ACCOUNTS 每个账号都需要 key")
        accounts.append(
            AccountConfig(
                key=key,
                name=str(item.get("name") or key),
                square_openapi_key=str(
                    item.get("square_openapi_key")
                    or item.get("openapi_key")
                    or ""
                ).strip(),
                proxy_url=normalize_proxy_url(
                    str(item.get("proxy_url") or item.get("proxy") or "")
                )
                if item.get("proxy_url") or item.get("proxy")
                else "",
                mcp_url=str(item.get("mcp_url") or "").strip(),
                mcp_auth_token=str(item.get("mcp_auth_token") or "").strip(),
            )
        )
    return tuple(accounts)


@dataclass(frozen=True)
class Settings:
    llm_api_key: str
    llm_base_url: str
    llm_model: str
    embedding_provider: str
    embedding_api_key: str
    embedding_base_url: str
    embedding_model: str
    app_secret_key: str
    secret_key_path: Path
    database_path: Path
    chroma_path: Path
    publish_mode: str
    accounts: tuple[AccountConfig, ...]
    mcp_url: str
    mcp_publish_tool: str
    mcp_auth_token: str
    auto_monitor_enabled: bool
    auto_publish: bool
    material_poll_interval_seconds: int
    material_success_interval_seconds: int
    material_failure_interval_seconds: int
    material_ttl_seconds: int
    auto_consume_materials: bool
    material_consume_batch_size: int
    publish_failure_alert_threshold: int
    max_posts_per_account_per_hour: int
    max_posts_per_account_per_day: int
    alert_email_enabled: bool
    alert_email_to: str
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    smtp_from: str
    smtp_use_tls: bool
    web_auth_username: str
    web_auth_password: str
    allow_insecure_public_bind: bool

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        publish_mode = os.getenv("PUBLISH_MODE", "auto").strip().lower()
        legacy_dashscope_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
        embedding_provider = os.getenv("EMBEDDING_PROVIDER", "").strip().lower()
        if not embedding_provider:
            embedding_provider = "dashscope" if legacy_dashscope_key else "openai"
        embedding_model = (
            os.getenv("EMBEDDING_MODEL", "").strip()
            or os.getenv("DASHSCOPE_EMBEDDING_MODEL", "").strip()
            or (
                "text-embedding-v3"
                if embedding_provider == "dashscope"
                else "embedding-3"
            )
        )
        return cls(
            llm_api_key=os.getenv("LLM_API_KEY", ""),
            llm_base_url=normalize_openai_base_url(os.getenv("LLM_BASE_URL", "")),
            llm_model=os.getenv("LLM_MODEL", ""),
            embedding_provider=embedding_provider,
            embedding_api_key=(
                os.getenv("EMBEDDING_API_KEY", "").strip()
                or legacy_dashscope_key
            ),
            embedding_base_url=normalize_openai_base_url(
                os.getenv("EMBEDDING_BASE_URL", "")
            ),
            embedding_model=embedding_model,
            app_secret_key=os.getenv("APP_SECRET_KEY", "").strip(),
            secret_key_path=_resolve_project_path(
                os.getenv("SECRET_KEY_PATH", "./data/app_secret.key")
            ),
            database_path=_resolve_project_path(
                os.getenv("DATABASE_PATH", "./data/bn_square.db")
            ),
            chroma_path=_resolve_project_path(os.getenv("CHROMA_PATH", "./chroma_db")),
            publish_mode=publish_mode,
            accounts=_load_accounts(os.getenv("AGENT_ACCOUNTS", "")),
            mcp_url=os.getenv("MCP_URL", "").strip(),
            mcp_publish_tool=os.getenv("MCP_PUBLISH_TOOL", "").strip(),
            mcp_auth_token=os.getenv("MCP_AUTH_TOKEN", "").strip(),
            auto_monitor_enabled=os.getenv("AUTO_MONITOR_ENABLED", "1")
            .strip()
            .lower()
            not in {"0", "false", "no", "off"},
            auto_publish=os.getenv("AUTO_PUBLISH", "1").strip().lower()
            not in {"0", "false", "no", "off"}
            and publish_mode != "manual",
            material_poll_interval_seconds=_bounded_integer(
                "MATERIAL_POLL_INTERVAL_SECONDS",
                os.getenv("MATERIAL_POLL_INTERVAL_SECONDS", "300"),
                300,
            ),
            material_success_interval_seconds=_bounded_integer(
                "MATERIAL_SUCCESS_INTERVAL_SECONDS",
                os.getenv("MATERIAL_SUCCESS_INTERVAL_SECONDS", "600"),
                600,
            ),
            material_failure_interval_seconds=_bounded_integer(
                "MATERIAL_FAILURE_INTERVAL_SECONDS",
                os.getenv("MATERIAL_FAILURE_INTERVAL_SECONDS", "120"),
                120,
            ),
            material_ttl_seconds=_bounded_integer(
                "MATERIAL_TTL_SECONDS",
                os.getenv("MATERIAL_TTL_SECONDS", "7200"),
                7200,
            ),
            auto_consume_materials=os.getenv("AUTO_CONSUME_MATERIALS", "1")
            .strip()
            .lower()
            not in {"0", "false", "no", "off"},
            material_consume_batch_size=_bounded_integer(
                "MATERIAL_CONSUME_BATCH_SIZE",
                os.getenv("MATERIAL_CONSUME_BATCH_SIZE", "1"),
                1,
            ),
            publish_failure_alert_threshold=_bounded_integer(
                "PUBLISH_FAILURE_ALERT_THRESHOLD",
                os.getenv("PUBLISH_FAILURE_ALERT_THRESHOLD", "5"),
                5,
            ),
            max_posts_per_account_per_hour=_bounded_integer(
                "MAX_POSTS_PER_ACCOUNT_PER_HOUR",
                os.getenv("MAX_POSTS_PER_ACCOUNT_PER_HOUR", "5"),
                5,
            ),
            max_posts_per_account_per_day=_bounded_integer(
                "MAX_POSTS_PER_ACCOUNT_PER_DAY",
                os.getenv("MAX_POSTS_PER_ACCOUNT_PER_DAY", "80"),
                80,
            ),
            alert_email_enabled=os.getenv("ALERT_EMAIL_ENABLED", "0")
            .strip()
            .lower()
            not in {"0", "false", "no", "off"},
            alert_email_to=os.getenv("ALERT_EMAIL_TO", "").strip(),
            smtp_host=os.getenv("SMTP_HOST", "").strip(),
            smtp_port=_bounded_integer(
                "SMTP_PORT",
                os.getenv("SMTP_PORT", "587"),
                587,
            ),
            smtp_username=os.getenv("SMTP_USERNAME", "").strip(),
            smtp_password=os.getenv("SMTP_PASSWORD", "").strip(),
            smtp_from=os.getenv("SMTP_FROM", "").strip(),
            smtp_use_tls=os.getenv("SMTP_USE_TLS", "1").strip().lower()
            not in {"0", "false", "no", "off"},
            web_auth_username=os.getenv("WEB_AUTH_USERNAME", "").strip(),
            web_auth_password=os.getenv("WEB_AUTH_PASSWORD", "").strip(),
            allow_insecure_public_bind=os.getenv(
                "ALLOW_INSECURE_PUBLIC_BIND", "0"
            )
            .strip()
            .lower()
            not in {"0", "false", "no", "off"},
        )

    def validate_for_llm(self) -> None:
        missing = [
            name
            for name, value in (
                ("LLM_API_KEY", self.llm_api_key),
                ("LLM_BASE_URL", self.llm_base_url),
                ("LLM_MODEL", self.llm_model),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"缺少配置: {', '.join(missing)}")

    def validate_for_rag(self) -> None:
        if self.embedding_provider not in {"dashscope", "openai"}:
            raise ValueError("EMBEDDING_PROVIDER 只支持 dashscope 或 openai")
        missing = []
        if not self.resolved_embedding_api_key():
            missing.append("EMBEDDING_API_KEY")
        if self.embedding_provider == "openai" and not self.resolved_embedding_base_url():
            missing.append("EMBEDDING_BASE_URL")
        if not self.embedding_model:
            missing.append("EMBEDDING_MODEL")
        if missing:
            raise ValueError(f"缺少配置: {', '.join(missing)}")

    def resolved_embedding_api_key(self) -> str:
        if self.embedding_api_key:
            return self.embedding_api_key
        if self.embedding_provider == "openai":
            return self.llm_api_key
        return ""

    def resolved_embedding_base_url(self) -> str:
        if self.embedding_base_url:
            return self.embedding_base_url
        if self.embedding_provider == "openai":
            return self.llm_base_url
        return ""

    def validate_for_publish(self) -> None:
        missing = [
            account.key for account in self.accounts if not account.square_openapi_key
        ]
        if missing:
            raise ValueError(
                f"以下账号缺少 Binance Square OpenAPI Key: {', '.join(missing)}"
            )

    def with_overrides(self, values: dict[str, str]) -> "Settings":
        if not values:
            return self

        def text(name: str, current: str) -> str:
            value = values.get(name)
            return current if value is None else value.strip()

        def integer(name: str, current: int) -> int:
            value = values.get(name)
            if value is None or not value.strip():
                return current
            return _bounded_integer(name, value, current)

        def boolean(name: str, current: bool) -> bool:
            value = values.get(name)
            if value is None:
                return current
            return value.strip().lower() not in {"0", "false", "no", "off"}

        return replace(
            self,
            llm_api_key=text("LLM_API_KEY", self.llm_api_key),
            llm_base_url=normalize_openai_base_url(
                text("LLM_BASE_URL", self.llm_base_url)
            ),
            llm_model=text("LLM_MODEL", self.llm_model),
            embedding_provider=text(
                "EMBEDDING_PROVIDER",
                (
                    "dashscope"
                    if "EMBEDDING_PROVIDER" not in values
                    and values.get("DASHSCOPE_API_KEY")
                    else self.embedding_provider
                ),
            ).lower(),
            embedding_api_key=text(
                "EMBEDDING_API_KEY",
                text("DASHSCOPE_API_KEY", self.embedding_api_key),
            ),
            embedding_base_url=normalize_openai_base_url(
                text("EMBEDDING_BASE_URL", self.embedding_base_url)
            ),
            embedding_model=text(
                "EMBEDDING_MODEL",
                text("DASHSCOPE_EMBEDDING_MODEL", self.embedding_model),
            ),
            mcp_url=text("MCP_URL", self.mcp_url),
            mcp_publish_tool=text("MCP_PUBLISH_TOOL", self.mcp_publish_tool),
            mcp_auth_token=text("MCP_AUTH_TOKEN", self.mcp_auth_token),
            auto_monitor_enabled=boolean(
                "AUTO_MONITOR_ENABLED",
                self.auto_monitor_enabled,
            ),
            auto_publish=boolean("AUTO_PUBLISH", self.auto_publish),
            material_poll_interval_seconds=integer(
                "MATERIAL_POLL_INTERVAL_SECONDS",
                self.material_poll_interval_seconds,
            ),
            material_success_interval_seconds=integer(
                "MATERIAL_SUCCESS_INTERVAL_SECONDS",
                self.material_success_interval_seconds,
            ),
            material_failure_interval_seconds=integer(
                "MATERIAL_FAILURE_INTERVAL_SECONDS",
                self.material_failure_interval_seconds,
            ),
            material_ttl_seconds=integer("MATERIAL_TTL_SECONDS", self.material_ttl_seconds),
            auto_consume_materials=boolean(
                "AUTO_CONSUME_MATERIALS",
                self.auto_consume_materials,
            ),
            material_consume_batch_size=integer(
                "MATERIAL_CONSUME_BATCH_SIZE",
                self.material_consume_batch_size,
            ),
            publish_failure_alert_threshold=integer(
                "PUBLISH_FAILURE_ALERT_THRESHOLD",
                self.publish_failure_alert_threshold,
            ),
            max_posts_per_account_per_hour=integer(
                "MAX_POSTS_PER_ACCOUNT_PER_HOUR",
                self.max_posts_per_account_per_hour,
            ),
            max_posts_per_account_per_day=integer(
                "MAX_POSTS_PER_ACCOUNT_PER_DAY",
                self.max_posts_per_account_per_day,
            ),
            alert_email_enabled=boolean(
                "ALERT_EMAIL_ENABLED",
                self.alert_email_enabled,
            ),
            alert_email_to=text("ALERT_EMAIL_TO", self.alert_email_to),
            smtp_host=text("SMTP_HOST", self.smtp_host),
            smtp_port=integer("SMTP_PORT", self.smtp_port),
            smtp_username=text("SMTP_USERNAME", self.smtp_username),
            smtp_password=text("SMTP_PASSWORD", self.smtp_password),
            smtp_from=text("SMTP_FROM", self.smtp_from),
            smtp_use_tls=boolean("SMTP_USE_TLS", self.smtp_use_tls),
        )

    def build_database(self):
        return _build_database_cached(
            str(self.database_path),
            self.app_secret_key,
            str(self.secret_key_path),
        )


@lru_cache(maxsize=4)
def _build_database_cached(
    database_path: str,
    app_secret_key: str,
    secret_key_path: str,
):
    from ..storage.database import Database
    from .secret_store import SecretStore

    return Database(
        Path(database_path),
        secret_store=SecretStore.from_values(
            app_secret_key=app_secret_key,
            secret_key_path=Path(secret_key_path),
        ),
    )


def _resolve_project_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path
