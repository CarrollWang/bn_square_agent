from __future__ import annotations

import re
from dataclasses import asdict, dataclass, replace

from ..models.schemas import MaterialAssessment


TOKEN_RE = re.compile(
    r"(?:\{future\}\(([A-Z0-9]{2,20}USDT)\))|\$([A-Z][A-Z0-9]{1,20})|([A-Z0-9]{2,20}USDT)"
)
BARE_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9$])([A-Z][A-Z0-9]{1,19})(?![A-Za-z0-9])")

TOKEN_ALIASES = (
    ("BTC", ("BTC", "比特币", "Bitcoin")),
    ("ETH", ("ETH", "以太坊", "Ethereum")),
    ("BNB", ("BNB", "币安币", "BNB Chain")),
    ("SOL", ("SOL", "Solana")),
    ("XRP", ("XRP", "Ripple")),
    ("DOGE", ("DOGE", "Dogecoin")),
    ("ADA", ("ADA", "Cardano")),
    ("ZEC", ("ZEC", "Zcash")),
    ("ONDO", ("ONDO", "Ondo")),
    ("PYTH", ("PYTH", "Pyth")),
    ("UNI", ("UNI", "Uniswap")),
    ("LINK", ("LINK", "Chainlink")),
    ("MANTA", ("MANTA", "Manta")),
)


@dataclass(frozen=True)
class MaterialTag:
    accepted: bool
    token: str | None
    symbol: str | None
    direction: str
    topics: list[str]
    has_chart_symbol: bool
    reasons: list[str]
    strategy: str
    decision_source: str
    semantic_review: dict | None

    def to_dict(self) -> dict:
        return asdict(self)


