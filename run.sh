#!/usr/bin/env bash
#
# run.sh — the whole daily pipeline, in order.
#
#   fetch_world.py → cluster_stories.py → classify_articles.py
#     → track_stories.py → fetch_coverage.py
#   fetch_sports.py    — its own beat, independent of the world chain
#   fetch_lietuva.py   — ditto, and its own sources: the Lithuanian press
#   fetch_europe.py    — reads the world chain's own region labels, fetches nothing
#   fetch_stats.py     — results and standings for the sports page, no news roster
#
# write_desk.py was the sixth step and is PARKED — the desk quote is out of the
# design for now. The script still exists and still runs standalone; it is just
# not part of the daily run. Putting it back means adding it to the four arrays
# below, restoring data/desk.json to PUBLISHED, and uncommenting #deskBlock in
# index.html.
#
# Roughly seven minutes, most of it in classify and coverage: both call the
# model once per headline, so they are network-bound and cannot be parallelised
# without reordering their output.
#
# Usage:
#   ./run.sh                 the full run
#   ./run.sh --from track    resume from a step, reusing earlier outputs
#   ./run.sh --only coverage just one step
#   ./run.sh --keep-going    run on after a failure instead of stopping
#   ./run.sh --no-log        log to stdout only
#   ./run.sh --list          show the steps and their budgets
#
# Two things this script owns that the individual steps cannot know about:
#
#   1. Atomicity of the published page. Two files are what the pages read:
#      data/world_covered.json from coverage and data/sports.json from the sports
#      beat. Both are written at the end of their step rather than as they go, so
#      a failure part-way leaves the previous file intact — but the write itself
#      is not atomic, so a crash mid-write could truncate one. Both are
#      snapshotted before the run and restored together if it fails, because a
#      half-updated page is worse than a day-old one.
#
#   2. Overlap. A run takes minutes; a manual run started while the cron run is
#      still going would interleave two writers on the same SQLite history. One
#      flock prevents that.
#
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT" || exit 1

# --- The pipeline -------------------------------------------------------------
# Parallel arrays, in run order. Budgets are seconds; they are generous — the
# point is to stop a hung socket, not to police normal variance.
STEPS=(discover cluster classify track coverage europe sports stats lietuva)
LABELS=("Discovery" "Clustering" "Classification" "Tracking" "Coverage" "Europe" "Sports" "Stats" "Lietuva")
FUNCS=(fetch_world cluster_stories classify_articles track_stories fetch_coverage fetch_europe fetch_sports fetch_stats fetch_lietuva)
BUDGETS=(600 300 2400 300 2400 300 600 300 600)

# What the pages read. Restored as a unit if the run fails.
PUBLISHED=(data/world_covered.json data/europe.json data/sports.json data/stats.json data/lietuva.json)

LOG_DIR="logs"
LOGFILE=""
QUIET=0
KEEP_GOING=0

# --- Output -------------------------------------------------------------------

RULE="--------------------------------------------------------------------------"

emit() {
    # Everything goes to the log; --quiet keeps stdout for failures only, which
    # is what cron wants: silence when the run is fine, a mail when it is not.
    if [[ -n "$LOGFILE" ]]; then
        printf '%s\n' "$*" >>"$LOGFILE"
    fi
    ((QUIET)) || printf '%s\n' "$*"
    return 0
}

log() {
    emit "[$(date '+%H:%M:%S')] $*"
}

rule() {
    emit "$RULE"
}

die() {
    printf 'run.sh: %s\n' "$*" >&2
    exit 1
}

# --- Failure handling ---------------------------------------------------------

cleanup_done=0
SNAPSHOT=""

restore_published() {
    # Put back the page the reader could see before this run touched anything.
    [[ -n "$SNAPSHOT" ]] || return 0
    local name src path
    for path in "${PUBLISHED[@]}"; do
        name="$(basename "$path")"
        src="$SNAPSHOT/$name"
        if [[ -f "$src" ]]; then
            cp -p "$src" "$path" && log "restored $path"
        fi
    done
}

