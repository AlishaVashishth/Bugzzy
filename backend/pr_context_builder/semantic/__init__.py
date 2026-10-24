"""Deep Learning semantic code representation and relevance ranking subpackage."""

from pr_context_builder.semantic.encoder import CodeEncoderProtocol, TransformerCodeEncoder
from pr_context_builder.semantic.metrics import mean_reciprocal_rank, precision_at_k, recall_at_k
from pr_context_builder.semantic.ranker import SemanticRanker

__all__ = [
    "CodeEncoderProtocol",
    "SemanticRanker",
    "TransformerCodeEncoder",
    "mean_reciprocal_rank",
    "precision_at_k",
    "recall_at_k",
]
