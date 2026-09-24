"""
Aggregate per-corridor sub-job outputs into a single coherent run.

For spatial-mode batches, each worker produces a regular ``main.py`` output
directory: ``simulation_info.json``, ``environment_traffic.json``,
``trip_data.json``, ``performance_metrics.json``, ``tripinfo.xml`` (optional)
and any ``knowledge_output/*`` files.

The aggregator merges them into a single output directory whose layout
matches what a non-distributed simulation would have produced. Specifically:

* ``environment_traffic.json``: union of edges (each edge is local to one
  corridor by construction, so no edge collisions are expected). For the
  rare overlap case (boundary edges), the per-slot lists are concatenated.
* ``trip_data.json``: list concatenation. Each trip is local to its corridor.
* ``simulation_info.json``: original config plus an ``aggregated`` block
  reporting which sub-jobs contributed, their durations and trip counts.
* ``tripinfo.xml``: concatenation as ``<tripinfos>`` root with all
  ``<tripinfo>`` children.
* ``aggregate_summary.json``: helper file with per-corridor stats.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional


# ==================== Helpers ====================

def _read_json(path: Path) -> Optional[dict]:
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as fp:
            return json.load(fp)
    except Exception as e:
        print(f"[aggregator] WARN: could not parse {path}: {e}")
        return None


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fp:
        json.dump(data, fp, indent=2)


# ==================== Mergers ====================

def merge_environment_traffic(parts: List[dict]) -> dict:
    merged: Dict[str, Dict[str, list]] = {}
    for part in parts:
        if not part:
            continue
        for edge_id, slots in part.items():
            tgt = merged.setdefault(edge_id, {})
            for slot_key, observations in slots.items():
                tgt.setdefault(slot_key, []).extend(observations)
    return merged


def merge_trip_data(parts: List[list]) -> list:
    out: List[dict] = []
    for part in parts:
        if isinstance(part, list):
            out.extend(part)
    return out


def merge_performance_metrics(parts: List[dict]) -> dict:
    """Naive merge: keep the longest entry per key (workers run in parallel,
    so wall-time is the max, not the sum)."""
    merged: Dict[str, dict] = {}
    for part in parts:
        if not isinstance(part, dict):
            continue
        for key, value in part.items():
            if key not in merged:
                merged[key] = value
            else:
                if isinstance(value, dict) and isinstance(merged[key], dict):
                    cur = merged[key]
                    for sub_k, sub_v in value.items():
                        if isinstance(sub_v, (int, float)) and isinstance(
                            cur.get(sub_k), (int, float)
                        ):
                            cur[sub_k] = max(cur[sub_k], sub_v)
                        else:
                            cur[sub_k] = sub_v
    return merged


def merge_tripinfo_xml(parts: List[Path], out_path: Path) -> int:
    """Concatenate <tripinfo> elements from multiple tripinfo.xml files."""
    root_out = ET.Element("tripinfos")
    total = 0
    for p in parts:
        if not p.is_file():
            continue
        try:
            tree = ET.parse(p)
            for ti in tree.getroot().findall("tripinfo"):
                root_out.append(ti)
                total += 1
        except ET.ParseError as e:
            print(f"[aggregator] WARN: bad tripinfo {p}: {e}")
    if total > 0:
        ET.ElementTree(root_out).write(out_path, encoding="utf-8", xml_declaration=True)
    return total


# ==================== Top-level entry ====================

def aggregate(
    sub_dirs: List[Path],
    output_dir: Path,
    parent_sim_id: Optional[str] = None,
    extra_info: Optional[dict] = None,
) -> Path:
    """Merge all ``sub_dirs`` into ``output_dir`` and return the latter."""

    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"[aggregator] Aggregating {len(sub_dirs)} sub-runs -> {output_dir}")

    sim_infos: List[dict] = []
    env_parts: List[dict] = []
    trip_parts: List[list] = []
    perf_parts: List[dict] = []
    tripinfo_paths: List[Path] = []

    sub_summaries: List[dict] = []
    longest_runtime = 0.0
    total_trip_count = 0

    for sub in sub_dirs:
        sub = Path(sub)
        if not sub.is_dir():
            print(f"[aggregator] WARN: missing sub dir {sub}")
            continue

        info = _read_json(sub / "simulation_info.json") or {}
        env = _read_json(sub / "environment_traffic.json") or {}
        trips = _read_json(sub / "trip_data.json") or []
        perf = _read_json(sub / "performance_metrics.json") or {}
        ti = sub / "tripinfo.xml"

        sim_infos.append(info)
        env_parts.append(env)
        trip_parts.append(trips)
        perf_parts.append(perf)
        if ti.is_file():
            tripinfo_paths.append(ti)

        runtime = float(info.get("aggregated_runtime_sec", 0.0)) or 0.0
        if runtime > longest_runtime:
            longest_runtime = runtime
        n_trips = len(trips) if isinstance(trips, list) else 0
        total_trip_count += n_trips

        sub_summaries.append(
            {
                "sub_dir": str(sub),
                "corridor": info.get("spatial_corridor"),
                "trip_count": n_trips,
                "edges_covered": len(env),
                "runtime_sec": runtime,
                "sync_stats": info.get("sync_stats"),
            }
        )

    # ---- write merged outputs ----
    merged_env = merge_environment_traffic(env_parts)
    merged_trips = merge_trip_data(trip_parts)
    merged_perf = merge_performance_metrics(perf_parts)

    _write_json(output_dir / "environment_traffic.json", merged_env)
    _write_json(output_dir / "trip_data.json", merged_trips)
    _write_json(output_dir / "performance_metrics.json", merged_perf)

    # tripinfo.xml
    n_ti = merge_tripinfo_xml(tripinfo_paths, output_dir / "tripinfo.xml")

    # simulation_info: take the first (configurations are identical except
    # for the corridor) and tack on the aggregation summary.
    base_info = dict(sim_infos[0]) if sim_infos else {}
    base_info.pop("spatial_corridor", None)
    base_info["aggregated"] = {
        "parent_sim_id": parent_sim_id,
        "sub_jobs": sub_summaries,
        "wallclock_sec": longest_runtime,
        "merged_at": datetime.now().isoformat(timespec="seconds"),
        "total_trip_count": total_trip_count,
        "tripinfo_records": n_ti,
        **(extra_info or {}),
    }
    _write_json(output_dir / "simulation_info.json", base_info)

    # Roll up sync stats across sub-jobs (handoffs, barrier, transport).
    sync_roll = {
        "transport": None,
        "handoffs_out": 0,
        "handoffs_in": 0,
        "barriers_max": 0,
        "barrier_timeouts": 0,
        "barrier_total_sec_max": 0.0,
        "handoff_failures": 0,
        "self_disabled": False,
    }
    any_sync = False
    for sj in sub_summaries:
        ss = sj.get("sync_stats") or {}
        if not ss:
            continue
        any_sync = True
        sync_roll["transport"] = sync_roll["transport"] or ss.get("transport")
        sync_roll["handoffs_out"] += int(ss.get("handoffs_out", 0) or 0)
        sync_roll["handoffs_in"] += int(ss.get("handoffs_in", 0) or 0)
        sync_roll["barriers_max"] = max(sync_roll["barriers_max"],
                                        int(ss.get("barriers", 0) or 0))
        sync_roll["barrier_timeouts"] += int(ss.get("barrier_timeouts", 0) or 0)
        sync_roll["barrier_total_sec_max"] = max(
            sync_roll["barrier_total_sec_max"],
            float(ss.get("barrier_total_sec", 0.0) or 0.0))
        sync_roll["handoff_failures"] += int(ss.get("handoff_failures", 0) or 0)
        if int(ss.get("self_disabled_at_step", -1) or -1) >= 0:
            sync_roll["self_disabled"] = True

    # Summary file (more concise than simulation_info)
    _write_json(
        output_dir / "aggregate_summary.json",
        {
            "parent_sim_id": parent_sim_id,
            "merged_at": datetime.now().isoformat(timespec="seconds"),
            "sub_jobs": sub_summaries,
            "merged_edges": len(merged_env),
            "merged_trips": len(merged_trips),
            "wallclock_sec": longest_runtime,
            "num_groups": len(sub_summaries),
            "sync": sync_roll if any_sync else None,
        },
    )

    print(
        f"[aggregator] Done: {len(merged_trips)} trips, "
        f"{len(merged_env)} edges, tripinfo={n_ti}, wall={longest_runtime:.0f}s"
    )
    return output_dir


# ==================== CLI ====================

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Aggregate distributed spatial-mode sub-runs into one folder"
    )
    parser.add_argument("--sub-dir", action="append", required=True, dest="sub_dirs",
                        help="Path to a sub-run output dir (repeat for each sub-job)")
    parser.add_argument("--out", required=True, help="Aggregated output dir")
    parser.add_argument("--sim-id", default=None,
                        help="Parent sim ID to record in simulation_info.json")
    args = parser.parse_args(argv)

    aggregate(
        [Path(p) for p in args.sub_dirs],
        Path(args.out),
        parent_sim_id=args.sim_id,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
