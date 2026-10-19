"""Deep Learning semantic relevance ranker for repository code snippets."""

import math

from pr_context_builder.models.context_package import SemanticRelevanceEstimate
from pr_context_builder.models.repository import CodeSnippet, EvidenceOrigin
from pr_context_builder.semantic.encoder import CodeEncoderProtocol


def _cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    """Computes cosine similarity between two float vectors."""
    dot = sum(a * b for a, b in zip(vec_a, vec_b, strict=False))
    norm_a = math.sqrt(sum(a * a for a in vec_a))
    norm_b = math.sqrt(sum(b * b for b in vec_b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    val = dot / (norm_a * norm_b)
    # Clamp to [-1.0, 1.0] to guard against floating-point inaccuracies
    return max(-1.0, min(1.0, val))


class SemanticRanker:
    """Ranks repository code snippets by semantic relevance to a PR diff context.

    Uses deep learned vector representations from a CodeEncoderProtocol to compute
    cosine similarities. All produced scores are explicitly labeled as model-generated
    relevance estimates to preserve distinction from ground-truth source evidence.
    """

    def __init__(self, encoder: CodeEncoderProtocol):
        self._encoder = encoder

    @property
    def model_name(self) -> str:
        return self._encoder.model_name

    def rank_snippets(
        self,
        query: str,
        candidates: list[CodeSnippet],
        top_k: int | None = None,
        min_threshold: float = -1.0,
    ) -> list[SemanticRelevanceEstimate]:
        """Ranks candidate snippets by cosine similarity against the query code/diff.

        Args:
            query: The PR diff text or focal code region.
            candidates: List of candidate CodeSnippet objects from the repository.
            top_k: Optional maximum number of top results to return.
            min_threshold: Minimum cosine similarity score to qualify.

        Returns:
            List of SemanticRelevanceEstimate objects sorted by descending similarity.
        """
        if not query.strip() or not candidates:
            return []

        # Filter out empty snippets while preserving index mapping
        valid_indices: list[int] = []
        valid_texts: list[str] = []

        for idx, candidate in enumerate(candidates):
            text = candidate.content.strip()
            if text:
                valid_indices.append(idx)
                valid_texts.append(text)

        if not valid_texts:
            return []

        # Encode query and valid candidate snippets
        all_embeddings = self._encoder.encode([query] + valid_texts)
        query_vec = all_embeddings[0]
        candidate_vecs = all_embeddings[1:]

        estimates: list[SemanticRelevanceEstimate] = []

        for cand_idx, emb in zip(valid_indices, candidate_vecs, strict=True):
            cand = candidates[cand_idx]
            sim = _cosine_similarity(query_vec, emb)

            if sim >= min_threshold:
                # Annotate snippet if needed
                cand.relevance_score = sim
                cand.is_model_estimate = True
                if cand.origin == EvidenceOrigin.REPOSITORY_REFERENCE:
                    cand.origin = EvidenceOrigin.SEMANTIC_RANKING

                estimates.append(
                    SemanticRelevanceEstimate(
                        file_path=cand.file_path,
                        start_line=cand.start_line,
                        end_line=cand.end_line,
                        similarity_score=round(sim, 4),
                        model_name=self.model_name,
                        is_model_estimate=True,
                    )
                )

        estimates.sort(key=lambda x: x.similarity_score, reverse=True)

        if top_k is not None and top_k > 0:
            return estimates[:top_k]

        return estimates
