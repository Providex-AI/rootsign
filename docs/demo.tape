# vhs script for docs/demo.gif — happy path RootSign demo.
#
# Regenerate: vhs docs/demo.tape
# Prereqs:
#   * vhs installed (brew install vhs)
#   * the project venv at .venv/ with rootsign installed (uv sync)
#   * TimescaleDB running on :5432 (rootsign-admin start-db)

Output docs/demo.gif

Set FontFamily "Menlo"
Set FontSize 16
Set Width 1100
Set Height 540
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

# Step 1: run the demo script
Type "# RootSign: tamper-evident provenance for AI agent tool calls"
Sleep 500ms
Enter
Type "python scripts/demo_for_gif.py"
Sleep 300ms
Enter
Sleep 2500ms

# Step 2: verify the chain
Type "rootsign verify $(cat /tmp/rs_demo_session)"
Sleep 300ms
Enter
Sleep 2500ms

# Final breath before the loop restarts
Sleep 1500ms
