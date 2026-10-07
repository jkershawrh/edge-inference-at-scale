"""Shared data models for Edge Inference at Scale services."""
from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Optional, Dict, Any, List, Literal
from datetime import datetime
from enum import Enum


class MessagePriority(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    EMERGENCY = "emergency"


class MessageType(str, Enum):
    QUERY = "query"
    COMMAND = "command"
    EMERGENCY = "emergency"
    TEMPLATE = "template"
    RAG = "rag"


class MessageChannel(str, Enum):
    """Transport used to bring a message into the shared reasoning pipeline."""

    SMS = "sms"
    SIMULATOR = "simulator"
    DISCORD = "discord"
    LORA = "lora"


class ChannelMessage(BaseModel):
    """Channel-neutral message envelope used by the router and filters."""

    id: Optional[str] = None
    sender: str = Field(..., description="Channel-scoped sender identifier")
    receiver: str = Field(..., description="Channel-scoped destination identifier")
    content: str = Field(..., max_length=4000, description="Inbound message content")
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    priority: MessagePriority = MessagePriority.NORMAL
    channel: MessageChannel = MessageChannel.SMS
    metadata: Optional[Dict[str, Any]] = None


class SMSMessage(ChannelMessage):
    """SMS-compatible envelope with the transport's 160-character limit."""

    content: str = Field(..., max_length=160, description="Message content (SMS limit)")
    channel: MessageChannel = MessageChannel.SMS


class ProcessedMessage(BaseModel):
    original_message: ChannelMessage
    message_type: MessageType
    intent: Optional[str] = None
    entities: Optional[Dict[str, Any]] = None
    requires_rag: bool = False
    requires_llm: bool = True
    priority: MessagePriority = MessagePriority.NORMAL


class LLMRequest(BaseModel):
    prompt: str
    context: Optional[str] = None
    max_length: int = 160
    temperature: float = 0.7
    model: Optional[str] = None
    chat_history: Optional[List[Dict[str, str]]] = None


class LLMResponse(BaseModel):
    response: str
    model_used: str
    tokens_used: int
    processing_time: float
    metadata: Optional[Dict[str, Any]] = None


class RAGQuery(BaseModel):
    query: str
    top_k: int = 3
    filter_metadata: Optional[Dict[str, Any]] = None


class RAGResult(BaseModel):
    documents: List[str]
    scores: List[float]
    metadata: List[Dict[str, Any]]
    active_corpus_digest: Optional[str] = Field(
        default=None, pattern=r"^sha256:[a-f0-9]{64}$"
    )
    active_corpus_sequence: Optional[int] = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_corpus_identity(self) -> "RAGResult":
        if self.active_corpus_sequence is not None and self.active_corpus_digest is None:
            raise ValueError("active corpus sequence requires a digest")
        return self


class RAGEvidenceReference(BaseModel):
    """Bounded retrieval evidence safe for internal answer attribution."""

    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    score: float = Field(ge=0.0, le=1.0)


class AnswerAttribution(BaseModel):
    """Internal provenance record; deliberately excludes prompts and paths."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    channel: MessageChannel
    retrieval_status: Literal["grounded", "no_evidence", "unavailable"]
    response_mode: Literal[
        "rag_direct",
        "llm_grounded",
        "llm_ungrounded",
        "refused_grounding",
        "refused_generation_disabled",
        "refused_emergency_grounding",
        "error",
    ]
    grounded: bool
    active_corpus_digest: Optional[str] = Field(
        default=None, pattern=r"^sha256:[a-f0-9]{64}$"
    )
    active_corpus_sequence: Optional[int] = Field(default=None, ge=1)
    evidence: List[RAGEvidenceReference] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_coherent_attribution(self) -> "AnswerAttribution":
        if self.active_corpus_sequence is not None and self.active_corpus_digest is None:
            raise ValueError("active corpus sequence requires a digest")
        expected_grounded = self.response_mode in {"rag_direct", "llm_grounded"}
        if self.grounded is not expected_grounded:
            raise ValueError("grounded flag does not match response mode")
        if self.retrieval_status != "grounded" and self.evidence:
            raise ValueError("unavailable retrieval cannot claim evidence")
        return self


class RAGAddDocumentRequest(BaseModel):
    doc_id: Optional[str] = None
    text: str
    metadata: Optional[Dict[str, Any]] = None


class ServiceHealth(BaseModel):
    service_name: str
    status: str
    version: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    details: Optional[Dict[str, Any]] = None


class ServiceMetrics(BaseModel):
    service_name: str
    requests_total: int = 0
    requests_successful: int = 0
    requests_failed: int = 0
    average_response_time: float = 0.0
    uptime_seconds: float = 0.0
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class ErrorResponse(BaseModel):
    error: str
    detail: Optional[str] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)
