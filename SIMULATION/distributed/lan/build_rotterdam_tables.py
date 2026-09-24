"""Tablas de speedup de Rotterdam a partir de TODO lo que haya en results/.

Genera tres ficheros en ``results/``:

* ``rotterdam_speedups.csv``   una fila por combinacion medida
  (workers, division, eje, protocolo, vehiculos, accidente) con la media, la
  desviacion y el numero de replicas.
* ``rotterdam_matriz.csv``     la misma informacion en forma de matriz:
  una fila por (vehiculos, accidente, division) y una columna por protocolo.
* ``rotterdam_cobertura.csv``  cuantas replicas hay en cada celda de la matriz
  objetivo, para ver de un vistazo lo que falta.

Solo se tienen en cuenta las ejecuciones con el horizonte de 14400 pasos, que
son las comparables entre si (las pruebas cortas quedan fuera porque no llevan
speedup calculado).

    python -m SIMULATION.distributed.lan.build_rotterdam_tables
"""
from __future__ import annotations

import csv
import statistics as st
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "results"

FLEETS = ["15000", "20000", "30000"]
NGROUPS = [2, 3, 4, 6]
PROTOCOLS = ["tcp", "udp", "http", "zmq", "grpc"]
DIVS = [f"balanced-{ax}-{n}" for n in NGROUPS for ax in ("y", "x")]
HORIZON = 14400


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def collect() -> dict:
    """(vehiculos, accidente, division, protocolo) -> lista de speedups."""
    agg = defaultdict(list)
    for f in ROOT.rglob("benchmark.csv"):
        try:
            recs = list(csv.DictReader(f.open(newline="", encoding="utf-8")))
        except Exception:
            continue
        for x in recs:
            if "rotterdam" not in str(x.get("road", "")):
                continue
            if x.get("status") != "aggregated" or not x.get("partition"):
                continue
            sp = _f(x.get("speedup"))
            if sp is None:
                continue
            barriers = _f(x.get("barriers"))
            if barriers is not None and int(barriers) != HORIZON:
                continue
            agg[(str(x.get("vehicles")),
                 "si" if str(x.get("accident")).strip().lower() == "true" else "no",
                 x.get("partition"),
                 x.get("transport"))].append(sp)
    return agg


def write_long(agg: dict) -> Path:
    out = ROOT / "rotterdam_speedups.csv"
    cols = ["mapa", "workers", "division", "eje", "protocolo", "vehiculos",
            "accidente", "speedup", "sd", "n"]
    rows = []
    for (veh, acc, div, proto), vals in agg.items():
        ng = int(div.rsplit("-", 1)[1])
        rows.append({
            "mapa": "Rotterdam", "workers": ng, "division": div,
            "eje": div.split("-")[1], "protocolo": proto, "vehiculos": int(veh),
            "accidente": acc,
            "speedup": f"{st.mean(vals):.3f}",
            "sd": f"{st.stdev(vals):.3f}" if len(vals) > 1 else "",
            "n": len(vals),
        })
    rows.sort(key=lambda r: (r["vehiculos"], r["accidente"], r["workers"],
                             r["eje"], r["protocolo"]))
    with out.open("w", newline="", encoding="utf-8") as fp:
        w = csv.DictWriter(fp, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    return out


def write_matrix(agg: dict) -> Path:
    out = ROOT / "rotterdam_matriz.csv"
    cols = ["vehiculos", "accidente", "workers", "division"] + PROTOCOLS
    with out.open("w", newline="", encoding="utf-8") as fp:
        w = csv.DictWriter(fp, fieldnames=cols)
        w.writeheader()
        for veh in FLEETS:
            for acc in ("no", "si"):
                for div in DIVS:
                    row = {"vehiculos": veh, "accidente": acc,
                           "workers": int(div.rsplit("-", 1)[1]),
                           "division": div}
                    any_val = False
                    for p in PROTOCOLS:
                        vals = agg.get((veh, acc, div, p))
                        if vals:
                            row[p] = f"{st.mean(vals):.3f}"
                            any_val = True
                        else:
                            row[p] = ""
                    if any_val:
                        w.writerow(row)
    return out


def write_coverage(agg: dict) -> Path:
    out = ROOT / "rotterdam_cobertura.csv"
    cols = ["vehiculos", "accidente", "division"] + PROTOCOLS
    with out.open("w", newline="", encoding="utf-8") as fp:
        w = csv.DictWriter(fp, fieldnames=cols)
        w.writeheader()
        for veh in FLEETS:
            for acc in ("no", "si"):
                for div in DIVS:
                    row = {"vehiculos": veh, "accidente": acc, "division": div}
                    for p in PROTOCOLS:
                        row[p] = len(agg.get((veh, acc, div, p), []))
                    w.writerow(row)
    return out


def main() -> int:
    agg = collect()
    n_runs = sum(len(v) for v in agg.values())
    print(f"[rotterdam] {n_runs} ejecuciones validas en {len(agg)} combinaciones")

    for p in (write_long(agg), write_matrix(agg), write_coverage(agg)):
        print(f"[rotterdam] {p}")

    total = len(FLEETS) * 2 * len(DIVS) * len(PROTOCOLS)
    print(f"\n{'proto':6} {'>=1':>6} {'>=2':>6} {'>=3':>6}  de "
          f"{total // len(PROTOCOLS)} celdas")
    for p in PROTOCOLS:
        c = [sum(1 for veh in FLEETS for acc in ("no", "si") for div in DIVS
                 if len(agg.get((veh, acc, div, p), [])) >= k)
             for k in (1, 2, 3)]
        print(f"{p:6} {c[0]:>6} {c[1]:>6} {c[2]:>6}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
