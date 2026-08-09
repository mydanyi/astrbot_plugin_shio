from __future__ import annotations

import asyncio
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from .models import Expression, SpeechPlan


class StyleRetriever:
    def __init__(self, data_dir: Path, assets_dir: Path, logger: Any) -> None:
        self.data_dir = data_dir
        self.assets_dir = assets_dir
        self.logger = logger
        self.library_path = data_dir / "expressions.json"
        self.cache_path = data_dir / "expression_vectors.json"
        self._expressions: list[Expression] | None = None
        self._vector_cache: dict[str, Any] | None = None
        self._vector_cache_lock = asyncio.Lock()

    def ensure_library(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if self.library_path.exists():
            return
        source = self.assets_dir / "default_expressions.json"
        self.library_path.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    def load(self) -> list[Expression]:
        if self._expressions is not None:
            return self._expressions
        self.ensure_library()
        try:
            raw = json.loads(self.library_path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.logger.warning("[星汐] 表达库读取失败：%s", exc)
            raw = []
        expressions: list[Expression] = []
        for index, item in enumerate(raw if isinstance(raw, list) else []):
            expression = Expression.from_mapping(item, index)
            if expression and expression.enabled:
                expressions.append(expression)
        self._expressions = expressions
        return expressions

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        if not left or len(left) != len(right):
            return -1.0
        dot = sum(a * b for a, b in zip(left, right))
        left_norm = math.sqrt(sum(a * a for a in left))
        right_norm = math.sqrt(sum(b * b for b in right))
        if left_norm == 0 or right_norm == 0:
            return -1.0
        return dot / (left_norm * right_norm)

    @staticmethod
    def _lexical_score(query: str, document: str) -> float:
        query_chars = {char for char in query if not char.isspace()}
        doc_chars = {char for char in document if not char.isspace()}
        if not query_chars or not doc_chars:
            return 0.0
        overlap = len(query_chars & doc_chars) / math.sqrt(len(query_chars) * len(doc_chars))
        bonuses = 0.0
        groups = (
            (("可爱", "萌", "厉害", "真棒", "高性能"), ("夸", "表扬", "害羞", "得意")),
            (("笨", "菜", "性能不行", "反应慢"), ("质疑", "欺负", "委屈", "嘴硬")),
            (("错了", "不对", "错误", "搞错"), ("出错", "校准", "改正")),
            (("难过", "焦虑", "害怕", "想哭"), ("低落", "安慰", "温柔")),
            (("谢谢", "感谢"), ("感谢", "道谢")),
        )
        for needles, related in groups:
            if any(token in query for token in needles) and any(
                token in document for token in related
            ):
                bonuses += 0.35
        return overlap + bonuses

    def _cache_key(self, provider_id: str, documents: list[str]) -> str:
        payload = provider_id + "\n" + "\n".join(documents)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _load_vector_cache(self) -> dict[str, Any]:
        if self._vector_cache is not None:
            return self._vector_cache
        try:
            value = json.loads(self.cache_path.read_text(encoding="utf-8"))
            self._vector_cache = value if isinstance(value, dict) else {}
        except Exception:
            self._vector_cache = {}
        return self._vector_cache

    async def _document_vectors(
        self,
        embedding_provider: Any,
        provider_id: str,
        documents: list[str],
    ) -> list[list[float]]:
        key = self._cache_key(provider_id, documents)
        async with self._vector_cache_lock:
            # 并发群消息可能同时命中首次向量化。进入锁后重新检查缓存，
            # 避免重复请求 Provider 或交叉写坏 JSON 文件。
            cache = self._load_vector_cache()
            cached = cache.get(key)
            if isinstance(cached, list) and len(cached) == len(documents):
                return cached
            vectors = await embedding_provider.get_embeddings(documents)
            cache.clear()
            cache[key] = vectors
            temporary_path = self.cache_path.with_suffix(".tmp")
            temporary_path.write_text(
                json.dumps(cache, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            temporary_path.replace(self.cache_path)
            return vectors

    async def retrieve(
        self,
        *,
        current_message: str,
        plan: SpeechPlan,
        embedding_provider: Any = None,
        embedding_provider_id: str = "",
        rerank_provider: Any = None,
        candidate_count: int = 12,
        top_k: int = 3,
        feedback_scores: dict[str, float] | None = None,
    ) -> list[Expression]:
        expressions = self.load()
        if not expressions or top_k <= 0:
            return []
        query = " ".join(
            part
            for part in (
                current_message,
                plan.intent,
                plan.reply_act,
                plan.emotion,
                plan.tone,
            )
            if part
        )
        documents = [item.document() for item in expressions]
        ranked_indices: list[int]
        if embedding_provider is not None:
            try:
                query_vector = await embedding_provider.get_embedding(query)
                doc_vectors = await self._document_vectors(
                    embedding_provider,
                    embedding_provider_id,
                    documents,
                )
                ranked_indices = sorted(
                    range(len(expressions)),
                    key=lambda index: self._cosine(query_vector, doc_vectors[index]),
                    reverse=True,
                )
            except Exception as exc:
                self.logger.warning("[星汐] Embedding 检索失败，降级为本地匹配：%s", exc)
                ranked_indices = sorted(
                    range(len(expressions)),
                    key=lambda index: self._lexical_score(query, documents[index]),
                    reverse=True,
                )
        else:
            ranked_indices = sorted(
                range(len(expressions)),
                key=lambda index: self._lexical_score(query, documents[index]),
                reverse=True,
            )

        candidates = ranked_indices[: max(top_k, candidate_count)]
        if rerank_provider is not None and len(candidates) > top_k:
            candidate_docs = [documents[index] for index in candidates]
            try:
                reranked = await rerank_provider.rerank(query, candidate_docs, top_n=top_k)
                selected = [
                    candidates[item.index]
                    for item in sorted(
                        reranked,
                        key=lambda result: result.relevance_score,
                        reverse=True,
                    )
                    if 0 <= item.index < len(candidates)
                ]
                if selected:
                    candidates = selected
            except Exception as exc:
                self.logger.warning("[星汐] Reranker 调用失败，使用向量排序结果：%s", exc)
        if feedback_scores:
            original_order = {index: rank for rank, index in enumerate(candidates)}
            candidates = sorted(
                candidates,
                key=lambda index: (
                    -original_order[index]
                    + max(
                        -2.0,
                        min(2.0, float(feedback_scores.get(expressions[index].id, 0.0))),
                    )
                    * 0.30
                ),
                reverse=True,
            )
        return [expressions[index] for index in candidates[:top_k]]
