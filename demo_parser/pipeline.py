"""Parse one match (one or more demo files) into a model-ready snapshot table and a utility table."""

import time
from pathlib import Path

import polars as pl
from awpy import Demo

from .config import FIRE_SLOTS, HISTORY_OFFSETS, SAMPLE_INTERVAL, SLOTS, SMOKE_SLOTS, TICK_RATE
from .events import (
    STAT_COLS,
    attach_stats,
    bomb_state,
    game_state,
    round_samples,
    team_names,
    utility_counts,
    utility_table,
    valid_rounds,
)
from .players import PLAYER_PROPS, POSITION, player_frame, slot_columns
from .utility_slots import assign_slots, slot_frame

DATASETS = ("rounds", "players", "kills", "damages", "bomb")

POSITION_COLS = [*POSITION, *[f"{c}_before{off}" for off in HISTORY_OFFSETS for c in POSITION]]
STATE_COLS = ["is_alive", *[c for c in PLAYER_PROPS.values() if c not in POSITION]]
FEATURE_COLS = [*POSITION_COLS, *STATE_COLS, *STAT_COLS.values()]


def to_wide(players: pl.DataFrame, samples: pl.DataFrame) -> pl.DataFrame:
    """Pivot (tick, player) rows into one row per tick with `<feature>_<slot>` columns."""
    wide = samples.select("part", "tick")
    for slot in SLOTS:
        cols = slot_columns(slot, FEATURE_COLS)
        sub = players.filter(pl.col("slot") == slot).select("part", "tick", *[pl.col(c).alias(f"{c}_{slot}") for c in cols])
        wide = wide.join(sub, on=["part", "tick"], how="left")
    # group by feature: all slots of X, then all slots of Y, ...
    ordered = [f"{c}_{s}" for c in FEATURE_COLS for s in SLOTS if f"{c}_{s}" in wide.columns]
    return wide.select("part", "tick", *ordered)


def parse_match(
    paths: list[Path],
    match_id: str,
    interval: int = SAMPLE_INTERVAL,
    smoke_slots: int = SMOKE_SLOTS,
    fire_slots: int = FIRE_SLOTS,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    demo = Demo([str(p) for p in paths]) if len(paths) > 1 else Demo(str(paths[0]))
    for part in demo.parts:
        if part.tick_rate != TICK_RATE:
            raise ValueError(f"{part.paths[0].name} is a {part.tick_rate:g} tick demo; the parser expects {TICK_RATE}")
    demo.load(*DATASETS)

    rounds = valid_rounds(demo)
    samples = round_samples(rounds, interval)
    players = attach_stats(player_frame(demo, samples), demo, rounds)
    n_slots = {"smoke": smoke_slots, "fire": fire_slots}
    util = assign_slots(utility_table(demo, rounds), n_slots)

    keys = ["part", "tick"]
    snapshots = (
        game_state(samples, players)
        .join(bomb_state(demo.bomb, samples, players), on=keys, how="left")
        .join(utility_counts(util, samples), on=keys, how="left")
        .join(slot_frame(util, samples, n_slots), on=keys, how="left")
        .join(team_names(players, demo.players), on=keys, how="left")
        .join(to_wide(players, samples), on=keys, how="left")
        .drop("start_tick", "freeze_end_tick")
        .with_columns(pl.lit(match_id).alias("match_id"), pl.lit(demo.header["map_name"]).alias("map_name"))
    )
    meta = ["match_id", "map_name", "part", "round_num", "tick", "ct_team", "t_team", "ct_win"]
    snapshots = snapshots.select(*meta, pl.exclude(meta)).sort("part", "tick")
    util = util.with_columns(pl.lit(match_id).alias("match_id")).select(
        "match_id", "part", "round_num", "kind", "grenade", "slot", pl.exclude("match_id", "part", "round_num", "kind", "grenade", "slot")
    )
    return _compact(snapshots), _compact(util)


def _compact(df: pl.DataFrame) -> pl.DataFrame:
    """Booleans as 0/1 and 32-bit numbers keep the CSVs small."""
    return df.with_columns(
        pl.col(pl.Boolean).cast(pl.Int8),
        pl.col(pl.Float64).cast(pl.Float32),
        pl.col(pl.Int64).cast(pl.Int32),
    )


def run_match(paths: list[Path], out_dir: Path, match_id: str, interval: int = SAMPLE_INTERVAL,
              smoke_slots: int = SMOKE_SLOTS, fire_slots: int = FIRE_SLOTS) -> dict:
    start = time.perf_counter()
    snapshots, util = parse_match(paths, match_id, interval, smoke_slots, fire_slots)
    (out_dir / "utility").mkdir(parents=True, exist_ok=True)
    snapshots.write_csv(out_dir / f"{match_id}_snapshots.csv", float_precision=3)
    util.write_csv(out_dir / "utility" / f"{match_id}_utility.csv", float_precision=3)
    return {
        "match": match_id,
        "rows": snapshots.height,
        "cols": snapshots.width,
        "utility_rows": util.height,
        "overflow": int(snapshots.select(pl.col("smokes_overflow").max(), pl.col("fires_overflow").max()).max_horizontal().item()),
        "seconds": round(time.perf_counter() - start, 2),
    }
