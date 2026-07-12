from __future__ import annotations

import json
from typing import Iterable

from .llm import StructuredLLM
from ..models.schemas import (
    Candidate,
    CandidateSet,
    ContentReview,
    MaterialAssessment,
    PostAnalysis,
    ProfileBatchSummary,
    StyleProfile,
)


def as_json(value: object) -> str:
    if isinstance(value, list):
        payload = [
            item.model_dump() if hasattr(item, "model_dump") else item for item in value
        ]
    elif hasattr(value, "model_dump"):
        payload = value.model_dump()
    else:
        payload = value
    return json.dumps(payload, ensure_ascii=False, indent=2)


class AnalysisAgent:
    def __init__(self, llm: StructuredLLM):
        self.llm = llm

    def analyze(self, content: str) -> PostAnalysis:
        return self.llm.invoke(
            system_prompt=(
                "你是加密市场短帖分析 Agent。只提取原文明示的信息，"
                "不得补充原文没有的理由、事实或判断。分析结果使用中文。"
            ),
            user_prompt=f"分析以下目标作者历史文章：\n\n{content}",
            response_model=PostAnalysis,
        )


class MaterialAssessmentAgent:
    def __init__(self, llm: StructuredLLM):
        self.llm = llm

    def assess(
        self,
        *,
        title: str | None,
        content: str,
        source_name: str | None = None,
        source_type: str | None = None,
    ) -> MaterialAssessment:
        return self.llm.invoke(
            system_prompt=(
                "你是 Binance Square 素材价值判断 Agent，只判断一条素材是否值得进入"
                "后续写作，不生成帖子。有效素材不要求明确看多或看空，但必须与加密市场、"
                "Web3 或重要 AI 行业动态相关，并包含可核实的具体事件、数据、公告、链上"
                "变化、监管变化或明确市场观点。拒绝广告、加群引流、返佣推广、空泛口号、"
                "只有标题没有事实、与主题无关的传统市场消息，以及信息密度过低的内容。"
                "不得补充素材没有的事实，不得把潜在影响写成确定结果。accepted=true 时"
                " relevance_score 必须至少 60，information_density 必须至少 50。"
            ),
            user_prompt=(
                f"来源名称：{source_name or '未知'}\n"
                f"来源类型：{source_type or '未知'}\n"
                f"标题：{title or ''}\n\n"
                f"正文：\n{content}"
            ),
            response_model=MaterialAssessment,
            retries=1,
        )


class StyleProfileAgent:
    BATCH_SIZE = 20

    def __init__(self, llm: StructuredLLM):
        self.llm = llm

    def _summarize_batch(self, analyses: list[dict]) -> ProfileBatchSummary:
        return self.llm.invoke(
            system_prompt=(
                "你是写作风格归纳 Agent。根据一批文章分析结果总结反复出现、"
                "可观察的写作模式。不要推断真实身份、人格或未出现的观点。"
            ),
            user_prompt=f"归纳这批历史文章分析：\n{as_json(analyses)}",
            response_model=ProfileBatchSummary,
        )

    def build(self, analyses: list[dict]) -> StyleProfile:
        if not analyses:
            raise ValueError("没有成功分析的历史文章，无法生成写作风格档案")
        batches = [
            analyses[index : index + self.BATCH_SIZE]
            for index in range(0, len(analyses), self.BATCH_SIZE)
        ]
        summaries = [self._summarize_batch(batch) for batch in batches]
        return self.llm.invoke(
            system_prompt=(
                "你是写作风格档案 Agent。合并所有分批摘要，生成稳定、具体、"
                "可用于改写的风格档案。只描述文章中可观察到的模式。"
            ),
            user_prompt=(
                f"历史文章总数：{len(analyses)}\n"
                f"分批摘要：\n{as_json(summaries)}"
            ),
            response_model=StyleProfile,
        )


