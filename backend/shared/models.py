"""Shared data models for Edge Inference at Scale services."""
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List
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


class SMSMessage(BaseModel):
    id: Optional[str] = None
    sender: str = Field(..., description="Phone number of sender")
    receiver: str = Field(..., description="Phone number of receiver")
    content: str = Field(..., max_length=160, description="Message content (SMS limit)")
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    priority: MessagePriority = MessagePriority.NORMAL
    metadata: Optional[Dict[str, Any]] = None


class ProcessedMessage(BaseModel):
    original_message: SMSMessage
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
