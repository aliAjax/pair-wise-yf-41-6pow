import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def _reports(*items):
    return [
        {
            "station": station,
            "amplitude": amplitude,
            "time_offset": idx,
            "distance_km": float(idx),
        }
        for idx, (station, amplitude) in enumerate(items)
    ]


class MergeSplitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.analyst = Actor("analyst", "analyst")
        self.reviewer = Actor("reviewer", "reviewer")

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
        return self.service.create(self.admin, "event", data)

    def test_merge_combines_reports_and_dedupes_station(self):
        a = self._event("A", _reports(("STA-1", 1.0), ("STA-2", 3.0)))
        b = self._event("B", _reports(("STA-2", 5.0), ("STA-3", 7.0)))
        merged = self.service.transition(
            self.analyst, a["id"], "merge", {"source_event_id": b["id"]}
        )
        stations = [r["station"] for r in merged["data"]["reports"]]
        self.assertEqual(stations, ["STA-1", "STA-2", "STA-3"])
        # one report per station
        self.assertEqual(len(stations), len(set(stations)))
        # source is marked merged and points at the target
        source = self.service.get(b["id"])
        self.assertEqual(source["status"], "merged")
        self.assertEqual(source["data"]["merged_into"], a["id"])
        # reports moved to the target: the source no longer owns any
        self.assertEqual(source["data"].get("reports"), None)
        # the source's pre-merge reports survive in version history
        pre_merge = self.service.get_version(b["id"], source["version"] - 1)
        self.assertEqual(len(pre_merge["data"]["reports"]), 2)

    def test_merge_recomputes_magnitude_from_combined_reports(self):
        a = self._event("A", _reports(("STA-1", 1.0), ("STA-2", 3.0)))
        b = self._event("B", _reports(("STA-2", 5.0), ("STA-3", 7.0)))
        merged = self.service.transition(
            self.analyst, a["id"], "merge", {"source_event_id": b["id"]}
        )
        # median of [1.0, 3.0, 7.0]
        self.assertEqual(merged["data"]["magnitude"], 3.0)

    def test_merge_conflict_reports_where_event_went(self):
        a = self._event("A", _reports(("STA-1", 1.0), ("STA-2", 3.0)))
        b = self._event("B", _reports(("STA-3", 5.0), ("STA-4", 7.0)))
        self.service.transition(
            self.analyst, a["id"], "merge", {"source_event_id": b["id"]}
        )
        c = self._event("C", _reports(("STA-5", 2.0), ("STA-6", 4.0)))
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(
                self.analyst, c["id"], "merge", {"source_event_id": b["id"]}
            )
        self.assertIn(a["id"], str(ctx.exception))

    def test_split_moves_selected_reports_and_recomputes(self):
        a = self._event("A", _reports(("STA-1", 1.0), ("STA-2", 3.0), ("STA-3", 7.0)))
        sta3 = [r for r in a["data"]["reports"] if r["station"] == "STA-3"][0]
        split = self.service.transition(
            self.analyst,
            a["id"],
            "split",
            {"report_ids": [sta3["id"]], "title": "A-aftershock"},
        )
        self.assertEqual(
            [r["station"] for r in split["data"]["reports"]], ["STA-1", "STA-2"]
        )
        # magnitude recomputed from the reports that remain
        self.assertEqual(split["data"]["magnitude"], 2.0)
        # the moved report now belongs to the new event
        new_events = [
            e for e in self.service.list("event") if e["id"] != a["id"]
        ]
        self.assertEqual(len(new_events), 1)
        self.assertEqual(
            [r["station"] for r in new_events[0]["data"]["reports"]], ["STA-3"]
        )

    def test_split_into_existing_event(self):
        a = self._event("A", _reports(("STA-1", 1.0), ("STA-2", 3.0)))
        b = self._event("B", _reports(("STA-3", 7.0), ("STA-4", 9.0)))
        sta1 = [r for r in a["data"]["reports"] if r["station"] == "STA-1"][0]
        updated = self.service.transition(
            self.analyst,
            a["id"],
            "split",
            {"report_ids": [sta1["id"]], "target_event_id": b["id"]},
        )
        self.assertEqual([r["station"] for r in updated["data"]["reports"]], ["STA-2"])
        target = self.service.get(b["id"])
        self.assertEqual(
            [r["station"] for r in target["data"]["reports"]],
            ["STA-3", "STA-4", "STA-1"],
        )

    def test_published_event_reverts_to_pending_review_on_merge(self):
        a = self._event("A", _reports(("STA-1", 2.0), ("STA-2", 4.0)))
        self.service.transition(self.admin, a["id"], "associate", {})
        self.service.transition(
            self.admin, a["id"], "review", {"reviewer": "R1", "magnitude": 3.0}
        )
        self.service.transition(
            self.admin, a["id"], "publish", {"communication_id": "C1"}
        )
        b = self._event("B", _reports(("STA-3", 6.0), ("STA-4", 8.0)))
        merged = self.service.transition(
            self.analyst, a["id"], "merge", {"source_event_id": b["id"]}
        )
        self.assertEqual(merged["status"], "associated")
        # the previously published version is retained with its old magnitude
        versions = self.service.list_versions(a["id"])
        published = [v for v in versions if v["status"] == "published"][-1]
        self.assertEqual(published["data"]["magnitude"], 3.0)
        # the current draft carries the recomputed magnitude
        self.assertEqual(merged["data"]["magnitude"], 5.0)

    def test_backfill_recomputes_magnitude_from_reports(self):
        a = self._event("A", _reports(("STA-1", 1.5), ("STA-2", 2.5)))
        # simulate legacy data with no magnitude
        data = dict(a["data"])
        data.pop("magnitude", None)
        self.repo.update_entity(a["id"], a["version"], a["status"], data)
        result = self.service.backfill_magnitudes(self.admin)
        self.assertGreaterEqual(result["updated"], 1)
        refreshed = self.service.get(a["id"])
        self.assertEqual(refreshed["data"]["magnitude"], 2.0)

    def test_merge_requires_analyst_role(self):
        a = self._event("A", _reports(("STA-1", 1.0), ("STA-2", 3.0)))
        b = self._event("B", _reports(("STA-3", 5.0), ("STA-4", 7.0)))
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                Actor("viewer", "viewer"),
                a["id"],
                "merge",
                {"source_event_id": b["id"]},
            )

    def test_cannot_merge_event_with_itself(self):
        a = self._event("A", _reports(("STA-1", 1.0), ("STA-2", 3.0)))
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.analyst, a["id"], "merge", {"source_event_id": a["id"]}
            )


if __name__ == "__main__":
    unittest.main()
