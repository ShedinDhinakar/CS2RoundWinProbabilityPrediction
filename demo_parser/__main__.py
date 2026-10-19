"""Batch-parse every demo under a folder.

    python -m demo_parser                                   # demo_parser/sample_data -> demo_parser/parsed_data
    python -m demo_parser path/to/demos --out path/to/out --workers 4
"""

import argparse
import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from .config import FIRE_SLOTS, SAMPLE_INTERVAL, SMOKE_SLOTS
from .discover import find_matches
from .pipeline import run_match

PACKAGE_DIR = Path(__file__).resolve().parent


def main() -> None:
    ap = argparse.ArgumentParser(prog="demo_parser")
    ap.add_argument("input", type=Path, nargs="?", default=PACKAGE_DIR / "sample_data",
                    help="folder searched recursively for .dem files")
    ap.add_argument("--out", type=Path, default=PACKAGE_DIR / "parsed_data")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) // 4))
    ap.add_argument("--interval", type=int, default=SAMPLE_INTERVAL, help="ticks between snapshots")
    ap.add_argument("--smoke-slots", type=int, default=SMOKE_SLOTS, help="smoke columns per snapshot")
    ap.add_argument("--fire-slots", type=int, default=FIRE_SLOTS, help="molotov/incendiary columns per snapshot")
    args = ap.parse_args()

    matches = find_matches(args.input)
    print(f"{len(matches)} matches found under {args.input}")
    start = time.perf_counter()
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            # mirror the input folder layout under --out
            pool.submit(run_match, paths, args.out / key.parent.relative_to(args.input), key.name,
                        args.interval, args.smoke_slots, args.fire_slots): key
            for key, paths in matches.items()
        }
        for fut in as_completed(futures):
            try:
                r = fut.result()
                print(f"[ok] {r['match']}: {r['rows']} rows x {r['cols']} cols, {r['utility_rows']} utility rows, {r['seconds']}s")
                if r["overflow"]:
                    print(f"  [WARN] {r['match']}: up to {r['overflow']} active smokes/fires had no free slot; raise --smoke-slots/--fire-slots")
            except Exception as e:
                print(f"[SKIPPED] {futures[fut].name}: {type(e).__name__}: {e}")
    print(f"done in {time.perf_counter() - start:.1f}s")


if __name__ == "__main__":
    main()
