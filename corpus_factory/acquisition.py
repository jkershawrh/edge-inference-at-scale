"""Policy-bound connected acquisition for the Big EVY evidence vault.

The registry is configuration, not authority by itself: it is validated and
digest-bound before an enabled connector can fetch. HTTPS redirects are handled
explicitly so every hop is checked against the same host and network policy.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlparse

from corpus_factory.core import EvidenceSnapshot, EvidenceVault, SourceRecordSpec, build_source_record
from corpus_factory.validator import validate_instance


REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")


class AcquisitionError(ValueError):
    """The registry or remote response violated acquisition policy."""


@dataclass(frozen=True)
class FetchResponse:
    status: int
    final_url: str
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True)
class AcquisitionResult:
    snapshot: EvidenceSnapshot
    report: Dict[str, Any]


Resolver = Callable[[str, int], Sequence[str]]
Transport = Callable[[str, int, int], FetchResponse]


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def source_registry_digest(registry: Mapping[str, Any]) -> str:
    validate_instance(registry, "source_registry")
    return "sha256:" + hashlib.sha256(_canonical_bytes(registry)).hexdigest()


def validate_registry_policy_alignment(
    registry: Mapping[str, Any], event_policy: Mapping[str, Any]
) -> None:
    """Prove every enabled connector is authorized by the event policy."""

    validate_instance(registry, "source_registry")
    validate_instance(event_policy, "event_policy")
    if registry["event_id"] != event_policy["event_id"]:
        raise AcquisitionError("source registry event does not match event policy")
    deployment = event_policy["deployment_scope"]
    authorities = {
        source_id: authority
        for authority in event_policy["authority_registry"]
        for source_id in authority["source_ids"]
    }
    for entry in registry["sources"]:
        if not entry["enabled"]:
            continue
        authority = authorities.get(entry["source_id"])
        if authority is None:
            raise AcquisitionError("enabled source is not authorized by event policy")
        if entry["publisher"]["id"] not in authority["publisher_ids"]:
            raise AcquisitionError("source publisher is not authorized by event policy")
        if entry["authority_class"] != authority["authority_class"]:
            raise AcquisitionError("source authority class does not match event policy")
        scope = entry["scope"]
        if not set(scope["subjects"]).issubset(authority["subjects"]):
            raise AcquisitionError("source subjects exceed event-policy authority")
        if not set(scope["geographies"]).issubset(authority["geographies"]):
            raise AcquisitionError("source geographies exceed event-policy authority")
        for source_field, deployment_field in (("geographies", "geographies"), ("languages", "languages"), ("audiences", "audiences")):
            if not set(scope[source_field]).issubset(deployment[deployment_field]):
                raise AcquisitionError("source scope exceeds event deployment scope")


def _default_resolver(host: str, port: int) -> Sequence[str]:
    try:
        return sorted({item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
    except socket.gaierror as exc:
        raise AcquisitionError("source host cannot be resolved") from exc


def validate_https_url(
    url: str, allowed_hosts: Sequence[str], resolver: Resolver = _default_resolver
) -> Tuple[str, Tuple[str, ...]]:
    """Validate URL shape, exact host allowlist, and globally routable DNS answers."""

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    allowed = {item.lower() for item in allowed_hosts}
    if parsed.scheme != "https" or not host or parsed.port not in (None, 443):
        raise AcquisitionError("connector URL must use HTTPS on port 443")
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise AcquisitionError("connector URL cannot contain credentials or fragments")
    if host not in allowed:
        raise AcquisitionError("connector host is not allowlisted")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise AcquisitionError("literal IP connector hosts are not permitted")

    addresses = tuple(resolver(host, 443))
    if not addresses:
        raise AcquisitionError("source host resolved to no addresses")
    for address in addresses:
        try:
            parsed_address = ipaddress.ip_address(address)
        except ValueError as exc:
            raise AcquisitionError("resolver returned an invalid address") from exc
        if not parsed_address.is_global:
            raise AcquisitionError("source host resolves to a non-public address")
    return host, addresses


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # pragma: no cover - stdlib hook
        return None


def urllib_transport(url: str, timeout_seconds: int, maximum_bytes: int) -> FetchResponse:
    """Fetch one HTTPS hop without ambient proxies or automatic redirects."""

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json,text/plain,text/markdown,application/pdf", "User-Agent": "evy-corpus-factory/1"},
        method="GET",
    )
    try:
        response = opener.open(request, timeout=timeout_seconds)
    except urllib.error.HTTPError as exc:
        response = exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AcquisitionError("HTTPS acquisition failed") from exc
    try:
        body = response.read(maximum_bytes + 1) if response.status not in REDIRECT_STATUSES else b""
        headers = {key.lower(): value for key, value in response.headers.items()}
        return FetchResponse(int(response.status), str(response.geturl()), headers, body)
    finally:
        response.close()


def _source_spec(registry: Mapping[str, Any], entry: Mapping[str, Any], observed_at: str) -> SourceRecordSpec:
    publisher = entry["publisher"]
    steward = entry["steward"]
    rights = entry["rights"]
    scope = entry["scope"]
    freshness = entry["freshness"]
    connector = entry["connector"]
    return SourceRecordSpec(
        source_id=entry["source_id"],
        publisher_id=publisher["id"],
        publisher_name=publisher["name"],
        steward_identity=steward["identity"],
        steward_role=steward["role"],
        authority_class=entry["authority_class"],
        connector_id=connector["connector_id"],
        acquisition_policy_version=registry["acquisition_policy_version"],
        acquired_at=observed_at,
        last_verified_at=observed_at,
        license=rights["license"],
        redistribution=rights["redistribution"],
        restrictions=tuple(rights["restrictions"]),
        geographies=tuple(scope["geographies"]),
        languages=tuple(scope["languages"]),
        audiences=tuple(scope["audiences"]),
        subjects=tuple(scope["subjects"]),
        effective_from=freshness["effective_from"],
        valid_until=freshness["valid_until"],
        expected_refresh_seconds=freshness["expected_refresh_seconds"],
        stale_action=freshness["stale_action"],
        sensitivity=entry["sensitivity"],
        distribution=entry["distribution"],
    )


def acquire_registry_source(
    registry: Mapping[str, Any],
    event_policy: Mapping[str, Any],
    source_id: str,
    *,
    evidence_store: Path,
    observed_at: str,
    previous_digest: Optional[str] = None,
    transport: Transport = urllib_transport,
    resolver: Resolver = _default_resolver,
) -> AcquisitionResult:
    """Fetch one enabled registry source and produce immutable evidence plus a change report."""

    validate_registry_policy_alignment(registry, event_policy)
    try:
        observed_time = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise AcquisitionError("observed_at must be an ISO-8601 time") from exc
    if observed_time.tzinfo is None:
        raise AcquisitionError("observed_at must include a timezone")
    if previous_digest is not None and not _DIGEST.fullmatch(previous_digest):
        raise AcquisitionError("previous_digest must be a sha256 digest")
    registry_digest = source_registry_digest(registry)
    matching = [item for item in registry["sources"] if item["source_id"] == source_id]
    if len(matching) != 1:
        raise AcquisitionError("source ID is not uniquely registered")
    entry = matching[0]
    if not entry["enabled"]:
        raise AcquisitionError("source connector is disabled")
    connector = entry["connector"]
    if connector.get("auth_secret_ref") is not None:
        raise AcquisitionError("authenticated connectors require an external secret provider")

    maximum_bytes = connector["maximum_bytes"]
    timeout_seconds = connector["timeout_seconds"]
    allowed_hosts = connector["allowed_hosts"]
    current_url = connector["url"]
    response: Optional[FetchResponse] = None
    redirect_chain = []
    for attempt in range(connector["maximum_redirects"] + 1):
        validate_https_url(current_url, allowed_hosts, resolver)
        response = transport(current_url, timeout_seconds, maximum_bytes)
        if response.status not in REDIRECT_STATUSES:
            break
        location = next(
            (value for key, value in response.headers.items() if key.lower() == "location"),
            None,
        )
        if not location:
            raise AcquisitionError("redirect response omitted Location")
        if attempt >= connector["maximum_redirects"]:
            raise AcquisitionError("redirect limit exceeded")
        next_url = urljoin(current_url, location)
        validate_https_url(next_url, allowed_hosts, resolver)
        redirect_chain.append(next_url)
        current_url = next_url

    if response is None or response.status != 200:
        raise AcquisitionError("source returned non-success status")
    validate_https_url(response.final_url, allowed_hosts, resolver)
    if response.final_url != current_url:
        raise AcquisitionError("transport followed an undeclared redirect")
    if not response.body:
        raise AcquisitionError("source returned an empty payload")
    if len(response.body) > maximum_bytes:
        raise AcquisitionError("source payload exceeds maximum_bytes")

    normalized_headers = {key.lower(): value for key, value in response.headers.items()}
    declared_length = normalized_headers.get("content-length")
    if declared_length is not None:
        try:
            length = int(declared_length)
        except ValueError as exc:
            raise AcquisitionError("source returned an invalid Content-Length") from exc
        if length != len(response.body) or length > maximum_bytes:
            raise AcquisitionError("source Content-Length does not match the bounded payload")
    media_type = normalized_headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type not in {item.lower() for item in connector["allowed_media_types"]}:
        raise AcquisitionError("source media type is not allowlisted")

    digest, evidence_path = EvidenceVault(evidence_store).store(response.body)
    record = build_source_record(
        _source_spec(registry, entry, observed_at),
        reference=connector["url"],
        locator_kind="url",
        acquisition_method="https",
        media_type=media_type,
        byte_size=len(response.body),
        digest=digest,
    )
    snapshot = EvidenceSnapshot(digest=digest, path=evidence_path, source_record=record)
    if previous_digest is None:
        change = "initial"
    elif previous_digest == digest:
        change = "unchanged"
    else:
        change = "changed"
    report_body = {
        "schema_version": "2.0.0",
        "record_type": "acquisition_report",
        "source_id": source_id,
        "event_id": registry["event_id"],
        "registry_digest": registry_digest,
        "connector_id": connector["connector_id"],
        "observed_at": observed_at,
        "final_url": response.final_url,
        "redirect_chain": redirect_chain,
        "previous_digest": previous_digest,
        "current_digest": digest,
        "change": change,
        "byte_size": len(response.body),
        "media_type": media_type,
    }
    report = {
        "report_id": "sha256:" + hashlib.sha256(_canonical_bytes(report_body)).hexdigest(),
        **report_body,
    }
    validate_instance(report, "acquisition_report")
    return AcquisitionResult(snapshot=snapshot, report=report)
