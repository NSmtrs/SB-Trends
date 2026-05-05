import statsapi
import pandas as pd
import time
import random
from datetime import datetime, timedelta
from tqdm import tqdm

# ==========================================================
# USER SETTINGS – EDIT THESE
# ==========================================================
# Define each season you want to pull here.
# Just add/remove entries or tweak the opening-day / final-game dates.
# Format: "YYYY": ("MM/DD/YYYY" opening day, "MM/DD/YYYY" final regular-season game)
# NOTE: 2020 is intentionally skipped (shortened COVID season).
SEASONS = {
    2019: ("03/20/2019", "09/29/2019"),
    # 2020 skipped
    2021: ("04/01/2021", "10/03/2021"),
    2022: ("04/07/2022", "10/05/2022"),
    2023: ("03/30/2023", "10/01/2023"),
    2024: ("03/28/2024", "09/30/2024"),
    2025: ("03/27/2025", "09/28/2025"),
}

# Optional: limit to one team (MLB team ID), or set to None for all teams
# Example: 139 = Tampa Bay Rays, 110 = Orioles, etc.
TEAM_ID = None   # e.g. 139 or None

# Output files
OUTPUT_PLAYER_CSV_PER_SEASON = "player_sb_scoring_stats_{year}.csv"
OUTPUT_PLAYER_CSV_COMBINED   = "player_sb_scoring_stats_2009_2025.csv"

# Retry / chunking behavior
MAX_RETRIES       = 6      # total attempts per API call
BASE_BACKOFF_SEC  = 2.0    # first retry waits ~2s, then 4, 8, 16, 32, 64 (+jitter)
MAX_BACKOFF_SEC   = 60.0
SCHEDULE_CHUNK_DAYS = 30   # break long schedule pulls into ~monthly windows


# ==========================================================
# RETRY WRAPPER (handles 503/timeouts/transient errors)
# ==========================================================
def _looks_transient(exc):
    """Return True if an exception looks like a transient network/server issue."""
    msg = str(exc).lower()
    transient_markers = (
        "503", "502", "504", "500",
        "timeout", "timed out",
        "first byte",
        "connection", "reset",
        "temporarily", "unavailable",
    )
    return any(m in msg for m in transient_markers)


def call_with_retry(fn, *args, label="api call", **kwargs):
    """
    Call `fn(*args, **kwargs)` with exponential backoff on transient errors.
    Raises the last exception if all retries fail.
    """
    attempt = 0
    while True:
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            attempt += 1
            if attempt >= MAX_RETRIES or not _looks_transient(e):
                raise
            sleep_for = min(
                BASE_BACKOFF_SEC * (2 ** (attempt - 1)),
                MAX_BACKOFF_SEC,
            )
            # add jitter so concurrent callers don't sync up
            sleep_for += random.uniform(0, 1.0)
            print(
                f"  [retry {attempt}/{MAX_RETRIES - 1}] {label} failed "
                f"({type(e).__name__}): {e}. Sleeping {sleep_for:.1f}s..."
            )
            time.sleep(sleep_for)


# ==========================================================
# SCHEDULE + METADATA
# ==========================================================
def _parse_mdY(s):
    return datetime.strptime(s, "%m/%d/%Y")


def _fmt_mdY(d):
    return d.strftime("%m/%d/%Y")


def _iter_date_chunks(start_date, end_date, chunk_days):
    """Yield (chunk_start, chunk_end) pairs covering [start_date, end_date]."""
    start = _parse_mdY(start_date)
    end = _parse_mdY(end_date)
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=chunk_days - 1), end)
        yield _fmt_mdY(cur), _fmt_mdY(chunk_end)
        cur = chunk_end + timedelta(days=1)


def _fetch_schedule_chunk(start_date, end_date, team_id=None):
    """Call statsapi.schedule for one small date window, with retries."""
    kwargs = dict(start_date=start_date, end_date=end_date, sportId=1)
    if team_id is not None:
        kwargs["team"] = team_id
    return call_with_retry(
        statsapi.schedule,
        label=f"schedule {start_date}→{end_date}",
        **kwargs,
    )


