#!/usr/bin/env python3
"""Recommend the lightest safe engineering iteration mode."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT_FIELDS = {"schema_version", "signals"}
SIGNAL_FIELDS = {
    "goal_clarity",
    "reversibility",
    "oracle",
    "attempts",
    "control_planes",
    "production_effect",
    "security_or_credentials",
    "data_migration",
    "external_protocol",
    "expensive_validation",
    "parallel_writers",
    "baseline_pinned",
    "failure_visibility",
    "repeated_edit_context",
    "misplaced_context_edit",
    "bugfix_helper_state_machine",
    "bugfix_shadow_state",
    "bugfix_fallback",
    "bugfix_unconditional_full_scan",
    "canonical_lifecycle_hooks_reviewed",
    "cheapest_representation_reviewed",
    "focused_falsifier_run",
    "invariant_mechanism_relation",
    "canonical_invariant_owner",
    "invariant_owner_strategy",
    "duplicate_invariant_owners_removed",
}
ENUMS = {
    "goal_clarity": {"clear", "partial", "unclear"},
    "reversibility": {"easy", "costly", "irreversible"},
    "oracle": {"deterministic", "stochastic", "unknown"},
}
DEFAULTS = {
    "goal_clarity": "clear",
    "reversibility": "easy",
    "oracle": "deterministic",
    "attempts": 0,
    "control_planes": 1,
    "production_effect": False,
    "security_or_credentials": False,
    "data_migration": False,
    "external_protocol": False,
    "expensive_validation": False,
    "parallel_writers": 1,
    "baseline_pinned": True,
    "failure_visibility": "clear",
    "repeated_edit_context": False,
    "misplaced_context_edit": False,
    "bugfix_helper_state_machine": False,
    "bugfix_shadow_state": False,
    "bugfix_fallback": False,
    "bugfix_unconditional_full_scan": False,
    "canonical_lifecycle_hooks_reviewed": False,
    "cheapest_representation_reviewed": False,
    "focused_falsifier_run": False,
    "invariant_mechanism_relation": "none",
    "canonical_invariant_owner": None,
    "invariant_owner_strategy": "none",
    "duplicate_invariant_owners_removed": 0,
}
ENUMS["failure_visibility"] = {"clear", "opaque"}
ENUMS["invariant_mechanism_relation"] = {"none", "same", "different"}
ENUMS["invariant_owner_strategy"] = {"none", "reuse_existing", "new_abstraction"}


def load_packet(path: Path | None) -> dict[str, Any]:
    try:
        raw = sys.stdin.read() if path is None else path.read_text()
        packet = json.loads(raw)
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid input: {error}") from error
    if not isinstance(packet, dict):
        raise ValueError("input must be a JSON object")
    unknown = sorted(set(packet) - ROOT_FIELDS)
    if unknown:
        raise ValueError(f"unsupported root fields: {unknown}")
    if packet.get("schema_version") != 1:
        raise ValueError("schema_version must be 1")
    raw_signals = packet.get("signals")
    if not isinstance(raw_signals, dict):
        raise ValueError("signals must be an object")
    unknown = sorted(set(raw_signals) - SIGNAL_FIELDS)
    if unknown:
        raise ValueError(f"unsupported signal fields: {unknown}")
    signals = {**DEFAULTS, **raw_signals}
    for field, allowed in ENUMS.items():
        if signals[field] not in allowed:
            raise ValueError(f"{field} must be one of {sorted(allowed)}")
    for field in (
        "attempts",
        "control_planes",
        "parallel_writers",
        "duplicate_invariant_owners_removed",
    ):
        value = signals[field]
        minimum = (
            0
            if field in {"attempts", "duplicate_invariant_owners_removed"}
            else 1
        )
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < minimum
        ):
            raise ValueError(f"{field} must be an integer >= {minimum}")
    owner = signals["canonical_invariant_owner"]
    if owner is not None and (
        not isinstance(owner, str) or not owner or "\n" in owner or "\r" in owner
    ):
        raise ValueError(
            "canonical_invariant_owner must be null or a non-empty single-line string"
        )
    for field in (
        "production_effect",
        "security_or_credentials",
        "data_migration",
        "external_protocol",
        "expensive_validation",
        "baseline_pinned",
        "repeated_edit_context",
        "misplaced_context_edit",
        "bugfix_helper_state_machine",
        "bugfix_shadow_state",
        "bugfix_fallback",
        "bugfix_unconditional_full_scan",
        "canonical_lifecycle_hooks_reviewed",
        "cheapest_representation_reviewed",
        "focused_falsifier_run",
    ):
        if not isinstance(signals[field], bool):
            raise ValueError(f"{field} must be boolean")
    return signals


def recommend(signals: dict[str, Any]) -> dict[str, Any]:
    l1_reasons: list[str] = []
    l2_reasons: list[str] = []
    boundaries: list[str] = []
    delegates: list[str] = []
    edit_placement_guard: list[str] = []
    complexity_mechanisms = [
        label
        for field, label in (
            ("bugfix_helper_state_machine", "helper_state_machine"),
            ("bugfix_shadow_state", "shadow_state"),
            ("bugfix_fallback", "fallback"),
            ("bugfix_unconditional_full_scan", "unconditional_full_scan"),
        )
        if signals[field]
    ]
    complexity_checks = {
        "canonical_lifecycle_hooks_reviewed": signals[
            "canonical_lifecycle_hooks_reviewed"
        ],
        "cheapest_representation_reviewed": signals[
            "cheapest_representation_reviewed"
        ],
        "focused_falsifier_run": signals["focused_falsifier_run"],
    }
    missing_complexity_checks = (
        sorted(
            field for field, completed in complexity_checks.items() if not completed
        )
        if complexity_mechanisms
        else []
    )
    complexity_admission = {
        "required": bool(complexity_mechanisms),
        "mechanisms": complexity_mechanisms,
        "admitted": not missing_complexity_checks,
        "missing_checks": missing_complexity_checks,
    }
    relation = signals["invariant_mechanism_relation"]
    canonical_owner = signals["canonical_invariant_owner"]
    owner_strategy = signals["invariant_owner_strategy"]
    removed = signals["duplicate_invariant_owners_removed"]
    convergence_actions: list[str] = []
    convergence_admitted = True
    if relation == "same":
        if canonical_owner is None:
            convergence_actions.append("name one canonical invariant owner")
        if owner_strategy == "none":
            convergence_actions.append(
                "declare whether convergence reuses an existing owner or adds a new abstraction"
            )
        elif owner_strategy == "reuse_existing" and removed != 0:
            convergence_actions.append(
                "reusing an existing owner must not claim removal by a new abstraction"
            )
        elif owner_strategy == "new_abstraction" and removed < 2:
            convergence_actions.append(
                "make the new abstraction remove at least two duplicate owners"
            )
        convergence_admitted = not convergence_actions
    elif relation == "different":
        if (
            canonical_owner is not None
            or owner_strategy != "none"
            or removed != 0
        ):
            convergence_actions.append(
                "keep different mechanisms under separate invariant owners"
            )
        convergence_admitted = not convergence_actions
    elif canonical_owner is not None or owner_strategy != "none" or removed != 0:
        convergence_actions.append(
            "declare whether the compared invariant mechanisms are the same or different"
        )
        convergence_admitted = False
    invariant_owner_convergence = {
        "applicable": relation != "none",
        "mechanism_relation": relation,
        "canonical_owner": canonical_owner,
        "owner_strategy": owner_strategy,
        "duplicate_owners_removed": removed,
        "admitted": convergence_admitted,
        "required_actions": convergence_actions,
    }

    if signals["goal_clarity"] != "clear":
        l1_reasons.append(f"goal clarity is {signals['goal_clarity']}")
    if signals["reversibility"] == "costly":
        l1_reasons.append("rollback is costly")
    if signals["oracle"] != "deterministic":
        l1_reasons.append(f"the validation oracle is {signals['oracle']}")
    if signals["attempts"] >= 1:
        l1_reasons.append("an earlier attempt did not fully settle the task")
    if signals["control_planes"] >= 2:
        l1_reasons.append(
            f"{signals['control_planes']} control planes are involved"
        )
    if signals["external_protocol"]:
        l1_reasons.append("an external protocol or version contract is involved")
    if signals["expensive_validation"]:
        l1_reasons.append("the strongest validation path is expensive")
    if not signals["baseline_pinned"]:
        l1_reasons.append("the implementation or validation baseline is not pinned")
    if signals["repeated_edit_context"] or signals["misplaced_context_edit"]:
        edit_placement_guard = [
            "name the intended function or test symbol",
            "include one locally unique invariant in the edit anchor",
            "inspect the scoped diff and verify the changed hunk remains under that symbol",
        ]
    if signals["misplaced_context_edit"]:
        l1_reasons.append(
            "a contextual edit landed under the wrong repeated symbol"
        )
    if complexity_mechanisms:
        l1_reasons.append(
            "the proposed bugfix adds broader state, fallback, or scan complexity"
        )
        if missing_complexity_checks:
            boundaries.append(
                "do not add the proposed complexity until canonical lifecycle hooks, "
                "the cheapest representation, and a focused falsifier are reviewed"
            )
    if relation == "same":
        l1_reasons.append(
            "a second instance of the same invariant mechanism needs one owner"
        )
    if convergence_actions:
        boundaries.extend(convergence_actions)
    if signals["parallel_writers"] > 1:
        l1_reasons.append("multiple writers require explicit ownership")
        delegates.extend(
            ["isolated-worktree-per-writer", "worktree-cache-lifecycle-owner"]
        )
    if signals["production_effect"]:
        l1_reasons.append("the task includes a production side effect")
        boundaries.append(
            "keep implementation and production mutation authorization explicit"
        )
    if signals["security_or_credentials"]:
        l1_reasons.append("security or credential boundaries are involved")
        boundaries.append(
            "do not expose credentials or weaken authorization for convenience"
        )
    if signals["data_migration"]:
        boundaries.append(
            "require rollback or forward-recovery proof before mutating data"
        )

    uncertainty = (
        signals["oracle"] != "deterministic"
        or signals["goal_clarity"] != "clear"
        or signals["control_planes"] >= 2
        or signals["external_protocol"]
        or signals["expensive_validation"]
        or not signals["baseline_pinned"]
    )
    if signals["reversibility"] == "irreversible":
        l2_reasons.append("the action is irreversible")
    if signals["data_migration"]:
        l2_reasons.append("data migration needs a controlled experiment boundary")
    if signals["attempts"] >= 2 and uncertainty:
        l2_reasons.append(
            "repeated attempts plus unresolved uncertainty require attribution"
        )
    if signals["control_planes"] >= 3:
        l2_reasons.append("three or more control planes can hide the first divergence")
    if signals["oracle"] == "stochastic" and signals["expensive_validation"]:
        l2_reasons.append(
            "a stochastic oracle is coupled to an expensive validation path"
        )
    if (
        signals["goal_clarity"] == "unclear"
        and signals["control_planes"] >= 2
    ):
        l2_reasons.append("an unclear goal spans multiple control planes")
    if signals["security_or_credentials"] and signals["production_effect"]:
        l2_reasons.append(
            "security-sensitive behavior reaches a production boundary"
        )
    opaque_failure_matrix = []
    if signals["attempts"] >= 2 and signals["failure_visibility"] == "opaque":
        if "repeated opaque failures require boundary isolation" not in l2_reasons:
            l2_reasons.append("repeated opaque failures require boundary isolation")
        opaque_failure_matrix = [
            {
                "layer": "command_construction",
                "evidence": "exact argv, cwd, environment allowlist, and input bytes",
            },
            {
                "layer": "process_launch",
                "evidence": "parent-observed spawn result, pid/exit class, and side effects",
            },
            {
                "layer": "sandbox_policy",
                "evidence": "permission denial or a matched control outside the sandbox",
            },
            {
                "layer": "tool_transport",
                "evidence": "request/session boundary and raw transport status",
            },
            {
                "layer": "result_parsing",
                "evidence": "raw bounded response versus interpreted result",
            },
        ]

    if l2_reasons:
        mode = "L2"
        workflow = [
            "freeze the baseline, scope, acceptance contract, and side-effect boundary",
            "obtain a deterministic reproduction or add bounded diagnostics first",
            "state one hypothesis with prediction, falsifier, and primary variable",
            "change one attributable control variable and run focused negative tests",
            "broaden gates, freeze the artifact, then run the minimum authorized live check",
        ]
    elif l1_reasons:
        mode = "L1"
        workflow = [
            "state the current direction and the main uncertainty in a few lines",
            "choose the cheapest check that distinguishes the plausible directions",
            "make the smallest coherent root-cause change",
            "run focused verification, inspect the diff, and escalate only if results diverge",
        ]
    else:
        mode = "L0"
        workflow = [
            "understand the local code and acceptance signal",
            "make the smallest coherent change",
            "run focused verification and inspect the diff",
        ]

    escalate_when = [
        "the observed result contradicts the expected behavior",
        "a second repair attempt is needed without a clearer failure class",
        "the change expands into another control plane or a costly live environment",
        "baseline, ownership, or authoritative evidence becomes ambiguous",
    ]
    return {
        "schema_version": 1,
        "mode": mode,
        "advisory_only": True,
        "reasons": l2_reasons if mode == "L2" else l1_reasons,
        "mandatory_boundaries": boundaries,
        "workflow": workflow,
        "escalate_when": escalate_when,
        "delegate_to": sorted(set(delegates)),
        "opaque_failure_matrix": opaque_failure_matrix,
        "edit_placement_guard": edit_placement_guard,
        "complexity_admission": complexity_admission,
        "invariant_owner_convergence": invariant_owner_convergence,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "packet",
        nargs="?",
        type=Path,
        help="JSON packet; read stdin when omitted",
    )
    args = parser.parse_args()
    try:
        result = recommend(load_packet(args.packet))
    except ValueError as error:
        print(json.dumps({"valid": False, "error": str(error)}))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