class MaterialTagger:
    STRATEGY = "hybrid_semantic_v3"
    MIN_EDITORIAL_LENGTH = 24

    CRYPTO_PATTERNS = (
        "加密",
        "区块链",
        "链上",
        "币安",
        "交易所",
        "代币",
        "稳定币",
        "钱包",
        "主网",
        "矿工",
        "矿池",
        "挖矿",
        "巨鲸",
        "空投",
        "合约",
        "现货",
        "去中心化",
        "web3",
        "defi",
        "dex",
        "binance",
        "ethereum",
        "bitcoin",
        "solana",
    )
    AI_PATTERNS = (
        "人工智能",
        "大模型",
        "智能体",
        "生成式 ai",
        "openai",
        "anthropic",
        "chatgpt",
        "claude",
        "gemini",
        "deepseek",
        "智谱",
    )
    PROMOTIONAL_PATTERNS = (
        "加群",
        "进群",
        "私聊",
        "邀请码",
        "注册链接",
        "返佣",
        "带单",
        "跟单",
        "稳赚",
        "保证收益",
        "保本收益",
    )

    def __init__(
        self,
        *,
        valid_futures_symbols: set[str] | frozenset[str] | None = None,
    ) -> None:
        self.valid_futures_symbols = (
            frozenset(symbol.upper() for symbol in valid_futures_symbols)
            if valid_futures_symbols is not None
            else None
        )

    def tag(self, *, title: str | None, content: str) -> MaterialTag:
        text = f"{title or ''}\n{content}".strip()
        token, symbol = self._extract_token((title or "").strip())
        if not symbol:
            token, symbol = self._extract_token(content)
        direction, has_conflict = self._extract_direction(text)
        topics = self._extract_topics(text, token=token)
        reasons: list[str] = []

        if not text:
            reasons.append("empty_content")
        if not topics:
            reasons.append("missing_relevant_topic")
        if any(pattern in text for pattern in self.PROMOTIONAL_PATTERNS):
            reasons.append("promotional_content")
        compact_length = len(re.sub(r"\s+", "", text))
        directional_brief = bool(
            token and direction in {"long", "short"} and compact_length >= 12
        )
        if compact_length < self.MIN_EDITORIAL_LENGTH and not directional_brief:
            reasons.append("content_too_short")
        if has_conflict:
            reasons.append("conflicting_direction")
        elif direction == "unknown":
            reasons.append("direction_not_explicit")

        accepted = bool(
            text
            and topics
            and (compact_length >= self.MIN_EDITORIAL_LENGTH or directional_brief)
            and "promotional_content" not in reasons
        )
        if accepted:
            reasons.append("ready_for_editorial_consume")

        return MaterialTag(
            accepted=accepted,
            token=token,
            symbol=symbol,
            direction=direction,
            topics=topics,
            has_chart_symbol=bool(symbol),
            reasons=reasons,
            strategy=self.STRATEGY,
            decision_source="rules",
            semantic_review=None,
        )

    @staticmethod
    def needs_semantic_review(tag: MaterialTag) -> bool:
        if any(
            reason in tag.reasons
            for reason in ("empty_content", "content_too_short", "promotional_content")
        ):
            return False
        return not (
            tag.accepted
            and tag.symbol
            and tag.direction in {"long", "short"}
            and "conflicting_direction" not in tag.reasons
        )

    @staticmethod
    def apply_semantic_assessment(
        tag: MaterialTag,
        assessment: MaterialAssessment,
    ) -> MaterialTag:
        accepted = bool(
            assessment.accepted
            and assessment.relevance_score >= 60
            and assessment.information_density >= 50
        )
        reasons = [
            reason
            for reason in tag.reasons
            if reason != "ready_for_editorial_consume"
        ]
        reasons.append(
            "semantic_review_accepted" if accepted else "semantic_review_rejected"
        )
        return replace(
            tag,
            accepted=accepted,
            reasons=reasons,
            decision_source="llm",
            semantic_review=assessment.model_dump(),
        )

    @staticmethod
    def apply_semantic_fallback(tag: MaterialTag, error: Exception) -> MaterialTag:
        return replace(
            tag,
            decision_source="rules_fallback",
            semantic_review={"error_type": type(error).__name__},
        )

    def _extract_topics(self, text: str, *, token: str | None) -> list[str]:
        lowered = text.lower()
        topics: list[str] = []
        if token or any(pattern in lowered for pattern in self.CRYPTO_PATTERNS):
            topics.append("crypto")
        has_ai = any(pattern in lowered for pattern in self.AI_PATTERNS) or bool(
            re.search(r"(?<![A-Za-z])AI(?![A-Za-z])", text, re.I)
            or re.search(r"(?<![A-Za-z])LLM(?![A-Za-z])", text, re.I)
            or re.search(r"(?<![A-Za-z])Agent(?:s)?(?![A-Za-z])", text, re.I)
        )
        if has_ai:
            topics.append("ai")
        return topics

    def _extract_token(self, text: str) -> tuple[str | None, str | None]:
        for match in TOKEN_RE.finditer(text):
            futures_symbol, cash_token, plain_symbol = match.groups()
            if futures_symbol and self._is_valid_symbol(futures_symbol):
                return futures_symbol.removesuffix("USDT"), futures_symbol
            if plain_symbol and self._is_valid_symbol(plain_symbol):
                return plain_symbol.removesuffix("USDT"), plain_symbol
            if cash_token:
                token = cash_token.upper()
                symbol = f"{token}USDT"
                if self._is_valid_symbol(symbol):
                    return token, symbol
        alias_match = self._find_first_alias(text)
        if alias_match and self._is_valid_symbol(f"{alias_match}USDT"):
            return alias_match, f"{alias_match}USDT"
        if self.valid_futures_symbols is not None:
            for match in BARE_TOKEN_RE.finditer(text):
                token = match.group(1).upper()
                symbol = f"{token}USDT"
                if self._is_valid_symbol(symbol):
                    return token, symbol
        return None, None

    def _is_valid_symbol(self, symbol: str) -> bool:
        return (
            self.valid_futures_symbols is None
            or symbol.upper() in self.valid_futures_symbols
        )

    @staticmethod
    def _alias_index(text: str, alias: str) -> int | None:
        if re.fullmatch(r"[A-Za-z0-9]+", alias):
            match = re.search(
                rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])",
                text,
                re.I,
            )
            return match.start() if match else None
        index = text.find(alias)
        return index if index >= 0 else None

    def _find_first_alias(self, text: str) -> str | None:
        matches: list[tuple[int, str]] = []
        for token, aliases in TOKEN_ALIASES:
            indexes = [
                index
                for alias in aliases
                if (index := self._alias_index(text, alias)) is not None
            ]
            if indexes:
                matches.append((min(indexes), token))
        if not matches:
            return None
        return min(matches, key=lambda item: item[0])[1]

    def _extract_direction(self, text: str) -> tuple[str, bool]:
        short_patterns = (
            "看空",
            "做空",
            "开空",
            "空进",
            "空！",
            "空!",
            "空了",
            "继续空",
            "偏空",
            "转空",
            "空头",
            "空单",
            "看跌",
            "下看",
            "利空",
            "逢高空",
            "反弹空",
            "反弹做空",
        )
        long_patterns = (
            "看多",
            "做多",
            "开多",
            "接多",
            "抄底",
            "多！",
            "多!",
            "多进",
            "继续多",
            "偏多",
            "转多",
            "多头",
            "多单",
            "看涨",
            "上看",
            "利多",
            "逢低多",
            "回踩多",
            "回踩做多",
        )
        has_short = any(word in text for word in short_patterns) or bool(
            re.search(r"\b(?:short|bearish)\b", text, re.I)
        )
        has_long = any(word in text for word in long_patterns) or bool(
            re.search(r"\b(?:long|bullish)\b", text, re.I)
        )
        if has_short and has_long:
            return "unknown", True
        if has_short:
            return "short", False
        if has_long:
            return "long", False
        return "unknown", False
