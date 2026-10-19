# CS2 Round Win Probability Prediction

Predicting, at any moment of a Counter-Strike 2 round, the probability that the CT side wins that
round. The prediction is **per round**, not per match, so economy and score are deliberately
left out. Only what is active in the round (players, equipment, bomb, utility, time) is used.

| Stage | Status |
|---|---|
| Demo parser (`demo_parser/`) | Done, validated on 3 maps of a 2026 Major match |
| Win probability model | Not started |

## Repository layout

```
environment.yml            conda environment (cs2parse)
demo_parser/               demo -> CSV parser (Python package)
  __main__.py              batch CLI, parallel over matches
  pipeline.py              parses one match, assembles and writes the CSVs
  players.py               per-player state from awpy snapshots
  events.py                rounds, bomb, utility events, scoreboard stats
  utility_slots.py         first-free-slot layout of smokes / fires
  leakage_check.py         verifies no row uses data from after its tick
  discover.py              finds demos, groups split recordings (-p1, -p2)
  config.py                tick constants, sampling, slot counts
  sample_data/             input demos (.dem files are git-ignored, see its README)
  parsed_data/             parser output for the sample match
```

## Setup

```bash
conda env create -f environment.yml
conda activate cs2parse
```

Python 3.12 with **awpy 3.1** (Rust-backed demo parser, used for everything), polars 2.0,
numpy, pandas, pyarrow, tqdm and ipykernel. **demoparser2 0.42** is installed but not used yet.

## Running the parser

```bash
python -m demo_parser                                    # demo_parser/sample_data -> demo_parser/parsed_data
python -m demo_parser /path/to/demos --out /path/to/out --workers 4
```

| Option | Default | Meaning |
|---|---|---|
| `input` | `demo_parser/sample_data` | folder searched recursively for `.dem` files |
| `--out` | `demo_parser/parsed_data` | output folder; mirrors the input folder layout |
| `--workers` | CPU count / 4 | matches parsed in parallel |
| `--interval` | 64 | ticks between snapshots (64 = 1 s) |
| `--smoke-slots`, `--fire-slots` | 20, 20 | utility columns per snapshot |

Only 64-tick demos are supported; any other tick rate stops with an error. Demos named
`<name>-p1.dem`, `<name>-p2.dem`, ... are parsed as one match. A match that fails is reported as
`[SKIPPED]` and the rest continue.

**Speed:** 22-39 s per match on the sample demos (~3 parallel matches in ~40 s). Most of the time
is one full decode of all entities to read each fire's type and lifetime.

## Output

Per match, two CSVs:

```
parsed_data/<event folder>/<match>_snapshots.csv          model input, one row per second
parsed_data/<event folder>/utility/<match>_utility.csv    one row per smoke / fire
```

### `<match>_snapshots.csv` (661 columns)

One row every 64 ticks from the end of freeze time until the round is decided. Rounds that are
knife rounds, or have no freeze end, end tick or CT/T winner, are skipped. Booleans are 0/1.

**Match and label**

| Column | Source | Logic |
|---|---|---|
| `match_id` | file name | demo name without the `-pN` suffix |
| `map_name` | awpy header | |
| `part` | awpy | recording index (0 unless the match is split); ticks restart per part |
| `round_num`, `tick` | awpy rounds | |
| `ct_team`, `t_team` | awpy players | team name of the players on each side at that tick |
| `ct_win` | awpy rounds | **label**: 1 if the round's winner is CT |

**Round state**

| Column | Logic |
|---|---|
| `round_time_left` | `freeze_end_tick + 7360 - tick` (115 s round). Keeps counting after a plant, so it can go negative |
| `ct_alive`, `t_alive` | players with health > 0 |
| `ct_hp`, `t_hp` | summed health per side |
| `is_defusing` | a CT is defusing at this tick |
| `defuser_has_kit` | the defusing CT has a kit (5 s instead of 10 s) |

**Bomb** (from awpy bomb events: plant, pickup, drop; only events of the current round count)

| Column | Logic |
|---|---|
| `is_bomb_planted` | a plant happened this round at or before the tick |
| `bomb_time_left` | `plant_tick + 2560 - tick` (40 s), 2560 when not planted |
| `bomb_status` | `planted`, else `carried` (a T holds it), else `dropped` |
| `bomb_site` | A / B from the plant event |
| `bomb_X/Y/Z` | plant position, carrier position, or where it was dropped |

**Utility**

| Column | Logic |
|---|---|
| `smokes_active`, `fires_active` | smokes / fires burning at this tick |
| `smokes_overflow`, `fires_overflow` | active ones that found no free slot (0 on all data so far) |
| `smoke{0-19}_X/Y/Z`, `smoke{i}_time_left` | slot columns, see below |
| `fire{0-19}_X/Y/Z`, `fire{i}_time_left`, `fire{i}_type` | `type` is `molotov` or `incendiary` |

Each smoke or fire takes the lowest free slot when it starts and keeps it until it ends, so a slot
describes the same grenade for its whole life. Empty slots are blank. `time_left` is
`start + full lifetime - tick`: smokes last 1412 ticks (22 s, fixed by the game); a fire's type
and lifetime (molotov 7.0 s, incendiary 5.5 s) are read from its `CInferno` entity. A fire put
out early by a smoke leaves its slot at that tick, but `time_left` never anticipates it (that
would leak the future). 20 slots each is the per-round maximum: 10 players can buy one of each
per round, plus one each carried over by survivors.

