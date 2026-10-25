# claude-manager

One web page (works on your phone) to drive every Claude Code session you have running in
tmux / psmux on every machine, under every account. A side panel shows the 5-hour and weekly
limits for each account and when they reset. A DECISIONS.md picker lets you put the latest N
entries into a prompt or copy them for the project's Perplexity thread.

**No extra tokens.** The manager never calls a model. It reads the screens of your tmux panes,
types into them, reads files Claude Code already keeps on disk, and asks Anthropic's usage
endpoint (the same one `/usage` uses) for your limits. The only tokens spent are the prompts you send.

## Setup (per machine, no configuration)

```bash
git clone https://github.com/rudra-8000/claude-manager && cd claude-manager
pipx install -e .        # Ubuntu (24.04 blocks plain pip into the system Python)
pip install -e .         # Windows
cm up                    # starts it in a detached tmux/psmux session called "cm" and prints its address
```

Do that on every machine, then open any machine's address (for example
`http://computenode-01.<tailnet>.ts.net:8765`) on your phone or laptop with Tailscale on. Every page
shows all machines, so it doesn't matter which one you open. Add it to your phone's home screen.

- **Other machines** are found through Tailscale: every online computer of *your* Tailscale user that runs
  `cm` shows up. Machines shared into your tailnet by other people are ignored.
- **Who can connect:** only your own Tailscale devices, checked with `tailscale whois`. No token is needed.
- **Accounts** are found automatically: `~/.claude`, `~/.claude-*`, and any config dir your shell aliases or
  functions point at. Your alias names (`claude-b`, `claude-rudra`) become the labels.
- **Projects** are the folders you have used Claude Code in, taken from Claude Code's own history, newest
  first. You don't list them anywhere.

Updating: `git pull && cm up --restart`. Stopping: `cm down`. Checking: `cm doctor` (shows accounts, live usage,
sessions, and which of your other machines are reachable).

If you configured an earlier version with `cm init`, delete `~/.claude-manager/config.json`; it isn't needed
any more, and the example peers (`labpc`, `winbox`) in it aren't real.

## What it does

- **Sessions list:** every Claude Code pane on every machine, with its status (needs you / working /
  idle), project, account, tmux target, and the last line of output. "Needs you" means a permission
  prompt, a question or a "trust this folder?" dialog is on screen. Alerts fire when a session starts
  waiting for you or finishes.
- **Session view:** a live, colour copy of the pane (fit-to-width or wrapped for phones) and a prompt box.
  Multi-line prompts are pasted as one block, so they don't submit at the first newline. "Paste only" puts
  the text in Claude's input without pressing Enter. A key bar has Esc, Enter, arrows, Tab, Shift+Tab, 1/2/3,
  Backspace and Ctrl+C. If Remote Control is on for that session, a ☁ button opens it in the Claude app.
- **New session:** pick a machine, an account (by alias) and a folder (from your Claude history or typed). It
  starts a detached tmux session with a normal shell, so the pane survives `/exit`, and runs `claude` (optionally
  `--continue`) with the right `CLAUDE_CONFIG_DIR`.
- **Usage panel** (collapsible): per account, the 5-hour and weekly bars with reset times. The same login
  on several machines, even under different alias names, is shown once.
- **DECISIONS.md:** found at or above the session's folder (`DECISIONS.md` or `decisions.md`). Entries are
  the `## ` headings, newest first, showing id, date, short title, tags and a ≈token estimate. You can pick
  "latest N" or tick entries one by one, read any entry in full, then **Insert** it into the prompt, **Copy**
  it, or **Copy + Perplexity**, which copies it and opens that project's saved thread.

Your sessions are not changed in any way: they stay ordinary interactive `claude` in tmux, and you can
still `ssh` + `tmux attach` the old way. Nothing is installed into Claude Code's settings.

## Why not just Claude Code's Remote Control?

Remote Control (`/remote-control`, then claude.ai/code or the Claude app) is great for one account: you
get the native UI for a running session. It doesn't cover this setup:

- It shows only the sessions of the account you're signed into, so with several accounts you'd keep
  switching accounts in the app to find a session.
- It has no view of all accounts' 5-hour and weekly limits side by side, and no DECISIONS / Perplexity flow.
- Each session has to be started with Remote Control on, and there is no public API to list or drive
  sessions across accounts, so this tool can't use Remote Control as its transport without depending on
  private endpoints.

The two work together: when a session has Remote Control on, the manager shows a ☁ link that opens it in
the Claude app (signed into that account).

## Optional config (`~/.claude-manager/config.json`)

Everything has a default; create the file only to change something (`cm init` writes one).

| key | default | meaning |
|---|---|---|
| `port` | 8765 | must be the same on all machines for discovery (`cm up --port N` also works) |
| `name` | hostname | label in the UI |
| `bind` | `["tailscale", "127.0.0.1"]` | if a port is taken on one address (e.g. VS Code on localhost), that address is skipped |
| `trust_local` | Windows/macOS: true, Linux: false | allow connections from the machine itself without a token. Off on Linux because a shared server's other users could connect locally |
| `token` | `""` | optional shared secret, for access from outside your Tailscale user |
| `discover` / `peers` | true / `[]` | Tailscale discovery; `peers` adds machines it can't find (`[{"name": "x", "url": "http://x:8765"}]`) |
| `accounts` | auto | explicit list if an alias isn't recognised: `[{"dir": "~/.claude-work", "label": "work"}]` |
| `project_roots` | `[]` | extra folders whose sub-folders are listed as projects |
| `tmux`, `claude_command` | from PATH, `claude` | |

Perplexity thread URLs are stored per project name in `~/.claude-manager/hub-prefs.json` on the machine whose page
you set them on.

### HTTPS (optional)

Browsers allow the clipboard API and system notifications only on HTTPS. Over plain `http://` copying falls back
to an older method (works in most browsers) and only in-page alerts work. For HTTPS on one machine:
`tailscale serve --bg 8765`, then open `https://<machine>.<tailnet>.ts.net`. On Linux, also set
`"trust_local": true` in that machine's config if you're its only user, because `tailscale serve` connects
from localhost.

## How it works (and what could break)

- **Reading and typing:** `tmux capture-pane -p -e` about once a second for the open session; prompts go through
  `load-buffer` + `paste-buffer -p` (bracketed paste) + `send-keys Enter`. There is no terminal attach, so your
  desktop's pane size never changes.
- **Which account a pane uses:** Claude Code writes `<config dir>/sessions/<pid>.json` with the tmux pane it runs in
  (and the Remote Control session id when that is on). The manager matches those files to `tmux list-panes`.
- **Status:** read from the screen: a numbered choice, "Do you want to…" or "Enter to confirm" means it needs you;
  "esc to interrupt" means working. If Claude Code changes these strings, update `screen_status` in
  `cmgr/claude_state.py`.
- **Usage:** `GET https://api.anthropic.com/api/oauth/usage` with the account's own token, at most every 3 minutes
  per account. This endpoint is undocumented and may change. The manager never refreshes tokens (that could log the
  CLI out). If an account sits unused until its token expires, the panel says so; running Claude Code on that account
  once refreshes it.
- **Security:** only your own Tailscale devices (or the optional token) can use the API, and connections from the
  machine itself are refused on Linux by default. Only files named `DECISIONS.md` / `decisions.md` can be read, and
  only a fixed list of keys can be sent.

## Development

```bash
python -m pytest -q tests      # 41 tests
```

Architecture decisions are recorded in [DECISIONS.md](DECISIONS.md).
