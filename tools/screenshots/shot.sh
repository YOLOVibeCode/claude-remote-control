#!/bin/zsh
# shot.sh <name> <window-title> [highlight-regex...]  — capture demo pane to shots/<name>.png
S=${0:A:h}; name=$1; title=$2; shift 2
tmux -L ${SOCK:-shots} capture-pane -e -p -J -t ${TARGET:-outer} > $S/shots/$name.ansi
python3 $S/ansi2html.py $S/shots/$name.ansi $S/shots/$name.html "$title" "$@"
agent-browser --session shots open "file://$S/shots/$name.html" >/dev/null
agent-browser --session shots set viewport 1700 900 2 >/dev/null
agent-browser --session shots screenshot body $S/shots/$name.png >/dev/null && echo "$S/shots/$name.png"