**Players**: ten slots `CT0-CT4` and `T0-T4`. The lineup is taken from each round's first
snapshot and sorted by steamid within each side, so a slot is the same player for the whole round
(but a different person after the halftime side swap). Shown for `CT0`:

| Column | Logic |
|---|---|
| `X_CT0`, `Y_CT0`, `Z_CT0` | world position |
| `X_before{32,64,96,128,160,192}_CT0` (and Y, Z) | the same player's position 0.5-3 s earlier |
| `yaw_CT0`, `pitch_CT0` | view angles |
| `is_alive_CT0`, `health_CT0`, `armor_value_CT0`, `has_helmet_CT0` | |
| `current_equip_value_CT0` | value of the equipment carried right now (not money) |
| `active_weapon_CT0`, `primary_weapon_CT0`, `secondary_weapon_CT0` | e.g. `ak47`, `awp`, `knife` |
| `flashbangs_CT0`, `smoke_grenades_CT0`, `he_grenades_CT0`, `fire_grenades_CT0` | grenades held |
| `is_blinded_CT0` | currently flashed |
| `has_defuser_CT0`, `is_defusing_CT0` | CT slots only |
| `has_c4_T0` | T slots only |
| `kills_total_CT0`, `deaths_total_CT0`, `assists_total_CT0`, `headshot_kills_total_CT0`, `damage_total_CT0`, `3k/4k/ace_rounds_total_CT0` | match scoreboard so far |

Dead (or disconnected) players: yes/no and number fields are 0, position, angles and weapons are
blank. Scoreboard rules: team kills and suicides don't count as kills, every death counts,
flash assists don't count, damage per hit is capped at the victim's remaining health, and
multi-kill rounds count once the round has ended.

### `utility/<match>_utility.csv`

The lossless source of the utility slots: `match_id, part, round_num, kind (smoke/fire),
grenade (smoke/molotov/incendiary), slot, start_tick, end_tick, lifetime_ticks, X, Y, Z`.
Start and end come from the `smokegrenade_detonate/expired` and `inferno_startburn/expire` game
events; `end_tick` includes fires put out by smokes and is cut at the round's end.

## No future data in any row

A row at tick T may only use what has happened up to T. `python -m demo_parser.leakage_check`
verifies this (exit code 1 on failure) and passes on the sample match:

- **Truncation test:** each match is re-parsed as if the demo ended at T: every event, game
  event and fire entity after T is removed. All rows at or before T must be identical to the
  full parse in every column except the `ct_win` label. Cut points are random rows plus the
  exact ticks of (and 1 tick before) plants, kills, smoke pops and fire starts: 42 cuts on
  Overpass and Dust2, 48,559 rows compared, every one identical (split matches are skipped).
- **Snapshot test:** no player is dead before their death event, or below the health their
  damage or death events so far allow (99,080 player-rows over all 3 maps, 0 violations).

Values that do come from later ticks are used only where they cannot leak: a round's end tick
decides which rows exist and fills the label; a smoke/fire's end tick only answers "already
gone at this tick?", which a viewer can see; multi-kill rounds are credited after their round.

The test found and fixed two problems:
- Two grenades starting on the same tick were ordered arbitrarily into slots, so a later event
  could swap them. Ties are now broken by position, known when the grenade starts.
- Demos skip recording some ticks (572 of 218,905 on Overpass, server hitches). awpy silently
  returns nothing for those ticks, which showed as all ten players dead. A tick with no frame now
  uses the latest earlier frame; a gap longer than 1 s stops the parse with an error.

## How the parser was checked (sample match, 3 maps, 99 rounds)

- Labels match awpy's round winners in every round; every round opens 5v5.
- Cumulative kills at the last snapshot match awpy's scoreboard (e.g. 136/136 on Overpass).
- Every snapshot of every smoke/fire is in its own slot with the right position and time left
  (20,294 checked, 0 errors); filled slots + overflow = active count in every row.
- All 646 fires got a type; every fire that burned out matches its type's length
  (incendiary 352 ticks, molotov 449-450). Shorter burns are fires put out by smokes, which end
  17-18 ticks after a smoke pops nearby.
- Bomb status always has a position; planted bombs always have a site.

## Development notes

- Started from an earlier notebook (Ancient-only demos, demoparser2 + awpy 2, 5 s snapshots,
  ~100 s per demo). This rewrite uses awpy 3.1, which parses a whole match in seconds.
- awpy 3.1 snapshots lose the steamid of many dead players; the per-round lineup works around it.
- Smoke and fire tables are built from game events instead of awpy's `smokes`/`fires`/`grenades`
  datasets, which each cost an extra full decode.
- Thrower identity and side were tried and removed: not needed for the model, and fires carry
  no thrower in the demo.
- Burn-length guesses for molotov vs incendiary were replaced by the entity's own fields.

## Next steps

- Parse a larger set of demos (all maps; one model per map is planned).
- Build the win probability model. When splitting train/test, split by match (or at least by
  round): snapshots of the same round are strongly correlated.
