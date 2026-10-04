#!/usr/bin/env bash
# install.sh — set up `cc` + Remote Control + tmux on this Mac.
#
#   ./install.sh              install (aliases use --dangerously-skip-permissions)
#   ./install.sh --safe       install, but `cc` / `ccc` keep normal permission prompts
#   ./install.sh --uninstall  remove the shell block (copied files are left in place)
#
# Safe to run twice: it backs up anything it replaces and never duplicates the shell block.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
mode=install flags="--dangerously-skip-permissions"
case "${1:-}" in
  --safe) flags="" ;;
  --uninstall) mode=uninstall ;;
  ""|--install) ;;
  *) echo "usage: $0 [--safe|--uninstall]" >&2; exit 2 ;;
esac

begin="# >>> claude-remote-control >>>"
end="# <<< claude-remote-control <<<"
stamp=$(date +%Y%m%d%H%M%S)

rcfiles() {
  # zsh is the macOS default; add bash only if the user already has a bashrc.
  echo "$HOME/.zshrc"
  [ -f "$HOME/.bashrc" ] && echo "$HOME/.bashrc"
  return 0
}

strip_block() {  # remove our marked block from $1, if present
  [ -f "$1" ] || return 0
  grep -qF "$begin" "$1" || return 0
  awk -v b="$begin" -v e="$end" '$0==b{skip=1;next} $0==e{skip=0;next} !skip' "$1" > "$1.tmp.$$"
  mv "$1.tmp.$$" "$1"
}

if [ "$mode" = uninstall ]; then
  for rc in $(rcfiles); do strip_block "$rc" && echo "cleaned $rc"; done
  echo "Done. Open a new terminal. (~/.claude-tmux.sh and ~/.tmux.conf were left in place.)"
  exit 0
fi

command -v claude >/dev/null 2>&1 || { echo "Claude Code not found. Install it first: https://code.claude.com/docs/en/setup" >&2; exit 1; }
command -v tmux   >/dev/null 2>&1 || { echo "tmux not found. Install it first: brew install tmux" >&2; exit 1; }

# 1) the claude() wrapper, cpick and ccserve
if [ -f "$HOME/.claude-tmux.sh" ] && ! cmp -s "$here/shell/claude-tmux.sh" "$HOME/.claude-tmux.sh"; then
  cp "$HOME/.claude-tmux.sh" "$HOME/.claude-tmux.sh.bak.$stamp"
  echo "backed up ~/.claude-tmux.sh -> ~/.claude-tmux.sh.bak.$stamp"
fi
cp "$here/shell/claude-tmux.sh" "$HOME/.claude-tmux.sh"
echo "installed ~/.claude-tmux.sh"

# 2) tmux settings: add only the lines that are missing
touch "$HOME/.tmux.conf"
added=0
while IFS= read -r line; do
  [ -n "$line" ] || continue
  setting=${line%%#*}; setting=$(printf '%s' "$setting" | sed 's/[[:space:]]*$//')
  grep -qF -- "$setting" "$HOME/.tmux.conf" || { printf '%s\n' "$line" >> "$HOME/.tmux.conf"; added=$((added+1)); }
done < "$here/shell/tmux.conf"
echo "~/.tmux.conf: $added line(s) added"

# 3) aliases + source line, inside a marked block we can find again
for rc in $(rcfiles); do
  touch "$rc"
  strip_block "$rc"
  if grep -qE '^[[:space:]]*alias cc=' "$rc"; then
    echo "note: $rc already defines alias cc; leaving yours and not adding another"
    aliases=""
  else
    aliases="alias cc=\"claude${flags:+ $flags}\"
alias ccc=\"claude${flags:+ $flags} -c\""
  fi
  {
    echo ""
    echo "$begin"
    [ -n "$aliases" ] && echo "$aliases"
    echo '[ -f "$HOME/.claude-tmux.sh" ] && . "$HOME/.claude-tmux.sh"'
    echo "$end"
  } >> "$rc"
  echo "updated $rc"
done

if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  echo "warning: ANTHROPIC_API_KEY is set. Remote Control needs claude.ai subscription auth; unset it." >&2
fi
echo
echo "Done. Open a new terminal, cd into a project and type: cc"
