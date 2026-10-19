# Sample demos

The `.dem` files are not in git (each is 300-800 MB, over GitHub's 100 MB limit).

The parser was developed and validated on the IEM Cologne Major 2026 quarter-final, G2 vs Spirit:

```
20260619_iem_cologne_major_2026_quarter_final_g2_vs_spirit/
  g2-vs-spirit-m1-overpass.dem
  g2-vs-spirit-m2-dust2.dem
  g2-vs-spirit-m3-mirage-p1.dem   # map recorded in two parts,
  g2-vs-spirit-m3-mirage-p2.dem   # parsed as one match
```

Put demos in any folder layout under here; the parser searches recursively and mirrors the
layout under `parsed_data/`. Files named `<name>-p1.dem`, `<name>-p2.dem`, ... are treated as
consecutive parts of one match.
