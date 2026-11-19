#!/usr/bin/env python3
"""Regression tests for the delivery evidence epoch contract."""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("delivery_evidence_epoch.py")
SPEC = importlib.util.spec_from_file_location("delivery_evidence_epoch", SCRIPT)
assert SPEC and SPEC.loader
CONTRACT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONTRACT)

IMPLEMENTATION_SHA = "1" * 40
EVIDENCE_SHA = "2" * 40
OTHER_SHA = "3" * 40
TREE_SHA256 = "4" * 64
DIFF_SHA256 = "5" * 64
CHECKOUT_SHA256 = "6" * 64


def named_hash(name: str, character: str) -> dict[str, str]:
    return {"name": name, "sha256": character * 64}


def command(
    identifier: str, authority_kind: str, target: str, profile: str
) -> dict[str, object]:
    return {
        "id": identifier,
        "argv": ["tool", identifier, "--profile", profile],
        "authority_kind": authority_kind,
        "target": target,
        "profile": profile,
    }


def receipt(
    identifier: str,
    authority_kind: str,
    target: str,
    profile: str,
    input_character: str,
    output_character: str,
) -> dict[str, object]:
    inputs = [named_hash("authoritative-input", input_character)]
    return {
        "id": identifier,
        "source_dependency": "source_dependent",
        "source_sha": IMPLEMENTATION_SHA,
        "authoritative_inputs_sha256": CONTRACT.canonical_sha256(inputs),
        "target": target,
        "profile": profile,
        "bounded_claim": f"{identifier} passed for the declared target and profile",
        "reuse_class": "implementation_bound",
        "authority": {
            "kind": authority_kind,
            "locator": f"receipt://{identifier}",
        },
        "plan_command_id": identifier,
        "input_hashes": inputs,
        "output_hashes": [named_hash("result", output_character)],
        "passed": True,
        "independence_basis": None,
    }


def base_packet() -> dict[str, object]:
    repository_identity = {
        "repository_id": "code.example.org/team/project",
        "checkout_sha256": CHECKOUT_SHA256,
    }
    plan = {
        "id": "release-plan",
        "established_at_revision": IMPLEMENTATION_SHA,
        "repository_identity": repository_identity,
        "commands": [
            command("local-unit", "local_runner", "darwin-arm64", "unit"),
            command("remote-ci", "remote_ci", "repo/pr-42", "required"),
            command(
                "compose",
                "artifact_generator",
                "release-bundle",
                "production",
            ),
            command(
                "release",
                "release_authority",
                "registry/stable",
                "production",
            ),
            command("revision-proof", "git", "repo/pr-42", "evidence-only"),
        ],
        "environment_policy": {
            "id": "env-prod",
            "description": "Pinned toolchain and production-compatible target",
        },
        "sample_policy": {
            "id": "sample-full",
            "description": "All required checks and the complete release bundle",
        },
        "evidence_paths": ["evidence/**"],
    }
    packet: dict[str, object] = {
        "schema_version": 1,
        "current_source_sha": EVIDENCE_SHA,
        "implementation_revision": IMPLEMENTATION_SHA,
        "evidence_revision": EVIDENCE_SHA,
        "matrix": {
            "local_gates": [
                receipt(
                    "local-unit",
                    "local_runner",
                    "darwin-arm64",
                    "unit",
                    "a",
                    "b",
                )
            ],
            "remote_checks": [
                receipt(
                    "remote-ci",
                    "remote_ci",
                    "repo/pr-42",
                    "required",
                    "c",
                    "d",
                )
            ],
            "composition_generated_artifacts": [
                receipt(
                    "compose",
                    "artifact_generator",
                    "release-bundle",
                    "production",
                    "e",
                    "f",
                )
            ],
            "release_receipts": [
                receipt(
                    "release",
                    "release_authority",
                    "registry/stable",
                    "production",
                    "6",
                    "7",
                )
            ],
        },
        "revision_binding": {
            "relationship": "later_evidence_revision",
            "implementation_revision": IMPLEMENTATION_SHA,
            "evidence_revision": EVIDENCE_SHA,
            "immutable_plan": plan,
            "immutable_plan_sha256": "0" * 64,
            "immutable_commands_sha256": "0" * 64,
            "environment_policy_sha256": "0" * 64,
            "sample_policy_sha256": "0" * 64,
            "inputs_sha256": "0" * 64,
            "outputs_sha256": "0" * 64,
            "authority_ledger_sha256": "0" * 64,
            "evidence_only_diff": {
                "implementation_revision": IMPLEMENTATION_SHA,
                "evidence_revision": EVIDENCE_SHA,
                "implementation_is_ancestor": True,
                "source_paths_changed": False,
                "changed_paths": ["evidence/delivery.json"],
                "source_tree_sha256_at_implementation": TREE_SHA256,
                "source_tree_sha256_at_evidence": TREE_SHA256,
                "diff_sha256": DIFF_SHA256,
                "repository_identity": repository_identity,
                "target": "repo/pr-42",
                "profile": "evidence-only",
                "authority": {
                    "kind": "git",
                    "locator": "git://implementation..evidence",
                },
                "plan_command_id": "revision-proof",
                "input_hashes": [
                    {
                        "name": "repository-identity",
                        "sha256": CONTRACT.canonical_sha256(repository_identity),
                    },
                    {
                        "name": "implementation-source-tree",
                        "sha256": TREE_SHA256,
                    },
                    {"name": "evidence-source-tree", "sha256": TREE_SHA256},
                ],
                "output_hashes": [
                    {"name": "diff", "sha256": DIFF_SHA256},
                ],
            },
        },
    }
    return seal(packet)


