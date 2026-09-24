"""Curva de escalado y LIMITE (por que NO tiende a infinito).

Para cada flota y eje (x/y) dibuja speedup medido vs nº de cortes y ajusta la
Ley de Escalabilidad Universal (USL):

    S(N) = N / (1 + alfa*(N-1) + beta*N*(N-1))

  - alfa (contencion): parte serie / reparto de carga.
  - beta (coherencia): coste de COMUNICACION/sincronizacion entre nodos.

La USL SUBE, alcanza un PICO en  N* = sqrt((1-alfa)/beta)  con speedup maximo
S_max, y luego BAJA: ese es el limite real (el overhead de comunicacion acaba
dominando). Se marca la recta ideal S=N como referencia.

Ajuste por minimos cuadrados lineales sobre  N/S - 1 = alfa*(N-1) + beta*N*(N-1).

    python -m SIMULATION.distributed.lan.plot_scaling_limit
"""
from __future__ import annotations

import csv
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
RES = ROOT / "SIMULATION" / "distributed" / "lan" / "results"
OUT = ROOT / "SIMULATION" / "distributed" / "lan" / "scaling"


def _collect():
    rows = {}
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
                except Exception:
                    continue
                key = (r.get("vehicles"), str(r.get("accident")).lower(), axis, ng)
                rows[key] = sp
        except Exception:
            pass
    return rows


def _fit_usl(ns, ss):
    """Ajusta alfa,beta por LSQ lineal. Devuelve (alfa,beta,Nstar,Smax)."""
    ns = np.asarray(ns, float)
    ss = np.asarray(ss, float)
    y = ns / ss - 1.0
    A = np.column_stack([(ns - 1.0), ns * (ns - 1.0)])
    (alfa, beta), *_ = np.linalg.lstsq(A, y, rcond=None)
    if beta > 1e-9 and alfa < 1:
        nstar = float(np.sqrt((1.0 - alfa) / beta))
        smax = nstar / (1 + alfa * (nstar - 1) + beta * nstar * (nstar - 1))
    else:
        nstar, smax = float("inf"), float("inf")
    return float(alfa), float(beta), nstar, smax


def main() -> int:
    rows = _collect()
    OUT.mkdir(parents=True, exist_ok=True)

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fleets = ["15000", "20000", "30000"]
    accs = ["false", "true"]
    axis_color = {"x": "#1f77b4", "y": "#d1004b"}

    fig, axes = plt.subplots(len(accs), len(fleets),
                             figsize=(16, 9), squeeze=False)
    summ = []
    for i, acc in enumerate(accs):
        for j, veh in enumerate(fleets):
            ax = axes[i][j]
            kmax = 2
            ymax = 2.0
            for axis in ("x", "y"):
                pts = sorted((ng, sp) for (v, a, ax_, ng), sp in rows.items()
                             if v == veh and a == acc and ax_ == axis)
                if len(pts) < 2:
                    continue
                col = axis_color[axis]
                # separar puntos superlineales (sp > nº cortes) = sospechosos
                clean = [(n, s) for n, s in pts if s <= n * 1.02]
                susp = [(n, s) for n, s in pts if s > n * 1.02]
                kmax = max(kmax, max(n for n, _ in pts))
                ymax = max(ymax, max(s for _, s in clean) if clean else 0)
                ax.plot([n for n, _ in clean], [s for _, s in clean], "o",
                        color=col, ms=7, label=f"medido eje {axis}")
                if susp:
                    ax.plot([n for n, _ in susp], [s for _, s in susp], "o",
                            mfc="none", mec=col, mew=2, ms=11)
                    for n, s in susp:
                        ax.annotate("superlineal\n(¿pierde veh.?)", (n, s),
                                    fontsize=6, color=col, ha="right",
                                    va="top", xytext=(-4, -4),
                                    textcoords="offset points")
                # USL ajustada SOLO con puntos limpios (>=3)
                if len(clean) >= 3:
                    ns = [n for n, _ in clean]
                    ss = [s for _, s in clean]
                    alfa, beta, nstar, smax = _fit_usl(ns, ss)
                    amdahl = (1.0 / alfa) if alfa > 1e-6 else float("inf")
                    xx = np.linspace(1, max(ns) + 3, 200)
                    yy = xx / (1 + alfa * (xx - 1) + beta * xx * (xx - 1))
                    ax.plot(xx, yy, "-", color=col, lw=1.6, alpha=0.8)
                    peak_ok = beta > 1e-7 and np.isfinite(nstar) and alfa < 1
                    if peak_ok:
                        ax.plot([nstar], [smax], "*", color=col, ms=15)
                    ax.axhline(amdahl if np.isfinite(amdahl) else 0,
                               color=col, ls=":", lw=1, alpha=0.5)
                    summ.append((veh, acc, axis, round(alfa, 4), round(beta, 5),
                                 round(nstar, 1) if peak_ok else "",
                                 round(smax, 2) if peak_ok else "",
                                 round(amdahl, 2) if np.isfinite(amdahl) else "inf"))
            xi = np.linspace(1, kmax, 50)
            ax.plot(xi, xi, "--", color="#888", lw=1, label="ideal S=N")
            ax.set_ylim(0, ymax * 1.25)
            ax.set_title(f"{int(veh)//1000}k veh · {'con' if acc=='true' else 'sin'} "
                         f"accidente", fontsize=10)
            ax.set_xlabel("nº de cortes (nodos)")
            ax.set_ylabel("speedup")
            ax.grid(alpha=0.25)
            ax.legend(fontsize=7)
    fig.suptitle("Límite de escalado (USL): speedup vs cortes — Rotterdam TCP\n"
                 "línea=USL(ajuste sin outliers) · ★=pico(N*,S_max) · "
                 "···=techo Amdahl 1/α · o hueco=superlineal sospechoso · --ideal",
                 fontsize=12)
    fig.tight_layout(rect=[0, 0, 1, 0.95])
    fig.savefig(OUT / "scaling_limit_usl.png", dpi=140)
    plt.close(fig)
    print(f"[scaling] -> {OUT / 'scaling_limit_usl.png'}")

    with (OUT / "scaling_usl_fit.csv").open("w", newline="",
                                            encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["vehicles", "accident", "axis", "alfa_contencion",
                    "beta_comunicacion", "N_pico", "Speedup_max",
                    "techo_amdahl_1_alfa"])
        w.writerows(summ)
    print(f"[scaling] -> {OUT / 'scaling_usl_fit.csv'}")
    for s in summ:
        print("  ", s)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
