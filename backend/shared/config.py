"""Shared configuration settings for Edge Inference at Scale services."""
from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    env: str = "development"

    # Node identity
    node_id: str = "edge-node-001"

    # SMS mode: "sim" (web simulator) or "twilio" (real SMS)
    sms_mode: str = "sim"

    # Twilio (only when sms_mode=twilio)
    twilio_account_sid: Optional[str] = None
    twilio_auth_token: Optional[str] = None
    twilio_phone_number: Optional[str] = None

    # BitNet inference server
    bitnet_server_url: str = "http://bitnet-server:8080"
    model_name: str = "bitnet-2b4t"

    # Model / LLM
    llm_provider: str = "bitnet"
    default_model: str = "bitnet-2b4t"
    embedding_model: str = "all-MiniLM-L6-v2"
    embedding_cache_dir: str = "/data/models/embedding_cache"
    rag_min_similarity: float = 0.5

    # Vector Database
    chroma_host: str = "chromadb"
    chroma_port: int = 8000
    chroma_persist_dir: str = "/data/chroma"

    # Data
    summit_data_dir: str = "/data/summit_connect"

    # Redis
    redis_url: str = "redis://redis:6379/0"

    # Redis Streams
    stream_name: str = "sms:inbound"
    stream_consumer_group: str = "processors"
    stream_max_len: int = 10000

    # Chat history
    chat_history_max_turns: int = 10
    chat_history_ttl_seconds: int = 3600

    # Service URLs (internal container networking)
    sms_gateway_url: str = "http://sms-gateway:8001"
    message_router_url: str = "http://message-router:8002"
    llm_inference_url: str = "http://llm-inference:8003"
    rag_service_url: str = "http://rag-service:8004"
    privacy_filter_url: str = "http://privacy-filter:8005"

    # Service Ports
    api_gateway_port: int = 8000
    sms_gateway_port: int = 8001
    message_router_port: int = 8002
    llm_inference_port: int = 8003
    rag_service_port: int = 8004
    privacy_filter_port: int = 8005

    # Rate Limiting
    max_sms_per_minute: int = 10
    max_sms_per_hour: int = 100

    # Edge load envelope
    sms_inbound_queue_maxsize: int = 500
    sms_outbound_queue_maxsize: int = 1000
    sms_forward_max_retries: int = 3
    sms_forward_retry_backoff_seconds: int = 2
    sms_router_timeout_seconds: float = 60.0
    llm_request_timeout_seconds: float = 60.0
    llm_max_inflight_requests: int = 2
    rag_direct_threshold: float = 0.8
    rag_chunk_size_chars: int = 600
    rag_chunk_overlap_chars: int = 120

    # Security
    secret_key: str = "change_this_secret_key_in_production"

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
