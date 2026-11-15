import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from food_recall.archive import Archive
from food_recall.cases import CaseService
from food_recall.intake import RecallIngestor
from food_recall.origin import load_scenario, serve
from food_recall.recall import (
    CaseAccumulator,
    inspect_announcement,
    inspect_record,
)


def connect():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.connection = connect()
        self.cases = CaseService(self.connection, sleeper=lambda seconds: None)
        self.uid = self.cases.case_uid

    def tearDown(self):
        self.connection.close()

    def ingest(self, capture_id, *records):
        return self.cases.ingest(capture_id, "feed", list(records))

    def send_all(self, case_uid):
        delivered = []
        for item in self.cases.dispatch(case_uid):
            delivered.append(self.cases.deliver(item["notification_id"], lambda key, message: None))
        return delivered


class NormalisationTests(unittest.TestCase):
    def test_accepts_split_strings_and_deduplicates_lots(self):
        announcement = inspect_announcement(
            {"authority": "a", "case": "C", "revision": 1, "product": "P", "lots": "L1, L2、L1"}
        )
        self.assertEqual(announcement.lots, ("L1", "L2"))
        self.assertTrue(announcement.complete)

    def test_missing_identity_is_tolerated_but_marked_incomplete(self):
        announcement = inspect_announcement({"product": "P", "lots": None})
        self.assertIsNone(announcement.identity)
        self.assertFalse(announcement.complete)

    def test_malformed_fields_are_rejected(self):
        for field, value in (("revision", "2"), ("lots", {"L1": 1}), ("authority", 7)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                inspect_announcement({"product": "P", field: value})

    def test_legacy_record_contract_is_unchanged(self):
        result = inspect_record({"source": " feed ", "key": " item ", "revision": 2, "value": None})
        self.assertEqual(result["event"], "store_notifications")


class AccumulatorTests(unittest.TestCase):
    def observation(self, revision, lots, status="published", withdrawn=(), product="P"):
        return {
            "id": revision,
            "revision": revision,
            "product": product,
            "lots": lots,
            "status": status,
            "withdrawn_lots": list(withdrawn),
            "content_digest": f"digest-{revision}-{lots}-{status}",
        }

    def test_missing_lots_waits_then_same_case_becomes_active(self):
        accumulator = CaseAccumulator()
        self.assertEqual(accumulator.feed(self.observation(1, None))[0], "pending_lots")
        self.assertEqual(accumulator.state, "pending_lots")
        self.assertEqual(accumulator.feed(self.observation(2, ["L1"]))[0], "applied")
        self.assertEqual(accumulator.state, "active")
        self.assertEqual(accumulator.effective, ["L1"])

    def test_later_revision_does_not_erase_known_lots(self):
        accumulator = CaseAccumulator()
        accumulator.feed(self.observation(1, ["L1"]))
        accumulator.feed(self.observation(2, None))
        self.assertEqual(accumulator.effective, ["L1"])
        self.assertEqual(accumulator.applied_revision, 1)

    def test_older_revision_never_restores_withdrawn_lot(self):
        accumulator = CaseAccumulator()
        accumulator.feed(self.observation(1, ["L1", "L2"]))
        accumulator.feed(self.observation(2, ["L1"], status="corrected", withdrawn=["L2"]))
        self.assertEqual(accumulator.feed(self.observation(1, ["L1", "L2"]))[0], "stale")
        self.assertEqual(accumulator.withdrawn, ["L2"])
        self.assertEqual(accumulator.effective, ["L1"])

    def test_higher_revision_relisting_withdrawn_lot_does_not_restore(self):
        accumulator = CaseAccumulator()
        accumulator.feed(self.observation(2, ["L1"]))
        accumulator.feed(self.observation(3, ["L1", "L2"], status="corrected", withdrawn=["L2"]))
        disposition = accumulator.feed(self.observation(4, ["L1", "L2", "L3"]))
        self.assertEqual(disposition, ("applied", "withdrawn_lot_relisted"))
        self.assertNotIn("L2", accumulator.effective)
        self.assertIn("withdrawn_lot_relisted", accumulator.review_reasons)

    def test_corrected_revision_without_explicit_withdrawal_narrows_scope(self):
        accumulator = CaseAccumulator()
        accumulator.feed(self.observation(1, ["L1", "L2"]))
        accumulator.feed(self.observation(2, ["L1"], status="corrected"))
        self.assertEqual(accumulator.effective, ["L1"])
        self.assertEqual(accumulator.withdrawn, ["L2"])

    def test_plain_revision_does_not_infer_withdrawal(self):
        accumulator = CaseAccumulator()
        accumulator.feed(self.observation(1, ["L1"]))
        accumulator.feed(self.observation(2, ["L1", "L2"]))
        self.assertEqual(accumulator.withdrawn, [])
        self.assertEqual(accumulator.effective, ["L1", "L2"])

    def test_whole_case_withdrawal_closes_scope(self):
        accumulator = CaseAccumulator()
        accumulator.feed(self.observation(1, ["L1", "L2", "L3"]))
        self.assertEqual(accumulator.feed(self.observation(2, [], status="withdrawn"))[0], "withdrawn_applied")
        self.assertEqual(accumulator.state, "withdrawn")
        self.assertEqual(accumulator.effective, [])
        self.assertEqual(accumulator.withdrawn, ["L1", "L2", "L3"])

    def test_conflicting_product_requires_review(self):
        accumulator = CaseAccumulator()
        accumulator.feed(self.observation(1, ["L1"]))
        disposition, reason = accumulator.feed(self.observation(2, ["L2"], product="OTHER"))
        self.assertEqual(disposition, "conflict")
        self.assertEqual(reason, "product_conflict")
        self.assertEqual(accumulator.effective, ["L1"])


class IngestionTests(ServiceCase):
    def test_same_case_number_in_different_authority_is_never_merged(self):
        self.ingest(
            1,
            {"authority": "district-a", "case": "RC-8", "revision": 1, "product": "P", "lots": ["L10"]},
            {"authority": "district-b", "case": "RC-8", "revision": 1, "product": "P", "lots": ["L20"]},
        )
        self.assertEqual(len(self.cases.list_cases()), 2)
        self.assertEqual(self.cases.effective_lots(self.uid("district-a", "RC-8")), ["L10"])
        self.assertEqual(self.cases.effective_lots(self.uid("district-b", "RC-8")), ["L20"])

    def test_batch_supplementation_keeps_the_same_case(self):
        self.ingest(
            1,
            {"authority": "a", "case": "C", "revision": 1, "product": "P", "lots": None},
            {"authority": "a", "case": "C", "revision": 2, "product": "P", "lots": ["L1", "L2"]},
        )
        self.assertEqual(len(self.cases.list_cases()), 1)
        case = self.cases.list_cases()[0]
        self.assertEqual(case["state"], "active")
        self.assertEqual(case["effective_lots"], ["L1", "L2"])

    def test_correction_and_withdrawal_are_recorded(self):
        records = load_scenario()["routes"]["/feed"]["json"]["items"]
        self.ingest(1, *records)
        case = self.cases.list_cases()[0]
        self.assertEqual(case["authority"], "district-a")
        self.assertEqual(case["effective_lots"], ["L10"])
        self.assertEqual(case["withdrawn_lots"], ["L11"])
        self.assertEqual(case["current_revision"], 3)

    def test_repeated_capture_does_not_duplicate_decisions(self):
        record = {"authority": "a", "case": "C", "revision": 3, "product": "P", "lots": ["L1"]}
        self.ingest(1, record)
        first = self.cases.list_cases()[0]
        ledger = len(self.cases.decision_ledger(self.uid("a", "C")))
        self.ingest(2, record)
        second = self.cases.list_cases()[0]
        self.assertEqual(first["effective_lots"], second["effective_lots"])
        self.assertEqual(len(self.cases.decision_ledger(self.uid("a", "C"))), ledger)
        self.assertEqual(len(self.cases.notifications_for()), 0)

    def test_invalid_records_are_reported_without_creating_cases(self):
        result = self.ingest(1, {"authority": "a", "case": "C", "revision": "x", "product": "P"})
        self.assertEqual(result["invalid"][0]["source_index"], 0)
        self.assertEqual(self.cases.list_cases(), [])


class ScopeTests(ServiceCase):
    def setUp(self):
        super().setUp()
        self.ingest(
            1,
            {"authority": "a", "case": "C", "revision": 1, "product": "P", "lots": ["L1", "L2"]},
            {"authority": "a", "case": "C", "revision": 2, "product": "P", "lots": ["L1"], "status": "corrected", "withdrawn_lots": ["L2"]},
        )
        self.cases.add_store("S1", ["m1"])
        self.cases.add_store("S2", ["m2"])

    def test_store_scope_only_contains_current_effective_lots(self):
        self.cases.set_inventory("S1", "P", ["L1", "L2"])
        scope = self.cases.query_scope("S1")
        self.assertEqual(scope[0]["effective_lots"], ["L1"])
        self.assertEqual(scope[0]["withdrawn_lots"], ["L2"])

    def test_store_without_affected_inventory_has_empty_scope(self):
        self.cases.set_inventory("S2", "P", ["L2"])
        self.assertEqual(self.cases.query_scope("S2"), [])

    def test_inventory_changes_are_tracked_dynamically(self):
        self.cases.set_inventory("S2", "P", ["L1"])
        self.assertEqual(self.cases.query_scope("S2")[0]["effective_lots"], ["L1"])
        self.cases.set_inventory("S2", "P", ["L2"])
        self.assertEqual(self.cases.query_scope("S2"), [])

    def test_pending_case_has_no_executable_scope(self):
        self.cases.ingest(9, "feed", [{"authority": "b", "case": "D", "revision": 1, "product": "P", "lots": None}])
        self.assertEqual(self.cases.query_scope("S1"), [])


class NotificationTests(ServiceCase):
    def setUp(self):
        super().setUp()
        self.cases.add_store("S1", ["m1"])
        self.cases.add_store("S2", ["m2"])

    def test_first_notice_only_goes_to_affected_stores(self):
        self.cases.set_inventory("S1", "P", ["L1"])
        self.cases.set_inventory("S2", "P", ["L2"])
        self.ingest(1, {"authority": "a", "case": "C", "revision": 1, "product": "P", "lots": ["L1"]})
        created = self.cases.dispatch(self.uid("a", "C"))
        self.assertEqual([item["store_id"] for item in created], ["S1"])

    def test_correction_only_notifies_stores_that_saw_the_old_scope(self):
        self.cases.set_inventory("S1", "P", ["L1"])
        self.cases.set_inventory("S2", "P", ["L2"])
        self.ingest(1, {"authority": "a", "case": "C", "revision": 1, "product": "P", "lots": ["L1", "L2"]})
        self.send_all(self.uid("a", "C"))
        self.ingest(2, {"authority": "a", "case": "C", "revision": 2, "product": "P", "lots": ["L1"], "status": "corrected", "withdrawn_lots": ["L2"]})
        plan = self.cases.plan(self.uid("a", "C"))
        self.assertEqual([(item["store_id"], item["kind"]) for item in plan], [("S2", "withdrawal")])
        self.send_all(self.uid("a", "C"))
        self.assertEqual(len(self.cases.notifications_for(self.uid("a", "C"))), 3)

    def test_scope_amendment_sends_only_the_delta(self):
        self.cases.set_inventory("S1", "P", ["L1", "L3"])
        self.ingest(1, {"authority": "a", "case": "C", "revision": 1, "product": "P", "lots": ["L1"]})
        self.send_all(self.uid("a", "C"))
        self.ingest(2, {"authority": "a", "case": "C", "revision": 2, "product": "P", "lots": ["L1", "L3"]})
        plan = self.cases.plan(self.uid("a", "C"))
        self.assertEqual(plan[0]["effective_lots"], ["L3"])
        self.assertEqual(plan[0]["kind"], "correction")

    def test_store_gaining_inventory_later_is_notified_only_then(self):
        self.cases.set_inventory("S1", "P", ["L1"])
        self.cases.set_inventory("S2", "P", [])
        self.ingest(1, {"authority": "a", "case": "C", "revision": 1, "product": "P", "lots": ["L1"]})
        self.send_all(self.uid("a", "C"))
        self.assertEqual(len(self.cases.notifications_for()), 1)
        self.cases.set_inventory("S2", "P", ["L1"])
        plan = self.cases.plan(self.uid("a", "C"))
        self.assertEqual([item["store_id"] for item in plan], ["S2"])
        self.assertEqual(plan[0]["kind"], "initial")

    def test_old_page_reappearance_does_not_restore_withdrawn_scope(self):
        self.cases.set_inventory("S1", "P", ["L1", "L2"])
        self.ingest(
            1,
            {"authority": "a", "case": "C", "revision": 1, "product": "P", "lots": ["L1", "L2"]},
            {"authority": "a", "case": "C", "revision": 2, "product": "P", "lots": ["L1"], "status": "corrected", "withdrawn_lots": ["L2"]},
        )
        self.send_all(self.uid("a", "C"))
        self.ingest(2, {"authority": "a", "case": "C", "revision": 1, "product": "P", "lots": ["L1", "L2"]})
        self.assertEqual(self.cases.effective_lots(self.uid("a", "C")), ["L1"])
        self.assertEqual(self.cases.plan(self.uid("a", "C")), [])

    def test_dispatch_is_idempotent(self):
        self.cases.set_inventory("S1", "P", ["L1"])
        self.ingest(1, {"authority": "a", "case": "C", "revision": 1, "product": "P", "lots": ["L1"]})
        first = self.cases.dispatch(self.uid("a", "C"))
        second = self.cases.dispatch(self.uid("a", "C"))
        self.assertEqual(len(first), 1)
        self.assertEqual([item["notification_id"] for item in first], [item["notification_id"] for item in second])
        self.assertTrue(second[0]["reused"])
        self.assertEqual(len(self.cases.notifications_for()), 1)

    def test_pending_case_cannot_be_dispatched(self):
        self.ingest(1, {"authority": "a", "case": "C", "revision": 1, "product": "P", "lots": None})
        with self.assertRaises(ValueError):
            self.cases.dispatch(self.uid("a", "C"))

    def test_product_level_advisory_is_sent_before_batches_are_known(self):
        self.cases.set_inventory("S1", "P", ["L1"])
        self.cases.set_inventory("S2", "P", ["L9"])
        self.ingest(1, {"authority": "a", "case": "C", "revision": 1, "product": "P", "lots": None})
        created = self.cases.dispatch_advisory(self.uid("a", "C"))
        self.assertEqual({item["store_id"] for item in created}, {"S1", "S2"})
        self.assertTrue(all(item["effective_lots"] == [] for item in created))
        self.assertEqual(self.cases.dispatch_advisory(self.uid("a", "C")), [])
        for item in created:
            self.cases.deliver(item["notification_id"], lambda key, message: None)

    def test_batch_supplementation_reaches_only_advisory_recipients(self):
        self.cases.set_inventory("S1", "P", ["L1"])
        self.cases.set_inventory("S2", "P", ["L9"])
        self.ingest(1, {"authority": "a", "case": "C", "revision": 1, "product": "P", "lots": None})
        for item in self.cases.dispatch_advisory(self.uid("a", "C")):
            self.cases.deliver(item["notification_id"], lambda key, message: None)
        # 批次公布后才开门、且明显持有受影响批次的门店，此前没见过任何公告。
        self.cases.add_store("S3", ["m3"])
        self.cases.set_inventory("S3", "P", ["L1"])
        self.ingest(2, {"authority": "a", "case": "C", "revision": 2, "product": "P", "lots": ["L1"]})
        plan = self.cases.plan(self.uid("a", "C"))
        by_store = {item["store_id"]: item for item in plan}
        self.assertEqual(by_store["S1"]["kind"], "supplement")
        self.assertEqual(by_store["S1"]["effective_lots"], ["L1"])
        self.assertEqual(by_store["S2"]["kind"], "closure")
        self.assertEqual(by_store["S3"]["kind"], "initial")
        for item in self.cases.dispatch(self.uid("a", "C")):
            self.cases.deliver(item["notification_id"], lambda key, message: None)
        self.assertEqual(self.cases.plan(self.uid("a", "C")), [])

    def test_closed_store_is_not_notified_again_until_scope_changes(self):
        self.cases.set_inventory("S2", "P", ["L9"])
        self.ingest(1, {"authority": "a", "case": "C", "revision": 1, "product": "P", "lots": None})
        for item in self.cases.dispatch_advisory(self.uid("a", "C")):
            self.cases.deliver(item["notification_id"], lambda key, message: None)
        self.ingest(2, {"authority": "a", "case": "C", "revision": 2, "product": "P", "lots": ["L1"]})
        for item in self.cases.dispatch(self.uid("a", "C")):
            self.cases.deliver(item["notification_id"], lambda key, message: None)
        self.assertEqual(self.cases.plan(self.uid("a", "C")), [])
        self.cases.set_inventory("S2", "P", ["L1"])
        plan = self.cases.plan(self.uid("a", "C"))
        self.assertEqual([(item["store_id"], item["kind"]) for item in plan], [("S2", "correction")])


class DeliveryTests(ServiceCase):
    def setUp(self):
        super().setUp()
        self.cases.add_store("S1", ["m1"])
        self.cases.set_inventory("S1", "P", ["L1"])
        self.ingest(1, {"authority": "a", "case": "C", "revision": 1, "product": "P", "lots": ["L1"]})
        self.notification_id = self.cases.dispatch(self.uid("a", "C"))[0]["notification_id"]

    def test_retry_reuses_one_key_and_one_notification(self):
        keys = []

        def flaky(key, message):
            keys.append(key)
            if len(keys) < 3:
                raise RuntimeError("provider unavailable")

        result = self.cases.deliver(self.notification_id, flaky, max_attempts=3, backoff=lambda attempt: 0)
        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["attempts"], 3)
        self.assertEqual(len(set(keys)), 1)
        self.assertEqual(len(self.cases.notifications_for()), 1)
        attempts = self.cases.delivery_attempts(self.notification_id)
        self.assertEqual([item["outcome"] for item in attempts], ["failed", "failed", "sent"])
        self.assertEqual({item["idempotency_key"] for item in attempts}, {result["idempotency_key"]})

    def test_exhausted_delivery_keeps_notification_and_retries_later(self):
        def always_fails(key, message):
            raise RuntimeError("still down")

        first = self.cases.deliver(self.notification_id, always_fails, max_attempts=2, backoff=lambda attempt: 0)
        self.assertEqual(first["status"], "failed")
        self.assertEqual(len(self.cases.notifications_for()), 1)
        second = self.cases.deliver(self.notification_id, lambda key, message: None)
        self.assertEqual(second["status"], "sent")
        self.assertEqual(first["idempotency_key"], second["idempotency_key"])

    def test_successful_notification_is_not_redelivered(self):
        calls = []
        self.assertEqual(self.cases.deliver(self.notification_id, lambda key, message: calls.append(key))["attempts"], 1)
        again = self.cases.deliver(self.notification_id, lambda key, message: calls.append(key))
        self.assertTrue(again["reused"])
        self.assertEqual(len(calls), 1)

    def test_superseded_notification_is_never_delivered(self):
        self.cases.set_inventory("S1", "P", ["L1", "L9"])
        self.ingest(2, {"authority": "a", "case": "C", "revision": 2, "product": "P", "lots": ["L1", "L9"]})
        newer = self.cases.dispatch(self.uid("a", "C"))[0]
        self.assertEqual(self.cases.notification(self.notification_id)["status"], "superseded")
        sent = []
        stale = self.cases.deliver(self.notification_id, lambda key, message: sent.append(key))
        self.assertEqual(stale["status"], "superseded")
        self.assertEqual(sent, [])
        self.assertEqual(self.cases.deliver(newer["notification_id"], lambda key, message: sent.append(key))["status"], "sent")
        self.assertEqual(len(sent), 1)

    def test_undelivered_correction_is_retried_not_duplicated(self):
        self.cases.set_inventory("S1", "P", ["L1", "L2"])
        self.cases.deliver(self.notification_id, lambda key, message: None)
        self.ingest(2, {"authority": "a", "case": "C", "revision": 2, "product": "P", "lots": ["L2"]})
        created = self.cases.dispatch(self.uid("a", "C"))
        self.assertEqual(len(created), 1)
        requirement = self.cases.plan(self.uid("a", "C"))
        self.assertEqual(len(requirement), 1)
        self.assertEqual(requirement[0]["pending_notification_id"], created[0]["notification_id"])
        self.assertTrue(requirement[0]["retry"])
        self.assertEqual(len(self.cases.notifications_for()), 2)


