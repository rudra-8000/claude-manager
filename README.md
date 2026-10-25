# claude-manager

One web page (works on your phone) to drive every Claude Code session you have running in
tmux / psmux on every machine, under every account. A side panel shows the 5-hour and weekly
limits for each account and when they reset. A DECISIONS.md picker lets you put the latest N
entries into a prompt or copy them for the project's Perplexity thread.

**No extra tokens.** The manager never calls a model. It reads the screens of your tmux panes,
types into them, reads files Claude Code already keeps on disk, and asks Anthropic's usage
endpoint (the same one `/usage` uses) for your limits. The only tokens spent are the prompts you send.

```
 phone / laptop browser ──(Tailscale)──► hub machine: cm serve (UI + /hub)
                                             │  fans out to every machine
             ┌───────────────────────────────┼──────────────────────────┐
         cm serve (Linux, tmux)       cm serve (Windows, psmux)      cm serve ...
          └ your tmux sessions         └ your psmux sessions
            running `claude`             running `claude`
```

## What it does

- **Sessions list:** every Claude Code pane on every machine, with its status (needs you / working /
  idle), project, account, tmux target, and the last line of output. "Needs you" means a permission
  prompt, a question or a "trust this folder?" dialog is on screen. Alerts fire when a session starts
  waiting for you or finishes.
- **Session view:** a live, colour copy of the pane (fit-to-width or wrapped for phones) and a prompt box.
  Multi-line prompts are pasted as one block, so they don't submit at the first newline. "Paste only" puts
  the text in Claude's input without pressing Enter. A key bar has Esc, Enter, arrows, Tab, Shift+Tab
  (mode cycle), 1/2/3 (answers to prompts), Backspace and Ctrl+C.
- **New session:** pick a machine, an account and a folder. It starts a detached tmux session with a
  normal shell (so the pane survives `/exit`) and runs `claude` (optionally `--continue`) with the right
  `CLAUDE_CONFIG_DIR`.
- **Usage panel** (collapsible): per account, the 5-hour and weekly bars with reset countdowns.
  Accounts logged in on several machines are shown once.
- **DECISIONS.md:** found at or above the session's folder (`DECISIONS.md` or `decisions.md`). Entries are
  the `## ` headings, newest first, showing id, date, short title, tags and a ≈token estimate. You can pick
  "latest N" or tick entries one by one, read any entry in full, then **Insert** it into the prompt, **Copy**
  it, or **Copy + Perplexity**, which copies it and opens that project's saved thread.
- **Projects page:** folders under `project_roots` on every machine, with their DECISIONS.md and a
  "new session here" button.

Your sessions are not changed in any way: they stay ordinary interactive `claude` in tmux, and you can
still `ssh` + `tmux attach` the old way. Nothing is installed into Claude Code's settings (no hooks).

## Requirements

- Python 3.9+ on each machine (standard library only, nothing to `pip install` beyond this package).
- tmux on Linux, psmux on Windows (its `tmux.exe` is used).
- Tailscale on every machine and on your phone.
- One Claude config dir per account. `~/.claude` is the default account; others live in their own dir and are
  started with `CLAUDE_CONFIG_DIR=<dir> claude` (the manager does this for you when it starts a session).

### Accounts and your aliases

Each machine finds its accounts by itself: `~/.claude`, every `~/.claude-*` dir, and every dir that one of
your shell aliases or functions points `CLAUDE_CONFIG_DIR` at. It reads `~/.bashrc`, `~/.bash_aliases`,
`~/.zshrc`, `~/.profile` and the PowerShell profiles, and only reads them, never runs them. Those names become
the labels in the UI, so you see `claude-b` rather than a path. These shapes are recognised:

```bash
alias claude-b='CLAUDE_CONFIG_DIR=~/.claude-b claude'
alias claude-rudra=claude                     # another name for the default account
claude-b() { CLAUDE_CONFIG_DIR="$HOME/.claude-b" claude "$@"; }
```
```powershell
function claude-b { $env:CLAUDE_CONFIG_DIR = "$env:USERPROFILE\.claude-b"; try { claude @args } finally { ... } }
```

The same login on several machines, even under different names (`claude` on one PC, `claude-rudra` on
another), is recognised by its account id and shown once in the usage panel, labelled with every name it has
(`claude / claude-rudra`). If a name isn't picked up, list the accounts in the config explicitly (see `accounts` below).

## Setup

On **every** machine (clone or copy this folder there first):

