"""The retrieval pipeline, exercised end to end without downloading a model.

CI has no GPU, no model cache and no API key, so these tests substitute a deterministic
lexical embedding for the multilingual sentence-transformer used in production. Every
other stage — loading, splitting, FAISS indexing, retrieval, grounded answering — is the
real code path.
"""

from __future__ import annotations

import hashlib
import math
from datetime import date, timedelta
from pathlib import Path

import pytest
from langchain_core.embeddings import Embeddings

from opportunity_sentinel.config import Settings
from opportunity_sentinel.models import (
    DeliveryMode,
    Evidence,
    OpportunityCandidate,
    OpportunityType,
)
from opportunity_sentinel.rag import (
    CHUNK_SIZE,
    GroundedAnswer,
    KnowledgeAnswerer,
    build_vector_store,
    load_corpus,
    load_knowledge_documents,
    split_documents,
)

KNOWLEDGE_DIR = Path("docs/knowledge")
DIMENSIONS = 512


class LexicalEmbeddings(Embeddings):
    """Hashing bag-of-words vectors: deterministic, offline, and actually rankable.

    Random fake embeddings would let the tests pass without proving retrieval works.
    Word overlap is a crude signal, but it is a real one, and it is language-agnostic
    enough for the Arabic corpus.
    """

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * DIMENSIONS
        for token in text.split():
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=4).digest()
            vector[int.from_bytes(digest, "big") % DIMENSIONS] += 1.0
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)


class StubResponder:
    """Stands in for the structured chat model, recording what it was asked."""

    def __init__(self, result) -> None:
        self.result = result
        self.prompts: list = []

    def invoke(self, messages):
        self.prompts.append(messages)
        return self.result


class StubRepository:
    def __init__(self, candidates: list[OpportunityCandidate]) -> None:
        self._candidates = candidates

    def list_verified_candidates(self, limit: int = 200) -> list[OpportunityCandidate]:
        return self._candidates[:limit]


@pytest.fixture
def candidate() -> OpportunityCandidate:
    return OpportunityCandidate(
        title="Software Engineering CO-OP Program",
        organization="Example Technology Company",
        opportunity_type=OpportunityType.COOP,
        city="Riyadh",
        delivery_mode=DeliveryMode.IN_PERSON,
        accepted_majors=["Software Engineering"],
        deadline=date.today() + timedelta(days=30),
        application_url="https://official.example/apply",
        source_url="https://official.example/coop",
        evidence=[
            Evidence(
                field_name="deadline",
                value="2026-09-30",
                quote="Applications close on 30 September 2026",
                source_url="https://official.example/coop",
                official_source=True,
            )
        ],
    )


@pytest.fixture(scope="module")
def knowledge_store():
    """One FAISS index over the real corpus, shared by the retrieval tests."""
    chunks = split_documents(load_knowledge_documents(KNOWLEDGE_DIR))
    return build_vector_store(chunks, LexicalEmbeddings())


def test_the_shipped_knowledge_corpus_is_not_empty() -> None:
    """A capstone demo must never depend on the database already having rows."""
    documents = load_knowledge_documents(KNOWLEDGE_DIR)

    assert len(documents) >= 5
    assert all(document.page_content.strip() for document in documents)
    assert {"tuwaiq-faq", "eligibility-rules"} <= {
        document.metadata["source"] for document in documents
    }


def test_a_missing_knowledge_directory_yields_no_documents(tmp_path: Path) -> None:
    assert load_knowledge_documents(tmp_path / "absent") == []


def test_verified_opportunities_join_the_corpus(candidate: OpportunityCandidate) -> None:
    documents = load_corpus(KNOWLEDGE_DIR, StubRepository([candidate]))

    kinds = {document.metadata["kind"] for document in documents}
    assert kinds == {"guide", "opportunity"}
    opportunity = next(d for d in documents if d.metadata["kind"] == "opportunity")
    assert "Example Technology Company" in opportunity.page_content
    assert "Applications close on 30 September 2026" in opportunity.page_content


def test_splitting_keeps_chunks_within_the_configured_size() -> None:
    chunks = split_documents(load_knowledge_documents(KNOWLEDGE_DIR))

    assert len(chunks) > len(load_knowledge_documents(KNOWLEDGE_DIR))
    assert max(len(chunk.page_content) for chunk in chunks) <= CHUNK_SIZE
    assert all(chunk.metadata["source"] for chunk in chunks)


