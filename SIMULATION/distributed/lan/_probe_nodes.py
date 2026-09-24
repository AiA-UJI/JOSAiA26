"""Quick reachability probe for the lab nodes (labrobNN.act.uji.es).

Tries SSH (key auth) to a range of hosts in parallel with a short timeout and
prints which are alive plus cpu/ram/sumo so we can pick the compute nodes.
"""
from __future__ import annotations

import concurrent.futures as cf
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from SIMULATION.distributed.lan.ssh import Ssh  # noqa: E402

USER = "usuario"
KEY = str(Path.home() / ".ssh" / "id_ed25519")
HOSTS = [f"labrob{n:02d}.act.uji.es" for n in range(7, 31)]


def probe(host: str) -> str:
    try:
        s = Ssh(host, USER, key_filename=KEY, timeout=5).connect()
        try:
            r = s.run(
                "nproc; free -m | awk '/Mem:/{print $2}'; "
                "(command -v sumo >/dev/null && sumo --version 2>/dev/null | head -1) "
                "|| echo 'sumo:?'",
                timeout=10,
            )
            info = " | ".join(x.strip() for x in r.out.splitlines() if x.strip())
            return f"OK   {host}  -> {info}"
        finally:
            s.close()
    except Exception as e:  # noqa: BLE001
        return f"DOWN {host}  ({type(e).__name__}: {e})"


def main() -> int:
    with cf.ThreadPoolExecutor(max_workers=len(HOSTS)) as ex:
        for line in sorted(ex.map(probe, HOSTS)):
            print(line, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
