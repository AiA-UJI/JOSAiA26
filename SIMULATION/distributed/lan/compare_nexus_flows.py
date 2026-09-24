"""Compara el flujo por segmento entre baseline (1 nodo) y distribuido
(N cortes) en las aristas-NEXO (frontera/handoff), para validar que el
particionado no altera el trafico.

Lee una carpeta de SIMULATION/distributed/lan/equivalence/<ts>/ con:
    baseline/environment_traffic.json
    distributed/environment_traffic.json
    meta.json

Flujo por edge (veh/h) = 3600 * media_slots(count * speed) / longitud_edge.
Misma formula en ambos lados -> comparable.

Salida (en la misma carpeta del run):
    nexus_flow_compare.csv     (edge, grupo, base, dist, err_rel)
    nexus_flow_scatter.png     (base vs dist, 1 punto por edge nexo)
    nexus_flow_summary.json

Uso:
    python -m SIMULATION.distributed.lan.compare_nexus_flows --run <ts>
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from SIMULATION.distributed.lan.find_nexus_edges import find_nexus  # noqa: E402

EQUIV_DIR = PROJECT_ROOT / "SIMULATION" / "distributed" / "lan" / "equivalence"


def edge_flow_vph(env: dict, lengths: dict) -> dict:
    """{edge: mean flow veh/h} desde environment_traffic.json."""
    out = {}
    for eid, slots in env.items():
        L = lengths.get(eid)
        if not L or L <= 0:
            continue
        prod = []
        for _slot, obs in slots.items():
            for o in obs:
                c = float(o.get("count", 0) or 0)
                v = float(o.get("speed", 0) or 0)
                prod.append(c * v)
        if prod:
            out[eid] = 3600.0 * (sum(prod) / len(prod)) / L
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True,
                    help="nombre de carpeta bajo equivalence/ (o ruta completa)")
    args = ap.parse_args(argv)

    run_dir = Path(args.run)
    if not run_dir.is_absolute():
        run_dir = EQUIV_DIR / args.run
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    road = meta["road"]
    partition = meta["partition"]
    accident = bool(meta.get("accident"))

    base_env = json.loads(
        (run_dir / "baseline" / "environment_traffic.json").read_text("utf-8"))
    dist_env = json.loads(
        (run_dir / "distributed" / "environment_traffic.json").read_text("utf-8"))
    print(f"[cmp] baseline edges={len(base_env)}  dist edges={len(dist_env)}")

    import sumolib
    net_file = str(PROJECT_ROOT / "SIMULATION" / "roads" / f"{road}.net.xml")
    net = sumolib.net.readNet(net_file, withInternal=False)
    lengths = {e.getID(): e.getLength() for e in net.getEdges()}

    _owner, _net, nexus = find_nexus(road, partition, accident)
    nexus_ids = set(nexus.keys())
    print(f"[cmp] aristas nexo: {len(nexus_ids)}")

    base_flow = edge_flow_vph(base_env, lengths)
    dist_flow = edge_flow_vph(dist_env, lengths)

    rows = []
    for eid in sorted(nexus_ids):
        b = base_flow.get(eid, 0.0)
        d = dist_flow.get(eid, 0.0)
        denom = b if b > 1e-9 else None
        err = (abs(d - b) / denom) if denom else None
        rows.append((eid, nexus[eid]["own"], ",".join(nexus[eid]["to"]),
                     round(b, 2), round(d, 2),
                     round(err, 4) if err is not None else ""))

    csv_path = run_dir / "nexus_flow_compare.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["edge", "grupo", "vecinos", "flujo_base_vph",
                    "flujo_dist_vph", "err_rel"])
        w.writerows(rows)

    # estadisticas sobre edges con flujo baseline no trivial
    pairs = [(b, d) for (_e, _g, _t, b, d, _er) in rows if b > 1.0]
    n = len(pairs)
    summary = {"run": run_dir.name, "road": road, "partition": partition,
               "accident": accident, "n_nexus": len(nexus_ids),
               "n_nexus_activos": n}
    if n:
        bs = [p[0] for p in pairs]
        ds = [p[1] for p in pairs]
        errs = [abs(d - b) / b for b, d in pairs]
        errs.sort()
        mb, md = sum(bs) / n, sum(ds) / n
        cov = sum((b - mb) * (d - md) for b, d in pairs)
        vb = sum((b - mb) ** 2 for b in bs) ** 0.5
        vd = sum((d - md) ** 2 for d in ds) ** 0.5
        r = cov / (vb * vd) if vb > 0 and vd > 0 else 0.0
        summary.update({
            "pearson_r": round(r, 4),
            "err_rel_medio": round(sum(errs) / n, 4),
            "err_rel_mediana": round(errs[n // 2], 4),
            "flujo_total_base": round(sum(bs), 1),
            "flujo_total_dist": round(sum(ds), 1),
            "ratio_total_dist_base": round(sum(ds) / sum(bs), 4) if sum(bs) else None,
        })
    (run_dir / "nexus_flow_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8")

    # scatter
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        bs = [b for (_e, _g, _t, b, d, _er) in rows if b > 1.0]
        ds = [d for (_e, _g, _t, b, d, _er) in rows if b > 1.0]
        fig, ax = plt.subplots(figsize=(7, 7))
        ax.scatter(bs, ds, s=14, alpha=0.5, color="#1f77b4")
        hi = max(max(bs, default=1), max(ds, default=1)) * 1.05
        ax.plot([0, hi], [0, hi], "--", color="#888", lw=1, label="y = x")
        ax.set_xlim(0, hi)
        ax.set_ylim(0, hi)
        ax.set_xlabel("Flujo baseline (1 nodo) [veh/h]")
        ax.set_ylabel(f"Flujo distribuido ({meta.get('workers')} cortes) [veh/h]")
        ax.set_title(f"Equivalencia de flujo en nexos - {partition} [{road}]\n"
                     f"n={n}  r={summary.get('pearson_r')}  "
                     f"err_rel_med={summary.get('err_rel_medio')}")
        ax.legend(loc="upper left")
        fig.tight_layout()
        fig.savefig(run_dir / "nexus_flow_scatter.png", dpi=140)
        plt.close(fig)
    except Exception as ex:
        print(f"[cmp] scatter fallo: {ex}")

    print(f"[cmp] CSV -> {csv_path}")
    print(f"[cmp] resumen: {json.dumps(summary, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
