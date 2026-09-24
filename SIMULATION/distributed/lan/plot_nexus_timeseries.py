"""Series temporales por segmento-NEXO: baseline (1 nodo) vs distribuido (6
cortes). Genera DOS figuras (flujo y conteo), con los nexos en rejilla de 2
columnas; eje X = hora, eje Y = la metrica.

    python -m SIMULATION.distributed.lan.plot_nexus_timeseries --run <ts> [--top 6]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from SIMULATION.distributed.lan.find_nexus_edges import find_nexus  # noqa: E402

EQUIV = PROJECT_ROOT / "SIMULATION" / "distributed" / "lan" / "equivalence"


def series_for(env: dict, eid: str, length: float):
    """Devuelve (horas, count, flujo_vph) ordenado por tiempo para un edge."""
    slots = env.get(eid, {})
    by_t = {}
    for _slot, obs in slots.items():
        for o in obs:
            t = float(o.get("time", 0) or 0)
            c = float(o.get("count", 0) or 0)
            v = float(o.get("speed", 0) or 0)
            # si hay varias obs al mismo t (agregacion), promediamos
            if t in by_t:
                pc, pv, n = by_t[t]
                by_t[t] = (pc + c, pv + v, n + 1)
            else:
                by_t[t] = (c, v, 1)
    ts = sorted(by_t)
    hrs = [t / 3600.0 for t in ts]
    cnt = [by_t[t][0] / by_t[t][2] for t in ts]
    spd = [by_t[t][1] / by_t[t][2] for t in ts]
    flow = [3600.0 * c * s / length if length else 0.0
            for c, s in zip(cnt, spd)]
    return hrs, cnt, flow


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--top", type=int, default=6, help="nº de nexos (rejilla 2 col)")
    args = ap.parse_args(argv)

    run_dir = EQUIV / args.run
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    road, partition = meta["road"], meta["partition"]

    base_env = json.loads(
        (run_dir / "baseline" / "environment_traffic.json").read_text("utf-8"))
    dist_env = json.loads(
        (run_dir / "distributed" / "environment_traffic.json").read_text("utf-8"))

    import sumolib
    net = sumolib.net.readNet(
        str(PROJECT_ROOT / "SIMULATION" / "roads" / f"{road}.net.xml"),
        withInternal=False)
    lengths = {e.getID(): e.getLength() for e in net.getEdges()}

    _o, _n, nexus = find_nexus(road, partition, bool(meta.get("accident")))

    # elegir los top-N nexos por flujo total en baseline
    scored = []
    for eid in nexus:
        L = lengths.get(eid, 0)
        _h, _c, f = series_for(base_env, eid, L)
        scored.append((sum(f), eid))
    scored.sort(reverse=True)
    picks = [eid for _s, eid in scored[:args.top] if _s > 0]
    print(f"[ts] top {len(picks)} nexos: {picks}")

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ncol = 2
    nrow = (len(picks) + ncol - 1) // ncol

    for metric, ylabel, idx in (("flujo", "Flujo (veh/h)", 2),
                                ("conteo", "Conteo (vehículos en arista)", 1)):
        fig, axes = plt.subplots(nrow, ncol, figsize=(12, 3 * nrow),
                                 squeeze=False)
        for k, eid in enumerate(picks):
            ax = axes[k // ncol][k % ncol]
            L = lengths.get(eid, 0)
            hb, cb, fb = series_for(base_env, eid, L)
            hd, cd, fd = series_for(dist_env, eid, L)
            yb = fb if metric == "flujo" else cb
            yd = fd if metric == "flujo" else cd
            ax.plot(hb, yb, "-o", ms=3, lw=1.4, color="#1f77b4",
                    label="1 nodo (baseline)")
            ax.plot(hd, yd, "-s", ms=3, lw=1.4, color="#d1004b",
                    label="6 cortes (distribuido)")
            ax.set_title(eid, fontsize=8)
            ax.set_xlabel("Hora (h)")
            ax.set_ylabel(ylabel, fontsize=8)
            ax.grid(alpha=0.25)
            if k == 0:
                ax.legend(fontsize=7, loc="upper right")
        # ocultar ejes vacios
        for k in range(len(picks), nrow * ncol):
            axes[k // ncol][k % ncol].axis("off")
        fig.suptitle(f"{metric.capitalize()} por nexo - baseline vs 6 cortes "
                     f"[{road}, {partition}]", fontsize=12)
        fig.tight_layout(rect=[0, 0, 1, 0.98])
        out = run_dir / f"nexus_timeseries_{metric}.png"
        fig.savefig(out, dpi=140)
        plt.close(fig)
        print(f"[ts] -> {out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
