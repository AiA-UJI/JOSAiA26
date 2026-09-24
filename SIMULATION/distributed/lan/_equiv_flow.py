"""Equivalencia de intensidad de trafico: baseline de 1 nodo frente a cada corte.

A diferencia de ``_equiv_segments.py``, que trabaja sobre la ocupacion
(``count`` de environment_traffic.json), aqui se usa el *volumen* de vehiculos
que pasan por cada segmento, leido de los ``edgedata.xml`` de SUMO. La
distincion importa: la ocupacion es flujo por tiempo de permanencia, asi que
se infla sola cuando los vehiculos circulan mas despacio y no sirve para
juzgar el reparto del trafico con independencia de la velocidad.

Dos definiciones de volumen, porque el traspaso entre subredes las separa:

* ``entered``            : vehiculos que entran al segmento desde otro segmento.
* ``entered + departed`` : anade los que se insertan directamente en el. En el
  baseline eso son solo los origenes de viaje; en el distribuido incluye ademas
  las reinserciones por traspaso, que de otro modo no se contarian en ningun
  sitio.

Procedimientos, todos sobre el volumen por segmento:

* GEH, criterio de calibracion habitual en modelizacion de trafico.
* CCC de Lin, Pearson y Spearman.
* Bland-Altman sobre ``log(dist / base)``, porque en volumenes la dispersion
  crece con la magnitud y la diferencia absoluta no es comparable entre un
  segmento de 10 vehiculos y otro de 5 000.
* TOST contra una zona de equivalencia declarada en +-``EQUIV_RATIO_PCT``.
* Estratificacion por volumen, para comprobar que el acuerdo no depende del
  tamano del segmento.

Escribe ``equivalence/flow_equivalence.csv`` y ``flow_strata.csv``.

    python -m SIMULATION.distributed.lan._equiv_flow
"""
from __future__ import annotations

import csv
import math
import xml.etree.ElementTree as ET
from pathlib import Path

from ._equiv_segments import CUTS, EQ

BASELINE_EDGEDATA = (EQ / "20260710_163101" / "baseline"
                     / "environment_traffic.edgedata.xml")

# Volumen minimo en el baseline para que un segmento entre en los
# estadisticos relativos.
MIN_VOL = 5.0

# Zona de equivalencia declarada a priori: el volumen por segmento no debe
# desviarse mas de este porcentaje.
EQUIV_RATIO_PCT = 10.0

ALPHA = 0.05
Z_TOST = 1.6449
Z_BA = 1.96

# Cortes de estratificacion por volumen del baseline (vehiculos en 4 h).
STRATA = [(5, 50), (50, 200), (200, 1000), (1000, float("inf"))]


def load_edgedata(paths) -> dict:
    """edge -> (entered, departed) acumulados en todo el horizonte."""
    out: dict = {}
    for path in paths:
        for _ev, el in ET.iterparse(str(path), events=("end",)):
            if el.tag != "edge":
                continue
            eid = el.get("id")
            if eid:
                ent = float(el.get("entered") or 0.0)
                dep = float(el.get("departed") or 0.0)
                cur = out.get(eid)
                if cur is None:
                    out[eid] = [ent, dep]
                else:
                    cur[0] += ent
                    cur[1] += dep
            el.clear()
    return {k: (v[0], v[1]) for k, v in out.items()}


def phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def pearson(xs, ys) -> float:
    n = len(xs)
    if n < 2:
        return float("nan")
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return float("nan")
    return sxy / math.sqrt(sxx * syy)


def ranks(xs) -> list:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    out = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            out[order[k]] = avg
        i = j + 1
    return out


def spearman(xs, ys) -> float:
    return pearson(ranks(xs), ranks(ys))


