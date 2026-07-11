from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from ..ai.agents import AnalysisAgent, ContentReviewAgent, StyleProfileAgent, WriterAgent
from ..storage.database import Database
from ..knowledge.style_rag import StyleRAG
from ..models.schemas import Candidate, ContentReview, StyleProfile


class ProfileState(TypedDict, total=False):
    account_key: str
    analyzed_count: int
    failed_count: int
    profile: StyleProfile
    source_count: int


class ContentState(TypedDict, total=False):
    account_key: str
    material_id: int
    content: str
    title: str | None
    url: str | None
    source_name: str | None
    profile: StyleProfile
    similar_analyses: list[dict[str, Any]]
    candidates: list[Candidate]
    originals: dict[int, str]
    reviews: dict[int, ContentReview]
    rewrite_counts: dict[int, int]
    generated_ids: list[int]
    approved_generated_id: int | None


def default_style_profile(account_key: str) -> StyleProfile:
    return StyleProfile.model_construct(
        persona=f"{account_key} auto operator",
        risk_level="中",
        favorite_topics=["加密市场", "行业事件", "AI 与 Web3"],
        favorite_words=["关注", "可能", "继续", "位置", "风险"],
        opening_style="直接点出关键事实或代币观点，开头不给空泛铺垫。",
        tone="短句、直接、有运营感，但避免承诺收益。",
        beliefs=[
            "只基于素材已有信息改写，不新增未经素材支持的事实。",
            "观点可以明确，但预测必须保留不确定表达。",
            "优先保留主体、关键事实、来源、明确方向和风险提示。",
            "素材没有交易方向时不自行制造多空结论。",
        ],
        structure_patterns=[
            "先说结论，再解释素材中的触发点。",
            "最后给出关注点或风险提醒。",
        ],
    )


def build_profile_graph(
    db: Database,
    analysis_agent: AnalysisAgent,
    profile_agent: StyleProfileAgent,
    rag: StyleRAG,
):
    def analyze_posts(state: ProfileState) -> ProfileState:
        account_key = state.get("account_key", "default")
        success = 0
        failed = 0
        for post in db.pending_reference_posts(account_key):
            try:
                analysis = analysis_agent.analyze(post["content"])
                db.save_analysis(post["id"], analysis)
                success += 1
            except Exception as exc:
                db.save_analysis(post["id"], None, str(exc))
                failed += 1
        return {"analyzed_count": success, "failed_count": failed}

    def build_profile(state: ProfileState) -> ProfileState:
        account_key = state.get("account_key", "default")
        records = db.successful_analyses(account_key)
        profile = profile_agent.build([record["analysis"] for record in records])
        db.save_profile(profile, len(records), account_key)
        return {"profile": profile, "source_count": len(records)}

    def rebuild_rag(state: ProfileState) -> ProfileState:
        account_key = state.get("account_key", "default")
        rag.rebuild(db.successful_analyses(account_key), account_key)
        return {}

    graph = StateGraph(ProfileState)
    graph.add_node("analyze_posts", analyze_posts)
    graph.add_node("build_profile", build_profile)
    graph.add_node("rebuild_rag", rebuild_rag)
    graph.add_edge(START, "analyze_posts")
    graph.add_edge("analyze_posts", "build_profile")
    graph.add_edge("build_profile", "rebuild_rag")
    graph.add_edge("rebuild_rag", END)
    return graph.compile()