class WriterAgent:
    def __init__(self, llm: StructuredLLM):
        self.llm = llm

    def generate(
        self,
        *,
        material: str,
        profile: StyleProfile,
        similar_analyses: list[dict],
    ) -> list[Candidate]:
        result = self.llm.invoke(
            system_prompt=(
                "你是 BN 广场短帖改写 Agent。任务是把外部素材改写成适合自动发布的"
                "加密或 AI 资讯短帖，而不是洗稿、研报或机械摘要。必须保留素材明确"
                "提供的主体、核心事实、数据、来源和不确定性，不得添加素材没有的"
                "新事实、价格目标、持仓经历或消息来源。素材明确给出多空方向时才"
                "保留方向；没有方向时禁止自行补做多、做空或涨跌预测。表达要短句、"
                "口语、有判断，但判断必须能够从素材事实直接推出，并用可能、值得"
                "关注、风险在于等表达保留边界。来源名称存在时应自然归因。不要写成"
                "触发点/需要留意/总结/首先其次这种报告结构。不得复制素材或历史"
                "文章的原句、句式和段落。正文提到币种代码时必须使用 $TOKEN 格式，"
                "例如 $BTC、$LAB；不要把 {future}(TOKENUSDT) 写进正文，该组件由发布"
                "边界补充。一次只生成一篇候选，candidate_index 固定为 1。"
            ),
            user_prompt=(
                f"外部素材：\n{material}\n\n"
                f"写作风格档案：\n{as_json(profile)}\n\n"
                "相似历史文章的结构化分析（只学习风格和判断逻辑）：\n"
                f"{as_json(similar_analyses)}\n\n"
                "输出要求：\n"
                "1. 只输出一条可直接发布的短帖正文。\n"
                "2. 交易观点素材可直接给币种和方向；资讯素材直接给事实及其影响。\n"
                "3. 不要解释自己在改写，不要写标题，不要写项目符号。\n"
                "4. 不得伪装成亲历者，不得虚构我买了、我卖了或独家获悉。\n"
                "5. 文末可以自然提醒风险，但不要变成投资建议声明。"
            ),
            response_model=CandidateSet,
        )
        by_index = {candidate.candidate_index: candidate for candidate in result.candidates}
        if 1 not in by_index:
            raise ValueError("Writer 必须生成 candidate_index 为 1 的候选稿")
        return [by_index[1]]

    def rewrite(
        self,
        *,
        material: str,
        profile: StyleProfile,
        candidate: Candidate,
        review: ContentReview,
    ) -> Candidate:
        return self.llm.invoke(
            system_prompt=(
                "你是 BN 广场短帖改写 Agent。根据审核反馈重写候选稿。"
                "不得添加素材没有的新事实，必须保留主体、核心事实、来源归因和口语"
                "节奏。只有素材明确存在方向时才保留方向，不得虚构交易动作或预测。"
                "正文提到币种代码时必须使用 $TOKEN 格式，不要自行输出 {future} 组件。"
                "必须解决全部审核问题，避免报告腔。"
            ),
            user_prompt=(
                f"外部素材：\n{material}\n\n"
                f"写作风格档案：\n{as_json(profile)}\n\n"
                f"待重写候选：\n{as_json(candidate)}\n\n"
                f"审核反馈：\n{as_json(review)}\n\n"
                f"保持 candidate_index={candidate.candidate_index}。"
                "输出一条可直接发布的短帖，不要写标题、项目符号或解释。"
            ),
            response_model=Candidate,
        )


class ContentReviewAgent:
    def __init__(self, llm: StructuredLLM):
        self.llm = llm

    def review(
        self,
        *,
        material: str,
        profile: StyleProfile,
        similar_analyses: list[dict],
        candidate: Candidate,
    ) -> ContentReview:
        review = self.llm.invoke(
            system_prompt=(
                "你是独立内容审核 Agent，只审核，不修改文章。逐项检查："
                "1. 是否捏造素材没有的新事实；2. 是否把预测写成事实；"
                "3. 是否偏离素材；4. 是否复制素材或历史表达；"
                "5. 是否符合写作风格档案；6. 是否像 BN 广场短帖而不是分析报告。"
                "评分必须严格。事实忠实度只有完全没有新增或扭曲事实时才能给 10 分。"
            ),
            user_prompt=(
                f"外部素材：\n{material}\n\n"
                f"风格档案：\n{as_json(profile)}\n\n"
                f"相似历史分析：\n{as_json(similar_analyses)}\n\n"
                f"候选稿：\n{as_json(candidate)}\n\n"
                "合格标准：事实忠实度=10；风格匹配度、原创度、表达质量均>=7；"
                "不能出现明显报告腔标题或项目符号。"
                "不合格时给出具体 issues 和可执行 rewrite_instructions。"
            ),
            response_model=ContentReview,
        )
        review.passed = review.meets_threshold()
        return review
