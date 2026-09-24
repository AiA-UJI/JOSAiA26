#!/usr/bin/env python3
"""
Fast standalone SUMO runner for tripinfo collection.

Runs SUMO natively (NO TraCI) — completes in minutes instead of hours.
Replicates:
  - Accident via variableSpeedSign (same edges, timing, speed limit)
  - Intelligent vehicle rerouting via SUMO's device.rerouting
  - Normal vehicles keep their fixed route

Usage:
    python run_fast.py                  # Run all combinations
    python run_fast.py --road modified_2ways --ratio 10 --accident
    python run_fast.py --only-table     # Re-generate table from existing results
"""

import os
import sys
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
import json
import shutil
import time
import argparse

SCRIPT_DIR = Path(__file__).parent.absolute()
SIMULATION_DIR = SCRIPT_DIR.parent
PROJECT_ROOT = SIMULATION_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    import numpy as np
except ImportError:
    np = None

import sumolib

from SIMULATION.CONSTANTS import get_road_config, DEFAULTS


# ═══════════════════════════════════════════════════════════════
# ACCIDENT: variableSpeedSign generation
# ═══════════════════════════════════════════════════════════════

def find_accident_route(net_file, start_edge_id, end_edge_id):
    """Find all edges between start and end using sumolib shortest path."""
    net = sumolib.net.readNet(net_file)
    start = net.getEdge(start_edge_id)
    end = net.getEdge(end_edge_id)
    route, _ = net.getShortestPath(start, end)
    if route:
        return [e.getID() for e in route]
    return [start_edge_id, end_edge_id]


def generate_accident_additional(net_file, start_edge_id, end_edge_id,
                                  start_time, duration, speed_limit,
                                  output_file):
    """Generate variableSpeedSign additional file to simulate accident."""
    edges = find_accident_route(net_file, start_edge_id, end_edge_id)
    end_time = start_time + duration
    net = sumolib.net.readNet(net_file)

    root = ET.Element("additional")
    for edge_id in edges:
        edge = net.getEdge(edge_id)
        lanes = " ".join(f"{edge_id}_{i}" for i in range(edge.getLaneNumber()))
        vss = ET.SubElement(root, "variableSpeedSign",
                            id=f"vss_{edge_id}", lanes=lanes)
        ET.SubElement(vss, "step", time=str(float(start_time)), speed=str(speed_limit))
        ET.SubElement(vss, "step", time=str(float(end_time)), speed="-1")

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write(output_file, xml_declaration=True, encoding="UTF-8")
    return edges


# ═══════════════════════════════════════════════════════════════
# TRIPS: add rerouting device to intelligent vType
# ═══════════════════════════════════════════════════════════════

def create_modified_trips(original_trips, output_trips, rerouting_period=30):
    """Copy trips file adding rerouting device to intelligent vType."""
    tree = ET.parse(original_trips)
    root = tree.getroot()

    for vtype in root.findall("vType"):
        if vtype.get("id") == "intelligent":
            p1 = ET.SubElement(vtype, "param")
            p1.set("key", "has.rerouting.device")
            p1.set("value", "true")
            p2 = ET.SubElement(vtype, "param")
            p2.set("key", "device.rerouting.period")
            p2.set("value", str(rerouting_period))

    ET.indent(tree, space="  ")
    tree.write(output_trips, xml_declaration=True, encoding="UTF-8")


# ═══════════════════════════════════════════════════════════════
# SUMO execution
# ═══════════════════════════════════════════════════════════════

def run_sumo(net_file, trips_file, additional_file, tripinfo_output,
             sim_duration=14400, step_length=0.1):
    """Run SUMO as a standalone process (no TraCI)."""
    sumo_binary = shutil.which("sumo")
    if not sumo_binary:
        print("[ERROR] 'sumo' not found in PATH")
        return False

    cmd = [
        sumo_binary,
        "--net-file", net_file,
        "--route-files", trips_file,
        "--begin", "0",
        "--end", str(sim_duration),
        "--step-length", str(step_length),
        "--time-to-teleport", "300",
        "--tripinfo-output", tripinfo_output,
        "--no-warnings", "true",
        "--verbose", "true",
    ]

    if additional_file:
        cmd.extend(["--additional-files", additional_file])

    t0 = time.perf_counter()
    result = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.perf_counter() - t0

    if result.returncode != 0:
        print(f"  [FAIL] SUMO exit code {result.returncode}")
        stderr = result.stderr.strip()
        if stderr:
            for line in stderr.split('\n')[:10]:
                print(f"    {line}")
        return False

    print(f"  [OK] Completed in {elapsed:.1f}s")
    return True


