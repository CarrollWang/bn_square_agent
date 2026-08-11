from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from bn_square_agent.core.config import Settings
from bn_square_agent.models.schemas import PostAnalysis, StyleAnalysis, StyleProfile
from bn_square_agent.workflows.graphs import build_profile_graph


def make_analysis(summary: str = "BTC 短线看多") -> PostAnalysis:
    return PostAnalysis(
        token=["BTC"],
        event_type="行情观点",
        stance="看多",
        summary=summary,
        reasoning=["回踩后继续关注"],
        style=StyleAnalysis(
            tone="分析型",
            emoji=False,
            sentence_length="短",
            cta_strength="低",
        ),
    )


def make_profile() -> StyleProfile:
    return StyleProfile(
        persona="短线交易观察者",
        risk_level="中",
        favorite_topics=["BTC"],
        favorite_words=["关注"],
        opening_style="先给方向",
        tone="短句、直接",
        beliefs=["不追高"],
        structure_patterns=["结论后补风险"],
    )


class ProfileDatabaseTests(unittest.TestCase):
    def test_reference_posts_are_deduplicated_and_profile_stats_are_queryable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings = replace(
                Settings.from_env(),
                app_secret_key="",
                database_path=root / "agent.sqlite3",
                secret_key_path=root / "secret.key",
                chroma_path=root / "chroma",
            )
            db = settings.build_database()
            db.upsert_account(account_key="writer", name="Writer", cookie="session=test")

            first_id, first_created = db.add_source_post(
                account_key="writer",
                role="reference",
                title="第一篇",
                content="BTC 回踩后继续看多",
            )
            duplicate_id, duplicate_created = db.add_source_post(
                account_key="writer",
                role="reference",
                title="重复",
                content="BTC 回踩后继续看多",
            )
            failed_id, _ = db.add_source_post(
                account_key="writer",
                role="reference",
                content="SOL 短线观察",
            )
            self.assertTrue(first_created)
            self.assertFalse(duplicate_created)
            self.assertEqual(first_id, duplicate_id)

            db.save_analysis(first_id, make_analysis())
            db.save_analysis(failed_id, None, "analysis failed")
            stats = db.reference_post_stats("writer")
            self.assertEqual(stats["reference_count"], 2)
            self.assertEqual(stats["analysis_status"], {"failed": 1, "success": 1})

            profile = make_profile()
            db.save_profile(profile, source_count=1, account_key="writer")
            record = db.get_profile_record("writer")
            self.assertIsNotNone(record)
            self.assertEqual(record["source_count"], 1)
            self.assertEqual(record["profile"]["persona"], profile.persona)


class ProfileGraphTests(unittest.TestCase):
    def test_profile_graph_analyzes_builds_and_rebuilds_rag(self) -> None:
        analysis = make_analysis()
        profile = make_profile()

        class FakeDatabase:
            def __init__(self) -> None:
                self.saved_analysis = []
                self.saved_profile = None

            def pending_reference_posts(self, account_key):
                self.account_key = account_key
                return [{"id": 1, "content": "BTC 回踩后继续看多"}]

            def save_analysis(self, post_id, result, error=None):
                self.saved_analysis.append((post_id, result, error))

            def successful_analyses(self, account_key):
                return [{"post_id": 1, "author": "author", "analysis": analysis.model_dump()}]

            def save_profile(self, result, source_count, account_key):
                self.saved_profile = (result, source_count, account_key)

        class FakeAnalysisAgent:
            def analyze(self, content):
                self.content = content
                return analysis

        class FakeProfileAgent:
            def build(self, analyses):
                self.analyses = analyses
                return profile

        class FakeRag:
            def rebuild(self, records, account_key):
                self.rebuilt = (records, account_key)

        db = FakeDatabase()
        rag = FakeRag()
        result = build_profile_graph(
            db,
            FakeAnalysisAgent(),
            FakeProfileAgent(),
            rag,
        ).invoke({"account_key": "writer"})

        self.assertEqual(result["analyzed_count"], 1)
        self.assertEqual(result["failed_count"], 0)
        self.assertEqual(result["source_count"], 1)
        self.assertEqual(db.saved_profile[2], "writer")
        self.assertEqual(rag.rebuilt[1], "writer")


class ProfileApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            from fastapi.testclient import TestClient
            from bn_square_agent.webapp import app
        except ModuleNotFoundError as exc:
            raise unittest.SkipTest(f"runtime dependency is not installed: {exc}")
        cls.client = TestClient(app)

    def test_reference_import_counts_duplicates(self) -> None:
        class FakeDatabase:
            def __init__(self) -> None:
                self.contents = set()

            def list_accounts(self, include_disabled=False):
                return [{"account_key": "writer"}]

            def add_source_post(self, **values):
                content = values["content"]
                created = content not in self.contents
                self.contents.add(content)
                return 1, created

        db = FakeDatabase()
        with patch("bn_square_agent.webapp.get_db", return_value=db):
            response = self.client.post(
                "/api/accounts/writer/reference-posts",
                json={
                    "posts": [
                        {"content": "第一篇"},
                        {"content": "第一篇"},
                        {"content": "第二篇"},
                    ]
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"added": 2, "duplicated": 1})

    def test_profile_build_uses_account_lock_and_returns_graph_counts(self) -> None:
        class FakeDatabase:
            def __init__(self) -> None:
                self.released = False

            def list_accounts(self, include_disabled=False):
                return [{"account_key": "writer"}]

            def try_acquire_job_lock(self, job_name, *, owner_id, lease_seconds):
                self.job_name = job_name
                self.owner_id = owner_id
                return True

            def release_job_lock(self, job_name, *, owner_id):
                self.released = job_name == self.job_name and owner_id == self.owner_id

        class FakeGraph:
            def invoke(self, state):
                self.state = state
                return {"analyzed_count": 2, "failed_count": 1, "source_count": 4}

        db = FakeDatabase()
        graph = FakeGraph()
        with patch("bn_square_agent.webapp.get_db", return_value=db), patch(
            "bn_square_agent.webapp.build_services",
            return_value=SimpleNamespace(profile_graph=graph),
        ):
            response = self.client.post("/api/accounts/writer/profile/build")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"analyzed_count": 2, "failed_count": 1, "source_count": 4},
        )
        self.assertEqual(graph.state, {"account_key": "writer"})
        self.assertEqual(db.job_name, "profile_build:writer")
        self.assertTrue(db.released)

    def test_unknown_publish_run_can_be_resolved(self) -> None:
        class FakeDatabase:
            def resolve_unknown_material_run(self, run_id, *, resolution):
                self.call = (run_id, resolution)
                return {
                    "status": resolution,
                    "generated_publish_status": (
                        "published" if resolution == "published" else "failed_retryable"
                    ),
                    "resolution": resolution,
                    "resolved_at": "2026-08-11T10:00:00+00:00",
                    "changed": True,
                }

        db = FakeDatabase()
        with patch("bn_square_agent.webapp.get_db", return_value=db):
            response = self.client.post(
                "/api/history/runs/12/resolve",
                json={"resolution": "failed"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(db.call, (12, "failed"))
        self.assertEqual(response.json()["generated_publish_status"], "failed_retryable")


if __name__ == "__main__":
    unittest.main()
