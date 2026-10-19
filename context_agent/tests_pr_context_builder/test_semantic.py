"""Unit tests for Deep Learning semantic code encoder, ranker, and IR metrics.

All tests run completely offline without downloading model weights or making network requests.
"""

from unittest.mock import MagicMock

import pytest

from pr_context_builder.exceptions import (
    ModelDependencyMissingError,
)
from pr_context_builder.models.repository import CodeSnippet, EvidenceOrigin
from pr_context_builder.semantic.encoder import TransformerCodeEncoder
from pr_context_builder.semantic.metrics import (
    mean_reciprocal_rank,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)
from pr_context_builder.semantic.ranker import SemanticRanker


class MockCodeEncoder:
    """Deterministic mock code encoder for offline unit testing."""

    def __init__(self, model_name: str = "mock-codebert"):
        self._model_name = model_name

    @property
    def model_name(self) -> str:
        return self._model_name

    def encode(self, texts: list[str]) -> list[list[float]]:
        # Deterministic mock vectors:
        # text mentioning "auth" -> [1.0, 0.0]
        # text mentioning "user" -> [0.8, 0.6]
        # text mentioning "math" -> [0.0, 1.0]
        # default -> [0.1, 0.1]
        embeddings = []
        for text in texts:
            lower = text.lower()
            if "auth" in lower:
                embeddings.append([1.0, 0.0])
            elif "user" in lower:
                embeddings.append([0.8, 0.6])
            elif "math" in lower:
                embeddings.append([0.0, 1.0])
            else:
                embeddings.append([0.5, 0.5])
        return embeddings


@pytest.fixture
def sample_candidates() -> list[CodeSnippet]:
    return [
        CodeSnippet(
            file_path="src/auth/jwt.py",
            start_line=1,
            end_line=15,
            content="def verify_auth_token(token): return True",
            commit_sha="abc1234",
            origin=EvidenceOrigin.REPOSITORY_REFERENCE,
        ),
        CodeSnippet(
            file_path="src/models/user.py",
            start_line=10,
            end_line=25,
            content="class UserProfile: pass",
            commit_sha="abc1234",
            origin=EvidenceOrigin.REPOSITORY_REFERENCE,
        ),
        CodeSnippet(
            file_path="src/utils/math.py",
            start_line=5,
            end_line=12,
            content="def compute_math_formula(x): return x * 2",
            commit_sha="abc1234",
            origin=EvidenceOrigin.REPOSITORY_REFERENCE,
        ),
    ]


def test_semantic_ranking_order(sample_candidates: list[CodeSnippet]) -> None:
    """Ensure SemanticRanker ranks most semantically similar code first."""
    encoder = MockCodeEncoder()
    ranker = SemanticRanker(encoder=encoder)

    query = "def authenticate_user(credentials):"  # matches "auth" -> [1.0, 0.0]
    results = ranker.rank_snippets(query=query, candidates=sample_candidates)

    assert len(results) == 3
    # jwt.py has [1.0, 0.0], dot with [1.0, 0.0] = 1.0
    assert results[0].file_path == "src/auth/jwt.py"
    assert results[0].similarity_score == 1.0
    assert results[0].is_model_estimate is True
    assert results[0].model_name == "mock-codebert"

    # user.py has [0.8, 0.6], dot with [1.0, 0.0] / norms = 0.8
    assert results[1].file_path == "src/models/user.py"
    assert results[1].similarity_score == 0.8

    # math.py has [0.0, 1.0], dot with [1.0, 0.0] = 0.0
    assert results[2].file_path == "src/utils/math.py"
    assert results[2].similarity_score == 0.0


def test_semantic_ranking_top_k_and_threshold(sample_candidates: list[CodeSnippet]) -> None:
    """Ensure top_k limit and minimum similarity threshold filter results correctly."""
    encoder = MockCodeEncoder()
    ranker = SemanticRanker(encoder=encoder)

    query = "def authenticate_user(credentials):"

    # top_k filter
    top_1 = ranker.rank_snippets(query=query, candidates=sample_candidates, top_k=1)
    assert len(top_1) == 1
    assert top_1[0].file_path == "src/auth/jwt.py"

    # min_threshold filter
    filtered = ranker.rank_snippets(query=query, candidates=sample_candidates, min_threshold=0.5)
    assert len(filtered) == 2
    assert all(r.similarity_score >= 0.5 for r in filtered)


