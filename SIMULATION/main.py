#!/usr/bin/env python3
"""
Unified SUMO Simulation with Vehicle Knowledge System
======================================================

This is the main entry point for running traffic simulations with vehicle
knowledge tracking and optional V2V (Vehicle-to-Vehicle) sharing.

Modes:
------
- none:   Each vehicle records only its own observations (isolated)
- v2v:    Vehicles share knowledge when within proximity radius
- global: All vehicles share with all others (for debugging/comparison)

Examples:
---------
    # Run with simple test network
    python main.py --road simple
    
    # With accident and GUI
    python main.py --road simple --accident --gui
    
    # Enable V2V sharing
    python main.py --road modified_4ways_all --sharing v2v
    
    # See available roads
    python main.py --list-roads
"""

import sys
import os
import time
import shutil
import argparse
import math
import hashlib
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional, Set
import json
import xml.etree.ElementTree as ET

# Add project root to path
SCRIPT_DIR = Path(__file__).parent.absolute()
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

import traci
import traci.constants as tc

from SIMULATION.CONSTANTS import (
    DEFAULTS, ROAD_CONFIG, SIMULATION_DIR,
    get_road_config, get_trips_file, get_network_file, discover_roads,
    discover_trips_for_road,
)
from SIMULATION.AccidentManager import AccidentManager
from CLASS.VehicleKnowledge import VehicleKnowledge, TrafficData
from CLASS.Vehicle import (
    Vehicle, NormalVehicle, IntelligentVehicle, EcoIntelligentVehicle,
    EdgeCO2Costs,
)
from CLASS.metrics import PerformanceMonitor, track_performance


# ==================== CONFIGURATION ====================
# SUMO-GUI: load native view scheme (vehicles bigger + show IDs) instead of "standard"
SUMO_GUI_SETTINGS_FILE = SCRIPT_DIR / "config" / "sumo_gui_bigger_with_ids.xml"

CIRCLE_SEGMENTS = 32  # Smoothness of circle polygons

STATE_COLORS = {
    'normal':         {'vehicle': (255, 0, 0, 255),     'circle': (255, 0, 0, 100)},
    'intelligent':    {'vehicle': (0, 255, 0, 255),     'circle': (0, 255, 0, 100)},
    'eco':            {'vehicle': (180, 0, 220, 255),   'circle': (180, 0, 220, 100)},
    'sharing':        {'vehicle': (0, 255, 255, 255),   'circle': (0, 255, 255, 150)},
    'in_range':       {'vehicle': (255, 255, 0, 255),   'circle': (255, 255, 0, 100)},
}
# Simplified mapping used for fallback paths and dashboard markers.
VEHICLE_TYPE_COLOR = {
    True: (0, 255, 0, 255),    # intelligent
    False: (255, 0, 0, 255),   # normal
}
# Circles use the same hue as the vehicle; alpha hints proximity / recent share (V2V only).
CIRCLE_ALPHA_BY_PROXIMITY = {
    "sharing": 70,   # recently exchanged with someone in range
    "in_range": 50,  # neighbor in radius, cooldown not fresh
    "idle": 25,      # default / no neighbor
}
EDGE_DATA_PERIOD_SECONDS = 300
ENVIRONMENT_SLOT_SECONDS = 300
DEFAULT_SHARE_BUDGET = 20
DEFAULT_MAX_SHARE_AGE_SECONDS = 900
DEFAULT_MAX_V2V_PAIRS_PER_STEP = 2000
DEFAULT_DASHBOARD_UPDATE_INTERVAL = 60
DEFAULT_SHARE_CANDIDATE_THRESHOLD = 0.75

try:
    import psutil
except ImportError:
    psutil = None


class ResourceMonitor:
    """Samples this Python process plus SUMO child processes during a run."""

    def __init__(self, interval: float = 1.0):
        self.interval = interval
        self.max_cpu_percent = 0.0
        self.peak_rss_bytes = 0
        self.samples = 0
        self.timeline = []
        self._start_time = None
        self._stop = threading.Event()
        self._thread = None
        self._process = psutil.Process(os.getpid()) if psutil else None
        self._pid = os.getpid()
        self._has_psutil = psutil is not None

    def start(self):
        self._start_time = time.perf_counter()
        # prime counters for psutil path, otherwise proceed with fallback
        if self._has_psutil:
            self._prime_cpu_counters()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        if not self._process:
            return
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.interval + 0.5)

    def _live_processes(self):
        if self._has_psutil:
            procs = [self._process]
            try:
                procs.extend(self._process.children(recursive=True))
            except psutil.Error:
                pass
            return [p for p in procs if p.is_running()]

        # Fallback: create a minimal dummy process wrapper for Windows (or other OS)
        class _DummyMem:
            def __init__(self, rss):
                self.rss = rss

        class _DummyProc:
            def __init__(self, pid):
                self.pid = pid

            def is_running(self):
                return True

            def name(self):
                return os.path.basename(sys.executable)

            def cpu_percent(self, _):
                # Approximate: on Windows, get overall CPU load via wmic (percent)
                try:
                    if os.name == 'nt':
                        out = os.popen('wmic cpu get loadpercentage').read()
                        for line in out.splitlines():
                            line = line.strip()
                            if not line:
                                continue
                            # pick first token that looks numeric
                            tok = ''.join(ch for ch in line if (ch.isdigit() or ch in ".-,"))
                            tok = tok.replace(',', '').replace('.', '')
                            if tok.isdigit():
                                try:
                                    return float(tok)
                                except Exception:
                                    continue
                    # Fallback to 0.0
                except Exception:
                    pass
                return 0.0

            def memory_info(self):
                # Try tasklist to get memory in KB for this PID
                try:
                    if os.name == 'nt':
                        cmd = f'tasklist /FI "PID eq {self.pid}" /FO CSV /NH'
                        out = os.popen(cmd).read()
                        if out:
                            # Extract the first numeric token (handles localized separators)
                            import re
                            match = re.search(r"([0-9][0-9\.,]*)\s*KB", out, re.IGNORECASE)
                            if match:
                                mem_str = match.group(1)
                                mem_str = mem_str.replace('.', '').replace(',', '').strip()
                                try:
                                    kb = float(mem_str)
                                    return _DummyMem(kb * 1024)
                                except Exception:
                                    pass
                except Exception:
                    pass
                return _DummyMem(0)

        return [_DummyProc(self._pid)]

    def _prime_cpu_counters(self):
        for proc in self._live_processes():
            try:
                proc.cpu_percent(None)
            except psutil.Error:
                pass

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        idx = (len(ordered) - 1) * percentile
        low = int(idx)
        high = min(low + 1, len(ordered) - 1)
        if low == high:
            return ordered[low]
        return ordered[low] + (ordered[high] - ordered[low]) * (idx - low)

    def _sample(self):
        total_cpu = 0.0
        total_rss = 0
        per_process = []
        for proc in self._live_processes():
            try:
                cpu_percent = proc.cpu_percent(None)
                rss_bytes = proc.memory_info().rss
                total_cpu += cpu_percent
                total_rss += rss_bytes
                per_process.append({
                    "pid": proc.pid,
                    "name": proc.name(),
                    "cpu_percent": round(cpu_percent, 2),
                    "rss_mb": round(rss_bytes / (1024 * 1024), 2),
                })
            except Exception:
                # If psutil isn't available, exceptions may be generic
                continue
        self.max_cpu_percent = max(self.max_cpu_percent, total_cpu)
        self.peak_rss_bytes = max(self.peak_rss_bytes, total_rss)
        self.samples += 1
        elapsed = 0.0
        if self._start_time is not None:
            elapsed = time.perf_counter() - self._start_time
        self.timeline.append({
            "elapsed_wall_s": round(elapsed, 3),
            "cpu_percent": round(total_cpu, 2),
            "rss_mb": round(total_rss / (1024 * 1024), 2),
            "processes": per_process,
        })

    def _run(self):
        while not self._stop.wait(self.interval):
            self._sample()
        self._sample()

    @property
    def peak_rss_mb(self) -> float:
        return self.peak_rss_bytes / (1024 * 1024)

    def as_dict(self, wall_time_seconds: float, sim_time_seconds: float = None,
                sim_steps: int = None, vehicle_counts: dict = None) -> dict:
        cpu_values = [sample["cpu_percent"] for sample in self.timeline]
        ram_values = [sample["rss_mb"] for sample in self.timeline]
        avg_cpu = sum(cpu_values) / len(cpu_values) if cpu_values else 0.0
        avg_ram = sum(ram_values) / len(ram_values) if ram_values else 0.0
        duration = max(wall_time_seconds, 1e-9)
        return {
            "schema_version": 2,
            "process_scope": "python_process_plus_sumo_children",
            "sampling_interval_seconds": self.interval,
            "psutil_available": psutil is not None,
            "wall_time": {
                "seconds": round(wall_time_seconds, 3),
                "human": format_time(wall_time_seconds),
            },
            "simulation_time": {
                "seconds": round(sim_time_seconds, 3) if sim_time_seconds is not None else None,
                "steps": sim_steps,
                "sim_seconds_per_wall_second": round(sim_time_seconds / duration, 3)
                if sim_time_seconds is not None else None,
            },
            "vehicles": vehicle_counts or {},
            "cpu": {
                "max_percent": round(self.max_cpu_percent, 2),
                "avg_percent": round(avg_cpu, 2),
                "p50_percent": round(self._percentile(cpu_values, 0.50), 2),
                "p95_percent": round(self._percentile(cpu_values, 0.95), 2),
                "p99_percent": round(self._percentile(cpu_values, 0.99), 2),
            },
            "memory": {
                "peak_rss_mb": round(self.peak_rss_mb, 2),
                "avg_rss_mb": round(avg_ram, 2),
                "p50_rss_mb": round(self._percentile(ram_values, 0.50), 2),
                "p95_rss_mb": round(self._percentile(ram_values, 0.95), 2),
                "p99_rss_mb": round(self._percentile(ram_values, 0.99), 2),
                "start_rss_mb": ram_values[0] if ram_values else 0.0,
                "end_rss_mb": ram_values[-1] if ram_values else 0.0,
                "growth_mb": round((ram_values[-1] - ram_values[0]), 2) if len(ram_values) >= 2 else 0.0,
            },
            "samples": {
                "count": self.samples,
                "timeline": self.timeline,
            },
        }


