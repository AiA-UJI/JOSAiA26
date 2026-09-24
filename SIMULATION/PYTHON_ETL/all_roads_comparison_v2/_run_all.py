"""Launcher: regenerate 18-sim mapping (v2), CSV summary, and comparison plots."""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

FINDER = os.path.join(HERE, "_find_18.py")
PLOTTER = os.path.join(HERE, "plot_all_roads.py")
CSV = os.path.join(HERE, "build_summary_csv.py")
BOXPLOTS = os.path.join(HERE, "boxplots_all_roads.py")


def run(label, script):
    print(f"\n{'=' * 60}\n{label}\n{'=' * 60}")
    rc = subprocess.call([sys.executable, script])
    if rc != 0:
        print(f"[ERROR] {label} exit={rc}")
        sys.exit(rc)


def main():
    run("1/4 Locating simulation folders", FINDER)
    run("2/4 Building per-case summary CSV", CSV)
    run("3/4 Generating time-series plots", PLOTTER)
    run("4/4 Generating boxplots", BOXPLOTS)
    print("\n[ALL DONE]")
    print(f"Results in: {os.path.join(HERE, 'results')}")


if __name__ == "__main__":
    main()
