"""Static safety contract for the connected OpenShift acquisition profile."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy" / "corpus-factory-acquisition"


def _documents(path):
    return [item for item in yaml.safe_load_all(path.read_text()) if item]


def test_controller_deployment_is_single_writer_bounded_and_non_privileged():
    deployment = _documents(DEPLOY / "base" / "deployment.yaml")[0]
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]
    args = container["args"]

    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"]["type"] == "Recreate"
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert args[args.index("--max-sources") + 1] == "32"
    assert args[args.index("--max-cycles") + 1] == "288"
    assert "--registry" in args and "--event-policy" in args


def test_network_policy_defaults_to_deny_and_opens_only_dns_and_https():
    policies = _documents(DEPLOY / "base" / "networkpolicy.yaml")
    deny, allow = policies
    assert deny["spec"]["policyTypes"] == ["Ingress", "Egress"]
    assert "egress" not in deny["spec"]
    ports = {
        (port["protocol"], port["port"])
        for rule in allow["spec"]["egress"]
        for port in rule["ports"]
    }
    assert ports == {("UDP", 53), ("TCP", 53), ("TCP", 443)}
    assert allow["spec"]["egress"][0]["to"][0]["namespaceSelector"]["matchLabels"] == {
        "kubernetes.io/metadata.name": "openshift-dns"
    }


def test_egress_firewall_allows_only_reviewed_https_host_then_denies_everything():
    firewall = _documents(
        DEPLOY / "profiles" / "openshift-connected" / "egressfirewall.yaml"
    )[0]
    rules = firewall["spec"]["egress"]

    assert firewall["metadata"]["name"] == "default"
    assert rules[0] == {
        "type": "Allow",
        "to": {"dnsName": "alerts.example.gov"},
        "ports": [{"protocol": "TCP", "port": 443}],
    }
    assert rules[-2]["to"]["cidrSelector"] == "0.0.0.0/0"
    assert rules[-1]["to"]["cidrSelector"] == "::/0"
