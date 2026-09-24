"""Validacion formal de equivalencia: baseline de 1 nodo frente a cada corte.

Sustituye la correlacion de Pearson por procedimientos que miden *acuerdo* y
no simple asociacion lineal. Pearson es invariante a desplazamientos, asi que
no detecta el sesgo sistematico de velocidad que introduce la particion.

Procedimientos aplicados a la velocidad media por segmento:

* Bland-Altman: sesgo medio y limites de acuerdo (sesgo +- 1.96 sd) sobre
  ``delta v = distribuido - baseline``. Responde a "que desviacion cabe
  esperar en un segmento cualquiera".
* TOST: dos pruebas unilaterales contra una zona de equivalencia fijada a
  priori en +-``EQUIV_DELTA_KMH``. Es el unico procedimiento que permite
  *afirmar* equivalencia; un contraste de diferencia no significativa solo
  indicaria falta de potencia.
* CCC de Lin: coeficiente de correlacion de concordancia, que penaliza tanto
  el sesgo como la diferencia de escala frente a la recta y = x.
* Spearman: alternativa robusta a Pearson, sin exclusion de extremos.

Y a la intensidad por segmento:

* GEH, el estadistico de calibracion habitual en modelizacion de trafico.

Todos los estadisticos se calculan sin ponderar y ponderados por la
intensidad del baseline, porque sin ponderar una calle residencial con un
vehiculo pesa igual que una arteria con miles.

Escribe ``equivalence/segment_agreement.csv``.

    python -m SIMULATION.distributed.lan._equiv_agreement
"""
from __future__ import annotations

import csv
import math

from ._equiv_segments import BASELINE, CUTS, EQ, MIN_INTENSITY, load

# Zona de equivalencia declarada antes de mirar los datos: se acepta que la
# particion es fiel si la velocidad media por segmento no se desvia mas de
# esto respecto al baseline.
EQUIV_DELTA_KMH = 5.0

# TOST se evalua al 5 %, que equivale a un intervalo de confianza del 90 %.
ALPHA = 0.05
Z_TOST = 1.6449
Z_BA = 1.96

MS_TO_KMH = 3.6


