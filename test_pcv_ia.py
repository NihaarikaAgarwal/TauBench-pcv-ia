#!/usr/bin/env python3
"""
Quick validation test to ensure PCV-IA agent can be instantiated and runs basic validation.
This is a smoke test before running full experiments.
"""

import sys
from tau_bench.agents.validation_framework import ValidationFramework, ActionType
from tau_bench.agents.pcv_ia_agent import PCVIAAgent

def test_validation_framework():
    """Test that validation framework initializes correctly."""
    print("Testing ValidationFramework initialization...")
    
    # Mock tools info
    tools_info = [
        {
            "function": {
                "name": "get_product_details",
                "description": "Get details of a product",
                "parameters": {
                    "required": ["product_id"],
                    "properties": {
                        "product_id": {"type": "string", "description": "Product ID"}
                    }
                }
            }
        },
        {
            "function": {
                "name": "exchange_delivered_order_items",
                "description": "Exchange items in a delivered order",
                "parameters": {
                    "required": ["order_id", "item_ids", "new_item_ids"],
                    "properties": {
                        "order_id": {"type": "string"},
                        "item_ids": {"type": "array"},
                        "new_item_ids": {"type": "array"}
                    }
                }
            }
        }
    ]
    
    wiki = "Test wiki content"
    
    try:
        validator = ValidationFramework(
            tools_info=tools_info,
            wiki=wiki,
            model="gpt-4o",
            provider="openai",
            temperature=0.0
        )
        print("✓ ValidationFramework initialized successfully")
        
        # Test action type categorization
        read_type = validator.categorize_action("get_product_details")
        assert read_type == ActionType.READ_ONLY, "Expected READ_ONLY"
        print("✓ Action type categorization works (READ_ONLY)")
        
        critical_type = validator.categorize_action("exchange_delivered_order_items")
        assert critical_type == ActionType.CRITICAL_STATE_CHANGE, "Expected CRITICAL_STATE_CHANGE"
        print("✓ Action type categorization works (CRITICAL_STATE_CHANGE)")
        
        return True
    except Exception as e:
        print(f"✗ ValidationFramework test failed: {e}")
        return False

def test_pcvia_agent():
    """Test that PCV-IA agent can be instantiated."""
    print("\nTesting PCVIAAgent initialization...")
    
    tools_info = [
        {
            "function": {
                "name": "test_tool",
                "description": "Test tool",
                "parameters": {"required": [], "properties": {}}
            }
        }
    ]
    
    try:
        agent = PCVIAAgent(
            tools_info=tools_info,
            wiki="Test wiki",
            model="gpt-4o",
            provider="openai",
            temperature=0.0,
            enable_validation=True,
            validation_temperature=0.0
        )
        print("✓ PCVIAAgent initialized successfully")
        print(f"  - Validation enabled: {agent.enable_validation}")
        print(f"  - Initial stats: {agent.validation_stats}")
        return True
    except Exception as e:
        print(f"✗ PCVIAAgent test failed: {e}")
        return False

def main():
    print("=" * 70)
    print("PCV-IA Implementation Smoke Test")
    print("=" * 70)
    print()
    
    test1 = test_validation_framework()
    test2 = test_pcvia_agent()
    
    print()
    print("=" * 70)
    if test1 and test2:
        print("✓ All smoke tests passed!")
        print()
        print("Next steps:")
        print("1. Run: bash pcv_ia_experiment.sh")
        print("2. Compare results between baseline and PCV-IA")
        print("3. Review validation_stats in result files")
        return 0
    else:
        print("✗ Some tests failed. Check errors above.")
        return 1

if __name__ == "__main__":
    sys.exit(main())
