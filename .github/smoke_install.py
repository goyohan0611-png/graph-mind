"""After `pip install graph-mind-memory` on a fresh machine: the server starts, lists its tools,
saves a memory and finds it again. Run by .github/workflows/install-check.yml."""
import asyncio
import shutil
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    server = StdioServerParameters(command=shutil.which("graph-mind"), args=[])
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            tools = {tool.name for tool in (await client.list_tools()).tools}
            assert {"brain_context", "brain_remember", "brain_folder"} <= tools, tools
            await client.call_tool("brain_remember", {
                "content": "The release train leaves on Thursdays", "title": "release day",
                "memory_type": "fact", "source_ref": "install-check"})
            found = await client.call_tool("brain_recall", {"query": "release train"})
            text = str(found.structured_content or found.content)
            assert "Thursdays" in text, text
            print(f"ok: {len(tools)} tools, remember + recall on Python {sys.version.split()[0]}")


asyncio.run(main())
