"""Justificaciones que CIERRAN el circulo balanceo <-> comunicacion <-> speedup.

Lee benchmark.csv (rotterdam tcp aggregated) y, para cada run, el
aggregate_summary.json de su carpeta aggregated/<label>/ para sacar el
trip_count por grupo -> DESBALANCEO de carga I = max_grupo / media_grupo.

Salida en scaling/analysis/:
  1) desbalanceo_vs_cortes.png  -> I(N) por eje/flota: la calidad de la particion.
  2) desbalanceo_vs_barrera.png -> scatter I vs % barrera: MAS desbalanceo =>
     los nodos rapidos esperan al lento => MAS tiempo de sincronizacion (causa).
  3) escalado_debil.png         -> speedup vs tamano de flota a N fijo (Gustafson):
     problemas mas grandes amortizan mejor el overhead => escalan mejor.

    python -m SIMULATION.distributed.lan.plot_loadbalance
"""
from __future__ import annotations

import csv
import json
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
    """(flota, acc, eje, N) -> {imbalance, barr_frac, speedup}."""
    rows: dict = {}
    for bench in RES.rglob("benchmark.csv"):
        try:
            reader = list(csv.DictReader(bench.open(newline="",
                                                    encoding="utf-8")))
        except Exception:
            continue
        for r in reader:
            if ("rotterdam" not in str(r.get("road", "")) or
                    r.get("transport") != "tcp" or
                    r.get("status") != "aggregated" or not r.get("speedup")):
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
            acc = str(r.get("accident")).strip().lower() == "true"
            label = str(r.get("label", ""))
            wall = _f(r.get("wall_sec"))
            barr = _f(r.get("barrier_total_sec"))
            imb = np.nan
            agg = bench.parent / "aggregated" / label / "aggregate_summary.json"
            if agg.is_file():
                try:
                    d = json.loads(agg.read_text(encoding="utf-8"))
                    tc = [s.get("trip_count", 0) for s in d.get("sub_jobs", [])]
                    tc = [t for t in tc if t > 0]
                    if tc:
                        imb = max(tc) / (sum(tc) / len(tc))
                except Exception:
                    pass
            rows[(str(r.get("vehicles")), acc, axis, ng)] = {
                "imbalance": imb,
                "barr_frac": (barr / wall) if wall else np.nan,
                "speedup": _f(r.get("speedup")),
            }
    return rows


def fig_imbalance_cuts(rows, plt):
    fig, axes = plt.subplots(1, 2, figsize=(14, 6), sharey=True)
    for j, axis in enumerate(("x", "y")):
        ax = axes[j]
        for fleet in FLEETS:
            for acc, ls in ((False, "-"), (True, "--")):
                ns, ys = [], []
                for n in CUTS:
                    d = rows.get((fleet, acc, axis, n))
                    if d and not np.isnan(d["imbalance"]):
                        ns.append(n)
                        ys.append(d["imbalance"])
                if ns:
                    ax.plot(ns, ys, ls, marker="o", lw=1.6, ms=5,
                            label=f"{FLEET_LBL[fleet]} {'acc' if acc else 'noacc'}")
        ax.axhline(1.0, color="#2a2", ls=":", lw=1.2, label="balance perfecto")
        ax.set_title(f"eje {axis.upper()}")
        ax.set_xlabel("nº de cortes (N)")
        ax.set_xticks(CUTS)
        ax.grid(alpha=0.25)
        if j == 0:
            ax.set_ylabel("desbalanceo  I = carga_máx / carga_media")
        ax.legend(fontsize=8)
    fig.suptitle("Desbalanceo de carga por partición (I=1 es reparto perfecto)",
                 fontsize=13)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig(OUT / "desbalanceo_vs_cortes.png", dpi=140)
    plt.close(fig)


