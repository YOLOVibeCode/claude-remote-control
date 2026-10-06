# claude-remote-control

Type `cc` on your Mac. Claude Code starts in a tmux session named after the folder, with
**Remote Control** on, so you can pick up the same session in the **Claude app** on your
phone, at **claude.ai/code**, or over **SSH**.

![How the pieces fit](docs/images/00-architecture.png)

**Read the step-by-step guide, with screenshots: [ARTICLE.md](ARTICLE.md)**

## Let your AI set it up

Paste this into a terminal on your Mac:

```bash
claude "Set me up with https://github.com/YOLOVibeCode/claude-remote-control — follow the 'For AI agents' steps in its README."
```

Or paste the quoted sentence into any coding agent (Cursor, Codex, Copilot…).

### For AI agents

If a user pointed you here, do this, in order:

1. Check prerequisites. Run `claude --version` (install Claude Code if it's missing) and `tmux -V` (`brew install tmux` if it's missing). Remote Control needs a claude.ai subscription login, so warn the user if `ANTHROPIC_API_KEY` is set.
2. Clone the repo: `git clone https://github.com/YOLOVibeCode/claude-remote-control.git ~/claude-remote-control`
3. **Ask the user** whether `cc` should skip permission prompts (`./install.sh`) or keep them (`./install.sh --safe`). Explain that skipping prompts plus Remote Control lets their phone run commands on this Mac without asking.
4. Run the installer they chose from inside the clone. It backs up `~/.claude-tmux.sh` and never duplicates lines in `~/.zshrc`.
5. Verify with `bash tests/run.sh`. It should end with `0 failed`.
6. Tell the user to open a **new** terminal, `cd` into a project, type `cc`, and then open the Claude app → **Code** to find the session named after that folder.

## Install

Requires macOS (or Linux), [Claude Code](https://code.claude.com/docs/en/setup) signed in with
a Pro, Max, Team or Enterprise plan, and `tmux` (`brew install tmux`).

```bash
git clone https://github.com/YOLOVibeCode/claude-remote-control.git
cd claude-remote-control
./install.sh            # cc = claude --dangerously-skip-permissions
./install.sh --safe     # cc = plain claude, permission prompts kept
./install.sh --uninstall
```

Open a new terminal, `cd` into a project and type `cc`.

## What's inside

| File | What it does |
| --- | --- |
| [`shell/claude-tmux.sh`](shell/claude-tmux.sh) | A `claude()` wrapper that runs interactive sessions in tmux with `--remote-control <folder>`. Also `cpick` (a session menu on SSH login) and `ccserve` (an always-on `claude remote-control` server, so the app can start new sessions) |
| [`shell/tmux.conf`](shell/tmux.conf) | Six tmux settings: mouse, scrollback, fast Esc, tab titles, and drag-to-copy into the macOS clipboard |
| [`install.sh`](install.sh) | Idempotent installer with backups, `--safe`, `--supervise` and `--uninstall` |
| [`claude_rc/`](claude_rc/), [`bin/claude-rc`](bin/claude-rc) | Pins each tmux session to its conversation, restarts dead ones, alerts and reports (see Supervision) |
| [`launchd/`](launchd/) | Templates for the 10-minute watchdog and the daily report |
| [`tests/`](tests/) | `run.sh`: wrapper and installer tests (bash + zsh, stub `claude`/`tmux`), then the Python suite |
| [`tools/screenshots/`](tools/screenshots/) | How the terminal screenshots were captured, with IDs masked |

## Commands

| Command | Effect |
| --- | --- |
| `cc` / `ccc` | New session or continue the last one. Runs in tmux with Remote Control on |
| `/remote-control` | Inside a session: show the URL and QR code, or reconnect |
| `ccserve` | Start the Remote Control server in tmux session `remote` (`ccserve restart`) |
| `cpick` | Pick a tmux session to attach to (runs on its own at SSH login; `NOMENU=1` skips it) |

## Supervision: survive outages and reboots

A restart that only knows a session's *name* opens a blank conversation. `claude-rc` pins every
tmux session to its conversation id and brings it back with `--resume <id>`, so a crash or a
reboot returns the same chat, in the same folder, under the same Remote Control name.

```bash
./install.sh --supervise        # installs claude-rc, schedules the watchdog + daily report, adopts running sessions
```

| Command | Effect |
| --- | --- |
| `claude-rc adopt` | Record every running tmux session in `~/.config/claude-rc/sessions.json` (never overwrites an entry) |
| `claude-rc check` | Each pinned session's state; exit 1 if any needs you (`--json` for scripts) |
| `claude-rc watch` | What launchd runs every 10 min and at login: restart, relink, alert on changes |
| `claude-rc up [name]` | Start pinned sessions that are down, by hand |
| `claude-rc report` | Email the all-clear now |
| `claude-rc notify-test alerts\|report` | Send one test message through the configured provider |

New sessions are pinned at birth: the `claude()` wrapper passes `--session-id <uuid>` and records
it. Each manifest line holds `name`, `dir`, `conversation`, `flags` (reused on restart; seeded as
`--dangerously-skip-permissions`) and `enabled` (set `false` to leave a session alone).

**States.** `OK` alive on its conversation and linked. `DEAD` no tmux session: restarted (3 an
hour at most, then `GAVE_UP`). `UNLINKED` Remote Control dropped: when the session is idle for
two runs, `/remote-control` is typed into it once (`UNLINKED_STUCK` if that did not work).
`WRONG_CONVERSATION` and `NOT_RUNNING` are reported and never touched. `RESTART_FAILED` means a
restart never registered; the pane's last lines go to `~/.config/claude-rc/watch.log`.

**Alerts and the daily report** go through pluggable providers, chosen per channel in
`~/.config/claude-rc/config.json` (copy [`config.example.json`](config.example.json)):

| Provider | Alerts (SMS) | Report (email) |
| --- | --- | --- |
| `noctusoft-relay` | ✓ | ✓ |
| `twilio` | ✓ | |
| `smtp` (any mailbox) | | ✓ |
| `sendgrid` | | ✓ |
| `console` (dry run) | ✓ | ✓ |

`env` in the config names environment variables; their values live in
`~/.config/claude-rc/secrets.env` (chmod 600), for example filled from 1Password without printing:

```bash
printf 'NOCTUSOFT_API_KEY=%s\n' "$(op read 'op://<vault>/<item-id>/credential')" > ~/.config/claude-rc/secrets.env
chmod 600 ~/.config/claude-rc/secrets.env
claude-rc notify-test alerts
```

One SMS per change (restarted, needs you, back), never one per run; undelivered alerts are
retried next run. Adding a vendor is one class in `claude_rc/notify/` plus one line in its
registry; `tests/test_providers.py` checks it against the same contract as the others.

**Reboots.** launchd user agents start at login. With FileVault on, a reboot waits for someone to
log in; for unattended reboots, turn on automatic login (System Settings → Users & Groups) only if
that trade-off is acceptable for this Mac.

## Safety

`--dangerously-skip-permissions` plus Remote Control means a device signed in to your Claude
account can run commands on your Mac without a prompt. Turn on **Require trusted devices** in
claude.ai settings, only trust project folders, and keep secrets out of prompts and command
lines. Or install with `--safe`. More in [ARTICLE.md → Safety](ARTICLE.md#safety-read-this-before-you-copy-the-alias).

## Test

```bash
bash tests/run.sh
```

MIT licensed.
