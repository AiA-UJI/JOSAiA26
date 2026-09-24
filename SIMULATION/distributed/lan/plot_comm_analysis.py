"""Graficos de COMUNICACIONES del barrido Rotterdam TCP.

Metricas por (flota, acc, eje, N) leidas de benchmark.csv:
  - barrier_total_sec / wall_sec  -> fraccion de tiempo en SINCRONIZACION.
  - barrier_total_sec / barriers  -> coste medio por barrera (ms/paso).
  - handoffs_out                  -> vehiculos transferidos entre maquinas (volumen).
  - handoffs_out / merged_trips   -> handoffs por vehiculo (intensidad de cruce).
  - barrier_timeouts, handoff_failures -> FIABILIDAD del transporte.

Salida en scaling/analysis/:
  comm_barrera_fraccion.png, comm_barrera_ms.png, comm_handoffs.png,
  comm_handoffs_por_vehiculo.png, comm_barrera_vs_speedup.png,
  comm_fiabilidad.png, comm_metrics.csv

    python -m SIMULATION.distributed.lan.plot_comm_analysis
"""
from __future__ import annotations

import csv
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
RES = ROOT / "SIMULATION" / "distributed" / "lan" / "results"
OUT = ROOT / "SIMULATION" / "distributed" / "lan" / "scaling" / "analysis"

CUTS = [2, 3, 4, 6, 8, 10, 12]
FLEETS = ["15000", "20000", "30000"]
FLEET_LBL = {"15000": "15k", "20000": "20k", "30000": "30k"}


def _f(x, d=0.0):
    try:
        return float(x)
    except Exception:
        return d


def collect() -> dict:
    rows: dict = {}
    for f in RES.rglob("benchmark.csv"):
        try:
            for r in csv.DictReader(f.open(newline="", encoding="utf-8")):
                if ("rotterdam" not in str(r.get("road", "")) or
                        r.get("transport") != "tcp" or
                        r.get("status") != "aggregated"):
                    continue
                part = str(r.get("partition", ""))
                if not part.startswith("balanced-"):
                    continue
                try:
                    axis = part.split("-")[1]
                    ng = int(part.split("-")[-1])
                except Exception:
                    continue
                if axis not in ("x", "y"):
                    continue
                wall = _f(r.get("wall_sec"))
                barr = _f(r.get("barrier_total_sec"))
                barriers = _f(r.get("barriers"))
                ho = _f(r.get("handoffs_out"))
                trips = _f(r.get("merged_trips"))
                acc = str(r.get("accident")).strip().lower() == "true"
                rows[(str(r.get("vehicles")), acc, axis, ng)] = {
                    "wall": wall, "barr": barr, "barriers": barriers,
                    "handoffs": ho, "trips": trips,
                    "speedup": _f(r.get("speedup")),
                    "barr_frac": (barr / wall) if wall else np.nan,
                    "barr_ms": (barr / barriers * 1000) if barriers else np.nan,
                    "ho_per_veh": (ho / trips) if trips else np.nan,
                    "timeouts": _f(r.get("barrier_timeouts")),
                    "failures": _f(r.get("handoff_failures")),
                }
        except Exception:
            pass
    return rows


def _ser(rows, fleet, acc, axis, field):
    ns, ys = [], []
    for n in CUTS:
        d = rows.get((fleet, acc, axis, n))
        if d and not (isinstance(d[field], float) and np.isnan(d[field])):
            ns.append(n)
            ys.append(d[field])
    return ns, ys


def _two_axis_fig(rows, plt, field, title, ylabel, fname, pct=False):
    fig, axes = plt.subplots(1, 2, figsize=(14, 6), sharey=True)
    for j, axis in enumerate(("x", "y")):
        ax = axes[j]
        for fleet in FLEETS:
            for acc, ls in ((False, "-"), (True, "--")):
                ns, ys = _ser(rows, fleet, acc, axis, field)
                if not ns:
                    continue
                if pct:
                    ys = [y * 100 for y in ys]
                ax.plot(ns, ys, ls, marker="o", lw=1.6, ms=5,
                        label=f"{FLEET_LBL[fleet]} {'acc' if acc else 'noacc'}")
        ax.set_title(f"eje {axis}")
        ax.set_xlabel("nº de cortes (N)")
        ax.set_xticks(CUTS)
        ax.grid(alpha=0.25)
        if j == 0:
            ax.set_ylabel(ylabel)
        ax.legend(fontsize=8)
    fig.suptitle(title, fontsize=13)
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    fig.savefig(OUT / fname, dpi=140)
    plt.close(fig)


