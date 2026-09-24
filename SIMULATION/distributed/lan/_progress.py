"""Avance real de cada grupo: pasos simulados, tiempo transcurrido y carga."""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from SIMULATION.distributed.lan.ssh import Ssh  # noqa: E402

KEY = str(Path.home() / ".ssh" / "id_ed25519")
GROUPS = {
    "G1x6": [f"labrob{n:02d}.act.uji.es" for n in range(8, 14)],
    "G2x4": [f"labrob{n:02d}.act.uji.es" for n in range(14, 18)],
    "G3x3": [f"labrob{n:02d}.act.uji.es" for n in range(18, 21)],
    "G4x3": [f"labrob{n:02d}.act.uji.es" for n in range(21, 24)],
    "G5x2": [f"labrob{n:02d}.act.uji.es" for n in range(24, 26)],
    "G6x2": [f"labrob{n:02d}.act.uji.es" for n in range(26, 28)],
    "G7x2": [f"labrob{n:02d}.act.uji.es" for n in range(28, 30)],
}

CMD = (
    "E=$(ps -eo etime,cmd --sort=-etime | grep '[s]umo' | head -1 "
    "| awk '{print $1}'); "
    "L=$(uptime | sed 's/.*load average: //' | cut -d, -f1); "
    "S=$(grep -ho 'step[ =]*[0-9]*' $HOME/VehicleKnowledge/_lan/worker.log "
    "2>/dev/null | tail -1); "
    "P=$(ls -t $HOME/VehicleKnowledge/_lan/worker_runs/*/*/ 2>/dev/null "
    "| head -1); "
    "echo \"$E|$L|$S\""
)


def probe(h: str):
    try:
        with Ssh(h, "usuario", key_filename=KEY, timeout=12) as s:
            return h, (s.run(CMD, timeout=40).out or "").strip()
    except Exception as e:
        return h, f"ERROR {type(e).__name__}"


for gname, hosts in GROUPS.items():
    with ThreadPoolExecutor(max_workers=6) as ex:
        outs = list(ex.map(probe, hosts))
    print(f"--- {gname} ---")
    for h, out in outs:
        p = out.split("|")
        if len(p) == 3:
            print(f"   {h.split('.')[0]:10} sumo lleva {p[0]:>10}  "
                  f"carga {p[1]:>6}  {p[2]}")
        else:
            print(f"   {h.split('.')[0]:10} {out}")
