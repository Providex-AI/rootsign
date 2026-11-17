# MCP proxy, no database, no API key

RootSign's MCP proxy sits between an MCP client and an MCP server and records
every `tools/call` on the hash chain. The agent changes one thing, the URL it
connects to. No framework adapter is involved.

```bash
pip install 'rootsign[mcp]'
python demo.py
```

```text
tool:  invoice sent to acme-corp for $1,500.00
tool:  payment of $1,500.00 recorded for acme-corp

2 actions recorded → ~/.rootsign/sessions/<session_id>.jsonl

Verify the chain:
    rootsign verify --local ~/.rootsign/sessions/<session_id>.jsonl
```

## Files

| File | What it does |
| --- | --- |
| `demo.py` | Runs an MCP server (FastMCP, from the official MCP Python SDK), the RootSign proxy in front of it, and an MCP client that calls two tools through the proxy, all in one process on 127.0.0.1. Uses ports 8001 and 8000. |
| `proxy.py` | The proxy on its own, for putting in front of a server you already run. Set `MCP_UPSTREAM_URL`, then point your agent at `http://127.0.0.1:8000/mcp`. Stop it with Ctrl+C; it prints the session ID. |

Nothing in `demo.py` is mocked: the client, the proxy and the server talk real
streamable HTTP to each other.

## Tested versions

CI runs `demo.py` in the MCP contract job (`tests/contract/mcp/test_docs_and_example.py`)
against the MCP release `rootsign[mcp]` resolves to. It has also been run on
MCP 1.28.0. The proxy itself is tested against real FastMCP servers in both
transport modes (`tests/contract/mcp/test_proxy_real_server.py`).
