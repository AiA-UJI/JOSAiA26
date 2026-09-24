"""Conservacion de vehiculos, distancia, tiempo y velocidad: baseline vs cortes.

Compara ``trip_data.json`` del run secuencial de 1 nodo con el agregado de
cada particion. En el distribuido cada vehiculo produce un registro por
subred atravesada, asi que los tramos se reagrupan por id de vehiculo antes
de comparar.

    python -m SIMULATION.distributed.lan._equiv_trips
"""
from __future__ import annotations

import collections
import csv
import json
from pathlib import Path

EQ = Path(__file__).resolve().parent / "equivalence"
BASE = EQ / "20260710_163101" / "baseline" / "trip_data.json"

CUTS = {
    "balanced-y-6": EQ / "20260710_163101" / "distributed",
    "balanced-x-6": EQ / "20260715_130537_balanced-x-6" / "distributed",
    "balanced-x-8": EQ / "20260715_111146_balanced-x-8" / "distributed",
    "balanced-y-8": EQ / "20260715_120508_balanced-y-8" / "distributed",
    "balanced-x-12": EQ / "20260715_095134_balanced-x-12" / "distributed",
    "balanced-y-12": EQ / "20260715_103435_balanced-y-12" / "distributed",
}


def totals(recs: list) -> dict:
    dist = sum(r["routeLength"] for r in recs)
    dur = sum(r["duration"] for r in recs)
    wait = sum(r["waitingTime"] for r in recs)
    vids = {r["id"] for r in recs}
    return {
        "registros": len(recs),
        "vehiculos": len(vids),
        "dist_km": dist / 1000.0,
        "tiempo_h": dur / 3600.0,
        "espera_h": wait / 3600.0,
        "vel_kmh": 3.6 * dist / dur if dur else 0.0,
    }


def main() -> int:
    base = json.loads(BASE.read_text(encoding="utf-8"))
    b = totals(base)
    print(f"[base] vehiculos={b['vehiculos']} dist={b['dist_km']:.0f} km "
          f"tiempo={b['tiempo_h']:.0f} h vel={b['vel_kmh']:.2f} km/h "
          f"espera={b['espera_h']:.0f} h")

    rows = []
    for cut, folder in CUTS.items():
        f = folder / "trip_data.json"
        if not f.is_file():
            print(f"[{cut}] falta trip_data.json")
            continue
        recs = json.loads(f.read_text(encoding="utf-8"))
        d = totals(recs)
        tramos = collections.Counter(r["id"] for r in recs)
        media_tramos = sum(tramos.values()) / len(tramos) if tramos else 0.0

        row = {
            "corte": cut,
            "vehiculos_base": b["vehiculos"],
            "vehiculos_dist": d["vehiculos"],
            "ratio_vehiculos": round(d["vehiculos"] / b["vehiculos"], 4),
            "tramos_por_vehiculo": round(media_tramos, 2),
            "dist_km_base": round(b["dist_km"]),
            "dist_km_dist": round(d["dist_km"]),
            "ratio_distancia": round(d["dist_km"] / b["dist_km"], 4),
            "tiempo_h_base": round(b["tiempo_h"]),
            "tiempo_h_dist": round(d["tiempo_h"]),
            "ratio_tiempo": round(d["tiempo_h"] / b["tiempo_h"], 4),
            "vel_kmh_base": round(b["vel_kmh"], 2),
            "vel_kmh_dist": round(d["vel_kmh"], 2),
            "dif_vel_pct": round(100.0 * (d["vel_kmh"] - b["vel_kmh"])
                                 / b["vel_kmh"], 2),
            "espera_h_base": round(b["espera_h"]),
            "espera_h_dist": round(d["espera_h"]),
        }
        rows.append(row)
        print(f"[{cut:14}] veh {d['vehiculos']:6d} ({row['ratio_vehiculos']:.3f}) "
              f"tramos/veh={media_tramos:.2f} "
              f"dist {row['ratio_distancia']:.3f} tiempo {row['ratio_tiempo']:.3f} "
              f"vel {d['vel_kmh']:.1f} km/h ({row['dif_vel_pct']:+.1f}%)")

    out = EQ / "trip_equivalence.csv"
    with out.open("w", newline="", encoding="utf-8") as fp:
        w = csv.DictWriter(fp, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"[equiv] -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
