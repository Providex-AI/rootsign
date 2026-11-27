#!/usr/bin/env bash
# Render the docs videos: docs/videos/render.sh [clip ...]
#
# Clips: quickstart, evidence-bundle, human-approval. With no arguments, renders
# all of them.
#
# Prerequisites: vhs and ffmpeg (brew install vhs ffmpeg), Python 3.11 or 3.12
# on PATH, the project venv at .venv/, and network access (the quickstart clip
# installs rootsign from PyPI), Google Chrome (the evidence-bundle clip scrolls
# through report.html in it), tmux and the rootsign-timescaledb container from
# `rootsign-admin start-db` (the human-approval clip). Run from the repo root.
#
# Each clip is staged fresh under /tmp/rootsign-videos/<clip> by stage.py, then
# recorded by its tape. Re-render after a change to the commands or the output
# a clip's page shows.
set -euo pipefail

cd "$(dirname "$0")/../.."
clips=("$@")
[ ${#clips[@]} -eq 0 ] && clips=(quickstart evidence-bundle human-approval)

for clip in "${clips[@]}"; do
  echo "== $clip"
  .venv/bin/python docs/videos/stage.py "$clip"
  case "$clip" in
    evidence-bundle)
      # Export in the terminal, scroll the report, then check the bundle.
      work=/tmp/rootsign-videos/evidence-bundle
      vhs docs/videos/evidence-bundle-1.tape
      .venv/bin/python docs/videos/report_scroll.py "$(ls -d $work/bundles/evidence-*)" $work/report.mp4
      vhs docs/videos/evidence-bundle-2.tape
      ffmpeg -v error -y -i $work/part1.mp4 -i $work/report.mp4 -i $work/part2.mp4 -filter_complex \
        "[0:v]setsar=1[a];[1:v]setsar=1[b];[2:v]setsar=1[c];[a][b][c]concat=n=3:v=1:a=0[v]" \
        -map "[v]" -c:v libx264 -pix_fmt yuv420p -r 25 -movflags +faststart docs/site/videos/evidence-bundle.mp4
      ;;
    human-approval)
      vhs docs/videos/human-approval.tape
      tmux -L rootsign-video kill-server 2>/dev/null || true
      ;;
    *) vhs "docs/videos/$clip.tape" ;;
  esac
done

# The README's GIF: GitHub and PyPI cannot play a video file from the repo.
# 8 fps, 880px and 64 colours keep it under 3 MB with the verdict colours intact.
if [[ " ${clips[*]} " == *" quickstart "* ]]; then
  ffmpeg -v error -y -i docs/site/videos/quickstart.mp4 -vf \
    "fps=8,scale=880:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=64:stats_mode=full[p];[b][p]paletteuse=dither=none:diff_mode=rectangle" \
    docs/demo.gif
fi
