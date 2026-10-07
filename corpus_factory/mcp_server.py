"""Constrained MCP interface for the connected Big EVY corpus factory.

The server is intentionally advisory.  Clients cannot supply paths, URLs, or
content and the exposed tools cannot acquire, approve, sign, publish, deploy,
or mutate corpus artifacts.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Dict, Mapping, Sequence

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from corpus_factory.coverage import plan_mission_coverage
from corpus_factory.sourcing import build_sourcing_work_items
from corpus_factory.summit_lineage import verify_summit_lineage
from corpus_factory.validator import validate_instance


MAX_INPUT_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_BYTES = 256 * 1024
MAX_CLASSIFICATIONS = 1_000
MAX_EVIDENCE_FILES = 1_000
MAX_TOOL_WORK_ITEMS = 25
DEFAULT_ROOT = Path(__file__).resolve().parents[1]


class MCPConfigurationError(ValueError):
    """Raised when the operator's MCP artifact configuration is unsafe."""


@dataclass(frozen=True)
class CorpusMCPSettings:
    """Exact, operator-controlled inputs available through MCP."""

    allowed_roots: tuple[Path, ...]
    mission_profile: Path
    source_registry: Path
    classifications: Path
    lineage_manifest: Path
    evidence_dir: Path

    @classmethod
    def from_environment(cls) -> "CorpusMCPSettings":
        raw_roots = os.environ.get("BIG_EVY_MCP_ALLOWED_ROOTS", str(DEFAULT_ROOT))
        roots = tuple(Path(item) for item in raw_roots.split(os.pathsep) if item.strip())
        if not roots:
            raise MCPConfigurationError("BIG_EVY_MCP_ALLOWED_ROOTS cannot be empty")

        def configured(name: str, default: str) -> Path:
            return Path(os.environ.get(name, default))

        return cls.create(
            allowed_roots=roots,
            mission_profile=configured(
                "BIG_EVY_MCP_MISSION_PROFILE",
                "corpus_factory/fixtures/valid/corpus-mission-profile.json",
            ),
            source_registry=configured(
                "BIG_EVY_MCP_SOURCE_REGISTRY",
                "corpus_factory/examples/summit_connect/source-registry.json",
            ),
            classifications=configured(
                "BIG_EVY_MCP_CLASSIFICATIONS",
                "corpus_factory/examples/summit_connect/document-classifications.json",
            ),
            lineage_manifest=configured(
                "BIG_EVY_MCP_LINEAGE_MANIFEST",
                "corpus_factory/examples/summit_connect/lineage-manifest.json",
            ),
            evidence_dir=configured(
                "BIG_EVY_MCP_EVIDENCE_DIR", "data/summit_connect"
            ),
        )

    @classmethod
    def create(
        cls,
        *,
        allowed_roots: Sequence[Path | str],
        mission_profile: Path | str,
        source_registry: Path | str,
        classifications: Path | str,
        lineage_manifest: Path | str,
        evidence_dir: Path | str,
    ) -> "CorpusMCPSettings":
        roots = tuple(_resolve_root(Path(root)) for root in allowed_roots)
        if not roots:
            raise MCPConfigurationError("at least one allowed root is required")
        base = roots[0]
        return cls(
            allowed_roots=roots,
            mission_profile=_resolve_allowed(Path(mission_profile), roots, base, False),
            source_registry=_resolve_allowed(Path(source_registry), roots, base, False),
            classifications=_resolve_allowed(Path(classifications), roots, base, False),
            lineage_manifest=_resolve_allowed(Path(lineage_manifest), roots, base, False),
            evidence_dir=_resolve_allowed(Path(evidence_dir), roots, base, True),
        )


def _resolve_root(path: Path) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise MCPConfigurationError(f"allowed root is unavailable: {path}") from exc
    if not resolved.is_dir() or resolved == Path(resolved.anchor):
        raise MCPConfigurationError("allowed roots must be existing non-filesystem-root directories")
    return resolved


def _resolve_allowed(
    path: Path, roots: Sequence[Path], base: Path, expect_directory: bool
) -> Path:
    candidate = path if path.is_absolute() else base / path
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise MCPConfigurationError(f"configured artifact is unavailable: {path}") from exc
    if not any(resolved.is_relative_to(root) for root in roots):
        raise MCPConfigurationError(f"configured artifact is outside allowed roots: {path}")
    if expect_directory != resolved.is_dir():
        kind = "directory" if expect_directory else "file"
        raise MCPConfigurationError(f"configured artifact must be a {kind}: {path}")
    return resolved


