"""
Distributed worker daemon.

Lifecycle::

    1. Probe local hardware (psutil) and pick a stable worker_id.
    2. POST /api/register to the master.
    3. Loop forever:
        a. POST /api/pull_job; if 204, sleep and retry.
        b. Spawn ``python -m SIMULATION.main`` as a subprocess with the
           job's args.
        c. Stream stdout, parsing ``[PROGRESS] step=X total=Y vehicles=Z``
           lines (emitted by main.py) to update master via heartbeats.
        d. On exit, zip the run's output directory and upload it.
        e. POST /api/complete with success/failure metadata.

Heartbeats run on a background thread and ping the master every
``DEFAULT_HEARTBEAT_INTERVAL`` seconds, regardless of whether a job is in
progress (idle workers also need to look alive on the dashboard).
"""

from __future__ import annotations

import argparse
import io
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from SIMULATION.distributed.protocol import (  # noqa: E402
    DEFAULT_HEARTBEAT_INTERVAL,
    DEFAULT_JOB_POLL_INTERVAL,
    DEFAULT_MASTER_PORT,
    EP_COMPLETE,
    EP_HEARTBEAT,
    EP_PULL_JOB,
    EP_REGISTER,
    EP_TRIPS_PREFIX,
    EP_UPLOAD,
    HeartbeatPayload,
    JobConfig,
    JobResult,
    WorkerInfo,
    WorkerStatus,
)


# ==================== Hardware detection ====================

def _detect_hardware() -> Dict[str, float]:
    cpu_count = os.cpu_count() or 1
    ram_mb = 0.0
    try:
        import psutil  # type: ignore
        ram_mb = psutil.virtual_memory().total / (1024 * 1024)
    except ImportError:
        pass
    return {"cpu_count": cpu_count, "ram_mb": ram_mb}


def _detect_sumo_version() -> str:
    sumo = shutil_which("sumo")
    if not sumo:
        return "not-found"
    try:
        out = subprocess.check_output([sumo, "--version"], stderr=subprocess.STDOUT, timeout=5)
        line = out.decode("utf-8", errors="ignore").splitlines()[0]
        return line.strip()[:80]
    except Exception:
        return "unknown"


def shutil_which(name: str) -> Optional[str]:
    return shutil.which(name)


def _detect_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
        except Exception:
            ip = "127.0.0.1"
        finally:
            s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ==================== HTTP helpers ====================

