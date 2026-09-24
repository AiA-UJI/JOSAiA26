"""Install the control PC's public key on every lab host and probe the
remote environment (python3, sumo, sumolib, optional zmq/grpc).

Run::

    python -m SIMULATION.distributed.lan.setup
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from SIMULATION.distributed.lan.hosts import load_config  # noqa: E402
from SIMULATION.distributed.lan.ssh import Ssh, install_public_key  # noqa: E402

PUBKEY_PATH = Path.home() / ".ssh" / "id_ed25519.pub"
KEY_PATH = Path.home() / ".ssh" / "id_ed25519"

PROBE = (
    "echo HOST=$(hostname); "
    "echo PY=$(python3 --version 2>&1); "
    "echo SUMO=$(sumo --version 2>&1 | head -1); "
    "python3 -c 'import sumolib; print(\"SUMOLIB=ok\")' 2>&1 | tail -1; "
    "python3 -c 'import zmq; print(\"ZMQ=\"+zmq.__version__)' 2>&1 | tail -1; "
    "python3 -c 'import grpc; print(\"GRPC=\"+grpc.__version__)' 2>&1 | tail -1; "
    "echo NPROC=$(nproc); "
    "echo MEM=$(free -m 2>/dev/null | awk '/Mem:/{print $2\"MB\"}'); "
    "echo SUMO_HOME=${SUMO_HOME:-unset}"
)


def main() -> int:
    cfg = load_config()
    if not PUBKEY_PATH.exists():
        print(f"[setup] ERROR: public key not found at {PUBKEY_PATH}. "
              f"Generate it with: ssh-keygen -t ed25519")
        return 2
    pubkey = PUBKEY_PATH.read_text(encoding="utf-8").strip()
    print(f"[setup] hosts={cfg.hosts}")
    print(f"[setup] master={cfg.master} user={cfg.user}")

    ok_hosts = []
    for host in cfg.hosts:
        print(f"\n=== {host} ===")
        # 1) install key with password (idempotent)
        try:
            r = install_public_key(host, cfg.user, cfg.password, pubkey)
            print(f"[key] {r.out.strip() or r.err.strip()}")
        except Exception as e:
            print(f"[key] FAILED ({type(e).__name__}): {e}")
            continue
        # 2) probe environment using the key now
        try:
            with Ssh(host, cfg.user, key_filename=str(KEY_PATH)) as s:
                pr = s.run(PROBE, timeout=30)
                print(pr.out.strip())
                if pr.err.strip():
                    print("[probe-stderr]", pr.err.strip())
            ok_hosts.append(host)
        except Exception as e:
            print(f"[probe] FAILED ({type(e).__name__}): {e}")

    print(f"\n[setup] reachable+keyed hosts: {len(ok_hosts)}/{len(cfg.hosts)} "
          f"-> {ok_hosts}")
    return 0 if ok_hosts else 1


if __name__ == "__main__":
    sys.exit(main())
