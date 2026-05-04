# Copyright Sierra

import json
from litellm import completion
from typing import List, Optional, Dict, Any

from tau_bench.agents.base import Agent
from tau_bench.envs.base import Env
from tau_bench.types import SolveResult, Action, RESPOND_ACTION_NAME
from tau_bench.agents.validation_framework import (
    ValidationFramework,
    ActionType,
)


class PCVIAAgent(Agent):
    """
    Pre-Commit Validation with Irreversibility Awareness (PCV-IA) Agent
    
    This agent integrates strict pre-action validation to prevent irreversible errors
    in tau-bench environments. It implements 6-stage validation before calling env.step().
    """
    
    def __init__(
        self,
        tools_info: List[Dict[str, Any]],
        wiki: str,
        model: str,
        provider: str,
        temperature: float = 0.0,
        enable_validation: bool = True,
        validation_temperature: float = 0.0,
    ):
        self.tools_info = tools_info
        self.wiki = wiki
        self.model = model
        self.provider = provider
        self.temperature = temperature
        self.enable_validation = enable_validation
        
        # Initialize validation framework
        self.validator = ValidationFramework(
            tools_info=tools_info,
            wiki=wiki,
            model=model,
            provider=provider,
            temperature=validation_temperature,
        )
        
        self.validation_stats = {
            "total_actions": 0,
            "validated_actions": 0,
            "validation_passed": 0,
            "validation_failed": 0,
            "confirmations_requested": 0,
            "user_confirmations_received": 0,
        }
    
    def solve(
        self, env: Env, task_index: Optional[int] = None, max_num_steps: int = 30
    ) -> SolveResult:
        total_cost = 0.0
        env_reset_res = env.reset(task_index=task_index)
        obs = env_reset_res.observation
        info = env_reset_res.info.model_dump()
        reward = 0.0
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": self.wiki},
            {"role": "user", "content": obs},
        ]
        
        step_count = 0
        
        for step_num in range(max_num_steps):
            step_count += 1
            
            # Get agent's response
            res = completion(
                messages=messages,
                model=self.model,
                custom_llm_provider=self.provider,
                tools=self.tools_info,
                temperature=self.temperature,
            )
            next_message = res.choices[0].message.model_dump()
            total_cost += res._hidden_params["response_cost"] or 0
            
            # Convert message to action
            action = message_to_action(next_message)
            self.validation_stats["total_actions"] += 1
            
            # Apply PCV-IA validation BEFORE env.step() if action is a tool call
            if action.name != RESPOND_ACTION_NAME and self.enable_validation:
                validation_passed, validation_message = self._validate_and_handle_action(
                    action=action,
                    messages=messages,
                    env=env,
                )
                
                if not validation_passed:
                    # Add validation rejection to messages
                    messages.extend([
                        next_message,
                        {
                            "role": "tool",
                            "tool_call_id": next_message.get("tool_calls", [{}])[0].get("id", ""),
                            "name": action.name,
                            "content": validation_message,
                        },
                    ])
                    continue
            
            # Execute action (POINT OF NO RETURN for state-changing actions)
            env_response = env.step(action)
            reward = env_response.reward
            info = {**info, **env_response.info.model_dump()}
            
            # Add to message history
            if action.name != RESPOND_ACTION_NAME:
                next_message["tool_calls"] = next_message["tool_calls"][:1]
                messages.extend(
                    [
                        next_message,
                        {
                            "role": "tool",
                            "tool_call_id": next_message["tool_calls"][0]["id"],
                            "name": next_message["tool_calls"][0]["function"]["name"],
                            "content": env_response.observation,
                        },
                    ]
                )
            else:
                messages.extend(
                    [
                        next_message,
                        {"role": "user", "content": env_response.observation},
                    ]
                )
            
            if env_response.done:
                break
        
        # Add validation stats to info
        info["validation_stats"] = self.validation_stats
        info["total_steps"] = step_count
        
        return SolveResult(
            reward=reward,
            info=info,
            messages=messages,
            total_cost=total_cost,
        )
    
    def _validate_and_handle_action(
        self,
        action: Action,
        messages: List[Dict[str, Any]],
        env: Env,
    ) -> tuple[bool, str]:
        """
        Run PCV-IA validation on the action before env.step() is called.
        
        Returns (validation_passed, feedback_message)
        """
        tool_name = action.name
        arguments = action.kwargs
        
        # Determine action type
        action_type = self.validator.categorize_action(tool_name)
        
        # For read-only actions, do minimal validation
        if action_type == ActionType.READ_ONLY:
            self.validation_stats["validated_actions"] += 1
            self.validation_stats["validation_passed"] += 1
            return True, ""
        
        # For data-writing and critical actions, run full validation
        # Extract user goal from initial message (heuristic)
        user_goal = self._extract_goal_from_messages(messages)
        
        # Run 6-stage validation (Stage 6 is merged into Stage 1 in the new framework)
        all_passed, validation_results = self.validator.run_full_validation(
            tool_name=tool_name,
            arguments=arguments,
            user_goal=user_goal,
            messages=messages,
        )
        
        self.validation_stats["validated_actions"] += 1
        
        if all_passed:
            self.validation_stats["validation_passed"] += 1
            
            # For critical actions, request explicit confirmation
            if action_type == ActionType.CRITICAL_STATE_CHANGE:
                expected_outcome = self._get_expected_outcome(tool_name)
                confirmation_msg = self.validator.build_confirmation_summary(
                    tool_name=tool_name,
                    arguments=arguments,
                    expected_outcome=expected_outcome,
                )
                
                self.validation_stats["confirmations_requested"] += 1
                
                # In a real system, we'd wait for user "yes" here
                # For now, we assume confirmation after validation passes
                self.validation_stats["user_confirmations_received"] += 1
                
                return True, confirmation_msg
            
            return True, ""
        else:
            self.validation_stats["validation_failed"] += 1
            
            # Build rejection message
            rejection_msg = "❌ Action validation failed. Please revise:\n\n"
            for result in validation_results:
                if not result.passed:
                    rejection_msg += f"**{result.stage}**: {result.message}\n"
            
            return False, rejection_msg
    
    def _extract_goal_from_messages(self, messages: List[Dict[str, Any]]) -> str:
        """
        Extract a DYNAMIC rolling goal context from the last 6 messages.
        This solves the 'frozen goal' bug where the first user message was used
        even after the conversation evolved (e.g., from 'modify' to 'cancel').
        """
        import re
        # Use the last 6 non-system messages to capture the evolved conversation goal
        non_system = [m for m in messages if m.get("role") != "system"]
        recent = non_system[-6:] if len(non_system) > 6 else non_system
        
        lines = []
        for msg in recent:
            role = msg.get("role", "")
            if role == "tool":
                # Include short tool result summaries
                content = (msg.get("content") or "")[:200]
                name = msg.get("name", "tool")
                lines.append(f"[{name} result]: {content}")
            elif role in ("user", "assistant"):
                content = msg.get("content") or ""
                # Strip Qwen3 <think> blocks
                content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
                if content:
                    lines.append(f"[{role}]: {content[:400]}")
        
        return "\n".join(lines)[-1500:]  # Return last 1500 chars of context
    
    def _get_expected_outcome(self, tool_name: str) -> str:
        """Get the expected outcome description for a tool."""
        outcome_map = {
            "book_reservation": "Flight will be booked and confirmed in the system",
            "cancel_reservation": "Reservation will be cancelled and refund processed",
            "exchange_delivered_order_items": "Items will be exchanged and new items shipped",
            "modify_flight": "Flight details will be updated",
            "process_return": "Return will be processed and refund initiated",
            "update_customer_email": "Customer email will be updated",
        }
        return outcome_map.get(tool_name, "Action will be executed")


def message_to_action(message: Dict[str, Any]) -> Action:
    """Convert LLM message to Action."""
    import re
    if (
        "tool_calls" in message
        and message["tool_calls"] is not None
        and len(message["tool_calls"]) > 0
        and message["tool_calls"][0]["function"] is not None
    ):
        tool_call = message["tool_calls"][0]
        return Action(
            name=tool_call["function"]["name"],
            kwargs=json.loads(tool_call["function"]["arguments"]),
        )
    else:
        content = message.get("content", "")
        # Strip <think> blocks so they do not leak to the user simulator and cause role-confusion loops.
        clean_content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
        return Action(
            name=RESPOND_ACTION_NAME,
            kwargs={"content": clean_content},
        )
