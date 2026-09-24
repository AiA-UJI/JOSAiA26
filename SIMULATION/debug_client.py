#!/usr/bin/env python3
"""
Interactive Terminal Client for V2V Simulation Debug
Connect to a running simulation and inspect vehicle state.

Usage: python debug_client.py
"""
import argparse
import os
import requests
import json
import sys
import time

try:
    import readline  # Enables command history / arrow navigation (on Unix)
except ImportError:
    readline = None

DEFAULT_PORT = 8080
PORT_FILE = os.path.join(os.path.dirname(__file__), "debug_port.txt")
HISTORY_FILE = os.path.join(os.path.expanduser("~"), ".v2v_debug_history")


def resolve_port(cli_port=None):
    """Determine which port to talk to."""
    if cli_port:
        return cli_port
    # Env var override
    env_port = os.environ.get("V2V_DEBUG_PORT")
    if env_port:
        return int(env_port)
    # Port file written by debug server
    try:
        if os.path.exists(PORT_FILE):
            with open(PORT_FILE, "r", encoding="utf-8") as f:
                return int(f.read().strip())
    except (OSError, ValueError):
        pass
    return DEFAULT_PORT


BASE_URL = None


def fetch(endpoint):
    """Fetch data from debug server."""
    try:
        response = requests.get(f"{BASE_URL}/{endpoint}", timeout=2)
        return response.json()
    except requests.exceptions.ConnectionError:
        return {"error": "Cannot connect to debug server. Is the simulation running?"}
    except Exception as e:
        return {"error": str(e)}


def print_colored(text, color="white"):
    """Print colored text."""
    colors = {
        "red": "\033[91m",
        "green": "\033[92m",
        "yellow": "\033[93m",
        "blue": "\033[94m",
        "magenta": "\033[95m",
        "cyan": "\033[96m",
        "white": "\033[97m",
        "reset": "\033[0m"
    }
    print(f"{colors.get(color, '')}{text}{colors['reset']}")


def cmd_status():
    """Show simulation status."""
    data = fetch("status")
    if "error" in data:
        print_colored(f"Error: {data['error']}", "red")
        return
    
    print_colored("\n=== Simulation Status ===", "cyan")
    print(f"  Step:           {data['step']}")
    print(f"  Vehicles:       {data['vehicle_count']}")
    print(f"  Sharing Events: {data['sharing_events']}")
    print(f"  Pairs in Range: {data['pairs_in_range']}")
    print(f"  Active Pairs:   {data['active_shared_pairs']}")


def cmd_vehicles():
    """List all vehicles."""
    data = fetch("vehicles")
    if "error" in data:
        print_colored(f"Error: {data['error']}", "red")
        return
    
    print_colored(f"\n=== Vehicles ({data['count']}) ===", "cyan")
    for v in data['vehicles']:
        pos = f"({v['position'][0]:.0f}, {v['position'][1]:.0f})" if v['position'] else "N/A"
        print(f"  {v['id']:>8} | {v['current_edge'] or 'N/A':>10} | {v['speed']:>5.1f} m/s | "
              f"pos {pos:>15} | sens:{v['sensor_count']:>2} shared:{v['shared_count']:>2}")


def cmd_vehicle(veh_id):
    """Show vehicle detail."""
    data = fetch(f"vehicle?id={veh_id}")
    if "error" in data:
        print_colored(f"Error: {data['error']}", "red")
        return
    
    print_colored(f"\n=== Vehicle: {data['id']} ===", "cyan")
    pos = f"({data['position'][0]:.1f}, {data['position'][1]:.1f})" if data['position'] else "N/A"
    print(f"  Position:      {pos}")
    print(f"  Speed:         {data['speed']} m/s")
    print(f"  Current Edge:  {data['current_edge'] or 'N/A'}")
    print(f"  Target Edge:   {data['target_edge'] or 'N/A'}")
    print(f"  Last Observed: {data['last_observed_edge'] or 'N/A'}")
    
    print_colored("\n  Sharing Partners:", "yellow")
    if data['sharing_partners']:
        for p in data['sharing_partners']:
            status = "[*]" if p['in_range'] else "[ ]"
            color = "green" if p['in_range'] else "red"
            print_colored(f"    {status} {p['id']} (step {p['shared_at_step']}, {p['distance']}m)", color)
    else:
        print("    None")
    
    print_colored("\n  Knowledge Summary:", "yellow")
    k = data['knowledge_summary']
    print(f"    Sensors: {k['sensors']['total_observations']} observations on {len(k['sensors']['segments'])} segments")
    print(f"    Shared:  {k['shared']['total_observations']} observations on {len(k['shared']['segments'])} segments")
    
    if k['sensors']['segments']:
        print(f"    Sensor segments: {', '.join(k['sensors']['segments'][:5])}{'...' if len(k['sensors']['segments']) > 5 else ''}")
    if k['shared']['segments']:
        print(f"    Shared segments: {', '.join(k['shared']['segments'][:5])}{'...' if len(k['shared']['segments']) > 5 else ''}")


