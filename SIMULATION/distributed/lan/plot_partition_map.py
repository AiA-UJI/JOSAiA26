"""Render the network partition (edge -> owner group) for each strategy as a
PNG, so the "cuts" over the map are visible. Runs on the control PC (needs
sumolib + matplotlib, both available locally).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_ROAD = "modified_4ways_all"

# Reference trips per road (used to weight the balanced cuts).
ROAD_TRIPS = {
    "modified_4ways_all": "fullnet_trips_intelligent_99.xml",
    "rotterdam_arterial": "rotterdam_trips_intelligent_99_20000.xml",
}


def _paths_for(road: str):
    net = str(PROJECT_ROOT / "SIMULATION" / "roads" / f"{road}.net.xml")
    trips = str(PROJECT_ROOT / "SIMULATION" / "trips" / ROAD_TRIPS[road])
    return net, trips


NET_FILE, TRIPS_FILE = _paths_for(DEFAULT_ROAD)

PALETTE = [
    "#e6194B", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#42d4f4",
    "#f032e6", "#bfef45", "#fabed4", "#469990",
]
SHARED_COLOR = "#999999"


def _owner_for(partition: str, accident: bool = False,
               road: str = DEFAULT_ROAD) -> Dict[str, str]:
    from SIMULATION.distributed.partitioning import parse_partition
    from SIMULATION.distributed.balanced_partition import (
        compute_balanced_owner, compute_highway_owner)
    from SIMULATION.distributed.edge_classifier import build_edge_owner_map
    from SIMULATION.CONSTANTS import get_road_config

    net_file, trips_file = _paths_for(road)
    spec = parse_partition(partition)
    if spec.kind == "highway":
        return compute_highway_owner(net_file)
    if spec.kind == "balanced":
        acc_edges = None
        if accident:
            acc_edges = get_road_config(road).get("accident_edges")
        return compute_balanced_owner(
            net_file, trips_file, accident=accident,
            accident_edges=list(acc_edges) if acc_edges else None,
            axis=spec.axis, num_groups=spec.num_groups)
    # corridor
    owner, _counts = build_edge_owner_map(net_file, trips_file, spec.num_groups)
    return owner


def plot_partition(partition: str, out_path: Path, accident: bool = False,
                   road: str = DEFAULT_ROAD):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import LineCollection
    import sumolib

    owner = _owner_for(partition, accident=accident, road=road)
    net_file, _ = _paths_for(road)
    net = sumolib.net.readNet(net_file, withInternal=False)

    groups = sorted({g for g in owner.values() if g and g != "SHARED"})
    color_of = {g: PALETTE[i % len(PALETTE)] for i, g in enumerate(groups)}
    color_of["SHARED"] = SHARED_COLOR

    segs: List = []
    cols: List = []
    counts: Dict[str, int] = {}
    for e in net.getEdges():
        shp = e.getShape()
        if not shp or len(shp) < 2:
            continue
        g = owner.get(e.getID(), "SHARED")
        c = color_of.get(g, SHARED_COLOR)
        for a, b in zip(shp[:-1], shp[1:]):
            segs.append([a, b])
            cols.append(c)
        counts[g] = counts.get(g, 0) + 1

    fig, ax = plt.subplots(figsize=(11, 11))
    ax.add_collection(LineCollection(segs, colors=cols, linewidths=0.7))
    ax.autoscale()
    ax.set_aspect("equal")
    ax.set_title(f"Partition: {partition}  [{road}]"
                 + ("  (accident-aware)" if accident else ""))
    ax.axis("off")
    handles = [plt.Line2D([0], [0], color=color_of[g], lw=3,
                          label=f"{g} ({counts.get(g, 0)} edges)")
               for g in groups]
    if "SHARED" in counts:
        handles.append(plt.Line2D([0], [0], color=SHARED_COLOR, lw=3,
                                  label=f"SHARED ({counts['SHARED']})"))
    ax.legend(handles=handles, loc="upper right", fontsize=9)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=130, bbox_inches="tight")
    plt.close(fig)
    print(f"[partmap] {partition}{' acc' if accident else ''} -> {out_path.name} "
          f"({counts})")


def plot_all_partitions(out_dir: Path, partitions: Optional[List[str]] = None,
                        road: str = DEFAULT_ROAD):
    from SIMULATION.distributed.lan.matrix import PARTITIONS, REF_PARTITION
    partitions = partitions or PARTITIONS
    out_dir = Path(out_dir)
    suffix = "" if road == DEFAULT_ROAD else f"_{road}"
    for p in partitions:
        try:
            plot_partition(p, out_dir / f"partition_{p}{suffix}.png",
                           accident=False, road=road)
        except Exception as e:
            print(f"[partmap] {p} FAILED: {e}")
    # accident-aware version of the reference balanced partition
    try:
        plot_partition(REF_PARTITION,
                       out_dir / f"partition_{REF_PARTITION}{suffix}_acc.png",
                       accident=True, road=road)
    except Exception as e:
        print(f"[partmap] {REF_PARTITION} acc FAILED: {e}")


if __name__ == "__main__":
    out = PROJECT_ROOT / "SIMULATION" / "distributed" / "lan" / "_partition_maps"
    plot_all_partitions(out)
