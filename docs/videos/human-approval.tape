# The approval clip on docs/site/guides/human-approval.mdx: the agent waits in
# one terminal while a person approves, then rejects, from another.
#
# Render with docs/videos/render.sh human-approval. Needs tmux and the
# rootsign-timescaledb container from `rootsign-admin start-db`; stage.py
# creates a scratch database on it. The action ID only exists once the agent
# is waiting, so the approver pane reads it from `rootsign approve --list`
# into $ID rather than typing it.

Output docs/site/videos/human-approval.mp4

Source docs/videos/settings.tape

Hide
Type "export PATH=$PWD/.venv/bin:$PATH && cd /tmp/rootsign-videos/human-approval"
Enter
Type "export HOME=$PWD/home PS1='$ '; unset $(env | grep -o '^ROOTSIGN_[A-Z_]*'); set -a; . ./db.env; set +a; clear"
Enter
Wait
Show

Type "cat transfer.py"
Enter
Wait
Sleep 7s

# Two terminals: the agent above, the approver below.
Hide
Type "tmux -f tmux.conf -L rootsign-video new-session -s demo \; split-window -v \; select-pane -t 0 -T 'Terminal 1: the agent' \; select-pane -t 1 -T 'Terminal 2: the approver' \; select-pane -t 0"
Enter
Sleep 1.5s
Show

Type "python transfer.py"
Enter
Sleep 3s
Ctrl+B
Down
Type "rootsign approve --list"
Enter
Wait+Screen /Pending approvals \(1\)/
Sleep 3s
Type "ID=$(rootsign approve --list | awk 'NR==2 {print $1}')"
Enter
Sleep 0.5s
Type 'rootsign approve $ID --reason "Verified with the supplier by phone"'
Enter
Wait+Screen /approved\./
Wait+Screen /transferred \$250,000\.00/
Sleep 5s

# The same call again, rejected this time.
Ctrl+B
Up
Type "python transfer.py"
Enter
Sleep 3s
Ctrl+B
Down
Type "clear"
Enter
Type "ID=$(rootsign approve --list | awk 'NR==2 {print $1}')"
Enter
Sleep 0.5s
Type 'rootsign approve $ID --reject --reason "Above the unattended mandate"'
Enter
Wait+Screen /rejected\./
Wait+Screen /rejected: Action/
Sleep 6s
