"""Fail-closed contract for GitOps/RHACM rollout annotation ownership."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy" / "gitops" / "lilevy-rollout"
RUNTIME_ANNOTATIONS = {
    "lilevy.edge/restart-operation-id",
    "lilevy.edge/target-digest",
    "lilevy.edge/target-sequence",
}


def _documents(path):
    return [document for document in yaml.safe_load_all(path.read_text()) if document]


def _policy_templates(policy):
    return [item["objectDefinition"] for item in policy["spec"]["policy-templates"]]


def test_argocd_preserves_exact_controller_owned_runtime_annotations():
    application = _documents(DEPLOY / "application.yaml")[0]
    assert application["kind"] == "Application"
    assert "RespectIgnoreDifferences=true" in application["spec"]["syncPolicy"]["syncOptions"]
    ignored = application["spec"]["ignoreDifferences"]
    assert len(ignored) == 1
    assert ignored[0]["group"] == "apps"
    assert ignored[0]["kind"] == "Deployment"
    assert ignored[0]["name"] == "lil-evy-rag-service"
    assert ignored[0]["namespace"] == "lil-evy-field"
    pointers = set(ignored[0]["jsonPointers"])
    assert pointers == {
        "/spec/template/metadata/annotations/" + key.replace("/", "~1")
        for key in RUNTIME_ANNOTATIONS
    }


def test_admission_policy_denies_annotation_changes_by_every_other_writer():
    policy, binding = _documents(DEPLOY / "annotation-ownership.yaml")
    assert policy["spec"]["failurePolicy"] == "Fail"
    assert binding["spec"]["validationActions"] == ["Deny"]
    assert binding["spec"]["matchResources"]["namespaceSelector"]["matchLabels"] == {
        "lilevy.edge/rollout-protected": "true"
    }
    validations = policy["spec"]["validations"]
    expressions = "\n".join(item["expression"] for item in validations)
    assert len(validations) == 3
    assert (
        "system:serviceaccount:lil-evy-field:lil-evy-rollout-controller"
        in expressions
    )
    for annotation in RUNTIME_ANNOTATIONS:
        assert annotation in expressions
    assert all("oldAnnotations" in item["expression"] for item in validations)
    assert all("newAnnotations" in item["expression"] for item in validations)


def test_rhacm_policy_enforces_same_admission_spec_and_declarative_release_identity():
    policy, placement, binding = _documents(DEPLOY / "rhacm-policy.yaml")
    templates = _policy_templates(policy)
    desired_policy, ownership_policy = templates
    desired_objects = [
        item["objectDefinition"]
        for item in desired_policy["spec"]["object-templates"]
    ]
    namespace, desired = desired_objects
    objects = [
        item["objectDefinition"]
        for item in ownership_policy["spec"]["object-templates"]
    ]
    standalone = _documents(DEPLOY / "annotation-ownership.yaml")

    assert policy["spec"]["remediationAction"] == "enforce"
    assert namespace["metadata"]["labels"] == {
        "lilevy.edge/rollout-protected": "true"
    }
    assert desired["kind"] == "ConfigMap"
    assert desired["data"] == {
        "event-id": "region4-flood-2026",
        "release-digest": "sha256:" + "a" * 64,
        "release-sequence": "7",
        "policy-digest": "sha256:" + "b" * 64,
        "rollout-ring": "canary",
    }
    assert objects[0]["spec"] == standalone[0]["spec"]
    assert objects[1]["spec"] == standalone[1]["spec"]
    assert all(item["kind"] != "Deployment" for item in objects)
    expressions = placement["spec"]["predicates"][0]["requiredClusterSelector"]["labelSelector"]["matchExpressions"]
    assert {item["key"] for item in expressions} == {
        "lilevy.edge/managed",
        "lilevy.edge/event",
        "lilevy.edge/rollout-ring",
    }
    assert binding["placementRef"]["kind"] == "Placement"
    assert binding["subjects"][0]["name"] == policy["metadata"]["name"]


def test_release_values_match_rhacm_inventory_and_never_declare_runtime_annotations():
    values = yaml.safe_load(
        (ROOT / "chart" / "profiles" / "values-gitops-release.yaml").read_text()
    )
    policy = _documents(DEPLOY / "rhacm-policy.yaml")[0]
    desired = _policy_templates(policy)[0]["spec"]["object-templates"][1]["objectDefinition"]
    rollout = values["rag"]["activation"]["rollout"]

    assert values["rag"]["corpus"]["eventId"] == desired["data"]["event-id"]
    assert rollout["targetDigest"] == desired["data"]["release-digest"]
    assert str(rollout["targetSequence"]) == desired["data"]["release-sequence"]
    assert values["rag"]["activation"]["operator"]["policyDigest"] == desired["data"]["policy-digest"]
    rendered_values = str(values)
    assert not any(annotation in rendered_values for annotation in RUNTIME_ANNOTATIONS)
