#!/usr/bin/env python3
"""Render read-only lifecycle and partial-cleanup recovery advice."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import disk_budget as hygiene  # noqa: E402


SCHEMA_VERSION = 1
FINISH_READINESS_KIND = "dev-disk-budget-finish-readiness"
RECOVERY_PLAN_KIND = "dev-disk-budget-recovery-plan"


class RecoveryError(ValueError):
    """Raised when recovery evidence is incomplete or inconsistent."""


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def load_object(path: Path, field: str) -> tuple[bytes, dict[str, Any]]:
    try:
        content = path.read_bytes()
    except OSError as error:
        raise RecoveryError(f"cannot read {field} {path}: {error}") from error
    try:
        value = json.loads(content)
    except json.JSONDecodeError as error:
        raise RecoveryError(f"cannot parse {field} {path}: {error}") from error
    if not isinstance(value, dict):
        raise RecoveryError(f"{field} must be an object")
    return content, value


def canonical_existing_path(value: str | Path, field: str) -> Path:
    supplied = Path(value).expanduser()
    if supplied.is_symlink():
        raise RecoveryError(f"{field} must be an existing non-symlink path")
    path = supplied.resolve()
    if not path.exists():
        raise RecoveryError(f"{field} must be an existing non-symlink path")
    return path


def resource_cohort(resource: dict[str, Any]) -> str:
    if resource["cleanup_policy"] == "report_only":
        return "intentionally_retained"
    if (
        resource["kind"] == "git_worktree"
        and not resource["cleanup_authorized"]
    ):
        return "authorization_pending"
    return "ordinary_disposable"


def finish_readiness(manifest_path: Path) -> dict[str, Any]:
    manifest_path = canonical_existing_path(manifest_path, "manifest")
    manifest_bytes, raw_manifest = load_object(manifest_path, "manifest")
    try:
        manifest = hygiene.validate_manifest(raw_manifest)
    except (hygiene.HygieneError, ValueError) as error:
        raise RecoveryError(f"invalid manifest: {error}") from error

    cohorts: dict[str, list[str]] = {
        "ordinary_disposable": [],
        "authorization_pending": [],
        "intentionally_retained": [],
    }
    for resource in manifest["resources"]:
        cohorts[resource_cohort(resource)].append(resource["id"])
    for resource_ids in cohorts.values():
        resource_ids.sort()

    blockers: list[dict[str, str]] = []
    if manifest["status"] != "active":
        blockers.append(
            {
                "code": "MANIFEST_NOT_ACTIVE",
                "message": (
                    "finish readiness applies only to an active manifest; "
                    f"observed '{manifest['status']}'"
                ),
            }
        )
    for resource_id in cohorts["authorization_pending"]:
        blockers.append(
            {
                "code": "WORKTREE_CLEANUP_NOT_AUTHORIZED",
                "resource_id": resource_id,
                "message": (
                    f"disposable worktree '{resource_id}' lacks cleanup "
                    "authorization"
                ),
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": FINISH_READINESS_KIND,
        "status": "ready" if not blockers else "blocked",
        "manifest_path": str(manifest_path),
        "manifest_sha256": sha256_bytes(manifest_bytes),
        "run_id": manifest["run_id"],
        "run_status": manifest["status"],
        "cohorts": cohorts,
        "blockers": blockers,
        "advice": {
            "current_manifest": (
                "finish succeeded only after every disposable worktree is "
                "authorized; otherwise finish failed or interrupted, "
                "investigate, then explicitly release only when safe"
            ),
            "future_manifests": (
                "place ordinary workers, read-only or migrated fixtures, and "
                "authorization-pending resources in separate manifests when "
                "they cannot share one terminal status and preservation policy"
            ),
        },
        "mutation_authorized": False,
    }


def validate_recovery_documents(
    manifest_path: Path,
    plan_path: Path,
    receipt_path: Path,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    str,
    str,
]:
    manifest_path = canonical_existing_path(manifest_path, "manifest")
    plan_path = canonical_existing_path(plan_path, "plan")
    receipt_path = canonical_existing_path(receipt_path, "receipt")
    manifest_bytes, raw_manifest = load_object(manifest_path, "manifest")
    plan_bytes, plan = load_object(plan_path, "plan")
    _, receipt = load_object(receipt_path, "receipt")
    try:
        manifest = hygiene.validate_manifest(raw_manifest)
    except (hygiene.HygieneError, ValueError) as error:
        raise RecoveryError(f"invalid manifest: {error}") from error

    if plan.get("schema_version") != SCHEMA_VERSION:
        raise RecoveryError("plan schema_version must be 1")
    if plan.get("kind") != hygiene.PLAN_KIND:
        raise RecoveryError(f"plan kind must be {hygiene.PLAN_KIND}")
    if receipt.get("schema_version") != SCHEMA_VERSION:
        raise RecoveryError("receipt schema_version must be 1")
    if receipt.get("kind") != hygiene.RECEIPT_KIND:
        raise RecoveryError(f"receipt kind must be {hygiene.RECEIPT_KIND}")
    if receipt.get("status") != "recovery_required":
        raise RecoveryError("receipt status must be recovery_required")

    manifest_sha256 = sha256_bytes(manifest_bytes)
    plan_sha256 = sha256_bytes(plan_bytes)
    if plan.get("manifest_sha256") != manifest_sha256:
        raise RecoveryError("plan does not bind the supplied manifest bytes")
    if receipt.get("plan_sha256") != plan_sha256:
        raise RecoveryError("receipt does not bind the supplied plan bytes")
    if Path(str(plan.get("manifest_path"))).expanduser().resolve() != manifest_path:
        raise RecoveryError("plan manifest_path does not match supplied manifest")
    if Path(str(receipt.get("manifest_path"))).expanduser().resolve() != manifest_path:
        raise RecoveryError("receipt manifest_path does not match supplied manifest")
    if Path(str(receipt.get("plan_path"))).expanduser().resolve() != plan_path:
        raise RecoveryError("receipt plan_path does not match supplied plan")
    if not (
        manifest["run_id"] == plan.get("run_id") == receipt.get("run_id")
    ):
        raise RecoveryError("manifest, plan, and receipt run_id values differ")
    if plan.get("run_status") != manifest["status"]:
        raise RecoveryError("plan run_status does not match manifest")

    manifest_ids = [resource["id"] for resource in manifest["resources"]]
    planned_resources = plan.get("resources")
    if not isinstance(planned_resources, list):
        raise RecoveryError("plan resources must be an array")
    plan_ids: list[str] = []
    for index, resource in enumerate(planned_resources):
        if not isinstance(resource, dict):
            raise RecoveryError(f"plan resource {index} must be an object")
        resource_id = resource.get("id")
        if not isinstance(resource_id, str) or not resource_id:
            raise RecoveryError(f"plan resource {index} has invalid id")
        if resource_id in plan_ids:
            raise RecoveryError(f"plan contains duplicate resource '{resource_id}'")
        if not isinstance(resource.get("eligible"), bool):
            raise RecoveryError(
                f"plan resource '{resource_id}' eligible must be boolean"
            )
        plan_ids.append(resource_id)
    if sorted(plan_ids) != sorted(manifest_ids):
        raise RecoveryError("plan resource set does not match manifest")

    return manifest, plan, receipt, manifest_sha256, plan_sha256


def recovery_plan(
    manifest_path: Path,
    plan_path: Path,
    receipt_path: Path,
) -> dict[str, Any]:
    (
        manifest,
        plan,
        receipt,
        manifest_sha256,
        plan_sha256,
    ) = validate_recovery_documents(manifest_path, plan_path, receipt_path)

    planned_resources = plan["resources"]
    eligible_ids = [
        resource["id"] for resource in planned_resources if resource["eligible"]
    ]
    results = receipt.get("results")
    if not isinstance(results, list):
        raise RecoveryError("receipt results must be an array")
    if not results:
        raise RecoveryError(
            "recovery_required receipt must contain a failed result"
        )
    result_ids: list[str] = []
    failed_ids: list[str] = []
    for index, result in enumerate(results):
        if not isinstance(result, dict):
            raise RecoveryError(f"receipt result {index} must be an object")
        resource_id = result.get("id")
        status = result.get("status")
        if resource_id in result_ids:
            raise RecoveryError(f"receipt contains duplicate result '{resource_id}'")
        if resource_id not in eligible_ids:
            raise RecoveryError(
                f"receipt result '{resource_id}' was not eligible in the reviewed plan"
            )
        if status not in {"failed", "removed"}:
            raise RecoveryError(
                f"receipt result '{resource_id}' has unsupported status"
            )
        if status == "failed":
            failed_ids.append(resource_id)
        result_ids.append(resource_id)
    if result_ids != eligible_ids[: len(result_ids)]:
        raise RecoveryError(
            "receipt results are not the executed prefix of eligible plan resources"
        )
    if len(failed_ids) != 1 or results[-1].get("status") != "failed":
        raise RecoveryError(
            "recovery_required receipt must end with exactly one failed resource"
        )
    failed_current = failed_ids[0]
    if receipt.get("current_resource") != failed_current:
        raise RecoveryError("receipt current_resource does not match failed result")

    removed = [
        result["id"] for result in results if result["status"] == "removed"
    ]
    remaining = [
        resource_id
        for resource_id in eligible_ids
        if resource_id not in result_ids
    ]
    intentionally_retained = [
        resource["id"]
        for resource in planned_resources
        if not resource["eligible"]
        and next(
            item
            for item in manifest["resources"]
            if item["id"] == resource["id"]
        )["cleanup_policy"]
        == "report_only"
    ]
    blocked_remaining = [
        resource["id"]
        for resource in planned_resources
        if not resource["eligible"]
        and resource["id"] not in intentionally_retained
    ]

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": RECOVERY_PLAN_KIND,
        "status": "review_required",
        "run_id": manifest["run_id"],
        "manifest_sha256": manifest_sha256,
        "reviewed_plan_sha256": plan_sha256,
        "source_receipt": str(receipt_path.expanduser().resolve()),
        "already_removed": sorted(removed),
        "failed_current": failed_current,
        "eligible_remaining": remaining,
        "blocked_remaining": sorted(blocked_remaining),
        "intentionally_retained": sorted(intentionally_retained),
        "current_quarantine": receipt.get("current_quarantine"),
        "next_steps": [
            "verify the failed resource and any quarantine against live filesystem and Git state",
            "preserve or repair the failed resource without changing the original receipt",
            "generate a fresh cleanup plan for still-present eligible resources",
            "review and confirm the fresh plan hash before any later cleanup execution",
        ],
        "warnings": [
            "already_removed is a cleanup-receipt claim and must not be replayed",
            "eligible_remaining is not deletion authority",
            "the original cleanup plan must never be rerun as fresh",
        ],
        "mutation_authorized": False,
    }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    subparsers = root.add_subparsers(dest="command", required=True)
    readiness = subparsers.add_parser(
        "finish-readiness",
        help="Explain whether one active manifest can finish succeeded.",
    )
    readiness.add_argument("--manifest", required=True)
    readiness.add_argument("--output")
    recovery = subparsers.add_parser(
        "recovery-plan",
        help="Classify immutable partial-cleanup evidence without mutation.",
    )
    recovery.add_argument("--manifest", required=True)
    recovery.add_argument("--plan", required=True)
    recovery.add_argument("--receipt", required=True)
    recovery.add_argument("--output")
    return root


def write_output(document: dict[str, Any], output: str | None) -> None:
    content = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if output:
        path = Path(output).expanduser()
        if path.exists() or path.is_symlink():
            raise RecoveryError(f"refusing to overwrite output {path}")
        path.write_text(content, encoding="utf-8")
        return
    sys.stdout.write(content)


def main() -> int:
    arguments = parser().parse_args()
    try:
        if arguments.command == "finish-readiness":
            result = finish_readiness(Path(arguments.manifest))
        else:
            result = recovery_plan(
                Path(arguments.manifest),
                Path(arguments.plan),
                Path(arguments.receipt),
            )
        write_output(result, arguments.output)
        if arguments.command == "recovery-plan":
            return 0
        return 0 if result["status"] == "ready" else 1
    except (RecoveryError, OSError) as error:
        sys.stdout.write(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "kind": "dev-disk-budget-recovery-error",
                    "status": "blocked",
                    "error": str(error),
                    "mutation_authorized": False,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
