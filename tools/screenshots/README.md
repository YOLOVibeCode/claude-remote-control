# How the terminal screenshots were made

They are real captures, not mockups. Each one is a `tmux capture-pane -e` of a live session,
turned into a styled HTML window by `ansi2html.py` and shot with
[agent-browser](https://github.com/vercel-labs/agent-browser).

```bash
# a throwaway terminal on its own tmux socket, so your real sessions stay out of the picture
tmux -L shots -f /dev/null new-session -d -s outer -x 110 -y 32 -c ~/Dev/my-app "env -u TMUX zsh -i"
tmux -L shots send-keys -t outer "unset TMUX; clear; cc" Enter
./shot.sh 03-session "cc — ~/Dev/my-app" '/remote-control is active'   # extra args are regexes to outline in red
```

`ansi2html.py` replaces every `session_…` and `env_…` id with a fake one before rendering.
Don't publish a real session URL or QR code.
