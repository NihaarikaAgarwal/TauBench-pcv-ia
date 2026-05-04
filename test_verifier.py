import unittest
from unittest.mock import patch
from tau_bench.types import SharedState
from tau_bench.agents import VerifierAgent


class TestVerifierAgent(unittest.TestCase):
    def setUp(self):
        self.wiki = "Domain Rules: Do not change reservations within 24 hours of flight."
        self.agent = VerifierAgent(model="gpt-4o", provider="openai", wiki=self.wiki)

    @patch("tau_bench.agents.verifier_agent.completion")
    def test_verify_ok(self, mock_completion):
        # Mocking the LiteLLM response
        mock_response = mock_completion.return_value
        # Mock choices -> message -> content
        mock_response.choices = [
            type('obj', (object,), {'message': type('obj', (object,), {'content': '{"status": "ok", "feedback": null, "reasoning": "Everything looks good."}'})()})()
        ]

        state = SharedState(
            user_goal="Book a flight",
            slots={"destination": "NYC"},
            history=[{"role": "user", "content": "I want to go to NYC."}],
            last_tool_call={"name": "search_flights", "kwargs": {"dest": "NYC"}},
            last_tool_output="Found 3 flights."
        )

        result = self.agent.verify(state)

        self.assertEqual(result.status, "ok")
        self.assertIsNone(result.feedback)
        self.assertEqual(result.reasoning, "Everything looks good.")
        
    @patch("tau_bench.agents.verifier_agent.completion")
    def test_verify_issues(self, mock_completion):
        mock_response = mock_completion.return_value
        mock_response.choices = [
            type('obj', (object,), {'message': type('obj', (object,), {'content': '{"status": "issues", "feedback": "Missing date slot.", "reasoning": "Need date to book flight."}'})()})()
        ]

        state = SharedState(
            user_goal="Book a flight",
            slots={"destination": "NYC"},
            history=[{"role": "user", "content": "I want to go to NYC."}],
            last_tool_call=None,
            last_tool_output=None
        )

        result = self.agent.verify(state)

        self.assertEqual(result.status, "issues")
        self.assertEqual(result.feedback, "Missing date slot.")


if __name__ == "__main__":
    unittest.main()
