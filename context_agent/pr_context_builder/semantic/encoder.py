"""Deep Learning code encoder using pretrained Transformer models.

Provides learned dense vector representations of source code and diff contexts
using architectures such as CodeBERT or GraphCodeBERT.
"""

from typing import Any, Protocol

from pr_context_builder.exceptions import (
    ModelDependencyMissingError,
    ModelInferenceError,
    ModelNotFoundError,
)


class CodeEncoderProtocol(Protocol):
    """Protocol contract for deep learning code encoders."""

    @property
    def model_name(self) -> str:
        """Name or identifier of the pretrained model."""
        ...

    def encode(self, texts: list[str]) -> list[list[float]]:
        """Encodes a batch of code/text strings into normalized dense vectors."""
        ...


class TransformerCodeEncoder:
    """Pretrained transformer encoder (e.g. CodeBERT, GraphCodeBERT).

    Uses lazy imports of PyTorch and Hugging Face Transformers to avoid hard
    dependencies when deep learning ranking is disabled.
    """

    def __init__(
        self,
        model_name: str = "microsoft/codebert-base",
        device: str = "cpu",
        batch_size: int = 16,
        tokenizer: Any = None,
        model: Any = None,
    ):
        self._model_name = model_name
        self._device = device
        self._batch_size = batch_size

        if tokenizer is not None and model is not None:
            self._tokenizer = tokenizer
            self._model = model
            self._torch = None
        else:
            self._torch, self._tokenizer, self._model = self._init_dependencies_and_model()

    @property
    def model_name(self) -> str:
        return self._model_name

    def _init_dependencies_and_model(self) -> tuple[Any, Any, Any]:
        """Lazy-loads PyTorch and Transformers, raising clear exceptions on failure."""
        try:
            import torch
            from transformers import AutoModel, AutoTokenizer
        except ImportError as exc:
            raise ModelDependencyMissingError(
                f"Deep learning dependencies are missing: {exc}. "
                "Install them using 'pip install -r requirements-dl.txt' or "
                "'pip install .[dl]'."
            ) from exc

        try:
            tokenizer = AutoTokenizer.from_pretrained(self._model_name)
            model = AutoModel.from_pretrained(self._model_name)
            model.to(self._device)
            model.eval()
        except Exception as exc:
            raise ModelNotFoundError(
                f"Failed to load pretrained code encoder '{self._model_name}': {exc}"
            ) from exc

        return torch, tokenizer, model

    def encode(self, texts: list[str]) -> list[list[float]]:
        """Encodes texts into L2-normalized embedding vectors.

        Args:
            texts: List of code snippets or diff text strings.

        Returns:
            List of float lists representing normalized dense vectors.
        """
        if not texts:
            return []

        all_embeddings: list[list[float]] = []

        for i in range(0, len(texts), self._batch_size):
            batch = texts[i : i + self._batch_size]
            try:
                embeddings = self._encode_batch(batch)
                all_embeddings.extend(embeddings)
            except Exception as exc:
                if isinstance(exc, (ModelDependencyMissingError, ModelNotFoundError)):
                    raise
                raise ModelInferenceError(f"Embedding inference failed: {exc}") from exc

        return all_embeddings

    def _encode_batch(self, batch: list[str]) -> list[list[float]]:
        """Encodes a single batch using mean pooling and L2 normalization."""
        if self._torch is None:
            import torch
        else:
            torch = self._torch

        inputs = self._tokenizer(
            batch,
            padding=True,
            truncation=True,
            max_length=512,
            return_tensors="pt",
        )
        inputs = {k: v.to(self._device) for k, v in inputs.items()}

        with torch.no_grad():
            outputs = self._model(**inputs)
            # Pool: mean pooling weighted by attention mask
            token_embeddings = outputs.last_hidden_state  # (batch_size, seq_len, hidden_dim)
            attention_mask = inputs["attention_mask"].unsqueeze(-1).expand(token_embeddings.size())
            sum_embeddings = torch.sum(token_embeddings * attention_mask, dim=1)
            sum_mask = torch.clamp(attention_mask.sum(dim=1), min=1e-9)
            mean_pooled = sum_embeddings / sum_mask

            # L2 Normalize for cosine similarity
            normalized = torch.nn.functional.normalize(mean_pooled, p=2, dim=1)
            return normalized.cpu().tolist()