def test_retrieval_returns_the_document_that_answers_the_question(knowledge_store) -> None:
    retrieved = knowledge_store.as_retriever(search_kwargs={"k": 3}).invoke(
        "هل معسكرات طويق مجانية"
    )

    assert retrieved
    assert "tuwaiq-faq" in {document.metadata["source"] for document in retrieved}


def test_the_answer_is_built_from_retrieved_context(knowledge_store) -> None:
    responder = StubResponder(
        GroundedAnswer(answer="نعم، مجانية.", citations=["tuwaiq-faq"], supported=True)
    )
    answerer = KnowledgeAnswerer(
        retriever=knowledge_store.as_retriever(search_kwargs={"k": 3}),
        responder=responder,
    )

    result = answerer.answer("هل معسكرات طويق مجانية")

    assert result.supported is True
    assert result.citations == ["tuwaiq-faq"]
    human_turn = responder.prompts[0][1][1]
    assert "هل معسكرات طويق مجانية" in human_turn
    assert "[tuwaiq-faq]" in human_turn


def test_the_answerer_refuses_when_retrieval_comes_back_empty() -> None:
    class EmptyRetriever:
        def invoke(self, _question: str) -> list:
            return []

    responder = StubResponder(GroundedAnswer(answer="should not be used", supported=True))
    answerer = KnowledgeAnswerer(retriever=EmptyRetriever(), responder=responder)

    result = answerer.answer("سؤال لا تغطيه قاعدة المعرفة")

    assert result.supported is False
    assert responder.prompts == []  # the model is never asked to invent an answer


def test_a_plain_dict_from_the_provider_is_validated(knowledge_store) -> None:
    """Some providers return the parsed JSON rather than the model instance."""
    responder = StubResponder({"answer": "نعم", "citations": ["tuwaiq-faq"], "supported": True})
    answerer = KnowledgeAnswerer(
        retriever=knowledge_store.as_retriever(search_kwargs={"k": 2}), responder=responder
    )

    result = answerer.answer("هل معسكرات طويق مجانية")

    assert isinstance(result, GroundedAnswer)
    assert result.supported is True


def test_from_settings_wires_an_injected_vector_store(knowledge_store) -> None:
    settings = Settings(groq_api_key="test-key", rag_top_k=2)

    answerer = KnowledgeAnswerer.from_settings(settings, vector_store=knowledge_store)

    assert answerer.top_k == 2
    assert answerer.retriever.search_kwargs == {"k": 2}


def test_from_settings_refuses_to_build_on_an_empty_corpus(tmp_path: Path) -> None:
    settings = Settings(groq_api_key="test-key", knowledge_dir=tmp_path / "absent")

    with pytest.raises(ValueError, match="No knowledge documents"):
        KnowledgeAnswerer.from_settings(settings, embeddings=LexicalEmbeddings())


def test_the_lazy_answerer_degrades_instead_of_crashing_the_bot(tmp_path: Path) -> None:
    """Deployments without the capstone extra must still answer, honestly."""
    from opportunity_sentinel.rag import LazyKnowledgeAnswerer

    settings = Settings(groq_api_key="test-key", knowledge_dir=tmp_path / "absent")
    lazy = LazyKnowledgeAnswerer(settings)

    first = lazy.answer("سؤال")
    second = lazy.answer("سؤال آخر")

    assert first.supported is False
    assert second.supported is False
    assert lazy._unavailable is True  # the failed build is not retried per message


def test_the_lazy_answerer_delegates_once_the_index_exists(knowledge_store) -> None:
    from opportunity_sentinel.rag import LazyKnowledgeAnswerer

    settings = Settings(groq_api_key="test-key")
    lazy = LazyKnowledgeAnswerer(settings)
    lazy._answerer = KnowledgeAnswerer(
        retriever=knowledge_store.as_retriever(search_kwargs={"k": 2}),
        responder=StubResponder(GroundedAnswer(answer="نعم", supported=True)),
    )

    assert lazy.answer("هل معسكرات طويق مجانية").supported is True