def _read_json(path: Path) -> Any:
    try:
        size = path.stat().st_size
        if size > MAX_INPUT_BYTES:
            raise MCPConfigurationError(f"configured JSON exceeds {MAX_INPUT_BYTES} bytes")
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MCPConfigurationError(f"cannot read configured JSON artifact: {path.name}") from exc
    return value


def _bounded(result: Dict[str, Any]) -> Dict[str, Any]:
    encoded = json.dumps(
        result, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    if len(encoded) > MAX_OUTPUT_BYTES:
        raise MCPConfigurationError("tool result exceeds the configured output boundary")
    return result


class CorpusFactoryMCP:
    """Read-only adapter over the deterministic corpus factory functions."""

    def __init__(self, settings: CorpusMCPSettings):
        self.settings = settings

    def _inputs(self) -> tuple[Mapping[str, Any], Mapping[str, Any], list[Mapping[str, Any]]]:
        mission = _read_json(self.settings.mission_profile)
        registry = _read_json(self.settings.source_registry)
        wrapper = _read_json(self.settings.classifications)
        if not isinstance(mission, Mapping) or not isinstance(registry, Mapping):
            raise MCPConfigurationError("mission and registry artifacts must be JSON objects")
        if not isinstance(wrapper, Mapping) or not isinstance(wrapper.get("classifications"), list):
            raise MCPConfigurationError("classification artifact must contain a classifications array")
        classifications = wrapper["classifications"]
        if len(classifications) > MAX_CLASSIFICATIONS:
            raise MCPConfigurationError("classification count exceeds the configured boundary")
        if not all(isinstance(item, Mapping) for item in classifications):
            raise MCPConfigurationError("each classification must be a JSON object")
        validate_instance(mission, "corpus_mission_profile")
        validate_instance(registry, "source_registry")
        if mission["event"]["event_id"] != registry["event_id"]:
            raise MCPConfigurationError("mission and registry event identities do not match")
        seen_classifications: set[str] = set()
        for item in classifications:
            validate_instance(item, "document_classification")
            if item["mission_profile_id"] != mission["mission_profile_id"]:
                raise MCPConfigurationError("classification names a different mission")
            if item["classification_id"] in seen_classifications:
                raise MCPConfigurationError("classification identity is duplicated")
            seen_classifications.add(item["classification_id"])
        return mission, registry, classifications

    def mission_status(self) -> Dict[str, Any]:
        mission, registry, classifications = self._inputs()
        requirements = sorted(
            (
                {
                    "requirement_id": item["requirement_id"],
                    "category_id": item["category_id"],
                    "safety_class": item["safety_class"],
                    "minimum_sources": item["minimum_sources"],
                    "required_intents": sorted(item["required_intents"]),
                }
                for item in mission["required_information"]
            ),
            key=lambda item: item["requirement_id"],
        )
        return _bounded(
            {
                "record_type": "mcp_mission_status",
                "mission_profile_id": mission["mission_profile_id"],
                "mission_version": mission["version"],
                "event": mission["event"],
                "scope": mission["scope"],
                "validity": mission["validity"],
                "registry_id": registry["registry_id"],
                "source_count": len(registry["sources"]),
                "classification_count": len(classifications),
                "requirements": requirements,
                "automation_boundary": _automation_boundary(),
            }
        )

    def coverage_plan(self, as_of: str) -> Dict[str, Any]:
        mission, registry, classifications = self._inputs()
        return _bounded(
            plan_mission_coverage(
                mission, registry, classifications, as_of=_bounded_as_of(as_of)
            )
        )

    def sourcing_work_plan(self, as_of: str, max_items: int) -> Dict[str, Any]:
        if isinstance(max_items, bool) or not 1 <= max_items <= MAX_TOOL_WORK_ITEMS:
            raise ValueError(f"max_items must be between 1 and {MAX_TOOL_WORK_ITEMS}")
        mission, registry, classifications = self._inputs()
        report = plan_mission_coverage(
            mission, registry, classifications, as_of=_bounded_as_of(as_of)
        )
        return _bounded(build_sourcing_work_items(mission, report, max_items=max_items))

    def lineage_status(self) -> Dict[str, Any]:
        evidence_files = sorted(self.settings.evidence_dir.glob("*.json"))
        if len(evidence_files) > MAX_EVIDENCE_FILES:
            raise MCPConfigurationError("evidence file count exceeds the configured boundary")
        evidence_root = self.settings.evidence_dir.resolve()
        for path in evidence_files:
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(evidence_root):
                raise MCPConfigurationError("evidence symlink leaves the configured directory")
            if resolved.stat().st_size > MAX_INPUT_BYTES:
                raise MCPConfigurationError("evidence file exceeds the configured input boundary")
        manifest = _read_json(self.settings.lineage_manifest)
        if not isinstance(manifest, Mapping):
            raise MCPConfigurationError("lineage manifest must be a JSON object")
        verify_summit_lineage(
            manifest,
            self.settings.evidence_dir,
            self.settings.source_registry,
            self.settings.classifications,
        )
        records = manifest.get("records", [])
        return _bounded(
            {
                "record_type": "mcp_lineage_status",
                "verified": True,
                "event_id": manifest.get("event_id"),
                "manifest_digest": manifest.get("manifest_digest"),
                "records": len(records),
                "classified": sum(
                    item.get("lifecycle", {}).get("state") == "classified" for item in records
                ),
                "excluded": sum(
                    item.get("lifecycle", {}).get("state") == "excluded" for item in records
                ),
                "automation_boundary": _automation_boundary(),
            }
        )


def _bounded_as_of(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 40:
        raise ValueError("as_of must be an ISO-8601 timestamp of at most 40 characters")
    return value


def _automation_boundary() -> Dict[str, bool]:
    return {
        "read_only": True,
        "advisory_only": True,
        "network_access": False,
        "may_fetch_content": False,
        "may_approve_sources": False,
        "may_sign_artifacts": False,
        "may_publish_releases": False,
        "may_deploy": False,
    }


READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


def create_mcp_server(settings: CorpusMCPSettings | None = None) -> MCPServer:
    service = CorpusFactoryMCP(settings or CorpusMCPSettings.from_environment())
    server = MCPServer(
        "big-evy-corpus-factory",
        version="1.0.0",
        instructions=(
            "Read-only, advisory access to preconfigured corpus-factory artifacts. "
            "This server cannot fetch, approve, sign, publish, deploy, or mutate content."
        ),
    )

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def get_mission_status() -> Dict[str, Any]:
        """Inspect the configured mission, scope, requirements, and artifact counts."""

        return service.mission_status()

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def plan_coverage(
        as_of: Annotated[
            str,
            Field(
                min_length=1,
                max_length=40,
                description="Timezone-aware ISO-8601 evaluation timestamp.",
            ),
        ],
    ) -> Dict[str, Any]:
        """Evaluate deterministic mission coverage without acquiring or changing content."""

        return service.coverage_plan(as_of)

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def generate_sourcing_work_plan(
        as_of: Annotated[
            str,
            Field(
                min_length=1,
                max_length=40,
                description="Timezone-aware ISO-8601 evaluation timestamp.",
            ),
        ],
        max_items: Annotated[
            int,
            Field(
                ge=1,
                le=MAX_TOOL_WORK_ITEMS,
                description="Maximum number of ranked advisory work items.",
            ),
        ] = 10,
    ) -> Dict[str, Any]:
        """Generate bounded advisory work from coverage gaps; perform no network action."""

        return service.sourcing_work_plan(as_of, max_items)

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def verify_lineage() -> Dict[str, Any]:
        """Verify the configured manifest against exact local evidence bytes."""

        return service.lineage_status()

    return server


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the constrained Big EVY MCP server")
    parser.add_argument(
        "--transport", choices=("stdio", "streamable-http"), default="stdio"
    )
    args = parser.parse_args(argv)
    server = create_mcp_server()
    if args.transport == "stdio":
        server.run()
    else:
        host = os.environ.get("BIG_EVY_MCP_HOST", "127.0.0.1")
        try:
            port = int(os.environ.get("BIG_EVY_MCP_PORT", "8006"))
        except ValueError as exc:
            raise MCPConfigurationError("BIG_EVY_MCP_PORT must be an integer") from exc
        if not 1 <= port <= 65_535:
            raise MCPConfigurationError("BIG_EVY_MCP_PORT must be between 1 and 65535")
        server.run(transport="streamable-http", host=host, port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
