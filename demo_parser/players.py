"""Per-player state at each sample tick, taken from awpy snapshots."""

import polars as pl

from .config import HISTORY_OFFSETS, SIDES

# awpy snapshot column -> output column
PLAYER_PROPS = {
    "x": "X",
    "y": "Y",
    "z": "Z",
    "yaw": "yaw",
    "pitch": "pitch",
    "health": "health",
    "armor": "armor_value",
    "has_helmet": "has_helmet",
    "equipment_value": "current_equip_value",
    "active_weapon": "active_weapon",
    "primary_weapon": "primary_weapon",
    "secondary_weapon": "secondary_weapon",
    "flashbangs": "flashbangs",
    "smoke_grenades": "smoke_grenades",
    "he_grenades": "he_grenades",
    "fire_grenades": "fire_grenades",
    "is_blinded": "is_blinded",
    "has_defuser": "has_defuser",
    "is_defusing": "is_defusing",
    "has_bomb": "has_c4",
}
CT_ONLY = ("has_defuser", "is_defusing")
T_ONLY = ("has_c4",)
POSITION = ("X", "Y", "Z")


BOOL_STATE = ("has_helmet", "is_blinded", "has_defuser", "is_defusing", "has_c4")
NUM_STATE = ("health", "armor_value", "current_equip_value", "flashbangs", "smoke_grenades", "he_grenades", "fire_grenades")
NULL_WHEN_DEAD = (*POSITION, "yaw", "pitch", "active_weapon", "primary_weapon", "secondary_weapon")


MAX_FRAME_GAP = 64  # ticks to look back for a recorded frame before giving up


def _snapshots_at(recording, ticks: list[int]) -> pl.DataFrame:
    """Player state at each tick, labelled with the requested tick.

    Demos do not record a frame on every tick (server hitches), and awpy silently returns no rows
    for those ticks. They take the latest earlier frame instead, which is still only past data.
    """
    snap = recording.snapshots(ticks=ticks)
    missing = sorted(set(ticks) - set(snap["tick"].unique().to_list()))
    filled = []
    for back in range(1, MAX_FRAME_GAP + 1):
        if not missing:
            break
        wanted = {t - back: t for t in missing}
        got = recording.snapshots(ticks=sorted(wanted)).filter(pl.col("tick").is_in(list(wanted)))
        if got.height:
            filled.append(got.with_columns(pl.col("tick").replace_strict(wanted, return_dtype=pl.Int32)))
            found = {wanted[t] for t in got["tick"].unique().to_list()}
            missing = [t for t in missing if t not in found]
    if missing:
        raise ValueError(f"no recorded frame within {MAX_FRAME_GAP} ticks before ticks {missing[:5]}")
    return pl.concat([snap, *filled]) if filled else snap


def player_frame(demo, samples: pl.DataFrame) -> pl.DataFrame:
    """One row per (sample tick, player) with slot label, current state and past positions.

    awpy snapshots lose the steamid of some dead players, so each round's lineup is taken
    from its first sample (everyone alive) and players missing later in the round are dead.
    """
    frames = []
    for part in samples["part"].unique().sort().to_list():
        base_ticks = samples.filter(pl.col("part") == part)["tick"]
        ticks = sorted({t - off for t in base_ticks.to_list() for off in (0, *HISTORY_OFFSETS)})
        snap = (
            _snapshots_at(demo.parts[part], ticks)
            .filter(pl.col("side").is_in(list(SIDES)), pl.col("steamid").is_not_null())
            .select("tick", "steamid", "side", *PLAYER_PROPS)
            .rename(PLAYER_PROPS)
            .with_columns(pl.lit(part, pl.UInt32).alias("part"))
        )
        cur = snap.filter(pl.col("tick").is_in(base_ticks.implode()))
        for off in HISTORY_OFFSETS:
            past = snap.select(
                "part",
                (pl.col("tick") + off).alias("tick"),
                "steamid",
                *[pl.col(c).alias(f"{c}_before{off}") for c in POSITION],
            )
            cur = cur.join(past, on=["part", "tick", "steamid"], how="left")
        frames.append(cur)
    cur = pl.concat(frames).with_columns(pl.col("side").replace_strict(SIDES))

    sample_rounds = samples.select("part", "round_num", "tick")
    first_tick = sample_rounds.group_by("part", "round_num").agg(pl.col("tick").min())
    lineup = (
        cur.join(first_tick, on=["part", "tick"])
        .select("part", "round_num", "steamid", "side")
        # stable slot order: steamid within side, so CT0..CT4 / T0..T4
        .with_columns(pl.col("steamid").rank("ordinal").over("part", "round_num", "side").sub(1).alias("slot"))
        .filter(pl.col("slot") < 5)
        .with_columns(pl.concat_str("side", pl.col("slot").cast(pl.Utf8)).alias("slot"))
    )
    alive = pl.col("is_alive")
    return (
        sample_rounds.join(lineup, on=["part", "round_num"])
        .join(cur.drop("side"), on=["part", "tick", "steamid"], how="left")
        .with_columns((pl.col("health").fill_null(0) > 0).alias("is_alive"))
        .with_columns(
            *[(pl.col(c) & alive).fill_null(False).alias(c) for c in BOOL_STATE],
            *[pl.when(alive).then(pl.col(c)).otherwise(0).alias(c) for c in NUM_STATE],
            *[pl.when(alive).then(pl.col(c)).alias(c) for c in NULL_WHEN_DEAD],
        )
    )


def slot_columns(slot: str, feature_cols: list[str]) -> list[str]:
    drop = T_ONLY if slot.startswith("CT") else CT_ONLY
    return [c for c in feature_cols if c not in drop]
