"""OpenAI 兼容 Embedding Provider，按路由组故障切换。"""

import asyncio
from dataclasses import dataclass

import httpx

from app.services.embedding.configuration import (
    EmbeddingConfigurationService,
    ResolvedEmbeddingModel,
)


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
        # 火山多模态接口每次返回一条向量；文本批次需要并发拆分请求，
        # 共享信号量避免多个批次同时到达时无限放大上游并发。
        self._volc_text_semaphore = asyncio.Semaphore(8)

    async def embed(
        self,
        model: str,
        inputs: list[str | dict[str, object]],
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
            try:
                if route.route_prefix == "volc":
                    indexed, token_count = await self._embed_volc(route, inputs, parameters or {})
                else:
                    if any(not isinstance(item, str) for item in inputs):
                        raise EmbeddingProviderError(
                            "unsupported_embedding_modality",
                            "图片和视频输入当前仅支持 volc/doubao-embedding-vision 模型",
                            422,
                        )
                    payload: dict[str, object] = {
                        "model": route.upstream_model,
                        "input": inputs,
                    }
                    payload.update(parameters or {})
                    response = await self._http_client.post(
                        route.base_url.rstrip("/") + "/embeddings",
                        headers={"Authorization": f"Bearer {route.api_key}"},
                        json=payload,
                    )
                    response.raise_for_status()
                    body = response.json()
                    indexed, token_count = self._parse_openai_response(body, len(inputs))
                indexed.sort(key=lambda item: item[0])
                dimensions = len(indexed[0][1])
                if any(len(vector) != dimensions for _, vector in indexed):
                    raise EmbeddingProviderError(
                        "invalid_upstream_response", "同一响应中的向量维度不一致"
                    )
                expected_count = (
                    1
                    if route.route_prefix == "volc"
                    and any(isinstance(item, dict) for item in inputs)
                    else len(inputs)
                )
                if len({index for index, _ in indexed}) != expected_count:
                    raise EmbeddingProviderError(
                        "invalid_upstream_response", "Embedding 上游重复或遗漏了向量序号"
                    )
                normalized: dict[str, object] = {
                    "object": "list",
                    "data": [
                        {"object": "embedding", "index": index, "embedding": vector}
                        for index, vector in indexed
                    ],
                    "usage": {"prompt_tokens": token_count, "total_tokens": token_count},
                }
                normalized["model"] = model
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

    async def _embed_volc(
        self,
        route: ResolvedEmbeddingModel,
        inputs: list[str | dict[str, object]],
        parameters: dict[str, object],
    ) -> tuple[list[tuple[int, list[float]]], int]:
        """方舟多模态 API 对一组混合内容返回一个向量；文本数组仍保持批量语义。"""
        dimensions = parameters.get("dimensions")
        if dimensions is not None and dimensions not in {1024, 2048}:
            raise EmbeddingProviderError(
                "unsupported_embedding_dimensions",
                "火山豆包多模态向量维度仅支持 1024 或 2048",
                422,
            )
        if any(isinstance(item, dict) for item in inputs) and any(
            isinstance(item, str) for item in inputs
        ):
            raise EmbeddingProviderError(
                "invalid_embedding_input", "多模态内容必须使用统一的结构化 input 数组", 422
            )
        if any(isinstance(item, dict) for item in inputs):
            request_inputs = [item for item in inputs if isinstance(item, dict)]
            results = [await self._volc_request(route, request_inputs, parameters)]
        else:
            text_inputs = [item for item in inputs if isinstance(item, str)]
            if len(text_inputs) != len(inputs):
                raise EmbeddingProviderError(
                    "invalid_embedding_input", "Embedding 文本批次包含不支持的输入类型", 422
                )

            async def embed_text(text: str) -> tuple[list[float], int]:
                async with self._volc_text_semaphore:
                    return await self._volc_request(
                        route, [{"type": "text", "text": text}], parameters
                    )

            results_or_errors = await asyncio.gather(
                *(embed_text(text) for text in text_inputs), return_exceptions=True
            )
            results = []
            for result in results_or_errors:
                if isinstance(result, BaseException):
                    raise result
                results.append(result)
        indexed = [(index, vector) for index, (vector, _) in enumerate(results)]
        return indexed, sum(tokens for _, tokens in results)

    async def _volc_request(
        self,
        route: ResolvedEmbeddingModel,
        inputs: list[dict[str, object]],
        parameters: dict[str, object],
    ) -> tuple[list[float], int]:
        payload: dict[str, object] = {
            "model": route.upstream_model,
            "input": inputs,
            "encoding_format": "float",
        }
        payload.update(parameters)
        response = await self._http_client.post(
            route.base_url.rstrip("/") + "/embeddings/multimodal",
            headers={"Authorization": f"Bearer {route.api_key}"},
            json=payload,
            timeout=1200,
        )
        response.raise_for_status()
        body = response.json()
        data = body.get("data") if isinstance(body, dict) else None
        embedding = data.get("embedding") if isinstance(data, dict) else None
        if not isinstance(embedding, list) or not embedding or not all(
            isinstance(value, (int, float)) and not isinstance(value, bool) for value in embedding
        ):
            raise EmbeddingProviderError(
                "invalid_upstream_response", "火山多模态 Embedding 返回了无效向量"
            )
        usage = body.get("usage", {}) if isinstance(body, dict) else {}
        token_count = usage.get("prompt_tokens", 0) if isinstance(usage, dict) else 0
        if not isinstance(token_count, int) or token_count < 0:
            token_count = 0
        return [float(value) for value in embedding], token_count

    @staticmethod
    def _parse_openai_response(
        body: object, expected_count: int
    ) -> tuple[list[tuple[int, list[float]]], int]:
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list) or len(data) != expected_count:
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
            if not isinstance(index, int) or index < 0 or index >= expected_count:
                raise EmbeddingProviderError(
                    "invalid_upstream_response", "Embedding 上游返回了无效向量序号"
                )
            indexed.append((index, [float(value) for value in vector]))
        if len({index for index, _ in indexed}) != expected_count:
            raise EmbeddingProviderError(
                "invalid_upstream_response", "Embedding 上游重复或遗漏了向量序号"
            )
        usage = body.get("usage", {}) if isinstance(body, dict) else {}
        token_count = usage.get("prompt_tokens", 0) if isinstance(usage, dict) else 0
        if not isinstance(token_count, int) or token_count < 0:
            token_count = 0
        return indexed, token_count
