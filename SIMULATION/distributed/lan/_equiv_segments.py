"""Equivalencia por segmento: baseline de 1 nodo frente a cada particion.

Compara ``environment_traffic.json`` (conteo y velocidad por edge y por
intervalo de 5 min) del run secuencial contra el agregado distribuido, sobre
TODOS los edges de la red y no solo sobre los edges frontera.

Metricas por corte:

* intensidad por segmento  = suma del conteo medio de vehiculos en cada
  intervalo (proporcional a vehiculo-hora sobre el edge).
* velocidad media por segmento = media de la velocidad ponderada por conteo.

Los estadisticos de velocidad se calculan sobre los segmentos *emparejados*,
es decir los que superan ``MIN_INTENSITY`` en los dos runs, y se publican en
km/h.

Escribe ``equivalence/segment_equivalence.csv`` (resumen por corte) y
``equivalence/segment_detail_<corte>.csv`` (una fila por edge).

    python -m SIMULATION.distributed.lan._equiv_segments
"""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path

EQ = Path(__file__).resolve().parent / "equivalence"
BASELINE = EQ / "20260710_163101" / "baseline" / "environment_traffic.json"

# corte -> carpeta con el agregado distribuido
CUTS = {
    "balanced-y-6": EQ / "20260710_163101" / "distributed",
    "balanced-x-6": EQ / "20260715_130537_balanced-x-6" / "distributed",
    "balanced-x-8": EQ / "20260715_111146_balanced-x-8" / "distributed",
    "balanced-y-8": EQ / "20260715_120508_balanced-y-8" / "distributed",
    "balanced-x-12": EQ / "20260715_095134_balanced-x-12" / "distributed",
    "balanced-y-12": EQ / "20260715_103435_balanced-y-12" / "distributed",
}

# Umbral de trafico para que un edge entre en los estadisticos relativos.
# Por debajo, el error relativo se dispara por ruido de discretizacion.
MIN_INTENSITY = 1.0

# Un segmento emparejado se considera extremo por dos criterios en paralelo:
# umbral fijo sobre |delta v| y desviacion respecto al sesgo medio del corte.
EXTREME_KMH = 15.0
EXTREME_SD = 3.0


