"""AskStrategy — `Step(requires_user_input=True, ...)` for HITL pauses.

: renamed from `HumanInputStrategy`. The node
identity is now `ask`; `kind` is `control_flow` (was `executable`).
Edge semantics unchanged — ask still participates as a dataflow
graph node with the same `Step(requires_user_input=True, ...)`
emission as before.

The `ask` node is a Pause/Resume step:

  - `requires_user_input=True` tells agno to pause this step.
  - `user_input_message` is the prompt shown to the user.
  - `user_input_schema` is the list of fields the user fills in
    (we render it from `cfg.choices` for `choice`-type prompts).

The runtime waits on a `WorkflowPausedEvent`; the EventAdapter turns
it into a `ConfirmationEvent` (`kind="ask"` per N5). The frontend
posts the user's response back to `Wf.continue_run(response=...)` —
no custom resume logic.

Note: agno 2.8.7's `Step.__init__` validates that exactly one executor
(`agent`/`team`/`executor`/`workflow`) is provided even when
`requires_user_input=True`. The executor itself never runs because
the step pauses before `_set_active_executor` dispatches; we still
have to satisfy the constructor check, so we pass an `executor=`
callable that returns the user's input verbatim from the resolved
`StepInput.user_input`.
"""
from __future__ import annotations

from typing import Any, ClassVar, Literal, Optional

from .base import NodeStrategy

def _echo_user_input(step_input) -> Any:
    """Executor stub for a human-input step.

    agno pauses the step BEFORE this executor runs (the
    `WorkflowPausedEvent` is what stops execution). When the user
    resumes via `Wf.continue_run(...)`, the resolved user input
    lands on `step_input.user_input` — we just echo it back so the
    step produces a non-empty `StepOutput.content`.
    """
    ui = getattr(step_input, "user_input", None) or {}
    if isinstance(ui, dict) and ui:
        # If the schema has a single field, surface just its value
        # so downstream text-rendering sees a clean string.
        if len(ui) == 1:
            return next(iter(ui.values()))
        return ui
    return ui

def _schema_for_input_type(
    input_type: str,
    choices: Optional[list[str]],
) -> Optional[list[dict]]:
    """Map a human_input's `inputType` onto agno's `user_input_schema`.

    The field name carries the semantic intent:
      - `text`    → `response`     (free-form text)
      - `confirm` → `confirmation` (bool)
      - `choice`  → `selection`    (one of `choices`)

    Multiple-choice prompts append the `options` list so agno's UI
    renders the right widget.
    """
    if input_type == "choice":
        return [
            {
                "name": "selection",
                "field_type": "str",
                "description": "Choose one of the options",
                **({"options": choices} if choices else {}),
            }
        ]
    if input_type == "confirm":
        return [
            {
                "name": "confirmation",
                "field_type": "bool",
                "description": "Confirm (yes/no)",
            }
        ]
    return [
        {
            "name": "response",
            "field_type": "str",
            "description": "The user's answer",
        }
    ]

class AskStrategy(NodeStrategy):
    """`Step(requires_user_input=True, ...)` for HITL pauses.

    : renamed from `HumanInputStrategy`. The
    KIND changes to `control_flow` (was `executable`); STEP_WRAPPER
    literal becomes `"ask"` (was `"human_input"`). agno emission
    is unchanged.
    """

    KIND: ClassVar[Literal["executable", "compound", "tool_source", "control_flow"]] = "control_flow"
    COMPOUND_PASS: ClassVar[Optional[int]] = None
    IS_TOOL_SOURCE: ClassVar[bool] = False
    NEEDS_TOOL_WIRING: ClassVar[bool] = False
    STEP_WRAPPER: ClassVar[Literal["agent", "ask", "none"]] = "ask"

    def build(self, nid: str, node: dict, ctx: Any) -> Any:
        """Build a `Step(requires_user_input=True, ...)` for an ask node."""
        from agno.workflow import Step

        cfg = node["data"].get("config") or {}
        label = node["data"].get("label") or nid
        prompt = cfg.get("prompt") or "Please provide input"
        input_type = (cfg.get("inputType") or "text").lower()
        choices = cfg.get("choices") or None
        schema = _schema_for_input_type(input_type, choices)
        return Step(
            name=label,
            step_id=nid,
            executor=_echo_user_input,
            requires_user_input=True,
            user_input_message=prompt,
            user_input_schema=schema,
            on_error="skip",
        )

    def to_source(self, nid: str, node: dict, ctx: Any) -> str:
        """Emit `<nid>_step = Step(requires_user_input=True, ...)`."""
        from app.core.compile._helpers.utils import q

        cfg = node["data"].get("config") or {}
        label = node["data"].get("label") or nid
        label_repr = q(label)
        prompt = cfg.get("prompt") or "Please provide input"
        input_type = (cfg.get("inputType") or "text").lower()
        choices = cfg.get("choices") or None
        prompt_repr = q(prompt)
        if input_type == "choice":
            opts_repr = "[" + ", ".join(q(c) for c in choices) + "]"
            schema_repr = (
                "[{'name': 'selection', 'field_type': 'str', "
                "'description': 'Choose one of the options', 'options': " + opts_repr + "}]"
            )
        elif input_type == "confirm":
            schema_repr = (
                "[{'name': 'confirmation', 'field_type': 'bool', "
                "'description': 'Confirm (yes/no)'}]"
            )
        else:
            schema_repr = (
                "[{'name': 'response', 'field_type': 'str', 'description': 'The user\\'s answer'}]"
            )
        return (
            f"def {nid}_echo_user_input(step_input):\n"
            f"    ui = getattr(step_input, 'user_input', None) or {{}}\n"
            f"    if isinstance(ui, dict) and ui:\n"
            f"        if len(ui) == 1:\n"
            f"            return next(iter(ui.values()))\n"
            f"        return ui\n"
            f"    return ui\n"
            f"{nid}_step = Step(\n"
            f"    name={label_repr},\n"
            f"    step_id={q(nid)},\n"
            f"    executor={nid}_echo_user_input,\n"
            f"    requires_user_input=True,\n"
            f"    user_input_message={prompt_repr},\n"
            f"    user_input_schema={schema_repr},\n"
            f"    on_error='skip',\n"
            f")\n"
        )

__all__ = ["AskStrategy"]