"""Run inside the MCP container; optional query tests live retrieval."""
import asyncio
import os
import sys

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main():
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(
            os.environ["LIGHTRAG_URL"] + "/health",
            headers={"X-API-Key": os.environ["LIGHTRAG_API_KEY"]},
        )
        response.raise_for_status()
        print("LightRAG health: OK")
    async with streamablehttp_client(
        "http://127.0.0.1:8000/mcp",
        headers={"Authorization": "Bearer " + os.environ["MCP_TOKEN"]},
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listing = await session.list_tools()
            assert "knowledge_search" in [tool.name for tool in listing.tools]
            print("MCP initialize + tools/list: OK")
            if len(sys.argv) > 1:
                result = await session.call_tool("knowledge_search", {"query": sys.argv[1]})
                print(result.model_dump_json(indent=2))
                if result.isError:
                    raise SystemExit(1)
                print("Verifica che i risultati contengano i documenti caricati.")


if __name__ == "__main__":
    asyncio.run(main())