def phi(z: float) -> float:
    """Funcion de distribucion acumulada de la normal estandar."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def wmean(xs, ws) -> float:
    sw = sum(ws)
    return sum(x * w for x, w in zip(xs, ws)) / sw if sw else float("nan")


def wvar(xs, ws, mu) -> float:
    sw = sum(ws)
    return sum(w * (x - mu) ** 2 for x, w in zip(xs, ws)) / sw if sw else float("nan")


def wcov(xs, ys, ws, mx, my) -> float:
    sw = sum(ws)
    return (sum(w * (x - mx) * (y - my) for x, y, w in zip(xs, ys, ws)) / sw
            if sw else float("nan"))


def kish_neff(ws) -> float:
    """Tamano de muestra efectivo de una muestra ponderada."""
    sw = sum(ws)
    sw2 = sum(w * w for w in ws)
    return (sw * sw) / sw2 if sw2 else 0.0


def ranks(xs) -> list:
    """Rangos con promedio en los empates."""
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


def spearman(xs, ys) -> float:
    return pearson(ranks(xs), ranks(ys))


def ccc(xs, ys, ws=None) -> float:
    """Coeficiente de correlacion de concordancia de Lin."""
    if ws is None:
        ws = [1.0] * len(xs)
    mx = wmean(xs, ws)
    my = wmean(ys, ws)
    sx2 = wvar(xs, ws, mx)
    sy2 = wvar(ys, ws, my)
    sxy = wcov(xs, ys, ws, mx, my)
    den = sx2 + sy2 + (mx - my) ** 2
    return 2.0 * sxy / den if den > 0 else float("nan")


def tost(mu: float, sd: float, n: float, delta: float) -> tuple:
    """Devuelve (p_tost, ic90_bajo, ic90_alto, equivalente)."""
    if n < 2 or sd <= 0:
        return float("nan"), float("nan"), float("nan"), False
    se = sd / math.sqrt(n)
    p_lo = 1.0 - phi((mu + delta) / se)   # H0: mu <= -delta
    p_hi = phi((mu - delta) / se)         # H0: mu >= +delta
    p = max(p_lo, p_hi)
    lo = mu - Z_TOST * se
    hi = mu + Z_TOST * se
    return p, lo, hi, (p < ALPHA)


def wilson(k: int, n: int) -> tuple:
    """Intervalo de confianza al 95 % de una proporcion."""
    if n == 0:
        return float("nan"), float("nan")
    p = k / n
    z = 1.96
    d = 1.0 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return 100.0 * (c - m) / d, 100.0 * (c + m) / d


def geh(model: float, count: float) -> float:
    s = model + count
    return math.sqrt(2.0 * (model - count) ** 2 / s) if s > 0 else 0.0


def analyse(base: dict, dist: dict, cut: str) -> dict:
    edges = sorted(set(base) | set(dist))

    # Segmentos emparejados: trafico apreciable en los dos runs.
    vb, vd, w = [], [], []
    for e in edges:
        b_n, b_v = base.get(e, (0.0, 0.0))
        d_n, d_v = dist.get(e, (0.0, 0.0))
        if b_n >= MIN_INTENSITY and d_n >= MIN_INTENSITY:
            vb.append(b_v * MS_TO_KMH)
            vd.append(d_v * MS_TO_KMH)
            w.append(b_n)
    n = len(vb)
    diff = [d - b for b, d in zip(vb, vd)]

    mu = sum(diff) / n
    sd = math.sqrt(sum((x - mu) ** 2 for x in diff) / (n - 1))
    mu_w = wmean(diff, w)
    sd_w = math.sqrt(wvar(diff, w, mu_w))
    neff = kish_neff(w)

    p_tost, ic_lo, ic_hi, equiv = tost(mu, sd, n, EQUIV_DELTA_KMH)
    p_tost_w, ic_lo_w, ic_hi_w, equiv_w = tost(mu_w, sd_w, neff, EQUIV_DELTA_KMH)

    within = sum(1 for x in diff if abs(x) <= EQUIV_DELTA_KMH)
    wil_lo, wil_hi = wilson(within, n)
    sw = sum(w)
    within_w = sum(wi for x, wi in zip(diff, w) if abs(x) <= EQUIV_DELTA_KMH)

    # GEH sobre la intensidad, en los edges con trafico en el baseline.
    gs = []
    for e in edges:
        b_n = base.get(e, (0.0, 0.0))[0]
        d_n = dist.get(e, (0.0, 0.0))[0]
        if b_n >= MIN_INTENSITY:
            gs.append(geh(d_n, b_n))
    gs.sort()

    return {
        "corte": cut,
        "segmentos": n,
        "n_efectivo_kish": round(neff, 1),
        # Bland-Altman
        "sesgo_kmh": round(mu, 2),
        "sd_dif_kmh": round(sd, 2),
        "ba_limite_inf_kmh": round(mu - Z_BA * sd, 2),
        "ba_limite_sup_kmh": round(mu + Z_BA * sd, 2),
        "ba_amplitud_kmh": round(2 * Z_BA * sd, 2),
        "sesgo_pond_kmh": round(mu_w, 2),
        "sd_dif_pond_kmh": round(sd_w, 2),
        "ba_limite_inf_pond_kmh": round(mu_w - Z_BA * sd_w, 2),
        "ba_limite_sup_pond_kmh": round(mu_w + Z_BA * sd_w, 2),
        # TOST contra la zona +-EQUIV_DELTA_KMH
        "tost_ic90_inf_kmh": round(ic_lo, 3),
        "tost_ic90_sup_kmh": round(ic_hi, 3),
        "tost_p": round(p_tost, 4),
        "tost_equivalente": "si" if equiv else "no",
        "tost_p_pond": round(p_tost_w, 4),
        "tost_equivalente_pond": "si" if equiv_w else "no",
        # Acuerdo
        "ccc_lin": round(ccc(vb, vd), 4),
        "ccc_lin_pond": round(ccc(vb, vd, w), 4),
        "pearson_r": round(pearson(vb, vd), 4),
        "spearman_rho": round(spearman(vb, vd), 4),
        # Tolerancia
        "pct_dentro_zona": round(100.0 * within / n, 1),
        "pct_dentro_zona_ic_inf": round(wil_lo, 1),
        "pct_dentro_zona_ic_sup": round(wil_hi, 1),
        "pct_dentro_zona_pond": round(100.0 * within_w / sw, 1) if sw else "",
        # GEH sobre intensidad
        "geh_mediano": round(gs[len(gs) // 2], 2) if gs else "",
        "pct_geh_menor_5": round(100.0 * sum(1 for g in gs if g < 5.0) / len(gs), 1) if gs else "",
        "pct_geh_menor_10": round(100.0 * sum(1 for g in gs if g < 10.0) / len(gs), 1) if gs else "",
    }


def main() -> int:
    base = load(BASELINE)
    print(f"[agree] baseline: {len(base)} edges")
    print(f"[agree] zona de equivalencia declarada: +-{EQUIV_DELTA_KMH:.0f} km/h\n")

    rows = []
    for cut, folder in CUTS.items():
        f = folder / "environment_traffic.json"
        if not f.is_file():
            print(f"[agree] falta {cut}")
            continue
        rows.append(analyse(base, load(f), cut))

    rows.sort(key=lambda r: -r["ccc_lin_pond"])

    print(f"{'corte':16} {'CCC pond':>9} {'CCC':>7} {'Pearson':>8} "
          f"{'sesgo':>8} {'limites Bland-Altman':>24} {'TOST':>6} {'GEH<5':>7}")
    for r in rows:
        ba = f"[{r['ba_limite_inf_kmh']:.1f}, {r['ba_limite_sup_kmh']:.1f}]"
        print(f"{r['corte']:16} {r['ccc_lin_pond']:>9.4f} {r['ccc_lin']:>7.4f} "
              f"{r['pearson_r']:>8.4f} {r['sesgo_kmh']:>7.2f}  {ba:>24} "
              f"{r['tost_equivalente']:>6} {r['pct_geh_menor_5']:>6.1f}%")

    out = EQ / "segment_agreement.csv"
    with out.open("w", newline="", encoding="utf-8") as fp:
        w = csv.DictWriter(fp, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"\n[agree] -> {out}")

    best = rows[0]
    print(f"[agree] corte mas fiel al baseline: {best['corte']} "
          f"(CCC ponderado {best['ccc_lin_pond']:.4f}, "
          f"sesgo {best['sesgo_kmh']:.2f} km/h)")
    if all(r["tost_equivalente"] == "no" for r in rows):
        print(f"[agree] ninguno supera el TOST de equivalencia a "
              f"+-{EQUIV_DELTA_KMH:.0f} km/h")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