class ReviewTests(ServiceCase):
    def test_product_only_notice_waits_for_review(self):
        self.ingest(1, {"product": "P", "lots": None})
        queue = self.cases.review_queue()
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["reason"], "missing_case_identity")
        self.assertEqual(self.cases.list_cases(), [])

    def test_same_product_name_does_not_merge_cases(self):
        self.ingest(1, {"product": "P", "lots": None})
        observation_id = self.cases.review_queue()[0]["observation_id"]
        self.cases.resolve_review(observation_id, "district-x", "RC-9", actor="operator")
        self.ingest(2, {"authority": "district-y", "case": "RC-9", "revision": 1, "product": "P", "lots": ["L9"]})
        cases = self.cases.list_cases()
        self.assertEqual(len(cases), 2)
        self.assertEqual({case["state"] for case in cases}, {"active", "pending_lots"})
        self.assertEqual(self.cases.review_queue(), [])

    def test_manual_confirmation_survives_later_ingestion(self):
        self.ingest(1, {"product": "P", "lots": None})
        observation_id = self.cases.review_queue()[0]["observation_id"]
        self.cases.resolve_review(observation_id, "district-x", "RC-9", actor="operator-1")
        self.ingest(2, {"authority": "district-x", "case": "RC-9", "revision": 2, "product": "P", "lots": ["L1"]})
        case = [item for item in self.cases.list_cases() if item["authority"] == "district-x"][0]
        self.assertEqual(case["review_status"], "confirmed")
        self.assertEqual(case["confirmed_by"], "operator-1")
        self.assertEqual(case["effective_lots"], ["L1"])
        self.assertEqual(self.cases.review_queue(), [])

    def test_conflicting_evidence_goes_to_review_instead_of_merging(self):
        self.ingest(1, {"authority": "a", "case": "C", "revision": 1, "product": "P", "lots": ["L1"]})
        self.ingest(2, {"authority": "a", "case": "C", "revision": 2, "product": "Q", "lots": ["L2"]})
        case = self.cases.list_cases()[0]
        self.assertEqual(case["review_status"], "pending_review")
        self.assertEqual(case["review_reason"], "product_conflict")
        self.assertEqual(case["effective_lots"], ["L1"])
        self.assertEqual(len(self.cases.review_queue()), 1)

    def test_review_pending_case_is_excluded_from_store_scope(self):
        self.cases.add_store("S1", ["m1"])
        self.cases.set_inventory("S1", "P", ["L1"])
        self.ingest(1, {"authority": "a", "case": "C", "revision": 1, "product": "P", "lots": ["L1"]})
        self.assertEqual(len(self.cases.query_scope("S1")), 1)
        self.ingest(2, {"authority": "a", "case": "C", "revision": 2, "product": "Q", "lots": ["L2"]})
        self.assertEqual(self.cases.query_scope("S1"), [])
        observation_id = self.cases.review_queue()[0]["observation_id"]
        self.cases.resolve_review(observation_id, "b", "D", actor="op")
        self.assertEqual(self.cases.query_scope("S1")[0]["effective_lots"], ["L1"])

    def test_manual_reassignment_removes_decision_from_old_case(self):
        self.ingest(
            1,
            {"authority": "a", "case": "C", "revision": 1, "product": "P", "lots": ["L1"]},
            {"product": "P", "revision": 2, "lots": ["L2"]},
        )
        observation_id = self.cases.review_queue()[0]["observation_id"]
        self.cases.resolve_review(observation_id, "b", "D", actor="op")
        self.assertEqual(
            [item["observation_id"] for item in self.cases.decision_ledger(self.uid("a", "C"))],
            [1],
        )
        self.assertEqual(
            [item["disposition"] for item in self.cases.decision_ledger(self.uid("b", "D"))],
            ["applied"],
        )

    def test_manual_confirmation_of_missing_revision_makes_scope_executable(self):
        self.cases.add_store("S1", ["m1"])
        self.cases.set_inventory("S1", "P", ["L1"])
        self.ingest(1, {"authority": "a", "case": "C", "product": "P", "lots": ["L1"]})
        self.assertEqual(self.cases.list_cases()[0]["review_reason"], "missing_revision")
        observation_id = self.cases.review_queue()[0]["observation_id"]
        self.cases.resolve_review(observation_id, "a", "C", actor="op")
        case = self.cases.list_cases()[0]
        self.assertEqual(case["state"], "active")
        self.assertEqual(case["effective_lots"], ["L1"])
        self.assertEqual(len(self.cases.query_scope("S1")), 1)

    def test_resolve_requires_actor_and_identity(self):
        self.ingest(1, {"product": "P", "lots": None})
        observation_id = self.cases.review_queue()[0]["observation_id"]
        with self.assertRaises(ValueError):
            self.cases.resolve_review(observation_id, "x", "C", actor=" ")
        with self.assertRaises(ValueError):
            self.cases.resolve_review(observation_id, "", "C", actor="op")
        with self.assertRaises(KeyError):
            self.cases.resolve_review(999, "x", "C", actor="op")

    def test_reassigning_observation_rebuilds_both_cases(self):
        self.ingest(1, {"authority": "a", "case": "C", "revision": 1, "product": "P", "lots": ["L1"]})
        self.ingest(2, {"product": "P", "revision": 4, "lots": ["L2"]})
        observation_id = self.cases.review_queue()[0]["observation_id"]
        self.cases.resolve_review(observation_id, "b", "D", actor="op")
        self.assertEqual(self.cases.list_cases()[0]["effective_lots"], ["L1"])
        target = [case for case in self.cases.list_cases() if case["authority"] == "b"][0]
        self.assertEqual(target["effective_lots"], ["L2"])
        self.assertEqual(target["review_status"], "confirmed")


