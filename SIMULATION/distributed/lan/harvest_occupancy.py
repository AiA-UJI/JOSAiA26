"""Cosecha node_occupancy.json (coches activos por step por nodo) de CADA run
Rotterdam TCP, desde master_work/uploads/<hash>/extracted/result/ en el master.

El aggregate_summary.json LOCAL de cada run mapea label -> sub_dir(uploads), asi
que es determinista y no re-simula nada. Enruta cada run a su master (prueba
labrob07 y labrob19). Guarda en:

    SIMULATION/distributed/lan/occupancy/<label>/<group>.json

Re-ejecutable: salta grupos ya descargados.

    python -m SIMULATION.distributed.lan.harvest_occupancy
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from SIMULATION.distributed.lan.ssh import Ssh  # noqa: E402

RES = PROJECT_ROOT / "SIMULATION" / "distributed" / "lan" / "results"
OUT = PROJECT_ROOT / "SIMULATION" / "distributed" / "lan" / "occupancy"
KEY = str(Path.home() / ".ssh" / "id_ed25519")
USER = "usuario"
MASTERS = ["labrob07.act.uji.es", "labrob19.act.uji.es"]
OCC = "node_occupancy.json"


def _newest_runs() -> dict:
    """label -> aggregate_summary.json mas reciente (rotterdam tcp)."""
    best: dict = {}
    for agg in RES.rglob("aggregated/*/aggregate_summary.json"):
        label = agg.parent.name
        if "_tcp_" not in label or not label.startswith("balanced-"):
            continue
        mt = agg.stat().st_mtime
        if label not in best or mt > best[label][1]:
            best[label] = (agg, mt)
    return {lbl: v[0] for lbl, v in best.items()}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    runs = _newest_runs()
    print(f"[occ] {len(runs)} runs Rotterdam TCP a cosechar", flush=True)

    sessions = {}
    for m in MASTERS:
        try:
            sessions[m] = Ssh(m, USER, key_filename=KEY, timeout=40).__enter__()
            print(f"[occ] conectado a {m}", flush=True)
        except Exception as e:
            print(f"[occ] no pude conectar a {m}: {e}", flush=True)

    total_files = 0
    for label, agg in sorted(runs.items()):
        try:
            d = json.loads(agg.read_text(encoding="utf-8"))
        except Exception:
            continue
        subs = [s.get("sub_dir", "") for s in d.get("sub_jobs", []) if s.get("sub_dir")]
        if not subs:
            continue
        dest = OUT / label
        dest.mkdir(parents=True, exist_ok=True)
        have = len(list(dest.glob("*.json")))
        if have >= len(subs):
            print(f"[occ] {label}: ya {have}/{len(subs)} -> salto", flush=True)
            total_files += have
            continue
        # decide master: el que tenga el primer upload
        master = None
        for m, s in sessions.items():
            try:
                if s.run(f"test -f '{subs[0]}/{OCC}' && echo Y || echo N").out.strip() == "Y":
                    master = m
                    break
            except Exception:
                continue
        if master is None:
            print(f"[occ] {label}: uploads no hallados en masters -> salto", flush=True)
            continue
        s = sessions[master]
        got = 0
        for i, sd in enumerate(subs):
            remote = f"{sd}/{OCC}"
            try:
                grp = s.run(f"python3 -c \"import json;print(json.load(open('{remote}'))['group'])\" 2>/dev/null").out.strip()
            except Exception:
                grp = ""
            gname = grp if grp else f"job{i:02d}"
            local = dest / f"{gname}.json"
            if local.is_file() and local.stat().st_size > 0:
                got += 1
                continue
            try:
                s.get(remote, str(local))
                got += 1
            except Exception as e:
                print(f"[occ] {label}/{gname} fallo: {e}", flush=True)
        total_files += got
        print(f"[occ] {label}: {got}/{len(subs)} desde {master.split('.')[0]}",
              flush=True)

    for m, s in sessions.items():
        try:
            s.__exit__(None, None, None)
        except Exception:
            pass
    print(f"[occ] TERMINADO. {total_files} ficheros de ocupacion -> {OUT}",
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
