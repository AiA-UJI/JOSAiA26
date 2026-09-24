"""Multigrafica de equivalencia: por cada segmento CRITICO (arista-nexo),
superpone el trazado del BASELINE (1 nodo) y el de CADA CORTE que tenga datos
de edgeData, para comprobar visualmente que el trafico por segmento coincide.

Escanea SIMULATION/distributed/lan/equivalence/*/ y usa toda ejecucion que tenga
  baseline/environment_traffic.edgedata.xml   y
  distributed/workers/*.edgedata.xml
agrupando por 'partition' (meta.json) -> un trazado (color) por corte.

Segmentos mostrados: union de aristas-nexo de los cortes disponibles, ordenadas
por trafico del baseline (top-N).

Salida (en equivalence/): multicut_ts_flujo.png, multicut_ts_conteo.png,
multicut_equivalencia.csv

    python -m SIMULATION.distributed.lan.plot_multicut_equivalence [--top 8]
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
from SIMULATION.distributed.lan.plot_nexus_edgedata import (  # noqa: E402
    parse_edgedata, merge_workers, series, _clock_ticks)

EQUIV = PROJECT_ROOT / "SIMULATION" / "distributed" / "lan" / "equivalence"

# Color por CORTE (no por grupo): baseline en negro.
CUT_PALETTE = [
    "#d1004b", "#1f9e3a", "#4363d8", "#f58231", "#911eb4", "#00b3b3",
    "#e6ab02", "#e7298a", "#66a61e", "#7570b3",
]
BASE_COLOR = "#111111"


def _discover_runs():
    """Devuelve [(partition, run_dir, meta)] con edgeData distribuido valido,
    quedandose con la ejecucion mas reciente por particion. El baseline es
    opcional (se comparte uno canonico entre cortes)."""
    runs = {}
    for d in sorted(EQUIV.iterdir()):
        if not d.is_dir():
            continue
        meta_p = d / "meta.json"
        wdir = d / "distributed" / "workers"
        if not (meta_p.is_file() and wdir.is_dir()):
            continue
        if not list(wdir.glob("*.edgedata.xml")):
            continue
        try:
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
        except Exception:
            continue
        part = meta.get("partition", "?")
        runs[part] = (part, d, meta)   # dir names ordenados -> queda el ultimo
    return list(runs.values())


def _find_canonical_baseline():
    """Mayor baseline/environment_traffic.edgedata.xml entre las validaciones."""
    best = None
    for d in EQUIV.iterdir():
        if not d.is_dir():
            continue
        bx = d / "baseline" / "environment_traffic.edgedata.xml"
        if bx.is_file() and (best is None or bx.stat().st_size > best.stat().st_size):
            best = bx
    return best


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=8,
                    help="nº de segmentos criticos a mostrar")
    args = ap.parse_args(argv)

    found = _discover_runs()
    if not found:
        print("[multicut] no hay ejecuciones de equivalencia con edgeData")
        return 1
    print(f"[multicut] cortes con datos: {[p for p, _, _ in found]}")

    road = found[0][2].get("road", "rotterdam_arterial")

    # baseline canonico compartido (todas las validaciones usan misma flota/mapa)
    base_xml = _find_canonical_baseline()
    if base_xml is None:
        print("[multicut] FATAL: no hay baseline con edgeData")
        return 1
    print(f"[multicut] baseline canonico: {base_xml.parent.parent.name}")

    # conjunto de segmentos criticos: union de nexos de cada corte disponible
    nexus_union = {}
    for part, d, meta in found:
        try:
            _o, _n, nexus = find_nexus(road, part, bool(meta.get("accident")))
            nexus_union.update(nexus)
        except Exception as ex:
            print(f"[multicut] aviso nexos {part}: {ex}")
    keep = set(nexus_union.keys())
    print(f"[multicut] segmentos-nexo union: {len(keep)}")

    # cargar edgeData: baseline (canonico) + distribuido de cada corte.
    # SUMA de todas las maquinas que tocan la arista = segmento-nexo COMPLETO
    # (recompone el flujo repartido entre ambos lados del handoff).
    base = parse_edgedata(base_xml, keep)
    cuts = []   # (partition, merged_dict, color)
    for i, (part, d, meta) in enumerate(found):
        wfiles = sorted((d / "distributed" / "workers").glob("*.edgedata.xml"))
        merged = merge_workers(wfiles, keep, mode="sum")
        cuts.append((part, merged, CUT_PALETTE[i % len(CUT_PALETTE)]))

    # ranking de segmentos por trafico del baseline
    def tot(dat, eid):
        return sum(r["entered"] for r in dat.get(eid, {}).values())

    picks = sorted((e for e in keep if tot(base, e) > 0),
                   key=lambda e: tot(base, e), reverse=True)[:args.top]
    print(f"[multicut] top-{len(picks)} segmentos por trafico baseline")

    # CSV resumen: totales por segmento y corte + error relativo vs baseline
    with (EQUIV / "multicut_equivalencia.csv").open("w", newline="",
                                                    encoding="utf-8") as f:
        w = csv.writer(f)
        head = ["edge", "total_baseline"]
        for part, _m, _c in cuts:
            head += [f"total_{part}", f"errrel_{part}"]
        w.writerow(head)
        for eid in picks:
            tb = tot(base, eid)
            row = [eid, int(round(tb))]
            for part, m, _c in cuts:
                td = tot(m, eid)
                err = abs(td - tb) / tb if tb > 0 else ""
                row += [int(round(td)), round(err, 4) if err != "" else ""]
            w.writerow(row)
    print(f"[multicut] -> {EQUIV / 'multicut_equivalencia.csv'}")

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ncol = 2
    nrow = (len(picks) + ncol - 1) // ncol
    for field, fname, ylabel, scale in (
            ("flow", "multicut_ts_flujo.png", "Flujo (veh/h)", 1.0),
            ("entered", "multicut_ts_conteo.png",
             "Vehículos entrantes / min", 1.0 / 5.0)):
        fig, axes = plt.subplots(nrow, ncol, figsize=(13, 3.1 * nrow),
                                 squeeze=False)
        for k, eid in enumerate(picks):
            ax = axes[k // ncol][k % ncol]
            xb, yb = series(base, eid, field)
            yb = [v * scale for v in yb]
            ax.plot(xb, yb, "-o", ms=3, lw=2.0, color=BASE_COLOR,
                    label="baseline (1 nodo)", zorder=5)
            for part, m, c in cuts:
                xd, yd = series(m, eid, field)
                yd = [v * scale for v in yd]
                if xd:
                    ax.plot(xd, yd, "-", ms=2, lw=1.3, color=c, alpha=0.85,
                            label=part)
            own = nexus_union[eid]["own"]
            ax.set_title(f"{eid}  ({own})", fontsize=8, fontweight="bold")
            max_h = xb[-1] if xb else 4.0
            ticks, labels = _clock_ticks(max_h)
            ax.set_xticks(ticks)
            ax.set_xticklabels(labels, rotation=90, fontsize=6)
            ax.set_xlabel("Hora (HH:MM)", fontsize=8)
            ax.set_ylabel(ylabel, fontsize=8)
            ax.grid(alpha=0.25)
            if k == 0:
                ax.legend(fontsize=7, ncol=2)
        for k in range(len(picks), nrow * ncol):
            axes[k // ncol][k % ncol].axis("off")
        cutlist = ", ".join(p for p, _m, _c in cuts)
        fig.suptitle(f"{ylabel} por segmento critico — baseline vs cortes "
                     f"[{road}]\ncortes: {cutlist}", fontsize=12)
        fig.tight_layout(rect=[0, 0, 1, 0.96])
        fig.savefig(EQUIV / fname, dpi=140)
        plt.close(fig)
        print(f"[multicut] -> {EQUIV / fname}")

    # ---- Scatter de CONSERVACION: coches totales por nexo, baseline vs corte ----
    # (sobre TODOS los segmentos-nexo con trafico, no solo el top-N)
    all_edges = [e for e in keep if tot(base, e) > 0]
    import numpy as np
    ncut = len(cuts)
    ncol2 = min(3, ncut) or 1
    nrow2 = (ncut + ncol2 - 1) // ncol2
    fig, axes = plt.subplots(nrow2, ncol2, figsize=(4.6 * ncol2, 4.4 * nrow2),
                             squeeze=False)
    cons_rows = []
    for i, (part, m, c) in enumerate(cuts):
        ax = axes[i // ncol2][i % ncol2]
        xs = np.array([tot(base, e) for e in all_edges], dtype=float)
        ys = np.array([tot(m, e) for e in all_edges], dtype=float)
        lim = max(xs.max(), ys.max()) * 1.05 if len(xs) else 1.0
        ax.plot([0, lim], [0, lim], "-", color="#888", lw=1, zorder=1)
        ax.fill_between([0, lim], [0, 0.9 * lim], [0, 1.1 * lim],
                        color="#4363d8", alpha=0.10, zorder=0, label="±10%")
        ax.scatter(xs, ys, s=14, color=c, alpha=0.6, edgecolors="none", zorder=3)
        tot_b, tot_d = float(xs.sum()), float(ys.sum())
        ratio = tot_d / tot_b if tot_b else 0.0
        within = float(np.mean(np.abs(ys - xs) <= 0.10 * np.maximum(xs, 1))) * 100
        mape = float(np.mean(np.abs(ys - xs) / np.maximum(xs, 1))) * 100
        ax.set_title(f"{part}\nΣ dist/base = {ratio:.3f}  |  "
                     f"{within:.0f}% en ±10%  |  MAPE {mape:.1f}%",
                     fontsize=8, fontweight="bold")
        ax.set_xlabel("coches baseline (1 nodo)", fontsize=8)
        ax.set_ylabel("coches distribuido", fontsize=8)
        ax.grid(alpha=0.25)
        ax.legend(fontsize=7, loc="upper left")
        cons_rows.append({"corte": part, "n_nexos": len(all_edges),
                          "total_baseline": int(round(tot_b)),
                          "total_distribuido": int(round(tot_d)),
                          "ratio": round(ratio, 4),
                          "pct_en_banda_10": round(within, 1),
                          "mape_pct": round(mape, 2)})
    for i in range(ncut, nrow2 * ncol2):
        axes[i // ncol2][i % ncol2].axis("off")
    fig.suptitle("Conservación de vehículos por segmento-nexo (baseline vs corte)"
                 f"  —  {road}", fontsize=12)
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    fig.savefig(EQUIV / "multicut_conservacion_scatter.png", dpi=140)
    plt.close(fig)
    print(f"[multicut] -> {EQUIV / 'multicut_conservacion_scatter.png'}")

    with (EQUIV / "multicut_conservacion.csv").open("w", newline="",
                                                    encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(cons_rows[0].keys()))
        w.writeheader()
        for r in cons_rows:
            w.writerow(r)
    print(f"[multicut] -> {EQUIV / 'multicut_conservacion.csv'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