def test_semantic_ranking_edge_cases(sample_candidates: list[CodeSnippet]) -> None:
    """Ensure empty inputs or whitespace snippets do not cause exceptions."""
    encoder = MockCodeEncoder()
    ranker = SemanticRanker(encoder=encoder)

    # Empty query
    assert ranker.rank_snippets(query="", candidates=sample_candidates) == []
    assert ranker.rank_snippets(query="   ", candidates=sample_candidates) == []

    # Empty candidate list
    assert ranker.rank_snippets(query="def foo():", candidates=[]) == []

    # Candidates with whitespace only
    empty_cand = CodeSnippet(
        file_path="src/empty.py",
        start_line=1,
        end_line=2,
        content="   \n  ",
        commit_sha="abc1234",
        origin=EvidenceOrigin.REPOSITORY_REFERENCE,
    )
    assert ranker.rank_snippets(query="def foo():", candidates=[empty_cand]) == []


def test_transformer_code_encoder_missing_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure missing torch or transformers raises ModelDependencyMissingError."""
    import builtins

    real_import = builtins.__import__

    def mock_import(name, *args, **kwargs):
        if name in ("torch", "transformers"):
            raise ImportError(f"No module named '{name}'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", mock_import)

    with pytest.raises(ModelDependencyMissingError):
        TransformerCodeEncoder(model_name="microsoft/codebert-base")


def test_transformer_code_encoder_with_mocked_internals() -> None:
    """Ensure TransformerCodeEncoder runs batch inference when components are injected."""
    mock_tokenizer = MagicMock()
    mock_model = MagicMock()

    # Mock tokenizer output
    mock_attn = MagicMock()
    mock_attn.to.return_value.unsqueeze.return_value.expand.return_value.sum.return_value = (
        MagicMock()
    )
    mock_tokenizer.return_value = {
        "input_ids": MagicMock(to=lambda dev: MagicMock()),
        "attention_mask": mock_attn,
    }

    encoder = TransformerCodeEncoder(
        model_name="test-model",
        tokenizer=mock_tokenizer,
        model=mock_model,
    )
    assert encoder.model_name == "test-model"
    # Empty batch returns empty list without calling model
    assert encoder.encode([]) == []


def test_information_retrieval_metrics() -> None:
    """Verify standard Information Retrieval evaluation metrics calculations."""
    retrieved = ["file_a.py", "file_b.py", "file_c.py", "file_d.py"]
    relevant = {"file_a.py", "file_c.py"}

    # Precision@k
    assert precision_at_k(retrieved, relevant, k=1) == 1.0  # file_a is in relevant
    assert precision_at_k(retrieved, relevant, k=2) == 0.5  # 1/2
    assert precision_at_k(retrieved, relevant, k=4) == 0.5  # 2/4
    assert precision_at_k(retrieved, set(), k=2) == 0.0

    # Recall@k
    assert recall_at_k(retrieved, relevant, k=1) == 0.5  # 1/2 of relevant found
    assert recall_at_k(retrieved, relevant, k=3) == 1.0  # both file_a and file_c found
    assert recall_at_k(retrieved, relevant, k=0) == 0.0

    # Reciprocal Rank
    assert reciprocal_rank(retrieved, relevant) == 1.0  # file_a is at rank 1
    assert reciprocal_rank(["irrelevant.py", "file_c.py"], relevant) == 0.5
    assert reciprocal_rank(["irrelevant.py"], relevant) == 0.0

    # Mean Reciprocal Rank (MRR)
    mrr = mean_reciprocal_rank(
        retrieved_lists=[
            ["file_a.py", "file_b.py"],
            ["wrong.py", "file_c.py"],
        ],
        relevant_sets=[
            {"file_a.py"},
            {"file_c.py"},
        ],
    )
    # query 1: RR = 1.0 (rank 1), query 2: RR = 0.5 (rank 2) -> MRR = (1.0 + 0.5) / 2 = 0.75
    assert mrr == 0.75

    with pytest.raises(ValueError):
        mean_reciprocal_rank([["a"]], [{"a"}, {"b"}])
