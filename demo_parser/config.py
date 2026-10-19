"""Parser constants. Tick values assume 64-tick demos."""

TICK_RATE = 64
ROUND_TICKS = 7360  # 115 s round timer
BOMB_TICKS = 2560  # 40 s bomb timer after plant
SAMPLE_INTERVAL = 64  # one snapshot every second
HISTORY_OFFSETS = (32, 64, 96, 128, 160, 192)  # past positions, 0.5 s to 3 s back

SIDES = {"counter-terrorist": "CT", "terrorist": "T"}
SLOTS = [f"{side}{i}" for side in ("CT", "T") for i in range(5)]

# Utility slots in the snapshot table. Each player can buy 1 smoke and 1 molotov/incendiary per round,
# and a survivor can also bring one of each over from the previous round, so a round has at most
# 20 of each. Active utility that finds no free slot is counted in smokes_overflow / fires_overflow.
SMOKE_SLOTS = 20
FIRE_SLOTS = 20

# smoke duration in ticks (22 s, fixed by the game); fires read theirs from the demo
SMOKE_LIFETIME = 1412
