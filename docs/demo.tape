# vhs script for docs/demo.gif — happy path RootSign demo.
#
# Regenerate: vhs docs/demo.tape
# Prereqs:
#   * vhs installed (brew install vhs)
#   * the project venv at .venv/ with rootsign installed (uv sync)
#
# No database needed since v0.2.0 — the demo runs on the default JSONL
# backend and writes to /tmp/rs_demo_data. The export beat writes a bundle to
# /tmp/rs_demo_bundles; demo_for_gif.py wipes both, because `export` refuses to
# overwrite an existing bundle and a second take would otherwise fail.

Output docs/demo.gif

Set FontFamily "Menlo"
Set FontSize 16
Set Width 1100
Set Height 560
Set Padding 24
Set Theme "Catppuccin Mocha"
Set TypingSpeed 60ms
Set PlaybackSpeed 1.2

# Activate the project venv and use a tidy prompt. Hidden from the GIF.
Hide
Type "source .venv/bin/activate"
Enter
Type "export PS1='$ '"
Enter
Type "clear"
Enter
Show

# Step 1: the pitch. Shell comments, so nothing is actually installed here —
# the venv already has rootsign (see Prereqs above).
Type "# RootSign: tamper-evident provenance for AI agent tool calls"
Sleep 500ms
Enter
Type "# pip install rootsign  —  no database, no Docker, no plumbing"
Sleep 500ms
Enter
Sleep 800ms

# Step 2: run an instrumented agent — init() + session() + @trace
Type "python scripts/demo_for_gif.py"
Sleep 300ms
Enter
Sleep 2500ms

# Step 3: verify the chain offline
Type "rootsign verify --local $(cat /tmp/rs_demo_session)"
Sleep 300ms
Enter
Sleep 2500ms

# Step 4: the beat that leaves engineering — hand the session to someone who
# will never open a JSONL file. The manifest hash is the line worth pausing on,
# so it gets the longest sleep in the tape.
Type "rootsign export --local $(cat /tmp/rs_demo_session) --out /tmp/rs_demo_bundles"
Sleep 300ms
Enter
Sleep 4000ms

# Final breath before the loop restarts
Sleep 1500ms