# ==================== SIMULATION STATE ====================
class SimulationState:
    """Holds all mutable state for the simulation."""
    def __init__(self):
        self.step = 0
        self.sharing_events = 0
        self.previous_vehicles: Set[str] = set()
        self.vehicles: dict = {}              # {veh_id: Vehicle instance}
        self.shared_pairs: dict = {}          # {frozenset: step_when_shared}
        self.share_versions: dict = {}        # {(sender_id, receiver_id): last sender knowledge_version sent}
        self.sharing_attempted_observations = 0
        self.sharing_accepted_observations = 0
        self.sharing_skipped_observations = 0
        self.sharing_pairs_processed = 0
        self.sharing_pairs_deferred_by_cap = 0
        self.pair_distances: dict = {}        # {frozenset: distance}
        self.current_pairs_in_range: Set = set()
        # Vehicle type counters
        self.intelligent_count = 0
        self.eco_count = 0
        self.normal_count = 0
        # Per-vehicle trip data (lightweight tripinfo via TraCI)
        self.trip_depart_times: dict = {}     # {veh_id: depart_time}
        self.trip_cache: dict = {}            # {veh_id: {distance, waitingTime}}
        self.trip_data: list = []             # collected trip records
        self.knowledge_summaries: list = []   # lightweight per-intelligent-vehicle knowledge counts


# ==================== CIRCLE VISUALIZATION (V2V mode) ====================
def create_circle_points(x, y, radius):
    """Generate polygon points for a circle."""
    return [(x + radius * math.cos(2 * math.pi * i / CIRCLE_SEGMENTS),
             y + radius * math.sin(2 * math.pi * i / CIRCLE_SEGMENTS))
            for i in range(CIRCLE_SEGMENTS)]


def update_circle(veh_id, pos, radius, color):
    """Update or create circle polygon for a vehicle."""
    polygon_id = f"circle_{veh_id}"
    try:
        points = create_circle_points(pos[0], pos[1], radius)
        if polygon_id in traci.polygon.getIDList():
            traci.polygon.setShape(polygon_id, points)
            traci.polygon.setColor(polygon_id, color)
        else:
            traci.polygon.add(polygon_id, points, color=color, fill=True, layer=-1)
    except traci.TraCIException:
        pass


def remove_circle(veh_id):
    """Remove circle polygon for a vehicle."""
    polygon_id = f"circle_{veh_id}"
    try:
        if polygon_id in traci.polygon.getIDList():
            traci.polygon.remove(polygon_id)
    except traci.TraCIException:
        pass


# ==================== VEHICLE LIFECYCLE ====================
import random

def _is_intelligent_for_ratio(veh_id: str, ratio: int) -> bool:
    """Decisión determinista y monótona de inteligente/normal a partir del veh_id.

    - Determinista: el mismo veh_id siempre cae en el mismo "bucket" 0..99,
      independiente de la corrida, máquina o seed global.
    - Monótono: si veh_X es intelligent al ratio R, también lo es para todo R'>R.
      Esto garantiza que al barrer ratios (10, 20, 30…) los vehículos
      inteligentes "se acumulan" en lugar de cambiar arbitrariamente.
    """
    if ratio >= 100:
        return True
    if ratio <= 0:
        return False
    digest = hashlib.md5(veh_id.encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:4], "big") % 100  # 0..99
    return bucket < ratio


def _is_eco_for_ratio(veh_id: str, eco_ratio: int) -> bool:
    """Decisión determinista y monótona de eco/no-eco dentro de los inteligentes.

    Se usa un namespace distinto (sufijo "::eco") para que el bucket
    sea independiente del bucket intelligent/normal. Así, al barrer
    ``--eco-ratio`` (0, 10, ..., 99), los vehículos eco se ACUMULAN en
    lugar de cambiar arbitrariamente, igual que el ratio intelligent.

    Si un vehículo NO es intelligent, esta decisión es irrelevante (no se
    llega a crear una instancia eco). Conviene llamarla sólo cuando el
    vehículo ya ha sido marcado como intelligent.
    """
    if eco_ratio >= 100:
        return True
    if eco_ratio <= 0:
        return False
    digest = hashlib.md5((veh_id + "::eco").encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:4], "big") % 100
    return bucket < eco_ratio


def handle_new_vehicle(
    veh_id: str,
    state: SimulationState,
    share_radius: float,
    ratio: int = 99,
    eco_ratio: int = 0,
):
    """Initialize a new vehicle entering the simulation.

    El tipo intelligent/normal se decide SIEMPRE en runtime a partir del
    veh_id y del ratio actual (hash determinista). Se ignora el atributo
    type= que pueda traer el XML de trips, porque los archivos base
    (fullnet_trips_intelligent_{10|50|99}.xml) tienen tipos preasignados
    que rompen las barridas de ratios intermedios (20/30/40/...).

    Dentro del grupo "intelligent", una segunda decisión determinista
    elige qué subconjunto utiliza la estrategia ECO (minimizar CO2) en
    lugar de la estrategia de tráfico por defecto.

    Args:
        veh_id: Vehicle ID
        state: Simulation state
        share_radius: Share radius for V2V
        ratio: Percentage of intelligent vehicles (0-100)
        eco_ratio: Percentage of the *intelligent* vehicles that should
            use the CO2-minimising routing strategy (0-100). When
            ``eco_ratio == 0`` no eco vehicle is created (back-compat).
    """
    is_intelligent = _is_intelligent_for_ratio(veh_id, int(ratio))

    if is_intelligent:
        is_eco = _is_eco_for_ratio(veh_id, int(eco_ratio))
        if is_eco:
            state.vehicles[veh_id] = EcoIntelligentVehicle(
                veh_id, share_radius=share_radius)
            state.eco_count += 1
        else:
            state.vehicles[veh_id] = IntelligentVehicle(
                veh_id, share_radius=share_radius)
            state.intelligent_count += 1
    else:
        state.vehicles[veh_id] = NormalVehicle(veh_id, share_radius=0)
        state.normal_count += 1

    try:
        state.trip_depart_times[veh_id] = traci.simulation.getTime()
    except traci.TraCIException:
        pass


def update_trip_cache(state: SimulationState, current_ids):
    """Periodically cache distance/waitingTime for all vehicles (called every ~10s sim)."""
    for veh_id in current_ids:
        try:
            state.trip_cache[veh_id] = {
                'distance': traci.vehicle.getDistance(veh_id),
                'waitingTime': traci.vehicle.getAccumulatedWaitingTime(veh_id),
            }
        except traci.TraCIException:
            pass


def collect_trip_record(veh_id: str, state: SimulationState):
    """Collect tripinfo-like data for a departing vehicle using cached values."""
    arrival_time = traci.simulation.getTime()
    depart_time = state.trip_depart_times.pop(veh_id, 0.0)
    cached = state.trip_cache.pop(veh_id, {})
    distance = cached.get('distance', 0)
    waiting_time = cached.get('waitingTime', 0)
    duration = arrival_time - depart_time
    if duration <= 0 or distance < 10:
        return
    speed_kmh = (distance / duration) * 3.6
    veh_obj = state.vehicles.get(veh_id)
    if isinstance(veh_obj, EcoIntelligentVehicle):
        vtype = 'eco'
    elif isinstance(veh_obj, IntelligentVehicle):
        vtype = 'intelligent'
    else:
        vtype = 'normal'

    state.trip_data.append({
        'id': veh_id,
        'depart': round(depart_time, 1),
        'arrival': round(arrival_time, 1),
        'duration': round(duration, 1),
        'routeLength': round(distance, 2),
        'waitingTime': round(waiting_time, 1),
        'speed_kmh': round(speed_kmh, 2),
        'vType': vtype,
    })


