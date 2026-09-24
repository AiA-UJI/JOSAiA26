"""Deploy the code + the data needed for the benchmark to every lab host.

Uploads (via sftp) a curated, minimal file set under ``~/<remote_dir>`` and
makes sure the remote Python can import everything:

* ``CLASS/*.py``
* ``SIMULATION/*.py`` (top level: main.py, CONSTANTS.py, AccidentManager.py...)
* ``SIMULATION/distributed/**/*.py`` (incl. transports/ and lan/)
* ``SIMULATION/roads/<net>.net.xml``
* the four trip files (8k/10k/12k/15k, ratio 99)

It also ``pip install --user`` the optional runtime deps (psutil) -- pyzmq and
grpcio are already present on the labs.

Run::

    python -m SIMULATION.distributed.lan.deploy
"""

from __future__ import annotations

import os
import posixpath
import sys
from pathlib import Path
from typing import List, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from SIMULATION.distributed.lan.hosts import load_config  # noqa: E402
from SIMULATION.distributed.lan.ssh import Ssh  # noqa: E402

KEY_PATH = str(Path.home() / ".ssh" / "id_ed25519")

NET_FILES = [
    "SIMULATION/roads/modified_4ways_all.net.xml",
    "SIMULATION/roads/rotterdam_arterial.net.xml",
]
NET_FILE = NET_FILES[0]  # backwards-compat
TRIP_FILES = [
    "SIMULATION/trips/fullnet_trips_intelligent_99.xml",
    "SIMULATION/trips/fullnet_trips_intelligent_99_8000.xml",
    "SIMULATION/trips/fullnet_trips_intelligent_99_12000.xml",
    "SIMULATION/trips/fullnet_trips_intelligent_99_15000.xml",
    "SIMULATION/trips/rotterdam_trips_intelligent_99_15000.xml",
    "SIMULATION/trips/rotterdam_trips_intelligent_99_20000.xml",
    "SIMULATION/trips/rotterdam_trips_intelligent_99_30000.xml",
]

REMOTE_DEPS = ["psutil"]


def _collect_files() -> List[str]:
    """Relative POSIX paths (from PROJECT_ROOT) to upload."""
    files: List[str] = []

    def add_py(rel_dir: str, recursive: bool):
        base = PROJECT_ROOT / rel_dir
        if not base.is_dir():
            return
        it = base.rglob("*.py") if recursive else base.glob("*.py")
        for p in it:
            # skip caches / private temp scripts
            if "__pycache__" in p.parts:
                continue
            files.append(p.relative_to(PROJECT_ROOT).as_posix())

    add_py("CLASS", recursive=False)
    add_py("SIMULATION", recursive=False)
    add_py("SIMULATION/distributed", recursive=True)

    files.extend(NET_FILES)
    files.extend(TRIP_FILES)

    # de-dup + keep existing
    out = []
    seen = set()
    for f in files:
        if f in seen:
            continue
        if (PROJECT_ROOT / f).is_file():
            out.append(f)
            seen.add(f)
        else:
            print(f"[deploy] WARN missing local file: {f}")
    return out


def deploy_host(host: str, user: str, remote_dir: str,
                files: List[str]) -> Tuple[str, bool, str]:
    try:
        with Ssh(host, user, key_filename=KEY_PATH, timeout=20) as s:
            s.run(f"mkdir -p '{remote_dir}'")
            for rel in files:
                local = str(PROJECT_ROOT / rel)
                remote = posixpath.join(remote_dir, rel)
                s.put(local, remote)
            # ensure package markers
            s.run(f"cd '{remote_dir}' && touch SIMULATION/__init__.py "
                  f"CLASS/__init__.py 2>/dev/null; true")
            # wipe stale bytecode caches so freshly uploaded sources are used
            s.run(f"find $HOME/{remote_dir} -name __pycache__ -type d "
                  f"-exec rm -rf {{}} + 2>/dev/null; true")
            # optional runtime deps
            dep = " ".join(REMOTE_DEPS)
            r = s.run(f"python3 -m pip install --user --quiet {dep} 2>&1 | tail -2; "
                      f"echo PIP_DONE", timeout=180)
            # sanity import with SUMO on path
            chk = s.run(
                'export PATH="$HOME/.local/bin:$PATH"; '
                "if [ -d /usr/share/sumo ]; then export SUMO_HOME=/usr/share/sumo; "
                "else export SUMO_HOME=$(ls -d $HOME/.local/lib/python3*/site-packages/sumo "
                "2>/dev/null | head -1); fi; "
                "export PYTHONPATH=$SUMO_HOME/tools:$HOME/" + remote_dir + ":$PYTHONPATH; "
                "cd $HOME/" + remote_dir + " && python3 -c "
                "'import sumolib, traci; "
                "from SIMULATION.distributed.partitioning import parse_partition; "
                "from SIMULATION.distributed.transports import make_client; "
                "import zmq, grpc; print(\"IMPORTS_OK\", \"zmq\", zmq.__version__, \"grpc\", grpc.__version__)'",
                timeout=60)
            ok = "IMPORTS_OK" in chk.out
            msg = (chk.out.strip() + " " + chk.err.strip()).strip()
            return host, ok, msg[-300:]
    except Exception as e:
        return host, False, f"{type(e).__name__}: {e}"


def main() -> int:
    cfg = load_config()
    files = _collect_files()
    total_mb = sum((PROJECT_ROOT / f).stat().st_size for f in files) / 1e6
    print(f"[deploy] {len(files)} files (~{total_mb:.1f} MB) -> {cfg.hosts}")
    print(f"[deploy] remote_dir=~/{cfg.remote_dir}")

    ok_hosts = []
    for host in cfg.hosts:
        print(f"\n=== deploy {host} ===")
        h, ok, msg = deploy_host(host, cfg.user, cfg.remote_dir, files)
        print(f"[{h}] {'OK' if ok else 'FAIL'} :: {msg}")
        if ok:
            ok_hosts.append(host)
    print(f"\n[deploy] success {len(ok_hosts)}/{len(cfg.hosts)} -> {ok_hosts}")
    return 0 if len(ok_hosts) == len(cfg.hosts) else 1


if __name__ == "__main__":
    sys.exit(main())
