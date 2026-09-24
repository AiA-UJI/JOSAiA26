"""Comparacion 1 nodo vs 6 cortes en aristas-NEXO usando el edgeData CRUDO de
SUMO (entered/left/flow), no la ocupacion. Metricas REALES:

  - conteo = vehiculos que ENTRAN en la arista por intervalo (5 min).
  - flujo  = atributo 'flow' de SUMO (veh/h) = entered*3600/periodo.

Lee de equivalence/<run>/:
  baseline/environment_traffic.edgedata.xml
  distributed/workers/*.edgedata.xml   (6 workers; se fusionan por edge)

Genera:
  nexus_ts_flujo.png, nexus_ts_conteo.png (rejilla 2 col, X=hora)
  nexus_edgedata_compare.csv, nexus_edgedata_summary.json, nexus_edgedata_scatter.png

    python -m SIMULATION.distributed.lan.plot_nexus_edgedata --run <ts> [--top 6]
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from SIMULATION.distributed.lan.find_nexus_edges import find_nexus  # noqa: E402

EQUIV = PROJECT_ROOT / "SIMULATION" / "distributed" / "lan" / "equivalence"

# Periodo del edgeData (s). El flujo REAL se calcula como entered*3600/PERIOD.
# NO se usa el atributo 'flow' de SUMO: en las aristas de handoff los vehiculos
# se vaporizan al entrar (sampledSeconds truncado) y SUMO hunde su 'flow' aunque
# el conteo de vehiculos que cruzan (entered/left) sea correcto.
PERIOD = 300.0

# Misma paleta que SIMULATION/roads/_plot_rot_partitions.py (color del corte).
PALETTE = [
    "#e6194B", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#42d4f4",
    "#f032e6", "#bfef45", "#fabed4", "#469990",
]

# La simulacion arranca a esta hora de reloj; eje X con marcas cada 15 min.
START_H, START_M = 18, 50
TICK_MIN = 15


def _clock_ticks(max_hours: float):
    """Devuelve (posiciones_en_horas, etiquetas HH:MM) cada TICK_MIN minutos."""
    base = START_H * 60 + START_M
    ticks, labels = [], []
    m = 0
    while m <= max_hours * 60 + 1e-6:
        ticks.append(m / 60.0)
        tot = (base + m) % (24 * 60)
        labels.append(f"{tot // 60:02d}:{tot % 60:02d}")
        m += TICK_MIN
    return ticks, labels


def host_to_pc(fname: str) -> str:
    """labrobNN_*.edgedata.xml -> 'PC{NN-6}' (labrob07->PC1 ... labrob12->PC6)."""
    import re
    m = re.search(r"labrob(\d+)", fname)
    if not m:
        return "PC?"
    return f"PC{int(m.group(1)) - 6}"


def group_to_pc_map(wfiles, owner: dict, keep: set) -> dict:
    """Deduce que grupo ejecuta cada PC: en cada fichero de worker, el grupo con
    mas 'entered' sumado sobre sus aristas propietarias es el grupo de ese PC."""
    g2pc = {}
    for f in wfiles:
        d = parse_edgedata(f, keep)
        per_g = {}
        for eid, series_ in d.items():
            g = owner.get(eid)
            if not g:
                continue
            per_g[g] = per_g.get(g, 0.0) + sum(r["entered"] for r in series_.values())
        if per_g:
            dom = max(per_g, key=per_g.get)
            g2pc[dom] = host_to_pc(f.name)
    return g2pc


def parse_edgedata(path: Path, keep: set) -> dict:
    """{edge: {begin: {'entered','left','speed'}}} solo para edges en keep."""
    out: dict = {}
    ctx = ET.iterparse(str(path), events=("start", "end"))
    begin = 0.0
    for ev, el in ctx:
        if ev == "start" and el.tag == "interval":
            begin = float(el.get("begin", 0))
        elif ev == "end" and el.tag == "edge":
            eid = el.get("id")
            if eid in keep:
                rec = {
                    "entered": float(el.get("entered", 0) or 0),
                    "left": float(el.get("left", 0) or 0),
                    "speed": float(el.get("speed", 0) or 0),
                }
                out.setdefault(eid, {})[begin] = rec
            el.clear()
        elif ev == "end" and el.tag == "interval":
            el.clear()
    return out


def merge_workers(files, keep: set, mode: str = "max") -> dict:
    """Fusiona varios edgedata de workers por (edge, begin).

    mode='max': toma el record con mas 'entered' (arista simulada por su worker
                propietario; util cuando una sola maquina la ejecuta).
    mode='sum': SUMA 'entered'/'left' de TODAS las maquinas que tocan la arista
                (segmento-nexo COMPLETO = ambos lados del handoff). 'speed' se
                promedia ponderado por 'entered'.
    """
    merged: dict = {}
    for f in files:
        d = parse_edgedata(f, keep)
        for eid, series in d.items():
            m = merged.setdefault(eid, {})
            for b, rec in series.items():
                if b not in m:
                    m[b] = dict(rec)
                elif mode == "sum":
                    prev = m[b]
                    e0, e1 = prev["entered"], rec["entered"]
                    tot = e0 + e1
                    prev["speed"] = ((prev["speed"] * e0 + rec["speed"] * e1) / tot
                                     if tot > 0 else max(prev["speed"], rec["speed"]))
                    prev["entered"] = tot
                    prev["left"] = prev["left"] + rec["left"]
                elif rec["entered"] > m[b]["entered"]:
                    m[b] = dict(rec)
    return merged


def series(dat: dict, eid: str, field: str):
    """field='entered'/'left'/'speed' directo; field='flow' = entered*3600/PERIOD."""
    s = dat.get(eid, {})
    xs = sorted(s)
    hrs = [b / 3600.0 for b in xs]
    if field == "flow":
        return hrs, [s[b]["entered"] * 3600.0 / PERIOD for b in xs]
    return hrs, [s[b][field] for b in xs]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--top", type=int, default=6)
    ap.add_argument("--top-bars", type=int, default=20,
                    help="nº de nexos en el grafico de barras de correlacion")
    args = ap.parse_args(argv)

    run_dir = EQUIV / args.run
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    road, partition = meta["road"], meta["partition"]

    owner, _n, nexus = find_nexus(road, partition, bool(meta.get("accident")))
    keep = set(nexus.keys())
    print(f"[edgedata] nexos: {len(keep)}")

    # color por grupo (mismo criterio que las imagenes de particion)
    all_groups = sorted({g for g in owner.values() if g and g != "SHARED"})
    color_of = {g: PALETTE[i % len(PALETTE)] for i, g in enumerate(all_groups)}

    base = parse_edgedata(run_dir / "baseline" / "environment_traffic.edgedata.xml",
                          keep)
    wfiles = sorted((run_dir / "distributed" / "workers").glob("*.edgedata.xml"))
    print(f"[edgedata] workers: {[f.name for f in wfiles]}")
    dist = merge_workers(wfiles, keep)

    g2pc = group_to_pc_map(wfiles, owner, keep)
    print(f"[edgedata] grupo->PC: {g2pc}")

    def frontier_label(eid: str) -> str:
        own = nexus[eid]["own"]
        tos = nexus[eid]["to"]
        own_pc = g2pc.get(own, own)
        to_pc = "/".join(g2pc.get(t, t) for t in sorted(tos))
        return f"{own_pc}\u2194{to_pc}"

    # totales reales por arista (suma de 'entered')
    def total_entered(dat, eid):
        return sum(r["entered"] for r in dat.get(eid, {}).values())

    rows = []
    for eid in sorted(keep):
        b = total_entered(base, eid)
        d = total_entered(dist, eid)
        err = abs(d - b) / b if b > 0 else ""
        rows.append((eid, nexus[eid]["own"], ",".join(nexus[eid]["to"]),
                     int(round(b)), int(round(d)),
                     round(err, 4) if err != "" else ""))

    with (run_dir / "nexus_edgedata_compare.csv").open("w", newline="",
                                                       encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["edge", "grupo", "vecinos", "entered_base", "entered_dist",
                    "err_rel"])
        w.writerows(rows)

    pairs = [(r[3], r[4]) for r in rows if r[3] > 0]
    n = len(pairs)
    summary = {"run": args.run, "road": road, "partition": partition,
               "metric": "entered (vehiculos reales)",
               "n_nexus": len(keep), "n_nexus_activos": n}
    if n:
        bs = [p[0] for p in pairs]
        ds = [p[1] for p in pairs]
        errs = sorted(abs(d - b) / b for b, d in pairs)
        mb, md = sum(bs) / n, sum(ds) / n
        cov = sum((b - mb) * (d - md) for b, d in pairs)
        vb = sum((b - mb) ** 2 for b in bs) ** 0.5
        vd = sum((d - md) ** 2 for d in ds) ** 0.5
        summary.update({
            "pearson_r": round(cov / (vb * vd), 4) if vb and vd else 0,
            "err_rel_medio": round(sum(errs) / n, 4),
            "err_rel_mediana": round(errs[n // 2], 4),
            "total_entered_base": int(sum(bs)),
            "total_entered_dist": int(sum(ds)),
            "ratio_dist_base": round(sum(ds) / sum(bs), 4) if sum(bs) else None,
        })
    (run_dir / "nexus_edgedata_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[edgedata] resumen: {json.dumps(summary, ensure_ascii=False)}")

    # top-N por entered en baseline
    picks = [r[0] for r in sorted(rows, key=lambda r: r[3], reverse=True)[:args.top]
             if r[3] > 0]

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ncol = 2
    nrow = (len(picks) + ncol - 1) // ncol
    for field, fname, ylabel in (
            ("flow", "nexus_ts_flujo.png", "Flujo (veh/h)"),
            ("entered", "nexus_ts_conteo.png",
             "Vehículos entrantes / 5 min")):
        fig, axes = plt.subplots(nrow, ncol, figsize=(12, 3 * nrow),
                                 squeeze=False)
        for k, eid in enumerate(picks):
            ax = axes[k // ncol][k % ncol]
            xb, yb = series(base, eid, field)
            xd, yd = series(dist, eid, field)
            ax.plot(xb, yb, "-o", ms=3, lw=1.4, color="#1f77b4",
                    label="1 nodo (baseline)")
            ax.plot(xd, yd, "-s", ms=3, lw=1.4, color="#d1004b",
                    label="6 cortes")
            own = nexus[eid]["own"]
            cut_color = color_of.get(own, "#000000")
            # cuadro coloreado con el color del corte (grupo propietario)
            ax.set_title(f"{eid}  [{frontier_label(eid)}]  {own}",
                         fontsize=8, color=cut_color, fontweight="bold",
                         bbox=dict(facecolor=cut_color, alpha=0.12,
                                   edgecolor=cut_color, boxstyle="round,pad=0.2"))
            max_h = max((xb[-1] if xb else 0), (xd[-1] if xd else 0))
            ticks, labels = _clock_ticks(max_h)
            ax.set_xticks(ticks)
            ax.set_xticklabels(labels, rotation=90, fontsize=6)
            ax.set_xlabel("Hora (HH:MM)", fontsize=8)
            ax.set_ylabel(ylabel, fontsize=8)
            ax.grid(alpha=0.25)
            if k == 0:
                ax.legend(fontsize=7)
        for k in range(len(picks), nrow * ncol):
            axes[k // ncol][k % ncol].axis("off")
        fig.suptitle(f"{ylabel} por nexo - 1 nodo vs 6 cortes "
                     f"[{road}, {partition}] (edgeData real)", fontsize=12)
        fig.tight_layout(rect=[0, 0, 1, 0.98])
        fig.savefig(run_dir / fname, dpi=140)
        plt.close(fig)
        print(f"[edgedata] -> {run_dir / fname}")

    # ---- correlacion temporal por arista-nexo (baseline vs distribuido) ----
    def _pearson(xs, ys):
        n = len(xs)
        if n < 3:
            return None
        mx, my = sum(xs) / n, sum(ys) / n
        cov = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
        vx = sum((a - mx) ** 2 for a in xs) ** 0.5
        vy = sum((b - my) ** 2 for b in ys) ** 0.5
        if vx == 0 or vy == 0:
            return None
        return cov / (vx * vy)

    corr_rows = []
    for eid in sorted(keep):
        bs_ = base.get(eid, {})
        ds_ = dist.get(eid, {})
        common = sorted(set(bs_) & set(ds_))
        if not common:
            continue
        xb = [bs_[b]["entered"] for b in common]
        xd = [ds_[b]["entered"] for b in common]
        r = _pearson(xb, xd)
        tb = sum(r_["entered"] for r_ in bs_.values())
        td = sum(r_["entered"] for r_ in ds_.values())
        own = nexus[eid]["own"]
        corr_rows.append({
            "edge": eid,
            "label": f"{eid} [{frontier_label(eid)}] {own}",
            "frontera": frontier_label(eid),
            "grupo": own,
            "color": color_of.get(own, "#000000"),
            "r_temporal": round(r, 4) if r is not None else "",
            "n_intervalos": len(common),
            "total_base": int(round(tb)),
            "total_dist": int(round(td)),
            "ratio_dist_base": round(td / tb, 4) if tb > 0 else "",
        })

    corr_rows.sort(key=lambda d: d["total_base"], reverse=True)
    with (run_dir / "nexus_correlacion.csv").open("w", newline="",
                                                  encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["edge", "frontera", "grupo",
                                          "r_temporal", "n_intervalos",
                                          "total_base", "total_dist",
                                          "ratio_dist_base", "label", "color"])
        w.writeheader()
        w.writerows(corr_rows)
    print(f"[edgedata] correlacion por nexo -> nexus_correlacion.csv "
          f"({len(corr_rows)} nexos)")

    # grafico de barras: r temporal de los top-N nexos por trafico
    topn = [d for d in corr_rows if d["r_temporal"] != ""][:args.top_bars]
    if topn:
        fig, ax = plt.subplots(figsize=(10, max(4, 0.42 * len(topn))))
        ypos = list(range(len(topn)))[::-1]
        ax.barh(ypos, [d["r_temporal"] for d in topn],
                color=[d["color"] for d in topn], alpha=0.85,
                edgecolor="#333", linewidth=0.5)
        ax.set_yticks(ypos)
        ax.set_yticklabels([d["label"] for d in topn], fontsize=7)
        for y, d in zip(ypos, topn):
            ax.text(d["r_temporal"] - 0.01, y, f"{d['r_temporal']:.2f}",
                    va="center", ha="right", fontsize=6, color="white",
                    fontweight="bold")
        ax.set_xlim(0, 1.0)
        ax.axvline(0.9, color="#888", ls="--", lw=1)
        ax.set_xlabel("Correlación temporal r (1 nodo vs 6 cortes, conteo/5 min)")
        ax.set_title(f"Correlación por arista-nexo [{road}, {partition}] "
                     f"- top {len(topn)} por tráfico")
        fig.tight_layout()
        fig.savefig(run_dir / "nexus_correlacion_barras.png", dpi=140)
        plt.close(fig)
        print(f"[edgedata] -> {run_dir / 'nexus_correlacion_barras.png'}")

    # scatter total entered coloreado por corte (grupo) + frontera en leyenda
    fig, ax = plt.subplots(figsize=(8, 8))
    pts = [r for r in rows if r[3] > 0]           # (edge, own, to, base, dist, err)
    hi = max(max(r[3] for r in pts), max(r[4] for r in pts)) * 1.05
    # banda +-10%
    ax.fill_between([0, hi], [0, hi * 0.9], [0, hi * 1.1], color="#bbb",
                    alpha=0.18, label="±10 %")
    ax.plot([0, hi], [0, hi], "--", color="#555", lw=1.2, label="y = x (igualdad)")
    seen = set()
    for edge, own, to, b, d, _e in pts:
        pc = g2pc.get(own, own)
        lab = f"{pc} ({own})" if own not in seen else None
        seen.add(own)
        ax.scatter(b, d, s=22, alpha=0.7, color=color_of.get(own, "#333"),
                   label=lab, edgecolors="none")
    ax.set_xlim(0, hi)
    ax.set_ylim(0, hi)
    ax.set_aspect("equal")
    ax.set_xlabel("Vehículos totales — SECUENCIAL (1 nodo)")
    ax.set_ylabel("Vehículos totales — DISTRIBUIDO (6 cortes)")
    ax.set_title(f"¿Cuánto se parecen? Conteo real por arista-nexo\n"
                 f"{partition} [{road}]   n={n}   r={summary.get('pearson_r')}   "
                 f"ratio global={summary.get('ratio_dist_base')}")
    ax.legend(loc="upper left", fontsize=8, title="corte (PC)")
    ax.grid(alpha=0.2)
    fig.tight_layout()
    fig.savefig(run_dir / "nexus_edgedata_scatter.png", dpi=140)
    plt.close(fig)
    print(f"[edgedata] -> {run_dir / 'nexus_edgedata_scatter.png'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