def get_schedule_with_metadata(start_date, end_date, team_id=None):
    """
    Get game IDs and metadata (date, home/away teams, ids) for date range.
    Breaks the window into monthly chunks to avoid 503 first-byte timeouts.
    """
    sched = []
    chunks = list(_iter_date_chunks(start_date, end_date, SCHEDULE_CHUNK_DAYS))
    for cs, ce in tqdm(chunks, desc="Fetching schedule chunks", leave=False):
        try:
            sched.extend(_fetch_schedule_chunk(cs, ce, team_id))
        except Exception as e:
            print(f"  Giving up on schedule chunk {cs}→{ce}: {e}")
            continue

    game_ids = []
    game_meta = {}
    team_name_map = {}

    for g in sched:
        game_pk = g["game_id"]
        game_ids.append(game_pk)

        home_id = g.get("home_id")
        away_id = g.get("away_id")
        home_name = g.get("home_name")
        away_name = g.get("away_name")

        game_meta[game_pk] = {
            "game_date": g.get("game_date"),
            "home_team": home_name,
            "away_team": away_name,
            "home_id": home_id,
            "away_id": away_id,
        }
        if home_id is not None:
            team_name_map[home_id] = home_name
        if away_id is not None:
            team_name_map[away_id] = away_name

    return game_ids, game_meta, team_name_map


# ==========================================================
# GAME-LEVEL PROCESSING
# ==========================================================
def process_game(game_pk, meta):
    """
    For a single game:
      - collect all stolen-base (SB) events, tied to specific basepath trips
      - collect all scoring events, tied to specific basepath trips

    Returns:
      sb_rows      : list of SB events (one row per SB *event*)
      scoring_rows : list of scoring events (one row per *run* scored)

    Runs are only marked as 'after SB' if the SB happened
    in the *same basepath trip* (reach → steal(s) → score/out).
    """
    if game_pk not in meta:
        return [], []

    game_info = meta[game_pk]
    home_id = game_info["home_id"]
    away_id = game_info["away_id"]

    try:
        pbp = call_with_retry(
            statsapi.get,
            "game_playByPlay",
            {"gamePk": game_pk},
            label=f"pbp gamePk={game_pk}",
        )
    except Exception as e:
        print(f"Error fetching pbp for game {game_pk} after retries: {e}")
        return [], []

    plays = pbp.get("allPlays", [])
    if not plays:
        return [], []

    # Per-runner state for this game
    runner_states = {}

    for play_index, play in enumerate(plays):
        about = play.get("about", {}) or {}
        result = play.get("result", {}) or {}

        inning = about.get("inning")
        half = about.get("halfInning")         # 'top' or 'bottom'
        event_type = result.get("eventType")   # 'single', 'double', 'home_run', etc.
        play_desc = result.get("description")

        # Determine offensive team by half-inning
        if half == "top":
            offense_team_id = away_id
        else:
            offense_team_id = home_id

        # Process each runner involved in this play
        for runner in play.get("runners", []):
            details = runner.get("details", {}) or {}
            movement = runner.get("movement", {}) or {}
            runner_info = details.get("runner", {}) or {}

            runner_id = runner_info.get("id")
            runner_name = runner_info.get("fullName")
            if runner_id is None:
                continue

            state = runner_states.setdefault(
                runner_id,
                {"current": None, "segments": []}
            )
            seg = state["current"]

            start_base = movement.get("start")
            end_base = movement.get("end")
            is_out = bool(movement.get("isOut", False))
            is_scoring = bool(details.get("isScoringEvent", False) and end_base == "score")

            # --- Robust stolen base detection (per RUNNER movement) ---
            event_text = (details.get("event") or "").lower()
            reason_text = (movement.get("reason") or "").lower()
            event_type_text = (event_type or "").lower()

            is_sb_event = any(
                ("stolen_base" in txt) or ("stolen base" in txt)
                for txt in (event_text, reason_text, event_type_text)
            )
            # ----------------------------------------------------------

            is_reach = (
                (start_base in (None, "home")) and
                (end_base in ("1B", "2B", "3B")) and
                not is_out and
                not is_scoring
            )

            # 1) First, close the current segment if this movement ends the trip
            if is_scoring and seg is not None:
                seg["scored"] = True
                seg["score_play_index"] = play_index
                seg["score_inning"] = inning
                seg["score_half"] = half
                seg["score_play_desc"] = play_desc
                seg["score_event_type"] = event_type
                seg["score_rbi"] = details.get("rbi")
                state["segments"].append(seg)
                state["current"] = None
                seg = None

            elif is_out and seg is not None:
                seg["scored"] = seg.get("scored", False)
                seg["removed_play_index"] = play_index
                state["segments"].append(seg)
                state["current"] = None
                seg = None

            # 2) If this movement is a "reach" (new trip), start a new segment
            if is_reach:
                if seg is not None:
                    seg["scored"] = seg.get("scored", False)
                    state["segments"].append(seg)

                state["current"] = {
                    "game_pk": game_pk,
                    "runner_id": runner_id,
                    "runner_name": runner_name,
                    "offense_team_id": offense_team_id,
                    "reach_play_index": play_index,
                    "reach_inning": inning,
                    "reach_half": half,
                    "sb_events": [],
                    "scored": False,
                }
                seg = state["current"]

            # 3) Log SB events into the current segment
            if is_sb_event:
                if seg is None:
                    state["current"] = {
                        "game_pk": game_pk,
                        "runner_id": runner_id,
                        "runner_name": runner_name,
                        "offense_team_id": offense_team_id,
                        "reach_play_index": None,
                        "reach_inning": None,
                        "reach_half": None,
                        "sb_events": [],
                        "scored": False,
                    }
                    seg = state["current"]

                seg["sb_events"].append({
                    "play_index": play_index,
                    "inning": inning,
                    "half": half,
                    "event": details.get("event"),
                    "start_base": start_base,
                    "end_base": end_base,
                })

    # After all plays, close any segments still open (no scoring)
    for runner_id, state in runner_states.items():
        seg = state["current"]
        if seg is not None:
            seg["scored"] = seg.get("scored", False)
            state["segments"].append(seg)
            state["current"] = None

    # Build sb_rows and scoring_rows from segments
    sb_rows = []
    scoring_rows = []

    for runner_id, state in runner_states.items():
        for seg in state["segments"]:
            for sb_ev in seg["sb_events"]:
                sb_rows.append({
                    "game_pk": seg["game_pk"],
                    "runner_id": seg["runner_id"],
                    "runner_name": seg["runner_name"],
                    "sb_play_index": sb_ev["play_index"],
                    "sb_inning": sb_ev["inning"],
                    "sb_half": sb_ev["half"],
                    "sb_event": sb_ev["event"],
                    "sb_start_base": sb_ev["start_base"],
                    "sb_end_base": sb_ev["end_base"],
                    "offense_team_id": seg["offense_team_id"],
                    "eventual_score": bool(seg["scored"]),
                })

            if seg.get("scored", False):
                scoring_rows.append({
                    "game_pk": seg["game_pk"],
                    "runner_id": seg["runner_id"],
                    "runner_name": seg["runner_name"],
                    "score_play_index": seg.get("score_play_index"),
                    "score_inning": seg.get("score_inning"),
                    "score_half": seg.get("score_half"),
                    "score_play_desc": seg.get("score_play_desc"),
                    "event_type": seg.get("score_event_type"),
                    "offense_team_id": seg["offense_team_id"],
                    "score_rbi": seg.get("score_rbi"),
                    "runner_had_prior_sb": (len(seg["sb_events"]) > 0),
                })

    return sb_rows, scoring_rows


