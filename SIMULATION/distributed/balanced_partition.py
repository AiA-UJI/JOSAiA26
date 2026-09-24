"""
Load-balanced spatial partition for synced-spatial distribution.

Unlike the corridor classifier (which groups whole corridors A7/N340 vs
CV/N225 and therefore puts ~92 %% of the Almenara traffic on a single
worker), this module builds a **two-way edge partition balanced by traffic
load** so that each SUMO instance carries roughly half of the per-step
vehicle work. That is the only way the step-by-step barrier of
``synced-spatial`` mode can actually accelerate the simulation.

How it works
------------
1. Load the network with ``sumolib`` and build an edge adjacency graph.
2. For every unique (from, to) origin-destination pair found in the trips
   file, compute a free-flow shortest route over the edge graph (own small
   Dijkstra so we can *block* edges).
3. **Accident awareness**: when ``accident=True`` the edges of the accident
   stretch are *blocked* during routing, so the computed routes are the ones
   vehicles will actually take once they reroute around the incident. The
   resulting per-edge load therefore reflects the post-reroute distribution
   (more traffic on the alternative corridors), which is what we want to
   balance on.
4. Accumulate a per-edge load = number of trips whose (rerouted) route uses
   the edge.
5. Sort every edge by the x-coordinate of its centroid and pick the cut
   ``x*`` where the cumulative load reaches 50 %%. Edges west of the cut go
   to group ``G0``; edges east of it go to ``G1``. Because trips traverse the
   map roughly west<->east, each instance ends up simulating ~half of every
   trip's lifetime, i.e. ~half of the per-step vehicle load.

The result is an ``edge_owner`` mapping ``{edge_id: "G0"|"G1"}`` covering
*every* edge of the network (so a vehicle can never stand on an unowned
edge), cached to disk keyed on (net, trips, accident).
"""

from __future__ import annotations

import hashlib
import heapq
import json
import os
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CACHE_DIR_DEFAULT = PROJECT_ROOT / "SIMULATION" / "distributed" / "balanced_cache"

# Group labels for the balanced two-way split.
GROUPS: List[str] = ["G0", "G1"]


def _import_sumolib():
    try:
        import sumolib  # type: ignore
        return sumolib
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "sumolib is required for balanced partitioning. Install SUMO and "
            "put SUMO_HOME/tools on PYTHONPATH."
        ) from e


def _edge_cost(e) -> float:
    try:
        spd = e.getSpeed() or 1.0
        return e.getLength() / max(spd, 0.1)
    except Exception:
        return 1.0


def _edge_centroid_x(e) -> float:
    try:
        shape = e.getShape()
        if not shape:
            return 0.0
        return sum(p[0] for p in shape) / len(shape)
    except Exception:
        return 0.0


def _edge_centroid_y(e) -> float:
    try:
        shape = e.getShape()
        if not shape:
            return 0.0
        return sum(p[1] for p in shape) / len(shape)
    except Exception:
        return 0.0


def _edge_centroid_xy(e):
    try:
        shape = e.getShape()
        if not shape:
            return (0.0, 0.0)
        n = len(shape)
        return (sum(p[0] for p in shape) / n, sum(p[1] for p in shape) / n)
    except Exception:
        return (0.0, 0.0)


def group_labels(num_groups: int) -> List[str]:
    """Canonical balanced group labels ``G0 .. G{num_groups-1}``."""
    return [f"G{i}" for i in range(max(1, num_groups))]


def _build_adjacency(net) -> Dict[str, Tuple[Tuple[str, float], ...]]:
    """edge_id -> tuple of (neighbour_edge_id, cost_to_enter_neighbour)."""
    adj: Dict[str, Tuple[Tuple[str, float], ...]] = {}
    for e in net.getEdges():
        outs: List[Tuple[str, float]] = []
        for oe in e.getOutgoing():
            outs.append((oe.getID(), _edge_cost(oe)))
        adj[e.getID()] = tuple(outs)
    return adj


def _dijkstra(adj, start_cost_of, from_id: str, to_id: str,
              blocked: Set[str]) -> List[str]:
    """Shortest path (list of edge ids, inclusive) over the edge graph,
    avoiding ``blocked`` edges (endpoints are always allowed)."""
    if from_id == to_id:
        return [from_id]
    start_cost = start_cost_of(from_id)
    dist: Dict[str, float] = {from_id: start_cost}
    prev: Dict[str, str] = {}
    pq: List[Tuple[float, str]] = [(start_cost, from_id)]
    while pq:
        d, e = heapq.heappop(pq)
        if e == to_id:
            break
        if d > dist.get(e, float("inf")):
            continue
        for nid, c in adj.get(e, ()):  # type: ignore[union-attr]
            if nid in blocked and nid != to_id:
                continue
            nd = d + c
            if nd < dist.get(nid, float("inf")):
                dist[nid] = nd
                prev[nid] = e
                heapq.heappush(pq, (nd, nid))
    if to_id not in dist:
        return []
    path = [to_id]
    while path[-1] != from_id:
        p = prev.get(path[-1])
        if p is None:
            return []
        path.append(p)
    path.reverse()
    return path


