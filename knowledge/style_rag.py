from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from openai import OpenAI

from ..core.config import Settings, add_no_proxy_host


class OpenAICompatibleEmbeddings:
    def __init__(self, *, api_key: str, base_url: str, model: str):
        self.model = model
        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=45.0,
            max_retries=1,
            http_client=httpx.Client(timeout=45.0, trust_env=False),
        )

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        response = self.client.embeddings.create(model=self.model, input=texts)
        return [item.embedding for item in sorted(response.data, key=lambda item: item.index)]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


def create_embeddings(settings: Settings):
    settings.validate_for_rag()
    if settings.embedding_provider == "dashscope":
        add_no_proxy_host("dashscope.aliyuncs.com")
        from langchain_community.embeddings import DashScopeEmbeddings

        return DashScopeEmbeddings(
            model=settings.embedding_model,
            dashscope_api_key=settings.resolved_embedding_api_key(),
        )

    base_url = settings.resolved_embedding_base_url()
    host = urlsplit(base_url).hostname
    if host:
        add_no_proxy_host(host)
    return OpenAICompatibleEmbeddings(
        api_key=settings.resolved_embedding_api_key(),
        base_url=base_url,
        model=settings.embedding_model,
    )


class StyleRAG:
    COLLECTION_PREFIX = "reference_post_summaries"

    def __init__(self, settings: Settings):
        settings.validate_for_rag()
        import chromadb

        Path(settings.chroma_path).mkdir(parents=True, exist_ok=True)
        self.client = chromadb.PersistentClient(path=str(settings.chroma_path))
        self.embeddings = create_embeddings(settings)
        identity = hashlib.sha256(
            (
                f"{settings.embedding_provider}|{settings.embedding_model}|"
                f"{settings.resolved_embedding_base_url()}"
            ).encode("utf-8")
        ).hexdigest()[:12]
        self.collection = self.client.get_or_create_collection(
            f"{self.COLLECTION_PREFIX}_{identity}"
        )

    def rebuild(self, records: list[dict[str, Any]], account_key: str = "default") -> None:
        if not records:
            existing = self.collection.get(where={"account_key": account_key})
            if existing.get("ids"):
                self.collection.delete(ids=existing["ids"])
            return
        documents = [record["analysis"]["summary"] for record in records]
        vectors = self.embeddings.embed_documents(documents)
        metadatas = []
        for record in records:
            analysis = record["analysis"]
            metadatas.append(
                {
                    "account_key": account_key,
                    "author": record.get("author") or "",
                    "token": ",".join(analysis.get("token", [])),
                    "event_type": analysis.get("event_type", ""),
                    "stance": analysis.get("stance", ""),
                }
            )
        existing = self.collection.get(where={"account_key": account_key})
        self.collection.upsert(
            ids=[f"{account_key}:{record['post_id']}" for record in records],
            documents=documents,
            embeddings=vectors,
            metadatas=metadatas,
        )
        new_ids = {f"{account_key}:{record['post_id']}" for record in records}
        stale_ids = [
            item_id
            for item_id in existing.get("ids") or []
            if item_id not in new_ids
        ]
        if stale_ids:
            self.collection.delete(ids=stale_ids)

    def search(
        self,
        query: str,
        *,
        account_key: str = "default",
        top_k: int = 5,
        where: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if self.collection.count() == 0:
            return []
        query_where = {"account_key": account_key}
        if where:
            query_where.update(where)
        query_vector = self.embeddings.embed_query(query)
        result = self.collection.query(
            query_embeddings=[query_vector],
            n_results=min(top_k, self.collection.count()),
            where=query_where,
            include=["documents", "metadatas", "distances"],
        )
        items = []
        for index, document in enumerate(result["documents"][0]):
            items.append(
                {
                    "post_id": int(str(result["ids"][0][index]).rsplit(":", 1)[-1]),
                    "summary": document,
                    "metadata": result["metadatas"][0][index],
                    "distance": result["distances"][0][index],
                }
            )
        return items
