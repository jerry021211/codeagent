"""Conservative discussion-mode tool policy, independent of permission approval.

This is a command policy, not an OS sandbox. Only literal, single commands with
known read-only options are accepted; shell programs and opaque tools fail closed.
"""

from __future__ import annotations

import re
import shlex

from codeagent.messages import ToolUse

READ_ONLY_TOOLS = frozenset({
    "read_file", "glob", "grep", "load_skill", "search_memory", "load_memory",
    "load_tool_output", "load_context_history", "compact", "TaskGet", "TaskList", "ask_user",
})

# No shell expressions, redirections, pipelines, substitutions or environment
# expansion, even inside quotes. Prefer native read/search tools for such input.
_SHELL_SYNTAX = re.compile(r"[\x00-\x1f\x7f;$`&|<>(){}%^!#@]")
_OPTIONS = {
    "cat": {"-n", "-b", "-s", "--"},
    "head": {"-n", "-c", "--"},
    "tail": {"-n", "-c", "--"},
    "ls": {"-a", "-l", "-la", "-al", "-h", "-lh", "-lah", "-R", "--"},
    "pwd": {"-L", "-P"},
    "wc": {"-l", "-w", "-c", "--"},
    "grep": {"-n", "-i", "-r", "-R", "-l", "-c", "-v", "-F", "-E", "-e", "--"},
    "rg": {"--files", "--hidden", "--no-ignore", "--glob", "-g", "-n", "-i",
           "-l", "-c", "-F", "-e", "-A", "-B", "-C", "--heading", "--"},
    "get-content": {"-path", "-literalpath", "-raw", "-encoding", "-totalcount", "-tail"},
    "get-childitem": {"-path", "-literalpath", "-name", "-force", "-recurse", "-file", "-directory", "-filter", "-depth"},
    "get-location": set(),
    "test-path": {"-path", "-literalpath", "-pathtype"},
    "select-string": {"-path", "-literalpath", "-pattern", "-simplematch", "-casesensitive", "-list", "-context"},
}
_GIT_OPTIONS = {
    "status": {"--short", "-s", "--branch", "-b", "--porcelain", "--porcelain=v1", "--porcelain=v2", "--untracked-files=no"},
    "diff": {"--stat", "--name-only", "--name-status", "--cached", "--staged", "--no-ext-diff", "--no-textconv", "--", "--numstat"},
    "log": {"--oneline", "--all", "--graph", "--decorate", "-n", "--no-ext-diff", "--no-textconv", "--"},
    "show": {"--stat", "--name-only", "--no-ext-diff", "--no-textconv", "--"},
    "ls-files": {"--", "--cached", "--others", "--exclude-standard"},
    "ls-tree": {"-r", "--name-only", "--"},
    "rev-parse": {"--show-toplevel", "--show-prefix", "--git-dir", "--is-inside-work-tree", "--verify", "--abbrev-ref"},
}


def is_safe_discuss_command(command: object) -> bool:
    if not isinstance(command, str) or not command.strip() or _SHELL_SYNTAX.search(command):
        return False
    try:
        # posix=False preserves Windows path backslashes. Reject unbalanced or
        # embedded quoting instead of attempting to interpret multiple shells.
        raw = shlex.split(command, posix=False)
        tokens = []
        for token in raw:
            if token[:1] in {"'", '"'} and token[-1:] == token[:1]:
                token = token[1:-1]
            if "'" in token or '"' in token or token.startswith("\\"):
                return False
            tokens.append(token)
    except ValueError:
        return False
    if not tokens:
        return False
    name, *args = tokens
    name = name.lower()
    if name in {"git", "git.exe"}:
        if args[:1] == ["--no-pager"]:
            args = args[1:]
        if not args:
            return False
        subcommand, *args = args
        if subcommand == "branch":
            return args in ([], ["--list"], ["--show-current"], ["-a"], ["-r"])
        if subcommand not in _GIT_OPTIONS:
            return False
        # Repository config can launch external diff/textconv helpers.
        options = args[:args.index("--")] if "--" in args else args
        if subcommand in {"diff", "show", "log"} and not {
            "--no-ext-diff", "--no-textconv"
        }.issubset(options):
            return False
        return _safe_options(args, _GIT_OPTIONS[subcommand])
    if name in {"python", "python3", "node"}:
        return args == ["--version"]
    allowed = _OPTIONS.get(name)
    if allowed is None:
        return False
    if "-" in name:
        args = [arg.lower() for arg in args]
    return _safe_options(args, allowed)


def _safe_options(args: list[str], allowed: set[str]) -> bool:
    return all(not arg.startswith("-") or arg in allowed for arg in args)


def discuss_tool_guard(tool_use: ToolUse) -> str | None:
    if tool_use.name in READ_ONLY_TOOLS:
        return None
    if tool_use.name == "bash" and is_safe_discuss_command(tool_use.input.get("command")):
        return None
    return (
        f"Blocked: Discuss mode cannot execute {tool_use.name}. "
        "Only read-only tools and literal allowlisted commands are available. "
        "Explain proposed changes; the user must exit discuss mode before execution."
    )
