"""Step state extraction pure functions.

Extracted from dcp_optimizer.py: extract_step_state (L4679-4713).
"""

from __future__ import annotations

import json
import logging

from ..state import StepState

logger = logging.getLogger(__name__)


def extract_step_state(message) -> StepState | None:
    """Extract StepState from LLM response message.

    Finds and removes report_step_state from message.tool_calls,
    returns parsed StepState or None.

    Args:
        message: OpenAI ChatCompletion message object with tool_calls attribute.

    Returns:
        StepState if report_step_state found, None otherwise.
    """
    step_state = None
    _report_step_call = None

    if not hasattr(message, 'tool_calls') or not message.tool_calls:
        return None

    remaining_calls = []
    for tc in message.tool_calls:
        if tc.function and tc.function.name == "report_step_state":
            try:
                args = json.loads(tc.function.arguments) if tc.function.arguments else {}
            except json.JSONDecodeError:
                args = {}
            step_state = StepState(
                step_id=args.get("step_id"),
                result_status=args.get("result_status"),
                flow_control=args.get("flow_control"),
                has_tool_calls=bool(len(message.tool_calls) > 1),
                raw_content=tc.function.arguments or "",
                strategy_phase=args.get("strategy_phase"),
                strategy_name=args.get("strategy_name"),
            )
            _report_step_call = tc
        else:
            remaining_calls.append(tc)

    if _report_step_call:
        message.tool_calls = remaining_calls if remaining_calls else None

    return step_state
