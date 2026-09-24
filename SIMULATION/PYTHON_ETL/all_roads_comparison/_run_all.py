"""Launcher: regenerate 18-sim mapping, CSV summary, and comparison plots."""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)

FINDER = os.path.join(PARENT, "_find_18.py")
PLOTTER = os.path.join(HERE, "plot_all_roads.py")
CSV = os.path.join(HERE, "build_summary_csv.py")


def run(label, script):
    print(f"\n{'=' * 60}\n{label}\n{'=' * 60}")
    rc = subprocess.call([sys.executable, script])
    if rc != 0:
        print(f"[ERROR] {label} exit={rc}")
        sys.exit(rc)


def main():
    run("1/3 Locating simulation folders", FINDER)
    run("2/3 Building per-case summary CSV", CSV)
    run("3/3 Generating comparison plots", PLOTTER)
    print("\n[ALL DONE]")
    print(f"Results in: {os.path.join(HERE, 'results')}")


if __name__ == "__main__":
    main()
