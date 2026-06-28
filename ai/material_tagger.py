from __future__ import annotations

import re
from dataclasses import dataclass, asdict


TOKEN_RE = re.compile(
    r"(?:\{future\}\(([A-Z0-9]{2,20}USDT)\))|\$([A-Z][A-Z0-9]{1,20})|([A-Z0-9]{2,20}USDT)"
)


@dataclass(frozen=True)
class MaterialTag:
    accepted: bool
    token: str | None
    symbol: str | None
    direction: str
    has_chart_symbol: bool
    reasons: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


class MaterialTagger:
    def tag(self, *, title: str | None, content: str) -> MaterialTag:
        text = f"{title or ''}\n{content}".strip()
        token, symbol = self._extract_token(text)
        direction = self._extract_direction(text)
        reasons: list[str] = []

        if not text:
            reasons.append("empty_content")
        if not token:
            reasons.append("missing_token")
        if direction == "unknown":
            reasons.append("missing_direction")

        accepted = bool(text and token and direction != "unknown")
        if accepted:
            reasons.append("ready_for_auto_consume")

        return MaterialTag(
            accepted=accepted,
            token=token,
            symbol=symbol,
            direction=direction,
            has_chart_symbol=bool(symbol),
            reasons=reasons,
        )

    def _extract_token(self, text: str) -> tuple[str | None, str | None]:
        for match in TOKEN_RE.finditer(text):
            futures_symbol, cash_token, plain_symbol = match.groups()
            if futures_symbol:
                return futures_symbol.removesuffix("USDT"), futures_symbol
            if plain_symbol:
                return plain_symbol.removesuffix("USDT"), plain_symbol
            if cash_token:
                token = cash_token.upper()
                return token, f"{token}USDT"
        return None, None

    def _extract_direction(self, text: str) -> str:
        short_patterns = (
            "看空",
            "做空",
            "开空",
            "空进",
            "空！",
            "空!",
            "空了",
            "继续空",
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
        )
        if any(word in text for word in short_patterns):
            return "short"
        if any(word in text for word in long_patterns):
            return "long"
        return "unknown"
