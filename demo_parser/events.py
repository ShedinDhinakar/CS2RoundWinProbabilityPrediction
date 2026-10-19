"""Round, bomb, utility and scoreboard features built from awpy event tables."""

import polars as pl

from .config import BOMB_TICKS, ROUND_TICKS, SIDES, SMOKE_LIFETIME, TICK_RATE

PART_STRIDE = 1_000_000_000  # orders (part, tick) across split recordings


def with_part(df: pl.DataFrame) -> pl.DataFrame:
    """Single-file demos have no `part` column; give every table one."""
    if "part" in df.columns:
        return df.with_columns(pl.col("part").cast(pl.UInt32))
    return df.with_columns(pl.lit(0, pl.UInt32).alias("part"))


def order_key(tick: str = "tick") -> pl.Expr:
    return pl.col("part").cast(pl.Int64) * PART_STRIDE + pl.col(tick)


def valid_rounds(demo) -> pl.DataFrame:
    return (
        with_part(demo.rounds)
        .filter(
            ~pl.col("is_knife_round"),
            pl.col("freeze_end_tick").is_not_null(),
            pl.col("end_tick").is_not_null(),
            pl.col("winner_side").is_in(list(SIDES)),
        )
        .with_columns(pl.coalesce("official_end_tick", "end_tick").alias("official_end_tick"))
        .sort("part", "start_tick")
    )


def round_samples(rounds: pl.DataFrame, interval: int) -> pl.DataFrame:
    return (
        rounds.select(
            "part",
            "round_num",
            "start_tick",
            "freeze_end_tick",
            pl.int_ranges("freeze_end_tick", "end_tick", interval).alias("tick"),
            (pl.col("winner_side") == "counter-terrorist").cast(pl.Int8).alias("ct_win"),
        )
        .explode("tick")
        .drop_nulls("tick")
        .with_columns(pl.col("tick").cast(pl.Int32))
        .with_columns(
            (pl.col("freeze_end_tick") - pl.col("tick") + ROUND_TICKS).alias("round_time_left")
        )
        .sort("part", "tick")
    )


def assign_round(df: pl.DataFrame, rounds: pl.DataFrame, tick: str = "tick") -> pl.DataFrame:
    """Tag events with their round; drops events outside any valid round (warmup, knife, pauses)."""
    r = rounds.select(
        "part",
        "round_num",
        pl.col("start_tick").alias("_round_start"),
        pl.col("official_end_tick").alias("_round_end"),
    ).sort("_round_start")
    return (
        df.sort(tick)
        .join_asof(r, left_on=tick, right_on="_round_start", by="part", strategy="backward", check_sortedness=False)
        .filter(pl.col(tick) <= pl.col("_round_end"))
        .drop("_round_start", "_round_end")
    )


# ---------------------------------------------------------------- game state


def game_state(samples: pl.DataFrame, players: pl.DataFrame) -> pl.DataFrame:
    ct, t = pl.col("side") == "CT", pl.col("side") == "T"
    alive = pl.col("is_alive")
    agg = players.group_by("part", "tick").agg(
        (ct & alive).sum().cast(pl.Int8).alias("ct_alive"),
        (t & alive).sum().cast(pl.Int8).alias("t_alive"),
        pl.col("health").filter(ct).sum().alias("ct_hp"),
        pl.col("health").filter(t).sum().alias("t_hp"),
        (ct & pl.col("is_defusing")).any().cast(pl.Int8).alias("is_defusing"),
        (ct & pl.col("is_defusing") & pl.col("has_defuser")).any().cast(pl.Int8).alias("defuser_has_kit"),
    )
    return samples.join(agg, on=["part", "tick"], how="left")


def team_names(players: pl.DataFrame, roster: pl.DataFrame) -> pl.DataFrame:
    names = roster.select("steamid", "team_clan_name")
    return (
        players.join(names, on="steamid", how="left")
        .group_by("part", "tick")
        .agg(
            pl.col("team_clan_name").filter(pl.col("side") == "CT").mode().first().alias("ct_team"),
            pl.col("team_clan_name").filter(pl.col("side") == "T").mode().first().alias("t_team"),
        )
    )