def load(path: Path) -> dict:
    """edge -> (intensidad, velocidad media ponderada por conteo)."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    for edge, slots in raw.items():
        n = 0.0
        ns = 0.0
        for obs in slots.values():
            # Varias observaciones en el mismo intervalo vienen de workers
            # distintos que vieron el mismo edge: son aportaciones disjuntas.
            for o in obs:
                c = float(o.get("count") or 0.0)
                s = float(o.get("speed") or 0.0)
                n += c
                ns += c * s
        out[edge] = (n, (ns / n) if n > 0 else 0.0)
    return out


def pearson(xs, ys) -> float:
    n = len(xs)
    if n < 2:
        return float("nan")
    mx = sum(xs) / n
    my = sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return float("nan")
    return sxy / math.sqrt(sxx * syy)


def analyse(base: dict, dist: dict, cut: str) -> dict:
    edges = sorted(set(base) | set(dist))

    bi, di, bs, ds, keys = [], [], [], [], []
    for e in edges:
        b_n, b_v = base.get(e, (0.0, 0.0))
        d_n, d_v = dist.get(e, (0.0, 0.0))
        bi.append(b_n)
        di.append(d_n)
        if b_n >= MIN_INTENSITY and d_n >= MIN_INTENSITY:
            bs.append(b_v)
            ds.append(d_v)
            keys.append(e)

    tot_b = sum(bi)
    tot_d = sum(di)

    # Estadisticos relativos solo sobre edges con trafico apreciable.
    rel = []
    for e in edges:
        b_n = base.get(e, (0.0, 0.0))[0]
        d_n = dist.get(e, (0.0, 0.0))[0]
        if b_n >= MIN_INTENSITY:
            rel.append(abs(d_n - b_n) / b_n)
    rel.sort()

    # Velocidad de red ponderada por intensidad.
    vb = sum(base.get(e, (0.0, 0.0))[0] * base.get(e, (0.0, 0.0))[1]
             for e in edges) / (tot_b or 1.0)
    vd = sum(dist.get(e, (0.0, 0.0))[0] * dist.get(e, (0.0, 0.0))[1]
             for e in edges) / (tot_d or 1.0)

    # bs/ds vienen en m/s; los estadisticos se publican en km/h.
    dsigned = [(d - b) * 3.6 for b, d in zip(bs, ds)]
    dspd = [abs(x) for x in dsigned]
    nd = len(dsigned)

    keep_fix = [i for i, d in enumerate(dspd) if d <= EXTREME_KMH]

    # El sesgo medio del corte no es cero, asi que el criterio estadistico se
    # centra en la media y no en el origen.
    mu = sum(dsigned) / nd if nd else 0.0
    sd = math.sqrt(sum((x - mu) ** 2 for x in dsigned) / nd) if nd else 0.0
    lim = EXTREME_SD * sd
    keep_sd = [i for i, x in enumerate(dsigned) if abs(x - mu) <= lim]

    dspd_sorted = sorted(dspd)

    detail = EQ / f"segment_detail_{cut}.csv"
    with detail.open("w", newline="", encoding="utf-8") as fp:
        w = csv.writer(fp)
        w.writerow(["edge", "intensidad_base", "intensidad_dist", "err_rel",
                    "vel_base_kmh", "vel_dist_kmh", "dif_vel_kmh"])
        for e in edges:
            b_n, b_v = base.get(e, (0.0, 0.0))
            d_n, d_v = dist.get(e, (0.0, 0.0))
            er = abs(d_n - b_n) / b_n if b_n >= MIN_INTENSITY else ""
            w.writerow([e, f"{b_n:.2f}", f"{d_n:.2f}",
                        f"{er:.4f}" if er != "" else "",
                        f"{b_v * 3.6:.2f}", f"{d_v * 3.6:.2f}",
                        f"{(d_v - b_v) * 3.6:.2f}"])

    return {
        "corte": cut,
        "edges_red": len(edges),
        "edges_con_trafico": sum(1 for e in edges
                                 if base.get(e, (0.0, 0.0))[0] >= MIN_INTENSITY),
        "intensidad_total_base": round(tot_b, 1),
        "intensidad_total_dist": round(tot_d, 1),
        "ratio_intensidad": round(tot_d / tot_b, 4) if tot_b else "",
        "pearson_r_intensidad": round(pearson(bi, di), 4),
        "err_rel_mediano": round(rel[len(rel) // 2], 4) if rel else "",
        "pct_en_banda_10": round(
            100.0 * sum(1 for r in rel if r <= 0.10) / len(rel), 1) if rel else "",
        "pct_en_banda_20": round(
            100.0 * sum(1 for r in rel if r <= 0.20) / len(rel), 1) if rel else "",
        "vel_red_base_kmh": round(vb * 3.6, 2),
        "vel_red_dist_kmh": round(vd * 3.6, 2),
        "dif_vel_red_pct": round(100.0 * (vd - vb) / vb, 2) if vb else "",
        "segmentos_emparejados": nd,
        "pearson_r_velocidad": round(pearson(bs, ds), 4),
        "extremos_15kmh": nd - len(keep_fix),
        "pct_extremos_15kmh": round(100.0 * (nd - len(keep_fix)) / nd, 2) if nd else "",
        "pearson_r_vel_sin_extremos_15kmh": round(
            pearson([bs[i] for i in keep_fix], [ds[i] for i in keep_fix]), 4),
        "sesgo_vel_medio_kmh": round(mu, 2),
        "sd_dif_vel_kmh": round(sd, 2),
        "limite_3sd_kmh": round(lim, 2),
        "extremos_3sd": nd - len(keep_sd),
        "pct_extremos_3sd": round(100.0 * (nd - len(keep_sd)) / nd, 2) if nd else "",
        "pearson_r_vel_sin_extremos_3sd": round(
            pearson([bs[i] for i in keep_sd], [ds[i] for i in keep_sd]), 4),
        "dif_vel_mediana_kmh": round(dspd_sorted[len(dspd_sorted) // 2], 2) if dspd else "",
        "pct_vel_dentro_5kmh": round(
            100.0 * sum(1 for d in dspd if d <= 5.0) / len(dspd), 1) if dspd else "",
        "pct_vel_dentro_10kmh": round(
            100.0 * sum(1 for d in dspd if d <= 10.0) / len(dspd), 1) if dspd else "",
    }


def main() -> int:
    base = load(BASELINE)
    print(f"[equiv] baseline: {len(base)} edges ({BASELINE.parent.parent.name})")

    rows = []
    for cut, folder in CUTS.items():
        f = folder / "environment_traffic.json"
        if not f.is_file():
            print(f"[equiv] falta {cut}: {f}")
            continue
        dist = load(f)
        r = analyse(base, dist, cut)
        rows.append(r)
        print(f"[equiv] {cut:16} r_int={r['pearson_r_intensidad']} "
              f"ratio={r['ratio_intensidad']} "
              f"banda10={r['pct_en_banda_10']}% "
              f"vel {r['vel_red_base_kmh']} -> {r['vel_red_dist_kmh']} km/h "
              f"({r['dif_vel_red_pct']}%)")
        print(f"{'':8} {'':16} n={r['segmentos_emparejados']} "
              f"r_vel={r['pearson_r_velocidad']} "
              f"| 15km/h: {r['extremos_15kmh']} ({r['pct_extremos_15kmh']}%) "
              f"-> {r['pearson_r_vel_sin_extremos_15kmh']} "
              f"| 3sd={r['limite_3sd_kmh']}km/h: {r['extremos_3sd']} "
              f"({r['pct_extremos_3sd']}%) -> {r['pearson_r_vel_sin_extremos_3sd']}")

    out = EQ / "segment_equivalence.csv"
    with out.open("w", newline="", encoding="utf-8") as fp:
        w = csv.DictWriter(fp, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"[equiv] -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
