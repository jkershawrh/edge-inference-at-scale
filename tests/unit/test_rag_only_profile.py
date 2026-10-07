"""Deployment-contract tests for the model-less field profile."""

import shutil
import subprocess

import pytest
import yaml
from fastapi import HTTPException
from unittest.mock import patch

from backend.api_gateway.main import APIGateway
from backend.shared.config import settings


def test_gateway_omits_llm_service_when_generation_is_disabled():
    with patch.object(settings, "generation_enabled", False):
        gateway = APIGateway()

    assert "llm-inference" not in gateway.services


@pytest.mark.asyncio
async def test_disabled_llm_route_reports_profile_state():
    with patch.object(settings, "generation_enabled", False):
        gateway = APIGateway()

    with pytest.raises(HTTPException) as exc:
        await gateway.proxy("llm-inference", "/health")

    assert exc.value.status_code == 503
    assert "disabled" in str(exc.value.detail)


def test_field_rag_only_chart_omits_generation_workloads(tmp_path):
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is required for the chart rendering contract")

    values = tmp_path / "signed-corpus.yaml"
    values.write_text(
        yaml.safe_dump(
            {
                "rag": {
                    "corpus": {
                        "enabled": True,
                        "image": "example.invalid/corpus@sha256:" + "a" * 64,
                        "requireSignature": True,
                        "publicKeySecretName": "corpus-signing-key",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            helm,
            "template",
            "lil-evy",
            "chart",
            "-f",
            "chart/profiles/values-field-rag-only.yaml",
            "-f",
            str(values),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    rendered = list(yaml.safe_load_all(result.stdout))
    objects = [item for item in rendered if isinstance(item, dict)]
    names = {item.get("metadata", {}).get("name") for item in objects}
    config = next(item for item in objects if item.get("kind") == "ConfigMap")
    topic = next(item for item in objects if item.get("kind") == "KafkaTopic")

    assert "lil-evy-bitnet" not in names
    assert "lil-evy-llm-inference" not in names
    assert config["data"]["GENERATION_ENABLED"] == "false"
    assert config["data"]["RAG_DIRECT_THRESHOLD"] == "0.55"
    assert config["data"]["RAG_DIRECT_MAX_CHARS"] == "1000"
    assert config["data"]["RAG_MIN_VECTOR_CONFIDENCE"] == "0.25"
    assert topic["metadata"]["name"] == "lil-evy-sms-inbound"
    assert topic["spec"]["topicName"] == "sms.inbound"
    assert config["data"]["STREAM_TOPIC"] == "sms.inbound"
