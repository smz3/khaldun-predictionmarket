"""
Step 1 of khaldun: the smallest possible agent loop.

No framework, no Tool Runner, no risk/execution tools, no state/logging.
Just: give Claude the read-only tools in khaldun.tools, let it ask for
data, run the tool ourselves, feed the result back, print what it decides.
This exists so the tool-use mechanics are visible and hand-written once
before reaching for any higher-level abstraction (see
khaldun/docs/agent-infrastructure-plan.md).
"""

import json
import os

import anthropic

from khaldun.tools import SCHEMAS, run_tool

MODEL = "claude-sonnet-5"


def main() -> None:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    messages = [
        {
            "role": "user",
            "content": (
                "Look at a handful of currently active Polymarket markets "
                "and tell me which one looks most interesting to research "
                "further, and why."
            ),
        }
    ]

    while True:
        response = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            tools=SCHEMAS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        for block in response.content:
            if block.type == "text":
                print(block.text)

        if response.stop_reason != "tool_use":
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            print(f"[tool call] {block.name}({block.input})")
            result = run_tool(block.name, block.input)
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                }
            )
        messages.append({"role": "user", "content": tool_results})


if __name__ == "__main__":
    main()
