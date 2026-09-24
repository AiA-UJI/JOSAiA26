"""Comparacion emparejada: solo vehiculos que hicieron el MISMO viaje completo.

Las comparaciones anteriores mezclan poblaciones. El baseline registra 17 302
vehiculos que terminaron su viaje dentro del horizonte; el distribuido registra
entre 18 873 y 19 602 identificadores con al menos un tramo cerrado, y un tramo
cerrado no implica viaje terminado. Dividir tiempos totales entre censos
distintos infla o desinfla el resultado segun que metrica se elija.

Este script se queda solo con los vehiculos presentes en ambos lados cuya
distancia recorrida coincide dentro de una tolerancia, es decir, los que
recorrieron el mismo trayecto completo en las dos ejecuciones, y compara sus
duraciones y velocidades vehiculo a vehiculo.

    python -m SIMULATION.distributed.lan._equiv_matched
"""
from __future__ import annotations

import collections
import csv
import json
import statistics as st
from pathlib import Path

EQ = Path(__file__).resolve().parent / "equivalence"
BASE = EQ / "20260710_163101" / "baseline" / "trip_data.json"

CUTS = {
    "balanced-x-6": EQ / "20260715_130537_balanced-x-6" / "distributed",
    "balanced-y-6": EQ / "20260710_163101" / "distributed",
    "balanced-x-8": EQ / "20260715_111146_balanced-x-8" / "distributed",
    "balanced-y-8": EQ / "20260715_120508_balanced-y-8" / "distributed",
    "balanced-x-12": EQ / "20260715_095134_balanced-x-12" / "distributed",
    "balanced-y-12": EQ / "20260715_103435_balanced-y-12" / "distributed",
}

# Tolerancia en distancia para considerar que es el mismo viaje completo.
TOL = 0.02


def main() -> int:
    base = {r["id"]: r for r in json.loads(BASE.read_text(encoding="utf-8"))}
    print(f"baseline: {len(base)} viajes completos")

    rows = []
    for cut, folder in CUTS.items():
        recs = json.loads((folder / "trip_data.json").read_text(encoding="utf-8"))
        per = collections.defaultdict(lambda: [0.0, 0.0, 0.0])
        for r in recs:
            a = per[r["id"]]
            a[0] += r["routeLength"]
            a[1] += r["duration"]
            a[2] += r["waitingTime"]

        comunes = 0
        emparejados = []
        for vid, b in base.items():
            d = per.get(vid)
            if d is None:
                continue
            comunes += 1
            if b["routeLength"] <= 0:
                continue
            if abs(d[0] - b["routeLength"]) / b["routeLength"] > TOL:
                continue
            emparejados.append((b, d))

        if not emparejados:
            print(f"[{cut}] sin viajes emparejables")
            continue

        db = sum(b["routeLength"] for b, _ in emparejados)
        dd = sum(d[0] for _, d in emparejados)
        tb = sum(b["duration"] for b, _ in emparejados)
        td = sum(d[1] for _, d in emparejados)
        wb = sum(b["waitingTime"] for b, _ in emparejados)
        wd = sum(d[2] for _, d in emparejados)

        ratios = sorted(d[1] / b["duration"] for b, d in emparejados
                        if b["duration"] > 0)
        peor = sum(1 for r in ratios if r > 1.10)

        row = {
            "corte": cut,
            "vids_comunes": comunes,
            "viajes_emparejados": len(emparejados),
            "pct_del_baseline": round(100.0 * len(emparejados) / len(base), 1),
            "dist_km": round(db / 1000),
            "dist_coincide_pct": round(100.0 * dd / db, 2),
            "vel_base_kmh": round(3.6 * db / tb, 2),
            "vel_dist_kmh": round(3.6 * dd / td, 2),
            "dif_vel_pct": round(100.0 * (3.6 * dd / td - 3.6 * db / tb)
                                 / (3.6 * db / tb), 2),
            "tiempo_medio_base_s": round(tb / len(emparejados), 1),
            "tiempo_medio_dist_s": round(td / len(emparejados), 1),
            "ratio_tiempo": round(td / tb, 4),
            "ratio_tiempo_mediano": round(st.median(ratios), 4),
            "pct_viajes_mas_10pct_lentos": round(100.0 * peor / len(ratios), 1),
            "espera_h_base": round(wb / 3600, 1),
            "espera_h_dist": round(wd / 3600, 1),
        }
        rows.append(row)
        print(f"[{cut:14}] emparejados={len(emparejados):6d} "
              f"({row['pct_del_baseline']:.0f}% del baseline)  "
              f"vel {row['vel_base_kmh']:.1f} -> {row['vel_dist_kmh']:.1f} km/h "
              f"({row['dif_vel_pct']:+.1f}%)  "
              f"tiempo x{row['ratio_tiempo']:.3f} "
              f"(mediana x{row['ratio_tiempo_mediano']:.3f})  "
              f"{row['pct_viajes_mas_10pct_lentos']:.0f}% de viajes >10% mas lentos")

    out = EQ / "matched_equivalence.csv"
    with out.open("w", newline="", encoding="utf-8") as fp:
        w = csv.DictWriter(fp, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
