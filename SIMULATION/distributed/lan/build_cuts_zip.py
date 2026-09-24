"""Collect every partition-map PNG (the network "cuts") into one ZIP, naming
each image after its cut type (e.g. balanced-y-6.png, corridor-3.png).

Reuses already-rendered maps and generates any missing one (balanced-y-6).
"""

from __future__ import annotations
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "results"
SRC = ROOT / "20260630_075315" / "partitions"   # latest complete set
STAGE = ROOT / "_cuts"
ZIP = ROOT / "cortes_particiones.zip"

# extra cuts to render if missing (used in the scaling study)
EXTRA = ["balanced-y-6"]
# accident-aware versions of the balanced-y cuts (boundary shifts near the crash)
ACC_CUTS = ["balanced-y-2", "balanced-y-3", "balanced-y-4", "balanced-y-6"]


def main():
    STAGE.mkdir(parents=True, exist_ok=True)
    # 1) copy existing maps, stripping the "partition_" prefix
    for p in sorted(SRC.glob("partition_*.png")):
        dst = STAGE / p.name.replace("partition_", "", 1)
        shutil.copyfile(p, dst)

    from SIMULATION.distributed.lan.plot_partition_map import plot_partition
    # 2) render any missing cut
    for cut in EXTRA:
        dst = STAGE / f"{cut}.png"
        if not dst.exists():
            try:
                plot_partition(cut, dst, accident=False)
            except Exception as e:
                print(f"[cuts] {cut} FAILED: {e}")

    # 2b) accident-aware versions of the balanced-y cuts
    for cut in ACC_CUTS:
        dst = STAGE / f"{cut}_acc.png"
        try:
            plot_partition(cut, dst, accident=True)
        except Exception as e:
            print(f"[cuts] {cut} acc FAILED: {e}")

    # 3) zip everything
    pngs = sorted(STAGE.glob("*.png"))
    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED) as z:
        for p in pngs:
            z.write(p, arcname=p.name)
    print(f"[cuts] {ZIP}  ({len(pngs)} imagenes)")
    for p in pngs:
        print("  ", p.name)


if __name__ == "__main__":
    main()
