"""Graficas de JUSTIFICACION adicionales para el paper (Rotterdam TCP).

Usa benchmark.csv (wall_sec, barrier_total_sec, speedup, num_groups...):

  1) karp_flatt.png   -> metrica de Karp-Flatt  e(N)=(1/S - 1/N)/(1 - 1/N).
     Si e es ~constante al crecer N -> el limite es la PARTE SERIE (Amdahl).
     Si e CRECE con N -> el limite es el OVERHEAD (comunicacion/sincronizacion).
     Distingue la CAUSA de la perdida de eficiencia. Justifica el techo.

  2) descomposicion_tiempo.png -> barras apiladas computo vs barrera (absoluto):
     wall = computo + sincronizacion. Ve a donde va el tiempo al subir N.

  3) tiempo_absoluto.png -> wall_sec vs N (reduccion REAL de tiempo de pared,
     no solo speedup relativo) con la recta ideal T1/N de referencia.

    python -m SIMULATION.distributed.lan.plot_more_justif
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
BASE_RT = {("15000", False): 18143.0, ("15000", True): 17916.8,
           ("20000", False): 23569.5, ("20000", True): 23512.0,
           ("30000", False): 34425.3, ("30000", True): 34149.2}


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
                rows[(str(r.get("vehicles")), acc, axis, ng)] = {
                    "wall": _f(r.get("wall_sec")),
                    "barr": _f(r.get("barrier_total_sec")),
                    "speedup": _f(r.get("speedup")),
                }
        except Exception:
            pass
    return rows


def _ser(rows, fleet, acc, axis, field):
    ns, ys = [], []
    for n in CUTS:
        d = rows.get((fleet, acc, axis, n))
        if d and d.get(field):
            ns.append(n)
            ys.append(d[field])
    return ns, ys


def fig_karp_flatt(rows, plt):
    fig, axes = plt.subplots(1, 2, figsize=(14, 6), sharey=True)
    for j, axis in enumerate(("x", "y")):
        ax = axes[j]
        for fleet in FLEETS:
            for acc, ls in ((False, "-"), (True, "--")):
                ns, ss = _ser(rows, fleet, acc, axis, "speedup")
                if len(ns) < 2:
                    continue
                e = [(1.0 / s - 1.0 / n) / (1.0 - 1.0 / n)
                     for s, n in zip(ss, ns)]
                ax.plot(ns, e, ls, marker="o", lw=1.6, ms=5,
                        label=f"{FLEET_LBL[fleet]} {'acc' if acc else 'noacc'}")
        ax.set_title(f"eje {axis.upper()}")
        ax.set_xlabel("nº de cortes (N)")
        ax.set_xticks(CUTS)
        ax.grid(alpha=0.25)
        if j == 0:
            ax.set_ylabel("fracción serie experimental  e(N)  (Karp-Flatt)")
        ax.legend(fontsize=8)
    fig.suptitle("Métrica de Karp-Flatt: ¿el límite es la parte serie o la "
                 "comunicación?\n(e≈constante ⇒ Amdahl/serie · e crece ⇒ "
                 "overhead de sincronización)", fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig(OUT / "karp_flatt.png", dpi=140)
    plt.close(fig)


def fig_decomp(rows, plt):
    fig, axes = plt.subplots(1, 2, figsize=(14, 6), sharey=True)
    fleet, acc = "20000", False
    for j, axis in enumerate(("x", "y")):
        ax = axes[j]
        ns, walls, barrs = [], [], []
        for n in CUTS:
            d = rows.get((fleet, acc, axis, n))
            if d and d["wall"]:
                ns.append(n)
                walls.append(d["wall"])
                barrs.append(d["barr"])
        comp = [w - b for w, b in zip(walls, barrs)]
        xpos = np.arange(len(ns))
        ax.bar(xpos, comp, 0.6, label="cómputo (simulación)", color="#2c7fb8")
        ax.bar(xpos, barrs, 0.6, bottom=comp,
               label="sincronización (barrera)", color="#d95f0e")
        for i, (c, b) in enumerate(zip(comp, barrs)):
            tot = c + b
            ax.text(i, tot, f"{b / tot * 100:.0f}%", ha="center",
                    va="bottom", fontsize=8)
        ax.set_xticks(xpos)
        ax.set_xticklabels([str(n) for n in ns])
        ax.set_title(f"eje {axis.upper()}")
        ax.set_xlabel("nº de cortes (N)")
        if j == 0:
            ax.set_ylabel("tiempo de pared (s)")
        ax.grid(alpha=0.2, axis="y")
        ax.legend(fontsize=9)
    fig.suptitle("Descomposición del tiempo de pared — 20k veh sin accidente\n"
                 "(% = fracción en sincronización)", fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig.savefig(OUT / "descomposicion_tiempo.png", dpi=140)
    plt.close(fig)


def fig_abs(rows, plt):
    fig, axes = plt.subplots(1, 2, figsize=(14, 6), sharey=True)
    for j, axis in enumerate(("x", "y")):
        ax = axes[j]
        for fleet in FLEETS:
            ns, walls = _ser(rows, fleet, False, axis, "wall")
            if not ns:
                continue
            p = ax.plot(ns, [w / 60 for w in walls], "o-", lw=1.8, ms=6,
                        label=f"{FLEET_LBL[fleet]} medido")[0]
            t1 = BASE_RT[(fleet, False)]
            ax.plot(CUTS, [t1 / n / 60 for n in CUTS], ":", lw=1.1,
                    color=p.get_color(), alpha=0.7,
                    label=f"{FLEET_LBL[fleet]} ideal T1/N")
        ax.set_title(f"eje {axis.upper()}")
        ax.set_xlabel("nº de cortes (N)")
        ax.set_xticks(CUTS)
        ax.grid(alpha=0.25)
        if j == 0:
            ax.set_ylabel("tiempo de pared (min)")
        ax.legend(fontsize=8)
    fig.suptitle("Reducción REAL del tiempo de simulación (sin accidente)\n"
                 "medido vs ideal T1/N", fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig.savefig(OUT / "tiempo_absoluto.png", dpi=140)
    plt.close(fig)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    rows = collect()
    print(f"[justif] {len(rows)} runs recogidos")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig_karp_flatt(rows, plt)
    fig_decomp(rows, plt)
    fig_abs(rows, plt)
    print(f"[justif] figuras -> {OUT}")
    for n in ("karp_flatt.png", "descomposicion_tiempo.png",
              "tiempo_absoluto.png"):
        print("   ", n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
