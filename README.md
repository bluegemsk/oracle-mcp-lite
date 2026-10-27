# oracle-mcp-lite

Free, offline **Oracle AWR / ASH report analyzer** for AI assistants (Model Context Protocol server, MIT).

Give Claude, Cursor, Codex or any MCP client an AWR or ASH report (HTML or text, Oracle 10g–23ai, single
instance or RAC global) and get back: report type and DB version, average active sessions, load profile, top wait
events and top SQL — each with a short, version-aware DBA tuning advice — plus a prioritised top-3 `tuning_summary`.
Runs locally; your reports never leave your machine. No Oracle client or database connection needed.

> **Full version:** PL/SQL code review, DDL summary (missing PKs, unindexed FKs), Data Pump `.dmp` inspection and
> more — see **https://bluegem.sk/oracle-mcp-lite.html**

## Install
```bash
pip install oracle-mcp-lite
oracle-mcp-lite --list-tools
```
Client config (Claude Desktop / Cursor / Gemini CLI):
```json
{"mcpServers": {"oracle-awr": {"command": "oracle-mcp-lite"}}}
```
Claude Code: `claude mcp add oracle-awr -- oracle-mcp-lite`

## Use
> "Analyze the AWR report at /reports/awr_0900.html and tell me what to fix first."

Example `tuning_summary` (synthetic report):
```
1. ARCH wait on SENDREQ (41.7%): Redo shipping to the standby is slow: check network bandwidth/latency to the
   standby and the transport mode; ASYNC transport removes it from the commit path.
2. CPU time (27.8%): Find the top SQL by CPU / buffer gets and cut logical I/O ...
3. SQL aaaaaaaaaaaa1 (25.0%): Slow per execution (1,800.0 s/exec): check the plan with DBMS_XPLAN.DISPLAY_AWR ...
```

## Development
```bash
pip install -e '.[dev]' && pytest && python -m build
```
Tests use synthetic fixtures only.

Not affiliated with Oracle. Oracle is a trademark of Oracle Corporation.

<!-- mcp-name: io.github.bluegemsk/oracle-mcp-lite -->
