from __future__ import annotations

from pathlib import Path
from typing import Any

from ..core.config import Settings, add_no_proxy_host


class StyleRAG:
    COLLECTION_NAME = "reference_post_summaries"

    def __init__(self, settings: Settings):
        settings.validate_for_rag()
        add_no_proxy_host("dashscope.aliyuncs.com")
        import chromadb
        from langchain_community.embeddings import DashScopeEmbeddings

        Path(settings.chroma_path).mkdir(parents=True, exist_ok=True)
        self.client = chromadb.PersistentClient(path=str(settings.chroma_path))
        self.embeddings = DashScopeEmbeddings(
            model=settings.dashscope_embedding_model,
            dashscope_api_key=settings.dashscope_api_key,
        )
        self.collection = self.client.get_or_create_collection(self.COLLECTION_NAME)

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