def handle_departed_vehicle(veh_id: str, state: SimulationState, save_knowledge: bool,
                            gui: bool, save_shared_knowledge: bool = False):
    """Clean up a vehicle leaving the simulation."""
    if gui:
        remove_circle(veh_id)
    if veh_id in state.vehicles:
        collect_trip_record(veh_id, state)
        vehicle = state.vehicles[veh_id]
        if isinstance(vehicle, IntelligentVehicle):
            if save_knowledge:
                vehicle.export_knowledge(include_shared=save_shared_knowledge)
            state.knowledge_summaries.append(vehicle.knowledge.summary())
        del state.vehicles[veh_id]


def cleanup_departed_pairs(current_vehicles: Set[str], state: SimulationState):
    """Remove tracking data for departed vehicles."""
    to_remove = [p for p in state.shared_pairs if not p.issubset(current_vehicles)]
    for pair in to_remove:
        state.shared_pairs.pop(pair, None)
        state.pair_distances.pop(pair, None)
    departed_share_versions = [
        key for key in state.share_versions
        if key[0] not in current_vehicles or key[1] not in current_vehicles
    ]
    for key in departed_share_versions:
        state.share_versions.pop(key, None)


# ==================== KNOWLEDGE SHARING ====================
@track_performance
def detect_pairs_in_range(state: SimulationState):
    """Find all vehicle pairs currently within sharing range."""
    state.current_pairs_in_range = set()
    
    for veh_id, vehicle in state.vehicles.items():
        if not isinstance(vehicle, IntelligentVehicle):
            continue
        for neighbor_id, dist in vehicle.get_neighbors():
            if isinstance(state.vehicles.get(neighbor_id), IntelligentVehicle):
                pair = frozenset([veh_id, neighbor_id])
                state.current_pairs_in_range.add(pair)
                state.pair_distances[pair] = dist


@track_performance
def process_v2v_sharing(state: SimulationState, cooldown_seconds: float,
                        share_budget: int = DEFAULT_SHARE_BUDGET,
                        max_pairs_per_step: int = DEFAULT_MAX_V2V_PAIRS_PER_STEP,
                        urgent_congestion_bypass: bool = False):
    """Handle V2V knowledge sharing for pairs ready to exchange new deltas."""
    current_time = traci.simulation.getTime()
    pairs = sorted(state.current_pairs_in_range, key=lambda p: tuple(sorted(p)))
    if max_pairs_per_step > 0 and len(pairs) > max_pairs_per_step:
        state.sharing_pairs_deferred_by_cap += len(pairs) - max_pairs_per_step
        pairs = pairs[:max_pairs_per_step]

    for pair in pairs:
        state.sharing_pairs_processed += 1
        veh_list = sorted(pair)
        v1 = state.vehicles.get(veh_list[0])
        v2 = state.vehicles.get(veh_list[1])

        if not (isinstance(v1, IntelligentVehicle) and isinstance(v2, IntelligentVehicle)):
            continue

        key_1_to_2 = (v1.id, v2.id)
        key_2_to_1 = (v2.id, v1.id)
        since_1_to_2 = state.share_versions.get(key_1_to_2, 0)
        since_2_to_1 = state.share_versions.get(key_2_to_1, 0)

        has_new_data = (
            v1.knowledge.knowledge_version > since_1_to_2
            or v2.knowledge.knowledge_version > since_2_to_1
        )
        if not has_new_data:
            continue

        last_shared = state.shared_pairs.get(pair)
        cooldown_expired = (
            last_shared is None
            or current_time - last_shared >= cooldown_seconds
        )
        urgent_congestion = False
        if urgent_congestion_bypass:
            urgent_congestion = (
                v1.knowledge.has_new_congestion_since(since_1_to_2)
                or v2.knowledge.has_new_congestion_since(since_2_to_1)
            )
        if not cooldown_expired and not urgent_congestion:
            continue

        result_1_to_2 = v1.knowledge.share_with(
            v2.knowledge,
            since_version=since_1_to_2,
            max_observations=share_budget,
            current_time=current_time,
        )
        result_2_to_1 = v2.knowledge.share_with(
            v1.knowledge,
            since_version=since_2_to_1,
            max_observations=share_budget,
            current_time=current_time,
        )

        if result_1_to_2["sent_through_version"] > since_1_to_2:
            state.share_versions[key_1_to_2] = result_1_to_2["sent_through_version"]
        if result_2_to_1["sent_through_version"] > since_2_to_1:
            state.share_versions[key_2_to_1] = result_2_to_1["sent_through_version"]

        attempted = result_1_to_2["attempted"] + result_2_to_1["attempted"]
        accepted = result_1_to_2["accepted"] + result_2_to_1["accepted"]
        skipped = result_1_to_2["skipped"] + result_2_to_1["skipped"]
        state.sharing_skipped_observations += skipped
        if attempted > 0:
            state.shared_pairs[pair] = current_time
            state.sharing_events += 1
            state.sharing_attempted_observations += attempted
            state.sharing_accepted_observations += accepted


def create_edge_data_definition(output_dir: str, period_seconds: int = EDGE_DATA_PERIOD_SECONDS) -> tuple[str, str]:
    """
    Create a SUMO additional-file that writes edgeData for environment ground truth.

    Returns:
        (additional_file_path, edge_data_output_path)
    """
    edge_data_output = os.path.join(output_dir, "environment_traffic.edgedata.xml")
    additional_file = os.path.join(output_dir, "environment_traffic.add.xml")

    # SUMO resolves the edgeData ``file`` attribute relative to the additional
    # file's own directory (which is ``output_dir``). Using the full path here
    # would double the prefix when ``output_dir`` is relative (e.g. a distributed
    # output override), producing ``output_dir/output_dir/...edgedata.xml`` and a
    # "Could not build output file" crash. Use the basename so it lands next to
    # the .add.xml regardless of whether output_dir is relative or absolute.
    root = ET.Element("additional")
    ET.SubElement(root, "edgeData", {
        "id": "environment_traffic",
        "file": os.path.basename(edge_data_output),
        "period": str(period_seconds),
        "excludeEmpty": "true",
    })
    ET.ElementTree(root).write(additional_file, encoding="utf-8", xml_declaration=True)

    return additional_file, edge_data_output


