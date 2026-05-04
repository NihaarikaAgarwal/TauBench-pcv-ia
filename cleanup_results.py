#!/usr/bin/env python3
"""
Script to clean up tau-bench results files:
- Removes duplicate (task_id, trial) entries (keeps the latest one)
- Sorts results by task_id, then by trial
"""

import json
import argparse
import sys
from pathlib import Path


def cleanup_results(input_file: str, output_file: str = None):
    """Remove duplicates and sort results by task_id."""

    if not Path(input_file).exists():
        print(f"❌ File not found: {input_file}")
        sys.exit(1)

    with open(input_file, "r") as f:
        data = json.load(f)

    original_count = len(data)
    print(f"📂 Loaded {original_count} results from {input_file}")

    # Keep track of seen (task_id, trial) pairs - keep the LAST occurrence
    seen = {}
    for item in data:
        key = (item["task_id"], item["trial"])
        seen[key] = item  # Later entries overwrite earlier ones

    # Convert back to list and sort by task_id, then trial
    cleaned = list(seen.values())
    cleaned.sort(key=lambda x: (x["task_id"], x["trial"]))

    duplicates_removed = original_count - len(cleaned)

    print(f"✅ Unique (task_id, trial) pairs: {len(cleaned)}")
    if duplicates_removed > 0:
        print(f"🗑️  Duplicates removed: {duplicates_removed}")

    # Output file
    if output_file is None:
        output_file = input_file  # Overwrite original

    with open(output_file, "w") as f:
        json.dump(cleaned, f, indent=2)

    print(f"📄 Saved cleaned results to {output_file}")

    # Show summary by trial
    trials = {}
    for item in cleaned:
        trial = item["trial"]
        trials[trial] = trials.get(trial, 0) + 1

    print(f"\n📊 Results per trial:")
    for trial in sorted(trials.keys()):
        print(f"   Trial {trial}: {trials[trial]} tasks")


def main():
    parser = argparse.ArgumentParser(description="Clean up tau-bench results file")
    parser.add_argument("input_file", help="Path to the results JSON file")
    parser.add_argument("-o", "--output", help="Output file (default: overwrite input)")

    args = parser.parse_args()
    cleanup_results(args.input_file, args.output)


if __name__ == "__main__":
    main()