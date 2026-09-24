"""Predice el TECHO de speedup SIN simular, combinando tres modelos independientes
ajustados a los datos ya medidos (benchmark.csv + aggregate_summary.json):

  1) USL:  S(N)=N/(1+alfa*(N-1)+beta*N*(N-1))  -> pico (N*, S_max) y techo Amdahl 1/alfa.
  2) Cota por BALANCE (barrier-bound):  S_lb(N) = N / I(N), con I(N)=max_grupo/media
     (imbalance real medido); eficiencia maxima alcanzable = 1/I.
  3) Descomposicion de camino critico: wall = compute(N) + barrera(N). Se ajusta
     compute(N)=a*N^-p (congestion super-lineal, p>1 => super-lineal) y
     barrera(N)=b*(I(N)-1)*compute(N) para extrapolar wall(N) y S(N)=T1/wall(N).

Salida en scaling/: ceiling_prediction.png, ceiling_prediction.csv
    python -m SIMULATION.distributed.lan.predict_ceiling
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
RES = ROOT / "SIMULATION" / "distributed" / "lan" / "results"
EQUIV = ROOT / "SIMULATION" / "distributed" / "lan" / "equivalence"
OUT = ROOT / "SIMULATION" / "distributed" / "lan" / "scaling"


def collect_speedups():
    rows = {}
    base = {}
    for f in RES.rglob("benchmark.csv"):
        try:
            for r in csv.DictReader(f.open(newline="", encoding="utf-8")):
                if ("rotterdam" not in str(r.get("road", "")) or
                        r.get("transport") != "tcp" or
                        r.get("status") != "aggregated" or not r.get("speedup")):
                    continue
                try:
                    ng = int(r["partition"].split("-")[-1])
                    axis = r["partition"].split("-")[1]
                    sp = float(r["speedup"])
                    wall = float(r.get("wall_sec") or 0)
                    barr = float(r.get("barrier_total_sec") or 0)
                except Exception:
                    continue
                key = (str(r.get("vehicles")), str(r.get("accident")).lower(), axis)
                rows.setdefault(key, {})[ng] = {"sp": sp, "wall": wall, "barr": barr}
        except Exception:
            pass
    return rows


def collect_imbalance():
    """axis -> {N: imbalance} usando trip_count por grupo (equivalence)."""
    out = {}
    for p in EQUIV.glob("*/distributed/aggregate_summary.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            meta = json.loads((p.parent.parent / "meta.json").read_text("utf-8"))
        except Exception:
            continue
        part = meta.get("partition", "")
        if "-" not in part:
            continue
        axis = part.split("-")[1]
        tc = [s.get("trip_count", 0) for s in d.get("sub_jobs", [])]
        if not tc:
            continue
        N = len(tc)
        mean = sum(tc) / N
        if mean <= 0:
            continue
        out.setdefault(axis, {})[N] = max(tc) / mean
    return out


def fit_usl(ns, ss):
    ns = np.asarray(ns, float)
    ss = np.asarray(ss, float)
    y = ns / ss - 1.0
    A = np.column_stack([(ns - 1.0), ns * (ns - 1.0)])
    (alfa, beta), *_ = np.linalg.lstsq(A, y, rcond=None)
    return float(alfa), float(beta)


def usl(N, alfa, beta):
    return N / (1 + alfa * (N - 1) + beta * N * (N - 1))


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    rows = collect_speedups()
    imb = collect_imbalance()

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # imbalance asintotica por eje (media de lo medido) para extrapolar la cota
    I_inf = {ax: (sum(v.values()) / len(v)) for ax, v in imb.items() if v}
    print("[ceiling] imbalance media por eje:", {k: round(v, 3)
                                                 for k, v in I_inf.items()})

    summary = []
    # figura principal: 20k sin accidente (mejor cobertura), ejes x e y
    veh, acc = "20000", "false"
    fig, ax = plt.subplots(figsize=(11, 7))
    Ngrid = np.linspace(1, 32, 300)
    colors = {"x": "#1f77b4", "y": "#d1004b"}
    for axis in ("x", "y"):
        d = rows.get((veh, acc, axis), {})
        pts = sorted((n, v["sp"]) for n, v in d.items())
        if len(pts) < 2:
            continue
        ns = [n for n, _ in pts]
        ss = [s for _, s in pts]
        clean = [(n, s) for n, s in pts if s <= n * 1.02]
        col = colors[axis]
        ax.plot(ns, ss, "o", color=col, ms=8, label=f"medido eje {axis}")
        # marca superlineales
        for n, s in pts:
            if s > n * 1.02:
                ax.plot([n], [s], "o", mfc="none", mec=col, mew=2, ms=13)

        # --- modelo 1: USL (ajuste sin superlineales) ---
        cn = [n for n, _ in clean] or ns
        cs = [s for _, s in clean] or ss
        alfa, beta = fit_usl(cn, cs)
        yy = usl(Ngrid, alfa, beta)
        ax.plot(Ngrid, yy, "-", color=col, lw=1.8, alpha=0.85,
                label=f"USL eje {axis}")
        if beta > 1e-7 and alfa < 1:
            nstar = float(np.sqrt((1 - alfa) / beta))
            smax = usl(nstar, alfa, beta)
            ax.plot([nstar], [smax], "*", color=col, ms=18)
            ax.annotate(f"tope USL\nN*={nstar:.0f}, S={smax:.1f}",
                        (nstar, smax), color=col, fontsize=8,
                        xytext=(6, -2), textcoords="offset points")
        else:
            nstar, smax = float("inf"), float("inf")
        amdahl = 1 / alfa if alfa > 1e-6 else float("inf")

        # --- modelo 2: cota por balance (barrier-bound) ---
        Iax = I_inf.get(axis, 1.35)
        ax.plot(Ngrid, Ngrid / Iax, "--", color=col, lw=1.1, alpha=0.55,
                label=f"cota balance N/{Iax:.2f} (ef≤{100/Iax:.0f}%)")

        summary.append({
            "vehiculos": veh, "accidente": acc, "eje": axis,
            "alfa_contencion": round(alfa, 4), "beta_comunicacion": round(beta, 5),
            "N_pico_USL": round(nstar, 1) if np.isfinite(nstar) else "inf",
            "Smax_USL": round(smax, 2) if np.isfinite(smax) else "inf",
            "techo_amdahl_1/alfa": round(amdahl, 1) if np.isfinite(amdahl) else "inf",
            "imbalance_medio": round(Iax, 3),
            "eficiencia_max_balance_%": round(100 / Iax, 1),
        })

    ax.plot(Ngrid, Ngrid, ":", color="#888", lw=1, label="ideal S=N")
    ax.set_xlim(0, 32)
    ax.set_ylim(0, 22)
    ax.set_xlabel("nº de cortes / nodos (N)")
    ax.set_ylabel("speedup S(N)")
    ax.set_title("Predicción del techo SIN simular — Rotterdam TCP, 20k, sin accidente\n"
                 "USL (comunicación) + cota por balance (N/I) + Amdahl (1/α)")
    ax.grid(alpha=0.25)
    ax.legend(fontsize=8, ncol=2)
    fig.tight_layout()
    fig.savefig(OUT / "ceiling_prediction.png", dpi=140)
    plt.close(fig)
    print(f"[ceiling] -> {OUT / 'ceiling_prediction.png'}")

    with (OUT / "ceiling_prediction.csv").open("w", newline="",
                                               encoding="utf-8") as f:
        if summary:
            w = csv.DictWriter(f, fieldnames=list(summary[0].keys()))
            w.writeheader()
            w.writerows(summary)
    print(f"[ceiling] -> {OUT / 'ceiling_prediction.csv'}")
    for s in summary:
        print("  ", s)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