# ═══════════════════════════════════════════════════════════════
# tripinfo parsing
# ═══════════════════════════════════════════════════════════════

def parse_tripinfo(tripinfo_file):
    """Parse tripinfo.xml → list of dicts with per-vehicle metrics."""
    records = []
    for _, elem in ET.iterparse(tripinfo_file, events=('end',)):
        if elem.tag == 'tripinfo':
            duration = float(elem.get('duration', 0))
            route_length = float(elem.get('routeLength', 0))
            if duration > 0 and route_length > 10:
                records.append({
                    'id': elem.get('id'),
                    'vType': elem.get('vType', 'unknown'),
                    'depart': float(elem.get('depart', 0)),
                    'arrival': float(elem.get('arrival', 0)),
                    'duration': duration,
                    'routeLength': route_length,
                    'waitingTime': float(elem.get('waitingTime', 0)),
                    'timeLoss': float(elem.get('timeLoss', 0)),
                    'speed_kmh': (route_length / duration) * 3.6,
                })
            elem.clear()
    return records


# ═══════════════════════════════════════════════════════════════
# RUN CONFIGURATIONS
# ═══════════════════════════════════════════════════════════════

CONFIGURATIONS = [
    {
        "road": "modified_2ways",
        "ratios": {
            "10": "trips/modified_2ways_trips_10_10000.xml",
            "99": "trips/modified_2ways_trips_99_10000.xml",
        },
        "net": "roads/modified_2ways.net.xml",
    },
    {
        "road": "modified_3ways_top",
        "ratios": {
            "10": "trips/fullnet_trips_intelligent_10_cv_10000.xml",
            "99": "trips/fullnet_trips_intelligent_99_cv_10000.xml",
        },
        "net": "roads/modified_3ways_top.net.xml",
    },
    {
        "road": "modified_4ways_all",
        "ratios": {
            "10": "trips/fullnet_trips_intelligent_10_cv_10000.xml",
            "99": "trips/fullnet_trips_intelligent_99_cv_10000.xml",
        },
        "net": "roads/modified_4ways_all.net.xml",
    },
]


def mean(values):
    if not values:
        return 0.0
    return sum(values) / len(values)


def std(values):
    if not values or len(values) < 2:
        return 0.0
    m = mean(values)
    return (sum((x - m) ** 2 for x in values) / len(values)) ** 0.5


# ═══════════════════════════════════════════════════════════════
# TABLE GENERATION
# ═══════════════════════════════════════════════════════════════

def generate_latex_table(all_results):
    """Generate LaTeX table from collected results."""
    road_labels = {
        'modified_2ways': 'Two Itin.',
        'modified_3ways_top': 'Three Itin.',
        'modified_4ways_all': 'Four Itin.',
    }
    ratios = ['10', '99']
    roads = ['modified_2ways', 'modified_3ways_top', 'modified_4ways_all']

    lines = []
    lines.append(r"\begin{table}[h!]")
    lines.append(r"\centering")
    lines.append(r"\caption{Average trip metrics per vehicle (10{,}000 vehicles, step=0.1s)}")
    lines.append(r"\label{tab:trip_metrics}")
    lines.append(r"\resizebox{\textwidth}{!}{%")
    lines.append(r"\begin{tabular}{ll|rr|rr|rr}")
    lines.append(r"\hline")
    lines.append(r" & & \multicolumn{2}{c|}{Avg Speed (km/h)} & \multicolumn{2}{c|}{Avg Distance (m)} & \multicolumn{2}{c}{Avg Duration (s)} \\")
    lines.append(r"Network & Accident & 10\% SV & 99\% SV & 10\% SV & 99\% SV & 10\% SV & 99\% SV \\")
    lines.append(r"\hline")

    for road in roads:
        road_label = road_labels.get(road, road)
        for acc in ['acc', 'noacc']:
            acc_label = "Yes" if acc == "acc" else "No"
            vals = []
            for ratio in ratios:
                label = f"{road}_{ratio}pct_{acc}"
                records = all_results.get(label, [])
                if records:
                    speeds = [r['speed_kmh'] for r in records]
                    distances = [r['routeLength'] for r in records]
                    durations = [r['duration'] for r in records]
                    vals.append((mean(speeds), mean(distances), mean(durations)))
                else:
                    vals.append((0, 0, 0))

            s10, d10, t10 = vals[0]
            s99, d99, t99 = vals[1]
            lines.append(f"{road_label} & {acc_label} & {s10:.1f} & {s99:.1f} & {d10:.0f} & {d99:.0f} & {t10:.0f} & {t99:.0f} \\\\")
        lines.append(r"\hline")

    lines.append(r"\end{tabular}")
    lines.append(r"}%")
    lines.append(r"\end{table}")

    table_str = "\n".join(lines)
    print(table_str)
    return table_str


