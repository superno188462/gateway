"""Agent Gateway 向量数据库客户端（仅标准库，无需 LangChain）。

示例::

    store = GatewayVectorStore("http://127.0.0.1:8000/v1", "agw_xxx", "docs")
    store.create_collection("text-embedding-3-small")
    store.add_documents([{"id": "1", "content": "退款规则", "metadata": {"type": "faq"}}])
    hits = store.similarity_search("如何退款？", k=3)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class GatewayVectorStoreError(RuntimeError):
    """网关向量数据库请求失败。"""


@dataclass(slots=True)
class GatewayVectorStore:
    """以 collection 名称访问网关向量数据库。"""

    base_url: str
    api_key: str
    collection: str
    timeout: float = 30.0

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = Request(
            f"{self.base_url}/vector-stores{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except (HTTPError, URLError, TimeoutError) as error:
            detail = "网关向量数据库请求失败"
            if isinstance(error, HTTPError):
                try:
                    payload = json.loads(error.read())
                    detail = payload.get("detail", payload.get("error", detail))
                except (ValueError, OSError):
                    pass
            raise GatewayVectorStoreError(str(detail)) from error

    def create_collection(
        self,
        model: str,
        *,
        description: str | None = None,
        chunk_size: int = 1000,
        chunk_overlap: int = 120,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/collections",
            {
                "name": self.collection,
                "description": description,
                "model": model,
                "chunk_size": chunk_size,
                "chunk_overlap": chunk_overlap,
            },
        )

    def list_collections(self) -> list[dict[str, Any]]:
        return self._request("GET", "/collections")

    def delete_collection(self) -> None:
        """删除当前项目中的整个 collection 及其文档和向量。"""
        self._request("DELETE", f"/collections/{quote(self.collection, safe='')}")

    def add_documents(self, documents: list[dict[str, Any]]) -> dict[str, Any]:
        collection = quote(self.collection, safe="")
        return self._request("POST", f"/collections/{collection}/upsert", {"documents": documents})

    def delete(self, ids: list[str]) -> dict[str, Any]:
        collection = quote(self.collection, safe="")
        return self._request("POST", f"/collections/{collection}/delete", {"ids": ids})

    def similarity_search(
        self, query: str, *, k: int = 5, metadata: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        result = self._request(
            "POST",
            f"/collections/{quote(self.collection, safe='')}/query",
            {"query": query, "top_k": k, "metadata": metadata},
        )
        return result["results"]
