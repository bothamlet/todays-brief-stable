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
# The two waits below exist because of the first morning this ran for real. The
# laptop suspended at 01:39 and resumed at 07:02:51; the timer fired at exactly
# 07:02:51, which is the catch-up working perfectly — and the job failed six
# seconds later with ConnectionError on every request, because a machine that has
# just woken has no network for the first half-minute.
#
# `After=network-online.target` in the unit does not help, and that is the trap:
# the target is a one-shot that was satisfied when the laptop booted, so after a
# resume it is still "active" and the ordering constraint means nothing. The job
# ran with no DNS at all.
#
# Usage:
#     ./deliver.sh                 # the whole morning
#     ./deliver.sh --no-pipeline   # re-send what is already built
#     ./deliver.sh --quiet         # for the timer: only speak on failure

set -uo pipefail

cd "$(dirname "$0")"

# How long to wait for the network after a resume. Generous, because the cost of
# waiting is a paper a few minutes late and the cost of not waiting is no paper at
# all — the timer fires once, and a failed run is not retried until tomorrow.
NETWORK_WAIT_SECONDS=300

# How long to keep trying to reach Telegram. Separate from the wait above because
# it covers a different failure: the network being up and the API being slow, or
# a connection dropped mid-upload.
DELIVERY_ATTEMPTS=3
DELIVERY_WAIT_SECONDS=60

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

log() { ((QUIET)) || printf '%s\n' "$*" ; }

# Is there actually a network, as opposed to a satisfied target?
#
# A TCP connect rather than a ping or a DNS lookup: ICMP is often filtered, and
# DNS can answer from a cache before a route exists — both of which would say yes
# to a machine that cannot reach anything.
network_is_up() {
    python3 - <<'PY' >/dev/null 2>&1
import socket
import sys

try:
    socket.create_connection(("api.telegram.org", 443), timeout=5).close()
except OSError:
    sys.exit(1)
PY
}

wait_for_network() {
    local waited=0
    while ((waited < NETWORK_WAIT_SECONDS)); do
        if network_is_up; then
            if ((waited > 0)); then
                log "Network came up after ${waited}s."
            fi
            return 0
        fi
        if ((waited == 0)); then
            log "No network yet — the machine has just woken. Waiting…"
        fi
        sleep 5
        waited=$((waited + 5))
    done

    log "Still no network after ${NETWORK_WAIT_SECONDS}s. Running anyway: the"
    log "pipeline records what it cannot reach, and yesterday's issue is still"
    log "delivered rather than nothing at all."
    return 1
}

# Credentials are read by telegram.py, from ~/.config/todays-brief/env or from
# the environment if they are already exported. Not sourced here: three callers
# reach this step — this script, the systemd timer and a person at a prompt — and
# one of them will always be the one that forgot.

started=$(date +%s)
pipeline_status=0

# Before anything else, and before the pipeline in particular: a run that starts
# without a network does not fail cleanly, it fails slowly and partially, leaving
# some files updated and some not.
wait_for_network || true

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

# Retried, because the timer fires exactly once. A message that fails to send at
# 07:03 is not tried again until 07:00 tomorrow, so a dropped connection costs the
# whole day — the one failure here with no second chance to absorb it.
sent=1
for ((attempt = 1; attempt <= DELIVERY_ATTEMPTS; attempt++)); do
    if ((pipeline_status != 0)); then
        python3 telegram.py --stale && sent=0
    else
        python3 telegram.py && sent=0
    fi
    ((sent == 0)) && break

    if ((attempt < DELIVERY_ATTEMPTS)); then
        log "  delivery failed (attempt $attempt of $DELIVERY_ATTEMPTS), retrying in ${DELIVERY_WAIT_SECONDS}s"
        sleep "$DELIVERY_WAIT_SECONDS"
        network_is_up || wait_for_network || true
    fi
done

if ((sent != 0)); then
    log "Telegram did not accept the paper after $DELIVERY_ATTEMPTS attempts."
fi

elapsed=$(($(date +%s) - started))
log ""
log "Delivered in $((elapsed / 60))m $((elapsed % 60))s."

# A failed pipeline is reported as a failure, but a paper that was built and sent
# is not: the exit code is what the timer's journal shows, and a run that delivered
# yesterday's issue did its job.
if ((pipeline_status != 0 && sent == 0)); then
    exit "$pipeline_status"
fi
exit 0
