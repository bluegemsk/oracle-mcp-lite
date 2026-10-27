import asyncio
from pathlib import Path
from oracle_mcp_lite.server import mcp
F = Path(__file__).parent / "fixtures"


def test_only_awr_tool():
    assert {t.name for t in asyncio.run(mcp.list_tools())} == {"awr_analyze"}
    res = asyncio.run(mcp.call_tool("awr_analyze", {"path": str(F / "awr_10g.html")}))
    assert "tuning_summary" in str(res) and "bluegem.sk/oracle-mcp" in str(res)
