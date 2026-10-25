import os
import tempfile
import unittest

from cmgr.aliases import discover, parse_text

HOME = os.path.expanduser("~")
DEFAULT = os.path.join(HOME, ".claude")
J = lambda *p: os.path.normpath(os.path.join(HOME, *p))


class AliasTests(unittest.TestCase):
    def test_bash_aliases(self):
        text = """
# alias claude-old='CLAUDE_CONFIG_DIR=~/.claude-old claude'
alias claude-b='CLAUDE_CONFIG_DIR=~/.claude-b claude'
alias claude-w="CLAUDE_CONFIG_DIR=$HOME/accounts/work claude --model opus"
alias claude-rudra='claude'
alias ll='ls -la'
alias cc=claude
"""
        self.assertEqual(parse_text(text, DEFAULT), {
            "claude-b": J(".claude-b"), "claude-w": J("accounts", "work"),
            "claude-rudra": DEFAULT, "cc": DEFAULT})

    def test_bash_functions(self):
        text = """
claude-b() { CLAUDE_CONFIG_DIR="$HOME/.claude-b" claude "$@"; }
function claude-x {
    export CLAUDE_CONFIG_DIR=${HOME}/.claude-x
    claude "$@"
}
mkcd() {
    mkdir -p "$1" && cd "$1"
}
"""
        self.assertEqual(parse_text(text, DEFAULT), {"claude-b": J(".claude-b"), "claude-x": J(".claude-x")})

    def test_powershell_function(self):  # the exact shape used on the Windows PC
        text = r"""function claude-b {
    $prev = $env:CLAUDE_CONFIG_DIR
    $env:CLAUDE_CONFIG_DIR = "$env:USERPROFILE\.claude-b"
    try { claude @args } finally { $env:CLAUDE_CONFIG_DIR = $prev }
}
function Get-Thing { Write-Output 1 }
"""
        self.assertEqual(parse_text(text, DEFAULT), {"claude-b": J(".claude-b")})

    def test_discover_groups_names_by_dir(self):
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "rc")
            with open(f, "w") as fh:
                fh.write("alias claude-rudra=claude\nalias claude-b='CLAUDE_CONFIG_DIR=~/.claude-b claude'\n")
            got = discover(DEFAULT, [f])
            self.assertEqual(got[os.path.normcase(DEFAULT)], ["claude", "claude-rudra"])
            self.assertEqual(got[os.path.normcase(J(".claude-b"))], ["claude-b"])


if __name__ == "__main__":
    unittest.main()
