"""
Edge ownership classifier for synced-spatial mode.

Builds, for a given (network, trips, split) tuple, a mapping
``edge_id -> owner_group`` where ``owner_group`` is one of the spatial group
labels (``"AN"``, ``"CV"``, ``"A7"`` ...) or ``"SHARED"`` when the edge is
used heavily by trips of more than one group.

The classification is computed offline via free-flow shortest paths over the
network with ``sumolib``. For each unique (from_edge, to_edge) pair found in
the trips XML we:

    1. Compute the shortest free-flow path.
    2. Classify the trip into a corridor group (delegated to ``corridors``).
    3. Increment a per-edge counter for that group.

Once all trips are processed, every edge is assigned to the group with the
largest share, provided that share exceeds ``DOMINANCE_THRESHOLD`` (default
70%). Edges below the threshold are marked ``"SHARED"``: the synced-spatial
runtime treats vehicles standing on a SHARED edge as "still owned by their
current worker" and only triggers a handoff once they enter an edge clearly
belonging to a different group.

Results are cached to disk keyed on (net mtime, trips mtime, workers).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from SIMULATION.distributed.corridors import (  # noqa: E402
    SPLITS,
    assign_to_group,
    classify_route,
    get_default_split,
)


# ==================== Tunables ====================

# An edge is owned by a single group only if that group accounts for at
# least this fraction of all classified trip-routes traversing the edge.
# Lower values produce fewer SHARED edges (more aggressive ownership) but
# more spurious handoffs; higher values produce more SHARED edges (looser
# ownership, fewer handoffs) but the partition is less crisp.
DOMINANCE_THRESHOLD = 0.70

# Minimum number of routes that must traverse an edge before we trust the
# dominance ratio. Edges below this fall back to SHARED to avoid noise on
# low-traffic ramps.
MIN_TRIP_SUPPORT = 2

CACHE_DIR_DEFAULT = PROJECT_ROOT / "SIMULATION" / "distributed" / "edge_owner_cache"

SHARED_LABEL = "SHARED"


# ==================== sumolib helpers ====================

def _import_sumolib():
    try:
        import sumolib  # type: ignore
        return sumolib
    except ImportError as e:
        raise RuntimeError(
            "sumolib is required for edge classification. Install SUMO and "
            "ensure the SUMO_HOME/tools dir is on PYTHONPATH."
        ) from e


def _shortest_route_edges(net, from_id: str, to_id: str) -> List[str]:
    try:
        from_e = net.getEdge(from_id)
        to_e = net.getEdge(to_id)
    except Exception:
        return []
    try:
        edges, _cost = net.getShortestPath(from_e, to_e)
        if edges is None:
            return []
        return [e.getID() for e in edges]
    except Exception:
        return []


# ==================== Cache key ====================

def _hash_inputs(net_file: str, trips_file: str, workers: int) -> str:
    h = hashlib.md5()
    for path in (net_file, trips_file):
        try:
            stat = os.stat(path)
            h.update(path.encode("utf-8"))
            h.update(str(int(stat.st_mtime)).encode())
            h.update(str(stat.st_size).encode())
        except OSError:
            h.update(path.encode("utf-8"))
    h.update(str(workers).encode())
    h.update(str(DOMINANCE_THRESHOLD).encode())
    return h.hexdigest()[:12]


# ==================== Public API ====================

def build_edge_owner_map(
    net_file: str,
    trips_file: str,
    workers: int,
    cache_dir: Optional[Path] = None,
    force: bool = False,
    balanced: bool = False,
    accident: bool = False,
    accident_edges: Optional[List[str]] = None,
    axis: str = "x",
    num_groups: int = 2,
    highway: bool = False,
) -> Tuple[Dict[str, str], Dict[str, int]]:
    """Compute (or load from cache) the edge -> owner-group mapping.

    Returns a tuple ``(edge_owner_map, group_edge_counts)``:
        * ``edge_owner_map`` -- ``{edge_id: group_label_or_SHARED}``.
        * ``group_edge_counts`` -- ``{group_label: number_of_edges_owned}``
          (for diagnostics; SHARED is included).

    When ``balanced=True`` the corridor heuristic is bypassed and a
    load-balanced two-way edge partition (``G0`` / ``G1``) is computed
    instead (see :mod:`balanced_partition`). ``accident`` makes the routing
    used for balancing avoid the accident stretch, so the partition reflects
    the post-reroute load distribution.
    """
    if highway:
        from SIMULATION.distributed.balanced_partition import (
            compute_highway_owner,
        )
        owner = compute_highway_owner(net_file, force=force)
        counts = {}
        for g in owner.values():
            counts[g] = counts.get(g, 0) + 1
        return owner, counts

    if balanced:
        from SIMULATION.distributed.balanced_partition import (
            compute_balanced_owner,
        )
        owner = compute_balanced_owner(
            net_file, trips_file,
            accident=accident, accident_edges=accident_edges, force=force,
            axis=axis, num_groups=num_groups,
        )
        counts: Dict[str, int] = {}
        for g in owner.values():
            counts[g] = counts.get(g, 0) + 1
        return owner, counts

    cache_dir = Path(cache_dir or CACHE_DIR_DEFAULT)
    cache_dir.mkdir(parents=True, exist_ok=True)

    if workers not in SPLITS:
        raise ValueError(
            f"Unsupported worker count {workers}. Supported: {sorted(SPLITS)}"
        )

    cache_key = _hash_inputs(net_file, trips_file, workers)
    cache_path = cache_dir / f"edge_owner_{cache_key}.json"

    if cache_path.exists() and not force:
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            owner_map = data["edge_owner"]
            counts = data.get("group_edge_counts", {})
            print(f"[edge-classifier] Cache hit ({cache_key}) -> "
                  f"{len(owner_map)} edges classified")
            return owner_map, counts
        except Exception:
            pass

    print(f"[edge-classifier] Building edge owner map for {workers} workers...")
    sumolib = _import_sumolib()
    t0 = time.perf_counter()
    net = sumolib.net.readNet(net_file, withInternal=False)
    print(f"[edge-classifier] Network loaded in {time.perf_counter() - t0:.1f}s")

    split = get_default_split(workers)

    # Per-edge: how many trip-routes of each group traverse it.
    edge_group_counts: Dict[str, Dict[str, int]] = defaultdict(
        lambda: defaultdict(int))

    tree = ET.parse(trips_file)
    root = tree.getroot()
    trips = list(root.findall("trip"))
    print(f"[edge-classifier] {len(trips)} trips to process")

    od_cache: Dict[Tuple[str, str], Tuple[str, List[str]]] = {}

    for idx, trip in enumerate(trips):
        from_e = trip.get("from")
        to_e = trip.get("to")
        if not from_e or not to_e:
            continue

        key = (from_e, to_e)
        cached = od_cache.get(key)
        if cached is None:
            edges = _shortest_route_edges(net, from_e, to_e)
            corridor = classify_route(edges) if edges else None
            group = assign_to_group(corridor, split)
            od_cache[key] = (group, edges)
        else:
            group, edges = cached

        for e in edges:
            edge_group_counts[e][group] += 1

        if (idx + 1) % 2000 == 0:
            print(f"[edge-classifier]   processed {idx + 1}/{len(trips)} "
                  f"trips (unique ODs={len(od_cache)})")

    # Decide ownership per edge.
    owner_map: Dict[str, str] = {}
    group_edge_counts: Dict[str, int] = defaultdict(int)
    for edge_id, group_counts in edge_group_counts.items():
        total = sum(group_counts.values())
        if total < MIN_TRIP_SUPPORT:
            owner_map[edge_id] = SHARED_LABEL
            group_edge_counts[SHARED_LABEL] += 1
            continue
        best_group = max(group_counts, key=group_counts.get)
        share = group_counts[best_group] / total
        if share >= DOMINANCE_THRESHOLD:
            owner_map[edge_id] = best_group
            group_edge_counts[best_group] += 1
        else:
            owner_map[edge_id] = SHARED_LABEL
            group_edge_counts[SHARED_LABEL] += 1

    print(f"[edge-classifier] Done in {time.perf_counter() - t0:.1f}s")
    print(f"[edge-classifier] Edge ownership distribution:")
    for grp, n in sorted(group_edge_counts.items()):
        print(f"[edge-classifier]   {grp}: {n} edges")

    cache_path.write_text(
        json.dumps({
            "edge_owner": owner_map,
            "group_edge_counts": dict(group_edge_counts),
            "split": split,
            "workers": workers,
            "net_file": net_file,
            "trips_file": trips_file,
            "dominance_threshold": DOMINANCE_THRESHOLD,
            "min_trip_support": MIN_TRIP_SUPPORT,
            "created_at": time.time(),
        }, indent=2),
        encoding="utf-8",
    )
    print(f"[edge-classifier] Cached -> {cache_path.name}")
    return owner_map, dict(group_edge_counts)


# ==================== CLI ====================

def main(argv: Optional[List[str]] = None) -> int:
    from SIMULATION.CONSTANTS import get_network_file, get_trips_file

    parser = argparse.ArgumentParser(
        description="Compute edge -> owner-group mapping for synced-spatial mode"
    )
    parser.add_argument("--road", default="modified_4ways_all")
    parser.add_argument("--trips", default=None,
                        help="Path to trips XML (default: resolved from --road/--ratio)")
    parser.add_argument("--ratio", default="99")
    parser.add_argument("--vehicles", type=int, default=None)
    parser.add_argument("--force-cv", action="store_true")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--out", default=str(CACHE_DIR_DEFAULT))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    net_file = get_network_file(args.road)
    trips_file = args.trips or get_trips_file(
        args.road, args.ratio,
        force_cv=args.force_cv,
        vehicles=args.vehicles,
    )

    if not os.path.isfile(trips_file):
        print(f"[edge-classifier] ERROR: trips file not found: {trips_file}")
        return 1

    owner_map, counts = build_edge_owner_map(
        net_file, trips_file, args.workers,
        cache_dir=Path(args.out),
        force=args.force,
    )
    print(f"\n[edge-classifier] Total classified: {len(owner_map)} edges")
    return 0


if __name__ == "__main__":
    sys.exit(main())
