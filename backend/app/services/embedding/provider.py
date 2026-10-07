"""OpenAI 兼容 Embedding Provider，按路由组故障切换。"""

from dataclasses import dataclass

import httpx

from app.services.embedding.configuration import EmbeddingConfigurationService


class EmbeddingProviderError(RuntimeError):
    """上游调用失败，公开 message 不含凭据或原始响应正文。"""

    def __init__(self, code: str, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    response: dict[str, object]
    input_tokens: int
    dimensions: int


class ConfiguredEmbeddingProvider:
    """发送 OpenAI ``POST /embeddings`` 请求并验证标准响应结构。"""

    def __init__(
        self,
        configuration: EmbeddingConfigurationService | None,
        http_client: httpx.AsyncClient,
    ) -> None:
        self._configuration = configuration
        self._http_client = http_client

    async def embed(
        self,
        model: str,
        inputs: list[str],
        parameters: dict[str, object] | None = None,
    ) -> EmbeddingResult:
        if self._configuration is None:
            raise EmbeddingProviderError(
                "provider_configuration_missing", "Embedding 上游配置不可用", 503
            )
        routes = await self._configuration.resolve_model_pool(model)
        if not routes:
            raise EmbeddingProviderError(
                "model_route_not_found", "没有可用的 Embedding 模型路由", 404
            )
        last_status: int | None = None
        for route in routes:
            payload: dict[str, object] = {"model": route.upstream_model, "input": inputs}
            payload.update(parameters or {})
            try:
                response = await self._http_client.post(
                    route.base_url.rstrip("/") + "/embeddings",
                    headers={"Authorization": f"Bearer {route.api_key}"},
                    json=payload,
                )
                response.raise_for_status()
                body = response.json()
                data = body.get("data") if isinstance(body, dict) else None
                if not isinstance(data, list) or len(data) != len(inputs):
                    raise EmbeddingProviderError(
                        "invalid_upstream_response", "Embedding 上游返回的向量数量不正确"
                    )
                indexed: list[tuple[int, list[float]]] = []
                for item in data:
                    if not isinstance(item, dict) or not isinstance(item.get("embedding"), list):
                        raise EmbeddingProviderError(
                            "invalid_upstream_response", "Embedding 上游返回了无效向量"
                        )
                    vector = item["embedding"]
                    if not vector or not all(
                        isinstance(value, (int, float)) and not isinstance(value, bool)
                        for value in vector
                    ):
                        raise EmbeddingProviderError(
                            "invalid_upstream_response", "Embedding 上游返回了空或非数值向量"
                        )
                    index = item.get("index", len(indexed))
                    if not isinstance(index, int) or index < 0 or index >= len(inputs):
                        raise EmbeddingProviderError(
                            "invalid_upstream_response", "Embedding 上游返回了无效向量序号"
                        )
                    indexed.append((index, [float(value) for value in vector]))
                indexed.sort(key=lambda item: item[0])
                dimensions = len(indexed[0][1])
                if any(len(vector) != dimensions for _, vector in indexed):
                    raise EmbeddingProviderError(
                        "invalid_upstream_response", "同一响应中的向量维度不一致"
                    )
                if len({index for index, _ in indexed}) != len(inputs):
                    raise EmbeddingProviderError(
                        "invalid_upstream_response", "Embedding 上游重复或遗漏了向量序号"
                    )
                usage = body.get("usage", {}) if isinstance(body, dict) else {}
                token_count = usage.get("prompt_tokens", 0) if isinstance(usage, dict) else 0
                if not isinstance(token_count, int) or token_count < 0:
                    token_count = 0
                normalized = dict(body)
                normalized["model"] = model
                normalized["data"] = [
                    {"object": "embedding", "index": index, "embedding": vector}
                    for index, vector in indexed
                ]
                return EmbeddingResult(normalized, token_count, dimensions)
            except httpx.HTTPStatusError as error:
                last_status = error.response.status_code
                if last_status < 400 or last_status in {400, 401, 403, 404, 408, 409, 422, 429}:
                    continue
            except httpx.RequestError:
                continue
        status = last_status if last_status is not None else 502
        raise EmbeddingProviderError(
            "upstream_embedding_failed",
            "Embedding 上游调用失败，请检查连接配置、模型名称和 API Key",
            502 if status >= 400 else status,
        )
