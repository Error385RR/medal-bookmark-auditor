import csv
import datetime as dt
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from medal_audit import UTC, evaluate_bookmark, extract, infer_game, parse_metadata, sqlite_json_mode


class FormulaTests(unittest.TestCase):
    def test_sqlite_normalized_json_is_preferred_for_blob_metadata(self):
        self.assertEqual(parse_metadata(b"not directly decodable", '{"bookmarks":[{"name":"ACE"}]}'),
                         {"bookmarks": [{"name": "ACE"}]})

    def test_sqlite_jsonb_blob_is_normalized(self):
        connection = sqlite3.connect(":memory:")
        try:
            if sqlite_json_mode(connection) < 2:
                self.skipTest("SQLite build does not support JSONB")
            original = {"clipDuration": 12, "bookmarks": [{"name": "ACE"}]}
            blob = connection.execute("SELECT jsonb(?)", (json.dumps(original),)).fetchone()[0]
            normalized = connection.execute("SELECT CASE WHEN json_valid(?, 9) THEN json(?) END", (blob, blob)).fetchone()[0]
            self.assertEqual(parse_metadata(blob, normalized), original)
        finally:
            connection.close()

    def test_known_formula_positive_and_negative_stored_times(self):
        start = dt.datetime(2026, 8, 20, tzinfo=UTC)
        export = start + dt.timedelta(seconds=12)
        positive = evaluate_bookmark({"time": 3.25, "timestamp": (start.timestamp() + 15.25) * 1000}, 30, start, export, .001)
        negative = evaluate_bookmark({"time": -4, "timestamp": (start.timestamp() + 8) * 1000}, 30, start, export, .001)
        self.assertEqual(positive["recovered"], 15.25)
        self.assertEqual(negative["recovered"], 8)
        self.assertAlmostEqual(positive["consistency_difference"], 0, places=5)

    def test_boundaries_and_out_of_range(self):
        start = dt.datetime(2026, 1, 1, tzinfo=UTC); export = start + dt.timedelta(seconds=10)
        for t, expected in ((-10, "IN_RANGE"), (0, "IN_RANGE"), (0.01, "OUT_OF_RANGE")):
            recovered = t + 10
            stamp = (start.timestamp() + recovered) * 1000
            result = evaluate_bookmark({"time": t, "timestamp": stamp}, 10, start, export, .001)
            self.assertEqual(result["position_status"], expected)

    def test_missing_timing_and_precision(self):
        start = dt.datetime(2026, 1, 1, tzinfo=UTC)
        self.assertEqual(evaluate_bookmark({"time": 2}, 10, None, start, .001)["position_status"], "MISSING_TIMING_DATA")
        self.assertEqual(evaluate_bookmark({"time": 2, "timestamp": 1}, None, start, start, .001)["position_status"], "MISSING_TIMING_DATA")
        self.assertEqual(evaluate_bookmark({"time": 2}, 10, start, start, .001)["position_status"], "MISSING_TIMING_DATA")
        result = evaluate_bookmark({"time": 1.234567, "timestamp": (start.timestamp() + 1.234568) * 1000}, 10, start, start, .001)
        self.assertAlmostEqual(result["timestamp_position"], 1.234568, places=5)

    def test_game_inference_is_cautious(self):
        self.assertEqual(infer_game("C:/clips/VALORANT/game.mp4"), "valorant")
        self.assertEqual(infer_game("C:/misc/export.mp4"), "Unknown")


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path(__file__).resolve().parents[1] / ".test_tmp"
        temp_root.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=temp_root)
        self.root = Path(self.tmp.name)
        self.db = self.root / "sample.db"
        self.out = self.root / "reports"
        con = sqlite3.connect(self.db)
        try:
            con.execute("CREATE TABLE contents (created_at INTEGER NOT NULL, category_id TEXT, video_path TEXT, image_path TEXT, thumbnail_path TEXT, metadata BLOB NOT NULL, remote_content_id TEXT, local_content_id TEXT UNIQUE NOT NULL, parent_id TEXT, film_reel_path TEXT)")
            self.add(con, "v1", "C:/clips/Valorant/a.mp4", "2026-08-14T00:00:00Z", [{"name":"Kill","time":1,"timestamp":1}])
            self.add(con, "v2", "C:/clips/Valorant/b.mp4", "2026-08-15T00:00:00Z", [{"name":"4K","time":-2,"timestamp":1755216000000}])
            self.add(con, "r1", "C:/clips/Roblox/c.mp4", "2024-01-01T00:00:00Z", [])
            con.execute("INSERT INTO contents (created_at, video_path, metadata, local_content_id) VALUES (?, ?, ?, ?)", (1, "C:/clips/Tekken/bad.mp4", b"{bad", "bad"))
            con.commit()
        finally:
            con.close()

    @staticmethod
    def add(con, cid, path, created, bookmarks):
        when = int(dt.datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp())
        start = "2026-08-15T00:00:00Z"
        export = "2026-08-15T00:00:10Z"
        start_epoch = dt.datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp()
        for bookmark in bookmarks:
            if "time" in bookmark:
                bookmark["timestamp"] = (start_epoch + 10 + float(bookmark["time"])) * 1000
        meta = {"clipDuration": 20, "triggerType": "ICYMI", "captureType": "InjectionCapture", "clipType": "clip",
                "recorder": {"clipStartTime": start, "exportStatsTimestamp": export}, "bookmarks": bookmarks}
        con.execute("INSERT INTO contents (created_at, video_path, metadata, local_content_id) VALUES (?, ?, ?, ?)", (when, path, json.dumps(meta).encode(), cid))

    def tearDown(self): self.tmp.cleanup()

    def test_date_and_valorant_cutoff_filters_and_csv_outputs(self):
        clips, marks = extract(self.db, self.out, None, None, ["valorant", "roblox"], dt.date(2026, 8, 15), set(), set(), set(), True, True, True, .001, False)
        self.assertEqual((clips, marks), (2, 1))
        with (self.out / "clips.csv").open(encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual({r["local_content_id"] for r in rows}, {"v2", "r1"})
        self.assertEqual(next(r for r in rows if r["local_content_id"] == "v2")["in_range_count"], "1")

    def test_inclusive_start_and_end_date(self):
        clips, marks = extract(self.db, self.out, dt.date(2026, 8, 15), dt.date(2026, 8, 15), ["Valorant"], dt.date(2026, 8, 15), set(), set(), set(), True, True, True, .001, False)
        self.assertEqual((clips, marks), (1, 1))

    def test_missing_video_and_out_of_range_options(self):
        clips, marks = extract(self.db, self.out, None, None, ["tekken"], None, set(), set(), set(), True, True, False, .001, False)
        self.assertEqual((clips, marks), (0, 0))

    def test_malformed_metadata_record_survives_when_requested(self):
        clips, marks = extract(self.db, self.out, dt.date(1970, 1, 1), dt.date(1970, 1, 1), None, None, set(), set(), set(), True, True, True, .001, False)
        self.assertEqual((clips, marks), (1, 0))


if __name__ == "__main__":
    unittest.main()