def _hash_inputs(net_file: str, trips_file: str, accident: bool,
                 accident_edges: Optional[Sequence[str]],
                 axis: str = "x", num_groups: int = 2) -> str:
    h = hashlib.md5()
    for path in (net_file, trips_file):
        try:
            st = os.stat(path)
            h.update(path.encode("utf-8"))
            h.update(str(int(st.st_mtime)).encode())
            h.update(str(st.st_size).encode())
        except OSError:
            h.update(path.encode("utf-8"))
    h.update(b"acc" if accident else b"noacc")
    if accident and accident_edges:
        h.update("|".join(accident_edges).encode("utf-8"))
    h.update(f"axis={axis}".encode())
    h.update(f"n={num_groups}".encode())
    return h.hexdigest()[:12]


def compute_balanced_owner(
    net_file: str,
    trips_file: str,
    accident: bool = False,
    accident_edges: Optional[Sequence[str]] = None,
    cache_dir: Optional[Path] = None,
    force: bool = False,
    axis: str = "x",
    num_groups: int = 2,
) -> Dict[str, str]:
    """Return ``{edge_id: "G0".."G{n-1}"}`` balanced by (rerouted) traffic load.

    The network is sliced into ``num_groups`` CONTIGUOUS geographic strips
    along ``axis`` (``"x"`` = west->east vertical cuts, ``"y"`` = south->north
    horizontal cuts). The cut positions are chosen so each strip carries
    ~``1/num_groups`` of the total per-edge traffic load. Because trips
    traverse the map monotonically along the axis, a single trip crosses the
    strips in order and is therefore handed off across every worker in turn
    (which is the whole point: vehicles must circulate through several PCs).

    Cached on disk keyed on (net, trips, accident, axis, num_groups).
    """
    cache_dir = Path(cache_dir or CACHE_DIR_DEFAULT)
    cache_dir.mkdir(parents=True, exist_ok=True)
    axis = "y" if str(axis).lower().startswith("y") else "x"
    num_groups = max(2, int(num_groups))
    labels = group_labels(num_groups)
    key = _hash_inputs(net_file, trips_file, accident, accident_edges,
                       axis=axis, num_groups=num_groups)
    cache_path = cache_dir / f"balanced_{key}.json"

    if cache_path.exists() and not force:
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            owner = data["edge_owner"]
            print(f"[balanced] cache hit ({key}) axis={axis} n={num_groups} "
                  f"-> {len(owner)} edges, counts={data.get('counts', {})}")
            return owner
        except Exception:
            pass

    sumolib = _import_sumolib()
    t0 = time.perf_counter()
    net = sumolib.net.readNet(net_file, withInternal=False)
    adj = _build_adjacency(net)
    cost_of = {e.getID(): _edge_cost(e) for e in net.getEdges()}
    start_cost_of = lambda eid: cost_of.get(eid, 1.0)

    # Determine blocked edges (accident stretch) for accident-aware routing.
    blocked: Set[str] = set()
    if accident and accident_edges and len(accident_edges) >= 2:
        try:
            r = net.getShortestPath(
                net.getEdge(accident_edges[0]),
                net.getEdge(accident_edges[1]),
            )
            if r and r[0]:
                blocked = {e.getID() for e in r[0]}
            else:
                blocked = set(accident_edges)
        except Exception:
            blocked = set(accident_edges)
        print(f"[balanced] accident-aware: blocking {len(blocked)} edges "
              f"{sorted(blocked)}")

    # Per-edge load from (rerouted) shortest routes.
    edge_load: Dict[str, float] = defaultdict(float)
    tree = ET.parse(trips_file)
    root = tree.getroot()
    trips = root.findall("trip")
    # Large nets with mostly-unique O/D pairs (e.g. Rotterdam: 20k pairs over
    # 22k edges) make exhaustive dijkstra prohibitive. Sampling a few thousand
    # trips estimates the cumulative-load cut positions just as well.
    MAX_ROUTED = 3000
    if len(trips) > MAX_ROUTED:
        import random as _rnd
        trips = _rnd.Random(1234).sample(trips, MAX_ROUTED)
        print(f"[balanced] sampling {MAX_ROUTED} trips for load estimation")
    od_route: Dict[Tuple[str, str], List[str]] = {}
    n_unrouted = 0
    for tr in trips:
        f = tr.get("from")
        t = tr.get("to")
        if not f or not t:
            continue
        route = od_route.get((f, t))
        if route is None:
            try:
                route = _dijkstra(adj, start_cost_of, f, t, blocked)
            except Exception:
                route = []
            od_route[(f, t)] = route
        if not route:
            n_unrouted += 1
            continue
        for e in route:
            edge_load[e] += 1.0

    # Order all edges by centroid coordinate along the chosen axis and slice
    # into ``num_groups`` strips at cumulative-load fractions k/num_groups.
    centroid = _edge_centroid_y if axis == "y" else _edge_centroid_x
    all_edges = [e.getID() for e in net.getEdges()]
    coord = {eid: centroid(net.getEdge(eid)) for eid in all_edges}
    ordered = sorted(all_edges, key=lambda eid: coord[eid])
    total = sum(edge_load.values()) or 1.0
    thresholds = [total * k / num_groups for k in range(1, num_groups)]

    owner: Dict[str, str] = {}
    cuts: List[float] = []
    cum = 0.0
    gi = 0
    for eid in ordered:
        # Advance to the next group while we've crossed the next threshold.
        while gi < len(thresholds) and cum >= thresholds[gi]:
            cuts.append(coord[eid])
            gi += 1
        owner[eid] = labels[gi]
        cum += edge_load.get(eid, 0.0)

    counts = {g: 0 for g in labels}
    load_by_group = {g: 0.0 for g in labels}
    for eid, g in owner.items():
        counts[g] += 1
        load_by_group[g] += edge_load.get(eid, 0.0)

    print(f"[balanced] built in {time.perf_counter() - t0:.1f}s | axis={axis} "
          f"n={num_groups} | edges={counts} | "
          f"load={{ {', '.join(f'{g}:{load_by_group[g]:.0f}' for g in labels)} }} | "
          f"cuts={[round(c) for c in cuts]} | unrouted ODs={n_unrouted}")

    cache_path.write_text(json.dumps({
        "edge_owner": owner,
        "counts": counts,
        "load_by_group": load_by_group,
        "axis": axis,
        "num_groups": num_groups,
        "cuts": cuts,
        "groups": labels,
        "accident": accident,
        "accident_edges": list(accident_edges) if accident_edges else None,
        "net_file": net_file,
        "trips_file": trips_file,
        "created_at": time.time(),
    }, indent=2), encoding="utf-8")
    return owner