def load_environment_traffic_from_edge_data(edge_data_file: str, slot_seconds: int = ENVIRONMENT_SLOT_SECONDS) -> dict:
    """
    Convert SUMO edgeData XML into the legacy environment_traffic.json structure.

    edgeData does not provide an explicit instantaneous vehicle count, so we store
    the average vehicles on edge during each aggregation interval:
        count = sampledSeconds / interval_duration
    """
    if not os.path.exists(edge_data_file):
        return {}

    tree = ET.parse(edge_data_file)
    root = tree.getroot()
    environment_data = {}

    for interval in root.findall("interval"):
        begin = float(interval.get("begin", "0"))
        end = float(interval.get("end", begin))
        interval_duration = max(end - begin, 1e-9)
        slot = int(begin // slot_seconds)

        for edge in interval.findall("edge"):
            edge_id = edge.get("id", "")
            if not edge_id or ":" in edge_id:
                continue

            speed = round(float(edge.get("speed", "0")), 2)
            sampled_seconds = float(edge.get("sampledSeconds", "0"))
            avg_vehicle_count = round(sampled_seconds / interval_duration, 2)

            if edge_id not in environment_data:
                environment_data[edge_id] = {}
            if slot not in environment_data[edge_id]:
                environment_data[edge_id][slot] = []

            environment_data[edge_id][slot].append({
                "speed": speed,
                "count": avg_vehicle_count,
                "time": round(begin, 1),
            })

    return environment_data


def save_trip_data(state: SimulationState, output_dir: str):
    """Save per-vehicle trip data (tripinfo equivalent) to JSON."""
    if not state.trip_data:
        return
    filepath = os.path.join(output_dir, 'trip_data.json')
    with open(filepath, 'w') as f:
        json.dump(state.trip_data, f)
    n = len(state.trip_data)
    avg_speed = sum(t['speed_kmh'] for t in state.trip_data) / n
    avg_dist = sum(t['routeLength'] for t in state.trip_data) / n
    avg_time = sum(t['duration'] for t in state.trip_data) / n
    print(f"[STATS] Trip data: {n} vehicles | avg speed={avg_speed:.1f} km/h | avg dist={avg_dist:.0f}m | avg time={avg_time:.0f}s")
    print(f"[FILE] Saved trip data: {filepath}")


def save_knowledge_summary(state: SimulationState, output_dir: str):
    """Save lightweight knowledge counts suitable for filter comparisons."""
    if not state.knowledge_summaries:
        return

    summaries = state.knowledge_summaries
    n = len(summaries)
    totals = {
        "vehicles": n,
        "sensor_observations": sum(s["sensor_observations"] for s in summaries),
        "shared_observations": sum(s["shared_observations"] for s in summaries),
        "sensor_segments": sum(s["sensor_segments"] for s in summaries),
        "shared_segments": sum(s["shared_segments"] for s in summaries),
    }
    totals["avg_sensor_observations_per_vehicle"] = round(totals["sensor_observations"] / n, 2) if n else 0
    totals["avg_shared_observations_per_vehicle"] = round(totals["shared_observations"] / n, 2) if n else 0

    filepath = os.path.join(output_dir, "knowledge_summary.json")
    with open(filepath, "w") as f:
        json.dump({"totals": totals, "vehicles": summaries}, f)
    print(
        f"[STATS] Knowledge summary: {n} intelligent vehicles | "
        f"shared obs={totals['shared_observations']} | sensor obs={totals['sensor_observations']}"
    )
    print(f"[FILE] Saved knowledge summary: {filepath}")


def save_environment_traffic_data(output_dir: str, edge_data_file: str):
    """Convert SUMO edgeData output and save it as environment_traffic.json."""
    environment_data = load_environment_traffic_from_edge_data(edge_data_file)
    if not environment_data:
        print(f"[WARN] No environment edgeData found in {edge_data_file}")
        return

    filepath = os.path.join(output_dir, "environment_traffic.json")
    with open(filepath, "w") as f:
        json.dump(environment_data, f, indent=2)
    print(f"[STATS] Saved environment traffic data from edgeData: {filepath}")


def check_cooldown_resets(state: SimulationState, cooldown_seconds: float):
    """Expire sharing records once cooldown_seconds have passed.

    This applies to ALL pairs -- both those that left range and those still in
    range -- so that vehicles in prolonged contact re-share every cooldown
    interval instead of just once on first contact.
    """
    current_time = traci.simulation.getTime()
    expired = [
        pair for pair, shared_time in state.shared_pairs.items()
        if current_time - shared_time >= cooldown_seconds
    ]
    for pair in expired:
        state.shared_pairs.pop(pair, None)


# ==================== SHARING HELPERS ====================
def sharing_includes_v2v(sharing) -> bool:
    """`--sharing` uses nargs='+', so this is usually a list like ['v2v']."""
    if sharing is None:
        return False
    if isinstance(sharing, (list, tuple)):
        return 'v2v' in sharing
    return sharing == 'v2v'


# ==================== VISUALS ====================
def update_visuals(state: SimulationState, share_radius: float, sharing_mode, draw_circles: bool = True):
    """Update vehicle colors (type only) and optional V2V circles (hue = type, alpha = proximity)."""
    for veh_id, vehicle in state.vehicles.items():
        try:
            if isinstance(vehicle, EcoIntelligentVehicle):
                base_state = 'eco'
            elif isinstance(vehicle, IntelligentVehicle):
                base_state = 'intelligent'
            else:
                base_state = 'normal'

            visual_state = base_state
            if sharing_mode == 'v2v':
                for pair in state.current_pairs_in_range:
                    if veh_id in pair:
                        shared_step = state.shared_pairs.get(pair)
                        if shared_step is not None:
                            is_fresh = (state.step - shared_step) < 10
                            visual_state = 'sharing' if is_fresh else 'in_range'
                        break

            color = STATE_COLORS[visual_state]['vehicle']
            traci.vehicle.setColor(veh_id, color)

            if sharing_mode == 'v2v' and vehicle.position and draw_circles:
                circle_color = STATE_COLORS[visual_state]['circle']
                update_circle(veh_id, vehicle.position, share_radius / 2, circle_color)

        except traci.TraCIException:
            pass


# ==================== VALIDATION ====================
def validate_edges_in_network(edges: list, network_file: str) -> tuple:
    """
    Validate that edges exist in the network.
    
    Returns:
        (valid: bool, missing: list)
    """
    try:
        tree = ET.parse(network_file)
        root = tree.getroot()
        
        # Get all edge IDs
        network_edges = set()
        for edge in root.findall('.//edge'):
            edge_id = edge.get('id', '')
            if not edge_id.startswith(':'):  # Skip internal edges
                network_edges.add(edge_id)
        
        missing = [e for e in edges if e not in network_edges]
        return (len(missing) == 0, missing)
        
    except Exception as e:
        print(f"[WARN] Could not validate edges: {e}")
        return (True, [])  # Assume valid if can't parse


def count_vehicles_in_trips(trips_file: str) -> Optional[int]:
    """Count vehicles in trips file."""
    try:
        tree = ET.parse(trips_file)
        root = tree.getroot()
        trips = root.findall('.//trip') + root.findall('.//vehicle')
        return len(trips)
    except Exception:
        return None


def format_time(seconds: float) -> str:
    """Format seconds into readable time."""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    if hours > 0:
        return f"{hours}h {minutes}m {secs}s"
    elif minutes > 0:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def save_simulation_info(output_dir: str, args, road_config: dict, trips_file: str, net_file: str):
    """Save simulation metadata to JSON file."""
    info = {
        "timestamp": datetime.now().isoformat(),
        "configuration": {
            "road": args.road,
            "accident_enabled": args.accident,
            "intelligent_ratio": args.ratio,
            "eco_ratio": getattr(args, "eco_ratio", "0"),
            "co2_refresh_seconds": getattr(args, "co2_refresh", 60),
            "sharing_mode": args.sharing,
            "share_radius": road_config.get("share_radius", DEFAULTS["share_radius"]),
            "share_cooldown": road_config.get("share_cooldown", DEFAULTS["share_cooldown"]),
            "filters": args.filters,
            "tripinfo_enabled": getattr(args, 'save_tripinfo', False),
            "tripinfo_xml_enabled": getattr(args, "save_tripinfo_xml", False),
            "knowledge_raw_enabled": getattr(args, "save_knowledge", False),
            "shared_knowledge_raw_enabled": getattr(args, "save_shared_knowledge", False),
            "edge_data_period": getattr(args, "edge_data_period", EDGE_DATA_PERIOD_SECONDS),
            "share_budget": getattr(args, "share_budget", DEFAULT_SHARE_BUDGET),
            "share_only_congestion": getattr(args, "share_only_congestion", True),
            "share_candidate_threshold": getattr(args, "share_candidate_threshold", DEFAULT_SHARE_CANDIDATE_THRESHOLD),
            "max_share_age_seconds": getattr(args, "max_share_age_seconds", DEFAULT_MAX_SHARE_AGE_SECONDS),
            "max_v2v_pairs_per_step": getattr(args, "max_v2v_pairs_per_step", DEFAULT_MAX_V2V_PAIRS_PER_STEP),
            "pretty_json": getattr(args, "pretty_json", False),
        },
        "files": {
            "network": os.path.basename(net_file),
            "trips": os.path.basename(trips_file),
        },
        "vehicle_count": count_vehicles_in_trips(trips_file),
        "spatial_corridor": os.environ.get("VEHICLEKNOWLEDGE_SPATIAL_CORRIDOR"),
    }
    
    if args.accident and road_config.get("accident_edges"):
        # Prefer CLI overrides (--accident-start/--accident-duration): son los
        # valores EFECTIVOS con los que corre AccidentManager.
        eff_start = (args.accident_start if getattr(args, "accident_start", None) is not None
                     else road_config.get("accident_start_time", DEFAULTS["accident_start_time"]))
        eff_duration = (args.accident_duration if getattr(args, "accident_duration", None) is not None
                        else road_config.get("accident_duration", DEFAULTS["accident_duration"]))
        info["accident"] = {
            "start_time": eff_start,
            "duration": eff_duration,
            "speed_limit": road_config.get("accident_speed_limit", DEFAULTS["accident_speed_limit"]),
            "start_edge": road_config["accident_edges"][0],
            "end_edge": road_config["accident_edges"][1],
        }
    
    info_file = os.path.join(output_dir, "simulation_info.json")
    with open(info_file, 'w') as f:
        json.dump(info, f, indent=2)
    print(f"[FILE] Saved simulation info: {info_file}")


# ==================== MAIN ====================
def parse_arguments():
    """Parse command line arguments."""
    # Discover available roads for dynamic help
    available_roads = discover_roads()
    
    parser = argparse.ArgumentParser(
        description="SUMO Traffic Simulation with Vehicle Knowledge System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Available roads: {', '.join(available_roads)}

Examples:
  python main.py --road simple                   # Quick test with simple network
  python main.py --road simple --accident --gui  # Test accident with GUI
  python main.py --road modified_4ways_all       # Full network simulation
  python main.py --list-roads                    # Show available roads
        """
    )
    
    parser.add_argument("--road", type=str, default="simple",
                        help=f"Road network name (default: simple)")

    parser.add_argument("--trips", type=str, default=None, metavar="FILENAME",
                        help="Override trips file (basename from trips/ folder, "
                             "e.g. modified_minimal_small.trips.xml). "
                             "Must be compatible with the selected road.")

    parser.add_argument("--list-roads", action="store_true",
                        help="List available road networks and exit")
    
    parser.add_argument("--accident", action="store_true",
                        help="Enable accident scenario")
    
    parser.add_argument("--accident-edges", nargs=2, metavar=("START", "END"),
                        help="Override accident edges (default: from road config)")
    
    parser.add_argument("--accident-start", type=int, metavar="SECONDS",
                        help="Accident start time in seconds (default: from road config)")
    
    parser.add_argument("--accident-duration", type=int, metavar="SECONDS",
                        help="Accident/incident duration in seconds (default: from road config)")
    
    parser.add_argument("--force-cv", action="store_true",
                        help="Force traffic through CV route (uses trips file with _cv suffix when available)")
    
    parser.add_argument("--vehicles", type=int, metavar="N", default=None,
                        help="Number of vehicles (e.g. 10000). Uses trips file with that count when available (empty = default file)")
    
    parser.add_argument("--ratio",
                        choices=["10", "20", "30", "40", "50",
                                 "60", "70", "80", "90", "99"],
                        default="99",
                        help="Intelligent vehicles ratio (default: 99%%)")

    parser.add_argument("--eco-ratio",
                        choices=["0", "10", "20", "30", "40", "50",
                                 "60", "70", "80", "90", "99", "100"],
                        default="0",
                        help=("Percentage of the INTELLIGENT vehicles that "
                              "use the CO2-minimising routing strategy "
                              "instead of the traffic-aware fastest one "
                              "(default: 0%%, i.e. all intelligent vehicles "
                              "stay traffic-aware)."))

    parser.add_argument("--co2-refresh",
                        type=int, default=60, metavar="SECONDS",
                        help=("How often (in simulated seconds) the global "
                              "edge CO2 cost map used by eco vehicles is "
                              "recomputed (default: 60)."))

    parser.add_argument("--sharing", choices=["none", "v2v", "global"], default="none", nargs="+",
                        help="Knowledge sharing mode (can specify multiple, default: none)")
    
    parser.add_argument("--gui", action="store_true",
                        help="Run with SUMO-GUI")
    parser.add_argument("--no-circles", action="store_true",
                        help="Disable V2V radius circles in GUI (less visual clutter)")
    
    # Data collection flags
    parser.add_argument('--save-environment', action='store_true', default=True,
                        help='Save Environment traffic data (default: enabled)')
    parser.add_argument('--no-save-environment', action='store_false', dest='save_environment',
                        help='Disable Environment traffic data collection')
    
    parser.add_argument('--save-knowledge', action='store_true', default=False,
                        help='Save raw per-vehicle sensor knowledge JSON (default: disabled)')
    parser.add_argument('--no-save-knowledge', action='store_false', dest='save_knowledge',
                        help='Disable raw per-vehicle knowledge JSON output')
    parser.add_argument('--save-shared-knowledge', action='store_true', default=False,
                        help='Also save raw per-vehicle shared knowledge JSON. Very large; default: disabled')
    
    parser.add_argument("--filters", type=str, default="default",
                        help="Comma-separated filter names: sensor,shared,consistency,age,geo,congestion "
                             "or 'none'/'default'/'all' (default: default=sensor+shared)")

    parser.add_argument('--save-tripinfo', action='store_true', default=True,
                        help='Save lightweight trip_data.json (per-vehicle speed, distance, time; default: enabled)')
    parser.add_argument('--no-save-tripinfo', action='store_false', dest='save_tripinfo',
                        help='Disable tripinfo output')
    parser.add_argument('--save-tripinfo-xml', action='store_true', default=True,
                        help='Ask SUMO to write tripinfo.xml together with trip_data.json (default: enabled)')
    parser.add_argument('--no-save-tripinfo-xml', action='store_false', dest='save_tripinfo_xml',
                        help='Disable SUMO tripinfo.xml output while keeping lightweight trip_data.json')

    parser.add_argument("--edge-data-period", type=int, default=EDGE_DATA_PERIOD_SECONDS,
                        help=f"SUMO edgeData sampling period in seconds (default: {EDGE_DATA_PERIOD_SECONDS}, "
                             f"matching {ENVIRONMENT_SLOT_SECONDS}s environment slots)")

    parser.add_argument("--share-radius", type=float, default=None,
                        help="Override V2V radius in meters. Default comes from road config "
                             f"({DEFAULTS['share_radius']}m global default)")
    parser.add_argument("--share-cooldown", type=float, default=None,
                        help="Override seconds before a pair can re-share. Default comes from road config "
                             f"({DEFAULTS['share_cooldown']}s global default)")

    parser.add_argument("--share-budget", type=int, default=DEFAULT_SHARE_BUDGET,
                        help="Maximum new observations to send per direction in one V2V exchange "
                             f"(0 = unlimited, default: {DEFAULT_SHARE_BUDGET})")
    parser.add_argument("--share-all-observations", action="store_false",
                        dest="share_only_congestion", default=True,
                        help="Disable the optimized default and share normal/free-flow observations too")
    parser.add_argument("--share-candidate-threshold", type=float, default=DEFAULT_SHARE_CANDIDATE_THRESHOLD,
                        help="When optimized sharing is active, send only observations with "
                             "speed/free-flow below this looser candidate threshold "
                             f"(default: {DEFAULT_SHARE_CANDIDATE_THRESHOLD})")
    parser.add_argument("--max-share-age-seconds", type=int, default=DEFAULT_MAX_SHARE_AGE_SECONDS,
                        help="Do not relay observations older than this many seconds "
                             f"(default: {DEFAULT_MAX_SHARE_AGE_SECONDS}; <0 disables)")
    parser.add_argument("--max-v2v-pairs-per-step", type=int, default=DEFAULT_MAX_V2V_PAIRS_PER_STEP,
                        help="Cap V2V pair processing per simulation step "
                             f"(0 = unlimited, default: {DEFAULT_MAX_V2V_PAIRS_PER_STEP})")
    parser.add_argument("--urgent-congestion-bypass", action="store_true", default=False,
                        help="Allow fresh congestion to bypass pair cooldown. More responsive but more expensive")
    parser.add_argument("--dashboard-update-interval", type=int, default=DEFAULT_DASHBOARD_UPDATE_INTERVAL,
                        help="When GUI/debug dashboard cache is active, refresh every N simulated seconds "
                             f"(default: {DEFAULT_DASHBOARD_UPDATE_INTERVAL})")

    parser.add_argument("--pretty-json", action="store_true",
                        help="Write per-vehicle knowledge JSON with indentation. Default is compact JSON.")

    parser.add_argument("--debug", action="store_true",
                        help="Enable debug server (HTTP dashboard)")

    parser.add_argument("--reroute-log", nargs="?", const="all", default=None,
                        metavar="VEH_ID",
                        help="Print reroute events to stdout (route changes only). "
                             "Omit value for all vehicles; pass ID to filter (e.g. --reroute-log veh5).")

    parser.add_argument("--reroute-log-verbose", action="store_true",
                        help="Also print SKIP decisions (use with --reroute-log VEH_ID only — "
                             "very noisy with many vehicles).")

    parser.add_argument("--max-steps", type=int, default=0,
                        help="Stop after N steps (0 = run until finished)")

    # ---- Synced-spatial mode (distributed simulation with handoffs) ----
    parser.add_argument("--sync-master", default=None,
                        help="Master URL host:port; activates synced-spatial mode")
    parser.add_argument("--sync-parent", default=None,
                        help="parent_sim_id this run participates in (see master)")
    parser.add_argument("--sync-group", default=None,
                        help="Group label for this worker (e.g. AN, CV, A7)")
    parser.add_argument("--sync-peers", nargs="*", default=None,
                        help="Other group labels participating in the sync")

    return parser.parse_args()


def main():
    args = parse_arguments()
    
    # Handle --list-roads
    if args.list_roads:
        print("\n[DIR] Available road networks:")
        print("=" * 50)
        for road in discover_roads():
            config = get_road_config(road)
            desc = config.get("description", "")
            accident = "[OK]" if config.get("accident_edges") else "[NO]"
            print(f"  {road:25} {desc}")
            if config.get("accident_edges"):
                edges = config["accident_edges"]
                print(f"    `- Accident edges: {edges[0]} -> {edges[1]}")
        print()
        return 0
    
    # Load road configuration
    try:
        net_file = get_network_file(args.road)
    except FileNotFoundError as e:
        print(f"\n[ERROR] {e}")
        return 1
    
    road_config = get_road_config(args.road)

    # Trips file resolution (priority order):
    #   1. VEHICLEKNOWLEDGE_TRIPS_OVERRIDE env var (distributed/worker mode)
    #   2. --trips CLI override
    #   3. get_trips_file(road, ratio, force_cv, vehicles)
    trips_override = os.environ.get("VEHICLEKNOWLEDGE_TRIPS_OVERRIDE")
    if trips_override and os.path.isfile(trips_override):
        trips_file = trips_override
        print(f"[INFO] Using trips override from env: {trips_file}")
    elif getattr(args, "trips", None):
        trips_path = SIMULATION_DIR / "trips" / args.trips
        if not trips_path.exists():
            trips_path = Path(args.trips)
        if not trips_path.exists():
            print(f"\n[ERROR] Trips file not found: {args.trips}")
            compatible = discover_trips_for_road(args.road)
            print(f"   Compatible trips for '{args.road}': {', '.join(compatible) or '(none found)'}")
            return 1
        trips_file = str(trips_path)
    else:
        trips_file = get_trips_file(
            args.road,
            args.ratio,
            force_cv=getattr(args, "force_cv", False),
            vehicles=getattr(args, "vehicles", None),
        )

    if not os.path.isfile(trips_file):
        print(f"\n[ERROR] Trips file not found: {trips_file}")
        print(f"   Available trips files:")
        for f in sorted((SIMULATION_DIR / "trips").glob("*.xml")):
            print(f"     - {f.name}")
        if getattr(args, "force_cv", False):
            print("   With --force-cv use a file named e.g. fullnet_trips_intelligent_99_cv.xml")
        if getattr(args, "vehicles", None):
            print(f"   With --vehicles {args.vehicles} use a file named e.g. fullnet_trips_intelligent_{args.ratio}_{args.vehicles}.xml")
        return 1
    
    # Handle accident edges
    accident_edges = None
    if args.accident:
        if args.accident_edges:
            # CLI override
            accident_edges = tuple(args.accident_edges)
        elif road_config.get("accident_edges"):
            accident_edges = road_config["accident_edges"]
        else:
            print(f"\n[WARN] No accident edges configured for '{args.road}'")
            print("   Use --accident-edges START END to specify, or disable --accident")
            return 1
        
        # Validate accident edges exist in network
        valid, missing = validate_edges_in_network(list(accident_edges), net_file)
        if not valid:
            print(f"\n[ERROR] Accident edges not found in network: {missing}")
            print("   Check the network file or use --accident-edges with valid edges")
            return 1
    
    vehicle_count = count_vehicles_in_trips(trips_file)
    save_knowledge = args.save_knowledge
    save_shared_knowledge = args.save_shared_knowledge
    save_environment = args.save_environment
    save_tripinfo = args.save_tripinfo
    save_tripinfo_xml = args.save_tripinfo and args.save_tripinfo_xml
    args.save_tripinfo_xml = save_tripinfo_xml
    any_saving = save_knowledge or save_environment or save_tripinfo or save_tripinfo_xml
    
    # Get timing parameters (use args if provided, otherwise road config)
    share_radius = args.share_radius if args.share_radius is not None else road_config.get("share_radius", DEFAULTS["share_radius"])
    share_cooldown = args.share_cooldown if args.share_cooldown is not None else road_config.get("share_cooldown", DEFAULTS["share_cooldown"])
    max_share_age_seconds = int(args.max_share_age_seconds)
    if max_share_age_seconds < 0:
        max_share_age_seconds = None
    
    # Accident timing - command line args override config
    accident_start = args.accident_start if args.accident_start is not None else \
                     road_config.get("accident_start_time", DEFAULTS["accident_start_time"])
    accident_duration = args.accident_duration if args.accident_duration is not None else \
                        road_config.get("accident_duration", DEFAULTS["accident_duration"])
    accident_speed = road_config.get("accident_speed_limit", DEFAULTS["accident_speed_limit"])
    
    # Simulation duration: from road config, or fallback based on road type
    if "sim_duration" in road_config:
        sim_duration = road_config["sim_duration"]
    elif args.road == "simple":
        sim_duration = 600
    else:
        sim_duration = 14400

    road_config["share_radius"] = share_radius
    road_config["share_cooldown"] = share_cooldown
    
    # Print configuration summary
    print("\n" + "=" * 60)
    print("[INFO] SIMULATION CONFIGURATION")
    print("=" * 60)
    print(f"   Road:       {args.road}")
    print(f"   Network:    {Path(net_file).name}")
    print(f"   Trips:      {Path(trips_file).name}")
    print(f"   Vehicles:   {vehicle_count or 'unknown'}")
    print(f"   Duration:   {format_time(sim_duration)}")
    print(f"   Ratio:      {args.ratio}% intelligent")
    eco_ratio_int = int(getattr(args, "eco_ratio", "0"))
    if eco_ratio_int > 0:
        print(f"   Eco ratio:  {eco_ratio_int}% of intelligent route by CO2"
              f" (refresh every {args.co2_refresh}s)")
    print(f"   Sharing:    {args.sharing}")
    if getattr(args, "force_cv", False):
        print(f"   Force CV:   Yes")
    if getattr(args, "vehicles", None):
        print(f"   Vehicles:   {args.vehicles}")
    print(f"   Save Env:   {'Yes' if save_environment else 'No'}")
    print(f"   Save Knowledge: {'Yes' if save_knowledge else 'No'}")
    if save_knowledge:
        print(f"   Save Shared Knowledge: {'Yes' if save_shared_knowledge else 'No'}")
    print(f"   Filters:    {args.filters}")
    print(f"   Trip Data:  {'Yes' if save_tripinfo else 'No'}")
    print(f"   Tripinfo XML: {'Yes' if save_tripinfo_xml else 'No'}")
    print(f"   V2V Radius: {share_radius:g}m | Cooldown: {share_cooldown:g}s | Budget: {max(0, int(args.share_budget))}")
    print(
        f"   V2V Optimized: {'slowdown-candidates' if args.share_only_congestion else 'all observations'} "
        f"| Candidate threshold: {args.share_candidate_threshold:g} | Max age: {max_share_age_seconds if max_share_age_seconds is not None else 'disabled'}s"
    )
    print(f"   GUI:        {'Yes' if args.gui else 'No'}")
    print(f"   Debug:      {'Yes' if args.debug else 'No'}")
    if args.reroute_log:
        filt = args.reroute_log if args.reroute_log != "all" else "all vehicles"
        print(f"   Reroute Log: {filt}")
    
    if args.accident and accident_edges:
        start_fmt = format_time(accident_start)
        end_fmt = format_time(accident_start + accident_duration)
        print(f"   Accident:   {start_fmt} -> {end_fmt}")
        print(f"   `- Edges:   {accident_edges[0]} -> {accident_edges[1]}")
    else:
        print(f"   Accident:   DISABLED")
    print("=" * 60)
    
    # Setup output directory
    # Distributed-mode override: workers pin a specific output path so they
    # know where to find the artefacts to zip and upload.
    output_override = os.environ.get("VEHICLEKNOWLEDGE_OUTPUT_OVERRIDE")
    spatial_corridor = os.environ.get("VEHICLEKNOWLEDGE_SPATIAL_CORRIDOR")
    run_output_dir = None
    edge_data_additional_file = None
    edge_data_output_file = None
    if any_saving:
        if output_override:
            run_output_dir = output_override
            os.makedirs(run_output_dir, exist_ok=True)
            print(f"[INFO] Using output override from env: {run_output_dir}")
        else:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            folder_name = f"{timestamp}_{args.road}_{args.ratio}pct"
            filter_slug = (args.filters or "default").replace(",", "-")
            folder_name += f"_{filter_slug}"
            if args.accident:
                folder_name += "_accident"
            else:
                folder_name += "_noaccident"

            output_base = SIMULATION_DIR / "knowledge_output"
            run_output_dir = str(output_base / folder_name)
            os.makedirs(run_output_dir, exist_ok=True)
        
        filter_names = args.filters.split(",") if args.filters != "default" else None
        VehicleKnowledge.setup(
            output_dir=run_output_dir,
            slot_seconds=300,
            pretty=args.pretty_json,
            filters=filter_names,
            share_only_congestion=args.share_only_congestion,
            max_share_age_seconds=max_share_age_seconds,
            share_candidate_threshold=args.share_candidate_threshold,
        )
        save_simulation_info(run_output_dir, args, road_config, trips_file, net_file)
        if save_environment:
            edge_data_period = max(1, int(args.edge_data_period))
            edge_data_additional_file, edge_data_output_file = create_edge_data_definition(
                run_output_dir,
                period_seconds=edge_data_period,
            )
        print(f"[DIR] Output: {run_output_dir}")
    else:
        filter_names = args.filters.split(",") if args.filters != "default" else None
        VehicleKnowledge.setup(
            output_dir=".",
            slot_seconds=300,
            pretty=args.pretty_json,
            filters=filter_names,
            share_only_congestion=args.share_only_congestion,
            max_share_age_seconds=max_share_age_seconds,
            share_candidate_threshold=args.share_candidate_threshold,
        )

    # Reset IntelligentVehicle class-level caches (safe for repeated runs from gui.py)
    IntelligentVehicle._auto_bifurcations = None
    IntelligentVehicle.reroute_log = []
    IntelligentVehicle._ff_tt_cache = {}
    IntelligentVehicle._reroute_log_enabled = False
    IntelligentVehicle._reroute_log_filter = None
    IntelligentVehicle.dashboard_cache_enabled = bool(args.debug or args.gui)
    IntelligentVehicle.dashboard_update_interval_steps = max(1, int(args.dashboard_update_interval))
    
    # Start SUMO
    sumo_binary = shutil.which("sumo-gui" if args.gui else "sumo")
    if not sumo_binary:
        print("[ERROR] SUMO not found in PATH")
        return 1
    
    sumo_args = [
        sumo_binary,
        "--net-file", net_file,
        "--route-files", trips_file,
        "--begin", "0",
        "--end", str(sim_duration),
        "--step-length", "1.0",
        "--ignore-route-errors", "false",
        "--time-to-teleport", "1800",
    ]

    if edge_data_additional_file:
        sumo_args.extend(["--additional-files", edge_data_additional_file])

    if save_tripinfo_xml and run_output_dir:
        tripinfo_file = os.path.join(run_output_dir, "tripinfo.xml")
        sumo_args.extend(["--tripinfo-output", tripinfo_file])
        print(f"[FILE] Tripinfo output: {tripinfo_file}")

    # Default view: bigger vehicles + IDs (native scheme), not "standard"
    if args.gui and SUMO_GUI_SETTINGS_FILE.is_file():
        sumo_args.extend(["--gui-settings-file", str(SUMO_GUI_SETTINGS_FILE)])

    # Auto-start and auto-close for GUI mode
    if args.gui:
        sumo_args.extend(["--start", "--quit-on-end"])
    
    try:
        traci.start(sumo_args)
        print(f"[OK] SUMO started")
    except Exception as e:
        print(f"[ERROR] Starting SUMO: {e}")
        return 1

    # Pre-populate free-flow travel time cache for all edges. Must run BEFORE
    # any accident or dynamics modify the network, so that IntelligentVehicle
    # congestion detection always compares against the static free-flow baseline.
    try:
        for eid in traci.edge.getIDList():
            if not eid.startswith(':'):
                IntelligentVehicle._ff_tt_cache[eid] = traci.edge.getTraveltime(eid)
        print(f"[OK] Free-flow TT cache populated for {len(IntelligentVehicle._ff_tt_cache)} edges")
    except Exception as e:
        print(f"[WARN] Could not pre-populate FF cache: {e}")

    # Freeze routing weights for accident edges at their free-flow travel time.
    # Captured before the accident lowers maxSpeed so the router never sees the
    # reduced speeds. All rerouting with currentTravelTimes=False uses this
    # override, so Normal and Intelligent vehicles share the same baseline and
    # are NOT diverted around the accident at insertion.
    if args.accident and accident_edges:
        try:
            ff_times = {}
            if accident_edges[0] == accident_edges[1]:
                ff_times[accident_edges[0]] = traci.edge.getTraveltime(accident_edges[0])
            else:
                try:
                    accident_route = traci.simulation.findRoute(
                        accident_edges[0], accident_edges[1])
                    for eid in accident_route.edges:
                        ff_times[eid] = traci.edge.getTraveltime(eid)
                except traci.TraCIException:
                    ff_times[accident_edges[0]] = traci.edge.getTraveltime(accident_edges[0])
                    ff_times[accident_edges[1]] = traci.edge.getTraveltime(accident_edges[1])
            for eid, ff in ff_times.items():
                traci.edge.adaptTraveltime(eid, ff)
            print(f"[OK] Free-flow routing weights pinned on {len(ff_times)} accident edges: {list(ff_times)}")
        except Exception as e:
            print(f"[WARN] Could not pin accident free-flow weights: {e}")

    # Initialize accident manager
    accident_manager = None
    if args.accident and accident_edges:
        accident_manager = AccidentManager(
            accident_edges[0],
            accident_edges[1],
            accident_start,
            accident_duration,
            accident_speed,
            visuals_enabled=args.gui
        )

    # Initialize the global CO2-cost map used by EcoIntelligentVehicle.
    if eco_ratio_int > 0:
        try:
            n_edges = EdgeCO2Costs.initialize(refresh_every=float(args.co2_refresh))
            print(f"[OK] Eco routing armed: CO2 cost map over {n_edges} edges "
                  f"(refresh every {args.co2_refresh}s)")
        except Exception as e:
            print(f"[WARN] Could not initialize CO2 cost map: {e}")

    # Enable reroute logging if requested
    if args.reroute_log:
        vehicle_filter = None if args.reroute_log == "all" else args.reroute_log
        verbose = getattr(args, "reroute_log_verbose", False)
        if verbose and vehicle_filter is None:
            print("[WARN] --reroute-log-verbose without vehicle filter is very noisy. "
                  "Add --reroute-log VEH_ID to filter one vehicle.")
        IntelligentVehicle.enable_reroute_log(vehicle_filter, verbose=verbose)

    # Initialize simulation state
    state = SimulationState()
    sim_start_time = time.perf_counter()

    # Synced-spatial bridge (only active when --sync-* flags are set)
    sync_bridge = None
    if getattr(args, "sync_master", None) and getattr(args, "sync_parent", None) and getattr(args, "sync_group", None):
        try:
            from SIMULATION.distributed.sync_runtime import SyncBridge
            sync_bridge = SyncBridge(
                master_url=args.sync_master,
                parent_sim_id=args.sync_parent,
                group=args.sync_group,
                peers=args.sync_peers or [],
            )
            sync_bridge.boot()
            print(f"[OK] Sync bridge active: group={args.sync_group} "
                  f"peers={args.sync_peers} master={args.sync_master}")
        except Exception as e:
            print(f"[ERROR] sync bridge boot failed: {e}")
            sync_bridge = None

    resource_monitor = ResourceMonitor()
    resource_monitor.start()

    if getattr(args, "debug", False):
        from SIMULATION.debug_server import start_debug_server
        if start_debug_server(state):
            port = getattr(state, "debug_port", None) or 8080
            try:
                import webbrowser
                url = f"http://localhost:{port}"
                webbrowser.open(url)
                print(f"[INFO] Opened debug dashboard: {url}")
            except Exception:
                print(f"[INFO] Debug dashboard: http://localhost:{port}")

    print(f"\n[START] Running simulation (sharing={args.sharing})...")

    # Drain safety net for synced-spatial runs: a vehicle can bounce across a
    # partition boundary (shipped worker A->B->A ...) and never arrive, so
    # getMinExpectedNumber() never reaches 0 and the whole lockstep run spins
    # until the external per-run timeout (hours wasted). Once every worker is
    # well past the departure horizon, abandon the last stragglers. Baselines
    # and standalone runs (no sync bridge) are unaffected.
    drain_deadline = sim_duration + max(7200.0, sim_duration)

    # Per-step per-node occupancy timeseries (only meaningful in distributed
    # synced-spatial runs, where each worker == one partition/node). Each entry
    # is [sim_time, active_vehicles_in_this_node]; dumped to node_occupancy.json
    # so the aggregator can reconstruct vehicles-per-node vs step across workers.
    node_occupancy = [] if sync_bridge is not None else None

    try:
        while traci.simulation.getMinExpectedNumber() > 0:
            # Check step limit
            if args.max_steps > 0 and state.step >= args.max_steps:
                print(f"\n[STOP] Step limit reached: {args.max_steps}")
                break
            
            current_time = traci.simulation.getTime()

            if (sync_bridge is not None and args.max_steps <= 0
                    and current_time > drain_deadline):
                remaining = traci.simulation.getMinExpectedNumber()
                print(f"\n[STOP] Synced drain deadline reached at "
                      f"t={current_time:.0f}s (horizon={sim_duration:.0f}, "
                      f"deadline={drain_deadline:.0f}); abandoning "
                      f"{remaining} straggler(s)", flush=True)
                break
            
            # Accident management
            if accident_manager:
                accident_manager.step(current_time)
            
            # Simulation step
            traci.simulationStep()

            # Synced-spatial handoff: BEFORE we materialise the new
            # current_ids set, ship out vehicles that crossed into a
            # foreign-owned edge AND apply incoming handoffs from peers.
            # This guarantees the rest of the loop (lifecycle hooks,
            # knowledge sync, environment collection) sees the post-handoff
            # state.
            if sync_bridge is not None:
                try:
                    sync_bridge.exchange(traci, state.step)
                except Exception as e:
                    print(f"[WARN] sync exchange failed at step {state.step}: {e}")

            current_ids = set(traci.vehicle.getIDList())

            if node_occupancy is not None:
                node_occupancy.append([round(current_time, 1), len(current_ids)])
            
            # Handle vehicle lifecycle
            ratio_int = int(args.ratio)
            for veh_id in current_ids - state.previous_vehicles:
                handle_new_vehicle(
                    veh_id, state,
                    share_radius if sharing_includes_v2v(args.sharing) else 0,
                    ratio_int,
                    eco_ratio_int,
                )

            # Keep the CO2 cost map fresh for eco-routing vehicles.
            if eco_ratio_int > 0 and EdgeCO2Costs.is_initialized():
                EdgeCO2Costs.refresh_if_stale(current_time)
            
            for veh_id in state.previous_vehicles - current_ids:
                handle_departed_vehicle(veh_id, state, save_knowledge, args.gui, save_shared_knowledge)
            
            cleanup_departed_pairs(current_ids, state)
            
            # Build edge vehicle counts once per step (replaces per-vehicle IPC calls)
            edge_counts = {}
            for veh_id in current_ids:
                veh = state.vehicles.get(veh_id)
                if veh and veh.current_segment_id and ':' not in veh.current_segment_id:
                    edge_counts[veh.current_segment_id] = edge_counts.get(veh.current_segment_id, 0) + 1
            
            # Sync all vehicles (subscription results - no individual IPC calls)
            for vehicle in state.vehicles.values():
                vehicle.sync_with_traci(current_ids, edge_counts)

            # Cache trip data periodically (~every 10 simulated seconds)
            if state.step % 10 == 0:
                update_trip_cache(state, current_ids)

            # Knowledge sharing based on modes
            if 'v2v' in args.sharing:
                detect_pairs_in_range(state)
                check_cooldown_resets(state, share_cooldown)
                process_v2v_sharing(
                    state,
                    share_cooldown,
                    share_budget=max(0, int(args.share_budget)),
                    max_pairs_per_step=max(0, int(args.max_v2v_pairs_per_step)),
                    urgent_congestion_bypass=args.urgent_congestion_bypass,
                )
            
            if 'global' in args.sharing:
                # Global sharing: all vehicles share with all others instantly
                all_vehs = list(state.vehicles.values())
                for i in range(len(all_vehs)):
                    for j in range(i + 1, len(all_vehs)):
                        if isinstance(all_vehs[i], IntelligentVehicle) and isinstance(all_vehs[j], IntelligentVehicle):
                            all_vehs[i].knowledge.share_with(all_vehs[j].knowledge)
                            all_vehs[j].knowledge.share_with(all_vehs[i].knowledge)
                            state.sharing_events += 1
            
            # Update visuals
            if args.gui and state.step % 10 == 0:
                update_visuals(state, share_radius, args.sharing, draw_circles=not args.no_circles)
            
            state.previous_vehicles = current_ids
            state.step += 1

            # Progress feedback (human + machine-readable for distributed workers)
            if state.step > 0 and state.step % 300 == 0:
                print(f"[TIME] {current_time:.0f}s | Vehicles: {len(state.vehicles)} | Shares: {state.sharing_events}")
            if state.step % 100 == 0:
                print(
                    f"[PROGRESS] step={state.step} total={int(sim_duration)} "
                    f"vehicles={len(state.vehicles)}",
                    flush=True,
                )
    
    except KeyboardInterrupt:
        print("\n[WARN] Interrupted by user")
    
    finally:
        sim_duration_actual = time.perf_counter() - sim_start_time
        resource_monitor.stop()
        
        print(f"\n{'=' * 50}")
        print(f"[TIME] Wall time: {format_time(sim_duration_actual)}")
        if psutil is not None:
            print(f"[PERF] Max CPU: {resource_monitor.max_cpu_percent:.1f}% | Peak RAM: {resource_monitor.peak_rss_mb:.1f} MB")
        else:
            print("[PERF] CPU/RAM metrics unavailable (psutil not installed)")
        print(f"[STATS] Total sharing events: {state.sharing_events}")
        if state.sharing_attempted_observations:
            acceptance_rate = state.sharing_accepted_observations / state.sharing_attempted_observations * 100
            print(
                f"[STATS] V2V observations attempted: {state.sharing_attempted_observations} | "
                f"accepted: {state.sharing_accepted_observations} ({acceptance_rate:.1f}%)"
            )
        if state.sharing_skipped_observations:
            print(f"[STATS] V2V observations skipped by communication policy: {state.sharing_skipped_observations}")
        
        # Vehicle type summary
        total_vehs = (state.intelligent_count + state.normal_count
                      + state.eco_count)
        if total_vehs > 0:
            int_pct = state.intelligent_count / total_vehs * 100
            eco_pct = state.eco_count / total_vehs * 100
            norm_pct = state.normal_count / total_vehs * 100
            print(
                f"[VEHICLE] {state.intelligent_count} intelligent "
                f"({int_pct:.0f}%) | {state.eco_count} eco "
                f"({eco_pct:.0f}%) | {state.normal_count} normal "
                f"({norm_pct:.0f}%)"
            )
        
        # Save remaining data
        if any_saving:
            if save_knowledge:
                print("[SAVE] Saving remaining vehicles...")
                for vehicle in state.vehicles.values():
                    if not isinstance(vehicle, IntelligentVehicle):
                        continue
                    try:
                        vehicle.export_knowledge(include_shared=save_shared_knowledge)
                    except Exception as e:
                        print(f"  [ERROR] Saving {vehicle.id}: {e}")

            for vehicle in state.vehicles.values():
                if isinstance(vehicle, IntelligentVehicle):
                    state.knowledge_summaries.append(vehicle.knowledge.summary())

            # Save trip data (tripinfo equivalent via TraCI)
            if save_tripinfo and run_output_dir:
                save_trip_data(state, run_output_dir)

            if run_output_dir:
                save_knowledge_summary(state, run_output_dir)

            # Save performance metrics
            if run_output_dir:
                perf_file = os.path.join(run_output_dir, "performance_metrics.json")
                PerformanceMonitor.save_to_json(perf_file)

                resource_file = os.path.join(run_output_dir, "resource_metrics.json")
                resource_data = resource_monitor.as_dict(
                    sim_duration_actual,
                    sim_time_seconds=traci.simulation.getTime(),
                    sim_steps=state.step,
                    vehicle_counts={
                        "intelligent": state.intelligent_count,
                        "normal": state.normal_count,
                        "total_seen": total_vehs,
                        "active_at_end": len(state.vehicles),
                    },
                )
                resource_data["sharing"] = {
                    "events": state.sharing_events,
                    "share_budget": max(0, int(args.share_budget)),
                    "share_only_congestion": args.share_only_congestion,
                    "share_candidate_threshold": args.share_candidate_threshold,
                    "max_share_age_seconds": max_share_age_seconds,
                    "max_pairs_per_step": max(0, int(args.max_v2v_pairs_per_step)),
                    "pairs_processed": state.sharing_pairs_processed,
                    "pairs_deferred_by_cap": state.sharing_pairs_deferred_by_cap,
                    "attempted_observations": state.sharing_attempted_observations,
                    "accepted_observations": state.sharing_accepted_observations,
                    "skipped_observations": state.sharing_skipped_observations,
                    "acceptance_rate": round(
                        state.sharing_accepted_observations / state.sharing_attempted_observations,
                        4,
                    ) if state.sharing_attempted_observations else None,
                    "directed_delta_links": len(state.share_versions),
                }
                with open(resource_file, "w") as f:
                    json.dump(resource_data, f, indent=2)
                print(f"[FILE] Saved resource metrics: {resource_file}")

                # Patch simulation_info.json with wallclock + sync stats so the
                # distributed aggregator can pick the longest sub-run.
                info_file = os.path.join(run_output_dir, "simulation_info.json")
                if os.path.isfile(info_file):
                    try:
                        with open(info_file, "r", encoding="utf-8") as fp:
                            info = json.load(fp)
                        info["aggregated_runtime_sec"] = round(sim_duration_actual, 2)
                        info["actual_steps"] = state.step
                        if sync_bridge is not None:
                            info["sync_stats"] = {
                                "transport": sync_bridge.stats.transport,
                                "barriers": sync_bridge.stats.barrier_calls,
                                "barrier_timeouts": sync_bridge.stats.barrier_timeouts,
                                "handoffs_out": sync_bridge.stats.handoffs_out,
                                "handoffs_in": sync_bridge.stats.handoffs_in,
                                "handoff_failures": sync_bridge.stats.handoff_failures,
                                "barrier_total_sec": round(
                                    sync_bridge.stats.barrier_total_sec, 2),
                                "self_disabled_at_step": sync_bridge.stats.self_disabled_at_step,
                            }
                        with open(info_file, "w", encoding="utf-8") as fp:
                            json.dump(info, fp, indent=2)
                    except Exception as e:
                        print(f"[WARN] Could not patch simulation_info.json: {e}")
        
        # Cleanup
        if args.gui:
            for pid in traci.polygon.getIDList():
                if pid.startswith("circle_"):
                    try:
                        traci.polygon.remove(pid)
                    except:
                        pass
        
        # Close sync bridge BEFORE closing TraCI so peers can leave the
        # barrier cleanly.
        if sync_bridge is not None:
            try:
                sync_bridge.finish()
            except Exception as e:
                print(f"[WARN] sync bridge finish failed: {e}")

        try:
            traci.close()
        except:
            pass

        if save_environment and run_output_dir and edge_data_output_file:
            save_environment_traffic_data(run_output_dir, edge_data_output_file)

        if node_occupancy is not None and run_output_dir:
            try:
                occ_file = os.path.join(run_output_dir, "node_occupancy.json")
                with open(occ_file, "w", encoding="utf-8") as _fh:
                    json.dump({
                        "group": getattr(args, "sync_group", None),
                        "transport": getattr(sync_bridge, "stats", None)
                        and sync_bridge.stats.transport,
                        "horizon_sec": int(sim_duration),
                        "samples": node_occupancy,
                    }, _fh)
                print(f"[FILE] Saved node occupancy: {occ_file} "
                      f"({len(node_occupancy)} steps)")
            except Exception as _e:
                print(f"[WARN] node_occupancy write failed: {_e}")
        
        if args.debug:
            from SIMULATION.debug_server import stop_debug_server
            stop_debug_server()
        
        print("[DONE] Simulation finished")
    
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
