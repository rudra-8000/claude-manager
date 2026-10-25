# claude-manager

One web page (works on your phone) to drive every Claude Code session you have running in
tmux / psmux on every machine, under every account. A side panel shows the 5-hour and weekly
limits for each account and when they reset. A DECISIONS.md picker lets you put the latest N
entries into a prompt or copy them for the project's Perplexity thread.

**No extra tokens.** The manager never calls a model. It reads the screens of your tmux panes,
types into them, reads files Claude Code already keeps on disk, and asks Anthropic's usage
endpoint (the same one `/usage` uses) for your limits. The only tokens spent are the prompts you send.

## Setup: one machine runs it, the rest are reached over SSH

On the machine that will host the page (an always-on one, e.g. `computenode-01`):

```bash
git clone https://github.com/rudra-8000/claude-manager && cd claude-manager
pipx install -e .        # Ubuntu (24.04 blocks plain pip into the system Python); on Windows: pip install -e .
cm up                    # starts it in a detached tmux/psmux session called "cm" and prints its address
```

Open that address on your phone or laptop with Tailscale on (for example
`http://computenode-01.<tailnet>.ts.net:8765`) and add it to your home screen.

**Your other machines connect by themselves** if the hub machine already reaches them with an alias in its
`~/.ssh/config`: an alias whose `HostName` is the machine's Tailscale name or IP is matched to it (`Host robot` /
`HostName guru` means guru is reached with `ssh robot`). ⚙ *Devices & accounts* lists every computer on your
Tailscale account (phones and machines shared in by other people are left out) and shows how each one is reached:

- **Over SSH (nothing to install there):** the hub opens `ssh <alias>`, streams its own code over that connection and
  runs it in memory. Requirements on the other machine: Python 3.9+ and tmux, which you have wherever you run Claude
  Code in tmux, and key login from the hub without a password. If there's no alias, tap *Set up SSH* and enter what
  works as `ssh <this>` on the hub (`user@host` or an alias), plus the Python to use if it isn't `python3` (on
  Windows the default is `python`). The tmux used is the one running your sessions, even if it's a conda install
  that a non-interactive ssh login doesn't have on PATH.
- **Or run `cm up` on it too:** a machine that runs `cm` is picked up automatically, no SSH needed.

Switch devices on and off to choose whose sessions you see. In the same panel, switch accounts off to hide their
sessions. Usage stays visible on the right either way.

- **Who can connect to the page:** only your own Tailscale devices, checked with `tailscale whois`. No token.
- **Accounts** are found automatically on every machine: `~/.claude`, `~/.claude-*`, and any config dir your shell
  aliases or functions point at. Your alias names (`claude-b`, `claude-rudra`) become the labels.
- **Projects** are the folders you have used Claude Code in, from Claude Code's own history, newest first.

Updating: `git pull && cm up --restart` on the hub. SSH machines get the new code on the next connection. Stopping:
`cm down`. Checking: `cm doctor`.

If you configured an earlier version with `cm init`, delete `~/.claude-manager/config.json`; it isn't needed any
more, and the example peers (`labpc`, `winbox`) in it aren't real.

## What it does

- **Which sessions show up:** your tmux/psmux sessions running Claude, found from Claude Code's own session registry
  (not by scanning folders), titled with the name you gave them. When a tmux session has parked its conversation
  into a Claude Code background job, the tmux pane is a live view of that job, so the page shows the job's chat
  and status for that session. Copies of a tmux session's conversation and
  Claude Code's idle spare processes are hidden. Sessions in a plain terminal outside tmux appear when you tick
  *outside tmux*: they're read-only (full chat, plus *Reply in the Claude app* when Remote Control is on). Start a
  session inside tmux to prompt it from here.
- **Sessions list:** grouped by device. Each session shows its status (needs you / working / idle), project,
  account and last line. "Needs you" means a permission prompt, a question or a "trust this folder?" dialog is
  on screen. Alerts fire when a session starts waiting for you or finishes.
- **Chat tab:** the whole conversation, read from Claude Code's own transcript file. It scrolls, with
  *Load earlier messages* for older history. Claude's replies are rendered as formatted text, and each tool call
  is a line you can tap to see its result (long runs of tool calls are folded). The tmux screen can't give you
  this: Claude Code draws in tmux's alternate screen, so tmux keeps no scrollback.
- **Screen tab:** the live terminal, for menus and anything the chat doesn't show. When Claude is waiting for an
  answer, the chat shows the prompt in a "waiting for you" card, with keys 1/2/3/Esc/Enter right below.
- **Prompt box:** multi-line prompts are pasted as one block, so they don't submit at the first newline. "Paste
  only" puts the text in Claude's input without pressing Enter. If Remote Control is on for the session, ☁ opens
  it in the Claude app.
- **New session:** pick a device, an account (by alias) and a folder (from your Claude history or typed). It starts
  a detached tmux session with a normal shell, so the pane survives `/exit`, and runs `claude` (optionally
  `--continue`) with the right `CLAUDE_CONFIG_DIR`.
- **Usage** (right column, collapsible; a drawer on phones): per account, the 5-hour and weekly bars with reset
  times. The same login on several machines, even under different alias names, is shown once.
- **DECISIONS.md:** found at or above the session's folder (`DECISIONS.md` or `decisions.md`). Entries are the `## `
  headings, newest first, showing id, date, short title, tags and a ≈token estimate. Pick "latest N" or tick entries
  one by one, read any entry in full, then **Insert** it into the prompt, **Copy** it, or **Copy + Perplexity**, which
  copies it and opens that project's saved thread.

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
- **Chat:** `<config dir>/projects/<cwd with non-alphanumerics as '-'>/<sessionId>.jsonl`, the transcript Claude Code
  writes. Only byte ranges are read (the tail, then older chunks on request, then whatever was appended), so even
  a 150 MB transcript loads in ~20 ms. After `/clear` the newest file in that folder is followed.
- **SSH devices:** `ssh -T -o BatchMode=yes <target> python3 -u -c "exec(input())"`; the first stdin line installs
  an in-memory importer, the second carries this package's source, then requests and answers are JSON lines. Two
  connections per device, so a slow refresh never blocks the open session. If ssh fails, its own error message is
  shown on the device.
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
python -m pytest -q tests      # 57 tests
```

Architecture decisions are recorded in [DECISIONS.md](DECISIONS.md).
