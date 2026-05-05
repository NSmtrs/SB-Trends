import statsapi
import pandas as pd
from tqdm import tqdm

# ==========================
# USER SETTINGS – EDIT THESE
# ==========================

# Date range (MM/DD/YYYY)
START_DATE = "03/28/2024"
END_DATE   = "09/30/2024"

# Optional: limit to one team (MLB team ID), or set to None for all teams
# Example: 139 = Tampa Bay Rays, 110 = Orioles, etc.
TEAM_ID = None   # e.g. 139 or None


# Output file
OUTPUT_TEAM_CSV = "team_sb_scoring_stats2024.csv"


# ==========================
# SCHEDULE + METADATA
# ==========================

def get_schedule_with_metadata(start_date, end_date, team_id=None):
    """
    Get game IDs and metadata (date, home/away teams, ids) for date range.
    """
    kwargs = dict(start_date=start_date, end_date=end_date, sportId=1)
    if team_id is not None:
        kwargs["team"] = team_id

    sched = statsapi.schedule(**kwargs)

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


# ==========================
# GAME-LEVEL PROCESSING
# ==========================

def process_game(game_pk, meta):
    """
    For a single game:
      - collect all stolen-base (SB) events, tied to specific basepath trips
      - collect all scoring events, tied to specific basepath trips
      - collect all plays (for singles/doubles totals)

    Returns:
      sb_rows      : list of SB events (one row per SB *event*)
      scoring_rows : list of scoring events (one row per *run* scored)
      play_rows    : list of plays (one row per play)

    IMPORTANT CHANGE:
    Runs are only marked as 'after SB' if the SB happened
    in the *same basepath trip* (reach → steal(s) → score/out).
    """
    if game_pk not in meta:
        return [], [], []

    game_info = meta[game_pk]
    home_id = game_info["home_id"]
    away_id = game_info["away_id"]

    try:
        pbp = statsapi.get("game_playByPlay", {"gamePk": game_pk})
    except Exception as e:
        print(f"Error fetching pbp for game {game_pk}: {e}")
        return [], [], []

    plays = pbp.get("allPlays", [])
    if not plays:
        return [], [], []

    # One row per play (for total singles/doubles)
    play_rows = []

    # Per-runner state for this game
    # runner_states[runner_id] = {"current": segment_or_None, "segments": [segment, ...]}
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

        # Track the play itself (for total singles/doubles)
        play_rows.append({
            "game_pk": game_pk,
            "inning": inning,
            "half": half,
            "offense_team_id": offense_team_id,
            "event_type": event_type,
            "play_index": play_index,
        })

        # Process each runner involved in this play
        for runner in play.get("runners", []):
            details = runner.get("details", {}) or {}
            movement = runner.get("movement", {}) or {}

            runner_info = details.get("runner", {}) or {}
            runner_id = runner_info.get("id")
            runner_name = runner_info.get("fullName")

            if runner_id is None:
                continue

            # Initialize state for this runner in this game
            state = runner_states.setdefault(
                runner_id,
                {"current": None, "segments": []}
            )
            seg = state["current"]

            start_base = movement.get("start")   # e.g. '1B', '2B', '3B', None
            end_base = movement.get("end")       # e.g. '2B', '3B', 'score', 'out', etc.
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

            # Does this movement represent the runner *reaching* base
            # and remaining on base (i.e., new trip)?
            # Example: start None/home -> end '1B'/'2B'/'3B', not out, not scoring.
            is_reach = (
                (start_base in (None, "home")) and
                (end_base in ("1B", "2B", "3B")) and
                not is_out and
                not is_scoring
            )

            # 1) First, close the current segment if this movement ends the trip
            if is_scoring and seg is not None:
                # Runner scores on this play
                seg["scored"] = True
                seg["score_play_index"] = play_index
                seg["score_inning"] = inning
                seg["score_half"] = half
                seg["score_play_desc"] = play_desc
                seg["score_event_type"] = event_type
                seg["score_rbi"] = details.get("rbi")  # <--- NEW

                # Store this finished segment
                state["segments"].append(seg)
                state["current"] = None
                seg = None


            elif is_out and seg is not None:
                # Runner is removed from bases without scoring
                seg["scored"] = seg.get("scored", False)
                seg["removed_play_index"] = play_index
                state["segments"].append(seg)
                state["current"] = None
                seg = None

            # 2) If this movement is a "reach" (new trip), start a new segment
            if is_reach:
                # In weird edge cases where a segment is still open, close it without scoring
                if seg is not None:
                    seg["scored"] = seg.get("scored", False)
                    state["segments"].append(seg)

                # Start a new basepath trip segment
                state["current"] = {
                    "game_pk": game_pk,
                    "runner_id": runner_id,
                    "runner_name": runner_name,
                    "offense_team_id": offense_team_id,
                    "reach_play_index": play_index,
                    "reach_inning": inning,
                    "reach_half": half,
                    "sb_events": [],  # list of SB events within this trip
                    "scored": False,
                }
                seg = state["current"]

            # 3) Log SB events into the current segment
            if is_sb_event:
                # If somehow we see an SB without a current segment, create an implicit one
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
            # Each SB in the segment inherits whether this trip eventually scored
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

            # Add one scoring row per *trip* that ended in a run
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
                    "score_rbi": seg.get("score_rbi"),  # <--- NEW
                    # TRUE iff this specific trip had at least one SB
                    "runner_had_prior_sb": (len(seg["sb_events"]) > 0),
                    })


    return sb_rows, scoring_rows, play_rows




