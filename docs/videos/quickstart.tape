# The quickstart clip on docs/site/quickstart.mdx (render.sh also makes the
# README's docs/demo.gif from it).
#
# Render with docs/videos/render.sh quickstart (it stages the directory first).
# Every rootsign, pip and python command typed here must appear on the page;
# tests/unit/test_docs_site_videos.py checks that.

Output docs/site/videos/quickstart.mp4

Source docs/videos/settings.tape

# A fresh venv without rootsign, and HOME pointed at an empty folder so
# ~/.rootsign starts empty. Hidden from the recording.
Hide
Type "cd /tmp/rootsign-videos/quickstart && source .venv/bin/activate"
Enter
Type "export HOME=$PWD/home PIP_DISABLE_PIP_VERSION_CHECK=1 PS1='$ '; unset $(env | grep -o '^ROOTSIGN_[A-Z_]*'); clear"
Enter
Wait
Show

Type "# Install RootSign: no database, no Docker, no API key"
Enter
Type "pip install rootsign"
Enter
Wait
Sleep 1.5s
Type "rootsign version"
Enter
Wait
Sleep 1.5s
Type "clear"
Enter

Type "# Three RootSign calls around ordinary async code"
Enter
Type "cat agent.py"
Enter
Wait
Sleep 7s
Type "clear"
Enter

Type "# Run it: two tool calls, recorded to a local file"
Enter
Type "python agent.py"
Enter
Wait
Sleep 1s
Type "ls ~/.rootsign/sessions/"
Enter
Wait
Sleep 1.5s
Type "rootsign verify --local ~/.rootsign/sessions/*.jsonl"
Enter
Wait
Sleep 3s
Type "echo $?"
Enter
Wait
Sleep 1.5s
Type "clear"
Enter

Type "# Edit a record: change the amount on line 2 (the first tool call)"
Enter
Type "cp ~/.rootsign/sessions/*.jsonl tampered.jsonl"
Enter
Type "sed -i '' '2s/1500.0/15000.0/' tampered.jsonl"
Enter
Type "rootsign verify --local tampered.jsonl"
Enter
Wait
Sleep 4s
Type "echo $?"
Enter
Wait
Sleep 1.5s
Type "clear"
Enter

Type "# Delete a record: line 2 goes missing"
Enter
Type "cp ~/.rootsign/sessions/*.jsonl gap.jsonl"
Enter
Type "sed -i '' '2d' gap.jsonl"
Enter
Type "rootsign verify --local gap.jsonl"
Enter
Wait
Sleep 4s
Type "echo $?"
Enter
Wait
Sleep 3s