def cmd_knowledge(veh_id):
    """Show full knowledge for a vehicle."""
    data = fetch(f"knowledge?id={veh_id}")
    if "error" in data:
        print_colored(f"Error: {data['error']}", "red")
        return
    
    print_colored(f"\n=== Knowledge: {data['vehicle_id']} ===", "cyan")
    
    print_colored("\n  Sensor Observations:", "yellow")
    if data['sensors']:
        for segment, slots in data['sensors'].items():
            for slot, observations in slots.items():
                for obs in observations:
                    print(f"    {segment} [slot {slot}]: speed={obs['speed']:.1f}m/s, count={obs['count']}")
    else:
        print("    None")
    
    print_colored("\n  Shared Observations:", "yellow")
    if data['shared']:
        count = 0
        for segment, slots in data['shared'].items():
            for slot, observations in slots.items():
                for obs in observations:
                    print(f"    {segment} [slot {slot}]: speed={obs['speed']:.1f}m/s, count={obs['count']}")
                    count += 1
                    if count >= 20:
                        print(f"    ... and more (showing first 20)")
                        return
    else:
        print("    None")


def cmd_pairs():
    """Show all sharing pairs."""
    data = fetch("pairs")
    if "error" in data:
        print_colored(f"Error: {data['error']}", "red")
        return
    
    print_colored(f"\n=== Sharing Pairs ({data['total_shared']}) ===", "cyan")
    print(f"  Currently in range: {data['currently_in_range']}")
    
    for p in data['pairs']:
        status_icon = "[*]" if p['in_range'] else "[ ]"
        color = "green" if p['in_range'] else "white"
        print_colored(f"  {status_icon} {p['vehicles'][0]} <-> {p['vehicles'][1]} "
                     f"(step {p['shared_at_step']}, {p['distance']}m)", color)


def cmd_watch(interval=2):
    """Watch simulation status in real-time."""
    print_colored("Watching simulation (Ctrl+C to stop)...\n", "yellow")
    try:
        while True:
            # Clear screen
            print("\033[H\033[J", end="")
            
            status = fetch("status")
            if "error" in status:
                print_colored(f"Error: {status['error']}", "red")
                time.sleep(interval)
                continue
            
            print_colored("=== V2V Simulation Watch ===", "cyan")
            print(f"Step: {status['step']} | Vehicles: {status['vehicle_count']} | "
                  f"Sharing Events: {status['sharing_events']} | Pairs: {status['active_shared_pairs']}")
            print()
            
            vehicles = fetch("vehicles")
            if "vehicles" in vehicles:
                print_colored("Vehicles:", "yellow")
                for v in vehicles['vehicles'][:15]:  # Show first 15
                    print(f"  {v['id']:>8} | {v['current_edge'] or 'N/A':>10} | "
                          f"{v['speed']:>5.1f} m/s | sens:{v['sensor_count']:>2} shared:{v['shared_count']:>2}")
                if len(vehicles['vehicles']) > 15:
                    print(f"  ... and {len(vehicles['vehicles']) - 15} more")
            
            print()
            pairs = fetch("pairs")
            if "pairs" in pairs and pairs['pairs']:
                print_colored("Recent Pairs:", "yellow")
                for p in pairs['pairs'][-5:]:  # Show last 5
                    status_icon = "[*]" if p['in_range'] else "[ ]"
                    print(f"  {status_icon} {p['vehicles'][0]} <-> {p['vehicles'][1]} ({p['distance']}m)")
            
            print(f"\nRefreshing every {interval}s... (Ctrl+C to stop)")
            time.sleep(interval)
            
    except KeyboardInterrupt:
        print("\nStopped watching.")