# ==========================
# MAIN: COLLECT + AGGREGATE
# ==========================

def main():
    print("Fetching schedule...")
    game_ids, game_meta, team_name_map = get_schedule_with_metadata(
        START_DATE, END_DATE, TEAM_ID
    )
    print(f"Found {len(game_ids)} games.")

    all_sb_rows = []
    all_scoring_rows = []
    all_play_rows = []

    for game_pk in tqdm(game_ids, desc="Processing games"):
        sb_rows, scoring_rows, play_rows = process_game(game_pk, game_meta)
        all_sb_rows.extend(sb_rows)
        all_scoring_rows.extend(scoring_rows)
        all_play_rows.extend(play_rows)

    sb_df = pd.DataFrame(all_sb_rows)
    scoring_df = pd.DataFrame(all_scoring_rows)
    play_df = pd.DataFrame(all_play_rows)

    print(f"SB events collected: {len(sb_df)}")
    print(f"Scoring events collected: {len(scoring_df)}")
    print(f"Total plays collected: {len(play_df)}")

    # If nothing, bail
    if sb_df.empty and scoring_df.empty and play_df.empty:
        print("No data collected; nothing to aggregate.")
        return

    # --- Ensure key columns exist even if frames are empty (prevents KeyError) ---

    # For SB dataframe
    for col in ["offense_team_id", "eventual_score"]:
        if col not in sb_df.columns:
            sb_df[col] = pd.Series(dtype="object")

    # For scoring dataframe
    for col in ["offense_team_id", "runner_had_prior_sb", "event_type"]:
        if col not in scoring_df.columns:
            scoring_df[col] = pd.Series(dtype="object")

    # For plays dataframe
    for col in ["offense_team_id", "event_type", "score_rbi"]:
        if col not in play_df.columns:
            play_df[col] = pd.Series(dtype="object")

    # ==========================
    # BUILD PER-TEAM STATS
    # ==========================

    # Set of all teams that appeared on offense in this dataset
    teams = set()
    if "offense_team_id" in scoring_df.columns:
        teams.update(scoring_df["offense_team_id"].dropna().unique().tolist())
    if "offense_team_id" in sb_df.columns:
        teams.update(sb_df["offense_team_id"].dropna().unique().tolist())
    if "offense_team_id" in play_df.columns:
        teams.update(play_df["offense_team_id"].dropna().unique().tolist())

    teams = sorted([t for t in teams if t is not None])

    if not teams:
        print("No offensive teams found in data – nothing to aggregate.")
        return

    team_rows = []

    for tid in teams:
        # Masks
        sb_mask = (sb_df["offense_team_id"] == tid)
        sc_mask = (scoring_df["offense_team_id"] == tid)
        pl_mask = (play_df["offense_team_id"] == tid)

        # Helper lambdas that handle empty frames
        def count_sb(cond):
            if sb_df.empty:
                return 0
            return sb_df[cond].shape[0]

        def count_sc(cond):
            if scoring_df.empty:
                return 0
            return scoring_df[cond].shape[0]

        def count_pl(cond):
            if play_df.empty:
                return 0
            return play_df[cond].shape[0]

        # ---- Your requested metrics ----

        # Total times when a runner had stolen a base and ended up scoring
        total_sb_and_scored = 0
        if "runner_had_prior_sb" in scoring_df.columns:
            total_sb_and_scored = count_sc(sc_mask & (scoring_df["runner_had_prior_sb"] == True))

        # Total times when a runner had stolen a base and did NOT end up scoring
        total_sb_no_score = 0
        if "eventual_score" in sb_df.columns:
            total_sb_no_score = count_sb(sb_mask & (sb_df["eventual_score"] == False))

        # Total runs scored per team
        total_runs = count_sc(sc_mask)

        # Total times when a runner had stolen a base and ended up scoring on a home run
        total_sb_and_scored_on_hr = 0
        if "runner_had_prior_sb" in scoring_df.columns and "event_type" in scoring_df.columns:
            total_sb_and_scored_on_hr = count_sc(
                sc_mask
                & (scoring_df["runner_had_prior_sb"] == True)
                & (scoring_df["event_type"] == "home_run")
            )

        # Total runs scored off a home run
        total_runs_hr = 0
        if "event_type" in scoring_df.columns:
            total_runs_hr = count_sc(sc_mask & (scoring_df["event_type"] == "home_run"))

        # Total runs scored off a single
        total_runs_single = 0
        if "event_type" in scoring_df.columns:
            total_runs_single = count_sc(sc_mask & (scoring_df["event_type"] == "single"))

        # Total runs scored off a double
        total_runs_double = 0
        if "event_type" in scoring_df.columns:
            total_runs_double = count_sc(sc_mask & (scoring_df["event_type"] == "double"))

        # Total runs scored off a single when they had stolen a base
        total_runs_single_after_sb = 0
        if "runner_had_prior_sb" in scoring_df.columns and "event_type" in scoring_df.columns:
            total_runs_single_after_sb = count_sc(
                sc_mask
                & (scoring_df["event_type"] == "single")
                & (scoring_df["runner_had_prior_sb"] == True)
            )

        # Total runs scored off a double when they had stolen a base
        total_runs_double_after_sb = 0
        if "runner_had_prior_sb" in scoring_df.columns and "event_type" in scoring_df.columns:
            total_runs_double_after_sb = count_sc(
                sc_mask
                & (scoring_df["event_type"] == "double")
                & (scoring_df["runner_had_prior_sb"] == True)
            )
        # Total sac fly runs after SB
        sac_fly_after_sb = 0
        if (
                "runner_had_prior_sb" in scoring_df.columns
                and "event_type" in scoring_df.columns
        ):
            sac_fly_after_sb = count_sc(
                sc_mask
                & (scoring_df["runner_had_prior_sb"] == True)
                & (scoring_df["event_type"].isin(["sac_fly", "sac_fly_double_play"]))
            )

        # Total out-RBI runs after SB (non-hit, non-sac-fly with RBI)
        out_rbi_after_sb = 0
        if (
                "runner_had_prior_sb" in scoring_df.columns
                and "event_type" in scoring_df.columns
                and "score_rbi" in scoring_df.columns
        ):
        # treat "out RBI" as: RBI == 1 AND not a hit AND not sac fly
            out_rbi_after_sb = count_sc(
                sc_mask
                & (scoring_df["runner_had_prior_sb"] == True)
                & (scoring_df["score_rbi"] == 1)
                & (~scoring_df["event_type"].isin(
                    ["single", "double", "triple", "home_run", "sac_fly", "sac_fly_double_play"]
            ))
            )

        # Total singles (regardless of scoring)
        total_singles = 0
        if "event_type" in play_df.columns:
            total_singles = count_pl(pl_mask & (play_df["event_type"] == "single"))

        # Total doubles (regardless of scoring)
        total_doubles = 0
        if "event_type" in play_df.columns:
            total_doubles = count_pl(pl_mask & (play_df["event_type"] == "double"))

        team_rows.append({
            "team_id": tid,
            "team_name": team_name_map.get(tid, ""),

            "sb_runner_scored": total_sb_and_scored,
            "sb_runner_did_not_score": total_sb_no_score,
            "total_runs_scored": total_runs,
            "sb_runner_scored_on_hr": total_sb_and_scored_on_hr,
            "runs_scored_off_hr": total_runs_hr,
            "runs_scored_off_single": total_runs_single,
            "runs_scored_off_double": total_runs_double,
            "runs_scored_off_single_after_sb": total_runs_single_after_sb,
            "runs_scored_off_double_after_sb": total_runs_double_after_sb,
            "sac_fly_after_sb": sac_fly_after_sb,
            "out_rbi_after_sb": out_rbi_after_sb,
            "total_singles": total_singles,
            "total_doubles": total_doubles,
        })

    team_stats_df = pd.DataFrame(team_rows)
    team_stats_df = team_stats_df.sort_values("team_name").reset_index(drop=True)

    team_stats_df.to_csv(OUTPUT_TEAM_CSV, index=False)
    print(f"\nDone! Wrote per-team stats to '{OUTPUT_TEAM_CSV}'.")
    print(team_stats_df.head())


if __name__ == "__main__":
    main()