# ==========================================================
# SEASON RUNS LOOKUP (per player_id + season)
# ==========================================================
# Cache keyed by (player_id, year) so we don't hit the API twice for the same player+season.
_SEASON_RUNS_CACHE = {}

def get_season_runs(player_id, year):
    """
    Fetch a player's total regular-season runs for a given year.
    Uses statsapi.player_stat_data(personId, group='hitting', type='season').
    Returns an int (0 if the stat isn't found or the call fails).
    """
    key = (player_id, year)
    if key in _SEASON_RUNS_CACHE:
        return _SEASON_RUNS_CACHE[key]

    runs_val = 0
    try:
        data = call_with_retry(
            statsapi.player_stat_data,
            personId=player_id,
            group="hitting",
            type="season",
            sportId=1,
            season=year,
            label=f"season runs player={player_id} year={year}",
        )
        # player_stat_data returns {"stats": [ { "group": "...", "type": "...", "stats": {...} }, ... ]}
        for entry in data.get("stats", []) or []:
            stats = entry.get("stats", {}) or {}
            if "runs" in stats:
                try:
                    runs_val = int(stats["runs"])
                except (TypeError, ValueError):
                    runs_val = 0
                break
    except Exception as e:
        print(f"  Warning: could not fetch season runs for player {player_id} ({year}): {e}")
        runs_val = 0

    _SEASON_RUNS_CACHE[key] = runs_val
    return runs_val


