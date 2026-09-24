"""% de tiempo en BARRERA por corte (Rotterdam TCP). Es la evidencia de que el
cuello de botella es el DESBALANCE de carga (el grupo mas lento manda), no N.

Para cada (corte, vehiculos, accidente) toma barrier_total_sec / wall_sec de los
benchmark.csv (status=aggregated) y dibuja barras por nº de cortes, coloreadas por
eje. Anota el speedup encima de cada barra.

    python -m SIMULATION.distributed.lan.plot_barrier_fraction [--veh 20000] [--acc false]
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RES = ROOT / "SIMULATION" / "distributed" / "lan" / "results"
OUT = ROOT / "SIMULATION" / "distributed" / "lan" / "scaling"


def _collect(veh: str, acc: str):
    best = {}
    for f in RES.rglob("benchmark.csv"):
        try:
            for r in csv.DictReader(f.open(newline="", encoding="utf-8")):
                if ("rotterdam" not in str(r.get("road", "")) or
                        r.get("transport") != "tcp" or
                        r.get("status") != "aggregated"):
                    continue
                if str(r.get("vehicles")) != veh:
                    continue
                if str(r.get("accident")).lower() != acc:
                    continue
                part = r.get("partition", "")
                if not part.startswith("balanced-"):
                    continue
                try:
                    wall = float(r.get("wall_sec") or 0)
                    barr = float(r.get("barrier_total_sec") or 0)
                    ng = int(part.split("-")[-1])
                    axis = part.split("-")[1]
                    sp = float(r.get("speedup") or 0)
                except Exception:
                    continue
                if wall <= 0:
                    continue
                frac = 100.0 * barr / wall
                k = part
                # quedarse con el de mayor speedup (mejor run)
                if k not in best or sp > best[k]["sp"]:
                    best[k] = {"part": part, "ng": ng, "axis": axis,
                               "frac": frac, "sp": sp}
        except Exception:
            pass
    return best


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--veh", default="20000")
    ap.add_argument("--acc", default="false")
    args = ap.parse_args(argv)

    best = _collect(args.veh, args.acc)
    if not best:
        print("[barrier] sin datos para esa combinacion")
        return 1
    OUT.mkdir(parents=True, exist_ok=True)

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    order = sorted(best.values(), key=lambda d: (d["axis"], d["ng"]))
    labels = [d["part"].replace("balanced-", "") for d in order]
    fracs = [d["frac"] for d in order]
    sps = [d["sp"] for d in order]
    colors = ["#1f77b4" if d["axis"] == "x" else "#d1004b" for d in order]

    fig, ax = plt.subplots(figsize=(max(8, 0.7 * len(order)), 5.2))
    bars = ax.bar(range(len(order)), fracs, color=colors, alpha=0.85,
                  edgecolor="#333", linewidth=0.6)
    for i, (b, sp) in enumerate(zip(bars, sps)):
        ax.text(i, b.get_height() + 1, f"S={sp:.1f}", ha="center",
                va="bottom", fontsize=8, fontweight="bold")
    ax.set_xticks(range(len(order)))
    ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=9)
    ax.set_ylabel("% del tiempo total en BARRERA de sincronización")
    ax.set_ylim(0, 100)
    ax.axhline(50, color="#888", ls="--", lw=1)
    from matplotlib.patches import Patch
    ax.legend(handles=[Patch(color="#1f77b4", label="corte eje X"),
                       Patch(color="#d1004b", label="corte eje Y")],
              fontsize=8)
    acc_txt = "con accidente" if args.acc == "true" else "sin accidente"
    ax.set_title(f"Fracción de tiempo en barrera por corte — Rotterdam TCP, "
                 f"{int(args.veh)//1000}k veh, {acc_txt}\n"
                 f"(más barrera = grupo cuello de botella = peor balance; "
                 f"S = speedup)")
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    outp = OUT / f"barrier_fraction_{args.veh}_{args.acc}.png"
    fig.savefig(outp, dpi=140)
    plt.close(fig)
    print(f"[barrier] -> {outp}")
    for d in order:
        print(f"  {d['part']:>16} barrera={d['frac']:.1f}%  speedup={d['sp']:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
