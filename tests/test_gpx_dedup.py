"""Offline regression tests: sources stay intact and statistics count once."""

import contextlib
import hashlib
import io
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "run_page"))

import generator
import synced_data_file_logger
from generator.db import init_db
from gpx_dedup import build_plan, read_record
from gpxtrackposter.track_loader import TrackLoader, load_gpx_file

from utils import make_activities_file

BASE = datetime(2026, 1, 1, tzinfo=UTC)
ET.register_namespace("", "http://www.topografix.com/GPX/1/1")


def iso(seconds, offset=0):
    return (
        (BASE + timedelta(seconds=seconds))
        .astimezone(timezone(timedelta(hours=offset)))
        .isoformat()
    )


def write_gpx(
    directory,
    name,
    start=0,
    duration=600,
    distance=1800,
    longitude_offset=0,
    sport="running",
    gps=True,
    step=5,
    gap=False,
    timezone_offset=0,
):
    points = []
    if gps:
        for t in range(start, start + duration + 1, step):
            if gap and 50 < t - start < duration - 50:
                continue
            points.append(
                f'<trkpt lat="{30 + t * 0.00002:.7f}" '
                f'lon="{120 + longitude_offset:.7f}"><ele>10</ele>'
                f"<time>{iso(t, timezone_offset)}</time></trkpt>"
            )
    path = directory / name
    path.write_text(
        '<gpx version="1.1" creator="regression" '
        'xmlns="http://www.topografix.com/GPX/1/1">'
        f"<metadata><time>{iso(start, timezone_offset)}</time></metadata>"
        f"<trk><name>Test</name><type>{sport}</type><trkseg>"
        + "".join(points)
        + "</trkseg></trk><extensions>"
        f"<distance>{distance}</distance><moving_time>{duration}</moving_time>"
        f"<elapsed_time>{duration}</elapsed_time>"
        f"<average_speed>{distance / duration}</average_speed>"
        f"<start_time>{iso(start, timezone_offset)}</start_time>"
        f"<end_time>{iso(start + duration, timezone_offset)}</end_time>"
        "</extensions></gpx>"
    )
    return path


def serial_load(files, load_func=load_gpx_file, activity_title_dict=None):
    return {f: load_func(f, activity_title_dict) for f in files}


class DedupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.gpx = self.root / "GPX_OUT"
        self.gpx.mkdir()
        self.db = self.root / "data.db"
        self.json = self.root / "activities.json"
        self.imported = self.root / "imported.json"
        self.enterContext(
            patch.object(synced_data_file_logger, "SYNCED_FILE", self.imported)
        )
        self.enterContext(
            patch.object(TrackLoader, "_load_data_tracks", side_effect=serial_load)
        )
        self.enterContext(patch("generator.db.g.reverse", return_value="Test city"))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def pair(self, **second):
        a = write_gpx(self.gpx, "a.gpx")
        b = write_gpx(self.gpx, "b.gpx", **({"start": 5} | second))
        return a, b

    def sync(self):
        make_activities_file(self.db, self.gpx, self.json)
        return json.loads(self.json.read_text())

    def rows(self):
        with sqlite3.connect(self.db) as conn:
            return conn.execute(
                "SELECT run_id, distance, duplicate_of FROM activities ORDER BY run_id"
            ).fetchall()

    def override(self, data):
        (self.root / "gpx_dedup_overrides.json").write_text(json.dumps(data))

    def test_near_simultaneous_same_route_is_duplicate(self):
        self.pair(longitude_offset=0.00003)
        plan = build_plan(self.gpx)
        self.assertEqual(plan.aliases, {"b.gpx": "a.gpx"})
        self.assertGreater(
            plan.report["overlapping_pairs"][0]["metrics"]["sample_coverage"], 0.99
        )

    def test_morning_evening_and_next_day_runs_are_separate(self):
        original = write_gpx(self.gpx, "a.gpx")
        for name, offset in (("b.gpx", 3600), ("c.gpx", 86400)):
            root = ET.fromstring(original.read_text())
            for element in root.iter():
                if element.tag.split("}")[-1] in {"time", "start_time", "end_time"}:
                    element.text = (
                        datetime.fromisoformat(element.text) + timedelta(seconds=offset)
                    ).isoformat()
            ET.ElementTree(root).write(self.gpx / name, encoding="utf-8")
        self.assertEqual(build_plan(self.gpx).aliases, {})
        self.assertEqual(len(self.sync()), 3)

    def test_simultaneous_different_routes_remain(self):
        self.pair(longitude_offset=0.003)
        self.assertEqual(build_plan(self.gpx).aliases, {})
        self.assertEqual(len(self.sync()), 2)

    def test_different_sports_remain(self):
        self.pair(sport="walking")
        self.assertEqual(build_plan(self.gpx).aliases, {})
        self.assertEqual(len(self.sync()), 2)

    def test_missing_gps_requires_review(self):
        self.pair(gps=False)
        plan = build_plan(self.gpx)
        self.assertEqual(plan.aliases, {})
        self.assertEqual(
            plan.report["overlapping_pairs"][0]["reason"], "missing_or_insufficient_gps"
        )
        self.assertEqual(len(self.sync()), 2)

    def test_no_gps_similar_statistics_do_not_merge(self):
        write_gpx(self.gpx, "a.gpx", gps=False)
        write_gpx(self.gpx, "b.gpx", start=5, gps=False)
        self.assertEqual(build_plan(self.gpx).aliases, {})

    def test_partial_overlaps_and_short_embedded_sessions_remain(self):
        self.pair(start=200, duration=100, distance=300)
        self.assertEqual(build_plan(self.gpx).aliases, {})

    def test_sparse_route_and_large_gps_gaps_do_not_merge(self):
        for params in ({"step": 30}, {"gap": True}):
            with self.subTest(params=params):
                self.pair(**params)
                self.assertEqual(build_plan(self.gpx).aliases, {})

    def test_distance_disagreement_requires_review(self):
        self.pair(distance=2500)
        self.assertEqual(build_plan(self.gpx).aliases, {})

    def test_stationary_placeholder_gps_is_not_evidence(self):
        for path in self.pair():
            root = ET.fromstring(path.read_text())
            for point in root.findall(".//{*}trkpt"):
                point.set("lat", "30")
            ET.ElementTree(root).write(path, encoding="utf-8")
        self.assertEqual(build_plan(self.gpx).aliases, {})

    def test_conflicting_same_second_positions_require_review(self):
        _, path = self.pair()
        root = ET.fromstring(path.read_text())
        segment = root.find("{*}trk/{*}trkseg")
        duplicate = ET.fromstring(ET.tostring(segment[10]))
        duplicate.set("lon", "121")
        segment.insert(11, duplicate)
        ET.ElementTree(root).write(path, encoding="utf-8")
        self.assertEqual(build_plan(self.gpx).aliases, {})

    def test_gps_comparison_does_not_bridge_separate_segments(self):
        self.pair()
        for path in self.gpx.glob("*.gpx"):
            root = ET.fromstring(path.read_text())
            track = root.find("{*}trk")
            segment = track.find("{*}trkseg")
            track.remove(segment)
            for i in range(0, len(segment), 2):
                new_segment = ET.SubElement(track, segment.tag)
                new_segment.extend(list(segment)[i : i + 2])
            ET.ElementTree(root).write(path, encoding="utf-8")
        # Shift the second file by 1s so matching would require crossing segments.
        path = self.gpx / "b.gpx"
        root = ET.fromstring(path.read_text())
        for element in root.iter():
            if element.tag.split("}")[-1] in {"time", "start_time", "end_time"}:
                element.text = (
                    datetime.fromisoformat(element.text) + timedelta(seconds=1)
                ).isoformat()
        ET.ElementTree(root).write(path, encoding="utf-8")
        plan = build_plan(self.gpx)
        self.assertEqual(plan.aliases, {})
        self.assertEqual(
            plan.report["overlapping_pairs"][0]["reason"],
            "insufficient_time_aligned_samples",
        )

    def test_timezone_offsets_are_normalized(self):
        self.pair(timezone_offset=8)
        self.assertEqual(build_plan(self.gpx).aliases, {"b.gpx": "a.gpx"})

    def test_naive_timestamps_fail_before_import(self):
        a, _ = self.pair()
        a.write_text(a.read_text().replace("+00:00", ""))
        with self.assertRaisesRegex(ValueError, "timezone"):
            self.sync()
        self.assertFalse(self.imported.exists())
        self.assertEqual(self.rows(), [])

    def test_nontransitive_matches_do_not_collapse_group(self):
        self.pair(longitude_offset=0.00012)
        write_gpx(self.gpx, "c.gpx", start=10, longitude_offset=0.00024)
        self.assertEqual(build_plan(self.gpx).aliases, {"b.gpx": "a.gpx"})

    def test_identical_content_with_different_filename_is_idempotent(self):
        a = write_gpx(self.gpx, "a.gpx")
        self.sync()
        shutil.copyfile(a, self.gpx / "renamed.gpx")
        self.assertEqual(len(self.sync()), 1)
        self.assertEqual(len(self.rows()), 1)
        self.assertCountEqual(
            json.loads(self.imported.read_text()), ["a.gpx", "renamed.gpx"]
        )

    def test_timestamp_id_collision_stops_without_overwriting(self):
        self.pair(start=0, longitude_offset=0.003)
        with self.assertRaisesRegex(ValueError, "collision"):
            self.sync()
        self.assertFalse(self.imported.exists())
        self.assertEqual(self.rows(), [])

    def test_new_duplicates_keep_both_db_rows_and_raw_files(self):
        files = self.pair()
        before = {p.name: p.read_bytes() for p in files}
        data = self.sync()
        rows = self.rows()
        self.assertEqual(len(data), 1)
        self.assertEqual(sum(a["distance"] for a in data), 1800)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][2], rows[0][0])
        self.assertEqual(before, {p.name: p.read_bytes() for p in files})
        self.assertCountEqual(json.loads(self.imported.read_text()), list(before))

    def test_rerun_is_idempotent_and_empty_db_rebuilds_despite_import_log(self):
        self.pair()
        expected = self.sync()
        report = (self.root / "gpx_dedup_report.json").read_bytes()
        imported = self.imported.read_bytes()
        self.assertEqual(self.sync(), expected)
        self.assertEqual((self.root / "gpx_dedup_report.json").read_bytes(), report)
        self.assertEqual(self.imported.read_bytes(), imported)
        with sqlite3.connect(self.db) as conn:
            conn.execute("DELETE FROM activities")  # isolated temporary test database
        self.assertEqual(self.sync(), expected)
        self.assertEqual(len(self.rows()), 2)

    def test_backfill_existing_rows_and_reversible_keep_separate(self):
        self.pair()
        self.override({"enabled": False})
        self.assertEqual(len(self.sync()), 2)
        self.override({"enabled": True})
        self.assertEqual(len(self.sync()), 1)
        self.override(
            {"decisions": [{"files": ["a.gpx", "b.gpx"], "action": "keep_separate"}]}
        )
        self.assertEqual(len(self.sync()), 2)
        self.assertTrue(all(row[2] is None for row in self.rows()))

    def test_late_arrival_with_earlier_start_chooses_same_canonical(self):
        write_gpx(self.gpx, "b.gpx", start=5)
        self.sync()
        write_gpx(self.gpx, "a.gpx")
        self.assertEqual(
            self.sync()[0]["run_id"], read_record(self.gpx / "a.gpx").run_id
        )
        self.assertEqual(self.rows()[1][2], self.rows()[0][0])

    def test_manual_merge_is_hash_bound_and_can_be_disabled(self):
        a, b = self.pair(gps=False)
        decision = {
            "files": ["a.gpx", "b.gpx"],
            "action": "merge",
            "canonical": "a.gpx",
            "sha256": [hashlib.sha256(p.read_bytes()).hexdigest() for p in (a, b)],
        }
        self.override({"decisions": [decision]})
        self.assertEqual(len(self.sync()), 1)
        self.override({"enabled": False, "decisions": [decision]})
        self.assertEqual(len(self.sync()), 2)
        b.write_text(b.read_text() + "\n")
        self.assertEqual(len(self.sync()), 2)
        self.override({"enabled": True, "decisions": [decision]})
        with self.assertRaisesRegex(ValueError, "hashes"):
            self.sync()
        self.assertTrue(all(row[2] is None for row in self.rows()))

    def test_failed_commit_never_advances_import_log(self):
        self.pair()
        app = generator.Generator(self.db)
        self.addCleanup(app.session.bind.dispose)
        self.addCleanup(app.session.close)
        with (
            patch.object(
                app.session, "commit", side_effect=RuntimeError("commit failed")
            ),
            self.assertRaisesRegex(RuntimeError, "commit failed"),
        ):
            app.sync_from_data_dir(self.gpx)
        self.assertFalse(self.imported.exists())
        self.assertEqual(self.rows(), [])

    def test_db_mismatch_leaves_prior_markers_unchanged(self):
        self.pair()
        self.sync()
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "UPDATE activities SET distance = 9999 WHERE duplicate_of IS NULL"
            )
        before = self.rows()
        with self.assertRaisesRegex(ValueError, "safely associate"):
            self.sync()
        self.assertEqual(self.rows(), before)

    def test_svg_loaders_and_json_use_the_same_canonical_rows(self):
        self.pair()
        data = self.sync()
        for is_grid in (False, True):
            tracks = TrackLoader().load_tracks_from_db(self.db, is_grid=is_grid)
            self.assertEqual([t.run_id for t in tracks], [a["run_id"] for a in data])
            self.assertEqual(sum(t.length for t in tracks), 1800)

    def test_schema_migration_preserves_all_old_columns(self):
        self.pair()
        self.sync()
        with sqlite3.connect(self.db) as conn:
            conn.execute("ALTER TABLE activities DROP COLUMN duplicate_of")
            before = conn.execute("SELECT * FROM activities ORDER BY run_id").fetchall()
        session = init_db(self.db)
        session.close()
        session.bind.dispose()
        with sqlite3.connect(self.db) as conn:
            after = conn.execute("SELECT * FROM activities ORDER BY run_id").fetchall()
        self.assertEqual(before, [row[:-1] for row in after])
        self.assertTrue(all(row[-1] is None for row in after))

    def test_nearby_subsecond_samples_rounded_to_same_second(self):
        for path in self.pair():
            root = ET.fromstring(path.read_text())
            segment = root.find("{*}trk/{*}trkseg")
            duplicate = ET.fromstring(ET.tostring(segment[10]))
            duplicate.set("lat", str(float(duplicate.attrib["lat"]) + 0.00001))
            segment.insert(11, duplicate)
            ET.ElementTree(root).write(path, encoding="utf-8")
        plan = build_plan(self.gpx)
        self.assertEqual(plan.aliases, {"b.gpx": "a.gpx"})
        data = self.sync()
        self.assertEqual(len(data), 1)
        self.assertEqual(len(self.rows()), 2)


if __name__ == "__main__":
    unittest.main()