class AuditTests(ServiceCase):
    def test_notification_traces_back_to_announcement(self):
        self.cases.add_store("S1", ["m1"])
        self.cases.set_inventory("S1", "P", ["L1"])
        self.ingest(1, {"authority": "a", "case": "C", "revision": 2, "product": "P", "lots": ["L1"]})
        self.ingest(2, {"authority": "a", "case": "C", "revision": 3, "product": "P", "lots": ["L1"], "status": "corrected", "withdrawn_lots": ["L2"]})
        created = self.cases.dispatch(self.uid("a", "C"))
        notification = self.cases.notification(created[0]["notification_id"])
        basis = notification["basis"]
        self.assertEqual(basis["source"], "feed")
        self.assertEqual(basis["capture_id"], 2)
        self.assertEqual(basis["revision"], 3)
        self.assertEqual(len(basis["content_digest"]), 64)

    def test_audit_lists_decisions_and_delivery_attempts(self):
        self.cases.add_store("S1", ["m1"])
        self.cases.set_inventory("S1", "P", ["L1"])
        self.ingest(1, {"authority": "a", "case": "C", "revision": 1, "product": "P", "lots": ["L1"]})
        notification_id = self.cases.dispatch(self.uid("a", "C"))[0]["notification_id"]
        self.cases.deliver(notification_id, lambda key, message: None)
        audit = self.cases.audit_case(self.uid("a", "C"))
        self.assertEqual(audit["case"], "C")
        self.assertEqual([item["disposition"] for item in audit["decisions"]], ["applied"])
        self.assertEqual(len(audit["notifications"]), 1)
        self.assertEqual(audit["notifications"][0]["deliveries"][0]["outcome"], "sent")

    def test_export_is_json_serialisable(self):
        self.ingest(1, {"authority": "a", "case": "C", "revision": 1, "product": "P", "lots": ["L1"]})
        exported = json.loads(self.cases.export_case(self.uid("a", "C")))
        self.assertEqual(exported["authority"], "a")
        self.assertEqual(exported["effective_lots"], ["L1"])


