"""Finds the names you use for each Claude account (`claude-b`, `claude-rudra`, ...) by reading shell
startup files: bash/zsh aliases and functions on Linux, PowerShell functions on Windows. Only the
CLAUDE_CONFIG_DIR each name sets is extracted; nothing is executed."""
from __future__ import annotations

import os
import re

SHELL_FILES = ["~/.bashrc", "~/.bash_aliases", "~/.bash_profile", "~/.profile", "~/.zshrc", "~/.zprofile"]
PS_FILES = ["~/Documents/WindowsPowerShell/Microsoft.PowerShell_profile.ps1",
            "~/Documents/WindowsPowerShell/profile.ps1",
            "~/Documents/PowerShell/Microsoft.PowerShell_profile.ps1",
            "~/Documents/PowerShell/profile.ps1"]

_ALIAS = re.compile(r"""^\s*alias\s+([\w.+-]+)=(?:'([^']*)'|"([^"]*)"|(\S+))""")
_FUNC = re.compile(r"""^\s*(?:function\s+([\w.+-]+)\s*(?:\(\s*\))?|([\w.+-]+)\s*\(\s*\))\s*\{?""")
_SH_ASSIGN = re.compile(r"""CLAUDE_CONFIG_DIR=(?:"([^"]*)"|'([^']*)'|([^\s;'"]+))""")
_PS_ASSIGN = re.compile(r"""\$env:CLAUDE_CONFIG_DIR\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s;]+))""", re.I)
_CALLS_CLAUDE = re.compile(r"""(?:^|[\s;&|(])claude(?:\.exe)?(?:\s|$|;)""")


def _expand(raw: str) -> str:
    home = os.path.expanduser("~")
    s = raw.strip()
    for pat in (r"\$\{HOME\}", r"\$HOME\b", r"\$env:USERPROFILE\b", r"\$env:HOME\b", r"%USERPROFILE%"):
        s = re.sub(pat, lambda _m: home, s, flags=re.I)
    if s.startswith("~"):
        s = home + s[1:]
    if os.sep == "/":
        s = s.replace("\\", "/")  # a PowerShell-style path read on Linux
    return os.path.normpath(s)


def _first(m) -> str:
    return next(g for g in m.groups() if g is not None)


def parse_text(text: str, default_dir: str) -> dict[str, str]:
    """-> {name: config_dir} for every alias / function that runs claude."""
    out: dict[str, str] = {}
    func, depth, opened, func_dir, func_calls = None, 0, False, None, False
    for code in text.splitlines():
        if code.lstrip().startswith("#"):
            continue
        m = _ALIAS.match(code)
        if m:
            name, value = m.group(1), next(g for g in m.groups()[1:] if g is not None)
            a = _SH_ASSIGN.search(value)
            if a:
                out[name] = _expand(_first(a))
            elif _CALLS_CLAUDE.search(" " + value) and name != "claude":
                out[name] = default_dir
            continue
        fm = _FUNC.match(code)
        if fm and depth == 0:
            func, opened, func_dir, func_calls = fm.group(1) or fm.group(2), False, None, False
        if func:
            a = _PS_ASSIGN.search(code) or _SH_ASSIGN.search(code)
            # first real path wins; later "restore the previous value" lines ($prev, "") are ignored
            if a and func_dir is None and not re.fullmatch(r"\$[\w:{}]*|", _first(a).strip()):
                func_dir = _expand(_first(a))
            body = code[fm.end():] if fm else code  # don't count the function's own name
            if _CALLS_CLAUDE.search(" " + body.replace("@args", "").replace('"$@"', "")):
                func_calls = True
            opened = opened or "{" in code
            depth += code.count("{") - code.count("}")
            if opened and depth <= 0:  # function body closed
                if func_calls and func != "claude":
                    out[func] = func_dir or default_dir
                func, depth = None, 0
    return out


def discover(default_dir: str, files: list[str] | None = None) -> dict[str, list[str]]:
    """-> {normalized config dir: [names]}; the default dir always includes "claude"."""
    by_dir: dict[str, list[str]] = {os.path.normcase(default_dir): ["claude"]}
    for f in files if files is not None else SHELL_FILES + PS_FILES:
        p = os.path.expanduser(f)
        try:
            with open(p, encoding="utf-8-sig", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        for name, d in parse_text(text, default_dir).items():
            names = by_dir.setdefault(os.path.normcase(d), [])
            if name not in names:
                names.append(name)
    return by_dir
