"""Evaluation metrics for relevant code retrieval and ranking.

Provides deterministic implementations of standard Information Retrieval (IR)
benchmarking metrics: Precision@k, Recall@k, and Mean Reciprocal Rank (MRR).
"""


def precision_at_k(retrieved: list[str], relevant: set[str], k: int) -> float:
    """Calculates Precision@k for retrieved candidate identifiers (e.g. file paths or symbol names).

    Args:
        retrieved: Ordered list of retrieved items (most relevant first).
        relevant: Set of ground-truth relevant items.
        k: Cut-off rank.

    Returns:
        Precision score between 0.0 and 1.0.
    """
    if k <= 0 or not retrieved or not relevant:
        return 0.0

    top_k = retrieved[:k]
    hits = sum(1 for item in top_k if item in relevant)
    return hits / k


def recall_at_k(retrieved: list[str], relevant: set[str], k: int) -> float:
    """Calculates Recall@k for retrieved candidate identifiers.

    Args:
        retrieved: Ordered list of retrieved items.
        relevant: Set of ground-truth relevant items.
        k: Cut-off rank.

    Returns:
        Recall score between 0.0 and 1.0.
    """
    if k <= 0 or not retrieved or not relevant:
        return 0.0

    top_k = retrieved[:k]
    hits = sum(1 for item in top_k if item in relevant)
    return hits / len(relevant)


def reciprocal_rank(retrieved: list[str], relevant: set[str]) -> float:
    """Calculates the reciprocal rank (1 / rank) of the first relevant item.

    Args:
        retrieved: Ordered list of retrieved items.
        relevant: Set of ground-truth relevant items.

    Returns:
        Reciprocal rank score between 0.0 and 1.0.
    """
    for rank, item in enumerate(retrieved, start=1):
        if item in relevant:
            return 1.0 / rank
    return 0.0


def mean_reciprocal_rank(retrieved_lists: list[list[str]], relevant_sets: list[set[str]]) -> float:
    """Calculates the Mean Reciprocal Rank (MRR) across multiple PR queries.

    Args:
        retrieved_lists: List of retrieved item lists for each query.
        relevant_sets: List of ground-truth relevant item sets for each query.

    Returns:
        MRR score between 0.0 and 1.0.
    """
    if not retrieved_lists or not relevant_sets:
        return 0.0
    if len(retrieved_lists) != len(relevant_sets):
        raise ValueError("retrieved_lists and relevant_sets must have the same length.")

    total_rr = sum(
        reciprocal_rank(r_list, rel_set)
        for r_list, rel_set in zip(retrieved_lists, relevant_sets, strict=True)
    )
    return total_rr / len(retrieved_lists)
