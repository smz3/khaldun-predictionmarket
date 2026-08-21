"""Read-only tool: fetch active Polymarket markets from the Gamma API."""

import json

import requests

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
