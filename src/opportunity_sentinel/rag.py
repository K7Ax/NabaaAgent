"""Retrieval-augmented answering over Nabaa's own knowledge.

**Why Hybrid RAG.** Retrieval here is *gated by the supervisor*, and *fixed once entered*.
The supervisor decides whether a message needs the knowledge base at all — that is the
agentic half — and only ``ask_knowledge`` messages reach this module. Once inside,
retrieval is a plain 2-Step retrieve-then-generate.

The alternatives were considered and rejected:

* **Pure 2-Step** would retrieve on every message, including "find me a bootcamp", where
  the answer must come from a live search rather than from stored documents. Retrieval
  there is wasted work and actively misleading.
* **Full Agentic RAG**, where the model issues retrieval calls in a loop, buys little on a
  corpus this small (a handful of policy documents plus the verified opportunities) while
  spending tokens the free tier has to ration, and it makes latency unpredictable.

The corpus has two halves: hand-written policy and FAQ documents under ``docs/knowledge``,
and the opportunities the pipeline itself has verified. Answers are grounded — every claim
must be supported by retrieved context, and the model is expected to say so when it is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from opportunity_sentinel.config import Settings
from opportunity_sentinel.logging import logger

if TYPE_CHECKING:  # pragma: no cover - import only for type checking
    from langchain_core.documents import Document
    from langchain_core.runnables import Runnable

CHUNK_SIZE = 800
CHUNK_OVERLAP = 120
EMBEDDING_MODEL = "intfloat/multilingual-e5-small"

ANSWER_SYSTEM_PROMPT = """\
You answer questions about Saudi student opportunities for Nabaa, using ONLY the context \
provided. Answer in the language of the question, which is usually Arabic.

Set supported=true only when the context actually contains the answer. When it does not, \
set supported=false and say plainly that the knowledge base does not cover it — never \
guess, and never fill the gap from your own knowledge.