def compute_highway_owner(
    net_file: str,
    cache_dir: Optional[Path] = None,
    force: bool = False,
) -> Dict[str, str]:
    """Partition every edge to the nearest highway corridor: ``"A7"`` vs
    ``"N340"`` (by Euclidean distance from the edge centroid to each
    corridor's reference centroid, derived from the sentinel key edges in
    :mod:`corridors`). This yields two contiguous halves split by the
    perpendicular bisector of the two parallel highways, so trips that weave
    between A7 and N340 are handed off between the two workers.
    """
    from SIMULATION.distributed.corridors import CORRIDOR_KEY_EDGES

    cache_dir = Path(cache_dir or CACHE_DIR_DEFAULT)
    cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        st = os.stat(net_file)
        key = hashlib.md5(
            f"{net_file}|{int(st.st_mtime)}|{st.st_size}|highway".encode()
        ).hexdigest()[:12]
    except OSError:
        key = hashlib.md5(f"{net_file}|highway".encode()).hexdigest()[:12]
    cache_path = cache_dir / f"highway_{key}.json"
    if cache_path.exists() and not force:
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))["edge_owner"]
        except Exception:
            pass

    sumolib = _import_sumolib()
    net = sumolib.net.readNet(net_file, withInternal=False)

    def _ref_centroid(corridor: str):
        xs, ys = [], []
        for eid in CORRIDOR_KEY_EDGES.get(corridor, ()):  # type: ignore[union-attr]
            try:
                cxy = _edge_centroid_xy(net.getEdge(eid))
                xs.append(cxy[0])
                ys.append(cxy[1])
            except Exception:
                continue
        if not xs:
            return None
        return (sum(xs) / len(xs), sum(ys) / len(ys))

    ref = {"A7": _ref_centroid("A7"), "N340": _ref_centroid("N340")}
    if ref["A7"] is None or ref["N340"] is None:
        raise RuntimeError("highway partition: missing A7/N340 key-edge geometry")

    owner: Dict[str, str] = {}
    counts = {"A7": 0, "N340": 0}
    for e in net.getEdges():
        cx, cy = _edge_centroid_xy(e)
        da = (cx - ref["A7"][0]) ** 2 + (cy - ref["A7"][1]) ** 2
        dn = (cx - ref["N340"][0]) ** 2 + (cy - ref["N340"][1]) ** 2
        g = "A7" if da <= dn else "N340"
        owner[e.getID()] = g
        counts[g] += 1
    print(f"[highway] A7={counts['A7']} N340={counts['N340']} edges "
          f"(refA7={tuple(round(v) for v in ref['A7'])} "
          f"refN340={tuple(round(v) for v in ref['N340'])})")

    cache_path.write_text(json.dumps({
        "edge_owner": owner, "counts": counts, "groups": ["A7", "N340"],
        "net_file": net_file, "created_at": time.time(),
    }, indent=2), encoding="utf-8")
    return owner


def assign_trip_group(owner: Dict[str, str], from_edge: str,
                      default: Optional[str] = None) -> str:
    """Group that should *insert* a trip: owner of its origin edge.

    Falls back to ``default`` (or the lexicographically-first owner value
    seen) when the origin edge has no owner entry.
    """
    g = owner.get(from_edge)
    if g:
        return g
    if default is not None:
        return default
    # Deterministic fallback: first group label present in the map.
    try:
        return sorted(set(owner.values()))[0]
    except Exception:
        return "G0"


__all__ = [
    "GROUPS", "group_labels", "compute_balanced_owner",
    "compute_highway_owner", "assign_trip_group",
]