def ccc(xs, ys) -> float:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sx2 = sum((x - mx) ** 2 for x in xs) / n
    sy2 = sum((y - my) ** 2 for y in ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / n
    den = sx2 + sy2 + (mx - my) ** 2
    return 2.0 * sxy / den if den > 0 else float("nan")


def geh(model: float, count: float) -> float:
    s = model + count
    return math.sqrt(2.0 * (model - count) ** 2 / s) if s > 0 else 0.0


def median(vs):
    if not vs:
        return float("nan")
    s = sorted(vs)
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def tost_log(logs, delta_log):
    """TOST sobre la media de log-ratios. Devuelve (p, ic_lo, ic_hi, equiv)."""
    n = len(logs)
    if n < 2:
        return float("nan"), float("nan"), float("nan"), False
    mu = sum(logs) / n
    sd = math.sqrt(sum((x - mu) ** 2 for x in logs) / (n - 1))
    if sd <= 0:
        return float("nan"), float("nan"), float("nan"), False
    se = sd / math.sqrt(n)
    p = max(1.0 - phi((mu + delta_log) / se), phi((mu - delta_log) / se))
    return p, mu - Z_TOST * se, mu + Z_TOST * se, (p < ALPHA)


def analyse(base: dict, dist: dict, cut: str, use_departed: bool) -> dict:
    def vol(d, e):
        ent, dep = d.get(e, (0.0, 0.0))
        return ent + dep if use_departed else ent

    edges = sorted(set(base) | set(dist))
    bv, dv = [], []
    for e in edges:
        b = vol(base, e)
        if b >= MIN_VOL:
            bv.append(b)
            dv.append(vol(dist, e))

    n = len(bv)
    tot_b, tot_d = sum(bv), sum(dv)

    gs = [geh(d, b) for b, d in zip(bv, dv)]
    rel = [abs(d - b) / b for b, d in zip(bv, dv)]

    pairs = [(b, d) for b, d in zip(bv, dv) if d > 0]
    logs = [math.log(d / b) for b, d in pairs]
    mu_log = sum(logs) / len(logs) if logs else float("nan")
    sd_log = (math.sqrt(sum((x - mu_log) ** 2 for x in logs) / (len(logs) - 1))
              if len(logs) > 1 else float("nan"))

    delta_log = math.log(1.0 + EQUIV_RATIO_PCT / 100.0)
    p_tost, ic_lo, ic_hi, equiv = tost_log(logs, delta_log)

    return {
        "corte": cut,
        "volumen": "entered+departed" if use_departed else "entered",
        "segmentos": n,
        "volumen_total_base": round(tot_b, 0),
        "volumen_total_dist": round(tot_d, 0),
        "ratio_total": round(tot_d / tot_b, 4) if tot_b else "",
        "ratio_geometrico": round(math.exp(mu_log), 4),
        "ba_ratio_inf": round(math.exp(mu_log - Z_BA * sd_log), 4),
        "ba_ratio_sup": round(math.exp(mu_log + Z_BA * sd_log), 4),
        "tost_ic90_ratio_inf": round(math.exp(ic_lo), 4),
        "tost_ic90_ratio_sup": round(math.exp(ic_hi), 4),
        "tost_p": round(p_tost, 4),
        "tost_equivalente": "si" if equiv else "no",
        "ccc_lin": round(ccc(bv, dv), 4),
        "pearson_r": round(pearson(bv, dv), 4),
        "spearman_rho": round(spearman(bv, dv), 4),
        "geh_mediano": round(median(gs), 2),
        "pct_geh_menor_5": round(100.0 * sum(1 for g in gs if g < 5.0) / n, 1),
        "pct_geh_menor_10": round(100.0 * sum(1 for g in gs if g < 10.0) / n, 1),
        "err_rel_mediano_pct": round(100.0 * median(rel), 2),
        "mape_pct": round(100.0 * sum(rel) / n, 2),
        "pct_banda_10": round(100.0 * sum(1 for r in rel if r <= 0.10) / n, 1),
        "pct_banda_20": round(100.0 * sum(1 for r in rel if r <= 0.20) / n, 1),
    }


def strata_rows(base: dict, dist: dict, cut: str, use_departed: bool) -> list:
    def vol(d, e):
        ent, dep = d.get(e, (0.0, 0.0))
        return ent + dep if use_departed else ent

    edges = sorted(set(base) | set(dist))
    out = []
    for lo, hi in STRATA:
        bv, dv = [], []
        for e in edges:
            b = vol(base, e)
            if lo <= b < hi:
                bv.append(b)
                dv.append(vol(dist, e))
        if not bv:
            continue
        gs = [geh(d, b) for b, d in zip(bv, dv)]
        rel = [abs(d - b) / b for b, d in zip(bv, dv)]
        label = f"{lo:.0f}-{hi:.0f}" if hi != float("inf") else f">{lo:.0f}"
        out.append({
            "corte": cut,
            "estrato_veh": label,
            "segmentos": len(bv),
            "volumen_base": round(sum(bv), 0),
            "ratio": round(sum(dv) / sum(bv), 4) if sum(bv) else "",
            "geh_mediano": round(median(gs), 2),
            "pct_geh_menor_5": round(100.0 * sum(1 for g in gs if g < 5.0) / len(gs), 1),
            "err_rel_mediano_pct": round(100.0 * median(rel), 2),
            "pct_banda_10": round(100.0 * sum(1 for r in rel if r <= 0.10) / len(rel), 1),
        })
    return out


def main() -> int:
    print(f"[flow] leyendo baseline {BASELINE_EDGEDATA.name} ...")
    base = load_edgedata([BASELINE_EDGEDATA])
    print(f"[flow] baseline: {len(base)} edges, "
          f"entered={sum(v[0] for v in base.values()):.0f} "
          f"departed={sum(v[1] for v in base.values()):.0f}")
    print(f"[flow] zona de equivalencia declarada: +-{EQUIV_RATIO_PCT:.0f} %\n")

    rows, strata = [], []
    for cut, folder in CUTS.items():
        wdir = folder / "workers"
        files = sorted(wdir.glob("*.edgedata.xml"))
        if not files:
            print(f"[flow] falta edgedata de {cut}")
            continue
        dist = load_edgedata(files)
        for use_dep in (False, True):
            rows.append(analyse(base, dist, cut, use_dep))
        strata.extend(strata_rows(base, dist, cut, True))
        r = rows[-1]
        print(f"[flow] {cut:16} ({len(files)} workers) "
              f"ratio={r['ratio_total']} GEH<5={r['pct_geh_menor_5']}% "
              f"r={r['pearson_r']} CCC={r['ccc_lin']}")

    ent = [r for r in rows if r["volumen"] == "entered+departed"]
    ent.sort(key=lambda r: -r["ccc_lin"])

    print(f"\n[flow] volumen = entered + departed, ordenado por CCC")
    print(f"{'corte':16} {'CCC':>7} {'Pearson':>8} {'ratio':>7} "
          f"{'GEH<5':>7} {'±10%':>7} {'TOST':>6}")
    for r in ent:
        print(f"{r['corte']:16} {r['ccc_lin']:>7.4f} {r['pearson_r']:>8.4f} "
              f"{r['ratio_total']:>7.4f} {r['pct_geh_menor_5']:>6.1f}% "
              f"{r['pct_banda_10']:>6.1f}% {r['tost_equivalente']:>6}")

    out = EQ / "flow_equivalence.csv"
    with out.open("w", newline="", encoding="utf-8") as fp:
        w = csv.DictWriter(fp, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"\n[flow] -> {out}")

    out2 = EQ / "flow_strata.csv"
    with out2.open("w", newline="", encoding="utf-8") as fp:
        w = csv.DictWriter(fp, fieldnames=list(strata[0]))
        w.writeheader()
        w.writerows(strata)
    print(f"[flow] -> {out2}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
