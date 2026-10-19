"""Check that no snapshot row depends on anything after its own tick.

    python -m demo_parser.leakage_check                 # demos under demo_parser/sample_data
    python -m demo_parser.leakage_check path/to/demos --cuts 20 --seed 7

1. Truncation: each match is re-parsed as if the demo ended at tick T (every event table, game
   event and fire entity cut at T). Rows at or before T must be identical to the full parse in
   every column except the `ct_win` label. Cut points include random rows and the ticks of
   plants, kills, smoke pops and fire starts. Player snapshots are per-tick reads of the demo,
   so check 2 covers them.
2. Snapshots vs events: no player may be dead before their death event, or show less health
   than their latest damage event so far allows.

Split (multi-part) matches are skipped by the truncation check.
"""

import argparse
import random
import sys
from pathlib import Path

import polars as pl
from awpy import Demo

from . import pipeline
from .discover import find_matches
from .events import round_samples, valid_rounds, with_part
from .players import player_frame

PACKAGE_DIR = Path(__file__).resolve().parent
NO_CUT = 2**31 - 1


def _cut(df: pl.DataFrame, T: int) -> pl.DataFrame:
    return df.filter(pl.col("tick").cast(pl.Int64) <= T)


class _CutEvents:
    def __init__(self, events, T):
        self._events, self._T = events, T

    @property
    def names(self):
        return self._events.names

    def __contains__(self, name):
        return name in self._events

    def __getitem__(self, name):
        return _cut(self._events[name], self._T)


class _CutRecording:
    """One recording whose entity history ends at T; snapshots pass through."""

    def __init__(self, recording, T, cache):
        self._rec, self._T, self._cache = recording, T, cache
        self.tick_rate, self.paths = recording.tick_rate, recording.paths

    def snapshots(self, **kwargs):
        return self._rec.snapshots(**kwargs)

    def ticks(self, props, players_only=True, **kwargs):
        key = (tuple(props), players_only)
        if key not in self._cache:
            self._cache[key] = self._rec.ticks(props, players_only=players_only, **kwargs)
        return _cut(self._cache[key], self._T)


class _CutDemo:
    """A single-file Demo whose event history ends at tick T."""

    def __init__(self, demo, T, cache):
        self._demo, self._T = demo, T
        self.header, self.tick_rate = demo.header, demo.tick_rate
        self.parts = [_CutRecording(demo.parts[0], T, cache)]

    def load(self, *names):
        self._demo.load(*names)

    rounds = property(lambda self: self._demo.rounds)  # sample range and label only
    players = property(lambda self: self._demo.players)  # static roster names
    kills = property(lambda self: _cut(self._demo.kills, self._T))
    damages = property(lambda self: _cut(self._demo.damages, self._T))
    bomb = property(lambda self: _cut(self._demo.bomb, self._T))
    events = property(lambda self: _CutEvents(self._demo.events, self._T))


def _parse_until(demo, T: int, cache: dict) -> pl.DataFrame:
    real_demo = pipeline.Demo
    pipeline.Demo = lambda *_a, **_k: _CutDemo(demo, T, cache)
    try:
        snapshots, _ = pipeline.parse_match([demo.paths[0]], "check")
    finally:
        pipeline.Demo = real_demo
    return snapshots


def _cut_points(demo, full: pl.DataFrame, n: int, rng: random.Random) -> dict[str, int]:
    ticks = full["tick"].to_list()
    pts = {f"row {t}": t for t in rng.sample(ticks, min(n, len(ticks)))}
    pts |= {f"row {t} + 1": t + 1 for t in rng.sample(ticks, min(3, len(ticks)))}
    anchors = {
        "plant": demo.bomb.filter(pl.col("event") == "finish_plant")["tick"],
        "kill": demo.kills["tick"],
        "smoke pop": demo.events["smokegrenade_detonate"]["tick"] if "smokegrenade_detonate" in demo.events else None,
        "fire start": demo.events["inferno_startburn"]["tick"] if "inferno_startburn" in demo.events else None,
    }
    for label, series in anchors.items():
        if series is not None and series.len():
            t = int(rng.choice(series.to_list()))
            pts[f"at a {label}"], pts[f"1 tick before a {label}"] = t, t - 1
    return pts


