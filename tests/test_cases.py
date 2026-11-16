import json
import tempfile
import unittest
from pathlib import Path

from food_recall.archive import Archive
from food_recall.cases import CaseBook
from food_recall.client import Client, collect
from food_recall.origin import serve

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def record(authority="district-a", case="RC-8", revision=1, product="合成谷物棒",
           lots=None, **extra):
    item = {"authority": authority, "case": case, "revision": revision,
            "product": product, "lots": lots, "status": "published"}
    item.update(extra)
    return item


class CaseBookTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.book = CaseBook(Path(self.temporary.name) / "cases.sqlite3")

    def tearDown(self):
        self.book.close()
        self.temporary.cleanup()

    def add_store(self, store="S1", authorities=("district-a",), lots=()):
        self.book.add_store(store)
        for authority in authorities:
            self.book.subscribe(store, authority)
        self.book.set_inventory(store, list(lots))

    def ingest(self, item, **kwargs):
        return self.book.ingest(item, source="test", **kwargs)

    def test_pending_until_lots_arrive_then_same_case(self):
        first = self.ingest(record(revision=1, lots=None))
        case = self.book.get_case("district-a", "RC-8")
        self.assertEqual((case["state"], case["effective_lots"]), ("pending_lots", []))
        second = self.ingest(record(revision=2, lots=["L10", "L11"]))
        self.assertEqual(second["event"], "lots_supplied")
        self.assertEqual(second["case_id"], first["case_id"])
        self.assertEqual(len(self.book.cases()), 1)
        case = self.book.get_case("district-a", "RC-8")
        self.assertEqual((case["state"], case["effective_lots"]),
                         ("executable", ["L10", "L11"]))

    def test_same_number_in_different_authorities_stays_separate(self):
        self.ingest(record(authority="district-a", case="RC-8", revision=1, lots=["L10"]))
        self.ingest(record(authority="district-b", case="RC-8", revision=1, lots=["L20"]))
        cases = self.book.cases()
        self.assertEqual(len(cases), 2)
        self.assertEqual({case["authority"] for case in cases}, {"district-a", "district-b"})

    def test_product_name_alone_never_merges_cases(self):
        self.ingest(record(case="RC-8", revision=1, lots=["L10"]))
        self.ingest(record(case="RC-9", revision=1, lots=["L11"]))
        self.assertEqual(len(self.book.cases()), 2)
        self.assertEqual(self.book.review_queue(), [])

    def test_supplement_targets_previous_and_affected_not_everyone(self):
        self.add_store("S1", lots=["L10"])
        self.add_store("S2", lots=["L11"])
        self.add_store("S3")
        first = self.ingest(record(revision=1, lots=None))
        self.assertEqual({n["store"] for n in first["notified"]}, {"S1", "S2", "S3"})
        self.add_store("S4")  # 首版之后才订阅，无相关库存
        second = self.ingest(record(revision=2, lots=["L10", "L11"]))
        self.assertEqual({n["store"] for n in second["notified"]}, {"S1", "S2", "S3"})
        reasons = {n["store"]: n["reason"] for n in second["notified"]}
        self.assertEqual(reasons["S1"], "affected+previously_notified")
        self.assertEqual(reasons["S3"], "previously_notified")
        self.assertEqual(self.book.notifications(store_id="S4"), [])

    def test_unsubscribed_previous_recipient_still_gets_correction(self):
        self.add_store("S1", lots=["L10"])
        self.ingest(record(revision=1, lots=["L10"]))
        self.book.unsubscribe("S1", "district-a")
        result = self.ingest(record(revision=2, lots=["L9"], status="corrected"))
        self.assertEqual(result["event"], "corrected")
        self.assertEqual([n["store"] for n in result["notified"]], ["S1"])
        self.assertEqual(result["notified"][0]["reason"], "previously_notified")
        self.assertEqual(result["notified"][0]["kind"], "correction")

    def test_dynamic_inventory_pulls_store_into_scope(self):
        self.add_store("S1", lots=["L10"])
        self.ingest(record(revision=1, lots=["L10"]))
        self.add_store("S2")
        self.book.set_inventory("S2", ["L11"])  # 库存动态变化后才受影响
        result = self.ingest(record(revision=2, lots=["L10", "L11"]))
        self.assertEqual({n["store"] for n in result["notified"]}, {"S1", "S2"})
        first_seen = self.book.notifications(store_id="S2")
        self.assertEqual(len(first_seen), 1)
        self.assertEqual((first_seen[0]["kind"], first_seen[0]["reason"]),
                         ("supplement", "affected"))

    def test_old_page_replay_does_not_restore_withdrawn_scope(self):
        self.add_store("S1", lots=["L10", "L11"])
        self.ingest(record(revision=1, lots=["L10", "L11"]), capture_id=1)
        self.ingest(record(revision=2, lots=["L10"], withdrawn_lots=["L11"],
                           status="corrected"), capture_id=2)
        case = self.book.get_case("district-a", "RC-8")
        self.assertEqual((case["effective_lots"], case["withdrawn_lots"]),
                         (["L10"], ["L11"]))
        before = len(self.book.notifications())
        # 旧页被重新抓取是一次新捕获：登记为证据但不回退状态
        stale = self.ingest(record(revision=1, lots=["L10", "L11"]), capture_id=3)
        self.assertEqual(stale["event"], "stale")
        case = self.book.get_case("district-a", "RC-8")
        self.assertEqual((case["effective_lots"], case["withdrawn_lots"]),
                         (["L10"], ["L11"]))
        self.assertEqual(len(self.book.notifications()), before)
        timeline = self.book.case_timeline("district-a", "RC-8")
        self.assertEqual(len(timeline["observations"]), 3)
        self.assertFalse(timeline["observations"][-1]["applied"])

    def test_missing_identity_goes_to_review_then_manual_confirm(self):
        self.ingest(record(revision=1, lots=["L10"]))
        case_id = self.book.cases()[0]["id"]
        result = self.ingest({"product": "合成谷物棒", "lots": ["L10"], "status": "published"})
        self.assertEqual((result["event"], result["review"]),
                         ("pending_review", "missing_identity"))
        queue = self.book.review_queue()
        self.assertEqual(len(queue), 1)
        confirmed = self.book.confirm_association(
            queue[0]["observation_id"], case_id, note="门店核对为同一公告")
        self.assertEqual(confirmed["event"], "associated")
        self.assertEqual(self.book.review_queue(), [])
        timeline = self.book.case_timeline("district-a", "RC-8")
        manual = [o for o in timeline["observations"] if o["association"] == "manual"]
        self.assertEqual(len(manual), 1)
        again = self.ingest({"product": "合成谷物棒", "lots": ["L10"], "status": "published"})
        self.assertEqual(again["event"], "already_ingested")

    def test_conflicting_product_is_reviewed_not_applied(self):
        self.ingest(record(revision=1, lots=["L10"]))
        result = self.ingest(record(revision=2, product="合成麦片", lots=["L10"],
                                    status="corrected"))
        self.assertEqual((result["event"], result["review"]),
                         ("pending_review", "product_conflict"))
        case = self.book.get_case("district-a", "RC-8")
        self.assertEqual((case["product"], case["max_revision"]), ("合成谷物棒", 1))
        confirmed = self.book.confirm_association(result["observation_id"], case["id"])
        self.assertEqual(confirmed["event"], "applied")  # 仅产品名变化，无范围事件
        self.assertEqual(self.book.get_case("district-a", "RC-8")["product"], "合成麦片")

    def test_same_revision_with_different_content_is_conflict(self):
        self.ingest(record(revision=1, lots=["L10"]), capture_id=1)
        replay = self.ingest(record(revision=1, lots=["L10"]), capture_id=2)
        self.assertEqual(replay["event"], "duplicate")
        conflict = self.ingest(record(revision=1, lots=["L10", "L99"]), capture_id=3)
        self.assertEqual((conflict["event"], conflict["review"]),
                         ("pending_review", "revision_conflict"))
        self.assertEqual(self.book.get_case("district-a", "RC-8")["effective_lots"], ["L10"])

    def test_reingest_same_record_is_noop(self):
        first = self.ingest(record(revision=1, lots=["L10"]))
        second = self.ingest(record(revision=1, lots=["L10"]))
        self.assertEqual(second["event"], "already_ingested")
        self.assertEqual(second["observation_id"], first["observation_id"])
        self.assertEqual(len(self.book.notifications()), 0)

    def test_failed_retry_creates_no_duplicate_corrections(self):
        self.add_store("S1", lots=["L10"])
        self.add_store("S2", lots=["L11"])
        self.ingest(record(revision=1, lots=["L10", "L11"]))
        self.ingest(record(revision=2, lots=["L10"], withdrawn_lots=["L11"],
                           status="corrected"))
        corrections = [n for n in self.book.notifications() if n["kind"] == "correction"]
        self.assertEqual({n["store_id"] for n in corrections}, {"S1", "S2"})

        def flaky(payload):
            if payload["store_id"] == "S2":
                raise RuntimeError("channel down")

        first = self.book.dispatch(flaky)
        self.assertEqual(len(first["failed"]), 2)  # S2 的首版与更正都失败
        states = {n["store_id"]: n["state"] for n in self.book.notifications()
                  if n["kind"] == "correction"}
        self.assertEqual(states, {"S1": "sent", "S2": "failed"})
        total_before = len(self.book.notifications())
        second = self.book.dispatch()
        self.assertEqual(second["failed"], [])
        self.assertEqual(len(self.book.notifications()), total_before)
        self.assertEqual({n["state"] for n in self.book.notifications()}, {"sent"})
        timeline = self.book.case_timeline("district-a", "RC-8")
        correction_event = [e for e in timeline["events"] if e["kind"] == "corrected"][0]
        s2 = [n for n in correction_event["notifications"] if n["store_id"] == "S2"][0]
        self.assertEqual([d["outcome"] for d in s2["deliveries"]], ["failed", "sent"])
        third = self.book.dispatch()
        self.assertEqual((third["sent"], third["failed"]), ([], []))

    def test_store_scope_reflects_current_effective_range(self):
        self.add_store("S1", lots=["L10", "L11"])
        self.add_store("S2", authorities=("district-b",), lots=["L20"])
        self.ingest(record(revision=1, lots=None))
        self.ingest(record(revision=2, lots=["L10", "L11"]))
        self.ingest(record(revision=3, lots=["L10"], withdrawn_lots=["L11"],
                           status="corrected"))
        self.ingest(record(authority="district-b", case="RC-8", revision=1, lots=["L20"]))
        scope = self.book.store_scope("S1")
        self.assertEqual(len(scope["cases"]), 1)
        entry = scope["cases"][0]
        self.assertEqual(entry["effective_lots"], ["L10"])
        self.assertEqual(entry["inventory_hits"], ["L10"])
        self.assertEqual(entry["withdrawn_hits"], ["L11"])
        self.assertTrue(entry["notified"])
        other = self.book.store_scope("S2")["cases"]
        self.assertEqual([case["authority"] for case in other], ["district-b"])
        with self.assertRaises(KeyError):
            self.book.store_scope("S9")

    def test_unknown_case_timeline_and_observation_raise(self):
        with self.assertRaises(KeyError):
            self.book.case_timeline("district-a", "RC-404")
        with self.assertRaises(KeyError):
            self.book.confirm_association(999, 1)


