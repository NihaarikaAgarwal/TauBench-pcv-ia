"""
Pre-Commit Validation with Irreversibility Awareness (PCV-IA) Framework

This module provides a comprehensive validation system for agent actions
accounting for the irreversible nature of tau-bench environments.

The validation system consists of:
1. Tool Selection Validation (prevents E2) - Rule-based, no extra LLM call
2. Required Arguments Validation (prevents E3, E6)
3. Format & Type Validation (prevents E3)
4. Policy Compliance Check (prevents E5) - LLM-based, softened
5. Sequence Validation (prevents E8) - Argument + history based
"""

import json
import re
from typing import Dict, List, Any, Tuple, Optional
from enum import Enum
from dataclasses import dataclass, field
from litellm import completion

RESPOND_ACTION_NAME = "respond"


class ActionType(Enum):
    """Categorizes actions by their risk level and irreversibility."""
    READ_ONLY = "read_only"  # search, get_details, check_status
    DATA_WRITING = "data_writing"  # update_info, add_notes
    CRITICAL_STATE_CHANGE = "critical_state_change"  # book, exchange, cancel


@dataclass
class ValidationResult:
    """Result of a validation stage."""
    passed: bool
    stage: str
    message: str
    severity: str = "error"  # "error" or "warning"


@dataclass
class ConversationState:
    """Tracks the state of the conversation to prevent E4 (memory loss)."""
    goal: str = ""
    goal_components: List[str] = field(default_factory=list)
    collected_info: Dict[str, Any] = field(default_factory=dict)
    confirmed_facts: Dict[str, Any] = field(default_factory=dict)
    assumed_facts: Dict[str, Any] = field(default_factory=dict)
    executed_actions: List[str] = field(default_factory=list)
    progress_status: str = "not_started"  # not_started, in_progress, nearly_complete
    constraints: List[str] = field(default_factory=list)

    def detect_contradictions(self, new_info: Dict[str, Any]) -> List[str]:
        """Detect if new information contradicts previously confirmed facts."""
        contradictions = []
        for key, new_value in new_info.items():
            if key in self.confirmed_facts:
                if self.confirmed_facts[key] != new_value:
                    contradictions.append(
                        f"Contradiction detected: Previously confirmed '{key}' as "
                        f"'{self.confirmed_facts[key]}', but now you said '{new_value}'"
                    )
        return contradictions


# Rule-based intent-to-tool mapping.
# Keys are intent keywords; values are lists of tools that are valid for that intent.
GOAL_TOOL_MAP: Dict[str, List[str]] = {
    "book": [
        "book_reservation", "search_direct_flight", "search_onestop_flight",
        "get_user_details", "find_user_id_by_name_zip", "calculate",
    ],
    "cancel": [
        "cancel_reservation", "get_reservation_details", "get_user_details",
        "find_user_id_by_name_zip",
    ],
    "modify": [
        "update_reservation_flights", "update_reservation_baggages",
        "update_reservation_passengers", "get_reservation_details",
        "search_direct_flight", "search_onestop_flight",
        "get_user_details", "find_user_id_by_name_zip", "calculate",
    ],
    "change": [
        "update_reservation_flights", "update_reservation_baggages",
        "update_reservation_passengers", "get_reservation_details",
        "search_direct_flight", "search_onestop_flight",
        "get_user_details", "find_user_id_by_name_zip", "calculate",
    ],
    "refund": [
        "cancel_reservation", "get_reservation_details", "get_user_details",
    ],
    "insurance": [
        "cancel_reservation", "get_reservation_details", "get_user_details",
    ],
    "unwell": [
        "cancel_reservation", "get_reservation_details",
    ],
    "health": [
        "cancel_reservation", "get_reservation_details",
    ],
    "reschedule": [
        "update_reservation_flights", "cancel_reservation", "get_reservation_details",
        "search_direct_flight", "search_onestop_flight", "get_user_details",
    ],
    "return": [
        "return_delivered_order_items", "get_order_details", "get_user_details",
        "find_user_id_by_name_zip",
    ],
    "exchange": [
        "exchange_delivered_order_items", "get_order_details", "get_user_details",
        "find_user_id_by_name_zip",
    ],
    "order": [
        "get_order_details", "get_user_details", "find_user_id_by_name_zip",
        "modify_pending_order_items", "return_delivered_order_items",
        "exchange_delivered_order_items",
    ],
    "flight": [
        "search_direct_flight", "search_onestop_flight", "book_reservation",
        "get_reservation_details", "update_reservation_flights",
        "cancel_reservation", "get_user_details",
    ],
    "transfer": [
        "transfer_to_human_agents",
    ],
    "human": [
        "transfer_to_human_agents",
    ],
}

