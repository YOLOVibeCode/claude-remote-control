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
| [`shell/tmux.conf`](shell/tmux.conf) | Five tmux settings: mouse, scrollback, fast Esc, tab titles |
| [`install.sh`](install.sh) | Idempotent installer with backups, `--safe` and `--uninstall` |
| [`tests/run.sh`](tests/run.sh) | Wrapper and installer tests (bash + zsh, stub `claude`/`tmux`) |
| [`tools/screenshots/`](tools/screenshots/) | How the terminal screenshots were captured, with IDs masked |

## Commands

| Command | Effect |
| --- | --- |
| `cc` / `ccc` | New session or continue the last one. Runs in tmux with Remote Control on |
| `/remote-control` | Inside a session: show the URL and QR code, or reconnect |
| `ccserve` | Start the Remote Control server in tmux session `remote` (`ccserve restart`) |
| `cpick` | Pick a tmux session to attach to (runs on its own at SSH login; `NOMENU=1` skips it) |

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