class CaptureIntegrationTests(unittest.TestCase):
    def test_captured_announcements_drive_cases_and_audit(self):
        with serve() as origin, tempfile.TemporaryDirectory() as temporary:
            archive = Archive(Path(temporary) / "raw.sqlite3")
            book = CaseBook(Path(temporary) / "cases.sqlite3")
            try:
                book.add_store("S1")
                book.subscribe("S1", "district-a")
                book.set_inventory("S1", ["L10", "L11"])
                client = Client(origin.base_url)
                capture_ids = []
                for path in ("/observations/1", "/observations/2", "/observations/3"):
                    capture_id, _response = collect(client, archive, "fixture", path)
                    capture_ids.append(capture_id)
                    book.ingest_capture(archive, capture_id)
                case = book.get_case("district-a", "RC-8")
                self.assertEqual(
                    (case["state"], case["effective_lots"], case["withdrawn_lots"]),
                    ("executable", ["L10"], ["L11"]))
                timeline = book.case_timeline("district-a", "RC-8")
                self.assertEqual([o["capture_id"] for o in timeline["observations"]],
                                 capture_ids)
                self.assertEqual([e["kind"] for e in timeline["events"]],
                                 ["opened", "lots_supplied", "corrected"])
                self.assertEqual([e["basis"]["capture_id"] for e in timeline["events"]],
                                 capture_ids)
                # 重放同一归档不产生重复通知
                before = len(book.notifications())
                for capture_id in capture_ids:
                    result = book.ingest_capture(archive, capture_id)
                    self.assertEqual(
                        {r["event"] for r in result["results"]}, {"already_ingested"})
                self.assertEqual(len(book.notifications()), before)
                # 非 200 的归档响应不进入案件
                limited, _response = collect(client, archive, "fixture", "/rate-limited")
                skipped = book.ingest_capture(archive, limited)
                self.assertEqual(skipped["event"], "skipped")
                self.assertEqual(len(book.cases()), 1)
            finally:
                book.close()
                archive.close()