# ---------------------------------------------------------------- bomb


def _latest_in_round(samples: pl.DataFrame, ev: pl.DataFrame, ev_tick: str) -> pl.DataFrame:
    """As-of join the latest event at or before each sample, ignoring events from earlier rounds."""
    cols = [c for c in ev.columns if c != "part"]
    joined = samples.join_asof(ev.sort(ev_tick), left_on="tick", right_on=ev_tick, by="part", strategy="backward", check_sortedness=False)
    in_round = pl.col(ev_tick) >= pl.col("start_tick")
    return joined.with_columns(pl.when(in_round).then(pl.col(c)).alias(c) for c in cols)


def bomb_state(bomb: pl.DataFrame, samples: pl.DataFrame, players: pl.DataFrame) -> pl.DataFrame:
    ev = with_part(bomb)
    plants = ev.filter(pl.col("event") == "finish_plant").select(
        "part",
        pl.col("tick").alias("plant_tick"),
        pl.col("bombsite").alias("bomb_site"),
        pl.col("x").alias("plant_X"),
        pl.col("y").alias("plant_Y"),
        pl.col("z").alias("plant_Z"),
    )
    carry = ev.filter(pl.col("event").is_in(["pickup", "drop"])).select(
        "part",
        pl.col("tick").alias("carry_tick"),
        pl.col("event").alias("carry_event"),
        pl.col("x").alias("drop_X"),
        pl.col("y").alias("drop_Y"),
        pl.col("z").alias("drop_Z"),
    )
    carrier = players.filter(pl.col("has_c4")).group_by("part", "tick").agg(
        pl.col("X").first().alias("carrier_X"),
        pl.col("Y").first().alias("carrier_Y"),
        pl.col("Z").first().alias("carrier_Z"),
    )

    s = samples.select("part", "tick", "start_tick")
    s = _latest_in_round(s, plants, "plant_tick")
    s = _latest_in_round(s, carry, "carry_tick")
    s = s.join(carrier, on=["part", "tick"], how="left")

    planted = pl.col("plant_tick").is_not_null()
    carried = pl.col("carrier_X").is_not_null()
    dropped = pl.col("carry_event") == "drop"
    status = (
        pl.when(planted).then(pl.lit("planted"))
        .when(carried).then(pl.lit("carried"))
        .when(dropped).then(pl.lit("dropped"))
        .otherwise(None)
    )

    def pos(axis: str) -> pl.Expr:
        return (
            pl.when(planted).then(pl.col(f"plant_{axis}"))
            .when(carried).then(pl.col(f"carrier_{axis}"))
            .when(dropped).then(pl.col(f"drop_{axis}"))
            .alias(f"bomb_{axis}")
        )

    time_left = (pl.col("plant_tick") + BOMB_TICKS - pl.col("tick")).clip(upper_bound=BOMB_TICKS)
    return s.select(
        "part",
        "tick",
        planted.cast(pl.Int8).alias("is_bomb_planted"),
        time_left.fill_null(BOMB_TICKS).alias("bomb_time_left"),
        status.alias("bomb_status"),
        "bomb_site",
        pos("X"),
        pos("Y"),
        pos("Z"),
    )


# ---------------------------------------------------------------- utility


EVENT_TYPES = {"tick": pl.Int32, "entityid": pl.Int32, "x": pl.Float32, "y": pl.Float32, "z": pl.Float32}
INFERNO_TYPES = {0: "molotov", 1: "incendiary"}  # CInferno.m_nInfernoType


def _event(demo, name: str, cols: list[str]) -> pl.DataFrame:
    """A game event table with typed columns (awpy returns event fields as strings)."""
    if name not in demo.events:
        return pl.DataFrame(schema={"part": pl.UInt32, **{c: EVENT_TYPES[c] for c in cols}})
    return with_part(demo.events[name]).select("part", *[pl.col(c).cast(EVENT_TYPES[c]) for c in cols])


