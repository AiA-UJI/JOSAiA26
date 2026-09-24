"""Instala eclipse-sumo==1.24.0 (pip --user) en todos los nodos y verifica."""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from SIMULATION.distributed.lan.run_simpatizantes import load_cfg, KEY_PATH  # noqa: E402
from SIMULATION.distributed.lan.cluster import Ssh, env_prefix  # noqa: E402

INSTALL = ("python3 -m pip install --user --quiet 'eclipse-sumo==1.24.0' "
           "2>&1 | tail -2; true")

cfg = load_cfg()
VERIFY = (env_prefix(cfg.remote_dir) +
          " which sumo && sumo --version 2>/dev/null | head -1 && "
          "python3 -c 'import traci, sumolib; print(\"traci\", traci.__file__)'")

fail = []
for h in cfg.hosts:
    print(f"===== {h} =====", flush=True)
    try:
        with Ssh(h, cfg.user, key_filename=KEY_PATH, timeout=600) as s:
            r1 = s.run(INSTALL, timeout=560)
            print("install:", (r1.out or "").strip()[-200:], (r1.err or "").strip()[-200:])
            r2 = s.run(VERIFY, timeout=60)
            print("verify :", (r2.out or "").strip())
            if "1.24.0" not in (r2.out or ""):
                fail.append(h)
    except Exception as e:
        print("ERROR:", e)
        fail.append(h)

print("\nRESULT:", "OK todos con 1.24.0" if not fail else f"FALLAN: {fail}")
sys.exit(1 if fail else 0)
