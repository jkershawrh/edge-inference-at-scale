"""Contract and protocol tests for the constrained Big EVY MCP surface."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp import Client

from corpus_factory.mcp_server import (
    MAX_INPUT_BYTES,
    CorpusFactoryMCP,
    CorpusMCPSettings,
    MCPConfigurationError,
    create_mcp_server,
)


ROOT = Path(__file__).resolve().parents[2]


def test_default_service_exposes_current_advisory_evidence_only():
    service = CorpusFactoryMCP(CorpusMCPSettings.from_environment())

    mission = service.mission_status()
    coverage = service.coverage_plan("2026-07-02T12:00:00-05:00")
    work = service.sourcing_work_plan("2026-07-02T12:00:00-05:00", 3)
    lineage = service.lineage_status()

    assert mission["mission_profile_id"] == "summit-connect-2026"
    assert len(mission["requirements"]) == 11
    assert coverage["summary"]["requirements"] == 11
    assert sum(
        coverage["summary"][key] for key in ("covered", "gaps", "conflicted")
    ) == 11
    available = coverage["summary"]["gaps"]
    assert work["summary"]["available_gap_requirements"] == available
    assert work["summary"]["work_items"] == min(available, 3)
    assert work["summary"]["omitted"] == max(0, available - 3)
    assert work["summary"]["truncated"] is (available > 3)
    assert lineage["verified"] is True
    assert lineage["records"] == mission["classification_count"] + lineage["excluded"]
    assert all(
        result["automation_boundary"][prohibition] is False
        for result in (mission, lineage)
        for prohibition in (
            "network_access",
            "may_fetch_content",
            "may_approve_sources",
            "may_sign_artifacts",
            "may_publish_releases",
            "may_deploy",
        )
    )


@pytest.mark.asyncio
async def test_mcp_protocol_lists_only_four_closed_world_read_only_tools():
    server = create_mcp_server(CorpusMCPSettings.from_environment())

    async with Client(server, raise_exceptions=True) as client:
        listed = await client.list_tools()
        names = [tool.name for tool in listed.tools]
        result = await client.call_tool("get_mission_status")

    assert names == [
        "get_mission_status",
        "plan_coverage",
        "generate_sourcing_work_plan",
        "verify_lineage",
    ]
    assert all(tool.annotations.read_only_hint is True for tool in listed.tools)
    assert all(tool.annotations.open_world_hint is False for tool in listed.tools)
    assert result.is_error is False
    assert result.structured_content["result"]["mission_profile_id"] == (
        "summit-connect-2026"
    )
    forbidden_fragments = {"fetch", "approve", "sign", "publish", "deploy", "shell"}
    assert not any(
        fragment in name for name in names for fragment in forbidden_fragments
    )


@pytest.mark.asyncio
async def test_protocol_enforces_work_item_and_timestamp_bounds():
    server = create_mcp_server(CorpusMCPSettings.from_environment())

    async with Client(server) as client:
        too_many = await client.call_tool(
            "generate_sourcing_work_plan",
            {"as_of": "2026-07-02T12:00:00-05:00", "max_items": 26},
        )
        oversized_time = await client.call_tool(
            "plan_coverage", {"as_of": "2" * 41}
        )

    assert too_many.is_error is True
    assert oversized_time.is_error is True


def test_configuration_rejects_paths_outside_allowlisted_roots(tmp_path):
    with pytest.raises(MCPConfigurationError, match="outside allowed roots"):
        CorpusMCPSettings.create(
            allowed_roots=(tmp_path,),
            mission_profile=ROOT
            / "corpus_factory/fixtures/valid/corpus-mission-profile.json",
            source_registry=ROOT
            / "corpus_factory/examples/summit_connect/source-registry.json",
            classifications=ROOT
            / "corpus_factory/examples/summit_connect/document-classifications.json",
            lineage_manifest=ROOT
            / "corpus_factory/examples/summit_connect/lineage-manifest.json",
            evidence_dir=ROOT / "data/summit_connect",
        )


def test_configuration_rejects_oversized_json_before_parsing(tmp_path):
    huge = tmp_path / "mission.json"
    huge.write_bytes(b" " * (MAX_INPUT_BYTES + 1))
    settings = CorpusMCPSettings.create(
        allowed_roots=(tmp_path, ROOT),
        mission_profile=huge,
        source_registry=ROOT
        / "corpus_factory/examples/summit_connect/source-registry.json",
        classifications=ROOT
        / "corpus_factory/examples/summit_connect/document-classifications.json",
        lineage_manifest=ROOT
        / "corpus_factory/examples/summit_connect/lineage-manifest.json",
        evidence_dir=ROOT / "data/summit_connect",
    )

    with pytest.raises(MCPConfigurationError, match="exceeds"):
        CorpusFactoryMCP(settings).mission_status()


def test_lineage_verification_fails_closed_on_tampered_manifest(tmp_path):
    source = ROOT / "corpus_factory/examples/summit_connect/lineage-manifest.json"
    manifest = json.loads(source.read_text(encoding="utf-8"))
    manifest["records"][0]["evidence"]["byte_size"] += 1
    tampered = tmp_path / "lineage.json"
    tampered.write_text(json.dumps(manifest), encoding="utf-8")
    settings = CorpusMCPSettings.create(
        allowed_roots=(tmp_path, ROOT),
        mission_profile=ROOT
        / "corpus_factory/fixtures/valid/corpus-mission-profile.json",
        source_registry=ROOT
        / "corpus_factory/examples/summit_connect/source-registry.json",
        classifications=ROOT
        / "corpus_factory/examples/summit_connect/document-classifications.json",
        lineage_manifest=tampered,
        evidence_dir=ROOT / "data/summit_connect",
    )

    with pytest.raises(ValueError, match="does not match current evidence"):
        CorpusFactoryMCP(settings).lineage_status()
