"""
Pre-process trips XML files into per-corridor sub-files for spatial mode.

The splitter runs OUTSIDE of any TraCI / running SUMO instance: it uses
``sumolib`` to load the network, computes a free-flow shortest route for
every trip, classifies the route into one of the basic corridors
(``A7`` / ``N340`` / ``CV`` / ``N225``) and writes one trips XML per
group of corridors (group definitions live in :mod:`corridors`).

Usage (CLI):
    python -m SIMULATION.distributed.trip_splitter \\
        --road modified_4ways_all \\
        --trips SIMULATION/trips/fullnet_trips_intelligent_99.xml \\
        --workers 2 \\
        --out SIMULATION/distributed/trips_cache/

The cache is keyed on (trips file mtime, workers, road) so re-running the
splitter for the same (road, trips, workers) tuple is a no-op when the
inputs haven't changed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from SIMULATION.CONSTANTS import get_network_file, get_trips_file  # noqa: E402
from SIMULATION.distributed.corridors import (  # noqa: E402
    CORRIDOR_KEY_EDGES,
    SPLITS,
    assign_to_group,
    classify_route,
    get_default_split,
)


CACHE_DIR_DEFAULT = PROJECT_ROOT / "SIMULATION" / "distributed" / "trips_cache"


# ==================== sumolib helpers ====================

def _import_sumolib():
    """Import sumolib lazily so the rest of the package works without SUMO."""
    try:
        import sumolib  # type: ignore
        return sumolib
    except ImportError as e:
        raise RuntimeError(
            "sumolib is required for trip splitting. Install SUMO and ensure "
            "the SUMO_HOME/tools directory is on PYTHONPATH, e.g. on Windows:\n"
            "  set SUMO_HOME=C:\\Program Files (x86)\\Eclipse\\Sumo\n"
            "  set PYTHONPATH=%PYTHONPATH%;%SUMO_HOME%\\tools"
        ) from e


def _load_network(net_file: str):
    sumolib = _import_sumolib()
    print(f"[splitter] Loading network: {os.path.basename(net_file)}")
    t0 = time.perf_counter()
    net = sumolib.net.readNet(net_file, withInternal=False)
    print(f"[splitter] Network loaded in {time.perf_counter() - t0:.1f}s")
    return net


def _shortest_route_edges(net, from_edge_id: str, to_edge_id: str) -> List[str]:
    """Return list of edge IDs of the shortest free-flow route, or [] if none."""
    try:
        from_edge = net.getEdge(from_edge_id)
        to_edge = net.getEdge(to_edge_id)
    except Exception:
        return []
    try:
        edges, _cost = net.getShortestPath(from_edge, to_edge)
        if edges is None:
            return []
        return [e.getID() for e in edges]
    except Exception:
        return []


# ==================== Trip splitting ====================

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
    return h.hexdigest()[:12]


def split_trips(
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
) -> Dict[str, str]:
    """Split ``trips_file`` into per-group XMLs.

    Returns a mapping ``{group_label: path_to_split_trips_xml}``.

    Geometric strategies (``balanced=True`` or ``highway=True``) insert each
    trip on the worker that owns its origin edge:

    * ``balanced`` -> ``G0``..``G{num_groups-1}`` strips along ``axis``.
    * ``highway``  -> ``A7`` / ``N340`` nearest-highway split.

    Otherwise trips are classified by corridor (route overlap with sentinels).
    """
    if balanced or highway:
        return _split_trips_geometric(
            net_file, trips_file,
            cache_dir=cache_dir, force=force,
            accident=accident, accident_edges=accident_edges,
            axis=axis, num_groups=num_groups, highway=highway,
        )

    cache_dir = Path(cache_dir or CACHE_DIR_DEFAULT)
    cache_dir.mkdir(parents=True, exist_ok=True)

    if workers not in SPLITS:
        raise ValueError(
            f"Unsupported worker count {workers}. Supported: {sorted(SPLITS)}"
        )

    cache_key = _hash_inputs(net_file, trips_file, workers)
    manifest_path = cache_dir / f"manifest_{cache_key}.json"

    if manifest_path.exists() and not force:
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            paths = data.get("paths", {})
            if all(os.path.isfile(p) for p in paths.values()):
                print(f"[splitter] Cache hit ({cache_key}); skipping recompute")
                return paths
        except Exception:
            pass

    split = get_default_split(workers)
    print(f"[splitter] Splitting trips for {workers} worker(s): {split}")

    net = _load_network(net_file)

    tree = ET.parse(trips_file)
    root = tree.getroot()
    trips = list(root.findall("trip"))
    print(f"[splitter] {len(trips)} trips to classify")

    # Pre-create one root per group (carrying the same vTypes)
    group_roots: Dict[str, ET.Element] = {}
    group_trip_counts: Dict[str, int] = {}
    for grp in split:
        grp_root = ET.Element(
            "routes",
            attrib={
                "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
                "xsi:noNamespaceSchemaLocation": "http://sumo.dlr.de/xsd/routes_file.xsd",
            },
        )
        for vtype in root.findall("vType"):
            grp_root.append(_clone_element(vtype))
        group_roots[grp] = grp_root
        group_trip_counts[grp] = 0

    # Classification cache: same (from, to) -> same group
    od_cache: Dict[Tuple[str, str], str] = {}
    misses = 0
    t0 = time.perf_counter()

    for idx, trip in enumerate(trips):
        from_e = trip.get("from")
        to_e = trip.get("to")
        if not from_e or not to_e:
            continue

        key = (from_e, to_e)
        group_label = od_cache.get(key)
        if group_label is None:
            edges = _shortest_route_edges(net, from_e, to_e)
            corridor = classify_route(edges) if edges else None
            if edges and corridor is None:
                misses += 1
            group_label = assign_to_group(corridor, split)
            od_cache[key] = group_label

        group_roots[group_label].append(_clone_element(trip))
        group_trip_counts[group_label] += 1

        if (idx + 1) % 2000 == 0:
            print(
                f"[splitter] {idx + 1}/{len(trips)} trips classified "
                f"(cache size {len(od_cache)})"
            )

    print(
        f"[splitter] Classification done in {time.perf_counter() - t0:.1f}s "
        f"({misses} unclassified routes)"
    )

    # Write each group's trips file
    paths: Dict[str, str] = {}
    base = Path(trips_file).stem
    for grp, grp_root in group_roots.items():
        out_path = cache_dir / f"{base}__{cache_key}__{grp}.xml"
        ET.ElementTree(grp_root).write(
            out_path, encoding="utf-8", xml_declaration=True
        )
        paths[grp] = str(out_path)
        print(f"[splitter]   {grp}: {group_trip_counts[grp]} trips -> {out_path.name}")

    manifest = {
        "cache_key": cache_key,
        "net_file": net_file,
        "trips_file": trips_file,
        "workers": workers,
        "split": split,
        "paths": paths,
        "trip_counts": group_trip_counts,
        "created_at": time.time(),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"[splitter] Manifest written: {manifest_path.name}")
    return paths


def _split_trips_geometric(
    net_file: str,
    trips_file: str,
    cache_dir: Optional[Path] = None,
    force: bool = False,
    accident: bool = False,
    accident_edges: Optional[List[str]] = None,
    axis: str = "x",
    num_groups: int = 2,
    highway: bool = False,
) -> Dict[str, str]:
    """Split trips by the owner of their origin edge (balanced strips or the
    nearest-highway partition)."""
    from SIMULATION.distributed.balanced_partition import (
        assign_trip_group,
        compute_balanced_owner,
        compute_highway_owner,
        group_labels,
    )

    cache_dir = Path(cache_dir or CACHE_DIR_DEFAULT)
    cache_dir.mkdir(parents=True, exist_ok=True)

    if highway:
        owner = compute_highway_owner(net_file, force=force)
        groups = ["A7", "N340"]
        tag = "hwy"
    else:
        owner = compute_balanced_owner(
            net_file, trips_file,
            accident=accident, accident_edges=accident_edges, force=force,
            axis=axis, num_groups=num_groups,
        )
        groups = group_labels(num_groups)
        tag = f"bal_{axis}{num_groups}" + ("_acc" if accident else "_noacc")

    tree = ET.parse(trips_file)
    root = tree.getroot()
    trips = list(root.findall("trip"))

    group_roots: Dict[str, ET.Element] = {}
    group_counts: Dict[str, int] = {}
    for grp in groups:
        grp_root = ET.Element(
            "routes",
            attrib={
                "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
                "xsi:noNamespaceSchemaLocation": "http://sumo.dlr.de/xsd/routes_file.xsd",
            },
        )
        for vtype in root.findall("vType"):
            grp_root.append(_clone_element(vtype))
        group_roots[grp] = grp_root
        group_counts[grp] = 0

    default_grp = groups[0]
    for trip in trips:
        from_e = trip.get("from")
        if not from_e:
            continue
        grp = assign_trip_group(owner, from_e, default=default_grp)
        if grp not in group_roots:
            grp = default_grp
        group_roots[grp].append(_clone_element(trip))
        group_counts[grp] += 1

    cache_key = _hash_inputs(net_file, trips_file, num_groups) + "_" + tag
    base = Path(trips_file).stem
    paths: Dict[str, str] = {}
    for grp, grp_root in group_roots.items():
        out_path = cache_dir / f"{base}__{cache_key}__{grp}.xml"
        ET.ElementTree(grp_root).write(
            out_path, encoding="utf-8", xml_declaration=True
        )
        paths[grp] = str(out_path)
        print(f"[splitter][{tag}] {grp}: {group_counts[grp]} trips -> "
              f"{out_path.name}")

    manifest_path = cache_dir / f"manifest_{cache_key}.json"
    manifest_path.write_text(json.dumps({
        "cache_key": cache_key,
        "net_file": net_file,
        "trips_file": trips_file,
        "balanced": not highway,
        "highway": highway,
        "axis": axis,
        "num_groups": num_groups,
        "accident": accident,
        "split": groups,
        "paths": paths,
        "trip_counts": group_counts,
        "created_at": time.time(),
    }, indent=2), encoding="utf-8")
    return paths


def _clone_element(elem: ET.Element) -> ET.Element:
    """Deep-copy an ET element preserving attributes and children."""
    new = ET.Element(elem.tag, attrib=dict(elem.attrib))
    new.text = elem.text
    new.tail = elem.tail
    for child in list(elem):
        new.append(_clone_element(child))
    return new


# ==================== CLI ====================

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Split trips XML by corridor for spatial-mode distribution"
    )
    parser.add_argument("--road", default="modified_4ways_all",
                        help="Road network name (default: modified_4ways_all)")
    parser.add_argument("--trips", required=False,
                        help="Path to trips XML (default: resolved from --road and --ratio)")
    parser.add_argument("--ratio", default="99",
                        help="Intelligent ratio when --trips omitted (default: 99)")
    parser.add_argument("--vehicles", type=int, default=None,
                        help="Vehicle count when --trips omitted")
    parser.add_argument("--force-cv", action="store_true",
                        help="Use the *_cv variant when --trips omitted")
    parser.add_argument("--workers", type=int, default=2,
                        help=f"Number of workers (groups). Supported: {sorted(SPLITS)}")
    parser.add_argument("--out", default=str(CACHE_DIR_DEFAULT),
                        help=f"Cache directory (default: {CACHE_DIR_DEFAULT})")
    parser.add_argument("--force", action="store_true",
                        help="Ignore cache and recompute")
    args = parser.parse_args(argv)

    net_file = get_network_file(args.road)
    trips_file = args.trips or get_trips_file(
        args.road, args.ratio,
        force_cv=args.force_cv,
        vehicles=args.vehicles,
    )

    if not os.path.isfile(trips_file):
        print(f"[splitter] ERROR: trips file not found: {trips_file}")
        return 1

    paths = split_trips(net_file, trips_file, args.workers,
                        cache_dir=Path(args.out), force=args.force)

    print("\n[splitter] DONE. Per-group trip files:")
    for grp, p in paths.items():
        print(f"  {grp}: {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
