"""Corpus identity invariants for packaged and activation-managed releases."""

import pytest
from pydantic import ValidationError

from backend.shared.models import AnswerAttribution, MessageChannel, RAGResult


_DIGEST = "sha256:" + "a" * 64


def test_packaged_corpus_can_report_digest_without_activation_sequence() -> None:
    result = RAGResult(
        documents=["verified guidance"],
        scores=[0.9],
        metadata=[{}],
        active_corpus_digest=_DIGEST,
    )

    assert result.active_corpus_digest == _DIGEST
    assert result.active_corpus_sequence is None


def test_activation_sequence_cannot_exist_without_digest() -> None:
    with pytest.raises(ValidationError, match="sequence requires a digest"):
        RAGResult(
            documents=[],
            scores=[],
            metadata=[],
            active_corpus_sequence=3,
        )


def test_answer_attribution_accepts_packaged_corpus_digest() -> None:
    attribution = AnswerAttribution(
        channel=MessageChannel.SMS,
        retrieval_status="no_evidence",
        response_mode="llm_ungrounded",
        grounded=False,
        active_corpus_digest=_DIGEST,
    )

    assert attribution.active_corpus_digest == _DIGEST
    assert attribution.active_corpus_sequence is None
