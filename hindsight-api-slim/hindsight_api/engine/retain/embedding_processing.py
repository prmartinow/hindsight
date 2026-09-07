"""
Embedding processing for retain pipeline.

Handles augmenting fact texts with temporal information and generating embeddings.
"""

import logging

from . import embedding_utils
from .types import ExtractedFact

logger = logging.getLogger(__name__)


def augment_texts_with_dates(
    facts: list[ExtractedFact],
    format_date_fn,
    max_fact_text_chars: int = 24000,
    max_entities: int = 20
) -> list[str]:
    """
    Augment fact texts with readable dates and bounded entity tags for optimal embedding retrieval.

    Mitigates unbounded string amplification and preserves temporal/entity metadata suffixes
    from downstream tail-truncation.

    Args:
        facts: List of ExtractedFact objects
        format_date_fn: Function to format datetime to readable string
        max_fact_text_chars: Maximum characters allowed for raw fact text before suffixing
        max_entities: Maximum unique entity tags attached to embedding representation

    Returns:
        List of augmented text strings (same length as facts)
    """
    augmented_texts = []
    for fact in facts:
        raw_text = fact.fact_text or ""
        # Pre-bound raw text if unusually large so temporal/entity suffixes remain intact
        if len(raw_text) > max_fact_text_chars:
            raw_text = raw_text[:max_fact_text_chars]

        # Use occurred_start as the representative date, fall back to mentioned_at
        fact_date = fact.occurred_start or fact.mentioned_at
        if fact_date is not None:
            readable_date = format_date_fn(fact_date)
            if fact.occurred_end and fact.occurred_end != fact.occurred_start:
                readable_end = format_date_fn(fact.occurred_end)
                augmented_text = f"{raw_text} (happened from {readable_date} to {readable_end})"
            else:
                augmented_text = f"{raw_text} (happened in {readable_date})"
        else:
            augmented_text = raw_text

        if fact.entities:
            # Deduplicate entities while preserving order and bound count
            seen = set()
            unique_entities = []
            for e in fact.entities:
                e_clean = str(e).strip()
                if e_clean and e_clean not in seen:
                    seen.add(e_clean)
                    unique_entities.append(e_clean)
                if len(unique_entities) >= max_entities:
                    break
            if unique_entities:
                entity_str = ", ".join(unique_entities)
                if len(entity_str) > 500:
                    entity_str = entity_str[:500]
                augmented_text = f"{augmented_text} [{entity_str}]"

        augmented_texts.append(augmented_text)
    return augmented_texts


async def generate_embeddings_batch(embeddings_model, texts: list[str]) -> list[list[float]]:
    """
    Generate embeddings for a batch of texts.

    Args:
        embeddings_model: Embeddings backend, or a wrapper around one that batches
            concurrent callers together (``CoalescingEmbedder``, used by the streaming
            retain producer — see issue #3784). Such a wrapper is recognised by its
            ``embed_documents_async`` method; anything else is a plain backend.
        texts: List of text strings to embed

    Returns:
        List of embedding vectors (same length as texts)
    """
    if not texts:
        return []

    embed_batched = getattr(embeddings_model, "embed_documents_async", None)
    if embed_batched is not None:
        return await embed_batched(texts)

    embeddings = await embedding_utils.generate_embeddings_batch(embeddings_model, texts)

    return embeddings
