#!/usr/bin/env python3
"""Collect and validate OpenShift evidence before running live EDD.

This command is read-only. It records workload readiness and public health
metadata without copying Secrets, tokens, prompts, or message content.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple


ROOT = Path(__file__).resolve().parents[1]
CORE_COMPONENTS = (
    "api-gateway",
    "sms-gateway",
    "message-router",
    "rag-service",
    "privacy-filter",
    "chromadb",
)
GENERATION_COMPONENTS = ("llm-inference", "bitnet")
EXPECTED_COMPONENTS = CORE_COMPONENTS + GENERATION_COMPONENTS


def _oc_json(namespace: str, *args: str) -> Dict[str, Any]:
    command = ["oc", "-n", namespace, *args, "-o", "json"]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip().splitlines()[-1] if completed.stderr else "unknown error"
        raise RuntimeError("oc command failed: {0}".format(message))
    return json.loads(completed.stdout)


def _oc_text(*args: str) -> str:
    completed = subprocess.run(
        ["oc", *args],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("oc command failed")
    return completed.stdout.strip()


def _get_json(base_url: str, path: str) -> Dict[str, Any]:
    request = urllib.request.Request(
        "{0}{1}".format(base_url.rstrip("/"), path),
        headers={"Accept": "application/json"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def _normalize_channel(value: str) -> str:
    return {"sim": "simulator"}.get(value, value)


def evaluate_snapshot(
    snapshot: Dict[str, Any], expected: Dict[str, str]
) -> Tuple[str, List[str]]:
    """Return RED/GREEN and reasons for one observed OpenShift deployment."""
    reasons: List[str] = []
    generation_value = expected.get("generation_enabled", "").lower()
    if generation_value not in {"true", "false"}:
        reasons.append("GENERATION_ENABLED declaration must be true or false")
    generation_enabled = generation_value == "true"
    required_components = CORE_COMPONENTS + (
        GENERATION_COMPONENTS if generation_enabled else ()
    )
    deployments = snapshot.get("deployments") or []
    by_name = {item["name"]: item for item in deployments}
    for component in required_components:
        name = "{0}-{1}".format(expected["release"], component)
        deployment = by_name.get(name)
        if not deployment:
            reasons.append("missing deployment {0}".format(name))
        elif deployment["ready"] < deployment["desired"] or deployment["desired"] < 1:
            reasons.append(
                "deployment {0} is not ready ({1}/{2})".format(
                    name, deployment["ready"], deployment["desired"]
                )
            )
    if not generation_enabled:
        for component in GENERATION_COMPONENTS:
            name = "{0}-{1}".format(expected["release"], component)
            if name in by_name:
                reasons.append(
                    "generation-disabled profile still deploys {0}".format(name)
                )

    config = snapshot.get("config") or {}
    comparisons = {
        "EDGE_RESOURCE_PROFILE": expected["profile"],
        "EMBEDDING_MODEL": expected["embedding_model"],
        "GENERATION_ENABLED": "true" if generation_enabled else "false",
    }
    if generation_enabled:
        comparisons.update(
            {
                "LLM_PROVIDER": expected["llm_provider"],
                "LLM_MODEL": expected["llm_model"],
            }
        )
    for key, expected_value in comparisons.items():
        if config.get(key) != expected_value:
            reasons.append(
                "{0} mismatch: expected {1}, observed {2}".format(
                    key, expected_value, config.get(key, "missing")
                )
            )

    if config.get("RAG_GROUNDING_REQUIRED", "").lower() != "true":
        reasons.append("RAG_GROUNDING_REQUIRED is not true")
    if config.get("EMERGENCY_RAG_ENABLED", "").lower() != "true":
        reasons.append("EMERGENCY_RAG_ENABLED is not true")
    if _normalize_channel(config.get("SMS_MODE", "")) != expected["channel"]:
        reasons.append("configured channel does not match the declared channel")

    rag_stats = snapshot.get("rag_stats") or {}
    if config.get("CORPUS_MODE") != expected["corpus_mode"]:
        reasons.append("deployed corpus mode does not match CORPUS_MODE")
    if expected["corpus_mode"] == "activation":
        activation = rag_stats.get("activation") or {}
        if not activation.get("ready"):
            reasons.append("RAG activation is not ready")
        if activation.get("active_digest") != expected["corpus_digest"]:
            reasons.append("active corpus digest does not match CORPUS_DIGEST")
    elif expected["corpus_mode"] == "packaged":
        corpus = rag_stats.get("corpus") or {}
        corpus_identity = rag_stats.get("corpus_identity") or {}
        if config.get("CORPUS_REQUIRE_SIGNATURE", "").lower() != "true":
            reasons.append("packaged corpus signature enforcement is not enabled")
        if (corpus.get("event") or {}).get("id") != expected["event_id"]:
            reasons.append("live corpus event does not match CORPUS_EVENT_ID")
        if (corpus.get("corpus") or {}).get("version") != expected["version"]:
            reasons.append("live corpus version does not match CORPUS_VERSION")
        if corpus_identity.get("digest") != expected["corpus_manifest_digest"]:
            reasons.append(
                "live corpus manifest does not match CORPUS_MANIFEST_DIGEST"
            )
        resolved = snapshot.get("resolved_images") or []
        corpus_image = next(
            (item for item in resolved if item.get("name") == "install-corpus"), None
        )
        if not corpus_image or expected["corpus_digest"] not in corpus_image.get("image_id", ""):
            reasons.append("resolved corpus image does not match CORPUS_DIGEST")
    else:
        reasons.append("CORPUS_MODE must be activation or packaged")
    observed_embedding = (rag_stats.get("embedding_service") or {}).get("model_name")
    if observed_embedding != expected["embedding_model"]:
        reasons.append("live embedding model does not match EMBEDDING_MODEL")
    embedding_source = (rag_stats.get("embedding_service") or {}).get("model_source")
    if embedding_source != "baked":
        reasons.append("embedding model is not baked into the deployed RAG image")

    if generation_enabled:
        llm_details = (snapshot.get("llm_health") or {}).get("details") or {}
        if llm_details.get("provider") != expected["llm_provider"]:
            reasons.append("live LLM provider does not match LLM_PROVIDER")
        if llm_details.get("model_name") != expected["llm_model"]:
            reasons.append("live LLM model does not match LLM_MODEL")
        if not llm_details.get("provider_reachable"):
            reasons.append("LLM provider is not reachable")
    elif "llm-inference" in (snapshot.get("service_health") or {}):
        reasons.append("generation-disabled gateway still advertises llm-inference")

    service_health = snapshot.get("service_health") or {}
    unhealthy = sorted(
        name
        for name, value in service_health.items()
        if value.get("status") != "healthy"
    )
    if unhealthy:
        reasons.append("unhealthy services: {0}".format(", ".join(unhealthy)))
    return ("RED", reasons) if reasons else ("GREEN", [])


def collect(namespace: str, release: str, api_url: str) -> Dict[str, Any]:
    selector = "app.kubernetes.io/instance={0}".format(release)
    deployment_body = _oc_json(namespace, "get", "deployments", "-l", selector)
    pod_body = _oc_json(namespace, "get", "pods", "-l", selector)
    config_body = _oc_json(namespace, "get", "configmap", "{0}-config".format(release))
    route_body = _oc_json(namespace, "get", "route", "{0}-api-gateway".format(release))
    route_url = "https://{0}".format(route_body["spec"]["host"])
    if api_url.rstrip("/") != route_url:
        raise RuntimeError("EDGE_API_URL does not match the release Route")

    deployments = []
    for item in deployment_body.get("items", []):
        deployments.append(
            {
                "name": item["metadata"]["name"],
                "desired": item.get("spec", {}).get("replicas", 0),
                "ready": item.get("status", {}).get("readyReplicas", 0),
                "images": [
                    container.get("image")
                    for container in item.get("spec", {})
                    .get("template", {})
                    .get("spec", {})
                    .get("containers", [])
                ],
            }
        )
    resolved_images = []
    for pod in pod_body.get("items", []):
        status = pod.get("status", {})
        for container_type, statuses in (
            ("container", status.get("containerStatuses", [])),
            ("init", status.get("initContainerStatuses", [])),
        ):
            for container in statuses:
                resolved_images.append(
                    {
                        "pod": pod["metadata"]["name"],
                        "type": container_type,
                        "name": container.get("name"),
                        "image": container.get("image"),
                        "image_id": container.get("imageID"),
                    }
                )
    config = config_body.get("data") or {}
    snapshot = {
        "cluster": _oc_text("whoami", "--show-server"),
        "namespace": namespace,
        "release": release,
        "route_url": route_url,
        "deployments": deployments,
        "resolved_images": resolved_images,
        "config": config,
        "gateway_health": _get_json(api_url, "/health"),
        "service_health": _get_json(api_url, "/services/health"),
        "rag_stats": _get_json(api_url, "/rag/stats"),
    }
    if config.get("GENERATION_ENABLED", "true").lower() == "true":
        snapshot["llm_health"] = _get_json(api_url, "/llm/health")
    return snapshot


def main() -> int:
    output = Path(os.environ.get("OPENSHIFT_PREFLIGHT_OUTPUT", "artifacts/openshift-preflight.json"))
    expected = {
        "namespace": os.environ.get("EDGE_NAMESPACE", ""),
        "release": os.environ.get("EDGE_RELEASE", ""),
        "api_url": os.environ.get("EDGE_API_URL", ""),
        "profile": os.environ.get("EDGE_RESOURCE_PROFILE", ""),
        "corpus_digest": os.environ.get("CORPUS_DIGEST", ""),
        "corpus_manifest_digest": os.environ.get("CORPUS_MANIFEST_DIGEST", ""),
        "corpus_mode": os.environ.get("CORPUS_MODE", ""),
        "event_id": os.environ.get("CORPUS_EVENT_ID", ""),
        "version": os.environ.get("CORPUS_VERSION", ""),
        "embedding_model": os.environ.get("EMBEDDING_MODEL", ""),
        "generation_enabled": os.environ.get("GENERATION_ENABLED", ""),
        "llm_provider": os.environ.get("LLM_PROVIDER", ""),
        "llm_model": os.environ.get("LLM_MODEL", ""),
        "channel": os.environ.get("CHANNEL_DRIVER", ""),
    }
    required = set(expected)
    if expected["generation_enabled"].lower() == "false":
        required -= {"llm_provider", "llm_model"}
    missing = [key for key in sorted(required) if not expected[key]]
    report: Dict[str, Any] = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "expected": expected,
    }
    try:
        if missing:
            raise ValueError("missing required environment: {0}".format(", ".join(missing)))
        if expected["generation_enabled"].lower() not in {"true", "false"}:
            raise ValueError("GENERATION_ENABLED must be true or false")
        snapshot = collect(expected["namespace"], expected["release"], expected["api_url"])
        status, reasons = evaluate_snapshot(snapshot, expected)
        report.update({"status": status, "reasons": reasons, "observed": snapshot})
    except (ValueError, RuntimeError, KeyError, json.JSONDecodeError, urllib.error.URLError) as exc:
        report.update({"status": "RED", "reasons": [str(exc)], "observed": {}})

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "reasons": report["reasons"]}, indent=2))
    print("Evidence: {0}".format(output))
    return 0 if report["status"] == "GREEN" else 1


if __name__ == "__main__":
    sys.exit(main())
