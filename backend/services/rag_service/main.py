"""RAG Service — Retrieval-Augmented Generation for Summit Connect knowledge."""
try:
    __import__("pysqlite3")
    import sys
    sys.modules["sqlite3"] = sys.modules.pop("pysqlite3")
except ImportError:
    pass

import hashlib
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional

import chromadb
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from backend.shared.config import settings
from backend.shared.models import RAGAddDocumentRequest, RAGQuery, RAGResult, ServiceHealth
from backend.services.rag_service.document_manager import DocumentManager
from backend.services.rag_service.corpus_package import validate_corpus_package
from backend.services.rag_service.active_release import ActiveReleaseResolver
from backend.services.rag_service.activation_contracts import ActivationStatusResponse
from backend.services.rag_service.embedding_service import LocalEmbeddingService, SimpleEmbeddingService
from backend.services.rag_service.retrieval import (
    cosine_distance_to_confidence,
    fuse_ranked_results,
)

logger = logging.getLogger("rag-service")


class RAGService:
    def __init__(self):
        self.collection_name_base = "summit_connect"
        self.collection_name = self.collection_name_base
        self.embedding_fingerprint = None
        self.retrieval_index_fingerprint = None
        self.client = None
        self.collection = None
        self.min_similarity = settings.rag_min_similarity

        self.embedding_service = LocalEmbeddingService(model_name=settings.embedding_model)
        self.simple_embedding_service = SimpleEmbeddingService()
        self.corpus_manifest = None
        self.active_release = None
        self.corpus_read_only = settings.corpus_read_only
        data_dir = settings.summit_data_dir
        manifest_path = settings.corpus_manifest_path
        if settings.corpus_activation_root:
            if not (
                settings.corpus_event_id
                and settings.corpus_version
                and settings.corpus_public_key_path
            ):
                raise RuntimeError(
                    "activation-managed corpus requires event, version, and public key"
                )
            self.active_release = ActiveReleaseResolver(
                settings.corpus_activation_root,
                expected_event_id=settings.corpus_event_id,
                expected_version=settings.corpus_version,
                trusted_public_key_path=settings.corpus_public_key_path,
            ).resolve()
            data_dir = str(self.active_release.package_dir)
            manifest_path = str(self.active_release.manifest_path)
            self.corpus_read_only = True
        elif not manifest_path:
            candidate = os.path.join(data_dir, "manifest.json")
            if os.path.isfile(candidate):
                manifest_path = candidate
        if (settings.corpus_require_signature or self.corpus_read_only) and not manifest_path:
            raise RuntimeError("immutable or signed corpus policy requires a package manifest")
        if manifest_path:
            self.corpus_manifest = validate_corpus_package(
                manifest_path=manifest_path,
                expected_event_id=settings.corpus_event_id,
                expected_version=settings.corpus_version,
                public_key_path=settings.corpus_public_key_path,
                require_signature=(
                    settings.corpus_require_signature
                    or self.active_release is not None
                ),
            )
        self.document_manager = DocumentManager(data_dir=data_dir)

        self.stats = {
            "total_searches": 0,
            "successful_searches": 0,
            "failed_searches": 0,
            "total_documents_added": 0,
            "last_search": None,
            "embedding_service_available": False,
            "chromadb_available": False,
            "index_sync_status": "not_started",
            "index_sync_errors": 0,
        }
        self.timing_totals_ms: Dict[str, float] = {}
        self.timing_counts: Dict[str, int] = {}

    def _record_stage_timing(self, stage: str, started_at: float) -> None:
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        self.timing_totals_ms[stage] = self.timing_totals_ms.get(stage, 0.0) + elapsed_ms
        self.timing_counts[stage] = self.timing_counts.get(stage, 0) + 1

    def timing_snapshot(self) -> Dict[str, Dict[str, object]]:
        snapshot: Dict[str, Dict[str, object]] = {}
        for stage, total_ms in self.timing_totals_ms.items():
            samples = self.timing_counts.get(stage, 0)
            snapshot[stage] = {
                "samples": samples,
                "average_ms": round(total_ms / samples, 2) if samples else 0.0,
                "total_ms": round(total_ms, 2),
            }
        return snapshot

    async def initialize(self) -> bool:
        try:
            logger.info("Initializing RAG Service...")

            embedding_initialized = await self.embedding_service.initialize()
            if not embedding_initialized:
                logger.warning("Local embedding service unavailable, trying simple fallback")
                embedding_initialized = await self.simple_embedding_service.initialize()
                if embedding_initialized:
                    self.embedding_service = self.simple_embedding_service

            self.stats["embedding_service_available"] = embedding_initialized
            if not embedding_initialized:
                raise RuntimeError("No embedding service is available")

            self._select_embedding_collection()
            self._initialize_database()
            await self._sync_local_documents()
            if not await self.document_manager.get_all_documents():
                await self._add_sample_data()

            logger.info("RAG Service initialized successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to initialize RAG Service: {e}")
            return False

    def _select_embedding_collection(self) -> None:
        """Choose a non-destructive collection version for this embedding space."""
        model_info = self.embedding_service.get_model_info()
        embedding_identity = "{model}:{dimension}:cosine".format(
            model=model_info.get("model_name", "unknown"),
            dimension=model_info.get("embedding_dim", "unknown"),
        )
        self.embedding_fingerprint = hashlib.sha256(
            embedding_identity.encode("utf-8")
        ).hexdigest()[:10]
        if self.corpus_manifest:
            corpus = self.corpus_manifest["corpus"]
            event = self.corpus_manifest["event"]
            documents_hash = self.corpus_manifest["files"]["documents.json"]["sha256"]
            corpus_identity = "{event}:{version}:{documents_hash}".format(
                event=event["id"],
                version=corpus["version"],
                documents_hash=documents_hash,
            )
        else:
            corpus_identity = "mutable"
        index_identity = "rag-v3:{embedding}:{corpus}".format(
            embedding=embedding_identity,
            corpus=corpus_identity,
        )
        self.retrieval_index_fingerprint = hashlib.sha256(
            index_identity.encode("utf-8")
        ).hexdigest()[:12]
        self.collection_name = "{base}_v3_{fingerprint}".format(
            base=self.collection_name_base,
            fingerprint=self.retrieval_index_fingerprint,
        )

    def _initialize_database(self):
        try:
            chroma_host = os.getenv("CHROMA_HOST", settings.chroma_host)
            chroma_port = int(os.getenv("CHROMA_PORT", settings.chroma_port))

            try:
                self.client = chromadb.HttpClient(host=chroma_host, port=chroma_port)
                self.client.heartbeat()
                logger.info(f"Connected to ChromaDB at {chroma_host}:{chroma_port}")
            except Exception:
                logger.warning("ChromaDB server unavailable, using persistent local client")
                os.makedirs(settings.chroma_persist_dir, exist_ok=True)
                self.client = chromadb.PersistentClient(path=settings.chroma_persist_dir)

            self.collection = self.client.get_or_create_collection(
                name=self.collection_name,
                metadata={
                    "description": "Summit Connect knowledge base",
                    "embedding_fingerprint": self.embedding_fingerprint or "unknown",
                    "retrieval_index_fingerprint": self.retrieval_index_fingerprint
                    or "unknown",
                    "embedding_model": self.embedding_service.get_model_info().get(
                        "model_name", "unknown"
                    ),
                    # Chroma 1.x still supports this legacy key, which also keeps
                    # the service compatible with the declared Chroma 0.4 floor.
                    "hnsw:space": "cosine",
                },
            )

            logger.info(f"ChromaDB initialized with {self.collection.count()} documents")
            self.stats["chromadb_available"] = True

        except Exception as e:
            logger.error(f"Failed to initialize ChromaDB: {e}")
            self.client = None
            self.collection = None
            self.stats["chromadb_available"] = False

    async def _sync_local_documents(self):
        try:
            if not self.collection:
                self.stats["index_sync_status"] = "unavailable"
                return False

            local_docs = await self.document_manager.get_all_documents()
            local_index = self.document_manager.get_document_index()
            local_ids = set(local_index.keys())
            chroma_snapshot = self.collection.get(include=["metadatas"])
            chroma_ids = chroma_snapshot.get("ids", [])
            chroma_metadatas = chroma_snapshot.get("metadatas", [])
            parent_to_chunk_ids: Dict[str, List[str]] = {}
            parent_to_hash: Dict[str, str] = {}
            for idx, chunk_id in enumerate(chroma_ids):
                metadata = chroma_metadatas[idx] if idx < len(chroma_metadatas) else {}
                parent_id = metadata.get("parent_doc_id") or chunk_id.split("::", 1)[0]
                parent_to_chunk_ids.setdefault(parent_id, []).append(chunk_id)
                if metadata.get("content_hash"):
                    parent_to_hash[parent_id] = metadata["content_hash"]

            stale_parent_ids = list(set(parent_to_chunk_ids.keys()) - local_ids)
            for parent_id in stale_parent_ids:
                self.collection.delete(ids=parent_to_chunk_ids[parent_id])

            for doc in local_docs:
                doc_id = doc["id"]
                doc_hash = local_index[doc_id]
                if parent_to_hash.get(doc_id) == doc_hash:
                    continue
                metadata = {
                    "category": doc.get("category", "general"),
                    "title": doc.get("title", ""),
                    "created_at": doc.get("created_at", ""),
                    "keywords": ",".join(doc.get("keywords", [])),
                    "content_hash": doc_hash,
                    **doc.get("metadata", {}),
                }
                await self._upsert_document_chunks(doc_id, doc["text"], metadata, doc_hash)

            logger.info(f"Local documents synced (local={len(local_ids)} chroma={self.collection.count()})")
            self.stats["index_sync_status"] = "ready"
            return True
        except Exception as e:
            logger.error(f"Failed to sync local documents: {e}")
            self.stats["index_sync_status"] = "failed"
            self.stats["index_sync_errors"] += 1
            return False

    async def _add_sample_data(self):
        sample_documents = [
            {
                "id": "sc_about",
                "text": "Summit Connect is a 3-day technology conference featuring sessions on edge computing, AI, cloud-native, and open source. Text your questions to get info.",
                "metadata": {"category": "about", "priority": "high"},
            },
            {
                "id": "sc_schedule",
                "text": "Summit Connect runs July 15-17. Day 1: keynotes and workshops. Day 2: breakout sessions and demos. Day 3: hands-on labs and closing ceremony.",
                "metadata": {"category": "schedule", "priority": "high"},
            },
            {
                "id": "sc_venue",
                "text": "Summit Connect is held at the Convention Center, 100 Main St. Registration is in the North Lobby. Sessions in rooms A1-A10 (Level 2) and B1-B5 (Level 3).",
                "metadata": {"category": "venue", "priority": "high"},
            },
            {
                "id": "sc_edge_keynote",
                "text": "Keynote: 'Edge Inference at Scale' by Red Hat and Intel. July 15, 9:00 AM, Main Hall. Learn how 1-bit LLMs bring AI to the edge with minimal resources.",
                "metadata": {"category": "sessions", "priority": "high"},
            },
            {
                "id": "sc_food",
                "text": "Food options: Main cafeteria (Level 1) open 7AM-6PM. Coffee stations on every level. Food trucks outside the south entrance 11AM-3PM daily.",
                "metadata": {"category": "venue", "priority": "medium"},
            },
            {
                "id": "sc_wifi",
                "text": "Free WiFi available. Network: SummitConnect-Guest, no password needed. For high-bandwidth demos, use SummitConnect-Labs with badge QR code.",
                "metadata": {"category": "venue", "priority": "medium"},
            },
            {
                "id": "sc_emergency",
                "text": "For emergencies at the venue, text HELP or call event security at ext 5555. First aid station is near registration in the North Lobby.",
                "metadata": {"category": "emergency", "priority": "high"},
            },
            {
                "id": "sc_transport",
                "text": "Shuttle service runs every 15 min between partner hotels and the Convention Center, 7AM-10PM. Rideshare pickup/dropoff at the east entrance.",
                "metadata": {"category": "transport", "priority": "medium"},
            },
        ]

        try:
            added = 0
            for doc in sample_documents:
                if await self.add_document(
                    doc_id=doc["id"],
                    text=doc["text"],
                    metadata=doc["metadata"],
                ):
                    added += 1
            logger.info(f"Added {added}/{len(sample_documents)} Summit Connect sample documents")
            if not self.collection:
                self.stats["index_sync_status"] = "unavailable"
            elif added == len(sample_documents):
                self.stats["index_sync_status"] = "ready"
            else:
                self.stats["index_sync_status"] = "partial"
                self.stats["index_sync_errors"] += len(sample_documents) - added
        except Exception as e:
            logger.error(f"Failed to add sample data: {e}")
            self.stats["index_sync_status"] = "failed"
            self.stats["index_sync_errors"] += 1

    async def search(self, query: RAGQuery) -> RAGResult:
        started_at = time.perf_counter()
        self.stats["total_searches"] += 1
        self.stats["last_search"] = datetime.utcnow().isoformat()
        try:
            results = await self._hybrid_search(query)
            self.stats["successful_searches"] += 1
            return results
        except Exception as e:
            logger.error(f"Search error: {e}")
            self.stats["failed_searches"] += 1
            return RAGResult(documents=[], scores=[], metadata=[])
        finally:
            self._record_stage_timing("search_total", started_at)

    async def _hybrid_search(self, query: RAGQuery) -> RAGResult:
        vector_results = []
        text_results = []
        if self.collection and self.stats["chromadb_available"]:
            started_at = time.perf_counter()
            try:
                vector_results = await self._vector_search(query)
            finally:
                self._record_stage_timing("vector_search", started_at)

        started_at = time.perf_counter()
        try:
            text_results = await self._text_search(query)
        finally:
            self._record_stage_timing("text_search", started_at)

        started_at = time.perf_counter()
        try:
            return await self._combine_search_results(
                vector_results, text_results, query.top_k
            )
        finally:
            self._record_stage_timing("fusion", started_at)

    async def _vector_search(self, query: RAGQuery) -> List[Dict[str, Any]]:
        try:
            query_embedding = await self.embedding_service.encode_text(query.query)
            if query_embedding is None:
                logger.warning("Embedding service returned no vector for query")
                return []
            results = self.collection.query(
                query_embeddings=[query_embedding],
                n_results=min(query.top_k, 10),
                where=query.filter_metadata if query.filter_metadata else None,
            )
            documents = results["documents"][0] if results["documents"] else []
            distances = results["distances"][0] if results["distances"] else []
            metadatas = results["metadatas"][0] if results["metadatas"] else []

            vector_results = []
            for i, doc in enumerate(documents):
                metadata = metadatas[i] if i < len(metadatas) else {}
                vector_results.append(
                    {
                        "document": doc,
                        "metadata": metadata,
                        "score": cosine_distance_to_confidence(distances[i])
                        if i < len(distances)
                        else 0.5,
                        "source": "vector",
                    }
                )
            return vector_results
        except Exception as e:
            logger.error(f"Vector search error: {e}")
            return []

    async def _text_search(self, query: RAGQuery) -> List[Dict[str, Any]]:
        try:
            results = await self.document_manager.search_documents(
                query=query.query,
                category=query.filter_metadata.get("category") if query.filter_metadata else None,
                limit=query.top_k,
            )
            text_results = []
            for result in results:
                text_results.append(
                    {
                        "document": result["document"]["text"],
                        "metadata": {
                            "parent_doc_id": result["document"].get("id", ""),
                            "category": result["document"].get("category", "general"),
                            "title": result["document"].get("title", ""),
                            "keywords": ",".join(result["document"].get("keywords", [])),
                            **result["document"].get("metadata", {}),
                        },
                        "score": min(result["score"] / 15.0, 1.0),
                        "source": "text",
                    }
                )
            return text_results
        except Exception as e:
            logger.error(f"Text search error: {e}")
            return []

    async def _combine_search_results(
        self,
        vector_results: List[Dict[str, Any]],
        text_results: List[Dict[str, Any]],
        top_k: int,
    ) -> RAGResult:
        sorted_results = fuse_ranked_results(
            vector_results=vector_results,
            text_results=text_results,
            top_k=top_k,
            min_confidence=self.min_similarity,
        )

        return RAGResult(
            documents=[r["document"] for r in sorted_results],
            scores=[r["score"] for r in sorted_results],
            metadata=[r["metadata"] for r in sorted_results],
        )

    async def add_document(self, doc_id: str, text: str, metadata: Dict[str, Any] = None) -> bool:
        try:
            doc_metadata = metadata or {}
            await self.document_manager.add_document(
                text=text,
                title=doc_metadata.get("title", ""),
                category=doc_metadata.get("category", "general"),
                metadata=doc_metadata,
                doc_id=doc_id,
            )
            doc_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if self.collection:
                await self._upsert_document_chunks(doc_id, text, doc_metadata, doc_hash)
            self.stats["total_documents_added"] += 1
            return True
        except Exception as e:
            logger.error(f"Failed to add document: {e}")
            return False

    def _chunk_text(self, text: str) -> List[str]:
        chunk_size = max(100, settings.rag_chunk_size_chars)
        overlap = max(0, min(settings.rag_chunk_overlap_chars, chunk_size // 2))
        stripped = text.strip()
        if len(stripped) <= chunk_size:
            return [stripped]
        chunks: List[str] = []
        start = 0
        length = len(stripped)
        while start < length:
            end = min(length, start + chunk_size)
            chunk = stripped[start:end].strip()
            if chunk:
                chunks.append(chunk)
            if end >= length:
                break
            start = max(0, end - overlap)
        return chunks

    async def _upsert_document_chunks(self, doc_id: str, text: str, metadata: Dict[str, Any], content_hash: str):
        if not self.collection:
            return
        chunks = self._chunk_text(text)
        embeddings = await self.embedding_service.encode_texts(chunks)
        if len(embeddings) != len(chunks) or any(item is None for item in embeddings):
            raise RuntimeError("Embedding generation failed; existing index entry was preserved")

        existing = self.collection.get(where={"parent_doc_id": doc_id})
        existing_ids = existing.get("ids", [])
        if existing_ids:
            self.collection.delete(ids=existing_ids)
        chunk_ids = [f"{doc_id}::chunk::{idx}" for idx in range(len(chunks))]
        chunk_metadatas = [
            {**metadata, "parent_doc_id": doc_id, "chunk_index": idx, "chunk_count": len(chunks), "content_hash": content_hash}
            for idx in range(len(chunks))
        ]
        self.collection.upsert(
            documents=chunks,
            embeddings=embeddings,
            ids=chunk_ids,
            metadatas=chunk_metadatas,
        )

    def get_stats(self) -> Dict[str, Any]:
        try:
            doc_stats = self.document_manager.get_statistics()
            embedding_info = self.embedding_service.get_model_info()
            chromadb_stats = {"status": "unavailable", "document_count": 0}
            if self.collection:
                try:
                    chromadb_stats = {
                        "status": "available",
                        "document_count": self.collection.count(),
                        "collection_name": self.collection_name,
                    }
                except Exception as e:
                    chromadb_stats = {"status": "error", "error": str(e)}
            activation = None
            if self.active_release is not None:
                status = self.active_release.status
                activation = {
                    "active_digest": status.digest,
                    "active_sequence": status.sequence,
                    "sequence_floor": status.sequence_floor,
                    "mode": status.mode,
                    "state": status.activation_state,
                    "ready": True,
                    "reason_code": (
                        "RECOVERY_ACTIVE" if status.mode == "recovery" else "READY"
                    ),
                }
            return {
                **self.stats,
                "retrieval_timings_ms": self.timing_snapshot(),
                "document_manager": doc_stats,
                "chromadb": chromadb_stats,
                "embedding_service": embedding_info,
                "embedding_fingerprint": self.embedding_fingerprint,
                "retrieval_index_fingerprint": self.retrieval_index_fingerprint,
                "corpus": self.corpus_manifest or {"status": "unpackaged"},
                "activation": activation or {"status": "not_configured"},
            }
        except Exception as e:
            logger.error(f"Failed to get stats: {e}")
            return {"status": "error", "error": str(e)}


rag_service = RAGService()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("RAG Service starting up...")
    if not await rag_service.initialize():
        logger.error("Failed to initialize RAG Service")
        raise RuntimeError("RAG Service initialization failed")
    yield
    logger.info("RAG Service shutting down...")
    await rag_service.embedding_service.cleanup()


app = FastAPI(
    title="Edge Inference at Scale - RAG Service",
    description="Retrieval-Augmented Generation for Summit Connect knowledge base",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", response_model=ServiceHealth)
async def health_check():
    stats = rag_service.get_stats()
    return ServiceHealth(
        service_name="rag-service",
        status="healthy",
        version="1.0.0",
        details=stats,
    )


@app.post("/search", response_model=RAGResult)
async def search_knowledge(query: RAGQuery):
    return await rag_service.search(query)


@app.post("/add")
async def add_document(request: RAGAddDocumentRequest):
    if rag_service.corpus_read_only:
        raise HTTPException(status_code=403, detail="Active corpus package is immutable")
    doc_id = request.doc_id or hashlib.sha256(request.text.encode("utf-8")).hexdigest()[:12]
    success = await rag_service.add_document(doc_id, request.text, request.metadata)
    if success:
        return {"status": "added", "doc_id": doc_id}
    raise HTTPException(status_code=500, detail="Failed to add document")


@app.get("/stats")
async def get_statistics():
    return rag_service.get_stats()


@app.get("/activation/status", response_model=ActivationStatusResponse)
async def get_activation_status():
    if rag_service.active_release is None:
        raise HTTPException(
            status_code=409, detail="Activation-managed corpus is not configured"
        )
    status = rag_service.active_release.status
    return ActivationStatusResponse(
        active_digest=status.digest,
        active_sequence=status.sequence,
        sequence_floor=status.sequence_floor,
        mode=status.mode,
        state=status.activation_state,
        ready=True,
        reason_code="RECOVERY_ACTIVE" if status.mode == "recovery" else "READY",
    )


@app.get("/categories")
async def get_categories():
    categories = await rag_service.document_manager.get_all_categories()
    return {"categories": categories}


@app.post("/documents/bulk-add")
async def bulk_add_documents(documents: List[Dict[str, Any]]):
    if rag_service.corpus_read_only:
        raise HTTPException(status_code=403, detail="Active corpus package is immutable")
    try:
        doc_ids = await rag_service.document_manager.bulk_add_documents(documents)
        if rag_service.collection:
            for doc_data, doc_id in zip(documents, doc_ids):
                if doc_id:
                    try:
                        await rag_service._upsert_document_chunks(
                            doc_id=doc_id,
                            text=doc_data.get("text", ""),
                            metadata=doc_data.get("metadata", {}),
                            content_hash=hashlib.sha256(doc_data.get("text", "").encode("utf-8")).hexdigest(),
                        )
                    except Exception as e:
                        logger.warning(f"Failed to add {doc_id} to ChromaDB: {e}")
        return {"status": "added", "doc_ids": doc_ids}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/documents/{doc_id}")
async def delete_document(doc_id: str):
    if rag_service.corpus_read_only:
        raise HTTPException(status_code=403, detail="Active corpus package is immutable")
    success = await rag_service.document_manager.delete_document(doc_id)
    if success and rag_service.collection:
        try:
            existing = rag_service.collection.get(where={"parent_doc_id": doc_id})
            chunk_ids = existing.get("ids", [])
            if chunk_ids:
                rag_service.collection.delete(ids=chunk_ids)
            else:
                rag_service.collection.delete(ids=[doc_id])
        except Exception:
            pass
    if success:
        return {"status": "deleted", "doc_id": doc_id}
    raise HTTPException(status_code=404, detail="Document not found")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=settings.rag_service_port, log_level="info")
