"""Spread active smokes and fires over fixed snapshot columns.

Each grenade takes the lowest free slot of its kind when it starts and keeps it until it ends,
so `smoke3_*` describes the same smoke for its whole life.
"""

import polars as pl

SLOT_FIELDS = {
    "smoke": ("X", "Y", "Z", "time_left"),
    "fire": ("X", "Y", "Z", "time_left", "type"),
}


def assign_slots(util: pl.DataFrame, n_slots: dict[str, int]) -> pl.DataFrame:
    """Add `slot`: first free slot per (part, round, kind), null when every slot is taken."""
    ordered = util.with_row_index("_row").sort("part", "round_num", "kind", "start_tick")
    slots, group, busy_until = [], None, []
    for part, round_num, kind, start, end in ordered.select("part", "round_num", "kind", "start_tick", "end_tick").iter_rows():
        if (part, round_num, kind) != group:
            group, busy_until = (part, round_num, kind), [None] * n_slots[kind]
        free = next((i for i, until in enumerate(busy_until) if until is None or until <= start), None)
        if free is not None:
            busy_until[free] = end
        slots.append(free)
    return ordered.with_columns(pl.Series("slot", slots, dtype=pl.Int16)).sort("_row").drop("_row")


def slot_frame(util: pl.DataFrame, samples: pl.DataFrame, n_slots: dict[str, int]) -> pl.DataFrame:
    """One row per sample tick with `<kind><i>_<field>` columns for every slot."""
    natural_end = pl.col("start_tick") + pl.col("lifetime_ticks")
    active = (
        samples.select("part", "tick")
        .join(util.filter(pl.col("slot").is_not_null()), on="part")
        .filter(pl.col("start_tick") <= pl.col("tick"), pl.col("tick") < pl.col("end_tick"))
        .select(
            "part", "tick", "kind", "slot", "X", "Y", "Z",
            # from the full lifetime only: the real end_tick may reveal a future extinguish
            (natural_end - pl.col("tick")).clip(lower_bound=0).alias("time_left"),
            pl.col("grenade").alias("type"),
        )
    )
    wide = samples.select("part", "tick")
    for kind, n in n_slots.items():
        fields = SLOT_FIELDS[kind]
        for i in range(n):
            sub = active.filter(pl.col("kind") == kind, pl.col("slot") == i).select(
                "part", "tick", *[pl.col(f).alias(f"{kind}{i}_{f}") for f in fields]
            )
            wide = wide.join(sub, on=["part", "tick"], how="left")
    return wide