class MasterClient:
    """Thin HTTP wrapper around the master URL."""

    def __init__(self, base_url: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _post_json(self, path: str, payload: dict) -> Tuple[int, Optional[dict]]:
        body = json.dumps(payload).encode("utf-8")
        req = urllib_request.Request(
            self.base_url + path,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib_request.urlopen(req, timeout=self.timeout) as resp:
                status = resp.status
                raw = resp.read()
                if not raw:
                    return status, None
                try:
                    return status, json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    return status, None
        except HTTPError as e:
            return e.code, None
        except URLError as e:
            raise ConnectionError(f"master unreachable at {self.base_url}: {e}") from e

    def register(self, info: WorkerInfo) -> bool:
        status, _ = self._post_json(EP_REGISTER, info.to_dict())
        return status == 200

    def heartbeat(self, hb: HeartbeatPayload) -> bool:
        try:
            status, _ = self._post_json(EP_HEARTBEAT, hb.to_dict())
            return status == 200
        except ConnectionError:
            return False

    def pull_job(self, worker_id: str) -> Optional[JobConfig]:
        status, data = self._post_json(EP_PULL_JOB, {"worker_id": worker_id})
        if status != 200 or not data or not data.get("job"):
            return None
        return JobConfig.from_dict(data["job"])

    def complete(self, result: JobResult) -> bool:
        status, _ = self._post_json(EP_COMPLETE, result.to_dict())
        return status == 200

    def download_trips(self, job_id: str, dest_path: Path) -> Tuple[bool, Optional[str]]:
        """GET /api/trips/<job_id> and save to dest_path.

        Returns (success, error). On success, dest_path contains the XML bytes.
        """
        url = self.base_url + EP_TRIPS_PREFIX + job_id
        try:
            with urllib_request.urlopen(url, timeout=120.0) as resp:
                if resp.status != 200:
                    return False, f"status={resp.status}"
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                with dest_path.open("wb") as fp:
                    shutil.copyfileobj(resp, fp)
            return True, None
        except HTTPError as e:
            return False, f"http {e.code}"
        except (URLError, OSError) as e:
            return False, str(e)

    def upload(self, job_id: str, worker_id: str, zip_path: Path) -> bool:
        size = zip_path.stat().st_size
        with zip_path.open("rb") as fp:
            req = urllib_request.Request(
                self.base_url + EP_UPLOAD,
                data=fp.read(),
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(size),
                    "X-Job-Id": job_id,
                    "X-Worker-Id": worker_id,
                },
                method="POST",
            )
            try:
                with urllib_request.urlopen(req, timeout=300.0) as resp:
                    return resp.status == 200
            except (HTTPError, URLError) as e:
                print(f"[worker] upload failed: {e}")
                return False


# ==================== Job runner ====================

PROGRESS_RE = re.compile(
    r"\[PROGRESS\]\s+step=(?P<step>\d+)\s+"
    r"total=(?P<total>\d+)\s+"
    r"vehicles=(?P<veh>\d+)"
)


class WorkerState:
    """Mutable state shared between heartbeat thread and main loop."""

    def __init__(self):
        self.lock = threading.RLock()
        self.status = WorkerStatus.IDLE.value
        self.current_job_id: Optional[str] = None
        self.current_step = 0
        self.total_steps = 0
        self.current_vehicles = 0
        self.last_log_line = ""
        self.stop_event = threading.Event()

    def set_idle(self):
        with self.lock:
            self.status = WorkerStatus.IDLE.value
            self.current_job_id = None
            self.current_step = 0
            self.total_steps = 0
            self.current_vehicles = 0
            self.last_log_line = ""

    def set_busy(self, job_id: str):
        with self.lock:
            self.status = WorkerStatus.BUSY.value
            self.current_job_id = job_id
            self.current_step = 0
            self.total_steps = 0
            self.current_vehicles = 0

    def update_progress(self, step: int, total: int, vehicles: int, line: str):
        with self.lock:
            self.current_step = step
            self.total_steps = total
            self.current_vehicles = vehicles
            self.last_log_line = line[:200]

    def heartbeat_payload(self, worker_id: str) -> HeartbeatPayload:
        with self.lock:
            return HeartbeatPayload(
                worker_id=worker_id,
                status=self.status,
                current_job_id=self.current_job_id,
                current_step=self.current_step,
                total_steps=self.total_steps,
                current_vehicles=self.current_vehicles,
                last_log_line=self.last_log_line,
            )


def _heartbeat_loop(client: MasterClient, worker_id: str, ws: WorkerState,
                    interval: float) -> None:
    while not ws.stop_event.is_set():
        try:
            client.heartbeat(ws.heartbeat_payload(worker_id))
        except Exception as e:
            print(f"[worker] heartbeat error: {e}")
        ws.stop_event.wait(interval)


def _resolve_trips_file(cfg: JobConfig, client: "MasterClient",
                        job_output_dir: Path) -> Optional[str]:
    """Make a trips XML accessible locally for this worker.

    Strategy (in order):
      1. Try to download from master via /api/trips/<job_id>.
      2. If that fails AND the master-provided absolute path exists locally
         (e.g. shared NFS / SMB mount), use it as-is.
      3. Otherwise return None and the simulation will fail clearly.
    """
    if not cfg.custom_trips_file:
        # Batch mode con fichero de trips por nombre: resolver contra el
        # arbol local del worker (SIMULATION/trips/<nombre>). Compatible con
        # main.py antiguos que no tienen --trips (se inyecta por env).
        if cfg.trips:
            cand = PROJECT_ROOT / "SIMULATION" / "trips" / cfg.trips
            if cand.is_file():
                print(f"[worker]   trips resolved locally: {cand}")
                return str(cand)
            print(f"[worker]   ERROR: trips file not found locally: {cand}")
        return None

    local_trips = job_output_dir / "trips.xml"
    ok, err = client.download_trips(cfg.job_id, local_trips)
    if ok:
        print(f"[worker]   trips downloaded from master: {local_trips}")
        return str(local_trips)

    if os.path.isfile(cfg.custom_trips_file):
        print(f"[worker]   trips download failed ({err}); falling back to "
              f"shared path {cfg.custom_trips_file}")
        return cfg.custom_trips_file

    print(f"[worker]   ERROR: cannot obtain trips file (download err={err}, "
          f"path missing: {cfg.custom_trips_file})")
    return cfg.custom_trips_file  # let main.py error out clearly


def _run_simulation(cfg: JobConfig, ws: WorkerState, output_root: Path,
                    client: "MasterClient") -> Tuple[bool, Optional[Path], Optional[str]]:
    """Run main.py for one job and return (success, output_dir, error)."""

    cmd = [sys.executable, "-m", "SIMULATION.main", *cfg.to_main_py_args()]

    # Force a per-job output dir so we can locate the result later
    job_output_dir = output_root / cfg.job_id
    job_output_dir.mkdir(parents=True, exist_ok=True)

    # Trips override (used by spatial sub-jobs). The master serves the
    # corridor-specific XML over HTTP; we save it locally and point main.py
    # to that local copy.
    env = os.environ.copy()
    trips_path = _resolve_trips_file(cfg, client, job_output_dir)
    if trips_path:
        env["VEHICLEKNOWLEDGE_TRIPS_OVERRIDE"] = trips_path
    if cfg.spatial_corridor:
        env["VEHICLEKNOWLEDGE_SPATIAL_CORRIDOR"] = cfg.spatial_corridor
    env["VEHICLEKNOWLEDGE_OUTPUT_OVERRIDE"] = str(job_output_dir)

    print(f"[worker] starting job {cfg.job_id} ({cfg.short_label()})")
    print(f"[worker]   cmd: {' '.join(cmd)}")
    print(f"[worker]   cwd: {PROJECT_ROOT}")
    print(f"[worker]   output: {job_output_dir}")

    proc = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        bufsize=1,
        text=True,
    )

    try:
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            line = raw_line.rstrip("\n")
            if not line:
                continue
            print(f"[job {cfg.job_id[:6]}] {line}")
            m = PROGRESS_RE.search(line)
            if m:
                ws.update_progress(
                    int(m.group("step")),
                    int(m.group("total")),
                    int(m.group("veh")),
                    line,
                )
            else:
                ws.update_progress(
                    ws.current_step, ws.total_steps, ws.current_vehicles, line
                )
        rc = proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        proc.wait()
        return False, None, "interrupted by user"

    if rc != 0:
        return False, job_output_dir, f"main.py exited with code {rc}"

    return True, job_output_dir, None


