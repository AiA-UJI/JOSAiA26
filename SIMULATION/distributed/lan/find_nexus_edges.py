"""Identifica las aristas-NEXO (frontera) de una particion: aristas cuyo
sucesor (o predecesor) pertenece a OTRO grupo. Por ahi cruzan los vehiculos
de un worker a otro (puntos de handoff). Son las claves para validar que el
distribuido reproduce el mismo trafico que el secuencial.

Uso:
    python -m SIMULATION.distributed.lan.find_nexus_edges \
        --road rotterdam_arterial --partition balanced-y-6 [--accident]

Salida:
    - lista de edge IDs nexo (stdout + JSON)
    - PNG resaltando las aristas nexo sobre la red
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from SIMULATION.distributed.lan.plot_partition_map import _owner_for, _paths_for


def find_nexus(road: str, partition: str, accident: bool):
    import sumolib

    owner = _owner_for(partition, accident=accident, road=road)
    net_file, _ = _paths_for(road)
    net = sumolib.net.readNet(net_file, withInternal=False)

    nexus = {}  # edge_id -> {"own": g, "to": set(groups), "kind": set()}
    for e in net.getEdges():
        eid = e.getID()
        g = owner.get(eid)
        if not g:
            continue
        # sucesores (outgoing)
        for nxt in e.getOutgoing():
            og = owner.get(nxt.getID())
            if og and og != g:
                d = nexus.setdefault(eid, {"own": g, "to": set(), "kind": set()})
                d["to"].add(og)
                d["kind"].add("out")
        # predecesores (incoming)
        for prv in e.getIncoming():
            pg = owner.get(prv.getID())
            if pg and pg != g:
                d = nexus.setdefault(eid, {"own": g, "to": set(), "kind": set()})
                d["to"].add(pg)
                d["kind"].add("in")

    return owner, net, nexus


def plot_nexus(road, partition, net, owner, nexus, out_png):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import LineCollection

    bg, bg_c = [], []
    nx_segs = []
    for e in net.getEdges():
        pts = [(x / 1000.0, y / 1000.0) for x, y in e.getShape()]
        if len(pts) < 2:
            continue
        seglist = list(zip(pts[:-1], pts[1:]))
        if e.getID() in nexus:
            nx_segs.extend(seglist)
        else:
            bg.extend(seglist)
            bg_c.extend(["#d9d9d9"] * len(seglist))

    fig, ax = plt.subplots(figsize=(14, 9))
    ax.add_collection(LineCollection(bg, colors=bg_c, linewidths=0.3))
    ax.add_collection(LineCollection(nx_segs, colors="#d1004b", linewidths=2.2))
    ax.autoscale()
    ax.set_aspect("equal")
    ax.set_xlabel("x (km)")
    ax.set_ylabel("y (km)")
    ax.set_title(f"Nexus (handoff) edges: {partition} [{road}] "
                 f"- {len(nexus)} edges")
    from matplotlib.lines import Line2D
    ax.legend(handles=[
        Line2D([0], [0], color="#d1004b", lw=3,
               label=f"Nexus / handoff edges ({len(nexus)})"),
        Line2D([0], [0], color="#d9d9d9", lw=3, label="Other edges"),
    ], loc="upper right", fontsize=11)
    fig.tight_layout()
    fig.savefig(out_png, dpi=140)
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--road", default="rotterdam_arterial")
    ap.add_argument("--partition", default="balanced-y-6")
    ap.add_argument("--accident", action="store_true")
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()

    owner, net, nexus = find_nexus(args.road, args.partition, args.accident)

    out_dir = Path(args.out_dir or (PROJECT_ROOT / "SIMULATION" / "distributed"
                                    / "lan" / "nexus"))
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"{args.partition}_{args.road}" + ("_acc" if args.accident else "")

    result = {
        "road": args.road,
        "partition": args.partition,
        "accident": args.accident,
        "n_nexus": len(nexus),
        "edges": {k: {"own": v["own"], "to": sorted(v["to"]),
                      "kind": sorted(v["kind"])} for k, v in nexus.items()},
    }
    json_path = out_dir / f"nexus_{suffix}.json"
    json_path.write_text(json.dumps(result, indent=2), encoding="utf-8")

    png_path = out_dir / f"nexus_{suffix}.png"
    plot_nexus(args.road, args.partition, net, owner, nexus, png_path)

    print(f"[nexus] {args.partition} [{args.road}]"
          f"{' acc' if args.accident else ''}: {len(nexus)} aristas nexo")
    print(f"[nexus] JSON -> {json_path}")
    print(f"[nexus] PNG  -> {png_path}")
    # muestra las primeras 30
    for i, (k, v) in enumerate(sorted(nexus.items())):
        if i >= 30:
            print(f"   ... (+{len(nexus) - 30} mas)")
            break
        print(f"   {k}: {v['own']} <-> {','.join(sorted(v['to']))}")


if __name__ == "__main__":
    main()
