import asyncio
import json
from pathlib import Path

from fastmcp import Client

import server
from engineering.recipes import get_recipe, instantiate
from tests.test_recipes import _sample_inputs


async def main():
    async with Client(server.mcp) as client:
        steps = instantiate(
            get_recipe("multi_project_staged_edit"),
            _sample_inputs("multi_project_staged_edit", Path("D:/AI/Autocad-MCP/.tmp-recipe")),
        )
        result = (
            await client.call_tool("cad_batch", {"steps": steps, "dry_run": True})
        ).structured_content
        print(json.dumps(result, indent=2))


asyncio.run(main())