Put the source name of every document you used in citations.\
"""


class GroundedAnswer(BaseModel):
    """An answer that carries its own evidence."""

    answer: str = Field(description="The answer, in the language of the question")
    citations: list[str] = Field(default_factory=list, description="Source names used")
    supported: bool = Field(
        default=False, description="True only when the context supports the answer"
    )


def load_knowledge_documents(knowledge_dir: Path) -> list[Document]:
    """Load the hand-written policy and FAQ documents."""
    from langchain_core.documents import Document

    if not knowledge_dir.exists():
        return []
    documents = []
    for path in sorted(knowledge_dir.glob("*.md")):
        documents.append(
            Document(
                page_content=path.read_text(encoding="utf-8"),
                metadata={"source": path.stem, "kind": "guide"},
            )
        )
    return documents


def opportunity_documents(repository) -> list[Document]:
    """Render each verified opportunity as a document the model can quote."""
    from langchain_core.documents import Document

    if repository is None:
        return []
    documents = []
    for candidate in repository.list_verified_candidates():
        lines = [
            f"العنوان: {candidate.title}",
            f"الجهة: {candidate.organization}",
            f"النوع: {candidate.opportunity_type.value}",
            f"المدينة: {candidate.city or 'غير محددة'}",
            f"طريقة الحضور: {candidate.delivery_mode.value}",
            f"الموعد النهائي: {candidate.deadline or 'غير معلن'}",
            f"رابط التقديم: {candidate.application_url}",
        ]
        if candidate.accepted_majors:
            lines.append(f"التخصصات المقبولة: {', '.join(candidate.accepted_majors)}")
        lines.extend(f"دليل: {item.quote}" for item in candidate.evidence)
        documents.append(
            Document(
                page_content="\n".join(lines),
                metadata={
                    "source": candidate.organization,
                    "kind": "opportunity",
                    "url": str(candidate.application_url),
                },
            )
        )
    return documents


def load_corpus(knowledge_dir: Path, repository=None) -> list[Document]:
    """Both halves of the corpus: written guides plus verified opportunities."""
    documents = load_knowledge_documents(knowledge_dir) + opportunity_documents(repository)
    logger.info("rag_corpus_loaded", documents=len(documents))
    return documents


def split_documents(documents: list[Document]) -> list[Document]:
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        separators=["\n## ", "\n\n", "\n", "۔", ".", " "],
    )
    chunks = splitter.split_documents(documents)
    logger.info("rag_corpus_split", chunks=len(chunks))
    return chunks


def build_embeddings():  # pragma: no cover - downloads a model, never run in CI
    """Local multilingual embeddings.

    The corpus is Arabic, so an English-only model such as all-MiniLM-L6-v2 is not an
    option. multilingual-e5-small is the smallest widely used multilingual embedder, runs
    locally, and costs nothing per query.
    """
    from langchain_huggingface import HuggingFaceEmbeddings

    return HuggingFaceEmbeddings(model_name=EMBEDDING_MODEL)


def build_vector_store(chunks: list[Document], embeddings):
    from langchain_community.vectorstores import FAISS

    return FAISS.from_documents(chunks, embeddings)


@dataclass
class KnowledgeAnswerer:
    """Retrieve, then answer with citations."""

    retriever: Any
    responder: Runnable
    top_k: int = 4

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        repository=None,
        embeddings=None,
        vector_store=None,
    ) -> KnowledgeAnswerer:
        from opportunity_sentinel.chat_models import build_structured

        if vector_store is None:
            chunks = split_documents(load_corpus(settings.knowledge_dir, repository))
            if not chunks:
                raise ValueError(f"No knowledge documents found in {settings.knowledge_dir}")
            vector_store = build_vector_store(chunks, embeddings or build_embeddings())
        return cls(
            retriever=vector_store.as_retriever(search_kwargs={"k": settings.rag_top_k}),
            responder=build_structured(settings, GroundedAnswer),
            top_k=settings.rag_top_k,
        )

    def answer(self, question: str) -> GroundedAnswer:
        documents = self.retriever.invoke(question)
        if not documents:
            # Fail closed: no context means no grounded answer to give.
            return GroundedAnswer(
                answer="لا تحتوي قاعدة المعرفة على إجابة لهذا السؤال.",
                citations=[],
                supported=False,
            )
        context = "\n\n---\n\n".join(
            f"[{document.metadata.get('source', 'unknown')}]\n{document.page_content}"
            for document in documents
        )
        result = self.responder.invoke(
            [
                ("system", ANSWER_SYSTEM_PROMPT),
                ("human", f"السؤال: {question}\n\nالسياق:\n{context}"),
            ]
        )
        if not isinstance(result, GroundedAnswer):  # providers can return plain dicts
            result = GroundedAnswer.model_validate(result)
        logger.info(
            "rag_answer",
            retrieved=len(documents),
            supported=result.supported,
            citations=len(result.citations),
        )
        return result


class LazyKnowledgeAnswerer:
    """Defer building the index until someone actually asks a question.

    The embedding model is a few hundred megabytes and the index takes seconds to build,
    which is the wrong thing to do while a webhook is waiting for the process to start.
    Deployments without the ``capstone`` extra installed have no FAISS or sentence-
    transformers at all, so a failure here degrades to an honest refusal rather than
    taking the bot down.
    """

    def __init__(self, settings: Settings, repository=None) -> None:
        self._settings = settings
        self._repository = repository
        self._answerer: KnowledgeAnswerer | None = None
        self._unavailable = False

    def answer(self, question: str) -> GroundedAnswer:
        if self._answerer is None and not self._unavailable:
            try:
                self._answerer = KnowledgeAnswerer.from_settings(
                    self._settings, repository=self._repository
                )
            except Exception as exc:  # noqa: BLE001 - any failure must degrade, not crash
                self._unavailable = True
                logger.warning("rag_unavailable", error=str(exc))
        if self._answerer is None:
            return GroundedAnswer(
                answer="قاعدة المعرفة غير متاحة حاليًا، جرّب البحث عن فرصة بدلًا من ذلك.",
                citations=[],
                supported=False,
            )
        return self._answerer.answer(question)
