"""MCP server exposing awr_analyze over stdio or streamable HTTP."""
from __future__ import annotations
import argparse, os
from mcp.server.fastmcp import FastMCP
from . import awr

mcp = FastMCP("oracle-mcp-lite", host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")))
FULL = "Full version (PL/SQL review, DDL summary, Data Pump inspection): https://bluegem.sk/oracle-mcp"


@mcp.tool()
def awr_analyze(content: str | None = None, path: str | None = None) -> dict:
    """Analyze an Oracle AWR or ASH report (HTML or text, 10g-23ai, incl. RAC global). Returns DB info, load
    profile, top wait events and top SQL with tuning advice, recommendations and a top-3 tuning_summary."""
    if not content:
        if not path:
            raise ValueError("provide content or path")
        with open(path, encoding="utf-8", errors="replace") as f:
            content = f.read()
    r = awr.analyze(content)
    r["more"] = FULL
    return r


def main():
    ap = argparse.ArgumentParser(prog="oracle-mcp-lite")
    ap.add_argument("--transport", choices=["stdio", "http"], default=os.environ.get("TRANSPORT", "stdio"))
    ap.add_argument("--list-tools", action="store_true")
    a = ap.parse_args()
    if a.list_tools:
        import asyncio
        for t in asyncio.run(mcp.list_tools()):
            print(t.name)
        return
    mcp.run(transport="streamable-http" if a.transport == "http" else "stdio")


if __name__ == "__main__":
    main()