def _lifetimes(demo, start: str, end: str, kind: str) -> pl.DataFrame:
    """Pair each start event with the next end event of the same entity.

    Entity ids are reused, so an end event after the id's next start belongs to that later grenade.
    """
    starts = (
        _event(demo, start, ["tick", "entityid", "x", "y", "z"])
        .rename({"tick": "start_tick"})
        .sort("part", "entityid", "start_tick")
        .with_columns(pl.col("start_tick").shift(-1).over("part", "entityid").alias("_next_start"))
    )
    ends = _event(demo, end, ["tick", "entityid"]).rename({"tick": "end_tick"})
    own_end = pl.col("_next_start").is_null() | (pl.col("end_tick") <= pl.col("_next_start"))
    return (
        starts.sort("start_tick")
        .join_asof(ends.sort("end_tick"), left_on="start_tick", right_on="end_tick",
                   by=["part", "entityid"], strategy="forward", check_sortedness=False)
        .with_columns(pl.when(own_end).then(pl.col("end_tick")).alias("end_tick"), pl.lit(kind).alias("kind"))
        .drop("_next_start")
    )


def _fire_entities(demo) -> pl.DataFrame:
    """Type and lifetime of every fire, read from its CInferno entity.

    Reading non-player entities makes awpy decode the whole demo serially, the slowest step.
    """
    frames = []
    for part, sub in enumerate(demo.parts):
        ent = sub.ticks(["m_nInfernoType", "m_nFireLifetime"], players_only=False)
        frames.append(
            ent.filter(pl.col("class_name") == "CInferno")
            .group_by("entity_id", "entity_serial")
            .agg(
                pl.col("tick").min().cast(pl.Int32).alias("created_tick"),
                pl.col("m_nInfernoType").drop_nulls().first().alias("inferno_type"),
                pl.col("m_nFireLifetime").drop_nulls().first().alias("lifetime_s"),
            )
            .select(pl.lit(part, pl.UInt32).alias("part"), pl.col("entity_id").cast(pl.Int32).alias("entityid"),
                    "created_tick", "inferno_type", "lifetime_s")
        )
    return pl.concat(frames)


def utility_table(demo, rounds: pl.DataFrame) -> pl.DataFrame:
    """One row per smoke cloud / fire with its type, lifetime and position, from game events."""
    smokes = _lifetimes(demo, "smokegrenade_detonate", "smokegrenade_expired", "smoke").with_columns(
        pl.lit("smoke").alias("grenade"), pl.lit(SMOKE_LIFETIME, pl.Int32).alias("lifetime_ticks")
    )
    fires = (
        _lifetimes(demo, "inferno_startburn", "inferno_expire", "fire")
        .sort("start_tick")
        .join_asof(_fire_entities(demo).sort("created_tick"), left_on="start_tick", right_on="created_tick",
                   by=["part", "entityid"], strategy="nearest", check_sortedness=False)
        .with_columns(
            pl.col("inferno_type").replace_strict(INFERNO_TYPES, default=None).alias("grenade"),
            (pl.col("lifetime_s") * TICK_RATE).round().cast(pl.Int32).alias("lifetime_ticks"),
        )
    )
    cols = ["part", "kind", "grenade", "start_tick", "end_tick", "lifetime_ticks", "x", "y", "z"]
    round_end = rounds.select("part", "round_num", "official_end_tick")
    return (
        assign_round(pl.concat([smokes.select(cols), fires.select(cols)]), rounds, tick="start_tick")
        .join(round_end, on=["part", "round_num"], how="left")
        # utility still active at round end is cleared by the round restart
        .with_columns(pl.min_horizontal(pl.col("end_tick").fill_null(pl.col("official_end_tick")), "official_end_tick").alias("end_tick"))
        .rename({"x": "X", "y": "Y", "z": "Z"})
        .select("part", "round_num", "kind", "grenade", "start_tick", "end_tick", "lifetime_ticks", "X", "Y", "Z")
        .sort("part", "start_tick")
    )