on_exit() {
    local rc=$?
    ((cleanup_done)) && return
    cleanup_done=1
    if ((rc != 0)); then
        rule
        log "FAILED (exit $rc) — the published page has been left as it was."
        restore_published
    fi
    [[ -n "$SNAPSHOT" ]] && rm -rf "$SNAPSHOT"
    return 0
}
trap on_exit EXIT

snapshot_published() {
    SNAPSHOT="$(mktemp -d)"
    local path
    for path in "${PUBLISHED[@]}"; do
        [[ -f "$path" ]] && cp -p "$path" "$SNAPSHOT/"
    done
    log "snapshotted the published page"
}

# --- Preflight ----------------------------------------------------------------

preflight() {
    command -v python3 >/dev/null || die "python3 not found"
    python3 -c 'import requests' 2>/dev/null || die "the requests package is missing (pip install -r requirements.txt)"

    for func in "${FUNCS[@]}"; do
        [[ -f "$func.py" ]] || die "$func.py is missing — wrong directory?"
    done

    # The model key is optional: every step degrades rather than failing without
    # it. But a run that silently degrades is worth warning about up front.
    if ! python3 -c '
import sys
from llm import get_api_key
sys.exit(0 if get_api_key() else 1)
' 2>/dev/null; then
        log "WARNING: no DeepSeek key found. Classification and topic labelling"
        log "         will fall back to weaker heuristics for this run."
    fi

    mkdir -p "$LOG_DIR" || die "cannot create $LOG_DIR"
}

# --- Step runner --------------------------------------------------------------

run_step() {
    local index=$1
    local label="${LABELS[$index]}"
    local func="${FUNCS[$index]}"
    local budget="${BUDGETS[$index]}"

    rule
    log "$label ($func.py)"
    rule

    local started rc
    started=$(date +%s)

    if [[ -z "$LOGFILE" ]]; then
        timeout "$budget" python3 "$func.py"
        rc=$?
    elif ((QUIET)); then
        timeout "$budget" python3 "$func.py" >>"$LOGFILE" 2>&1
        rc=$?
    else
        timeout "$budget" python3 "$func.py" 2>&1 | tee -a "$LOGFILE"
        rc=${PIPESTATUS[0]}
    fi

    local elapsed=$(($(date +%s) - started))

    if ((rc == 124)); then
        log "$label timed out after $((budget / 60)) minutes."
        return 124
    fi
    if ((rc != 0)); then
        log "$label failed (exit $rc) after ${elapsed}s."
        return $rc
    fi

    printf '\n'
    log "$label done in ${elapsed}s"
    return 0
}

# --- Summary ------------------------------------------------------------------

write_build_stamp() {
    # Written only when a full run finishes, and never on the way out of a failed
    # one: the stamp is the page's only record of when it was last built, so it is
    # what lets the page tell the reader it is showing yesterday's brief rather
    # than today's. A failed run leaves the previous stamp untouched.
    if ! python3 - <<'PY'; then
from pathlib import Path

from common import save_json, utc_now

save_json(
    {"built_at": utc_now().isoformat(timespec="seconds")}, Path("data/built.json")
)
PY
        log "WARNING: could not write the build stamp."
        return 0
    fi
    log "Stamped data/built.json"
}

page_summary() {
    # What the page will actually say, so an unattended log is still worth reading.
    #
    # Stdout only, and no header: the caller prints the heading and routes this
    # through emit() once. When this function drew its own heading, it did so with
    # log(), which writes to the log as a side effect — so the heading landed in
    # the log twice, once from here and once from the caller echoing the capture.
    [[ -f data/world_covered.json ]] || return 0

    python3 - <<'PY' || true
import json
from pathlib import Path

try:
    covered = json.loads(Path("data/world_covered.json").read_text(encoding="utf-8"))
except (OSError, ValueError):
    raise SystemExit(0)

ranked = [s for s in covered if s.get("briefing_rank") is not None]
ranked.sort(key=lambda s: s["briefing_rank"])

for story in ranked:
    coverage = story.get("coverage") or {}
    # One letter per lean that has coverage: L2/C2/R1. Read straight from the
    # pipeline's output rather than recomputed, so the log cannot disagree with
    # the page. Iterate the lean order explicitly: the dict is built in this
    # order, but the display should not depend on that.
    leans = "/".join(
        f"{lean[0].upper()}{len(coverage[lean])}"
        for lean in ("left", "center", "right")
        if coverage.get(lean)
    )
    title = str(story.get("title") or "")[:58]
    print(f"    {story['briefing_rank'] + 1}. {leans:<12} {title}")
PY
}