class IntakeTests(unittest.TestCase):
    def test_archived_capture_is_decoded_and_reingested(self):
        scenario = load_scenario()
        with serve(scenario) as origin, tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = Archive(root / "raw.sqlite3")
            connection = connect()
            cases = CaseService(connection)
            try:
                capture_id, response = __import__(
                    "food_recall.client", fromlist=["collect"]
                ).collect(
                    __import__("food_recall.client", fromlist=["Client"]).Client(origin.base_url),
                    archive,
                    "fixture",
                )
                ingestor = RecallIngestor(archive, cases)
                result = ingestor.ingest_capture(capture_id)
                self.assertEqual(len(result["accepted"]), len(scenario["source_records"]))
                self.assertEqual(len(cases.list_cases()), 2)
                replay = ingestor.ingest_capture(capture_id)
                self.assertEqual(len(cases.list_cases()), 2)
                self.assertEqual(
                    [item["observation_id"] for item in replay["accepted"]],
                    [item["observation_id"] for item in result["accepted"]],
                )
                self.assertFalse(any(item["created"] for item in replay["accepted"]))
            finally:
                connection.close()
                archive.close()

    def test_non_success_capture_is_skipped_by_source_replay(self):
        connection = connect()
        cases = CaseService(connection)
        with tempfile.TemporaryDirectory() as temporary:
            archive = Archive(Path(temporary) / "raw.sqlite3")
            try:
                archive.append("fixture", "/unstable", 503, {"content-type": "application/json"}, b"{}")
                result = RecallIngestor(archive, cases).ingest_source("fixture")
                self.assertEqual(result["captures"][0]["skipped"], "non-success response")
                self.assertEqual(cases.list_cases(), [])
            finally:
                archive.close()
                connection.close()


if __name__ == "__main__":
    unittest.main()
