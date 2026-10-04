# claude-tmux: run Claude Code inside tmux so any SSH login (phone, iPad, laptop)
# can pick the session up. Sourced from ~/.zshrc and ~/.bashrc; works in both shells.
#
#   claude ...   interactive runs start in their own tmux session (named after the folder),
#                with Remote Control on so the Claude app sees it under the same name
#                (aliases like `cc` / `ccc` that call claude get this too)
#   ccserve      let the Claude app start new sessions on this Mac (bypass permissions;
#                folder: $CCSERVE_DIR, default ~/Dev)
#   cpick        list sessions, newest first; type a number to attach
#   NOMENU=1     set before login to skip the automatic menu over SSH

claude() {
  # Pass straight through when already in tmux, not on a terminal, or tmux is missing.
  if [ -n "$TMUX" ] || [ ! -t 0 ] || [ ! -t 1 ] || ! command -v tmux >/dev/null 2>&1; then
    command claude "$@"; return
  fi
  # Pass through print mode, help/version, and subcommands (a lone word like `mcp`, `doctor`).
  case $1 in
    -p|--print|-h|--help|-v|--version) command claude "$@"; return ;;
    -*|'') ;;
    *[!A-Za-z0-9-]*) ;;
    *) command claude "$@"; return ;;
  esac
  local a
  for a in "$@"; do
    case $a in -p|--print) command claude "$@"; return ;; esac
  done

  local base name i=2
  base=$(basename "$PWD" | tr -c 'A-Za-z0-9_\n-' '-')
  name=$base
  while tmux has-session -t "=$name" 2>/dev/null; do name=$base-$i; i=$((i+1)); done
  # Remote Control lets the Claude app / claude.ai/code drive the same session, under the same name.
  local rc="--remote-control"
  for a in "$@"; do
    case $a in --remote-control*) rc="" ;; esac
  done
  if [ -n "$rc" ]; then
    tmux new-session -s "$name" -c "$PWD" -e "PATH=$PATH" claude "$rc" "$name" "$@"
  else
    tmux new-session -s "$name" -c "$PWD" -e "PATH=$PATH" claude "$@"
  fi
}

# ccserve: let the Claude app start new sessions on this Mac (in $CCSERVE_DIR, default ~/Dev; bypass permissions).
# Runs in the tmux session "remote"; `ccserve restart` restarts it; stop it with: tmux kill-session -t remote
ccserve() {
  if tmux has-session -t "=remote" 2>/dev/null; then
    [ "$1" = restart ] || { echo "Already running (tmux session: remote). Use: ccserve restart"; return; }
    tmux kill-session -t "=remote"
    local n=0  # the old server must fully exit before a new one may register
    while pgrep -f 'claude remote-control' >/dev/null && [ $n -lt 20 ]; do sleep 0.5; n=$((n+1)); done
  fi
  tmux new-session -d -s remote -c "${CCSERVE_DIR:-$HOME/Dev}" -e "PATH=$PATH" \
    claude remote-control --name "$(hostname -s | tr 'A-Z' 'a-z')" --permission-mode bypassPermissions \
    && echo "Remote Control server running (tmux session: remote). Pick '$(hostname -s | tr 'A-Z' 'a-z')' in the Claude app."
}

cpick() {
  local list now choice name dir i act att title idle tilde='~'
  list=$(tmux list-sessions -F '#{session_activity}|#{session_name}|#{session_attached}|#{pane_current_path}|#{pane_title}' 2>/dev/null | sort -rn)
  if [ -z "$list" ]; then echo "No tmux sessions. Start one with: cd <project> && claude"; return; fi
  now=$(date +%s)

  echo
  printf '%s\n' "$list" | {
    i=1
    while IFS='|' read -r act name att dir title; do
      idle=$(( now - act ))
      if   [ $idle -lt 60 ];    then idle="now"
      elif [ $idle -lt 3600 ];  then idle="$(( idle / 60 ))m ago"
      elif [ $idle -lt 86400 ]; then idle="$(( idle / 3600 ))h ago"
      else                           idle="$(( idle / 86400 ))d ago"; fi
      [ "$att" -gt 0 ] && idle="$idle, open elsewhere"
      case $title in "$(hostname)"*|"$(hostname -s)"*) title="" ;; esac
      printf ' %d) %s  (%s)\n    %s\n' "$i" "$name" "$idle" "${title:-${dir/#"$HOME"/$tilde}}"
      i=$((i+1))
    done
  }
  printf '\nNumber to attach, n = new claude, Enter = shell: '
  read -r choice
  case $choice in
    '') return ;;
    n)  printf 'Folder [~]: '; read -r dir
        dir=${dir:-$HOME}; dir=${dir/#\~/$HOME}
        cd "$dir" && claude ;;
    *[!0-9]*) return ;;
    *)  name=$(printf '%s\n' "$list" | sed -n "${choice}p" | cut -d'|' -f2)
        if [ -n "$name" ]; then tmux attach -t "=$name"; else echo "No session $choice"; fi ;;
  esac
}

# Show the menu on SSH login when there is something to pick.
if [ -n "$SSH_CONNECTION" ] && [ -z "$TMUX" ] && [ -z "$NOMENU" ] && [ -t 0 ] \
   && command -v tmux >/dev/null 2>&1 && tmux has-session 2>/dev/null; then
  cpick
fi
