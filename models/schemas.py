from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


Stance = Literal["看多", "看空", "中立", "多空混合", "不确定"]
Tone = Literal["激进", "冷静", "分析型", "幽默", "不确定"]
SentenceLength = Literal["短", "中等", "长"]
CtaStrength = Literal["无", "低", "中", "高"]


class StyleAnalysis(BaseModel):
    tone: Tone
    emoji: bool
    sentence_length: SentenceLength
    cta_strength: CtaStrength


class PostAnalysis(BaseModel):
    token: list[str] = Field(default_factory=list)
    event_type: str
    stance: Stance
    summary: str
    reasoning: list[str] = Field(default_factory=list)
    style: StyleAnalysis


class StyleProfile(BaseModel):
    persona: str
    risk_level: Literal["低", "中", "高", "不确定"]
    favorite_topics: list[str] = Field(default_factory=list)
    favorite_words: list[str] = Field(default_factory=list)
    opening_style: str
    tone: str
    beliefs: list[str] = Field(default_factory=list)
    structure_patterns: list[str] = Field(default_factory=list)


class ProfileBatchSummary(BaseModel):
    recurring_topics: list[str] = Field(default_factory=list)
    recurring_words: list[str] = Field(default_factory=list)
    opening_patterns: list[str] = Field(default_factory=list)
    tone_patterns: list[str] = Field(default_factory=list)
    beliefs: list[str] = Field(default_factory=list)
    structure_patterns: list[str] = Field(default_factory=list)


class Candidate(BaseModel):
    candidate_index: int = Field(ge=1, le=3)
    content: str


class CandidateSet(BaseModel):
    candidates: list[Candidate] = Field(min_length=1, max_length=3)


class ReviewScores(BaseModel):
    factual_fidelity: int = Field(ge=0, le=10)
    style_match: int = Field(ge=0, le=10)
    originality: int = Field(ge=0, le=10)
    expression_quality: int = Field(ge=0, le=10)


class ContentReview(BaseModel):
    passed: bool
    scores: ReviewScores
    issues: list[str] = Field(default_factory=list)
    rewrite_instructions: list[str] = Field(default_factory=list)

    def meets_threshold(self) -> bool:
        return (
            self.scores.factual_fidelity == 10
            and self.scores.style_match >= 7
            and self.scores.originality >= 7
            and self.scores.expression_quality >= 7
        )


class MaterialAssessment(BaseModel):
    accepted: bool
    category: Literal[
        "trading_signal",
        "market_news",
        "project_update",
        "exchange_announcement",
        "onchain_data",
        "regulation",
        "ai_industry",
        "general",
    ]
    relevance_score: int = Field(ge=0, le=100)
    information_density: int = Field(ge=0, le=100)
    time_sensitivity: Literal["high", "medium", "low", "unknown"]
    impact: Literal["positive", "negative", "neutral", "mixed", "unclear"]
    reason: str
