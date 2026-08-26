"""
Agent Memory Extension for Hindsight FastMCP Server.

Exposes procedural rule mutation (`rethink_memory`) and context offloading
tools (`context_archive`, `context_retrieve`) directly through Hindsight's
native FastMCP server.
"""

import logging
import sys
from typing import Any, Dict

from fastmcp import FastMCP

from hindsight_api import MemoryEngine
from hindsight_api.extensions.mcp import MCPExtension

# Ensure agent-memory modules are importable
AGENT_MEMORY_DB_PATH = "/mnt/data/agent-memory/db"
AGENT_MEMORY_TRACES_PATH = "/mnt/data/agent-memory/traces"

for path in (AGENT_MEMORY_DB_PATH, AGENT_MEMORY_TRACES_PATH):
    if path not in sys.path:
        sys.path.append(path)

logger = logging.getLogger(__name__)


class AgentMemoryMCPExtension(MCPExtension):
    """
    Extends Hindsight FastMCP server with Agent Memory System capabilities:
    - Layer 2: Context offloading & log compaction (`context_archive`, `context_retrieve`)
    - Layer 4: Autonomous Git-tracked procedural rule editing (`rethink_memory`)
    """

    def register_tools(self, mcp: FastMCP, memory: MemoryEngine) -> None:
        logger.info("Registering Agent Memory System tools on Hindsight FastMCP server")

        try:
            from archiver import archive_content, retrieve_content
            from rule_mutator import mutate_rule
            from symbolic_brief import generate_symbolic_brief
        except ImportError as e:
            logger.error(f"Failed to import agent-memory backend modules: {e}")
            return

        @mcp.tool()
        def rethink_memory(
            rule_name: str,
            content: str,
            action: str = "update",
            bank_id: str = "default",
            session_id: str = "mcp-session",
            tool_caller: str = "hindsight_mcp",
        ) -> Dict[str, Any]:
            """
            Creates, updates, or deletes a standing repository coding standard, engineering rule,
            or workflow policy under /mnt/data/agent-memory/rules/ with Git versioning.
            """
            return mutate_rule(
                rule_name=rule_name,
                content=content,
                action=action,
                session_id=session_id,
                tool_caller=tool_caller,
            )

        @mcp.tool()
        def context_archive(
            raw_content: str,
            tool_name: str = "tool_execution",
            threshold_bytes: int = 5120,
            session_id: str = "mcp-session",
            tool_caller: str = "hindsight_mcp",
        ) -> Dict[str, Any]:
            """
            Archives verbose tool output (>5KB) to SQLite FTS5 ContentStore (traces/context.db).
            Returns compact symbolic brief containing a [ARCHIVE_ID: <uuid>] reference token.
            """
            content_size = len(raw_content.encode("utf-8"))
            if content_size < threshold_bytes:
                return {"offloaded": False, "raw_content": raw_content}

            meta = archive_content(
                raw_content=raw_content,
                tool_name=tool_name,
                session_id=session_id,
                tool_caller=tool_caller,
            )
            brief = generate_symbolic_brief(meta, raw_content)
            return {
                "offloaded": True,
                "archive_id": meta["archive_id"],
                "symbolic_brief": brief,
            }

        @mcp.tool()
        def context_retrieve(
            archive_id: str,
            session_id: str = "mcp-session",
            tool_caller: str = "hindsight_mcp",
        ) -> Dict[str, Any]:
            """
            Retrieves full raw log text, stack traces, and command output by archive_id reference token.
            """
            res = retrieve_content(
                archive_id=archive_id,
                session_id=session_id,
                tool_caller=tool_caller,
            )
            if not res:
                return {"found": False, "archive_id": archive_id, "content": None}
            return {
                "found": True,
                "archive_id": archive_id,
                "tool_name": res["tool_name"],
                "content": res["raw_content"],
                "created_at": res["created_at"],
            }
