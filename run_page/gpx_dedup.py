"""Conservative, reversible deduplication for one athlete's GPX archive.

The CLI only audits. Sync stores a duplicate_of marker; no source file or
activity row is deleted. Missing GPS is never enough for automatic merging.
"""

import argparse
import hashlib
import json
import math
import os
import statistics
import tempfile
import xml.etree.ElementTree as ET
from bisect import bisect_left
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

POLICY = {
    "max_start_gap_seconds": 60,
    "max_end_gap_seconds": 60,
    "min_overlap_ratio": 0.95,
    "max_distance_difference_ratio": 0.03,
    "max_moving_time_difference_ratio": 0.05,
    "min_gps_points": 30,
    "sample_interval_seconds": 5,
    "max_interpolation_gap_seconds": 10,
    "min_sample_coverage": 0.90,
    "min_route_duration_ratio": 0.80,
    "max_median_separation_meters": 15,
    "max_p95_separation_meters": 30,
}


def atomic_json(path, value):
    path = Path(path)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as stream:
        temporary = stream.name
        try:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        except BaseException:
            os.unlink(temporary)
            raise
    try:
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def timestamp(value):
    if not value:
        return None
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError(
            "GPX timestamps must include a timezone for safe deduplication"
        )
    return result.timestamp()


