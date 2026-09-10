#!/bin/bash
# Restart twitch-recorder as soon as it is not recording anything.
#
# Restarting mid-chunk kills streamlink and truncates the segment ffmpeg is
# still writing, which usually leaves an unplayable file that the recorder then
# quarantines. Waiting for a gap between streams costs nothing and loses no
# footage.
#
# Only processes inside the service cgroup count. Matching on a command line
# with pgrep -f also matched unrelated shells that merely mentioned the pattern,
# which would defer the restart forever.
set -uo pipefail

CGROUP=/sys/fs/cgroup/system.slice/twitch-recorder.service/cgroup.procs

recording() {
    [ -r "$CGROUP" ] || return 1
    while read -r pid; do
        [ -n "$pid" ] || continue
        [ -r "/proc/$pid/cmdline" ] || continue
        if tr "\0" " " < "/proc/$pid/cmdline" | grep -q "bin/streamlink"; then
            return 0
        fi
    done < "$CGROUP"
    return 1
}

if recording; then
    echo "recording in progress, deferring restart"
    exit 0
fi

echo "no active recording, restarting twitch-recorder to pick up new code"
systemctl restart twitch-recorder
systemctl disable --now twitch-recorder-deferred-restart.timer
echo "restart done, deferred timer disabled"
