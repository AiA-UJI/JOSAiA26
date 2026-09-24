"""Comprueba si quedan procesos vivos (master/worker/sumo) en los labrob."""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from SIMULATION.distributed.lan.ssh import Ssh  # noqa: E402

KEY = str(Path.home() / ".ssh" / "id_ed25519")
HOSTS = [f"labrob{n:02d}.act.uji.es" for n in range(8, 30)]
CMD = ("M=$(pgrep -fc '[S]IMULATION.distributed.master'); "
       "W=$(pgrep -fc '[S]IMULATION.distributed.worker'); "
       "S=$(pgrep -fc '[s]umo'); "
       "T=$(ps -eo etime,cmd --sort=-etime | grep '[S]IMULATION/main.py' "
       "| head -1 | awk '{print $1}'); "
       "echo \"master=$M worker=$W sumo=$S sim_desde=${T:-nada}\"")


def probe(h: str):
    try:
        with Ssh(h, "usuario", key_filename=KEY, timeout=15) as s:
            return h, (s.run(CMD, timeout=30).out or "").strip()
    except Exception as e:
        return h, f"ERROR {type(e).__name__}: {e}"


with ThreadPoolExecutor(max_workers=12) as ex:
    for h, out in ex.map(probe, HOSTS):
        print(f"{h.split('.')[0]:10} {out}")