def seal(packet: dict[str, object]) -> dict[str, object]:
    result = CONTRACT.validate_packet(packet)
    binding = packet["revision_binding"]
    assert isinstance(binding, dict)
    binding.update(result["derived_digests"])
    return packet


def blockers(packet: dict[str, object]) -> list[str]:
    return CONTRACT.validate_packet(packet)["blockers"]


class DeliveryEvidenceEpochTests(unittest.TestCase):
    def test_later_evidence_revision_binds_complete_delivery_matrix(self) -> None:
        result = CONTRACT.validate_packet(base_packet())

        self.assertTrue(result["ready"])
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["measured_source_sha"], IMPLEMENTATION_SHA)
        self.assertEqual(result["counts"]["source_dependent_receipts"], 4)
        self.assertEqual(
            sorted(result["matrix"]),
            [
                "composition_generated_artifacts",
                "local_gates",
                "release_receipts",
                "remote_checks",
            ],
        )

    def test_full_delivery_cannot_drop_or_empty_any_required_lane(self) -> None:
        for lane in CONTRACT.MATRIX_AUTHORITIES:
            for missing in (True, False):
                with self.subTest(lane=lane, missing=missing):
                    packet = base_packet()
                    if missing:
                        del packet["matrix"][lane]
                    else:
                        packet["matrix"][lane] = []
                    with self.assertRaisesRegex(
                        CONTRACT.ContractError, rf"matrix\.{lane}"
                    ):
                        CONTRACT.validate_packet(packet)

    def test_full_delivery_cannot_replace_required_source_bound_receipt(self) -> None:
        for lane in CONTRACT.MATRIX_AUTHORITIES:
            with self.subTest(lane=lane):
                packet = base_packet()
                item = packet["matrix"][lane][0]
                item.update({
                    "source_dependency": "source_independent",
                    "source_sha": None,
                    "reuse_class": "source_independent",
                    "independence_basis": "Tool availability is independent of source",
                })
                result = CONTRACT.validate_packet(seal(packet))
                self.assertFalse(result["ready"])
                self.assertIn(
                    f"matrix.{lane} requires at least one source-dependent receipt",
                    result["blockers"],
                )

    def test_same_revision_epoch_is_supported(self) -> None:
        packet = base_packet()
        packet["current_source_sha"] = IMPLEMENTATION_SHA
        packet["evidence_revision"] = IMPLEMENTATION_SHA
        binding = packet["revision_binding"]
        binding["relationship"] = "same_revision"
        binding["evidence_revision"] = IMPLEMENTATION_SHA
        proof = binding["evidence_only_diff"]
        proof["evidence_revision"] = IMPLEMENTATION_SHA
        proof["changed_paths"] = []
        for receipts in packet["matrix"].values():
            for item in receipts:
                item["reuse_class"] = "current_source_only"
        seal(packet)

        self.assertTrue(CONTRACT.validate_packet(packet)["ready"])

    def test_mixed_or_stale_source_receipt_is_rejected(self) -> None:
        packet = base_packet()
        packet["matrix"]["remote_checks"][0]["source_sha"] = OTHER_SHA
        seal(packet)

        self.assertTrue(
            any(
                "stale or names a mixed measured revision" in blocker
                for blocker in blockers(packet)
            )
        )

    def test_current_source_must_be_the_evidence_revision(self) -> None:
        packet = base_packet()
        packet["current_source_sha"] = OTHER_SHA

        self.assertIn(
            "current_source_sha must equal evidence_revision", blockers(packet)
        )

    def test_source_independent_reuse_requires_explicit_type_and_basis(self) -> None:
        packet = base_packet()
        reusable = receipt(
            "host-capability",
            "local_runner",
            "darwin-arm64",
            "unit",
            "a",
            "b",
        )
        reusable.update(
            {
                "source_dependency": "source_independent",
                "source_sha": None,
                "reuse_class": "source_independent",
                "independence_basis": "Host capability does not inspect repository bytes",
                "plan_command_id": "local-unit",
            }
        )
        packet["matrix"]["local_gates"].append(reusable)
        seal(packet)
        self.assertTrue(CONTRACT.validate_packet(packet)["ready"])

        invalid = copy.deepcopy(packet)
        invalid["matrix"]["local_gates"][1]["independence_basis"] = None
        seal(invalid)
        self.assertTrue(
            any("requires independence_basis" in item for item in blockers(invalid))
        )

    def test_later_evidence_revision_rejects_source_changes(self) -> None:
        cases = {
            "flag": lambda proof: proof.update({"source_paths_changed": True}),
            "tree": lambda proof: proof.update(
                {"source_tree_sha256_at_evidence": "a" * 64}
            ),
            "path": lambda proof: proof["changed_paths"].append("src/app.py"),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                packet = base_packet()
                mutate(packet["revision_binding"]["evidence_only_diff"])
                seal(packet)
                self.assertTrue(
                    any(
                        marker in item
                        for item in blockers(packet)
                        for marker in (
                            "changes source paths",
                            "changes the source tree digest",
                            "outside evidence_paths",
                        )
                    )
                )

    def test_wrong_repository_or_checkout_is_rejected_when_hashes_are_consistent(
        self,
    ) -> None:
        cases = {
            "repository": {
                "repository_id": "code.example.org/other/project",
                "checkout_sha256": CHECKOUT_SHA256,
            },
            "checkout": {
                "repository_id": "code.example.org/team/project",
                "checkout_sha256": "f" * 64,
            },
        }
        for name, wrong_identity in cases.items():
            with self.subTest(name=name):
                packet = base_packet()
                proof = packet["revision_binding"]["evidence_only_diff"]
                proof["repository_identity"] = wrong_identity
                proof_input = next(
                    item
                    for item in proof["input_hashes"]
                    if item["name"] == "repository-identity"
                )
                proof_input["sha256"] = CONTRACT.canonical_sha256(
                    wrong_identity
                )
                seal(packet)

                self.assertIn(
                    "evidence-only diff repository identity does not match immutable plan",
                    blockers(packet),
                )

    def test_revision_proof_target_and_profile_bind_plan_command(self) -> None:
        cases = {"target": "other/repository", "profile": "other-profile"}
        for field, value in cases.items():
            with self.subTest(field=field):
                packet = base_packet()
                packet["revision_binding"]["evidence_only_diff"][field] = value
                seal(packet)
                self.assertTrue(
                    any(
                        f"proof {field} does not match its plan command" in item
                        for item in blockers(packet)
                    )
                )

    def test_immutable_plan_and_policy_digests_are_enforced(self) -> None:
        packet = base_packet()
        binding = packet["revision_binding"]
        binding["immutable_plan"]["sample_policy"]["description"] = "One shard"
        binding["immutable_plan"]["commands"][0]["argv"].append("--changed")

        result = CONTRACT.validate_packet(packet)
        self.assertIn(
            "immutable_plan_sha256 does not match the immutable plan",
            result["blockers"],
        )
        self.assertIn(
            "sample_policy_sha256 does not match the immutable plan",
            result["blockers"],
        )
        self.assertIn(
            "immutable_commands_sha256 does not match the immutable plan",
            result["blockers"],
        )

    def test_authoritative_inputs_digest_is_enforced_per_receipt(self) -> None:
        packet = base_packet()
        packet["matrix"]["remote_checks"][0][
            "authoritative_inputs_sha256"
        ] = "f" * 64
        seal(packet)

        self.assertTrue(
            any(
                "authoritative inputs digest does not match input_hashes" in item
                for item in blockers(packet)
            )
        )

    def test_input_and_output_hash_aggregates_are_enforced(self) -> None:
        packet = base_packet()
        packet["matrix"]["local_gates"][0]["output_hashes"][0][
            "sha256"
        ] = "a" * 64
        result = CONTRACT.validate_packet(packet)

        self.assertIn(
            "outputs_sha256 does not match receipt and proof output hashes",
            result["blockers"],
        )

    def test_non_reproducible_authority_ledger_is_rejected(self) -> None:
        packet = base_packet()
        packet["revision_binding"]["authority_ledger_sha256"] = "f" * 64

        self.assertIn(
            "authority_ledger_sha256 is not reproducible from normalized receipts",
            blockers(packet),
        )

    def test_receipt_contract_requires_authoritative_metadata(self) -> None:
        packet = base_packet()
        del packet["matrix"]["release_receipts"][0]["bounded_claim"]

        with self.assertRaisesRegex(
            CONTRACT.ContractError, "bounded_claim must be a non-empty string"
        ):
            CONTRACT.validate_packet(packet)

    def test_order_does_not_change_normalized_result(self) -> None:
        packet = base_packet()
        packet["revision_binding"]["immutable_plan"]["commands"].reverse()
        packet["revision_binding"]["evidence_only_diff"]["output_hashes"].reverse()
        seal(packet)

        self.assertTrue(CONTRACT.validate_packet(packet)["ready"])

    def test_cli_exit_codes_and_purity(self) -> None:
        ready = subprocess.run(
            [sys.executable, str(SCRIPT), "-", "--compact"],
            input=json.dumps(base_packet()),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(ready.returncode, 0, ready.stderr)
        self.assertTrue(json.loads(ready.stdout)["ready"])

        blocked_packet = base_packet()
        blocked_packet["matrix"]["remote_checks"][0]["passed"] = False
        blocked = subprocess.run(
            [sys.executable, str(SCRIPT), "-", "--compact"],
            input=json.dumps(blocked_packet),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(blocked.returncode, 2, blocked.stderr)
        self.assertEqual(json.loads(blocked.stdout)["status"], "blocked")

        source = SCRIPT.read_text(encoding="utf-8")
        for forbidden in (
            "import os",
            "from os",
            "import subprocess",
            "from subprocess",
            "import git",
            "from git",
            "/Users/",
            "/home/",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