def build_content_graph(
    db: Database,
    rag: StyleRAG,
    writer: WriterAgent,
    reviewer: ContentReviewAgent,
    *,
    max_rewrites: int = 2,
):
    def material_context(state: ContentState) -> str:
        parts = []
        if state.get("source_name"):
            parts.append(f"来源：{state['source_name']}")
        if state.get("title"):
            parts.append(f"标题：{state['title']}")
        parts.append(f"正文：{state['content']}")
        if state.get("url"):
            parts.append(f"原文链接：{state['url']}")
        return "\n".join(parts)

    def prepare(state: ContentState) -> ContentState:
        account_key = state.get("account_key", "default")
        profile = db.get_profile(account_key) or default_style_profile(account_key)
        if not profile:
            raise ValueError(f"请先为账号 {account_key} 导入历史文章并生成写作风格档案")
        material_id = state.get("material_id")
        if material_id is None:
            material_id, _ = db.add_source_post(
                content=state["content"],
                title=state.get("title"),
                url=state.get("url"),
                role="material",
                account_key=account_key,
            )
        similar = rag.search(state["content"], account_key=account_key, top_k=5)
        analysis_by_id = {
            record["post_id"]: record["analysis"]
            for record in db.successful_analyses(account_key)
        }
        similar_analyses = [
            analysis_by_id[item["post_id"]]
            for item in similar
            if item["post_id"] in analysis_by_id
        ]
        return {
            "material_id": material_id,
            "account_key": account_key,
            "profile": profile,
            "similar_analyses": similar_analyses,
        }

    def generate(state: ContentState) -> ContentState:
        candidates = writer.generate(
            material=material_context(state),
            profile=state["profile"],
            similar_analyses=state["similar_analyses"],
        )
        return {
            "candidates": candidates,
            "originals": {
                candidate.candidate_index: candidate.content for candidate in candidates
            },
            "rewrite_counts": {
                candidate.candidate_index: 0 for candidate in candidates
            },
        }

    def review(state: ContentState) -> ContentState:
        reviews = {}
        for candidate in state["candidates"]:
            reviews[candidate.candidate_index] = reviewer.review(
                material=material_context(state),
                profile=state["profile"],
                similar_analyses=state["similar_analyses"],
                candidate=candidate,
            )
        return {"reviews": reviews}

    def route_after_review(state: ContentState) -> str:
        retryable = any(
            not review.passed
            and state["rewrite_counts"][index] < max_rewrites
            for index, review in state["reviews"].items()
        )
        return "rewrite" if retryable else "save"

    def rewrite_failed(state: ContentState) -> ContentState:
        updated = []
        counts = dict(state["rewrite_counts"])
        for candidate in state["candidates"]:
            review = state["reviews"][candidate.candidate_index]
            if not review.passed and counts[candidate.candidate_index] < max_rewrites:
                candidate = writer.rewrite(
                    material=material_context(state),
                    profile=state["profile"],
                    candidate=candidate,
                    review=review,
                )
                counts[candidate.candidate_index] += 1
            updated.append(candidate)
        return {"candidates": updated, "rewrite_counts": counts}

    def save(state: ContentState) -> ContentState:
        ids = []
        passed_candidates = [
            candidate
            for candidate in state["candidates"]
            if state["reviews"][candidate.candidate_index].passed
        ]
        approved_index = None
        if passed_candidates:
            best = max(
                passed_candidates,
                key=lambda candidate: (
                    state["reviews"][candidate.candidate_index].scores.factual_fidelity,
                    state["reviews"][candidate.candidate_index].scores.style_match,
                    state["reviews"][candidate.candidate_index].scores.originality,
                    state["reviews"][candidate.candidate_index].scores.expression_quality,
                    -state["rewrite_counts"][candidate.candidate_index],
                    -candidate.candidate_index,
                ),
            )
            approved_index = best.candidate_index
        approved_generated_id = None
        for candidate in state["candidates"]:
            review = state["reviews"][candidate.candidate_index]
            if not review.passed:
                status = "failed"
            elif candidate.candidate_index == approved_index:
                status = "approved"
            else:
                status = "rejected"
            generated_id = db.save_generated(
                source_post_id=state["material_id"],
                candidate_index=candidate.candidate_index,
                original_content=state["originals"][candidate.candidate_index],
                content=candidate.content,
                status=status,
                review=review,
                rewrite_count=state["rewrite_counts"][candidate.candidate_index],
                account_key=state.get("account_key", "default"),
            )
            ids.append(generated_id)
            if status == "approved":
                approved_generated_id = generated_id
        return {"generated_ids": ids, "approved_generated_id": approved_generated_id}

    graph = StateGraph(ContentState)
    graph.add_node("prepare", prepare)
    graph.add_node("generate", generate)
    graph.add_node("review", review)
    graph.add_node("rewrite", rewrite_failed)
    graph.add_node("save", save)
    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "generate")
    graph.add_edge("generate", "review")
    graph.add_conditional_edges(
        "review", route_after_review, {"rewrite": "rewrite", "save": "save"}
    )
    graph.add_edge("rewrite", "review")
    graph.add_edge("save", END)
    return graph.compile()
