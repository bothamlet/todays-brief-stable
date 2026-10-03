#!/usr/bin/env bash
# run.sh — collect and publish one consistent phone issue.
# Usage: ./run.sh [--from STEP | --only STEP] [--keep-going] [--quiet] [--no-log]
# --keep-going is diagnostic: any failed step still rolls back and exits non-zero.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT" || exit 1
STEPS=(discover cluster classify track coverage europe sports lietuva tech)
LABELS=(Discovery Clustering Classification Tracking Coverage Europe Sports Lietuva Tech)
FUNCS=(fetch_world cluster_stories classify_articles track_stories fetch_coverage fetch_europe fetch_sports fetch_lietuva fetch_tech)
BUDGETS=(600 300 2400 300 2400 300 600 600 600)
# Include intermediate inputs and history so a failed attempt is fully reversible.
PUBLISHED=(world_raw.json world_clustered.json world_filtered.json world_tracked.json world_covered.json europe.json sports.json lietuva.json tech.json built.json briefing.db briefing.db-journal briefing.db-wal briefing.db-shm)
FIRST=0
LAST=$((${#STEPS[@]} - 1))
FULL_RUN=1
QUIET=0
KEEP_GOING=0
LOG_ENABLED=1
LOGFILE=""
SNAPSHOT=""
emit() {
    [[ -z "$LOGFILE" ]] || printf '%s\n' "$*" >>"$LOGFILE"
    ((QUIET)) || printf '%s\n' "$*"
    return 0
}
log() { emit "[$(date '+%H:%M:%S')] $*"; }
die() { printf 'run.sh: %s\n' "$*" >&2; exit 1; }
index_of() {
    local i
    for i in "${!STEPS[@]}"; do
        if [[ "${STEPS[$i]}" == "$1" ]]; then printf '%s' "$i"; return 0; fi
    done
    return 1
}
while (($#)); do
    case "$1" in
        --from|--only)
            [[ $# -ge 2 ]] || die "$1 needs a step name"
            idx="$(index_of "$2")" || die "unknown step '$2' (try --list)"
            if [[ "$1" == --only ]]; then FIRST=$idx; LAST=$idx; else FIRST=$idx; fi
            FULL_RUN=0; shift 2 ;;
        --keep-going) KEEP_GOING=1; shift ;;
        --quiet) QUIET=1; shift ;;
        --no-log) LOG_ENABLED=0; shift ;;
        --list)
            for i in "${!STEPS[@]}"; do printf '%-9s %-22s %ss\n' "${STEPS[$i]}" "${FUNCS[$i]}.py" "${BUDGETS[$i]}"; done
            exit 0 ;;
        -h|--help) sed -n '2,4s/^# //p' "$0"; exit 0 ;;
        *) die "unknown option '$1' (try --help)" ;;
    esac
done
((FIRST <= LAST)) || die "no steps selected"
for utility in python3 timeout flock; do command -v "$utility" >/dev/null || die "$utility not found (Linux is required)"; done
# deliver.sh holds the same descriptor through collection, packaging and sending.
if [[ "${BRIEF_LOCK_HELD:-}" != "$ROOT" || "$(readlink /proc/self/fd/9 2>/dev/null || true)" != "$ROOT/.run.lock" ]]; then
    exec 9>"$ROOT/.run.lock"
fi
flock -n 9 || die "another run or delivery is already in progress"
if ((LOG_ENABLED)); then
    mkdir -p logs || die "cannot create logs"
    LOGFILE="logs/run-$(date '+%Y-%m-%d').log"
fi
python3 -c 'import requests' 2>/dev/null || die "install requirements.txt with the selected Python interpreter"
for func in "${FUNCS[@]}"; do [[ -f "$func.py" ]] || die "$func.py is missing"; done
mkdir -p data || die "cannot create data"
SNAPSHOT="$(mktemp -d)" || die "cannot create snapshot"
for name in "${PUBLISHED[@]}"; do
    if [[ -f "data/$name" ]]; then cp -p "data/$name" "$SNAPSHOT/$name" || die "cannot snapshot $name"; fi
done
cleanup() {
    local rc=$? name
    trap - EXIT
    if ((rc != 0)); then
        log "FAILED (exit $rc) — restoring the previous issue and history."
        for name in "${PUBLISHED[@]}"; do
            if [[ -f "$SNAPSHOT/$name" ]]; then
                cp -p "$SNAPSHOT/$name" "data/$name" || rc=1
            else
                rm -f "data/$name" || rc=1
            fi
        done
        printf 'run.sh: failed; previous issue restored\n' >&2
    fi
    rm -rf "$SNAPSHOT"
    exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
log "Run started — steps $((FIRST + 1))–$((LAST + 1)) of ${#STEPS[@]}"
if ! python3 -c 'from llm import get_api_key; import sys; sys.exit(0 if get_api_key() else 1)' 2>/dev/null; then
    log "WARNING: no DeepSeek key; a fresh classified issue requires one. Try demo.py for an offline sample."
fi
failures=0
for ((i=FIRST; i<=LAST; i++)); do
    log "${LABELS[$i]} (${FUNCS[$i]}.py)"
    if [[ -z "$LOGFILE" ]]; then
        timeout "${BUDGETS[$i]}" python3 "${FUNCS[$i]}.py"; status=$?
    elif ((QUIET)); then
        timeout "${BUDGETS[$i]}" python3 "${FUNCS[$i]}.py" >>"$LOGFILE" 2>&1; status=$?
    else
        timeout "${BUDGETS[$i]}" python3 "${FUNCS[$i]}.py" 2>&1 | tee -a "$LOGFILE"; status=${PIPESTATUS[0]}
    fi
    if ((status != 0)); then
        log "${LABELS[$i]} failed (exit $status)."
        failures=$status
        ((KEEP_GOING)) || exit "$status"
    fi
done
((failures == 0)) || exit "$failures"
if ((FULL_RUN)); then
    python3 - <<'PY' || exit 1
from pathlib import Path
from common import save_json, utc_now
save_json({"built_at": utc_now().isoformat(timespec="seconds")}, Path("data/built.json"))
PY
else
    log "Partial run: selected files updated; full-edition build date unchanged."
fi
log "Run finished."
