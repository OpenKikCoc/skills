#!/usr/bin/env python3
"""Tests for dev-disk-budget shell/state workflow examples."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("validate_shell_state_contract.py")
SPEC = importlib.util.spec_from_file_location("shell_state_contract", SCRIPT)
assert SPEC and SPEC.loader
CONTRACT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONTRACT)
REFERENCE = Path(__file__).resolve().parent.parent / "references/shell-state-contract.md"


def codes(snippet: str) -> list[str]:
    return [entry["code"] for entry in CONTRACT.validate_snippet(snippet)]


class ShellStateContractTests(unittest.TestCase):
    def test_canonical_reference_passes(self) -> None:
        self.assertEqual(CONTRACT.validate_file(REFERENCE), [])

    def test_complete_sequence_passes(self) -> None:
        snippet = '''
STATE_ROOT="${STATE_ROOT:?set an external persistent state root}"
mkdir -p -- "${STATE_ROOT}"
task_ids=(core controller)
task_args=()
for task_id in "${task_ids[@]}"; do
  task_args+=(--task-id "${task_id}")
done
tool go-topology \
  --wave-manifest "${STATE_ROOT}/wave.json" \
  --plan-sha256 "${PLAN_SHA256}" "${task_args[@]}" \
  --output "${STATE_ROOT}/go-topology.json"
receipts=("${STATE_ROOT}"/wave-*-receipt.json(N))
for receipt_file in "${receipts[@]}"; do
  jq -e . "${receipt_file}"
done
'''
        self.assertEqual(codes(snippet), [])

    def test_zsh_special_path_assignment_and_loop_are_rejected(self) -> None:
        self.assertIn("ZSH_SPECIAL_PATH", codes('path="${STATE_ROOT}/run.json"'))
        self.assertIn(
            "ZSH_SPECIAL_PATH",
            codes('for path in "${resource_paths[@]}"; do true; done'),
        )

    def test_one_command_prefix_and_disposable_state_root_are_rejected(self) -> None:
        self.assertIn(
            "ONE_COMMAND_STATE_PREFIX",
            codes(
                'STATE_ROOT="${CONTROL_ROOT}" python3 tool.py '
                '--manifest "${STATE_ROOT}/run.json"'
            ),
        )
        self.assertIn(
            "STATE_ROOT_UNDER_MANAGED_ROOT",
            codes('STATE_ROOT="${MANAGED_ROOT}/state"'),
        )

    def test_state_artifacts_require_established_external_root(self) -> None:
        self.assertIn(
            "STATE_ROOT_NOT_ESTABLISHED",
            codes('tool --manifest "${STATE_ROOT}/run.json"'),
        )
        self.assertIn(
            "STATE_ARTIFACT_OUTSIDE_ROOT",
            codes(
                'STATE_ROOT="${STATE_ROOT:?required}"\n'
                'tool --receipt "${MANAGED_ROOT}/receipt.json"'
            ),
        )
        self.assertIn(
            "STATE_RECEIPT_NOT_PERSISTED",
            codes(
                'STATE_ROOT="${STATE_ROOT:?required}"\n'
                'tool go-topology --wave-manifest "${STATE_ROOT}/wave.json"'
            ),
        )

    def test_optional_glob_and_scalar_pseudo_list_are_rejected(self) -> None:
        self.assertIn(
            "OPTIONAL_BARE_ZSH_GLOB",
            codes('receipts=("${STATE_ROOT}"/wave-*-receipt.json)'),
        )
        self.assertIn(
            "SCALAR_PSEUDO_LIST",
            codes('for receipt_file in $receipts; do true; done'),
        )
        self.assertEqual(
            codes(
                'receipts=("${STATE_ROOT}"/wave-*-receipt.json(N))\n'
                'for receipt_file in "${receipts[@]}"; do true; done'
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
