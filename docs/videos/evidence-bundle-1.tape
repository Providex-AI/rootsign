# Part 1 of the evidence-bundle clip on docs/site/guides/evidence-bundles.mdx:
# export the session. render.sh joins it to the report scroll and part 2.

Output "/tmp/rootsign-videos/evidence-bundle/part1.mp4"

Source docs/videos/settings.tape

# The repo's rootsign, and the session stage.py recorded under a fresh HOME.
Hide
Type "export PATH=$PWD/.venv/bin:$PATH && cd /tmp/rootsign-videos/evidence-bundle"
Enter
Type "export HOME=$PWD/home PS1='$ '; unset $(env | grep -o '^ROOTSIGN_[A-Z_]*'); clear"
Enter
Wait
Show

Type "# The quickstart's session, to hand to a reviewer"
Enter
Type "ls ~/.rootsign/sessions/"
Enter
Wait
Sleep 1.5s
Type "rootsign export --local ~/.rootsign/sessions/*.jsonl --out ./bundles"
Enter
Wait
Sleep 5s
Type "# Record that manifest hash somewhere else, then send the folder."
Enter
Sleep 1s
Type "# The reviewer opens report.html in any browser:"
Enter
Sleep 2s
