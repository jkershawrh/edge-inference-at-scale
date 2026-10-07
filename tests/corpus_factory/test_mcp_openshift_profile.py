"""Static safety contract for the connected OpenShift MCP profile."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy" / "corpus-factory-mcp" / "base"


def _documents(path):
    return [item for item in yaml.safe_load_all(path.read_text()) if item]


def test_mcp_pod_is_bounded_non_privileged_and_has_no_cluster_identity():
    deployment = _documents(DEPLOY / "deployment.yaml")[0]
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]

    assert deployment["spec"]["replicas"] == 1
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"] == {
        "runAsNonRoot": True,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "readOnlyRootFilesystem": True,
    }
    assert container["resources"] == {
        "requests": {"cpu": "50m", "memory": "128Mi"},
        "limits": {"cpu": "250m", "memory": "256Mi"},
    }
    assert "@sha256:" in container["image"]
    assert container["image"].endswith("0" * 64)


def test_mcp_has_no_public_route_and_default_denies_all_egress():
    assert not list(DEPLOY.glob("*route*"))
    service = _documents(DEPLOY / "service.yaml")[0]
    assert service["spec"]["type"] == "ClusterIP"

    deny, allow = _documents(DEPLOY / "networkpolicy.yaml")
    assert deny["spec"]["policyTypes"] == ["Ingress", "Egress"]
    assert "ingress" not in deny["spec"] and "egress" not in deny["spec"]
    assert allow["spec"]["policyTypes"] == ["Ingress"]
    peer = allow["spec"]["ingress"][0]["from"][0]
    assert peer == {
        "podSelector": {
            "matchLabels": {"lilevy.edge/mcp-client": "approved"}
        }
    }


def test_kustomization_declares_every_mcp_resource():
    value = _documents(DEPLOY / "kustomization.yaml")[0]
    assert set(value["resources"]) == {
        "deployment.yaml",
        "service.yaml",
        "networkpolicy.yaml",
    }