# These tools are universally valid regardless of goal
UNIVERSAL_TOOLS = {
    "get_user_details", "find_user_id_by_name_zip", "get_reservation_details",
    "get_order_details", "search_direct_flight", "search_onestop_flight",
    "get_product_details", "list_all_product_types", "calculate",
    "transfer_to_human_agents",
}

# Search tools that count as flight searching for sequencing
SEARCH_TOOLS = {"search_direct_flight", "search_onestop_flight"}

# Tools that require a reservation to already be identified
RESERVATION_REQUIRED_TOOLS = {
    "cancel_reservation", "update_reservation_flights",
    "update_reservation_baggages", "update_reservation_passengers",
}

# Tools that require an order to already be identified
ORDER_REQUIRED_TOOLS = {
    "exchange_delivered_order_items", "return_delivered_order_items",
    "modify_pending_order_items",
}


class ValidationFramework:
    """
    Implements the 5-stage pre-commit validation system.
    Stage 1 is now rule-based (no extra LLM call).
    All validation happens BEFORE env.step() to prevent irreversible errors.
    """

    def __init__(
        self,
        tools_info: List[Dict[str, Any]],
        wiki: str,
        model: str,
        provider: str,
        temperature: float = 0.0,
    ):
        self.tools_info = tools_info
        self.tools_map = {tool["function"]["name"]: tool for tool in tools_info}
        self.wiki = wiki
        self.model = model
        self.provider = provider
        self.temperature = temperature
        self.conversation_state = ConversationState()
        self.validation_history: List[Dict[str, Any]] = []

    def categorize_action(self, tool_name: str) -> ActionType:
        """Categorize an action by its risk level."""
        read_only_tools = [
            "get_user_details", "get_reservation_details", "search_direct_flight",
            "search_onestop_flight", "find_user_id_by_name_zip", "get_order_details",
            "get_product_details", "list_all_product_types", "calculate",
        ]

        critical_tools = [
            "book_reservation", "cancel_reservation", "update_reservation_flights",
            "update_reservation_baggages", "update_reservation_passengers",
            "transfer_to_human_agents", "exchange_delivered_order_items",
            "process_return", "return_delivered_order_items",
            "modify_pending_order_items",
        ]

        if tool_name in critical_tools:
            return ActionType.CRITICAL_STATE_CHANGE
        elif tool_name in read_only_tools:
            return ActionType.READ_ONLY
        else:
            return ActionType.DATA_WRITING

    def validate_stage_1_tool_selection(
        self,
        tool_name: str,
        user_goal: str,
        messages: List[Dict[str, Any]]
    ) -> ValidationResult:
        """
        Stage 1: Tool Selection Validation (prevents E2)
        Uses a rule-based lookup against GOAL_TOOL_MAP rather than an extra LLM call.
        This avoids the LLM arguing with itself and removes the frozen-goal problem.
        """
        if tool_name not in self.tools_map:
            return ValidationResult(
                passed=False,
                stage="Tool Selection",
                message=f"Unknown tool: {tool_name}. Available tools: {list(self.tools_map.keys())}"
            )

        # Universal tools are always valid
        if tool_name in UNIVERSAL_TOOLS:
            return ValidationResult(
                passed=True,
                stage="Tool Selection",
                message=f"Tool '{tool_name}' is a read/lookup tool, always valid."
            )

        # Extract intent keywords from the rolling goal context
        goal_lower = user_goal.lower()
        matched_intents = [intent for intent in GOAL_TOOL_MAP if intent in goal_lower]

        if not matched_intents:
            # No recognized intent → allow tool call (conversation context may be complex)
            return ValidationResult(
                passed=True,
                stage="Tool Selection",
                message=f"No specific intent keywords found in context; allowing '{tool_name}' through."
            )

        # Gather all tools allowed by matched intents
        allowed_tools: set = set()
        for intent in matched_intents:
            allowed_tools.update(GOAL_TOOL_MAP[intent])

        if tool_name in allowed_tools:
            return ValidationResult(
                passed=True,
                stage="Tool Selection",
                message=f"Tool '{tool_name}' matches intents: {matched_intents}"
            )
        else:
            # Tool doesn't match any intent keywords — still only warn, not hard-fail
            # because conversation context can evolve (e.g., cancel after modify fails)
            return ValidationResult(
                passed=True,
                stage="Tool Selection",
                severity="warning",
                message=(
                    f"Tool '{tool_name}' not explicitly listed for intents {matched_intents}, "
                    f"but allowing to avoid blocking valid evolved actions."
                )
            )

    def validate_stage_2_required_arguments(
        self,
        tool_name: str,
        arguments: Dict[str, Any]
    ) -> Tuple[ValidationResult, List[str]]:
        """
        Stage 2: Required Arguments Validation (prevents E3, E6)
        Checks if all required arguments are present.
        Note: confirmed_facts tracking is not implemented, so we skip that check.
        """
        tool_info = self.tools_map[tool_name]
        required_params = tool_info.get("function", {}).get("parameters", {}).get("required", [])

        missing_args = []

        for param in required_params:
            if param not in arguments:
                missing_args.append(param)

        if missing_args:
            return ValidationResult(
                passed=False,
                stage="Required Arguments",
                message=f"Missing required arguments: {', '.join(missing_args)}"
            ), missing_args

        return ValidationResult(
            passed=True,
            stage="Required Arguments",
            message="All required arguments are present."
        ), []

    def validate_stage_3_format_validation(
        self,
        tool_name: str,
        arguments: Dict[str, Any]
    ) -> ValidationResult:
        """
        Stage 3: Format & Type Validation (prevents E3)
        Ensures arguments are in correct format/type matching the schema.
        """
        tool_info = self.tools_map[tool_name]
        params_schema = tool_info.get("function", {}).get("parameters", {})
        properties = params_schema.get("properties", {})

        format_issues = []

        for arg_name, arg_value in arguments.items():
            if arg_name not in properties:
                continue

            prop_schema = properties[arg_name]
            expected_type = prop_schema.get("type", "string")

            if expected_type == "string" and not isinstance(arg_value, str):
                format_issues.append(f"'{arg_name}' should be string, got {type(arg_value).__name__}")
            elif expected_type == "number" and not isinstance(arg_value, (int, float)):
                format_issues.append(f"'{arg_name}' should be number, got {type(arg_value).__name__}")
            elif expected_type == "array" and not isinstance(arg_value, list):
                format_issues.append(f"'{arg_name}' should be array, got {type(arg_value).__name__}")
            elif expected_type == "object" and not isinstance(arg_value, dict):
                format_issues.append(f"'{arg_name}' should be object, got {type(arg_value).__name__}")

            param_format = prop_schema.get("format", "")
            if param_format == "date" and isinstance(arg_value, str):
                if not self._is_valid_date_format(arg_value):
                    format_issues.append(f"'{arg_name}' should be in YYYY-MM-DD format, got '{arg_value}'")

        if format_issues:
            return ValidationResult(
                passed=False,
                stage="Format Validation",
                message=f"Format issues detected: {'; '.join(format_issues)}"
            )

        return ValidationResult(
            passed=True,
            stage="Format Validation",
            message="All arguments are in correct format"
        )

    def validate_stage_4_policy_compliance(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        wiki_content: str,
        messages: List[Dict[str, Any]]
    ) -> ValidationResult:
        """
        Stage 4: Policy Compliance Check (prevents E5)
        Checks if the action violates any domain policies or constraints.
        Uses softened prompt to avoid false positives.
        """
        # Build a recent conversation snippet to give the LLM proper context
        recent_context = self._build_recent_context(messages, last_n=6)

        policy_check_prompt = f"""You are checking whether a customer service agent's action is clearly compliant with the domain policy.

## Domain Policy
{wiki_content}

## Action Being Reviewed
Tool: {tool_name}
Arguments:
{json.dumps(arguments, indent=2)}

## Recent Conversation Context
{recent_context}

## Instructions
- Look at the recent conversation context to understand what has been discussed and agreed upon.
- Check if the action is a CLEAR AND UNAMBIGUOUS policy violation.
- If the user has recently confirmed or agreed to the action, that satisfies confirmation requirements.
- If the action is compliant or you are unsure, respond with "No violations".
- Only flag genuine, clear violations — not edge cases or "maybe" issues.
"""

        validation_messages = messages + [
            {"role": "user", "content": policy_check_prompt}
        ]

        try:
            response = completion(
                messages=validation_messages,
                model=self.model,
                custom_llm_provider=self.provider,
                temperature=self.temperature,
                max_tokens=600
            )
            response_text = response.choices[0].message.content
            clean_response = re.sub(r'<think>.*?</think>', '', response_text, flags=re.DOTALL).strip()

            if "no violations" in clean_response.lower():
                return ValidationResult(
                    passed=True,
                    stage="Policy Compliance",
                    message="Action is policy-compliant"
                )
            else:
                return ValidationResult(
                    passed=False,
                    stage="Policy Compliance",
                    message=f"Policy violation detected: {clean_response}"
                )
        except Exception as e:
            return ValidationResult(
                passed=False,
                stage="Policy Compliance",
                message=f"Error checking policy compliance: {str(e)}",
                severity="warning"
            )

    def validate_stage_5_sequence_validation(
        self,
        tool_name: str,
        current_goal: str,
        arguments: Dict[str, Any],
        messages: List[Dict[str, Any]]
    ) -> ValidationResult:
        """
        Stage 5: Sequence Validation (prevents E8)
        Ensures action is being taken in the right sequence.
        Uses arguments and message history — NOT the empty confirmed_facts dict.
        """
        # Check if reservation ID is present for tools that require it
        if tool_name in RESERVATION_REQUIRED_TOOLS:
            if "reservation_id" in arguments:
                return ValidationResult(
                    passed=True,
                    stage="Sequence Validation",
                    message=f"Reservation ID '{arguments['reservation_id']}' found in arguments."
                )
            # Also check if a reservation was retrieved in the conversation history
            for msg in messages:
                if msg.get("role") == "tool" and "reservation_id" in (msg.get("content") or ""):
                    return ValidationResult(
                        passed=True,
                        stage="Sequence Validation",
                        message="Reservation ID found in conversation history (tool response)."
                    )
            return ValidationResult(
                passed=False,
                stage="Sequence Validation",
                message=(
                    f"Cannot call '{tool_name}' without first identifying the reservation. "
                    "Get the reservation_id from user or via get_user_details first."
                )
            )

        # Check if order ID is present for tools that require it
        if tool_name in ORDER_REQUIRED_TOOLS:
            if "order_id" in arguments:
                return ValidationResult(
                    passed=True,
                    stage="Sequence Validation",
                    message=f"Order ID '{arguments['order_id']}' found in arguments."
                )
            for msg in messages:
                if msg.get("role") == "tool" and "order_id" in (msg.get("content") or ""):
                    return ValidationResult(
                        passed=True,
                        stage="Sequence Validation",
                        message="Order ID found in conversation history (tool response)."
                    )
            return ValidationResult(
                passed=False,
                stage="Sequence Validation",
                message=(
                    f"Cannot call '{tool_name}' without first identifying the order. "
                    "Get the order_id from user or via get_user_details first."
                )
            )

        # Booking requires having searched for flights first
        if tool_name == "book_reservation":
            for msg in messages:
                tool_name_in_msg = msg.get("name", "")
                if tool_name_in_msg in SEARCH_TOOLS:
                    return ValidationResult(
                        passed=True,
                        stage="Sequence Validation",
                        message="Flight search was performed earlier in the conversation."
                    )
            return ValidationResult(
                passed=False,
                stage="Sequence Validation",
                message="Should show available flights/options before attempting to book. "
                        "Call search_direct_flight or search_onestop_flight first."
            )

        return ValidationResult(
            passed=True,
            stage="Sequence Validation",
            message="Action sequence is logical and correct"
        )

    def run_full_validation(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        user_goal: str,
        messages: List[Dict[str, Any]]
    ) -> Tuple[bool, List[ValidationResult]]:
        """
        Run all 5 validation stages and return whether action should proceed.
        Returns (all_passed, validation_results)
        Note: Stage 6 (Goal-Outcome Sanity) is merged into the improved Stage 1.
        """
        results = []

        # Stage 1: Tool Selection (rule-based)
        result1 = self.validate_stage_1_tool_selection(tool_name, user_goal, messages)
        results.append(result1)
        if not result1.passed:
            return False, results

        # Stage 2: Required Arguments
        result2, missing_args = self.validate_stage_2_required_arguments(tool_name, arguments)
        results.append(result2)
        if not result2.passed and missing_args:
            return False, results

        # Stage 3: Format Validation
        result3 = self.validate_stage_3_format_validation(tool_name, arguments)
        results.append(result3)
        if not result3.passed:
            return False, results

        # Stage 4: Policy Compliance (LLM-based, softened prompt)
        result4 = self.validate_stage_4_policy_compliance(
            tool_name, arguments, self.wiki, messages
        )
        results.append(result4)
        if not result4.passed:
            return False, results

        # Stage 5: Sequence Validation (argument + history based)
        result5 = self.validate_stage_5_sequence_validation(
            tool_name, user_goal, arguments, messages
        )
        results.append(result5)
        if not result5.passed:
            return False, results

        return True, results

    def _build_recent_context(self, messages: List[Dict[str, Any]], last_n: int = 6) -> str:
        """Build a compact recent conversation context string, stripping <think> tags."""
        recent = messages[-last_n:] if len(messages) > last_n else messages
        lines = []
        for msg in recent:
            role = msg.get("role", "")
            if role == "tool":
                name = msg.get("name", "tool")
                content = (msg.get("content") or "")[:300]
                lines.append(f"[{name} result]: {content}")
            elif role in ("user", "assistant"):
                content = msg.get("content") or ""
                content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
                if content:
                    lines.append(f"[{role}]: {content[:300]}")
        return "\n".join(lines)

    def build_confirmation_summary(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        expected_outcome: str
    ) -> str:
        """
        Build a clear confirmation summary before executing the action.
        This is the last chance to catch issues before irreversible execution.
        """
        summary = f"""
╔══════════════════════════════════════════════════════════════╗
║  CONFIRMATION GATE - POINT OF NO RETURN                     ║
╚══════════════════════════════════════════════════════════════╝

📋 ACTION SUMMARY:
   Tool: {tool_name}

📊 ARGUMENTS:
"""
        for key, value in arguments.items():
            summary += f"\n   • {key}: {value}"

        summary += f"""

🎯 EXPECTED OUTCOME:
   {expected_outcome}

⚠️  NOTE: This action is IRREVERSIBLE in the tau-bench environment.
   Once confirmed, the database will be modified and cannot be undone.

❓ Is this correct? (yes/no)
"""
        return summary

    def update_state_from_message(self, message: str):
        """Update conversation state based on user message (prevents E4)."""
        # This would be enhanced with NER/info extraction in production
        pass

    def _is_valid_date_format(self, date_string: str) -> bool:
        """Check if date string is in YYYY-MM-DD format."""
        return bool(re.match(r'^\d{4}-\d{2}-\d{2}$', date_string))