```bash
git clone https://github.com/rudra-8000/claude-manager && cd claude-manager
pipx install -e .           # Ubuntu (24.04 blocks plain `pip install` into the system Python)
pip install -e .            # Windows
```

Nothing has to be installed at all: it's standard library only, so `python3 -m cmgr <command>` from the clone
does the same as `cm <command>`.

On the machine that will host the web UI (any always-on one; it is also an agent):

```bash
cm init --project-root ~/projects --peer labpc=http://labpc:8765 --peer winbox=http://winbox:8765
# prints a token
```

On each other machine, use the same token and skip the UI:

```bash
cm init --no-hub --token <token> --project-root ~/projects
```

`labpc` / `winbox` are Tailscale MagicDNS names (or use `100.x.y.z` addresses). Then on each machine:

```bash
cm doctor     # checks tmux, accounts, live usage, panes, Tailscale, peers
cm serve      # listens on the Tailscale IP and 127.0.0.1, port 8765
```

Keep `cm serve` running the way you keep everything else running, in a detached tmux session:

```bash
tmux new -d -s cm 'cm serve'          # Linux
tmux new -d -s cm "cm serve"          # Windows (psmux), from PowerShell
```

On Windows, allow Python through the firewall for private networks the first time it asks. Otherwise
other machines can't reach it.

### Phone

1. On the hub machine run `cm link`. It prints `http://<tailscale-ip>:8765/#token=...`. Send it to yourself
   and open it once on the phone (with Tailscale on). The token is remembered and removed from the address bar.
2. "Add to Home Screen" makes it open like an app.

### HTTPS (recommended)

Browsers only allow the clipboard API and system notifications on HTTPS. Over plain `http://` copying
falls back to an older method (it works in most browsers but not all) and only in-page alerts work. Tailscale
can give the hub a real certificate:

```bash
tailscale serve --bg 8765                 # on the hub machine
cm link --host https://<hub-name>.<your-tailnet>.ts.net
```

## Config (`~/.claude-manager/config.json`)

| key | default | meaning |
|---|---|---|
| `name` | hostname | label in the UI; must be unique per machine |
| `token` | random | shared secret; the same on every machine |
| `port` | 8765 | |
| `bind` | `["tailscale", "127.0.0.1"]` | `"tailscale"` = this machine's Tailscale IPv4 |
| `hub` | true | serve the UI and aggregate `peers` |
| `peers` | `[]` | `[{"name": "labpc", "url": "http://labpc:8765"}]` (optional `"token"` per peer) |
| `accounts` | `[]` (auto) | explicit list: `[{"dir": "~/.claude-work", "label": "work"}]` |
| `project_roots` | `[]` | folders whose sub-folders are listed as projects |
| `tmux` | `""` | path to tmux / psmux if not on PATH |
| `claude_command` | `"claude"` | what new sessions run |

Perplexity thread URLs are saved per project name on the hub in `~/.claude-manager/hub-prefs.json`. A project
with the same folder name on two machines shares one thread.

## How it works (and what could break)

- **Reading and typing:** `tmux capture-pane -p -e` about once a second for the open session; prompts go
  through `load-buffer` + `paste-buffer -p` (bracketed paste) + `send-keys Enter`. There is no terminal
  attach, so your desktop's pane size never changes.
- **Which account a pane uses:** Claude Code writes `<config dir>/sessions/<pid>.json` with the tmux pane it
  runs in. The manager matches those files to `tmux list-panes`. Sessions started before a Claude Code version
  that writes this field will show without an account.
- **Status:** read from the screen: a numbered choice, "Do you want to…" or "Enter to confirm" means it needs
  you; "esc to interrupt" means working. If Claude Code changes these strings, update `screen_status` in
  `cmgr/claude_state.py`.
- **Usage:** `GET https://api.anthropic.com/api/oauth/usage` with the account's own token, at most every 3
  minutes per account. This endpoint is undocumented and may change. The manager never refreshes tokens
  (that could log the CLI out). If an account has not been used for a while its token expires and the panel
  says so; running Claude Code on that account once refreshes it.
- **Security:** every `/api` and `/hub` call needs the token. By default the server listens only on Tailscale
  and localhost. Only files named `DECISIONS.md` / `decisions.md` can be read, and only a fixed list of keys
  can be sent.

## Development

```bash
python -m pytest -q tests      # 31 tests: DECISIONS parser, aliases, tmux parsing, registry, status, usage, HTTP hub+peer
```

Architecture decisions are recorded in [DECISIONS.md](DECISIONS.md).
