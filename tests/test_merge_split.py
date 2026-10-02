import threading
import tempfile
import unittest
from pathlib import Path

from src.domain import (
    Actor,
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class MergeSplitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.analyst = Actor("analyst-1", "analyst")
        self.admin = Actor("admin", "admin")
        self.reviewer = Actor("reviewer-1", "reviewer")

    def tearDown(self):
        self.tmp.cleanup()

    def _event(self, title, reports, **extra):
        data = {
            "title": title,
            "origin_time": "2026-01-01T00:00:00Z",
            "location": "Region",
            "reports": reports,
        }
        data.update(extra)
        return self.service.create(self.analyst, "event", data)

    def _publish(self, entity, magnitude=4.0):
        self.service.transition(self.analyst, entity["id"], "associate", {})
        self.service.transition(
            self.reviewer,
            entity["id"],
            "review",
            {"reviewer": "R-1", "magnitude": magnitude},
        )
        return self.service.transition(
            self.reviewer, entity["id"], "publish", {"communication_id": "C-1"}
        )

    # ------------------------------------------------------------ merge

    def test_merge_deduplicates_and_keeps_single_ownership(self):
        a = self._event(
            "A",
            [
                {"station": "S1", "time_offset": 1, "distance_km": 1.0},
                {"station": "S2", "time_offset": 2, "distance_km": 1.0},
            ],
        )
        b = self._event(
            "B",
            [
                {"station": "S2", "time_offset": 9, "distance_km": 2.0},
                {"station": "S3", "time_offset": 3, "distance_km": 1.2},
            ],
        )
        merged = self.service.transition(
            self.analyst, a["id"], "merge", {"source_id": b["id"], "reason": "same quake"}
        )
        self.assertEqual(merged["status"], "candidate")
        stations = [report["station"] for report in merged["data"]["reports"]]
        self.assertEqual(stations, ["S1", "S2", "S3"])  # 同台站只留一份
        source = self.service.get(b["id"])
        self.assertEqual(source["status"], "merged")
        self.assertEqual(source["data"]["merged_into"], a["id"])
        self.assertEqual(source["data"]["reports"], [])  # 一份报文只归一个事件

    def test_merge_recomputes_magnitude_from_combined_reports(self):
        a = self._event(
            "A",
            [
                {"station": "S1", "magnitude": 4.0},
                {"station": "S2", "magnitude": 4.2},
            ],
            magnitude=3.5,
        )
        b = self._event(
            "B",
            [
                {"station": "S3", "magnitude": 4.6},
                {"station": "S4", "magnitude": 4.4},
            ],
        )
        merged = self.service.transition(
            self.analyst, a["id"], "merge", {"source_id": b["id"]}
        )
        self.assertEqual(merged["data"]["magnitude"], 4.3)  # 按并后的报文重算

    def test_merge_published_target_returns_to_review_and_keeps_old_version(self):
        a = self._event("A", [{"station": "S1"}, {"station": "S2"}])
        b = self._event("B", [{"station": "S3"}, {"station": "S4"}])
        published = self._publish(a, magnitude=4.2)
        merged = self.service.transition(
            self.analyst,
            a["id"],
            "merge",
            {"source_id": b["id"]},
            expected_version=published["version"],
        )
        self.assertEqual(merged["status"], "associated")  # 发布过的一边退回待复核
        history = merged["data"]["previous_publications"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["communication_id"], "C-1")
        self.assertEqual(history[0]["magnitude"], 4.2)
        self.assertEqual(len(history[0]["reports"]), 2)  # 先前外发内容留着旧版

    def test_merge_published_source_keeps_its_old_version(self):
        a = self._event("A", [{"station": "S1"}, {"station": "S2"}])
        b = self._event("B", [{"station": "S3"}, {"station": "S4"}])
        self._publish(b, magnitude=4.8)
        merged = self.service.transition(
            self.analyst, a["id"], "merge", {"source_id": b["id"]}
        )
        self.assertEqual(merged["status"], "associated")
        source = self.service.get(b["id"])
        self.assertEqual(source["status"], "merged")
        self.assertEqual(len(source["data"]["previous_publications"]), 1)
        self.assertEqual(
            source["data"]["previous_publications"][0]["magnitude"], 4.8
        )

    def test_second_merge_submission_sees_survivor(self):
        a = self._event("A", [{"station": "S1"}, {"station": "S2"}])
        b = self._event("B", [{"station": "S3"}, {"station": "S4"}])
        self.service.transition(self.analyst, a["id"], "merge", {"source_id": b["id"]})
        other = Actor("analyst-2", "analyst")
        with self.assertRaises(ConflictError) as same_direction:
            self.service.transition(other, a["id"], "merge", {"source_id": b["id"]})
        self.assertIn(a["id"], str(same_direction.exception))
        with self.assertRaises(ConflictError) as reverse_direction:
            self.service.transition(other, b["id"], "merge", {"source_id": a["id"]})
        self.assertIn(a["id"], str(reverse_direction.exception))

    def test_concurrent_merge_first_wins_loser_sees_survivor(self):
        a = self._event("A", [{"station": "S1"}, {"station": "S2"}])
        b = self._event("B", [{"station": "S3"}, {"station": "S4"}])
        results = {}

        def submit(name):
            try:
                results[name] = self.service.transition(
                    Actor(name, "analyst"), a["id"], "merge", {"source_id": b["id"]}
                )
            except Exception as exc:  # noqa: BLE001 - collected for assertions
                results[name] = exc

        threads = [
            threading.Thread(target=submit, args=("user-%d" % index,))
            for index in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        outcomes = list(results.values())
        successes = [item for item in outcomes if not isinstance(item, Exception)]
        failures = [item for item in outcomes if isinstance(item, ConflictError)]
        self.assertEqual(len(successes), 1)  # 先提交的生效
        self.assertEqual(len(failures), 1)
        self.assertIn(a["id"], str(failures[0]))  # 后提交的看到并到了哪条

    def test_merge_rejects_bad_role_and_inactive_events(self):
        a = self._event("A", [{"station": "S1"}, {"station": "S2"}])
        b = self._event("B", [{"station": "S3"}, {"station": "S4"}])
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                Actor("viewer", "viewer"), a["id"], "merge", {"source_id": b["id"]}
            )
        published = self._publish(a)
        self.service.transition(
            self.reviewer, a["id"], "withdraw", {"reason": "mistake"},
            expected_version=published["version"],
        )
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.analyst, b["id"], "merge", {"source_id": a["id"]}
            )

    # ------------------------------------------------------------ split

    def test_split_moves_selected_reports_to_new_event(self):
        a = self._event(
            "A",
            [
                {"station": "S1", "magnitude": 4.0},
                {"station": "S2", "magnitude": 4.2},
                {"station": "S3", "magnitude": 5.0},
                {"station": "S4", "magnitude": 5.2},
            ],
        )
        updated = self.service.transition(
            self.analyst,
            a["id"],
            "split",
            {"stations": ["S3", "S4"], "new_event": {"title": "B"}},
        )
        self.assertEqual(
            [report["station"] for report in updated["data"]["reports"]], ["S1", "S2"]
        )
        self.assertEqual(updated["data"]["magnitude"], 4.1)  # 剩下的报文重算
        new_id = updated["data"]["split_history"][0]["destination_id"]
        new_event = self.service.get(new_id)
        self.assertEqual(new_event["status"], "candidate")
        self.assertEqual(
            [report["station"] for report in new_event["data"]["reports"]],
            ["S3", "S4"],
        )
        self.assertEqual(new_event["data"]["split_from"], a["id"])
        self.assertEqual(new_event["data"]["magnitude"], 5.1)
        # 一份报文只归一个事件
        source_stations = {report["station"] for report in updated["data"]["reports"]}
        new_stations = {report["station"] for report in new_event["data"]["reports"]}
        self.assertEqual(source_stations & new_stations, set())

    def test_split_to_existing_event_deduplicates(self):
        a = self._event("A", [{"station": "S1"}, {"station": "S2"}, {"station": "S3"}])
        b = self._event("B", [{"station": "S3", "magnitude": 4.9}, {"station": "S4"}])
        updated = self.service.transition(
            self.analyst, a["id"], "split", {"stations": ["S3"], "target_id": b["id"]}
        )
        self.assertEqual(
            [report["station"] for report in updated["data"]["reports"]], ["S1", "S2"]
        )
        target = self.service.get(b["id"])
        self.assertEqual(
            [report["station"] for report in target["data"]["reports"]], ["S3", "S4"]
        )

    def test_split_published_source_returns_to_review_with_old_version(self):
        a = self._event(
            "A", [{"station": station} for station in ("S1", "S2", "S3", "S4")]
        )
        published = self._publish(a, magnitude=4.6)
        updated = self.service.transition(
            self.analyst,
            a["id"],
            "split",
            {"stations": ["S3", "S4"], "new_event": {"title": "B"}},
            expected_version=published["version"],
        )
        self.assertEqual(updated["status"], "associated")  # 退回待复核
        history = updated["data"]["previous_publications"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["magnitude"], 4.6)
        self.assertEqual(len(history[0]["reports"]), 4)

    def test_split_validation(self):
        a = self._event("A", [{"station": "S1"}, {"station": "S2"}, {"station": "S3"}])
        with self.assertRaises(ValidationError):  # 源事件至少留两条报文
            self.service.transition(
                self.analyst, a["id"], "split",
                {"stations": ["S2", "S3"], "new_event": {"title": "B"}},
            )
        with self.assertRaises(ValidationError):  # 台站不存在
            self.service.transition(
                self.analyst, a["id"], "split",
                {"stations": ["S9"], "target_id": a["id"]},
            )
        b = self._event(
            "B",
            [{"station": station} for station in ("S1", "S2", "S3", "S4")],
        )
        with self.assertRaises(ValidationError):  # 新事件至少两条报文
            self.service.transition(
                self.analyst, b["id"], "split",
                {"stations": ["S4"], "new_event": {"title": "C"}},
            )
        with self.assertRaises(ValidationError):  # target_id和new_event二选一
            self.service.transition(
                self.analyst, b["id"], "split",
                {"stations": ["S4"], "target_id": a["id"], "new_event": {"title": "C"}},
            )

    # ------------------------------------------------------------ backfill

    def test_backfill_magnitudes_from_reports(self):
        old = self._event(
            "old",
            [
                {"station": "S1", "magnitude": 3.6},
                {"station": "S2", "magnitude": 4.0},
                {"station": "S3", "magnitude": 4.4},
            ],
        )
        keep = self._event(
            "keep", [{"station": "S1"}, {"station": "S2"}], magnitude=9.9
        )
        nomag = self._event("nomag", [{"station": "S1"}, {"station": "S2"}])
        updated = self.service.backfill_magnitudes(self.admin)
        self.assertEqual([entity["id"] for entity in updated], [old["id"]])
        self.assertEqual(self.service.get(old["id"])["data"]["magnitude"], 4.0)
        self.assertEqual(self.service.get(keep["id"])["data"]["magnitude"], 9.9)
        self.assertIsNone(self.service.get(nomag["id"])["data"].get("magnitude"))
        actions = [
            entry["action"] for entry in self.service.audit_log(entity_id=old["id"])
        ]
        self.assertIn("backfill_magnitude", actions)

    def test_backfill_requires_admin(self):
        with self.assertRaises(PermissionDenied):
            self.service.backfill_magnitudes(self.analyst)


if __name__ == "__main__":
    unittest.main()
