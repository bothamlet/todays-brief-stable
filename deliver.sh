#!/usr/bin/env bash
#
# Build and send an issue; on build failure, send the previous edition.

set -uo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT" || exit 1
exec 9>"$ROOT/.run.lock"
flock -n 9 || { echo "deliver.sh: another run or delivery is already in progress" >&2; exit 1; }
export BRIEF_LOCK_HELD="$ROOT"

NETWORK_WAIT_SECONDS=${BRIEF_NETWORK_WAIT_SECONDS:-300}

DELIVERY_ATTEMPTS=3
DELIVERY_WAIT_SECONDS=${BRIEF_DELIVERY_WAIT_SECONDS:-60}

PIPELINE_ATTEMPTS=2
PIPELINE_WAIT_SECONDS=${BRIEF_PIPELINE_WAIT_SECONDS:-120}

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

# Probe Telegram directly; systemd network readiness does not reset after suspend.
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


started=$(date +%s)
pipeline_status=0

wait_for_network || true

if ((SKIP_PIPELINE)); then
    log "Skipping the pipeline (--no-pipeline); explicitly resending the issue."
    rm -f _site/delivery.json
else
    for ((attempt = 1; attempt <= PIPELINE_ATTEMPTS; attempt++)); do
        log "Building the paper… (attempt $attempt of $PIPELINE_ATTEMPTS)"
        ./run.sh --quiet
        pipeline_status=$?
        ((pipeline_status == 0)) && break

        if ((attempt < PIPELINE_ATTEMPTS)); then
            log "  the pipeline exited $pipeline_status; retrying in ${PIPELINE_WAIT_SECONDS}s"
            sleep "$PIPELINE_WAIT_SECONDS"
            network_is_up || wait_for_network || true
        fi
    done
fi

if ((pipeline_status != 0)); then
    log ""
    log "The pipeline exited $pipeline_status after $PIPELINE_ATTEMPTS attempt(s)."
    log "Delivering the previous issue, marked as such."
fi

if ! python3 bundle.py; then
    echo "deliver.sh: bundle failed — nothing to send" >&2
    exit 1
fi

# A failed image capture falls back to a text briefing.
rm -f _site/frontpage.jpg
python3 frontpage.py || log "  (no front page image this morning)"

sent=1
for ((attempt = 1; attempt <= DELIVERY_ATTEMPTS; attempt++)); do
    if ((pipeline_status != 0)); then
        python3 telegram.py --stale
        delivery_status=$?
    else
        python3 telegram.py
        delivery_status=$?
    fi
    if ((delivery_status == 3)); then
        log "Paper built; Telegram delivery skipped because credentials are missing."
        exit "$pipeline_status"
    fi
    ((delivery_status == 0)) && sent=0
    ((sent == 0)) && break

    if ((attempt < DELIVERY_ATTEMPTS)); then
        log "  delivery failed (attempt $attempt of $DELIVERY_ATTEMPTS), retrying in ${DELIVERY_WAIT_SECONDS}s"
        sleep "$DELIVERY_WAIT_SECONDS"
        network_is_up || wait_for_network || true
    fi
done

if ((sent != 0)); then
    printf 'deliver.sh: Telegram did not accept the paper after %s attempts.\n' "$DELIVERY_ATTEMPTS" >&2
    exit 1
fi

elapsed=$(($(date +%s) - started))
log ""
log "Delivered in $((elapsed / 60))m $((elapsed % 60))s."

# Report stale delivery as a failed build, even when sending succeeded.
exit "$pipeline_status"
