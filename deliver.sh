#!/usr/bin/env bash
#
# deliver.sh — build today's paper and put it on a phone.
#
# The whole morning, in one command:
#
#   1. run.sh        the nine pipeline steps — discovery through to the beats
#   2. bundle.py     fold the site into one file that opens with no server
#   3. frontpage.py  render the top of it as a picture
#   4. telegram.py   send the picture and the file
#
# Steps 2 to 4 run even when step 1 fails, and that is deliberate. The pipeline
# reads about twenty news feeds over the open internet and calls a model API; a
# partial failure is not an exotic event, it is a Tuesday. run.sh already protects
# the previous issue's files and the page carries a stale badge, so a failed
# morning has a perfectly good paper sitting on disk — yesterday's, honestly
# labelled. Withholding it would mean the one morning something went wrong is the
# one morning with nothing to read, which is a worse answer than the truth.
#
# So a failed run is delivered with a warning on the front and a non-zero exit,
# and a successful one with neither.
#
# Usage:
#     ./deliver.sh                 # the whole morning
#     ./deliver.sh --no-pipeline   # re-send what is already built
#     ./deliver.sh --quiet         # for cron: only speak on failure

set -uo pipefail

cd "$(dirname "$0")"

QUIET=0
SKIP_PIPELINE=0
for arg in "$@"; do
    case "$arg" in
    --quiet) QUIET=1 ;;
    --no-pipeline) SKIP_PIPELINE=1 ;;
    *)
        echo "deliver.sh: unknown argument: $arg" >&2
        exit 2
        ;;
    esac
done

log() { ((QUIET)) || printf '%s\n' "$*"; }

# Credentials are read by telegram.py, from ~/.config/todays-brief/env or from
# the environment if they are already exported. Not sourced here: three callers
# reach this step — this script, the systemd timer and a person at a prompt — and
# one of them will always be the one that forgot.

started=$(date +%s)
pipeline_status=0

if ((SKIP_PIPELINE)); then
    log "Skipping the pipeline (--no-pipeline)."
else
    log "Building the paper…"
    ./run.sh --quiet || pipeline_status=$?
fi

if ((pipeline_status != 0)); then
    log ""
    log "The pipeline exited $pipeline_status."
    log "Delivering the previous issue, marked as such."
fi

# Assembled from whatever is on disk. A bundler that refuses to run because the
# pipeline failed would take the readable paper down with the failed one.
if ! python3 bundle.py; then
    echo "deliver.sh: bundle failed — nothing to send" >&2
    exit 1
fi

# Soft: a paper without a picture is still a paper, and telegram.py falls back to
# sending the briefing as a message rather than losing the headlines with it.
python3 frontpage.py || log "  (no front page image this morning)"

if ((pipeline_status != 0)); then
    python3 telegram.py --stale
else
    python3 telegram.py
fi

elapsed=$(($(date +%s) - started))
log ""
log "Delivered in $((elapsed / 60))m $((elapsed % 60))s."

exit "$pipeline_status"
