"""
Agent Memory Extension for Hindsight FastMCP Server.

Exposes procedural rule mutation (`rethink_memory`) and context offloading
tools (`context_archive`, `context_retrieve`) directly through Hindsight's
native FastMCP server.
"""

import logging
import os
import sys
from typing import Any, Dict

from fastmcp import FastMCP

from hindsight_api import MemoryEngine
from hindsight_api.extensions.mcp import MCPExtension

# Ensure agent-memory modules are importable
_DEFAULT_BASE = os.path.join(os.sep, "mnt", "data", "agent-memory")
_BASE_PATH = os.environ.get("AGENT_MEMORY_ROOT", _DEFAULT_BASE)
AGENT_MEMORY_DB_PATH = os.path.join(_BASE_PATH, "db")
AGENT_MEMORY_TRACES_PATH = os.path.join(_BASE_PATH, "traces")

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
            or workflow policy in the procedural rules store with Git versioning.
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

        @mcp.tool()
        async def get_causal_subgraph(
            query_or_entity: str,
            bank_id: str = "hermes",
            relationship_filter: str = "",
            limit: int = 25,
        ) -> Dict[str, Any]:
            """
            Queries the causal entity knowledge graph (causes, caused_by, enables, prevents)
            connecting architecture components, failure modes, and system decisions.
            Use this tool when diagnosing complex service bugs, dependency deadlocks, or outages.
            """
            bank = bank_id or "hermes"
            search_pattern = f"%{query_or_entity.strip()}%"
            rel_filter = relationship_filter.strip().lower() if relationship_filter else None
            rel_types = [rel_filter] if rel_filter else ["causes", "caused_by", "enables", "prevents"]

            query = """
                SELECT 
                    m1.id::text as from_id,
                    m1.text as from_text,
                    l.link_type,
                    l.weight,
                    m2.id::text as to_id,
                    m2.text as to_text,
                    COALESCE(e.canonical_name, '') as entity_name
                FROM memory_links l
                JOIN memory_units m1 ON l.from_unit_id = m1.id
                JOIN memory_units m2 ON l.to_unit_id = m2.id
                LEFT JOIN entities e ON l.entity_id = e.id
                WHERE l.bank_id = $1 
                  AND l.link_type = ANY($2)
                  AND (m1.text ILIKE $3 OR m2.text ILIKE $3 OR e.canonical_name ILIKE $3)
                ORDER BY l.weight DESC, l.created_at DESC
                LIMIT $4;
            """
            try:
                pool = await memory._get_pool()
                async with pool.acquire() as conn:
                    rows = await conn.fetch(query, bank, rel_types, search_pattern, limit)

                links = []
                for r in rows:
                    links.append({
                        "from_text": r["from_text"],
                        "relationship": r["link_type"],
                        "to_text": r["to_text"],
                        "entity": r["entity_name"] or None,
                        "weight": float(r["weight"]) if r["weight"] is not None else 1.0,
                    })

                return {
                    "query": query_or_entity,
                    "bank_id": bank,
                    "causal_links": links,
                    "count": len(links),
                }
            except Exception as exc:
                logger.error(f"Failed to query causal subgraph: {exc}", exc_info=True)
                return {
                    "query": query_or_entity,
                    "bank_id": bank,
                    "error": str(exc),
                    "causal_links": [],
                    "count": 0,
                }
