#!/usr/bin/env python3
"""Tests for external-write transaction validation."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import external_write_transaction


SCRIPT = Path(__file__).with_name("external_write_transaction.py")


def read_event(
    packet: dict[str, object],
    phase: str,
    matches: list[dict[str, str]] | None = None,
    observed_after_attempt: int | None = None,
) -> dict[str, object]:
    return {
        "type": "authoritative_read",
        "phase": phase,
        "authority_locator": packet["provider_contract"]["authority_locator"],
        "query_sha256": packet["operation"]["authoritative_query_sha256"],
        "provider_schema_revision": packet["provider_contract"][
            "authoritative_read_schema_revision"
        ],
        "observed_after_attempt": (
            0
            if observed_after_attempt is None and phase == "preflight"
            else (
                1 if observed_after_attempt is None else observed_after_attempt
            )
        ),
        "matches": [] if matches is None else matches,
    }


def write_event(packet: dict[str, object], attempt: int, outcome: str) -> dict[str, object]:
    operation = packet["operation"]
    return {
        "type": "write_attempt",
        "attempt": attempt,
        "operation_id": operation["operation_id"],
        "intent_sha256": operation["intent_sha256"],
        "retry_key": operation["retry_key"],
        "outcome": outcome,
    }


def match(packet: dict[str, object], record_id: str = "record-1") -> dict[str, str]:
    operation = packet["operation"]
    return {
        "operation_id": operation["operation_id"],
        "intent_sha256": operation["intent_sha256"],
        "record_id": record_id,
    }


def packet_fixture() -> dict[str, object]:
    intent = {"resource": "ticket-42", "action": "add_label", "label": "ready"}
    query = {
        "operation_id": "operation-0001",
        "intent_sha256": external_write_transaction.intent_digest(intent),
    }
    return {
        "schema_version": 1,
        "provider_contract": {
            "idempotency_key_support": True,
            "same_key_replay_guaranteed": True,
            "retry_after_authoritative_absence": True,
            "authoritative_read_support": True,
            "authority_locator": "provider-api/operations/search",
            "authoritative_read_schema_revision": "operations-search-v3",
        },
        "operation": {
            "operation_id": "operation-0001",
            "intent": intent,
            "intent_sha256": external_write_transaction.intent_digest(intent),
            "authoritative_query": query,
            "authoritative_query_sha256": external_write_transaction.intent_digest(
                query
            ),
            "retry_key": "retry-0002",
            "prior_operations": [
                {
                    "operation_id": "operation-0002",
                    "intent_sha256": "a" * 64,
                    "retry_key": "retry-0001",
                }
            ],
        },
        "events": [],
        "claim": {
            "state": "ready_to_write",
            "next_action": "write_once",
            "outcome_unknown_seen": False,
            "late_success": False,
        },
    }


class ExternalWriteTransactionTest(unittest.TestCase):
    def assert_invalid(self, packet: dict[str, object], expected: str) -> None:
        with self.assertRaisesRegex(ValueError, expected):
            external_write_transaction.validate_packet(packet)

    def test_preflight_absence_is_ready_for_one_write(self):
        packet = packet_fixture()
        packet["events"] = [read_event(packet, "preflight")]

        result = external_write_transaction.validate_packet(packet)

        self.assertEqual("ready_to_write", result["state"])
        self.assertEqual(0, result["attempts"])

    def test_preflight_unique_existing_record_stops_without_write(self):
        packet = packet_fixture()
        packet["events"] = [read_event(packet, "preflight", [match(packet)])]
        packet["claim"].update(
            state="already_applied", next_action="stop"
        )

        result = external_write_transaction.validate_packet(packet)

        self.assertEqual("already_applied", result["state"])

    def test_transport_ambiguity_reconciles_unique_late_success(self):
        packet = packet_fixture()
        packet["events"] = [
            read_event(packet, "preflight"),
            write_event(packet, 1, "transport_ambiguous"),
            read_event(packet, "reconciliation", [match(packet)]),
        ]
        packet["claim"].update(
            state="applied",
            next_action="stop",
            outcome_unknown_seen=True,
            late_success=True,
        )

        result = external_write_transaction.validate_packet(packet)

        self.assertTrue(result["late_success"])
        self.assertEqual(1, result["attempts"])

    def test_ui_ambiguity_requires_authoritative_read_before_retry(self):
        packet = packet_fixture()
        packet["events"] = [
            read_event(packet, "preflight"),
            write_event(packet, 1, "ui_ambiguous"),
            write_event(packet, 2, "success"),
        ]
        packet["claim"].update(
            state="applied", next_action="stop", outcome_unknown_seen=True
        )

        self.assert_invalid(packet, "must be authoritative_read")

    def test_malformed_event_fails_with_stable_validation_error(self):
        packet = packet_fixture()
        packet["events"] = [{}]

        self.assert_invalid(packet, "type must be authoritative_read")

    def test_retry_after_authoritative_absence_reuses_identity_intent_and_key(self):
        packet = packet_fixture()
        packet["events"] = [
            read_event(packet, "preflight"),
            write_event(packet, 1, "transport_ambiguous"),
            read_event(packet, "reconciliation"),
            write_event(packet, 2, "success"),
        ]
        packet["claim"].update(
            state="applied", next_action="stop", outcome_unknown_seen=True
        )

        result = external_write_transaction.validate_packet(packet)

        self.assertEqual(2, result["attempts"])
        self.assertEqual("applied", result["state"])

    def test_retry_is_blocked_without_retry_safe_provider_contract(self):
        packet = packet_fixture()
        packet["provider_contract"]["retry_after_authoritative_absence"] = False
        packet["events"] = [
            read_event(packet, "preflight"),
            write_event(packet, 1, "transport_ambiguous"),
            read_event(packet, "reconciliation"),
        ]
        packet["claim"].update(
            state="blocked", next_action="stop_and_escalate", outcome_unknown_seen=True
        )

        result = external_write_transaction.validate_packet(packet)

        self.assertEqual("blocked", result["state"])

    def test_multiple_or_conflicting_matches_block(self):
        for matches in (
            [match(packet_fixture(), "one"), match(packet_fixture(), "two")],
            [
                {
                    "operation_id": "different-operation",
                    "intent_sha256": "b" * 64,
                    "record_id": "other",
                }
            ],
        ):
            with self.subTest(matches=matches):
                packet = packet_fixture()
                packet["events"] = [read_event(packet, "preflight", matches)]
                packet["claim"].update(
                    state="blocked", next_action="stop_and_escalate"
                )
                result = external_write_transaction.validate_packet(packet)
                self.assertEqual("blocked", result["state"])

    def test_intent_digest_and_same_operation_key_are_immutable(self):
        packet = packet_fixture()
        packet["operation"]["intent"]["label"] = "changed"
        packet["events"] = [read_event(packet, "preflight")]
        self.assert_invalid(packet, "does not match canonical intent")

        packet = packet_fixture()
        event = write_event(packet, 1, "success")
        event["retry_key"] = "different-key"
        packet["events"] = [read_event(packet, "preflight"), event]
        packet["claim"].update(state="applied", next_action="stop")
        self.assert_invalid(packet, "reuse the same retry key")

    def test_new_operation_rotates_identity_and_retry_key(self):
        for field, expected in (
            ("operation_id", "rotate operation identity"),
            ("retry_key", "rotate the idempotency retry key"),
        ):
            with self.subTest(field=field):
                packet = packet_fixture()
                packet["operation"][field] = packet["operation"]["prior_operations"][0][field]
                packet["events"] = [read_event(packet, "preflight")]
                self.assert_invalid(packet, expected)

    def test_cli_returns_stable_failure_json(self):
        packet = packet_fixture()
        packet["events"] = [read_event(packet, "wrong-phase")]
        process = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=json.dumps(packet),
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(2, process.returncode)
        result = json.loads(process.stdout)
        self.assertFalse(result["valid"])
        self.assertIn("phase must be preflight", result["error"])

    def test_non_authoritative_or_wrong_query_read_is_rejected(self):
        for field, value, expected in (
            (
                "authority_locator",
                "cache/local-search",
                "authority locator is not authoritative",
            ),
            ("query_sha256", "f" * 64, "query digest does not match"),
            (
                "provider_schema_revision",
                "operations-search-v2",
                "schema revision is stale",
            ),
        ):
            with self.subTest(field=field):
                packet = packet_fixture()
                event = read_event(packet, "preflight")
                event[field] = value
                packet["events"] = [event]
                self.assert_invalid(packet, expected)

    def test_stale_reconciliation_cannot_authorize_retry(self):
        packet = packet_fixture()
        packet["events"] = [
            read_event(packet, "preflight"),
            write_event(packet, 1, "transport_ambiguous"),
            read_event(packet, "reconciliation", observed_after_attempt=0),
        ]
        packet["claim"].update(
            state="retry_allowed",
            next_action="retry_same_operation",
            outcome_unknown_seen=True,
        )

        self.assert_invalid(packet, "must be 1 for freshness")


if __name__ == "__main__":
    unittest.main()