def utility_counts(util: pl.DataFrame, samples: pl.DataFrame) -> pl.DataFrame:
    active = (
        samples.select("part", "tick")
        .join(util.select("part", "kind", "slot", "start_tick", "end_tick"), on="part")
        .filter(pl.col("start_tick") <= pl.col("tick"), pl.col("tick") < pl.col("end_tick"))
    )
    exprs = []
    for kind in ("smoke", "fire"):
        is_kind = pl.col("kind") == kind
        exprs.append(is_kind.sum().alias(f"{kind}s_active"))
        exprs.append((is_kind & pl.col("slot").is_null()).sum().alias(f"{kind}s_overflow"))
    counts = active.group_by("part", "tick").agg(exprs)
    return (
        samples.select("part", "tick")
        .join(counts, on=["part", "tick"], how="left")
        .with_columns(pl.exclude("part", "tick").fill_null(0).cast(pl.Int16))
    )


# ---------------------------------------------------------------- scoreboard


STAT_COLS = {
    "kills": "kills_total",
    "deaths": "deaths_total",
    "assists": "assists_total",
    "headshot_kills": "headshot_kills_total",
    "damage": "damage_total",
    "ace_rounds": "ace_rounds_total",
    "4k_rounds": "4k_rounds_total",
    "3k_rounds": "3k_rounds_total",
}


def attach_stats(players: pl.DataFrame, demo, rounds: pl.DataFrame) -> pl.DataFrame:
    """Cumulative scoreboard stats per player as of each sample tick.

    Multi-kill rounds are credited at the end of the round they happened in.
    Team kills, team damage, flash assists and knife/warmup rounds are excluded.
    """
    kills = assign_round(with_part(demo.kills), rounds)
    enemy_kill = pl.col("attacker_steamid").is_not_null() & (pl.col("attacker_side") != pl.col("victim_side"))
    frag = kills.filter(enemy_kill)
    damages = assign_round(with_part(demo.damages), rounds).filter(
        pl.col("attacker_steamid").is_not_null(), pl.col("attacker_side") != pl.col("victim_side")
    )
    multi = (
        frag.group_by("part", "round_num", "attacker_steamid").len()
        .join(rounds.select("part", "round_num", pl.col("official_end_tick").alias("tick")), on=["part", "round_num"])
        .filter(pl.col("len") >= 3)
        .select(
            "part", "tick", pl.col("attacker_steamid").alias("steamid"),
            (pl.col("len") >= 5).cast(pl.Int32).alias("ace_rounds"),
            (pl.col("len") == 4).cast(pl.Int32).alias("4k_rounds"),
            (pl.col("len") == 3).cast(pl.Int32).alias("3k_rounds"),
        )
    )
    one = pl.lit(1, pl.Int32)
    increments = pl.concat(
        [
            frag.select("part", "tick", pl.col("attacker_steamid").alias("steamid"), one.alias("kills"),
                        pl.col("headshot").cast(pl.Int32).alias("headshot_kills")),
            kills.select("part", "tick", pl.col("victim_steamid").alias("steamid"), one.alias("deaths")),
            kills.filter(pl.col("assister_steamid").is_not_null(), pl.col("assister_side") != pl.col("victim_side"),
                         ~pl.col("assist_flash"))
                 .select("part", "tick", pl.col("assister_steamid").alias("steamid"), one.alias("assists")),
            damages.select("part", "tick", pl.col("attacker_steamid").alias("steamid"),
                           pl.min_horizontal("dmg_health", "health_pre").cast(pl.Int32).alias("damage")),
            multi,
        ],
        how="diagonal",
    ).with_columns(order_key().alias("order"))

    cum = (
        increments.group_by("steamid", "order").agg(pl.col(list(STAT_COLS)).sum())
        .sort("order")
        .with_columns(pl.col(list(STAT_COLS)).cum_sum().over("steamid"))
        .rename(STAT_COLS)
    )
    return (
        players.with_columns(order_key().alias("order"))
        .sort("order")
        .join_asof(cum, on="order", by="steamid", strategy="backward", check_sortedness=False)
        .with_columns(pl.col(list(STAT_COLS.values())).fill_null(0))
        .drop("order")
    )
