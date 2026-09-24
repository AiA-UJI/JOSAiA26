"""Prevuelo 2: SUMO pip --user (1.24), red rotterdam y trips en cada host."""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from SIMULATION.distributed.lan.cluster import env_prefix  # noqa: E402
from SIMULATION.distributed.lan.ssh import Ssh  # noqa: E402

KEY = str(Path.home() / ".ssh" / "id_ed25519")
HOSTS = [f"labrob{n:02d}.act.uji.es" for n in range(8, 31)]

CMD = (
    env_prefix("VehicleKnowledge")
    + " S=$(sumo --version 2>/dev/null | head -1 | sed 's/Eclipse SUMO //'); "
    "R=$(ls $HOME/VehicleKnowledge/SIMULATION/roads/rotterdam_arterial.net.xml "
    "2>/dev/null | wc -l); "
    "T=$(ls $HOME/VehicleKnowledge/SIMULATION/trips/rotterdam_trips_*.xml "
    "2>/dev/null | wc -l); "
    "Z=$(python3 -c 'import zmq,grpc;print(zmq.__version__,grpc.__version__)' "
    "2>/dev/null || echo NO); "
    "H=$(echo $SUMO_HOME); "
    "echo \"$S|$R|$T|$Z|$H\""
)


def probe(h: str):
    try:
        with Ssh(h, "usuario", key_filename=KEY, timeout=12) as s:
            return h, (s.run(CMD, timeout=60).out or "").strip()
    except Exception as e:
        return h, f"ERROR {type(e).__name__}: {e}"


print(f"{'host':10} {'sumo':10} {'rot':>4} {'trips':>6} {'zmq/grpc':16} SUMO_HOME")
bad = []
with ThreadPoolExecutor(max_workers=12) as ex:
    for h, out in ex.map(probe, HOSTS):
        short = h.split(".")[0]
        p = out.split("|")
        if len(p) != 5:
            print(f"{short:10} {out[:80]}")
            bad.append(short)
            continue
        s, r, t, z, home = p
        flag = "" if (s.startswith("1.24") and r == "1" and t == "3"
                      and z != "NO") else "   <-- REVISAR"
        print(f"{short:10} {s:10} {r:>4} {t:>6} {z:16} {home}{flag}")
        if flag:
            bad.append(short)
print(f"\nhosts a revisar: {bad}")
