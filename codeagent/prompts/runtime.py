"""Runtime entry point for system prompt assembly."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from codeagent.prompts.assembler import PromptAssembler
from codeagent.prompts.loader import PromptTemplateLoader
from codeagent.prompts.models import (
    PromptAssemblyResult,
    PromptBuildContext,
    PromptConfig,
    PromptFragment,
    PromptMode,
)
from codeagent.prompts.providers import PromptProvider, default_prompt_providers


class PromptRuntime:
    """Collect provider fragments and assemble the system prompt."""

    def __init__(
        self,
        *,
        workspace: Path | str,
        config: PromptConfig | None = None,
        providers: list[PromptProvider] | None = None,
    ) -> None:
        self.workspace = Path(workspace)
        self.config = config or PromptConfig()
        self.loader = PromptTemplateLoader(
            workspace=self.workspace,
            template_dir=self.config.template_dir,
        )
        self.providers = providers or default_prompt_providers(self.loader)
        self.assembler = PromptAssembler()

    def assemble(
        self,
        *,
        mode: PromptMode,
        base_system_prompt: str,
        model: str,
        tool_schemas: list[dict],
        selected_memory_context: str = "",
        memory_catalog: str = "",
        skill_catalog: str = "",
        context_summary: str = "",
        extra_reminders: list[str] | None = None,
    ) -> PromptAssemblyResult:
        context = PromptBuildContext(
            mode=mode,
            base_system_prompt=base_system_prompt,
            model=model,
            workspace=self.workspace,
            tool_schemas=tool_schemas,
            selected_memory_context=selected_memory_context,
            memory_catalog=memory_catalog,
            skill_catalog=skill_catalog,
            context_summary=context_summary,
            current_date=date.today().isoformat(),
            extra_reminders=extra_reminders or [],
            config=self.config,
        )
        fragments: list[PromptFragment] = []
        for provider in self.providers:
            fragments.extend(provider.fragments(context))
        return self.assembler.assemble(fragments, context)
