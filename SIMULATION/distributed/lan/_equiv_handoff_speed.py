"""Degradacion de velocidad segun el numero de traspasos ya sufridos.

Si el traspaso entre subredes fuese neutro, la velocidad media de un tramo no
deberia depender de si es el primero del viaje o el quinto. Este script mide
esa dependencia sobre el run de equivalencia canonico.

    python -m SIMULATION.distributed.lan._equiv_handoff_speed
"""
from __future__ import annotations

import collections
import json
import statistics as st
from pathlib import Path

E = Path(__file__).resolve().parent / "equivalence" / "20260710_163101"


def speeds(recs: list) -> list:
    return [3.6 * r["routeLength"] / r["duration"]
            for r in recs if r["duration"] > 0 and r["routeLength"] > 50]


def main() -> int:
    base = json.loads((E / "baseline" / "trip_data.json").read_text("utf-8"))
    dist = json.loads((E / "distributed" / "trip_data.json").read_text("utf-8"))

    wb = sum(r["waitingTime"] for r in base) / 3600.0
    wd = sum(r["waitingTime"] for r in dist) / 3600.0
    print(f"espera acumulada: baseline={wb:.1f} h  distribuido={wd:.1f} h")

    bs = speeds(base)
    print(f"baseline (viaje completo): n={len(bs)} "
          f"mediana={st.median(bs):.1f} km/h media={st.mean(bs):.1f} km/h")
    print()

    per = collections.defaultdict(list)
    for r in dist:
        per[r["id"]].append(r)

    by_idx = collections.defaultdict(list)
    for rs in per.values():
        rs.sort(key=lambda r: r["depart"])
        for i, r in enumerate(rs):
            if r["duration"] > 0 and r["routeLength"] > 50:
                by_idx[min(i, 6)].append(3.6 * r["routeLength"] / r["duration"])

    print("velocidad del tramo segun su posicion dentro del viaje:")
    for i in sorted(by_idx):
        v = by_idx[i]
        etiqueta = f"#{i + 1}" if i < 6 else "#7 o posterior"
        print(f"  tramo {etiqueta:>14}: n={len(v):6d} "
              f"mediana={st.median(v):6.1f} km/h  media={st.mean(v):6.1f} km/h")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