def fig_scatter(rows, plt):
    from matplotlib.lines import Line2D
    from matplotlib.colors import BoundaryNorm, ListedColormap

    fig, axes = plt.subplots(1, 2, figsize=(15, 7), sharex=True, sharey=True)
    # color = nº de cortes (discreto, mucho mas legible que el tamaño)
    palette = ["#4575b4", "#74add1", "#abd9e9", "#fdae61", "#f46d43",
               "#d73027", "#a50026"]
    cmap = ListedColormap(palette)
    norm = BoundaryNorm([1.5, 2.5, 3.5, 5, 7, 9, 11, 13], cmap.N)
    mk = {False: "o", True: "^"}                  # forma = accidente

    pts = [(k, d) for k, d in rows.items()
           if not np.isnan(d["barr_frac"]) and d["speedup"]]

    sc = None
    for j, axis in enumerate(("x", "y")):
        ax = axes[j]
        for (fl, acc, ax_, n), d in pts:
            if ax_ != axis:
                continue
            sc = ax.scatter(d["barr_frac"] * 100, d["speedup"], s=130,
                            c=[n], cmap=cmap, norm=norm, marker=mk[acc],
                            edgecolors="k", linewidths=0.6, alpha=0.9)
        # tendencia por eje
        xs = np.array([d["barr_frac"] * 100 for (fl, acc, a, n), d in pts
                       if a == axis])
        ys = np.array([d["speedup"] for (fl, acc, a, n), d in pts if a == axis])
        if len(xs) > 2:
            m, b = np.polyfit(xs, ys, 1)
            xg = np.linspace(xs.min(), xs.max(), 50)
            r = np.corrcoef(xs, ys)[0, 1]
            ax.plot(xg, m * xg + b, "k--", lw=1.4,
                    label=f"tendencia (r={r:.2f})")
        ax.set_title(f"eje {axis.upper()} "
                     f"({'corte vertical' if axis == 'x' else 'corte horizontal'})")
        ax.set_xlabel("fracción de tiempo en barrera / sincronización (%)")
        if j == 0:
            ax.set_ylabel("speedup")
        ax.grid(alpha=0.25)
        shape_leg = [
            Line2D([0], [0], marker="o", color="w", markerfacecolor="#888",
                   markeredgecolor="k", markersize=11, label="sin accidente"),
            Line2D([0], [0], marker="^", color="w", markerfacecolor="#888",
                   markeredgecolor="k", markersize=11, label="con accidente"),
            Line2D([0], [0], color="k", ls="--", lw=1.4,
                   label=f"tendencia (r={r:.2f})"),
        ]
        ax.legend(handles=shape_leg, loc="upper left", fontsize=9,
                  framealpha=0.95)

    cbar = fig.colorbar(sc, ax=axes, ticks=[2, 3, 4, 6, 8, 10, 12],
                        fraction=0.04, pad=0.02)
    cbar.set_label("nº de cortes (N)")
    fig.suptitle("Coste de comunicación vs speedup en Rotterdam (TCP)  ·  "
                 "color = nº de cortes · forma = escenario", fontsize=13)
    fig.savefig(OUT / "comm_barrera_vs_speedup.png", dpi=140,
                bbox_inches="tight")
    plt.close(fig)


def fig_reliability(rows, plt):
    tot_to = sum(d["timeouts"] for d in rows.values())
    tot_fail = sum(d["failures"] for d in rows.values())
    tot_ho = sum(d["handoffs"] for d in rows.values())
    fig, ax = plt.subplots(figsize=(9, 5.5))
    bars = ["handoffs totales", "handoff_failures", "barrier_timeouts"]
    vals = [tot_ho, tot_fail, tot_to]
    colors = ["#2c7fb8", "#d95f0e", "#c00"]
    b = ax.bar(bars, vals, color=colors)
    ax.set_yscale("symlog")
    ax.set_ylabel("recuento (escala symlog)")
    ax.set_title(f"Fiabilidad del transporte TCP — {len(rows)} runs\n"
                 f"{tot_ho:,.0f} handoffs, {tot_fail:.0f} fallos, "
                 f"{tot_to:.0f} timeouts")
    for rect, v in zip(b, vals):
        ax.text(rect.get_x() + rect.get_width() / 2, v,
                f"{v:,.0f}", ha="center", va="bottom", fontsize=10)
    ax.grid(alpha=0.2, axis="y")
    fig.tight_layout()
    fig.savefig(OUT / "comm_fiabilidad.png", dpi=140)
    plt.close(fig)


def dump_csv(rows):
    with (OUT / "comm_metrics.csv").open("w", newline="",
                                         encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["eje", "cortes", "flota", "accidente", "wall_sec",
                    "barrier_sec", "barrier_frac_%", "barrier_ms_paso",
                    "handoffs", "handoffs_por_veh", "speedup",
                    "timeouts", "failures"])
        for (fl, acc, axis, n), d in sorted(rows.items()):
            w.writerow([axis, n, fl, "si" if acc else "no",
                        f"{d['wall']:.1f}", f"{d['barr']:.1f}",
                        f"{d['barr_frac'] * 100:.1f}", f"{d['barr_ms']:.2f}",
                        f"{d['handoffs']:.0f}", f"{d['ho_per_veh']:.2f}",
                        f"{d['speedup']:.3f}", f"{d['timeouts']:.0f}",
                        f"{d['failures']:.0f}"])


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    rows = collect()
    print(f"[comm] {len(rows)} runs TCP Rotterdam con métricas de comunicación")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    _two_axis_fig(rows, plt, "barr_frac",
                  "Comunicación: % de tiempo en barrera de sincronización",
                  "tiempo en barrera (%)", "comm_barrera_fraccion.png", pct=True)
    _two_axis_fig(rows, plt, "barr_ms",
                  "Coste medio por barrera (sincronización por paso)",
                  "ms por barrera", "comm_barrera_ms.png")
    _two_axis_fig(rows, plt, "handoffs",
                  "Volumen de comunicación: vehículos transferidos (handoffs)",
                  "handoffs totales", "comm_handoffs.png")
    _two_axis_fig(rows, plt, "ho_per_veh",
                  "Intensidad de cruce: handoffs por vehículo",
                  "handoffs / vehículo", "comm_handoffs_por_vehiculo.png")
    fig_scatter(rows, plt)
    fig_reliability(rows, plt)
    dump_csv(rows)
    print(f"[comm] figuras -> {OUT}")
    for p in sorted(OUT.glob("comm_*.png")):
        print("   ", p.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