# ==========================================================
# SEASON-LEVEL PIPELINE
# ==========================================================
def process_season(year, start_date, end_date, team_id=None):
    """
    Run the full pipeline for a single season and return a per-player DataFrame.
    """
    print(f"\n=== Processing {year} season ({start_date} → {end_date}) ===")
    print("Fetching schedule...")
    game_ids, game_meta, team_name_map = get_schedule_with_metadata(
        start_date, end_date, team_id
    )
    print(f"Found {len(game_ids)} games.")

    all_sb_rows = []
    all_scoring_rows = []

    for game_pk in tqdm(game_ids, desc=f"Processing {year} games"):
        sb_rows, scoring_rows = process_game(game_pk, game_meta)
        all_sb_rows.extend(sb_rows)
        all_scoring_rows.extend(scoring_rows)

    sb_df = pd.DataFrame(all_sb_rows)
    scoring_df = pd.DataFrame(all_scoring_rows)

    print(f"  SB events collected: {len(sb_df)}")
    print(f"  Scoring events collected: {len(scoring_df)}")

    if sb_df.empty:
        print(f"  No SB data for {year}; skipping aggregation.")
        return pd.DataFrame()

    # Make sure key columns exist
    for col in ["runner_id", "runner_name", "offense_team_id", "eventual_score"]:
        if col not in sb_df.columns:
            sb_df[col] = pd.Series(dtype="object")

    # Per-player aggregation
    players = sb_df["runner_id"].dropna().unique().tolist()
    players = sorted([p for p in players if p is not None])

    player_rows = []
    for pid in tqdm(players, desc=f"Aggregating {year} players"):
        player_sb_df = sb_df[sb_df["runner_id"] == pid]

        # Player name (first non-null)
        names = player_sb_df["runner_name"].dropna().unique().tolist()
        runner_name = names[0] if names else ""

        # Dominant team for the season (handles traded players by picking most frequent)
        team_id_val = None
        team_name = ""
        team_counts = player_sb_df["offense_team_id"].dropna().value_counts()
        if not team_counts.empty:
            team_id_val = team_counts.index[0]
            team_name = team_name_map.get(team_id_val, "")

        sb_runner_scored = int(
            player_sb_df[player_sb_df["eventual_score"] == True].shape[0]
        )
        sb_runner_did_not_score = int(
            player_sb_df[player_sb_df["eventual_score"] == False].shape[0]
        )
        total_sb = int(player_sb_df.shape[0])

        # NEW: Total runs scored on the season for this player
        season_runs = get_season_runs(pid, year)

        player_rows.append({
            "player_id": pid,
            "player_name": runner_name,
            "team_id": team_id_val,
            "team_name": team_name,
            "year": year,
            "sb_runner_scored": sb_runner_scored,
            "sb_runner_did_not_score": sb_runner_did_not_score,
            "total_stolen_bases": total_sb,
            "season_total_runs": season_runs,
        })

    season_df = pd.DataFrame(player_rows)
    if not season_df.empty:
        season_df = (
            season_df
            .sort_values(["team_name", "player_name"])
            .reset_index(drop=True)
        )

    return season_df


# ==========================================================
# MAIN: LOOP OVER ALL SEASONS
# ==========================================================
def main():
    all_season_frames = []

    for year in sorted(SEASONS.keys()):
        start_date, end_date = SEASONS[year]
        season_df = process_season(year, start_date, end_date, TEAM_ID)

        if season_df.empty:
            continue

        # Write per-season CSV
        per_season_path = OUTPUT_PLAYER_CSV_PER_SEASON.format(year=year)
        season_df.to_csv(per_season_path, index=False)
        print(f"  Wrote per-player stats for {year} → '{per_season_path}'")

        all_season_frames.append(season_df)

    if not all_season_frames:
        print("\nNo data collected across any season.")
        return

    combined_df = pd.concat(all_season_frames, ignore_index=True)
    combined_df = (
        combined_df
        .sort_values(["year", "team_name", "player_name"])
        .reset_index(drop=True)
    )
    combined_df.to_csv(OUTPUT_PLAYER_CSV_COMBINED, index=False)
    print(f"\nDone! Wrote combined multi-season stats to '{OUTPUT_PLAYER_CSV_COMBINED}'.")
    print(combined_df.head())


if __name__ == "__main__":
    main()