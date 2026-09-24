"""Inventario completo de lo guardado para Rotterdam (y Almenara) en local."""
from __future__ import annotations

import csv
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RES = ROOT / "SIMULATION" / "distributed" / "lan" / "results"

rows = []
for f in RES.rglob("benchmark.csv"):
    try:
        with f.open(newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                r["_src"] = f.parent.name
                rows.append(r)
    except Exception as e:
        print("ERR leyendo", f, e)

print(f"TOTAL filas en todos los benchmark.csv: {len(rows)}\n")

by_road = Counter(str(r.get("road", "?")) for r in rows)
print("--- filas por red ---")
for k, v in by_road.most_common():
    print(f"  {k:24} {v}")

rot = [r for r in rows if "rotterdam" in str(r.get("road", ""))]
print(f"\n--- ROTTERDAM: {len(rot)} filas ---")
print("estados:", dict(Counter(str(r.get("status")) for r in rot)))

agg = [r for r in rot if r.get("status") == "aggregated"
       and r.get("speedup") not in (None, "")]
base = [r for r in rot if not r.get("partition") and r.get("status") == "ok"]
print(f"  distribuidos agregados con speedup: {len(agg)}")
print(f"  baselines ok: {len(base)}")
print("  por protocolo:", dict(Counter(str(r.get("transport")) for r in agg)))
print("  por flota:", dict(Counter(str(r.get("vehicles")) for r in agg)))
print("  por division:", dict(Counter(str(r.get("partition")) for r in agg)))

# combos unicos
combos = defaultdict(int)
for r in agg:
    combos[(str(r.get("vehicles")), str(r.get("accident")).strip().lower() == "true",
            r.get("partition"), r.get("transport"))] += 1
print(f"\n  combos UNICOS con >=1 rep: {len(combos)}")
print("  distribucion de replicas:", dict(Counter(combos.values())))

# carpetas aggregated en disco
n_aggdirs = 0
for d in RES.iterdir():
    if d.is_dir() and (d / "aggregated").is_dir():
        n_aggdirs += sum(1 for x in (d / "aggregated").iterdir() if x.is_dir())
print(f"\n  carpetas 'aggregated/<label>' en disco: {n_aggdirs}")

idx = RES / "INDEX_simulaciones.csv"
if idx.is_file():
    with idx.open(newline="", encoding="utf-8") as fh:
        irows = list(csv.DictReader(fh))
    print(f"\nINDEX_simulaciones.csv: {len(irows)} filas, "
          f"mtime={time.strftime('%Y-%m-%d %H:%M', time.localtime(idx.stat().st_mtime))}")
    print("  estados:", dict(Counter(str(r.get("status")) for r in irows)))