def fig_imbalance_barrier(rows, plt):
    from matplotlib.colors import BoundaryNorm, ListedColormap
    palette = ["#4575b4", "#74add1", "#abd9e9", "#fdae61", "#f46d43",
               "#d73027", "#a50026"]
    cmap = ListedColormap(palette)
    norm = BoundaryNorm([1.5, 2.5, 3.5, 5, 7, 9, 11, 13], cmap.N)
    fig, ax = plt.subplots(figsize=(10, 7))
    xs, ys = [], []
    sc = None
    for (fl, acc, axis, n), d in rows.items():
        if np.isnan(d["imbalance"]) or np.isnan(d["barr_frac"]):
            continue
        mk = "o" if axis == "x" else "^"
        sc = ax.scatter(d["imbalance"], d["barr_frac"] * 100, s=120, c=[n],
                        cmap=cmap, norm=norm, marker=mk, edgecolors="k",
                        linewidths=0.6, alpha=0.9)
        xs.append(d["imbalance"])
        ys.append(d["barr_frac"] * 100)
    if len(xs) > 2:
        m, b = np.polyfit(xs, ys, 1)
        xg = np.linspace(min(xs), max(xs), 50)
        r = np.corrcoef(xs, ys)[0, 1]
        ax.plot(xg, m * xg + b, "k--", lw=1.4,
                label=f"tendencia (r={r:.2f})")
    from matplotlib.lines import Line2D
    handles = [
        Line2D([0], [0], marker="o", color="w", markerfacecolor="#888",
               markeredgecolor="k", markersize=11, label="eje X"),
        Line2D([0], [0], marker="^", color="w", markerfacecolor="#888",
               markeredgecolor="k", markersize=11, label="eje Y"),
        Line2D([0], [0], color="k", ls="--", lw=1.4,
               label=f"tendencia (r={r:.2f})" if len(xs) > 2 else "tendencia"),
    ]
    ax.legend(handles=handles, fontsize=9, loc="upper left")
    if sc is not None:
        cbar = fig.colorbar(sc, ax=ax, ticks=[2, 3, 4, 6, 8, 10, 12])
        cbar.set_label("nº de cortes (N)")
    ax.set_xlabel("desbalanceo de carga  I = carga_máx / carga_media")
    ax.set_ylabel("tiempo en barrera / sincronización (%)")
    ax.set_title("La causa del coste de sincronización: desbalanceo de carga\n"
                 "(nodos rápidos esperan al más cargado en cada barrera)")
    ax.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(OUT / "desbalanceo_vs_barrera.png", dpi=140)
    plt.close(fig)


def fig_weak(rows, plt):
    fig, axes = plt.subplots(1, 2, figsize=(14, 6), sharey=True)
    fleets_num = [15, 20, 30]
    for j, axis in enumerate(("x", "y")):
        ax = axes[j]
        for n in CUTS:
            ys = []
            for fleet in FLEETS:
                d = rows.get((fleet, False, axis, n))
                ys.append(d["speedup"] if d else np.nan)
            if any(not np.isnan(y) for y in ys):
                ax.plot(fleets_num, ys, "o-", lw=1.6, ms=6, label=f"N={n}")
        ax.set_title(f"eje {axis.upper()}")
        ax.set_xlabel("tamaño de flota (miles de vehículos)")
        ax.set_xticks(fleets_num)
        ax.grid(alpha=0.25)
        if j == 0:
            ax.set_ylabel("speedup")
        ax.legend(fontsize=8, ncol=2)
    fig.suptitle("Escalado con el tamaño del problema (Gustafson) — sin accidente\n"
                 "¿aprovechan más los cortes altos cuando hay más vehículos?",
                 fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig.savefig(OUT / "escalado_debil.png", dpi=140)
    plt.close(fig)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    rows = collect()
    n_imb = sum(1 for d in rows.values() if not np.isnan(d["imbalance"]))
    print(f"[loadbal] {len(rows)} runs, {n_imb} con desbalanceo medido")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig_imbalance_cuts(rows, plt)
    fig_imbalance_barrier(rows, plt)
    fig_weak(rows, plt)
    print(f"[loadbal] figuras -> {OUT}")
    for n in ("desbalanceo_vs_cortes.png", "desbalanceo_vs_barrera.png",
              "escalado_debil.png"):
        print("   ", n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
