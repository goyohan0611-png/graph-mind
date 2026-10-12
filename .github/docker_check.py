"""What a directory's check does (Glama): start the image, initialize over stdio, list the tools."""
import asyncio

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    image = StdioServerParameters(command="docker", args=["run", "-i", "--rm", "graph-mind-check"])
    async with stdio_client(image) as (read, write):
        async with ClientSession(read, write) as client:
            await asyncio.wait_for(client.initialize(), timeout=120)
            tools = sorted(tool.name for tool in (await client.list_tools()).tools)
            assert "brain_context" in tools, tools
            print("container answers:", ", ".join(tools))


asyncio.run(main())