def generate_text_table(all_results):
    """Generate readable text table."""
    road_labels = {
        'modified_2ways': '2-ways',
        'modified_3ways_top': '3-ways',
        'modified_4ways_all': '4-ways',
    }
    ratios = ['10', '99']
    roads = ['modified_2ways', 'modified_3ways_top', 'modified_4ways_all']

    header = f"{'Network':<10} {'Acc':<4} | {'Spd10':>7} {'Spd99':>7} | {'Dist10':>8} {'Dist99':>8} | {'Dur10':>7} {'Dur99':>7} | {'Wait10':>7} {'Wait99':>7} | {'N10':>5} {'N99':>5}"
    print(header)
    print("-" * len(header))

    for road in roads:
        rl = road_labels.get(road, road)
        for acc in ['acc', 'noacc']:
            row = [f"{rl:<10}", f"{'Y' if acc == 'acc' else 'N':<4}"]
            for metric in ['speed_kmh', 'routeLength', 'duration', 'waitingTime']:
                for ratio in ratios:
                    label = f"{road}_{ratio}pct_{acc}"
                    records = all_results.get(label, [])
                    if records:
                        vals = [r[metric] for r in records]
                        row.append(f"{mean(vals):>7.1f}" if metric == 'speed_kmh' else f"{mean(vals):>8.0f}" if metric == 'routeLength' else f"{mean(vals):>7.0f}")
                    else:
                        row.append(f"{'---':>7}" if metric != 'routeLength' else f"{'---':>8}")
            for ratio in ratios:
                label = f"{road}_{ratio}pct_{acc}"
                records = all_results.get(label, [])
                row.append(f"{len(records):>5}")
            print(" | ".join([row[0] + " " + row[1]] + [row[i] + " " + row[i+1] for i in range(2, len(row), 2)]))
        print("-" * len(header))


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Fast SUMO tripinfo runner (no TraCI)")
    parser.add_argument('--road', type=str, help='Specific road to run (e.g. modified_2ways)')
    parser.add_argument('--ratio', type=str, help='Specific ratio (10 or 99)')
    parser.add_argument('--accident', action='store_true', help='Run only with accident')
    parser.add_argument('--no-accident', action='store_true', help='Run only without accident')
    parser.add_argument('--only-table', action='store_true', help='Re-generate table from existing results')
    parser.add_argument('--step-length', type=float, default=0.1, help='Simulation step length (default: 0.1)')
    parser.add_argument('--duration', type=int, default=14400, help='Simulation duration in seconds (default: 14400)')
    args = parser.parse_args()

    results_dir = SCRIPT_DIR / "results"
    results_dir.mkdir(exist_ok=True)
    temp_dir = SCRIPT_DIR / "temp"
    temp_dir.mkdir(exist_ok=True)

    # If --only-table, load existing tripinfo files and generate table
    if args.only_table:
        all_results = load_existing_results(results_dir)
        if all_results:
            generate_text_table(all_results)
            print()
            latex = generate_latex_table(all_results)
            with open(results_dir / "table.tex", 'w') as f:
                f.write(latex)
            print(f"\nLaTeX saved to {results_dir / 'table.tex'}")
        else:
            print("No results found in", results_dir)
        return

    # Determine which configurations to run
    configs = CONFIGURATIONS
    if args.road:
        configs = [c for c in configs if c['road'] == args.road]
        if not configs:
            print(f"[ERROR] Road '{args.road}' not found. Available: {[c['road'] for c in CONFIGURATIONS]}")
            return

    all_results = {}
    total_t0 = time.perf_counter()
    run_count = 0

    for config in configs:
        road = config['road']
        net_file = str(SIMULATION_DIR / config['net'])

        if not Path(net_file).exists():
            print(f"[SKIP] Network not found: {config['net']}")
            continue

        road_config = get_road_config(road)
        accident_edges = road_config.get("accident_edges")

        # Pre-compute accident additional file (shared across ratios)
        accident_additional = None
        if accident_edges:
            accident_additional = str(temp_dir / f"accident_{road}.add.xml")
            acc_start = road_config.get("accident_start_time", DEFAULTS["accident_start_time"])
            acc_dur = road_config.get("accident_duration", DEFAULTS["accident_duration"])
            acc_speed = road_config.get("accident_speed_limit", DEFAULTS["accident_speed_limit"])
            edges = generate_accident_additional(
                net_file, accident_edges[0], accident_edges[1],
                acc_start, acc_dur, acc_speed, accident_additional
            )
            print(f"\n[ACCIDENT] {road}: {len(edges)} edges affected ({accident_edges[0]} -> {accident_edges[1]})")

        ratios_to_run = config['ratios']
        if args.ratio:
            ratios_to_run = {k: v for k, v in ratios_to_run.items() if k == args.ratio}

        for ratio, trips_rel in ratios_to_run.items():
            trips_file = str(SIMULATION_DIR / trips_rel)
            if not Path(trips_file).exists():
                print(f"[SKIP] Trips not found: {trips_rel}")
                continue

            # Create modified trips with rerouting device for intelligent vehicles
            modified_trips = str(temp_dir / f"trips_{road}_{ratio}.xml")
            create_modified_trips(trips_file, modified_trips)

            accident_variants = []
            if not args.no_accident:
                accident_variants.append(True)
            if not args.accident:
                accident_variants.append(False)

            for with_accident in accident_variants:
                label = f"{road}_{ratio}pct_{'acc' if with_accident else 'noacc'}"
                tripinfo_file = str(results_dir / f"tripinfo_{label}.xml")

                print(f"\n{'='*60}")
                print(f"  {label}")
                print(f"  Network: {Path(net_file).name} | Trips: {Path(trips_rel).name}")
                print(f"  Accident: {'YES' if with_accident else 'NO'} | Duration: {args.duration}s")
                print(f"{'='*60}")

                additional = accident_additional if with_accident else None
                success = run_sumo(net_file, modified_trips, additional,
                                   tripinfo_file, args.duration, args.step_length)

                if success and Path(tripinfo_file).exists():
                    records = parse_tripinfo(tripinfo_file)
                    all_results[label] = records
                    n_int = sum(1 for r in records if r['vType'] == 'intelligent')
                    n_nor = sum(1 for r in records if r['vType'] != 'intelligent')
                    print(f"  Vehicles: {len(records)} total ({n_int} intelligent, {n_nor} normal)")
                    if records:
                        speeds = [r['speed_kmh'] for r in records]
                        print(f"  Avg speed: {mean(speeds):.1f} km/h")
                else:
                    print(f"  [FAIL] No tripinfo generated")

                run_count += 1

    total_elapsed = time.perf_counter() - total_t0
    print(f"\n{'='*60}")
    print(f"  ALL DONE: {run_count} simulations in {total_elapsed:.1f}s ({total_elapsed/60:.1f} min)")
    print(f"{'='*60}")

    if all_results:
        print("\n" + "=" * 80)
        print("  TEXT TABLE")
        print("=" * 80)
        generate_text_table(all_results)

        print("\n" + "=" * 80)
        print("  LATEX TABLE")
        print("=" * 80)
        latex = generate_latex_table(all_results)

        with open(results_dir / "table.tex", 'w') as f:
            f.write(latex)

        # Save summary JSON
        summary = {}
        for label, records in all_results.items():
            speeds = [r['speed_kmh'] for r in records]
            distances = [r['routeLength'] for r in records]
            durations = [r['duration'] for r in records]
            waiting = [r['waitingTime'] for r in records]
            summary[label] = {
                'n_vehicles': len(records),
                'avg_speed_kmh': round(mean(speeds), 2),
                'std_speed_kmh': round(std(speeds), 2),
                'avg_distance_m': round(mean(distances), 2),
                'avg_duration_s': round(mean(durations), 2),
                'avg_waiting_s': round(mean(waiting), 2),
            }
        with open(results_dir / "summary.json", 'w') as f:
            json.dump(summary, f, indent=2)

        print(f"\nResults: {results_dir}")
        print(f"LaTeX:   {results_dir / 'table.tex'}")
        print(f"JSON:    {results_dir / 'summary.json'}")

    # Cleanup temp
    shutil.rmtree(temp_dir, ignore_errors=True)


def load_existing_results(results_dir):
    """Load all existing tripinfo XML files from results directory."""
    all_results = {}
    for f in sorted(results_dir.glob("tripinfo_*.xml")):
        label = f.stem.replace("tripinfo_", "")
        try:
            records = parse_tripinfo(str(f))
            if records:
                all_results[label] = records
                print(f"  Loaded {label}: {len(records)} vehicles")
        except Exception as e:
            print(f"  Error loading {f.name}: {e}")
    return all_results


if __name__ == "__main__":
    main()
