#!/usr/bin/env bash
# tests/run.sh — checks the claude() wrapper and the installer with stub `claude` and `tmux`.
# No real Claude session or tmux server is started. Runs on macOS and Linux.
set -uo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
pass=0 fail=0

ok()   { pass=$((pass+1)); echo "  ok    $1"; }
bad()  { fail=$((fail+1)); echo "  FAIL  $1"; [ -n "${2:-}" ] && printf '        %s\n' "$2"; }
check(){ if grep -qE -- "$2" "$LOG" 2>/dev/null; then ok "$1"; else bad "$1" "log: $(tr '\n' '|' < "$LOG" 2>/dev/null)"; fi; }
refute(){ if grep -qE -- "$2" "$LOG" 2>/dev/null; then bad "$1" "log: $(tr '\n' '|' < "$LOG")"; else ok "$1"; fi; }

# --- stubs -------------------------------------------------------------------
mkdir -p "$work/bin"
cat > "$work/bin/tmux" <<'EOF'
#!/bin/sh
echo "tmux $*" >> "$LOG"
case "$1" in
  has-session) want=${3#=}; for e in $EXIST; do [ "$e" = "$want" ] && exit 0; done; exit 1 ;;
esac
exit 0
EOF
cat > "$work/bin/claude" <<'EOF'
#!/bin/sh
echo "claude-direct $*" >> "$LOG"
EOF
chmod +x "$work/bin/tmux" "$work/bin/claude"

# The wrapper only takes over on a real terminal, so run each case on a pseudo-terminal.
# Python's pty module gives the child a tty on stdin/stdout without forwarding our stdin;
# `script` forwards EOF from a non-tty stdin and could end the shell before it ran anything.
in_pty() {  # in_pty <shell> <script>
  python3 - "$1" -c "$2" <<'PY'
import os, pty, subprocess, sys
master, slave = pty.openpty()
child = subprocess.Popen(sys.argv[1:], stdin=slave, stdout=slave, stderr=slave, close_fds=True)
os.close(slave)
while True:  # drain output until the child closes the terminal
    try:
        if not os.read(master, 4096):
            break
    except OSError:
        break
sys.exit(child.wait())
PY
}

run_case() {  # run_case <shell> <dir> <claude args...>
  local sh=$1 dir=$2; shift 2
  : > "$LOG"
  in_pty "$sh" "unset TMUX SSH_CONNECTION; export PATH=\"$work/bin:\$PATH\"; . \"$root/shell/claude-tmux.sh\"; cd \"$dir\" && claude $*"
}

export LOG="$work/log" EXIST=""
mkdir -p "$work/my-app" "$work/weird name!"

shells="bash"
command -v zsh >/dev/null 2>&1 && shells="bash zsh"

echo "syntax"
if command -v tmux >/dev/null 2>&1; then   # the real tmux, before the stubs go on PATH
  sock="rc-test-$$"
  if tmux -L "$sock" -f "$root/shell/tmux.conf" new-session -d "sleep 5" 2>/dev/null; then
    got="mouse=$(tmux -L "$sock" show -gv mouse) copy=$(tmux -L "$sock" show -sv copy-command) clip=$(tmux -L "$sock" show -sv set-clipboard)"
    want="mouse=on copy= clip=external"
    command -v pbcopy >/dev/null 2>&1 && want="mouse=on copy=pbcopy clip=external"
    [ "$got" = "$want" ] && ok "real tmux loads shell/tmux.conf ($want)" || bad "real tmux loads shell/tmux.conf" "got $got, want $want"
    tmux -L "$sock" kill-server 2>/dev/null
  else
    bad "real tmux starts with shell/tmux.conf"
  fi
fi
for sh in $shells; do
  if $sh -n "$root/shell/claude-tmux.sh" 2>/dev/null; then ok "$sh parses claude-tmux.sh"; else bad "$sh parses claude-tmux.sh"; fi
done
if bash -n "$root/install.sh"; then ok "bash parses install.sh"; else bad "bash parses install.sh"; fi

for sh in $shells; do
  echo "wrapper ($sh)"

  run_case "$sh" "$work/my-app"
  check  "plain claude opens a tmux session named after the folder" 'tmux new-session -s my-app -c .*/my-app '
  check  "...with Remote Control on under the same name"            'claude --remote-control my-app$'

  run_case "$sh" "$work/my-app" --dangerously-skip-permissions
  check  "cc's flag is passed through after the name" 'claude --remote-control my-app --dangerously-skip-permissions$'

  EXIST="my-app" run_case "$sh" "$work/my-app"
  check  "a taken name gets a -2 suffix" 'new-session -s my-app-2 .*--remote-control my-app-2$'

  EXIST="my-app my-app-2" run_case "$sh" "$work/my-app"
  check  "...and -3 after that" 'new-session -s my-app-3 '

  run_case "$sh" "$work/my-app" --remote-control mine
  check  "an explicit --remote-control is kept" 'new-session -s my-app .* claude --remote-control mine$'
  refute "...and not doubled"                   'remote-control my-app'

  run_case "$sh" "$work/weird name!"
  check  "odd folder names become safe session names" 'new-session -s weird-name- '

  run_case "$sh" "$work/my-app" -p hello
  check  "print mode bypasses tmux" '^claude-direct -p hello$'
  refute "...no tmux session"       'new-session'

  run_case "$sh" "$work/my-app" mcp list
  check  "subcommands bypass tmux" '^claude-direct mcp list$'

  run_case "$sh" "$work/my-app" --version
  check  "--version bypasses tmux" '^claude-direct --version$'

  : > "$LOG"
  in_pty "$sh" "export TMUX=/tmp/x,1,0 PATH=\"$work/bin:\$PATH\"; . \"$root/shell/claude-tmux.sh\"; cd \"$work/my-app\" && claude"
  check  "inside tmux it runs claude directly" '^claude-direct ?$'

  : > "$LOG"
  $sh -c "unset TMUX SSH_CONNECTION; export PATH=\"$work/bin:\$PATH\"; . \"$root/shell/claude-tmux.sh\"; cd \"$work/my-app\" && claude" </dev/null >/dev/null 2>&1
  check  "without a terminal it runs claude directly" '^claude-direct ?$'
done

echo "installer"
H="$work/home"; mkdir -p "$H"
printf 'export FOO=1\n' > "$H/.zshrc"
inst() { HOME="$H" PATH="$work/bin:$PATH" ANTHROPIC_API_KEY= bash "$root/install.sh" "$@" >/dev/null 2>&1; }

inst
[ -f "$H/.claude-tmux.sh" ] && ok "copies ~/.claude-tmux.sh" || bad "copies ~/.claude-tmux.sh"
grep -q 'alias cc="claude --dangerously-skip-permissions"' "$H/.zshrc" && ok "adds the cc alias" || bad "adds the cc alias"
grep -q 'export FOO=1' "$H/.zshrc" && ok "keeps existing .zshrc content" || bad "keeps existing .zshrc content"
grep -q 'set -g mouse on' "$H/.tmux.conf" && ok "writes tmux settings" || bad "writes tmux settings"
grep -q 'set -s copy-command pbcopy' "$H/.tmux.conf" && ok "writes the copy-to-clipboard line" || bad "writes the copy-to-clipboard line"

inst
n=$(grep -c '>>> claude-remote-control >>>' "$H/.zshrc")
[ "$n" = 1 ] && ok "second run does not duplicate the block" || bad "second run does not duplicate the block" "found $n"
n=$(grep -c 'set -g mouse on' "$H/.tmux.conf")
[ "$n" = 1 ] && ok "second run does not duplicate tmux lines" || bad "second run does not duplicate tmux lines" "found $n"
n=$(grep -c 'copy-command' "$H/.tmux.conf")
[ "$n" = 1 ] && ok "second run does not duplicate the copy line" || bad "second run does not duplicate the copy line" "found $n"

inst --safe
grep -q 'alias cc="claude"' "$H/.zshrc" && ok "--safe drops the bypass flag" || bad "--safe drops the bypass flag"

inst --uninstall
grep -q 'claude-remote-control' "$H/.zshrc" && bad "--uninstall removes the block" || ok "--uninstall removes the block"
grep -q 'export FOO=1' "$H/.zshrc" && ok "--uninstall keeps the rest of .zshrc" || bad "--uninstall keeps the rest of .zshrc"

printf 'alias cc="claude --model opus"\n' > "$H/.zshrc"
inst
[ "$(grep -c '^alias cc=' "$H/.zshrc")" = 1 ] && ok "an existing cc alias is left alone" || bad "an existing cc alias is left alone"

echo
echo "$pass passed, $fail failed"
[ "$fail" = 0 ]
