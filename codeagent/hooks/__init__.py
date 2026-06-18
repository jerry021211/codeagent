"""Hook system extension point."""

from codeagent.hooks.defaults import create_default_hooks
from codeagent.hooks.manager import HookManager

__all__ = ["HookManager", "create_default_hooks"]
