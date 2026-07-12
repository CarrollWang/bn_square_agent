from __future__ import annotations

import base64
from dataclasses import dataclass
import mimetypes
import time
from typing import Any
from uuid import uuid4

import httpx


BASE_URL_V1 = "https://www.binance.com/bapi/composite/v1/public/pgc/openApi"
BASE_URL_V2 = "https://www.binance.com/bapi/composite/v2/public/pgc/openApi"
POLL_INTERVAL_SECONDS = 3
MAX_POLL_RETRIES = 10


class BinanceSquareOpenAPIError(RuntimeError):
    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class BinanceSquarePublishResult:
    success: bool
    outcome: str
    message: str
    post_id: str | None = None
    post_url: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "outcome": self.outcome,
            "message": self.message,
            "post_id": self.post_id,
            "post_url": self.post_url,
        }


class BinanceSquareOpenAPIClient:
    def __init__(
        self,
        api_key: str,
        *,
        proxy_url: str = "",
        timeout_seconds: float = 90,
    ):
        key = api_key.strip()
        if not key:
            raise ValueError("账号缺少 Binance Square OpenAPI Key")
        self.api_key = key
        self.proxy_url = proxy_url.strip()
        self.timeout_seconds = timeout_seconds

    def _client(self) -> httpx.Client:
        kwargs: dict[str, Any] = {
            "timeout": httpx.Timeout(self.timeout_seconds),
            "trust_env": False,
            "follow_redirects": True,
        }
        if self.proxy_url:
            kwargs["proxy"] = self.proxy_url
        return httpx.Client(**kwargs)

    def _headers(self) -> dict[str, str]:
        return {
            "X-Square-OpenAPI-Key": self.api_key,
            "Content-Type": "application/json",
            "clienttype": "binanceSkill",
        }

    def _api(
        self,
        endpoint: str,
        body: dict[str, Any],
        *,
        base_url: str = BASE_URL_V2,
    ) -> dict[str, Any]:
        with self._client() as client:
            response = client.post(
                f"{base_url}{endpoint}",
                headers=self._headers(),
                json=body,
            )
        if endpoint == "/content/add" and response.status_code == 504:
            return {
                "id": None,
                "shareLink": None,
                "publishStatus": "success_without_post_id",
            }
        try:
            payload = response.json()
        except ValueError as exc:
            raise BinanceSquareOpenAPIError(
                f"Binance Square OpenAPI 返回非 JSON: HTTP {response.status_code}"
            ) from exc
        if not isinstance(payload, dict):
            raise BinanceSquareOpenAPIError("Binance Square OpenAPI 返回格式错误")
        code = str(payload.get("code") or "")
        if code != "000000":
            message = str(payload.get("message") or "未知错误")
            raise BinanceSquareOpenAPIError(
                f"Binance Square OpenAPI 错误 [{code}]: {message}",
                code=code or None,
            )
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _decode_image(image_base64: str) -> tuple[bytes, str, str]:
        raw = image_base64.strip()
        if not raw:
            raise ValueError("图片内容为空")
        mime_type = "image/png"
        encoded = raw
        if raw.startswith("data:"):
            header, separator, encoded = raw.partition(",")
            if not separator or ";base64" not in header:
                raise ValueError("image_base64 必须是 base64 data URL")
            mime_type = header[5:].split(";", 1)[0] or mime_type
        if not mime_type.startswith("image/"):
            raise ValueError("只支持图片 base64")
        try:
            image_bytes = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise ValueError("image_base64 不是合法 base64") from exc
        if not image_bytes:
            raise ValueError("图片内容为空")
        extension = mimetypes.guess_extension(mime_type) or ".png"
        if extension == ".jpe":
            extension = ".jpg"
        return image_bytes, mime_type, extension

    def _upload_image(self, image_base64: str) -> str:
        image_bytes, mime_type, extension = self._decode_image(image_base64)
        image_name = f"bn-square-{uuid4().hex}{extension}"
        upload = self._api("/image/presignedUrl", {"imageName": image_name})
        presigned_url = str(upload.get("presignedUrl") or "")
        file_ticket = str(upload.get("fileTicket") or "")
        if not presigned_url or not file_ticket:
            raise BinanceSquareOpenAPIError("图片上传凭证缺失")
        with self._client() as client:
            response = client.put(
                presigned_url,
                headers={"Content-Type": mime_type},
                content=image_bytes,
            )
        if response.is_error:
            raise BinanceSquareOpenAPIError(
                f"图片上传失败: HTTP {response.status_code}"
            )
        for _ in range(MAX_POLL_RETRIES):
            status = self._api("/image/imageStatus", {"fileTicket": file_ticket})
            state = int(status.get("status") or 0)
            if state == 1:
                image_url = str(status.get("imageUrl") or "")
                if not image_url:
                    raise BinanceSquareOpenAPIError("图片处理完成但未返回 URL")
                return image_url
            if state == 2:
                reason = str(status.get("failedReason") or "未知原因")
                raise BinanceSquareOpenAPIError(f"图片处理失败: {reason}")
            time.sleep(POLL_INTERVAL_SECONDS)
        raise BinanceSquareOpenAPIError("图片处理超时")

    def publish_text(
        self,
        content: str,
        *,
        image_base64: str = "",
    ) -> BinanceSquarePublishResult:
        text = content.strip()
        if not text:
            raise ValueError("发布正文不能为空")
        body: dict[str, Any] = {
            "contentType": 1,
            "bodyTextOnly": text,
        }
        if image_base64.strip():
            body["imageList"] = [self._upload_image(image_base64)]
        result = self._api("/content/add", body, base_url=BASE_URL_V1)
        if result.get("publishStatus") == "success_without_post_id":
            return BinanceSquarePublishResult(
                success=False,
                outcome="unknown",
                message="请求返回 HTTP 504，可能已提交但没有帖子 ID 或 URL",
            )
        post_id = str(result.get("id") or "").strip() or None
        post_url = str(result.get("shareLink") or "").strip() or None
        if not post_id and not post_url:
            return BinanceSquarePublishResult(
                success=False,
                outcome="unknown",
                message="OpenAPI 返回成功码，但没有帖子 ID 或 URL",
            )
        return BinanceSquarePublishResult(
            success=True,
            outcome="published",
            message="Binance Square 发布成功",
            post_id=post_id,
            post_url=post_url,
        )
