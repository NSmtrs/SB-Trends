# MLB Baserunner Analytics

An end-to-end analysis of how often MLB basestealers actually come around to score — built across Python, R, and Power BI using the MLB Stats API.

## The Question

When a runner steals a base, how often does that steal actually translate into a run scored? Stolen bases sit in a strange place statistically: they're flashy, they show up on highlight reels, and they get credited as a positive event — but a stolen base only matters if the runner eventually scores. This project pulls play-by-play data for every regular-season game from 2009 to 2025 (excluding 2020), tracks each baserunner's "trip" from reaching base to either scoring or being put out, and aggregates the results at both the player-season and team-season level so the question can be answered head-on.

## Data Source

All raw data is pulled directly from the [MLB Stats API](https://statsapi.mlb.com/docs/), via:

- **Python**: the [`MLB-StatsAPI`](https://pypi.org/project/MLB-StatsAPI/) package for play-by-play extraction
- **R**: direct API calls via `httr` / `jsonlite`, plus the [`baseballr`](https://billpetti.github.io/baseballr/) package for season-level lookups

No scraped data, no third-party CSVs.

## Repository Structure

```
mlb-baserunner-analytics/
├── python/
│   └── player_sb_scoring_stats.py     # Multi-season play-by-play extraction
├── r/
│   ├── combine_seasons.R              # Stitch per-season CSVs into one file
│   ├── add_obp_and_pa.R               # Add OBP + PA per player-season
│   ├── bulk_player_season_hitting.R   # Bulk-pull season SB / Runs / OBP / PA
│   └── bulk_team_season_runs_sb.R     # Team-level Runs and SB per season
└── powerbi/
    └── screenshots/                   # Dashboard images
```

## Python Pipeline

`player_sb_scoring_stats.py` is the heavy-lifting script. For each season's games it walks the play-by-play, identifies each baserunner's "trip" (reach base → optional steals → score or out), and emits one row per (player, season) with:

- `sb_runner_scored` — stolen bases where the runner went on to score that inning
- `sb_runner_did_not_score` — stolen bases where the runner was stranded or put out
- `total_stolen_bases`, `season_total_runs`, plus team and year metadata

The script handles 16 seasons in a single run, configured via a `SEASONS` dictionary at the top. Schedule pulls are chunked into ~30-day windows and every API call has a retry-with-exponential-backoff wrapper so transient 503s and first-byte timeouts from the MLB API don't kill long-running runs.

## R Scripts

The R side picks up where the Python pipeline ends — combining outputs, enriching the dataset with season-level stats, and shaping it for the Power BI model.

`combine_seasons.R` concatenates the per-season Python outputs into one master CSV (`player_sb_scoring_stats_2009_2025.csv`).

`add_obp_and_pa.R` adds OBP and Plate Appearances per (player, season) and filters out any rows whose team isn't one of the 30 active MLB franchises.

`bulk_player_season_hitting.R` is the more efficient version: a single paginated call per season fetches SB, Runs, OBP, and PA for every batter at once. Handles the API's silent qualifier filter via `playerPool=All` and aggregates traded-player stats across team stints.

`bulk_team_season_runs_sb.R` is the team-level companion: 30 teams × 16 seasons = 480 rows of season totals, one API call per season.

Every R script uses the same retry-with-backoff pattern as the Python script and is designed to be safe to re-run.

## Power BI Dashboard

The cleaned, joined dataset feeds an interactive Power BI dashboard with player and team slicers, a 2009–2025 year range selector, season-level SB totals, and league-wide comparisons of SB Run Rate (the share of stolen bases that lead to a run that inning) versus the overall Run Scoring Rate.

A few of the more interesting DAX measures the dashboard relies on:

- `Run Scoring Rate = SUM(R) / SUMX(table, PA × OBP)` — runs per time-on-base
- `League Run Scoring Rate` — uses `ALLEXCEPT([Season])` so league averages stay year-specific
- `SB Rank in Season` — `RANKX` over the unfiltered season-leaderboard, with an alphabetical tiebreak so tied SB totals don't share a rank

## Running It Yourself

```bash
# Python
pip install MLB-StatsAPI pandas tqdm
python python/player_sb_scoring_stats.py
```

```r
# R — auto-installs missing packages on first run
source("r/combine_seasons.R")
source("r/add_obp_and_pa.R")
source("r/bulk_player_season_hitting.R")
source("r/bulk_team_season_runs_sb.R")
```

Edit the date ranges and output paths at the top of each script to match your environment.

## Notes

- 2020 is intentionally skipped (shortened COVID season).
- "Run scored after SB" is scoped to the same basepath trip — a stolen base earlier in the inning by a different runner doesn't count toward this player's metric.
- Traded players' season stats are aggregated across team stints; their reported team is whichever they had the most plate appearances with.
- Current issue in player dashboard where player stats divided between teams played with
- Every API-touching script uses retry-with-exponential-backoff, so transient 503/timeout errors are handled gracefully and the runs are safe to re-execute.
