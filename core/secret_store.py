from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from cryptography.fernet import Fernet, InvalidToken

if TYPE_CHECKING:
    from .config import Settings


class SecretStore:
    PREFIX = "enc:v1:"

    def __init__(self, *, fernet: Fernet, key_source: str):
        self._fernet = fernet
        self.key_source = key_source

    @classmethod
    def from_settings(cls, settings: "Settings") -> "SecretStore":
        return cls.from_values(
            app_secret_key=settings.app_secret_key,
            secret_key_path=settings.secret_key_path,
        )

    @classmethod
    def from_values(
        cls,
        *,
        app_secret_key: str,
        secret_key_path: Path,
    ) -> "SecretStore":
        inline_key = app_secret_key.strip()
        if inline_key:
            return cls._from_key(inline_key.encode("utf-8"), "env:APP_SECRET_KEY")
        key_path = secret_key_path
        key_bytes = _load_or_create_key_file(key_path)
        return cls._from_key(key_bytes, f"file:{key_path}")

    @classmethod
    def _from_key(cls, key: bytes, key_source: str) -> "SecretStore":
        try:
            fernet = Fernet(key)
        except Exception as exc:
            raise ValueError(
                "APP_SECRET_KEY 或 SECRET_KEY_PATH 必须提供合法的 Fernet key"
            ) from exc
        return cls(fernet=fernet, key_source=key_source)

    def is_encrypted(self, value: str) -> bool:
        return value.startswith(self.PREFIX)

    def encrypt(self, value: str) -> str:
        if not value or self.is_encrypted(value):
            return value
        token = self._fernet.encrypt(value.encode("utf-8")).decode("utf-8")
        return f"{self.PREFIX}{token}"

    def decrypt(self, value: str, *, label: str | None = None) -> str:
        if not value or not self.is_encrypted(value):
            return value
        token = value[len(self.PREFIX) :]
        try:
            return self._fernet.decrypt(token.encode("utf-8")).decode("utf-8")
        except InvalidToken as exc:
            target = f" {label}" if label else ""
            raise ValueError(
                f"无法解密{target}，请确认 APP_SECRET_KEY / SECRET_KEY_PATH 与历史数据一致"
            ) from exc


def _load_or_create_key_file(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        data = path.read_bytes().strip()
        if not data:
            raise ValueError(f"密钥文件为空: {path}")
        return data

    key = Fernet.generate_key()
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _load_or_create_key_file(path)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(key)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key
