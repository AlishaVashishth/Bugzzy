from typing import Any

from pr_context_builder.config import Settings, load_settings
from pr_context_builder.localization.analyzer import analyze_code_defects
from pr_context_builder.localization.groq_client import GroqLocalizationClient
from pr_context_builder.localization.models import (
    AISuggestedBugLocation,
    BugLocalizationResult,
)
from pr_context_builder.models.context_package import ContextPackage


def localize_bugs(
    context: ContextPackage,
    groq_client: Any = None,
    settings: Settings | None = None,
) -> BugLocalizationResult:
    """Consumes an existing ContextPackage and returns a structured BugLocalizationResult.

    Accepts the object returned by build_context() directly without requiring
    caller conversion or re-fetching repository data. Performs deterministic AST
    analysis and optionally augments with grounded AI suggestions via Groq.

    Args:
        context: Fully populated ContextPackage from Context Builder Agent.
        groq_client: Optional injected GroqLocalizationClient (useful for offline testing).
        settings: Optional custom Settings instance. Defaults to load_settings().

    Returns:
        BugLocalizationResult containing verified findings, AI suggestions,
        propagated SHAs, limitations, and snippet metrics.

    Raises:
        ValueError: If context is None or not a ContextPackage instance.
    """
    if context is None or not isinstance(context, ContextPackage):
        raise ValueError("A valid ContextPackage instance is required for bug localization.")

    limitations: list[str] = []
    for lim in context.limitations:
        type_val = lim.limitation_type.value
        if lim.target_path:
            limitations.append(f"[{type_val}] ({lim.target_path}) {lim.message}")
        else:
            limitations.append(f"[{type_val}] {lim.message}")

    analyzed_count = len(context.evidence_snippets) + len(context.test_snippets)

    # 1. Always run deterministic AST and diff analysis first
    findings = analyze_code_defects(context)

    # 2. Hybrid AI suggestion pass via Groq
    ai_suggestions: list[AISuggestedBugLocation] = []
    dl_model_applied: str | None = None

    active_settings = settings or load_settings()

    if groq_client is not None:
        client = groq_client
        dl_model_applied = getattr(client, "model_name", "groq")
        suggs, ai_limits = client.analyze(context)
        ai_suggestions.extend(suggs)
        limitations.extend(ai_limits)
    elif active_settings.groq_enabled:
        if not active_settings.groq_api_key:
            limitations.append(
                "[model_unavailable] GROQ_API_KEY is not configured. Groq AI localization skipped."
            )
        else:
            client = GroqLocalizationClient(
                api_key=active_settings.groq_api_key,
                model=active_settings.groq_model,
                timeout=active_settings.groq_timeout_seconds,
                max_tokens=active_settings.groq_max_tokens,
            )
            dl_model_applied = client.model_name
            suggs, ai_limits = client.analyze(context)
            ai_suggestions.extend(suggs)
            limitations.extend(ai_limits)

    return BugLocalizationResult(
        pr_number=context.pr.number,
        head_sha=context.head_sha,
        base_sha=context.base_sha,
        findings=findings,
        ai_suggestions=ai_suggestions,
        analysis_limitations=limitations,
        analyzed_snippets_count=analyzed_count,
        dl_model_applied=dl_model_applied,
    )
