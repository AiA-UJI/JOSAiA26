"""Graficos de ANALISIS del barrido Rotterdam TCP (todas las celdas de la matriz
speedup: eje x/y x cortes {2,3,4,6,8,10,12} x flotas {15k,20k,30k} x acc {no,si}).

Lee todos los benchmark.csv y produce en scaling/analysis/:
  1) scaling_por_flota.png  -> speedup vs nº cortes, subplot por (flota,acc), x vs y.
  2) heatmap_speedup.png     -> matriz completa (particion x flota/acc).
  3) eficiencia_vs_cortes.png-> S/N (eficiencia) vs nº cortes: rendimientos decrec.
  4) impacto_accidente.png   -> speedup sin vs con accidente por particion.
  5) eje_x_vs_y.png          -> comparativa eje x frente a eje y por nº de cortes.
  6) speedup_matrix.csv      -> volcado tabular de todo lo dibujado.

    python -m SIMULATION.distributed.lan.plot_speedup_analysis
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


def collect() -> dict:
    """(flota, acc, eje, N) -> speedup (ultimo aggregated que aparezca)."""
    rows: dict = {}
    for f in RES.rglob("benchmark.csv"):
        try:
            for r in csv.DictReader(f.open(newline="", encoding="utf-8")):
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
                    sp = float(r["speedup"])
                except Exception:
                    continue
                if axis not in ("x", "y"):
                    continue
                acc = str(r.get("accident")).strip().lower() == "true"
                rows[(str(r.get("vehicles")), acc, axis, ng)] = sp
        except Exception:
            pass
    return rows


def _series(rows, fleet, acc, axis):
    ns, ss = [], []
    for n in CUTS:
        v = rows.get((fleet, acc, axis, n))
        if v is not None:
            ns.append(n)
            ss.append(v)
    return ns, ss


def fig_scaling(rows, plt):
    fig, axes = plt.subplots(3, 2, figsize=(13, 14), sharex=True)
    for i, fleet in enumerate(FLEETS):
        for j, acc in enumerate((False, True)):
            ax = axes[i][j]
            for axis, col in (("x", "#1f77b4"), ("y", "#d1004b")):
                ns, ss = _series(rows, fleet, acc, axis)
                if ns:
                    ax.plot(ns, ss, "o-", color=col, lw=2, ms=7,
                            label=f"eje {axis}")
            ax.plot(CUTS, CUTS, ":", color="#888", lw=1, label="ideal S=N")
            ax.set_title(f"{FLEET_LBL[fleet]} veh · "
                         f"{'con' if acc else 'sin'} accidente")
            ax.grid(alpha=0.25)
            ax.set_xticks(CUTS)
            if j == 0:
                ax.set_ylabel("speedup")
            if i == 2:
                ax.set_xlabel("nº de cortes (N)")
            ax.legend(fontsize=8)
    fig.suptitle("Escalado Rotterdam TCP — speedup vs nº de cortes", fontsize=14)
    fig.tight_layout(rect=(0, 0, 1, 0.98))
    fig.savefig(OUT / "scaling_por_flota.png", dpi=140)
    plt.close(fig)


def fig_heatmap(rows, plt):
    parts = [f"{ax}-{n}" for ax in ("x", "y") for n in CUTS]
    cols = [(fl, acc) for fl in FLEETS for acc in (False, True)]
    col_lbl = [f"{FLEET_LBL[fl]}{'A' if acc else 'N'}" for fl, acc in cols]
    M = np.full((len(parts), len(cols)), np.nan)
    for pi, p in enumerate(parts):
        ax, n = p.split("-")
        for ci, (fl, acc) in enumerate(cols):
            v = rows.get((fl, acc, ax, int(n)))
            if v is not None:
                M[pi, ci] = v
    fig, ax = plt.subplots(figsize=(9, 11))
    im = ax.imshow(M, aspect="auto", cmap="viridis")
    ax.set_xticks(range(len(cols)))
    ax.set_xticklabels(col_lbl, rotation=45, ha="right")
    ax.set_yticks(range(len(parts)))
    ax.set_yticklabels(parts)
    for pi in range(len(parts)):
        for ci in range(len(cols)):
            if not np.isnan(M[pi, ci]):
                ax.text(ci, pi, f"{M[pi, ci]:.1f}", ha="center", va="center",
                        color="white" if M[pi, ci] < np.nanmax(M) * 0.6 else "black",
                        fontsize=8)
    fig.colorbar(im, ax=ax, label="speedup")
    ax.set_title("Matriz de speedup Rotterdam TCP")
    fig.tight_layout()
    fig.savefig(OUT / "heatmap_speedup.png", dpi=140)
    plt.close(fig)


def fig_efficiency(rows, plt):
    fig, axes = plt.subplots(1, 2, figsize=(14, 6), sharey=True)
    for j, axis in enumerate(("x", "y")):
        ax = axes[j]
        for fleet in FLEETS:
            for acc, ls in ((False, "-"), (True, "--")):
                ns, ss = _series(rows, fleet, acc, axis)
                if not ns:
                    continue
                eff = [s / n for s, n in zip(ss, ns)]
                ax.plot(ns, eff, ls, marker="o", lw=1.6, ms=5,
                        label=f"{FLEET_LBL[fleet]} {'acc' if acc else 'noacc'}")
        ax.axhline(1.0, color="#888", ls=":", lw=1)
        ax.set_title(f"eje {axis}")
        ax.set_xlabel("nº de cortes (N)")
        ax.set_xticks(CUTS)
        ax.grid(alpha=0.25)
        if j == 0:
            ax.set_ylabel("eficiencia = speedup / N")
        ax.legend(fontsize=8)
    fig.suptitle("Eficiencia paralela (rendimientos decrecientes)", fontsize=13)
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    fig.savefig(OUT / "eficiencia_vs_cortes.png", dpi=140)
    plt.close(fig)


def fig_accident(rows, plt):
    parts = [(ax, n) for ax in ("x", "y") for n in CUTS]
    labels = [f"{ax}-{n}" for ax, n in parts]
    xpos = np.arange(len(parts))
    fig, axes = plt.subplots(3, 1, figsize=(13, 12), sharex=True)
    for i, fleet in enumerate(FLEETS):
        ax = axes[i]
        noacc = [rows.get((fleet, False, a, n), np.nan) for a, n in parts]
        acc = [rows.get((fleet, True, a, n), np.nan) for a, n in parts]
        ax.bar(xpos - 0.2, noacc, 0.4, label="sin accidente", color="#2c7fb8")
        ax.bar(xpos + 0.2, acc, 0.4, label="con accidente", color="#d95f0e")
        ax.set_title(f"{FLEET_LBL[fleet]} veh")
        ax.set_ylabel("speedup")
        ax.grid(alpha=0.2, axis="y")
        ax.legend(fontsize=8)
    axes[-1].set_xticks(xpos)
    axes[-1].set_xticklabels(labels, rotation=45, ha="right")
    fig.suptitle("Impacto del accidente en el speedup", fontsize=13)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    fig.savefig(OUT / "impacto_accidente.png", dpi=140)
    plt.close(fig)


def fig_axis(rows, plt):
    fig, ax = plt.subplots(figsize=(11, 6))
    width = 0.11
    offs = np.linspace(-2.5, 2.5, 6) * width
    combos = [(fl, acc) for fl in FLEETS for acc in (False, True)]
    for k, (fl, acc) in enumerate(combos):
        diffs, xs = [], []
        for xi, n in enumerate(CUTS):
            sx = rows.get((fl, acc, "x", n))
            sy = rows.get((fl, acc, "y", n))
            if sx is not None and sy is not None:
                diffs.append(sx - sy)
                xs.append(xi + offs[k])
        ax.bar(xs, diffs, width,
               label=f"{FLEET_LBL[fl]}{'A' if acc else 'N'}")
    ax.axhline(0, color="k", lw=0.8)
    ax.set_xticks(range(len(CUTS)))
    ax.set_xticklabels([str(n) for n in CUTS])
    ax.set_xlabel("nº de cortes (N)")
    ax.set_ylabel("speedup(x) − speedup(y)")
    ax.set_title("Ventaja del corte en X frente a Y (>0 gana X)")
    ax.grid(alpha=0.2, axis="y")
    ax.legend(fontsize=8, ncol=3)
    fig.tight_layout()
    fig.savefig(OUT / "eje_x_vs_y.png", dpi=140)
    plt.close(fig)


def dump_csv(rows):
    with (OUT / "speedup_matrix.csv").open("w", newline="",
                                           encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["eje", "cortes", "flota", "accidente", "speedup"])
        for (fl, acc, axis, n), sp in sorted(rows.items()):
            w.writerow([axis, n, fl, "si" if acc else "no", f"{sp:.4f}"])


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    rows = collect()
    print(f"[analysis] {len(rows)} celdas TCP Rotterdam recogidas")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig_scaling(rows, plt)
    fig_heatmap(rows, plt)
    fig_efficiency(rows, plt)
    fig_accident(rows, plt)
    fig_axis(rows, plt)
    dump_csv(rows)
    print(f"[analysis] figuras -> {OUT}")
    for p in sorted(OUT.glob("*.png")):
        print("   ", p.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
