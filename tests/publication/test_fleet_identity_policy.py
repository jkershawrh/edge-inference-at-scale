import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
FLEET = ROOT / "deploy" / "gitops" / "lilevy-fleet"


def _yaml(path):
    return list(yaml.safe_load_all(path.read_text(encoding="utf-8")))


def _registry_from_policy():
    policy = _yaml(FLEET / "rhacm-policy.yaml")[0]
    templates = policy["spec"]["policy-templates"][0]["objectDefinition"]["spec"]["object-templates"]
    configmap = next(
        item["objectDefinition"] for item in templates
        if item["objectDefinition"]["kind"] == "ConfigMap"
    )
    return configmap, next(
        item for item in templates if item["objectDefinition"]["kind"] == "ConfigMap"
    )["complianceType"]


def test_gitops_and_rhacm_enforce_the_exact_same_public_registry():
    gitops = _yaml(FLEET / "base" / "node-registry.yaml")[0]
    policy, compliance = _registry_from_policy()
    assert compliance == "mustonlyhave"
    assert policy["data"]["nodes.json"] == gitops["data"]["nodes.json"]
    registry = json.loads(gitops["data"]["nodes.json"])
    assert set(registry) == {"schema_version", "nodes"}
    assert registry["nodes"]["field-001"]["enabled"] is True
    assert registry["nodes"]["field-002"]["enabled"] is False
    serialized = json.dumps(registry).upper()
    assert "PRIVATE KEY" not in serialized
    assert gitops["metadata"]["annotations"]["lilevy.edge/key-material"].startswith("synthetic")


def test_node_manager_registry_is_read_only_and_replay_state_is_persistent():
    deployment = _yaml(FLEET / "base" / "deployment.yaml")[0]
    pvc = _yaml(FLEET / "base" / "replay-pvc.yaml")[0]
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]
    mounts = {item["name"]: item for item in container["volumeMounts"]}
    volumes = {item["name"]: item for item in pod["volumes"]}
    env = {item["name"]: item["value"] for item in container["env"]}
    assert mounts["node-registry"]["readOnly"] is True
    assert volumes["node-registry"]["configMap"]["name"] == "lil-evy-node-registry"
    assert "persistentVolumeClaim" in volumes["replay-state"]
    assert volumes["replay-state"]["persistentVolumeClaim"]["claimName"] == pvc["metadata"]["name"]
    assert env["FLEET_AUTH_MODE"] == "required"
    assert env["FLEET_REPLAY_STATE_PATH"].startswith(mounts["replay-state"]["mountPath"] + "/")
    assert deployment["spec"]["strategy"]["type"] == "Recreate"


def test_base_has_no_route_and_defaults_to_same_namespace_ingress_only():
    resources = yaml.safe_load((FLEET / "base" / "kustomization.yaml").read_text())["resources"]
    assert not any("route" in item.lower() for item in resources)
    policy = _yaml(FLEET / "base" / "network-policy.yaml")[0]
    selector = policy["spec"]["ingress"][0]["from"][0]["namespaceSelector"]["matchLabels"]
    assert selector == {"kubernetes.io/metadata.name": "lil-evy-fleet"}


def test_controller_image_is_immutable_and_intentionally_fail_closed_until_overlaid():
    deployment = _yaml(FLEET / "base" / "deployment.yaml")[0]
    image = deployment["spec"]["template"]["spec"]["containers"][0]["image"]
    assert "@sha256:" in image and not image.endswith(":latest")
    assert image.endswith("0" * 64)
    assert deployment["metadata"]["annotations"]["lilevy.edge/image-material"] \
        == "immutable-placeholder-replace-before-deploy"


def test_fleet_manifests_render_with_kustomize():
    command = None
    if shutil.which("kubectl"):
        command = ["kubectl", "kustomize", str(FLEET / "base")]
    elif shutil.which("kustomize"):
        command = ["kustomize", "build", str(FLEET / "base")]
    if command is None:
        pytest.skip("kubectl or kustomize is required")
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    objects = [item for item in yaml.safe_load_all(result.stdout) if isinstance(item, dict)]
    assert {item["kind"] for item in objects} >= {
        "Namespace", "ConfigMap", "PersistentVolumeClaim", "Deployment",
        "Service", "NetworkPolicy",
    }
