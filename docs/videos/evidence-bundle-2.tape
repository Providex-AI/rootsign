# Part 2 of the evidence-bundle clip: check the bundle, edit the report, check
# again. Runs in the folder part 1 exported into.

Output "/tmp/rootsign-videos/evidence-bundle/part2.mp4"

Source docs/videos/settings.tape

Hide
Type "export PATH=$PWD/.venv/bin:$PATH && cd /tmp/rootsign-videos/evidence-bundle"
Enter
Type "export HOME=$PWD/home PS1='$ '; unset $(env | grep -o '^ROOTSIGN_[A-Z_]*'); clear"
Enter
Wait
Show

Type "# Before trusting it, the reviewer checks the folder they received"
Enter
Type "rootsign export --check ./bundles/evidence-*"
Enter
Wait
Sleep 5s
Type "clear"
Enter

Type "# Someone lowers the amount in the report after export"
Enter
Type "sed -i '' 's/1500.0/150.0/' bundles/evidence-*/report.html"
Enter
Type "rootsign export --check ./bundles/evidence-*"
Enter
Wait
Sleep 4s
Type "echo $?"
Enter
Wait
Sleep 4s
