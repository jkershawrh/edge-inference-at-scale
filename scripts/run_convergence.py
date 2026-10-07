#!/usr/bin/env python3
"""Execute the repository convergence matrix and retain attributable evidence."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml


ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = ROOT / "tests" / "validation_matrix.yaml"
STATUS_RANK = {"GREEN": 0, "AMBER": 1, "RED": 2}
SKIP_PATTERN = re.compile(r"(?:^|\s)(\d+) skipped(?:,|\s|$)", re.MULTILINE)
IDENTITY_KEYS = (
    "CORPUS_DIGEST",
    "CORPUS_MANIFEST_DIGEST",
    "CORPUS_MODE",
    "CORPUS_EVENT_ID",
    "CORPUS_VERSION",
    "EMBEDDING_MODEL",
    "GENERATION_ENABLED",
    "LLM_PROVIDER",
    "LLM_MODEL",
    "EDGE_RESOURCE_PROFILE",
    "CHANNEL_DRIVER",
)


def _run(
    command: list[str], extra_env: Optional[dict[str, str]] = None
) -> dict[str, Any]:
    env = os.environ.copy()
    env.update(extra_env or {})
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    print(completed.stdout, end="")
    skipped = sum(int(value) for value in SKIP_PATTERN.findall(completed.stdout))
    return {
        "command": command,
        "exit_code": completed.returncode,
        "skipped": skipped,
    }


def _command_environment(command_spec: dict[str, Any]) -> dict[str, str]:
    extra_env = dict(command_spec.get("env") or {})
    for target, source in (command_spec.get("env_from") or {}).items():
        value = os.environ.get(source)
        if value:
            extra_env[target] = value
    return extra_env


def _stage_status(
    results: list[dict[str, Any]], status_cap: Optional[str]
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if any(result["exit_code"] != 0 for result in results):
        return "RED", ["a required command failed"]
    skipped = sum(result["skipped"] for result in results)
    status = "GREEN"
    if skipped:
        status = "AMBER"
        reasons.append(f"{skipped} checks skipped")
    if status_cap and STATUS_RANK[status_cap] > STATUS_RANK[status]:
        status = status_cap
        reasons.append("evidence scope does not qualify this stage as green")
    return status, reasons


def _git_value(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else "unknown"


def run_matrix(profile: str) -> dict[str, Any]:
    matrix = yaml.safe_load(MATRIX_PATH.read_text(encoding="utf-8"))
    stages = []
    for stage_key, stage in matrix["stages"].items():
        execution = stage.get("execution", {})
        selected = execution.get(profile) or execution.get("local")
        print(f"\n=== {stage['id']}: {stage['name']} ===")
        if not selected or not selected.get("commands"):
            status = "AMBER"
            reason = (
                selected.get("reason", "required evidence has not been collected")
                if selected
                else "no execution contract"
            )
            reasons = [reason]
            results: list[dict[str, Any]] = []
        else:
            missing_env = [
                key for key in selected.get("required_env", []) if not os.environ.get(key)
            ]
            results = []
            if missing_env:
                status = "RED"
                reasons = ["missing required environment: " + ", ".join(missing_env)]
            else:
                for command_spec in selected["commands"]:
                    result = _run(
                        command_spec["argv"], _command_environment(command_spec)
                    )
                    results.append(result)
                    if result["exit_code"] != 0:
                        break
                status, reasons = _stage_status(results, selected.get("status_cap"))
            if selected.get("reason") and status != "GREEN":
                reasons.append(selected["reason"])
        suffix = f" — {'; '.join(reasons)}" if reasons else ""
        print(f"{stage['id']}: {status}{suffix}")
        stages.append(
            {
                "key": stage_key,
                "id": stage["id"],
                "name": stage["name"],
                "status": status,
                "reasons": reasons,
                "results": results,
            }
        )

    overall = max((stage["status"] for stage in stages), key=STATUS_RANK.get)
    report = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "profile": profile,
        "matrix": str(MATRIX_PATH.relative_to(ROOT)),
        "source": {
            "revision": _git_value("rev-parse", "HEAD"),
            "dirty": bool(_git_value("status", "--porcelain")),
        },
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "identities": {key: os.environ.get(key, "unknown") for key in IDENTITY_KEYS},
        },
        "overall": overall,
        "stages": stages,
    }
    output = ROOT / "artifacts" / f"convergence-{profile}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"\nOVERALL: {overall}")
    print(f"Evidence: {output.relative_to(ROOT)}")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("local", "openshift"), default="local")
    parser.add_argument("--require-green", action="store_true")
    args = parser.parse_args()
    report = run_matrix(args.profile)
    if report["overall"] == "RED":
        return 1
    if args.require_green and report["overall"] != "GREEN":
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
