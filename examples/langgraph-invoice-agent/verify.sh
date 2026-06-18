#!/usr/bin/env bash
# Verify the hash chain of the session `agent.py` just wrote.
#
# Reads the session UUID from .last_session (written by agent.py) and
# runs `rootsign verify` on it.
set -euo pipefail

if [[ ! -f .last_session ]]; then
  echo "no .last_session file — run \`python agent.py\` first" >&2
  exit 1
fi

session_id="$(cat .last_session)"
echo "verifying session: $session_id"
echo
rootsign verify "$session_id"