def cmd_filters():
    """Show filter pipeline statistics."""
    data = fetch("filter-stats")
    if "error" in data:
        print_colored(f"Error: {data['error']}", "red")
        return
    
    print_colored("\n=== Filter Statistics ===", "cyan")
    print(f"  Active filters: {', '.join(data['active_filters']) or 'none'}")
    print()
    
    if data["filters"]:
        print(f"  {'Filter':<30} {'Accepted':>10} {'Rejected':>10} {'Rej %':>8}")
        print(f"  {'-'*30} {'-'*10} {'-'*10} {'-'*8}")
        for f in data["filters"]:
            print(f"  {f['name']:<30} {f['accepted']:>10} {f['rejected']:>10} {f['rejection_rate']:>7.1f}%")
        print()
        print(f"  Total received:  {data['total_received']}")
        print(f"  Total accepted:  {data['total_accepted']}")
        print(f"  Total rejected:  {data['total_rejected']}")
    else:
        print("  No filter data recorded yet.")


def cmd_help():
    """Show help."""
    print_colored("\n=== V2V Debug Client Commands ===", "cyan")
    print("""
  status              - Show simulation status
  vehicles            - List all vehicles
  vehicle <id>        - Show detailed info for a vehicle
  knowledge <id>      - Show full knowledge data for a vehicle
  pairs               - Show all sharing pairs
  filters             - Show filter pipeline statistics
  watch [interval]    - Watch simulation in real-time (default: 2s)
  help                - Show this help
  quit / exit         - Exit the client
  
  Examples:
    vehicle veh4
    knowledge veh28
    watch 1
""")


def main():
    parser = argparse.ArgumentParser(description="V2V Simulation Debug Client")
    parser.add_argument("--port", type=int, help="Debug server port (default: auto-detect)")
    args = parser.parse_args()
    
    # Setup history (if readline available)
    if readline:
        try:
            readline.read_history_file(HISTORY_FILE)
        except FileNotFoundError:
            pass
        readline.set_history_length(500)
    
    global BASE_URL
    port = resolve_port(args.port)
    BASE_URL = f"http://localhost:{port}/api"
    
    print_colored("╔══════════════════════════════════════════╗", "cyan")
    print_colored("║   V2V Simulation Debug Client            ║", "cyan")
    print_colored("║   Type 'help' for commands               ║", "cyan")
    print_colored("╚══════════════════════════════════════════╝", "cyan")
    print_colored(f"(connecting to http://localhost:{port})", "magenta")
    
    # Check connection
    status = fetch("status")
    if "error" in status:
        print_colored(f"\n[WARN] {status['error']}", "yellow")
        print("Make sure the simulation is running with debug server enabled.")
    else:
        print_colored(f"\n[OK] Connected! Simulation at step {status['step']}", "green")
    
    while True:
        try:
            cmd = input("\n> ").strip().lower()
            
            if not cmd:
                continue
            elif cmd in ("quit", "exit", "q"):
                print("Goodbye!")
                break
            elif cmd == "help":
                cmd_help()
            elif cmd == "status":
                cmd_status()
            elif cmd == "vehicles":
                cmd_vehicles()
            elif cmd.startswith("vehicle "):
                veh_id = cmd.split()[1]
                cmd_vehicle(veh_id)
            elif cmd.startswith("knowledge "):
                veh_id = cmd.split()[1]
                cmd_knowledge(veh_id)
            elif cmd == "pairs":
                cmd_pairs()
            elif cmd == "filters":
                cmd_filters()
            elif cmd.startswith("watch"):
                parts = cmd.split()
                interval = int(parts[1]) if len(parts) > 1 else 2
                cmd_watch(interval)
            else:
                print_colored(f"Unknown command: {cmd}. Type 'help' for commands.", "yellow")
                
        except KeyboardInterrupt:
            print("\nGoodbye!")
            break
        except Exception as e:
            print_colored(f"Error: {e}", "red")

    # Persist history
    if readline:
        try:
            readline.write_history_file(HISTORY_FILE)
        except OSError:
            pass


if __name__ == "__main__":
    main()

