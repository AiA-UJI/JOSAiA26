"""Inspecciona como esta instalado SUMO en los nodos."""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from SIMULATION.distributed.lan.run_simpatizantes import load_cfg, KEY_PATH  # noqa: E402
from SIMULATION.distributed.lan.cluster import Ssh  # noqa: E402

CMD = (
    "which sumo; sumo --version 2>/dev/null | head -2; "
    "python3 -c 'import sumolib, os; print(sumolib.__file__); "
    "print(\"SUMO_HOME=\", os.environ.get(\"SUMO_HOME\"))'; "
    "pip3 list 2>/dev/null | grep -iE 'sumo|traci'"
)

cfg = load_cfg()
for h in cfg.hosts:
    print(f"===== {h} =====")
    try:
        with Ssh(h, cfg.user, key_filename=KEY_PATH, timeout=25) as s:
            print(s.run(CMD))
    except Exception as e:
        print("ERROR:", e)
