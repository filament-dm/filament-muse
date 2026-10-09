#!/usr/bin/env bash
# Poll every 5 seconds to recover after VM replacement or a frontdoor outage.
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

STATE="$(hook_state_get)" || quiet "hook state unavailable"
# Empty state is normal before the first wake; malformed state is not.
STATE="${STATE:-\{\}}"
printf '%s' "$STATE" | jq -se 'length == 1 and (.[0] | type == "object")' >/dev/null 2>&1 || quiet "invalid hook state JSON"
NOW="$(date +%s)" || quiet "clock unavailable"
[[ "$NOW" =~ ^[0-9]+$ ]] || quiet "clock unavailable"
# Bad or future outage timestamps must not prevent recovery indefinitely.
# Keep the original presence check so healthy/paused also clear invalid keys.
HAS_HEALTH="$(printf '%s' "$STATE" | jq -r 'has("down_since") or has("last_health_wake") or has("notified_down_since")')"
STATE="$(printf '%s' "$STATE" | jq -c --argjson now "$NOW" '
    reduce ["down_since", "last_health_wake", "notified_down_since"][] as $key (.;
        if (.[$key] | type) != "number" then del(.[$key])
        elif .[$key] < 0 or .[$key] > ($now + 300) or (.[$key] | floor) != .[$key]
        then del(.[$key]) else . end)
    | if has("down_since") and .down_since > $now then del(.down_since) else . end
')" || quiet "invalid health state"

# ensure reads local state only; it needs no credentials. Validate it before
# any state write, including clearing an outage or recording a new grace period.
ENSURE_OUT="$("$FILAMENT" ensure 2>/dev/null)" || quiet "filament ensure unavailable"
printf '%s' "$ENSURE_OUT" | jq -se 'length == 1 and (.[0] | type == "object")' >/dev/null 2>&1 || quiet "invalid ensure JSON"
LISTENER="$(printf '%s' "$ENSURE_OUT" | jq -r '.listener // "unknown"')"
ROLE="$(printf '%s' "$ENSURE_OUT" | jq -r '.role // "unknown"')"
if [ "$LISTENER" = paused ] || [ "$LISTENER:$ROLE" = alive:frontdoor ] || [ "$LISTENER:$ROLE" = handling:frontdoor ]; then
    # Recovery and auth pauses end this outage, including its notification budget.
    if [ "$HAS_HEALTH" = true ]; then
        NEXT_STATE="$(printf '%s' "$STATE" | jq -c 'del(.down_since, .last_health_wake, .notified_down_since)')" || quiet "invalid wake state"
        hook_state_set "$NEXT_STATE"
    fi
    [ "$LISTENER" != paused ] || quiet "auth paused; not an outage"
    quiet "frontdoor healthy"
fi

BOOT_WAKE=false
if [ "$UPTIME_SECS" -lt 900 ]; then
    if [ -n "$BOOT_ID" ] && [ "$BOOT_ID" != unknown ]; then
        SEEN="$(printf '%s' "$STATE" | jq -r '.wake_boot_id // empty')"
        if [ "$SEEN" != "$BOOT_ID" ]; then
            BOOT_WAKE=true
            NEXT_STATE="$(jq -cn --arg b "$BOOT_ID" '{wake_boot_id:$b}')" || quiet "invalid wake state"
        fi
    else
        # Never persist an empty/unknown ID. Approximate the boot epoch instead;
        # tolerate rounding and small clock drift between polls.
        BOOT_TIME=$(( (NOW - UPTIME_SECS) / 60 * 60 ))
        SEEN="$(printf '%s' "$STATE" | jq -r '.wake_boot_time // empty')"
        if ! [[ "$SEEN" =~ ^[0-9]+$ ]] || [ "$((BOOT_TIME - SEEN))" -lt -120 ] || [ "$((BOOT_TIME - SEEN))" -gt 120 ]; then
            BOOT_WAKE=true
            NEXT_STATE="$(jq -cn --argjson t "$BOOT_TIME" '{wake_boot_time:$t}')" || quiet "invalid wake state"
        fi
    fi
fi

if [ "$BOOT_WAKE" = true ]; then
    PAYLOAD="$(jq -cn --arg b "$BOOT_ID" --argjson u "$UPTIME_SECS" --argjson e "$ENSURE_OUT" \
        '{event:"vm_replaced",boot_id:$b,uptime_secs:$u,ensure:$e}')" || quiet "invalid wake payload"
    WAKE_TEXT="VM was replaced (uptime ${UPTIME_SECS}s < 15min) and no live frontdoor is present"
else
    DOWN_SINCE="$(printf '%s' "$STATE" | jq -r '.down_since // empty')"
    # This records first observation, not death: ensure can still see a fresh
    # listening/handling lock for a while after its owner has died.
    if [ -z "$DOWN_SINCE" ]; then
        NEXT_STATE="$(printf '%s' "$STATE" | jq -c --argjson now "$NOW" '.down_since = $now')" || quiet "invalid wake state"
        hook_state_set "$NEXT_STATE"
        quiet "frontdoor down; grace started"
    fi
    DOWN_SECS=$((NOW - DOWN_SINCE))
    [ "$DOWN_SECS" -ge 90 ] || quiet "frontdoor down ${DOWN_SECS}s; within grace"
    LAST_WAKE="$(printf '%s' "$STATE" | jq -r '.last_health_wake // empty')"
    if [ -n "$LAST_WAKE" ] && [ "$((NOW - LAST_WAKE))" -lt 600 ]; then
        quiet "woke $((NOW - LAST_WAKE))s ago for this outage; cooldown"
    fi
    NOTIFY="$(printf '%s' "$STATE" | jq -r '.notified_down_since != .down_since')"
    PAYLOAD="$(jq -cn --argjson d "$DOWN_SINCE" --argjson u "$UPTIME_SECS" --argjson e "$ENSURE_OUT" --argjson n "$NOTIFY" \
        '{event:"listener_down",down_since:$d,uptime_secs:$u,ensure:$e,notify:$n}')" || quiet "invalid wake payload"
    NEXT_STATE="$(printf '%s' "$STATE" | jq -c --argjson now "$NOW" '.last_health_wake = $now | .notified_down_since = .down_since')" || quiet "invalid wake state"
    WAKE_TEXT="Filament frontdoor down since ${DOWN_SINCE} (${DOWN_SECS}s) and not healed"
fi

# Build and validate the payload BEFORE consuming either kind of wake.
printf '%s' "$PAYLOAD" | jq -se 'length == 1 and (.[0] | type == "object")' >/dev/null 2>&1 || quiet "invalid wake payload"
# The runtime suppresses state writes on dry runs.
hook_state_set "$NEXT_STATE"
wake "$WAKE_TEXT" "$PAYLOAD"
