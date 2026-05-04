import json
from litellm import completion
from typing import List, Optional, Dict, Any

from tau_bench.types import SharedState, VerifierResult


VERIFIER_SYSTEM_PROMPT = """You are an expert Verifier Agent in a multi-agent system (Plan-Execute-Verify).
Your job is to evaluate the current state of a task to ensure high quality and correctness.
You will be provided with:
1. The User's Goal.
2. The Shared State (collected slots so far).
3. The Conversation/Action History.
4. The Last Tool Call and its Output (if applicable).

You must check for the following potential errors:
E1: Goal Incomplete / Premature Termination (Is the task truly finished, or are steps missing?)
E4: State Tracking / Memory Failure (Are slots contradicting each other or missing from history?)
E5: Policy / Constraint Violation (e.g., trying to modify an unmodifiable reservation, violating domain rules?)
E7: Tool Outcome Misinterpretation (Did a tool return an error or 'not found' that was ignored?)

Return a JSON object containing:
- "reasoning": A step-by-step chain of thought checking E1, E4, E5, and E7.
- "status": "ok" if no errors are found and the current step/state is completely sound. "issues" if there are any violations or missing steps.
- "feedback": If "status" is "issues", provide explicit diagnostic feedback to the Planner/Executor to repair the error. If "ok", this can be empty or null.

Your response must be valid JSON matching this schema exactly:
{
    "reasoning": "string",
    "status": "ok" | "issues",
    "feedback": "string | null"
}
"""

class VerifierAgent:
    def __init__(
        self,
        model: str,
        provider: str,
        wiki: str,
        temperature: float = 0.0,
    ):
        self.model = model
        self.provider = provider
        self.wiki = wiki
        self.temperature = temperature

    def verify(self, state: SharedState) -> VerifierResult:
        messages = [
            {"role": "system", "content": VERIFIER_SYSTEM_PROMPT + f"\n\nDomain Rules & Wiki:\n{self.wiki}"},
            {"role": "user", "content": self._format_state(state)}
        ]

        try:
            res = completion(
                messages=messages,
                model=self.model,
                custom_llm_provider=self.provider,
                temperature=self.temperature,
                response_format={"type": "json_object"}
            )
            response_content = res.choices[0].message.content
            parsed = json.loads(response_content)
            
            return VerifierResult(
                status=parsed.get("status", "issues"),
                feedback=parsed.get("feedback"),
                reasoning=parsed.get("reasoning")
            )
        except Exception as e:
            # Fallback in case of parsing errors or API failure
            return VerifierResult(
                status="issues",
                feedback=f"Verifier encountered an error during evaluation: {str(e)}",
                reasoning="Failed to perform verification."
            )

    def _format_state(self, state: SharedState) -> str:
        formatted = f"User Goal:\n{state.user_goal}\n\n"
        formatted += f"Current Slots:\n{json.dumps(state.slots, indent=2)}\n\n"
        formatted += f"Action History:\n{json.dumps(state.history, indent=2)}\n\n"
        if state.last_tool_call:
            formatted += f"Last Tool Call:\n{json.dumps(state.last_tool_call, indent=2)}\n\n"
        if state.last_tool_output:
            formatted += f"Last Tool Output:\n{state.last_tool_output}\n\n"
        return formatted
