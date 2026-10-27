"""The unified discovery plane as an MCP server, so any MCP client can reach the governed data.

    python -m claims.mcp_server                       # stdio, for Claude Desktop / Claude Code
    python -m claims.mcp_server --http                # streamable HTTP on :8020
    CLAIMS_ROLE=investigator python -m claims.mcp_server

Same functions, same governance and the same audit trail as the in-process demo agent,
at stage F. The role comes from CLAIMS_ROLE; in production it would come from the
gateway's authenticated identity.
"""

import os
import sys

from mcp.server.mcpserver import MCPServer

from claims import governance, plane

ROLE = os.getenv("CLAIMS_ROLE", "claims_officer")
_session = plane.Session(stage="F", principal=governance.Principal(user=os.getenv("USER", "mcp-client"), role=ROLE))

server = MCPServer(
    "claims-discovery-plane",
    instructions="Governed access to an insurer's claims data: lakehouse, documents, knowledge graph, signals and "
                 "actions. Personal data is masked by role and every call is audited. Payments need human approval.",
)
for fn in plane.bound_functions(lambda: _session, stage="F"):
    server.tool(name=fn.__name__, description=(fn.__doc__ or "").strip())(fn)


if __name__ == "__main__":
    if "--http" in sys.argv:
        server.run("streamable-http", port=8020)
    else:
        server.run("stdio")
