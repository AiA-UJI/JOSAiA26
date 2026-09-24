"""Lifecycle + control of the distributed cluster over SSH.

All HTTP interaction with the master is performed *on the master host itself*
(via a short python3 urllib snippet executed through SSH), so it works even if
the master's HTTP port is not reachable from the control PC's network. Workers
reach the master over the lab LAN.
"""

from __future__ import annotations

import json
import shlex
import time
from pathlib import Path
from typing import Dict, List, Optional

from SIMULATION.distributed.lan.hosts import LanConfig
from SIMULATION.distributed.lan.ssh import Ssh

KEY_PATH = str(Path.home() / ".ssh" / "id_ed25519")
MASTER_PORT = 9000
LAN_DIR = "_lan"   # under remote_dir: logs + run artefacts


def env_prefix(remote_dir: str) -> str:
    # PRIORIDAD: SUMO de pip --user (version fijada, p.ej. eclipse-sumo==1.24.0
    # para reproducir el paper) sobre el del sistema (/usr/share/sumo, 1.27).
    # ~/.local/bin va primero en PATH para que shutil.which("sumo") coja el pip.
    return (
        'export PATH="$HOME/.local/bin:$PATH"; '
        'export SUMO_HOME=$(ls -d $HOME/.local/lib/python3*/site-packages/sumo '
        '2>/dev/null | head -1); '
        'if [ -z "$SUMO_HOME" ] && [ -d /usr/share/sumo ]; then '
        'export SUMO_HOME=/usr/share/sumo; fi; '
        f"export PYTHONPATH=$SUMO_HOME/tools:$HOME/{remote_dir}:$PYTHONPATH; "
        f"cd $HOME/{remote_dir};"
    )


def _ssh(host: str, cfg: LanConfig) -> Ssh:
    return Ssh(host, cfg.user, key_filename=KEY_PATH, timeout=20).connect()


def _py_http_snippet(method: str, path: str, payload: Optional[dict],
                     http_timeout: int = 30) -> str:
    """Return a python3 one-liner that calls the local master and prints JSON."""
    body = "None" if payload is None else repr(json.dumps(payload))
    return (
        "python3 - <<'PYEOF'\n"
        "import json,urllib.request\n"
        f"url='http://127.0.0.1:{MASTER_PORT}{path}'\n"
        f"data={body}\n"
        "req=urllib.request.Request(url, data=(data.encode() if data else None),"
        " headers={'Content-Type':'application/json'},"
        f" method='{method}')\n"
        "try:\n"
        f"    r=urllib.request.urlopen(req, timeout={http_timeout}); print(r.read().decode())\n"
        "except Exception as e:\n"
        "    print(json.dumps({'__error__':str(e)}))\n"
        "PYEOF"
    )