def separation(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    square = math.sin((lat2 - lat1) / 2) ** 2 + (
        math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 12_742_000 * math.asin(min(1, math.sqrt(square)))


def sport(value):
    value = (value or "").strip().lower()
    return {"running": "run", "walking": "walk", "cycling": "ride"}.get(value, value)


@dataclass
class Record:
    name: str
    sha256: str
    run_id: int
    start: float
    end: float
    distance: float
    moving: float
    sport: str
    route_distance: float
    points: list
    times: list
    gps_valid: bool

    def position(self, when):
        i = bisect_left(self.times, when)
        if i < len(self.times) and self.times[i] == when:
            return self.points[i][1:3]
        if i == 0 or i == len(self.times):
            return None
        a, b = self.points[i - 1], self.points[i]
        if a[3] != b[3] or b[0] - a[0] > POLICY["max_interpolation_gap_seconds"]:
            return None
        fraction = (when - a[0]) / (b[0] - a[0])
        # Do not interpolate across the antimeridian.
        if abs(a[2] - b[2]) > 180:
            return None
        return tuple(a[k] + fraction * (b[k] - a[k]) for k in (1, 2))


def read_record(path):
    raw = path.read_bytes()
    root = ET.fromstring(raw)
    extensions = {
        element.tag.split("}")[-1]: element.text
        for element in root.findall("{*}extensions/*")
    }
    points = []
    gps_valid = True
    distance = 0.0
    for segment_id, segment in enumerate(root.findall("{*}trk/{*}trkseg")):
        previous = None
        for point in segment.findall("{*}trkpt"):
            lat, lon = float(point.attrib["lat"]), float(point.attrib["lon"])
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError(f"Invalid coordinates in {path.name}")
            if previous:
                distance += separation(previous, (lat, lon))
            previous = (lat, lon)
            when = timestamp(point.findtext("{*}time"))
            if when is None:
                gps_valid = False
                continue
            if points and when == points[-1][0]:
                # Some exporters round subsecond samples to the same second.
                # Collapse only nearby points in the same segment; conflicting
                # positions cannot supply automatic-merge evidence.
                if (
                    points[-1][3] == segment_id
                    and separation(points[-1][1:3], (lat, lon)) <= 5
                ):
                    points[-1] = (when, lat, lon, segment_id)
                    continue
                gps_valid = False
            elif points and when < points[-1][0]:
                gps_valid = False
            points.append((when, lat, lon, segment_id))
    points.sort(key=lambda point: point[0])
    types = {sport(t.findtext("{*}type")) for t in root.findall("{*}trk")}
    activity_sport = next(iter(types)) if len(types) == 1 else ""
    start = timestamp(
        extensions.get("start_time") or root.findtext("{*}metadata/{*}time")
    )
    end = timestamp(extensions.get("end_time"))
    start = start if start is not None else (points[0][0] if points else None)
    end = end if end is not None else (points[-1][0] if points else None)
    if start is None or end is None or end <= start:
        raise ValueError(f"Missing or invalid activity time bounds in {path.name}")
    route_distance = distance
    distance = float(extensions.get("distance", distance))
    moving = float(extensions.get("moving_time", end - start))
    if not all(math.isfinite(x) and x >= 0 for x in (distance, moving)):
        raise ValueError(f"Invalid distance/duration in {path.name}")
    if points and (points[0][0] < start or points[-1][0] > end):
        gps_valid = False
    # Match Track._load_gpx_data: first timed GPS point, extensions for no-GPS.
    run_start = points[0][0] if points else start
    return Record(
        path.name,
        hashlib.sha256(raw).hexdigest(),
        int(run_start * 1000),
        start,
        end,
        distance,
        moving,
        activity_sport,
        route_distance,
        points,
        [point[0] for point in points],
        gps_valid,
    )


def compare(a, b):
    overlap = max(0, min(a.end, b.end) - max(a.start, b.start))
    if not overlap:
        return None
    metrics = {
        "start_gap_seconds": abs(a.start - b.start),
        "end_gap_seconds": abs(a.end - b.end),
        "overlap_ratio": overlap / max(a.end - a.start, b.end - b.start),
        "distance_difference_ratio": abs(a.distance - b.distance)
        / max(a.distance, b.distance, 1),
        "moving_time_difference_ratio": abs(a.moving - b.moving)
        / max(a.moving, b.moving, 1),
    }
    result = {
        "files": [a.name, b.name],
        "run_ids": [a.run_id, b.run_id],
        "sha256": [a.sha256, b.sha256],
        "distances_meters": [a.distance, b.distance],
        "moving_seconds": [a.moving, b.moving],
        "gps_points": [len(a.points), len(b.points)],
        "metrics": metrics,
        "classification": "review",
        "reason": "time_or_distance_not_close_enough",
    }
    if a.sha256 == b.sha256:
        result.update(classification="duplicate", reason="identical_file_content")
        return result
    if not a.sport or a.sport != b.sport:
        result["reason"] = "different_or_unknown_sport"
        return result
    if (
        metrics["start_gap_seconds"] > POLICY["max_start_gap_seconds"]
        or metrics["end_gap_seconds"] > POLICY["max_end_gap_seconds"]
        or metrics["overlap_ratio"] < POLICY["min_overlap_ratio"]
        or metrics["distance_difference_ratio"]
        > POLICY["max_distance_difference_ratio"]
        or metrics["moving_time_difference_ratio"]
        > POLICY["max_moving_time_difference_ratio"]
        or min(a.distance, b.distance) < 100
    ):
        return result
    if (
        not a.gps_valid
        or not b.gps_valid
        or min(len(a.points), len(b.points)) < POLICY["min_gps_points"]
        or min(a.route_distance, b.route_distance) < 100
    ):
        result["reason"] = "missing_or_insufficient_gps"
        return result
    lo, hi = max(a.times[0], b.times[0]), min(a.times[-1], b.times[-1])
    metrics["route_duration_ratio"] = max(0, hi - lo) / overlap
    if metrics["route_duration_ratio"] < POLICY["min_route_duration_ratio"]:
        result["reason"] = "insufficient_route_duration"
        return result
    samples = int((hi - lo) // POLICY["sample_interval_seconds"]) + 1
    distances = []
    for i in range(samples):
        when = lo + i * POLICY["sample_interval_seconds"]
        left, right = a.position(when), b.position(when)
        if left is not None and right is not None:
            distances.append(separation(left, right))
    metrics["matched_samples"] = len(distances)
    metrics["sample_coverage"] = len(distances) / samples
    if (
        len(distances) < POLICY["min_gps_points"]
        or metrics["sample_coverage"] < POLICY["min_sample_coverage"]
    ):
        result["reason"] = "insufficient_time_aligned_samples"
        return result
    metrics["median_separation_meters"] = statistics.median(distances)
    metrics["p95_separation_meters"] = sorted(distances)[
        math.ceil(0.95 * len(distances)) - 1
    ]
    if (
        metrics["median_separation_meters"] <= POLICY["max_median_separation_meters"]
        and metrics["p95_separation_meters"] <= POLICY["max_p95_separation_meters"]
    ):
        result.update(classification="duplicate", reason="time_and_route_match")
    else:
        result["reason"] = "routes_differ"
    return result


@dataclass
class Plan:
    records: dict
    aliases: dict
    report: dict


def build_plan(data_dir, policy_path=None):
    data_dir = Path(data_dir)
    if not data_dir.is_dir():
        raise ValueError(f"Not a GPX directory: {data_dir}")
    policy_path = Path(policy_path or data_dir.parent / "gpx_dedup_overrides.json")
    overrides = json.loads(policy_path.read_text()) if policy_path.exists() else {}
    if set(overrides) - {"enabled", "decisions"}:
        raise ValueError("Unknown GPX deduplication policy key")
    enabled = overrides.get("enabled", True)
    if not isinstance(enabled, bool):
        raise TypeError("enabled must be a JSON boolean")
    records = {}
    for path in sorted(data_dir.glob("*.gpx")):
        if not path.name.startswith("."):
            records[path.name] = read_record(path)
    decisions = {}
    for decision in overrides.get("decisions", []):
        names = decision["files"]
        key = frozenset(names)
        if len(names) != 2 or len(key) != 2 or key in decisions:
            raise ValueError("Each decision must name a unique pair of GPX files")
        if not key <= records.keys():
            raise ValueError(f"Decision references missing GPX files: {names}")
        action = decision["action"]
        if action not in {"keep_separate", "merge"}:
            raise ValueError(f"Unknown deduplication decision: {action}")
        if enabled and action == "merge":
            if decision.get("sha256") != [records[n].sha256 for n in names]:
                raise ValueError(
                    "Manual merge hashes do not match the current GPX files"
                )
            if decision.get("canonical") not in key:
                raise ValueError("Manual merge must choose one file as canonical")
        decisions[key] = decision
    pairs = {}
    ordered = sorted(records.values(), key=lambda r: (r.start, r.name))
    for i, a in enumerate(ordered):
        for b in ordered[i + 1 :]:
            if b.start >= a.end:
                break
            pair = compare(a, b)
            if pair is not None:
                key = frozenset((a.name, b.name))
                decision = decisions.get(key)
                if not enabled:
                    pair.update(classification="kept", reason="deduplication_disabled")
                elif decision:
                    pair.update(
                        classification=(
                            "duplicate" if decision["action"] == "merge" else "kept"
                        ),
                        reason="manual_" + decision["action"],
                    )
                pairs[key] = pair
    for key, decision in decisions.items():
        if enabled and decision["action"] == "merge" and key not in pairs:
            raise ValueError("Refusing a manual merge of non-overlapping activities")
    # All members must match each other: never merge A--B--C by transitivity.
    groups = []
    for record in ordered:
        for group in groups:
            if all(
                pairs.get(frozenset((record.name, other.name)), {}).get(
                    "classification"
                )
                == "duplicate"
                for other in group
            ):
                group.append(record)
                break
        else:
            groups.append([record])
    aliases = {}
    for group in groups:
        group_names = {r.name for r in group}
        preferences = {
            d["canonical"]
            for key, d in decisions.items()
            if enabled and d["action"] == "merge" and key <= group_names
        }
        if len(preferences) > 1:
            raise ValueError(
                "Conflicting manual canonical choices in a duplicate group"
            )
        canonical = next(iter(preferences)) if preferences else group[0].name
        aliases.update({r.name: canonical for r in group if r.name != canonical})
    for key, decision in decisions.items():
        if (
            enabled
            and decision["action"] == "merge"
            and len({aliases.get(name, name) for name in key}) != 1
        ):
            raise ValueError("Manual merge conflicts with another pair's evidence")
    # Timestamp IDs are legacy IDs. Stop rather than overwrite distinct sessions.
    by_id = {}
    for record in ordered:
        canonical = aliases.get(record.name, record.name)
        if record.run_id in by_id and by_id[record.run_id] != canonical:
            raise ValueError(
                f"GPX run_id collision for {record.name}; distinct activities must not "
                "overwrite each other. Resolve their source timestamps before syncing."
            )
        by_id[record.run_id] = canonical
    digest = hashlib.sha256()
    for name, record in sorted(records.items()):
        digest.update(f"{name}\0{record.sha256}\n".encode())
    report = {
        "version": 1,
        "enabled": enabled,
        "policy": POLICY,
        "files_scanned": len(records),
        "files_without_gps": sum(not r.points for r in ordered),
        "inventory_sha256": digest.hexdigest(),
        "overlapping_pairs": list(pairs.values()),
        "aliases": aliases,
    }
    return Plan(records, aliases, report)


def apply_plan(session, plan):
    """Validate every target first; caller commits the reversible markers."""
    from generator.db import Activity

    markers = {}
    for duplicate, canonical in plan.aliases.items():
        a, b = plan.records[duplicate], plan.records[canonical]
        if a.run_id == b.run_id:
            continue  # identical timestamp IDs already share a single database row
        for record in (a, b):
            row = session.get(Activity, record.run_id)
            expected_start = datetime.fromtimestamp(record.run_id / 1000, UTC).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            if (
                row is None
                or row.start_date != expected_start
                or abs(row.distance - record.distance) > 0.1
                or sport(row.type) != record.sport
            ):
                raise ValueError(
                    f"Cannot safely associate {record.name} with its database row; "
                    "no deduplication markers have been changed"
                )
        markers[a.run_id] = b.run_id
    for row in session.query(Activity).filter(Activity.duplicate_of.isnot(None)):
        row.duplicate_of = None
    for run_id, canonical_id in markers.items():
        session.get(Activity, run_id).duplicate_of = canonical_id
    plan.report["excluded_run_ids"] = {str(k): v for k, v in sorted(markers.items())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gpx-dir", type=Path, default=Path("GPX_OUT"))
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    plan = build_plan(args.gpx_dir, args.policy)
    if args.report:
        atomic_json(args.report, plan.report)
    else:
        print(json.dumps(plan.report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