# --- Argument parsing ---------------------------------------------------------

usage() {
    # Everything after the shebang, up to the first line that is not a comment.
    awk 'NR > 2 { if ($0 !~ /^#/) exit; sub(/^# ?/, ""); print }' "$0"
}

list_steps() {
    printf '%-4s %-8s %-20s %s\n' "n" "step" "script" "budget"
    local i
    for i in "${!STEPS[@]}"; do
        printf '%-4s %-8s %-20s %ss\n' "$((i + 1))" "${STEPS[$i]}" "${FUNCS[$i]}.py" "${BUDGETS[$i]}"
    done
}

index_of() {
    local wanted=$1 i
    for i in "${!STEPS[@]}"; do
        if [[ "${STEPS[$i]}" == "$wanted" ]]; then
            printf '%s' "$i"
            return 0
        fi
    done
    return 1
}

FIRST=0
LAST=$((${#STEPS[@]} - 1))
# Only a full run rebuilds the page, so only a full run may stamp it as rebuilt.
FULL_RUN=1

parse_args() {
    while (($#)); do
        case "$1" in
        --from)
            [[ $# -ge 2 ]] || die "--from needs a step name"
            FIRST="$(index_of "$2")" || die "unknown step '$2' (try --list)"
            FULL_RUN=0
            shift 2
            ;;
        --only)
            [[ $# -ge 2 ]] || die "--only needs a step name"
            LAST="$(index_of "$2")" || die "unknown step '$2' (try --list)"
            FIRST=$LAST
            FULL_RUN=0
            shift 2
            ;;
        --keep-going)
            KEEP_GOING=1
            shift
            ;;
        --quiet)
            QUIET=1
            shift
            ;;
        --no-log)
            LOGFILE=""
            shift
            ;;
        --list)
            list_steps
            exit 0
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *) die "unknown option '$1' (try --help)" ;;
        esac
    done
    ((FIRST <= LAST)) || die "--from comes after --only; nothing to do"
}

# --- Main ---------------------------------------------------------------------

parse_args "$@"

# One writer at a time: a manual run must not interleave with the cron run on the
# same SQLite history.
LOCK="$ROOT/.run.lock"
exec 9>"$LOCK"
if ! flock -n 9; then
    die "another run is already in progress (lock: $LOCK)"
fi

if [[ -z "$LOGFILE" ]]; then
    LOGFILE="$LOG_DIR/run-$(date '+%Y-%m-%d').log"
fi

if ((FULL_RUN)); then
    # A full run replaces the page, so the snapshot is worth taking.
    snapshot_published
else
    log "Partial run: steps $((FIRST + 1))–$((LAST + 1)) only, published page untouched."
fi

emit ""
rule
log "Run started — steps $((FIRST + 1))–$((LAST + 1)) of ${#STEPS[@]}"
rule

preflight

for ((i = FIRST; i <= LAST; i++)); do
    # Not `if ! run_step`: inside that branch $? is the status of the negated
    # pipeline, which is 0 when the step failed — the run would exit 0 after
    # failing, and the EXIT trap would not restore the page.
    run_step "$i"
    status=$?
    if ((status != 0)); then
        if ((KEEP_GOING)); then
            log "Continuing anyway (--keep-going)."
            continue
        fi
        # Stop at the first failure: the next step reads this step's output file,
        # so carrying on would build the page from a mix of today and yesterday.
        exit "$status"
    fi
done

if ((FULL_RUN)); then
    write_build_stamp
fi

summary="$(page_summary)"
if [[ -n "$summary" ]]; then
    emit ""
    rule
    log "What the page says"
    rule
    emit "$summary"
fi

emit ""
log "Run finished."
