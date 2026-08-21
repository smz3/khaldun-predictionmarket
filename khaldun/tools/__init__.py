"""
Tool registry: every tool Claude is allowed to call, and how to run it.

Add a new tool by writing its schema + function in its own module here
(one file per concern - data/market_data.py, later risk.py, execution.py),
then registering it in SCHEMAS and _FUNCTIONS below.
"""

from .market_data import GET_MARKET_DATA_TOOL, get_market_data

SCHEMAS = [GET_MARKET_DATA_TOOL]

_FUNCTIONS = {
    "get_market_data": get_market_data,
}


def run_tool(name: str, tool_input: dict) -> object:
    if name not in _FUNCTIONS:
        raise ValueError(f"unknown tool: {name}")
    return _FUNCTIONS[name](**tool_input)