def truncation_check(demo, cuts: int, rng: random.Random) -> int:
    cache: dict = {}
    full = _parse_until(demo, NO_CUT, cache)
    cols = [c for c in full.columns if c != "ct_win"]
    failures = 0
    for label, T in _cut_points(demo, full, cuts, rng).items():
        a = full.filter(pl.col("tick") <= T).select(cols)
        b = _parse_until(demo, T, cache).filter(pl.col("tick") <= T).select(cols)
        diff = [c for c in cols if not a[c].equals(b[c], check_names=False, null_equal=True)] if a.height == b.height else ["<row count>"]
        failures += bool(diff)
        print(f"  cut T={T:>7} ({label:<24}) rows<=T {a.height:>5}: {'columns differ: ' + str(diff[:8]) if diff else 'identical'}")
    return failures


def snapshot_check(demo) -> int:
    rounds = valid_rounds(demo)
    players = player_frame(demo, round_samples(rounds, pipeline.SAMPLE_INTERVAL)).join(
        rounds.select("part", "round_num", "start_tick"), on=["part", "round_num"]
    )
    this_round = pl.col("event_tick") >= pl.col("start_tick")
    deaths = with_part(demo.kills).select("part", pl.col("tick").alias("event_tick"), pl.col("victim_steamid").alias("steamid"))
    dead_early = (
        players.filter(~pl.col("is_alive"))
        .join(deaths, on=["part", "steamid"], how="left")
        .with_columns(pl.when(this_round & (pl.col("event_tick") <= pl.col("tick"))).then(1).otherwise(0).alias("died"))
        .group_by("part", "tick", "steamid").agg(pl.col("died").max())
        .filter(pl.col("died") == 0)
        .height
    )
    damage = with_part(demo.damages).select(
        "part", pl.col("tick").alias("event_tick"), pl.col("victim_steamid").alias("steamid"), "health_post"
    ).sort("event_tick")
    # deaths without a damage event (falls, world) also allow 0 health
    died = deaths.sort("event_tick").rename({"event_tick": "death_tick"})
    hurt_early = (
        players.sort("tick")
        .join_asof(damage, left_on="tick", right_on="event_tick", by=["part", "steamid"], strategy="backward", check_sortedness=False)
        .join_asof(died, left_on="tick", right_on="death_tick", by=["part", "steamid"], strategy="backward", check_sortedness=False)
        .with_columns(
            pl.when(pl.col("death_tick") >= pl.col("start_tick")).then(0)
            .when(this_round).then(pl.col("health_post"))
            .otherwise(100)
            .alias("allowed")
        )
        .filter(pl.col("health") < pl.col("allowed"))
        .height
    )
    print(f"  snapshots: {players.height} player-rows, dead before their death event: {dead_early}, "
          f"health below what damage so far allows: {hurt_early}")
    return int(dead_early > 0) + int(hurt_early > 0)


def main() -> None:
    ap = argparse.ArgumentParser(prog="demo_parser.leakage_check")
    ap.add_argument("input", type=Path, nargs="?", default=PACKAGE_DIR / "sample_data")
    ap.add_argument("--cuts", type=int, default=10, help="random cut points per match (plus event anchors)")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    failures = 0
    for key, paths in find_matches(args.input).items():
        demo = Demo([str(p) for p in paths]) if len(paths) > 1 else Demo(str(paths[0]))
        demo.load(*pipeline.DATASETS)
        print(f"\n{key.name}")
        failures += snapshot_check(demo)
        if len(paths) > 1:
            print("  truncation: skipped (split match)")
        else:
            failures += truncation_check(demo, args.cuts, rng)
    print(f"\n{'PASS: no row depends on later data' if not failures else f'FAIL: {failures} check(s) failed'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
