#!/usr/bin/env bash
# Poll every 5 seconds to wake a recovery worker after VM replacement.
# Only the main agent starts
# the persistent front door; the worker returns a notification to it.
set -euo pipefail
source "$HATCH_HOOK_RUNTIME"

quiet() {
    silent "$1" '{}'
    exit 0
}

FILAMENT="$HOME/workspace/skills/filament/bin/filament"
command -v jq >/dev/null 2>&1 || quiet "jq missing"
[ -x "$FILAMENT" ] || quiet "filament CLI missing or not executable"
BOOT_ID="$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || true)"
UPTIME_RAW="$(cat /proc/uptime 2>/dev/null || true)"
UPTIME_SECS="${UPTIME_RAW%%.*}"
[[ "$UPTIME_SECS" =~ ^[0-9]+$ ]] || quiet "uptime unavailable"
[ "$UPTIME_SECS" -lt 900 ] || quiet "uptime ${UPTIME_SECS}s >= 15min"

STATE="$(hook_state_get)" || quiet "hook state unavailable"
# Empty state is normal before the first wake; malformed state is not.
STATE="${STATE:-\{\}}"
printf '%s' "$STATE" | jq -se 'length == 1 and (.[0] | type == "object")' >/dev/null 2>&1 || quiet "invalid hook state JSON"
if [ -n "$BOOT_ID" ] && [ "$BOOT_ID" != unknown ]; then
    SEEN="$(printf '%s' "$STATE" | jq -r '.wake_boot_id // empty')"
    [ "$SEEN" != "$BOOT_ID" ] || quiet "already woke for this boot"
    NEXT_STATE="$(jq -cn --arg b "$BOOT_ID" '{wake_boot_id:$b}')" || quiet "invalid wake state"
else
    # Never persist an empty/unknown ID. Approximate the boot epoch instead;
    # tolerate rounding and small clock drift between polls.
    NOW="$(date +%s)" || quiet "clock unavailable"
    BOOT_TIME=$(( (NOW - UPTIME_SECS) / 60 * 60 ))
    SEEN="$(printf '%s' "$STATE" | jq -r '.wake_boot_time // empty')"
    if [[ "$SEEN" =~ ^[0-9]+$ ]] && [ "$((BOOT_TIME - SEEN))" -ge -120 ] && [ "$((BOOT_TIME - SEEN))" -le 120 ]; then
        quiet "already woke for this approximate boot time"
    fi
    NEXT_STATE="$(jq -cn --argjson t "$BOOT_TIME" '{wake_boot_time:$t}')" || quiet "invalid wake state"
fi

# ensure reads local state only; it needs no credentials.
ENSURE_OUT="$("$FILAMENT" ensure 2>/dev/null)" || quiet "filament ensure unavailable"
printf '%s' "$ENSURE_OUT" | jq -se 'length == 1 and (.[0] | type == "object")' >/dev/null 2>&1 || quiet "invalid ensure JSON"
LISTENER="$(printf '%s' "$ENSURE_OUT" | jq -r '.listener // "unknown"')"
ROLE="$(printf '%s' "$ENSURE_OUT" | jq -r '.role // "unknown"')"
[ "$LISTENER:$ROLE" != alive:frontdoor ] || quiet "frontdoor alive after boot"

# Build and validate the payload BEFORE consuming this boot's wake.
PAYLOAD="$(jq -cn --arg b "$BOOT_ID" --argjson u "$UPTIME_SECS" --argjson e "$ENSURE_OUT" \
    '{event:"vm_replaced",boot_id:$b,uptime_secs:$u,ensure:$e}')" || quiet "invalid wake payload"
printf '%s' "$PAYLOAD" | jq -e 'type == "object"' >/dev/null 2>&1 || quiet "invalid wake payload"
# The runtime suppresses state writes on dry runs.
hook_state_set "$NEXT_STATE"
wake "VM was replaced (uptime ${UPTIME_SECS}s < 15min) and no live frontdoor is present" "$PAYLOAD"