class BusinessCaseFixtureTests(unittest.TestCase):
    def test_business_cases(self):
        scenarios = json.loads((FIXTURES / "business_cases.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(scenarios), 3)
        for scenario in scenarios:
            with self.subTest(scenario=scenario["name"]):
                self.run_scenario(scenario)

    def run_scenario(self, scenario):
        with tempfile.TemporaryDirectory() as temporary:
            book = CaseBook(Path(temporary) / "cases.sqlite3")
            try:
                for store in scenario.get("stores", []):
                    book.add_store(store["id"], store.get("name", ""))
                    for authority in store.get("authorities", []):
                        book.subscribe(store["id"], authority)
                    book.set_inventory(store["id"], store.get("lots", []))
                for step in scenario["steps"]:
                    result = self.run_step(book, step)
                    expect = step.get("expect")
                    if expect is None or result is None:
                        continue
                    label = f"{scenario['name']} / {step.get('ingest', step)}"
                    self.assertEqual(result["event"], expect["event"], label)
                    if "review_reason" in expect:
                        self.assertEqual(result.get("review"), expect["review_reason"], label)
                    if "case" in expect:
                        source_record = step["ingest"]
                        case = book.get_case(source_record["authority"],
                                             str(source_record["case"]))
                        for key, value in expect["case"].items():
                            self.assertEqual(case[key], value, f"{label} 案件字段 {key}")
                    if "notifications" in expect:
                        actual = {
                            n["store"]: {"kind": n["kind"], "reason": n["reason"]}
                            for n in result["notified"]
                        }
                        expected = {
                            store: ({"kind": spec, "reason": None} if isinstance(spec, str)
                                    else {"kind": spec["kind"], "reason": spec["reason"]})
                            for store, spec in expect["notifications"].items()
                        }
                        for store, spec in expected.items():
                            self.assertIn(store, actual, f"{label} 缺少通知 {store}")
                            self.assertEqual(actual[store]["kind"], spec["kind"], label)
                            if spec["reason"] is not None:
                                self.assertEqual(actual[store]["reason"], spec["reason"], label)
                        self.assertEqual(set(actual), set(expected), label)
                final = scenario["final"]
                self.assertEqual(len(book.cases()), final["cases"], scenario["name"])
                self.assertEqual(len(book.review_queue()), final["review_open"], scenario["name"])
                counts = {}
                for notification in book.notifications():
                    counts[notification["store_id"]] = counts.get(notification["store_id"], 0) + 1
                for store, count in final.get("notification_counts", {}).items():
                    self.assertEqual(counts.get(store, 0), count,
                                     f"{scenario['name']} 门店 {store} 的通知数")
                for store, expected_cases in final.get("store_scope", {}).items():
                    scope = book.store_scope(store)["cases"]
                    actual = [
                        {"authority": entry["authority"], "case": entry["case_number"],
                         "inventory_hits": entry["inventory_hits"],
                         "withdrawn_hits": entry["withdrawn_hits"],
                         "notified": entry["notified"]}
                        for entry in scope
                    ]
                    self.assertEqual(actual, expected_cases,
                                     f"{scenario['name']} 门店 {store} 的有效范围")
            finally:
                book.close()

    @staticmethod
    def run_step(book, step):
        if "subscribe" in step:
            spec = step["subscribe"]
            book.add_store(spec["store"], spec.get("name", ""))
            for authority in spec.get("authorities", []):
                book.subscribe(spec["store"], authority)
            book.set_inventory(spec["store"], spec.get("lots", []))
            return None
        if "set_inventory" in step:
            spec = step["set_inventory"]
            book.set_inventory(spec["store"], spec["lots"])
            return None
        if "ingest" in step:
            return book.ingest(step["ingest"], source="business-cases",
                               capture_id=step.get("capture_id"))
        raise AssertionError(f"未知步骤: {step}")


if __name__ == "__main__":
    unittest.main()
