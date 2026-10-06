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
cat > "$work/bin/claude-rc" <<'EOF'
#!/bin/sh
echo "claude-rc $*" >> "$LOG"
case "$1" in
  has) for r in $RESERVED; do [ "$r" = "$2" ] && exit 0; done; exit 1 ;;
esac
exit 0
EOF
cat > "$work/bin/uuidgen" <<'EOF'
#!/bin/sh
echo "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
EOF
cat > "$work/bin/launchctl" <<'EOF'
#!/bin/sh
echo "launchctl $*" >> "$LOG"
exit 0
EOF
chmod +x "$work/bin/tmux" "$work/bin/claude" "$work/bin/claude-rc" "$work/bin/uuidgen" "$work/bin/launchctl"

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

export LOG="$work/log" EXIST="" RESERVED=""
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
  check  "...with Remote Control on under the same name"            'claude --remote-control my-app --session-id aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee$'
  check  "a new session is pinned to a fresh conversation id"       'claude-rc pin my-app .*/my-app aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee --$'

  run_case "$sh" "$work/my-app" --dangerously-skip-permissions
  check  "cc's flag is passed through after the pin" 'claude --remote-control my-app --session-id aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee --dangerously-skip-permissions$'
  check  "...and recorded with the pin"             'claude-rc pin my-app .*/my-app aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee -- --dangerously-skip-permissions$'

  EXIST="my-app" run_case "$sh" "$work/my-app"
  check  "a taken name gets a -2 suffix" 'new-session -s my-app-2 .*--remote-control my-app-2 '

  EXIST="my-app my-app-2" run_case "$sh" "$work/my-app"
  check  "...and -3 after that" 'new-session -s my-app-3 '

  run_case "$sh" "$work/my-app" --remote-control mine
  check  "an explicit --remote-control is kept" 'new-session -s my-app .* claude --session-id aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee --remote-control mine$'
  refute "...and not doubled"                   'remote-control my-app'

  run_case "$sh" "$work/weird name!"
  check  "odd folder names become safe session names" 'new-session -s weird-name- '

  RESERVED="my-app" run_case "$sh" "$work/my-app"
  check  "a name pinned in the manifest counts as taken" 'new-session -s my-app-2 '
  refute "...so its pinned conversation is not overwritten" 'claude-rc pin my-app '

  run_case "$sh" "$work/my-app" --resume 1234
  check  "--resume is not pinned again"   'claude --remote-control my-app --resume 1234$'
  refute "...and nothing is recorded"     'claude-rc pin'

  run_case "$sh" "$work/my-app" -c
  refute "-c is not pinned"               'session-id'

  run_case "$sh" "$work/my-app" --session-id 9999
  check  "an explicit --session-id is kept as given" 'claude --remote-control my-app --session-id 9999$'

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

[ -x "$H/.local/bin/claude-rc" ] && ok "installs claude-rc" || bad "installs claude-rc"
[ -f "$H/.local/share/claude-rc/claude_rc/core.py" ] && ok "...with its package" || bad "...with its package"
[ -f "$H/Library/LaunchAgents/com.noctusoft.claude-rc.watch.plist" ] && bad "plain install does not schedule the watchdog" || ok "plain install does not schedule the watchdog"

: > "$LOG"
inst --supervise
W="$H/Library/LaunchAgents/com.noctusoft.claude-rc.watch.plist"
R="$H/Library/LaunchAgents/com.noctusoft.claude-rc.report.plist"
[ -f "$W" ] && ok "--supervise writes the watch agent" || bad "--supervise writes the watch agent"
[ -f "$R" ] && ok "--supervise writes the report agent" || bad "--supervise writes the report agent"
grep -q '<integer>600</integer>' "$W" 2>/dev/null && ok "watch runs every 10 minutes" || bad "watch runs every 10 minutes"
grep -q '<key>RunAtLoad</key>' "$W" 2>/dev/null && ok "watch runs at login (after a reboot)" || bad "watch runs at login (after a reboot)"
grep -q "$H/.local/bin/claude-rc" "$W" 2>/dev/null && ok "watch calls the installed claude-rc" || bad "watch calls the installed claude-rc"
grep -q '__' "$W" "$R" 2>/dev/null && bad "no template placeholders left" || ok "no template placeholders left"
plutil -lint "$W" "$R" >/dev/null 2>&1 && ok "plists are valid" || { command -v plutil >/dev/null || ok "plists are valid (plutil n/a)"; command -v plutil >/dev/null && bad "plists are valid"; }
check "--supervise loads both agents" 'launchctl bootstrap gui/[0-9]+ .*claude-rc.watch.plist'

: > "$LOG"
inst --uninstall
check "--uninstall unloads the agents" 'launchctl bootout gui/[0-9]+/com.noctusoft.claude-rc.watch'
[ -f "$W" ] && bad "--uninstall removes the agents" || ok "--uninstall removes the agents"

printf 'alias cc="claude --model opus"\n' > "$H/.zshrc"
inst
[ "$(grep -c '^alias cc=' "$H/.zshrc")" = 1 ] && ok "an existing cc alias is left alone" || bad "an existing cc alias is left alone"

echo "claude-rc (python unittest)"
py=/usr/bin/python3; [ -x "$py" ] || py=python3   # launchd runs the system python; test with it
if out=$(cd "$root" && "$py" -m unittest discover -s tests -p 'test_*.py' 2>&1 >/dev/null); then
  ok "$(printf '%s\n' "$out" | grep -E '^Ran ' | head -1)"
else
  bad "python unittest" "$(printf '%s\n' "$out" | tail -15 | tr '\n' '|')"
fi

echo
echo "$pass passed, $fail failed"
[ "$fail" = 0 ]