def _zip_output(output_dir: Path, zip_path: Path) -> int:
    """Create a zip of output_dir contents (recursive). Returns size in bytes."""
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(output_dir):
            for fname in files:
                fpath = Path(root) / fname
                arcname = Path("result") / fpath.relative_to(output_dir)
                zf.write(fpath, arcname=str(arcname))
    return zip_path.stat().st_size


# ==================== Main worker loop ====================

def run_worker(master_url: str,
               worker_label: Optional[str] = None,
               poll_interval: float = DEFAULT_JOB_POLL_INTERVAL,
               heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
               output_root: Optional[Path] = None) -> int:
    output_root = output_root or (PROJECT_ROOT / "SIMULATION" / "distributed" / "worker_runs")
    output_root.mkdir(parents=True, exist_ok=True)

    hardware = _detect_hardware()
    info = WorkerInfo(
        worker_id=worker_label or f"{platform.node().lower()}_{uuid.uuid4().hex[:6]}",
        hostname=platform.node(),
        ip=_detect_local_ip(),
        cpu_count=int(hardware["cpu_count"]),
        ram_mb=float(hardware["ram_mb"]),
        sumo_version=_detect_sumo_version(),
    )
    print(f"[worker] hardware: cpu={info.cpu_count} ram={info.ram_mb:.0f}MB ip={info.ip}")
    print(f"[worker] worker_id: {info.worker_id}")

    client = MasterClient(master_url)

    # Register (retry until success)
    while True:
        try:
            if client.register(info):
                print(f"[worker] registered with master at {master_url}")
                break
        except ConnectionError as e:
            print(f"[worker] register failed: {e}; retrying in 5s")
        time.sleep(5)

    ws = WorkerState()
    ws.set_idle()
    hb_thread = threading.Thread(
        target=_heartbeat_loop,
        args=(client, info.worker_id, ws, heartbeat_interval),
        name="worker-heartbeat",
        daemon=True,
    )
    hb_thread.start()

    try:
        while not ws.stop_event.is_set():
            try:
                cfg = client.pull_job(info.worker_id)
            except ConnectionError as e:
                print(f"[worker] master unreachable: {e}; retrying in 5s")
                time.sleep(5)
                continue

            if cfg is None:
                ws.set_idle()
                time.sleep(poll_interval)
                continue

            ws.set_busy(cfg.job_id)
            t_start = time.perf_counter()
            success = False
            error: Optional[str] = None
            output_dir: Optional[Path] = None

            try:
                success, output_dir, error = _run_simulation(cfg, ws, output_root, client)
            except Exception:
                error = traceback.format_exc()
                success = False

            duration = time.perf_counter() - t_start

            # Upload result
            archive_size = 0
            if output_dir and output_dir.is_dir():
                try:
                    zip_path = output_dir.parent / f"{cfg.job_id}.zip"
                    archive_size = _zip_output(output_dir, zip_path)
                    if not client.upload(cfg.job_id, info.worker_id, zip_path):
                        if success:
                            success = False
                            error = "upload failed"
                except Exception:
                    error = traceback.format_exc()
                    success = False

            try:
                client.complete(JobResult(
                    job_id=cfg.job_id,
                    worker_id=info.worker_id,
                    success=success,
                    duration_sec=duration,
                    output_size_bytes=archive_size,
                    error=error,
                    metadata={
                        "spatial_corridor": cfg.spatial_corridor,
                        "parent_sim_id": cfg.parent_sim_id,
                    },
                ))
            except Exception as e:
                print(f"[worker] complete-notify failed: {e}")

            ws.set_idle()
            print(f"[worker] job {cfg.job_id} {'OK' if success else 'FAIL'} in {duration:.1f}s")

    except KeyboardInterrupt:
        print("\n[worker] shutting down")
    finally:
        ws.stop_event.set()
        hb_thread.join(timeout=2.0)

    return 0


# ==================== CLI ====================

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="VehicleKnowledge distributed worker")
    parser.add_argument("--master", required=True,
                        help="Master URL (e.g. http://192.168.1.10:9000 or 192.168.1.10:9000)")
    parser.add_argument("--label", default=None,
                        help="Optional worker label/id (default: hostname-randomhex)")
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_JOB_POLL_INTERVAL)
    parser.add_argument("--heartbeat-interval", type=float, default=DEFAULT_HEARTBEAT_INTERVAL)
    parser.add_argument("--output-root", default=None)
    args = parser.parse_args(argv)

    master_url = args.master
    if not master_url.startswith("http://") and not master_url.startswith("https://"):
        master_url = "http://" + master_url
    if ":" not in master_url.replace("http://", "").replace("https://", ""):
        master_url = master_url + f":{DEFAULT_MASTER_PORT}"

    return run_worker(
        master_url=master_url,
        worker_label=args.label,
        poll_interval=args.poll_interval,
        heartbeat_interval=args.heartbeat_interval,
        output_root=Path(args.output_root) if args.output_root else None,
    )


if __name__ == "__main__":
    sys.exit(main())