class Cluster:
    def __init__(self, cfg: LanConfig):
        self.cfg = cfg
        self.master_host = cfg.master
        self.master_url = f"{cfg.master}:{MASTER_PORT}"
        self._m: Optional[Ssh] = None

    # ---- connections ----
    def master_ssh(self) -> Ssh:
        if self._m is None:
            self._m = _ssh(self.master_host, self.cfg)
        return self._m

    # ---- lifecycle ----
    def start_master(self, transports: str = "tcp,udp,zmq,grpc") -> str:
        s = self.master_ssh()
        log = f"$HOME/{self.cfg.remote_dir}/{LAN_DIR}/master.log"
        cmd = (env_prefix(self.cfg.remote_dir) +
               " python3 -m SIMULATION.distributed.master "
               f"--host 0.0.0.0 --port {MASTER_PORT} --transports {transports}")
        r = s.start_background(cmd, log)
        return r.out.strip()

    def start_workers(self) -> Dict[str, str]:
        pids: Dict[str, str] = {}
        for host in self.cfg.hosts:
            s = _ssh(host, self.cfg)
            try:
                log = f"$HOME/{self.cfg.remote_dir}/{LAN_DIR}/worker.log"
                cmd = (env_prefix(self.cfg.remote_dir) +
                       " python3 -m SIMULATION.distributed.worker "
                       f"--master http://{self.master_url} --label {host}")
                r = s.start_background(cmd, log)
                pids[host] = r.out.strip()
            finally:
                s.close()
        return pids

    def stop_all(self) -> None:
        for host in self.cfg.hosts:
            try:
                s = _ssh(host, self.cfg)
                # The bracket trick ([S]...) keeps these patterns from matching
                # the very shell that runs them (its argv contains the literal
                # bracketed string), which previously killed this command mid
                # way and left the master alive.
                s.run("pkill -f '[S]IMULATION.distributed.worker' 2>/dev/null; "
                      "pkill -f '[S]IMULATION.distributed.master' 2>/dev/null; "
                      "pkill -f '[S]IMULATION/main.py' 2>/dev/null; "
                      "find $HOME -maxdepth 1 -name '$HOME' -exec rm -rf {} + "
                      "2>/dev/null; true")
                s.close()
            except Exception:
                pass
        self._m = None

    # ---- master HTTP (executed on master host) ----
    def status(self) -> dict:
        # Robusto ante caidas SSH transitorias ('SSH session not active'):
        # reconecta y reintenta unas veces antes de rendirse, para no tumbar
        # todo el barrido por un glitch de red durante un poll de wait_parent.
        last_err = ""
        for attempt in range(4):
            try:
                s = self.master_ssh()
                r = s.run(_py_http_snippet("GET", "/api/status", None),
                          timeout=40)
                try:
                    return json.loads(r.out.strip().splitlines()[-1])
                except Exception:
                    return {"__error__": r.out.strip() or r.err.strip()}
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                # forzar reconexion del canal maestro en el siguiente intento
                try:
                    if self._m is not None:
                        self._m.close()
                except Exception:
                    pass
                self._m = None
                time.sleep(3.0 * (attempt + 1))
        return {"__error__": last_err or "status unreachable"}

    def submit(self, payload: dict) -> dict:
        # /api/submit computes the partition synchronously on the master. On
        # big nets (Rotterdam, 22k edges) a cold balanced cut takes minutes,
        # so allow a long HTTP read before giving up.
        s = self.master_ssh()
        r = s.run(_py_http_snippet("POST", "/api/submit", payload,
                                   http_timeout=900), timeout=960)
        try:
            return json.loads(r.out.strip().splitlines()[-1])
        except Exception:
            return {"__error__": r.out.strip() or r.err.strip()}

    def wait_master(self, timeout: float = 30.0) -> bool:
        t0 = time.time()
        while time.time() - t0 < timeout:
            st = self.status()
            if "__error__" not in st:
                return True
            time.sleep(1.5)
        return False

    def wait_workers(self, n: int, timeout: float = 60.0) -> int:
        t0 = time.time()
        alive = 0
        while time.time() - t0 < timeout:
            st = self.status()
            ws = st.get("workers", []) if isinstance(st, dict) else []
            alive = sum(1 for w in ws if w.get("alive"))
            if alive >= n:
                return alive
            time.sleep(2.0)
        return alive

    def wait_parent(self, parent_id: str, timeout: float = 3600.0,
                    poll: float = 5.0) -> dict:
        """Block until the parent sim aggregates / fails. Returns its record."""
        t0 = time.time()
        last = {}
        while time.time() - t0 < timeout:
            st = self.status()
            parents = st.get("parent_sims", {}) if isinstance(st, dict) else {}
            p = parents.get(parent_id)
            if p:
                last = p
                if p.get("status") in ("aggregated", "partial_failure",
                                       "aggregation_error"):
                    return p
            time.sleep(poll)
        last["status"] = last.get("status", "timeout")
        return last

    # ---- baseline (direct run, no master) ----
    def run_baseline(self, host: str, trips_filename: str, vehicles: int,
                     accident: bool, out_subdir: str,
                     max_steps: int = 0, timeout: float = 7200.0,
                     road: str = "modified_4ways_all",
                     save_environment: bool = False) -> dict:
        """Run a single non-distributed sim and return its recorded runtime.

        When ``save_environment`` is True the per-edge ``environment_traffic``
        (SUMO ``<edgeData>``) is kept -- needed for flow-equivalence checks.
        """
        s = _ssh(host, self.cfg)
        try:
            # Relative to remote_dir (env_prefix cd's there) -> avoids $HOME
            # expansion pitfalls inside quoted strings.
            out_dir = f"{LAN_DIR}/{out_subdir}"
            acc = "--accident" if accident else ""
            ms = f"--max-steps {max_steps}" if max_steps else ""
            env_flag = "" if save_environment else "--no-save-environment"
            cmd = (
                env_prefix(self.cfg.remote_dir) +
                f" VEHICLEKNOWLEDGE_OUTPUT_OVERRIDE={out_dir} "
                "python3 SIMULATION/main.py "
                f"--road {road} "
                f"--trips {trips_filename} --vehicles {vehicles} {acc} {ms} "
                f"--no-save-knowledge {env_flag}"
            )
            t0 = time.time()
            r = s.run(cmd, timeout=timeout)
            wall = time.time() - t0
            info = s.run(env_prefix(self.cfg.remote_dir) +
                         f" cat {out_dir}/simulation_info.json 2>/dev/null")
            runtime = None
            try:
                d = json.loads(info.out)
                runtime = d.get("aggregated_runtime_sec") or d.get("actual_runtime_sec")
            except Exception:
                pass
            return {
                "wall_sec": round(wall, 1),
                "runtime_sec": runtime,
                "rc": r.rc,
                "tail": (r.out[-400:] + r.err[-200:]).strip(),
            }
        finally:
            s.close()

    # ---- fetch artefacts ----
    def fetch_aggregated(self, remote_dir: str, local_dir: str) -> int:
        s = self.master_ssh()
        # remote_dir is absolute on master (from parent record)
        return s.get_dir(remote_dir, local_dir)


__all__ = ["Cluster", "env_prefix", "MASTER_PORT", "LAN_DIR", "KEY_PATH"]
