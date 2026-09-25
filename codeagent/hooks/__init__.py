"""Hook system extension point."""

from codeagent.hooks.defaults import create_default_hooks
from codeagent.hooks.manager import HookDecision, HookManager

__all__ = ["HookDecision", "HookManager", "create_default_hooks"]
