"""
Step 1 of khaldun: the smallest possible agent loop.

No framework, no Tool Runner, no execution tool, no state/logging.
Just: give Claude one read-only tool, let it ask for data, run the tool
ourselves, feed the result back, print what it decides. This exists so the
tool-use mechanics are visible and hand-written once before reaching for
any higher-level abstraction (see khaldun/docs/agent-infrastructure-plan.md).
"""

import json
import os

import anthropic
import requests

MODEL = "claude-sonnet-5"
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"

GET_MARKET_DATA_TOOL = {
    "name": "get_market_data",
    "description": (
        "Fetch currently active Polymarket prediction markets: question, "
        "outcomes, current outcome prices, volume, and liquidity."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "How many markets to fetch (default 5, max 20).",
            }
        },
    },
}


def get_market_data(limit: int = 5) -> list[dict]:
    limit = max(1, min(limit, 20))
    resp = requests.get(
        GAMMA_MARKETS_URL,
        params={"limit": limit, "active": "true", "closed": "false"},
        timeout=10,
    )
    resp.raise_for_status()

    markets = []
    for m in resp.json():
        try:
            outcomes = json.loads(m.get("outcomes", "[]"))
            prices = json.loads(m.get("outcomePrices", "[]"))
        except json.JSONDecodeError:
            outcomes, prices = [], []
        markets.append(
            {
                "question": m.get("question"),
                "outcomes": dict(zip(outcomes, prices)),
                "volume": m.get("volume"),
                "liquidity": m.get("liquidity"),
                "end_date": m.get("endDate"),
            }
        )
    return markets


def run_tool(name: str, tool_input: dict) -> object:
    if name == "get_market_data":
        return get_market_data(**tool_input)
    raise ValueError(f"unknown tool: {name}")


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
            tools=[GET_MARKET_DATA_TOOL],
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
