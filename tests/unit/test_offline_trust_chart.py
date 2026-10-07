import shutil
import subprocess

import pytest
import yaml


def test_offline_trust_field_profile_wires_only_bounded_files_and_settings():
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is required for the chart rendering contract")
    result = subprocess.run(
        [
            helm, "template", "lil-evy", "chart", "-f",
            "chart/profiles/values-field-offline-trust.yaml",
        ],
        check=True, capture_output=True, text=True,
    )
    objects = [item for item in yaml.safe_load_all(result.stdout) if isinstance(item, dict)]
    deployment = next(
        item for item in objects
        if item.get("kind") == "Deployment"
        and item.get("metadata", {}).get("name") == "lil-evy-rag-service"
    )
    container = next(
        item for item in deployment["spec"]["template"]["spec"]["containers"]
        if item["name"] == "rag-service"
    )
    env = {item["name"]: item["value"] for item in container["env"]}
    assert env["OFFLINE_TRUST_ENABLED"] == "true"
    assert env["OFFLINE_TRUST_SITE_ID"] == "field-site-alpha"
    assert env["OFFLINE_TRUST_MAX_SNAPSHOT_AGE_SECONDS"] == "3600"
    assert "PRIVATE" not in " ".join(env)
    volumes = {item["name"]: item for item in deployment["spec"]["template"]["spec"]["volumes"]}
    assert volumes["offline-trust-authorities"]["secret"]["secretName"] \
        == "lil-evy-offline-trust-authorities"
    assert volumes["offline-trust-evidence"]["configMap"]["name"] \
        == "lil-evy-offline-trust-evidence"


def test_offline_trust_cannot_enable_without_activation():
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is required for the chart rendering contract")
    result = subprocess.run(
        [
            helm, "template", "lil-evy", "chart",
            "--set", "rag.offlineTrust.enabled=true",
            "--set", "rag.offlineTrust.siteId=site-alpha",
            "--set", "rag.offlineTrust.trustGeneration=1",
            "--set", "rag.offlineTrust.maxSnapshotAgeSeconds=60",
            "--set", "rag.offlineTrust.timeKeyId=time-authority-01",
            "--set", "rag.offlineTrust.revocationKeyId=revocation-authority-01",
            "--set", "rag.offlineTrust.releaseSigningKeyId=release-signing-01",
            "--set", "rag.offlineTrust.authoritySecretName=authority-keys",
            "--set", "rag.offlineTrust.evidenceConfigMapName=signed-evidence",
        ],
        check=False, capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert (
        "rag.activation.enabled" in result.stderr
        or "/rag/activation/enabled" in result.stderr
    )
    assert "true" in result.stderr


def test_default_lab_chart_does_not_mount_offline_trust_material():
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is required for the chart rendering contract")
    result = subprocess.run(
        [helm, "template", "lil-evy", "chart"],
        check=True, capture_output=True, text=True,
    )
    assert "OFFLINE_TRUST_ENABLED" not in result.stdout
    assert "offline-trust-authorities" not in result.stdout
