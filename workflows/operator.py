from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Any

from ..core.config import AccountConfig
from ..storage.database import Database
from ..publishing.publisher import PublishingService, PublishResult


@dataclass
class AccountContentRun:
    account_key: str
    generated_ids: list[int] = field(default_factory=list)
    approved_generated_id: int | None = None
    publish_result: PublishResult | None = None
    status: str = "pending"
    skipped_reason: str | None = None
    error: str | None = None


class MultiAccountOperator:
    def __init__(
        self,
        *,
        db: Database,
        accounts: tuple[AccountConfig, ...],
        content_graph: Any,
        publishing_service: PublishingService | None = None,
        auto_publish: bool = True,
    ):
        self.db = db
        self.accounts = accounts
        self.content_graph = content_graph
        self.publishing_service = publishing_service
        self.auto_publish = auto_publish
        for account in accounts:
            self.db.upsert_account(
                account_key=account.key,
                name=account.name,
                cookie=account.cookie,
            )

    @staticmethod
    def _symbol_from_material(item: dict[str, Any]) -> str | None:
        raw = item.get("tag_json")
        if isinstance(raw, str) and raw.strip():
            try:
                tag = json.loads(raw)
            except ValueError:
                tag = {}
            symbol = str(tag.get("symbol") or "").strip().upper()
            if re.fullmatch(r"[A-Z0-9]{2,30}USDT", symbol):
                return symbol
        text = f"{item.get('title') or ''}\n{item.get('content') or ''}"
        explicit = re.search(r"\{future\}\(([A-Z0-9]{2,30}USDT)\)", text, re.I)
        if explicit:
            return explicit.group(1).upper()
        pair = re.search(r"\b([A-Z0-9]{2,30}USDT)\b", text)
        if pair:
            return pair.group(1).upper()
        token = re.search(r"\$([A-Z][A-Z0-9]{0,14})\b", text)
        if token and token.group(1).upper() not in {"USD", "USDT"}:
            return f"{token.group(1).upper()}USDT"
        return None

    @staticmethod
    def _ensure_future_marker(content: str, symbol: str | None) -> str:
        if not symbol:
            return content
        marker = f"{{future}}({symbol})"
        if marker in content or re.search(r"\{future\}\([A-Z0-9]{2,30}USDT\)", content):
            return content
        return f"{content.rstrip()}\n\n{marker}"

    def _attach_future_marker(
        self,
        *,
        generated_id: int | None,
        symbol: str | None,
    ) -> None:
        if generated_id is None or not symbol:
            return
        generated = self.db.get_generated(generated_id)
        content = self._ensure_future_marker(generated["content"], symbol)
        if content != generated["content"]:
            self.db.update_generated_content(generated_id, content)

    def _publish_blocker(self, account: AccountConfig) -> str | None:
        if not (self.auto_publish and self.publishing_service):
            return None
        if not account.enabled:
            return "账号已禁用，已跳过"
        if not account.cookie:
            return "账号缺少 Cookie，已跳过"
        if account.check_status == "invalid":
            return "账号检测失效，已跳过"
        return None

    def _account_requires_material_run(self, account: AccountConfig) -> bool:
        return self._publish_blocker(account) is None

    def _account_by_key(self, account_key: str) -> AccountConfig:
        for account in self.accounts:
            if account.key == account_key:
                return account
        raise KeyError(f"账号不存在: {account_key}")

    @staticmethod
    def _decode_publish_json(raw: Any) -> dict[str, Any]:
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str) and raw.strip():
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = {}
            if isinstance(payload, dict):
                return payload
        return {}

    def _generate_for_account(
        self,
        account: AccountConfig,
        *,
        content: str,
        title: str | None,
        url: str | None,
        future_symbol: str | None,
    ) -> AccountContentRun:
        run = AccountContentRun(account_key=account.key)
        blocker = self._publish_blocker(account)
        if blocker:
            run.status = "skipped"
            run.skipped_reason = blocker
            return run
        try:
            state = self.content_graph.invoke(
                {
                    "account_key": account.key,
                    "content": content,
                    "title": title,
                    "url": url,
                }
            )
            run.generated_ids = state.get("generated_ids", [])
            run.approved_generated_id = state.get("approved_generated_id")
            self._attach_future_marker(
                generated_id=run.approved_generated_id,
                symbol=future_symbol,
            )
            if run.approved_generated_id is None:
                run.status = "failed"
                run.error = "未生成通过审核的终稿"
                return run
            if self.auto_publish and self.publishing_service:
                run.publish_result = self.publishing_service.publish_generated(
                    account=account,
                    generated_id=run.approved_generated_id,
                )
                run.status = "published" if run.publish_result.success else "failed"
            else:
                run.status = "generated"
        except Exception as exc:
            run.status = "failed"
            run.error = str(exc)
        return run

    def _restore_material_run(
        self,
        *,
        account_key: str,
        record: dict[str, Any],
    ) -> AccountContentRun:
        run = AccountContentRun(
            account_key=account_key,
            approved_generated_id=record.get("generated_id"),
        )
        status = str(record.get("status") or "")
        if status == "published":
            run.status = "already_published"
        elif status == "skipped":
            run.status = "skipped"
            run.skipped_reason = record.get("error") or "此前已跳过"
        else:
            run.status = status or "pending"
            run.error = record.get("error")
            payload = self._decode_publish_json(record.get("publish_json"))
            if payload:
                run.publish_result = PublishResult(
                    account_key,
                    int(record.get("generated_id") or 0),
                    False,
                    payload,
                )
        return run

    def _save_material_run(
        self,
        material_item_id: int,
        run: AccountContentRun,
    ) -> None:
        if run.status == "published" and run.publish_result:
            self.db.save_material_account_run(
                material_item_id,
                account_key=run.account_key,
                status="published",
                generated_id=run.approved_generated_id,
                publish_result=run.publish_result.result,
                increment_attempts=True,
            )
            return
        if run.status == "skipped":
            self.db.save_material_account_run(
                material_item_id,
                account_key=run.account_key,
                status="skipped",
                generated_id=run.approved_generated_id,
                error=run.skipped_reason,
                increment_attempts=False,
            )
            return
        if run.status == "already_published":
            return
        self.db.save_material_account_run(
            material_item_id,
            account_key=run.account_key,
            status="failed",
            generated_id=run.approved_generated_id,
            publish_result=run.publish_result.result if run.publish_result else None,
            error=run.error,
            increment_attempts=True,
        )

    def _finalize_material_item(self, material_item_id: int) -> None:
        records = {
            str(record["account_key"]): record
            for record in self.db.list_material_account_runs(material_item_id)
        }
        target_accounts = [
            account for account in self.accounts if self._account_requires_material_run(account)
        ]
        errors = []
        skips = []
        pending = []
        for account in target_accounts:
            record = records.get(account.key)
            if record is None:
                pending.append(account.key)
                continue
            status = str(record.get("status") or "")
            if status == "published":
                continue
            if status == "failed":
                detail = str(record.get("error") or "")
                if not detail and record.get("publish_json"):
                    detail = str(record["publish_json"])
                if len(detail) > 300:
                    detail = f"{detail[:300]}..."
                errors.append(f"{account.key}: {detail or '未完成发布'}")
                continue
            pending.append(account.key)

        for account_key, record in records.items():
            if str(record.get("status") or "") == "skipped" and record.get("error"):
                skips.append(f"{account_key}: {record['error']}")

        if errors:
            message = "; ".join(errors + skips)
            self.db.mark_material_item(
                material_item_id,
                status="new",
                error=message or "仍有账号未完成发布",
            )
            return

        if pending:
            self.db.mark_material_item(
                material_item_id,
                status="new",
                error=f"等待账号继续处理: {', '.join(pending[:8])}",
            )
            return

        message = "; ".join(skips) if skips else None
        self.db.mark_material_item(material_item_id, status="used", error=message)

    def generate_for_all_accounts(
        self,
        *,
        content: str,
        title: str | None = None,
        url: str | None = None,
        future_symbol: str | None = None,
    ) -> list[AccountContentRun]:
        runs = []
        for account in self.accounts:
            runs.append(
                self._generate_for_account(
                    account,
                    content=content,
                    title=title,
                    url=url,
                    future_symbol=future_symbol,
                )
            )
        return runs

    def run_material_item_for_all_accounts(
        self,
        material_item_id: int,
    ) -> list[AccountContentRun]:
        item = self.db.get_material_item(material_item_id)
        symbol = self._symbol_from_material(item)
        runs: list[AccountContentRun] = []
        for account in self.accounts:
            existing = self.db.get_material_account_run(material_item_id, account.key)
            if existing and str(existing.get("status") or "") in {"published", "skipped"}:
                runs.append(
                    self._restore_material_run(
                        account_key=account.key,
                        record=existing,
                    )
                )
                continue
            run = self._generate_for_account(
                account,
                content=item["content"],
                title=item.get("title"),
                url=item.get("url"),
                future_symbol=symbol,
            )
            self._save_material_run(material_item_id, run)
            runs.append(run)
        self._finalize_material_item(material_item_id)
        return runs

    def run_material_item_for_account(
        self,
        material_item_id: int,
        account_key: str,
    ) -> AccountContentRun:
        account = self._account_by_key(account_key)
        existing = self.db.get_material_account_run(material_item_id, account.key)
        if existing and str(existing.get("status") or "") in {"published", "skipped"}:
            return self._restore_material_run(account_key=account.key, record=existing)
        item = self.db.get_material_item(material_item_id)
        symbol = self._symbol_from_material(item)
        run = self._generate_for_account(
            account,
            content=item["content"],
            title=item.get("title"),
            url=item.get("url"),
            future_symbol=symbol,
        )
        self._save_material_run(material_item_id, run)
        self._finalize_material_item(material_item_id)
        return run

    def run_pending_material_queue(
        self,
        *,
        limit_per_account: int = 1,
        account_offset: int = 0,
        max_total_runs: int | None = None,
    ) -> list[dict[str, Any]]:
        if not self.accounts:
            return []
        ordered_accounts = list(self.accounts)
        if ordered_accounts and account_offset:
            shift = account_offset % len(ordered_accounts)
            ordered_accounts = ordered_accounts[shift:] + ordered_accounts[:shift]
        reserved_material_ids: set[int] = set()
        consume_results: list[dict[str, Any]] = []
        for account in ordered_accounts:
            if max_total_runs is not None and len(consume_results) >= max_total_runs:
                break
            if not self._account_requires_material_run(account):
                continue
            queue = self.db.list_material_queue_for_account(
                account.key,
                limit=max(limit_per_account * 5, 10),
            )
            processed = 0
            for material in queue:
                material_id = int(material["id"])
                if material_id in reserved_material_ids:
                    continue
                run = self.run_material_item_for_account(material_id, account.key)
                consume_results.append(
                    {
                        "material_item_id": material_id,
                        "title": material.get("title"),
                        "account_key": account.key,
                        "runs": [run],
                    }
                )
                reserved_material_ids.add(material_id)
                processed += 1
                if max_total_runs is not None and len(consume_results) >= max_total_runs:
                    break
                if processed >= limit_per_account:
                    break
        return consume_results
