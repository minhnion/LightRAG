"""Read-only LightRAG retrieval facade exposed through MCP.

The facade deliberately sits at the LightRAG application boundary.  It uses
the existing workspace runtime and query methods, and never reaches into a
storage backend from an MCP tool.
"""

from __future__ import annotations

import hmac
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal

from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.types import ASGIApp, Receive, Scope, Send

from lightrag.api.routers.query_routes import QueryRequest
from lightrag.api.workspaces import WORKSPACE_HEADER, WorkspaceRuntimeManager


QueryMode = Literal["local", "global", "hybrid", "naive", "mix", "bypass"]


@dataclass(frozen=True, slots=True)
class RetrievalMcpSettings:
    """Operator-controlled safety limits for the MCP retrieval surface."""

    enabled: bool = False
    path: str = "/mcp/retrieval"
    api_key: str = ""
    max_query_chars: int = 12_000
    max_top_k: int = 50
    max_chunk_top_k: int = 50
    max_total_tokens: int = 12_000
    max_response_chars: int = 48_000
    max_history_messages: int = 12
    max_history_chars: int = 12_000
    allowed_origins: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, *, upstream_api_key: str | None = None) -> RetrievalMcpSettings:
        defaults = cls()

        def boolean(name: str, default: bool) -> bool:
            return os.getenv(name, str(default)).strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }

        def positive_int(name: str, default: int) -> int:
            value = int(os.getenv(name, str(default)))
            if value < 1:
                raise ValueError(f"{name} must be positive")
            return value

        def csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
            value = os.getenv(name, "")
            if not value.strip():
                return default
            return tuple(item.strip() for item in value.split(",") if item.strip())

        path = os.getenv("LIGHTRAG_MCP_PATH", defaults.path).strip() or defaults.path
        if not path.startswith("/"):
            path = f"/{path}"
        path = path.rstrip("/") or "/"
        return cls(
            enabled=boolean("LIGHTRAG_MCP_ENABLED", defaults.enabled),
            path=path,
            api_key=(
                os.getenv("LIGHTRAG_MCP_API_KEY", "").strip()
                or (upstream_api_key or "").strip()
            ),
            max_query_chars=positive_int(
                "LIGHTRAG_MCP_MAX_QUERY_CHARS", defaults.max_query_chars
            ),
            max_top_k=positive_int("LIGHTRAG_MCP_MAX_TOP_K", defaults.max_top_k),
            max_chunk_top_k=positive_int(
                "LIGHTRAG_MCP_MAX_CHUNK_TOP_K", defaults.max_chunk_top_k
            ),
            max_total_tokens=positive_int(
                "LIGHTRAG_MCP_MAX_TOTAL_TOKENS", defaults.max_total_tokens
            ),
            max_response_chars=positive_int(
                "LIGHTRAG_MCP_MAX_RESPONSE_CHARS", defaults.max_response_chars
            ),
            max_history_messages=positive_int(
                "LIGHTRAG_MCP_MAX_HISTORY_MESSAGES", defaults.max_history_messages
            ),
            max_history_chars=positive_int(
                "LIGHTRAG_MCP_MAX_HISTORY_CHARS", defaults.max_history_chars
            ),
            allowed_origins=csv("LIGHTRAG_MCP_ALLOWED_ORIGINS", defaults.allowed_origins),
        )


class QueryToolResult(BaseModel):
    """Bounded structured result returned by ``lightrag.query``."""

    model_config = ConfigDict(extra="forbid")

    response: str
    references: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class QueryDataToolResult(BaseModel):
    """Bounded structured result returned by ``lightrag.query_data``."""

    model_config = ConfigDict(extra="forbid")

    status: str
    message: str
    data: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class QueryStreamToolResult(BaseModel):
    """Bounded final result collected from LightRAG's NDJSON stream."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["success", "partial", "failure"]
    response: str
    references: list[dict[str, Any]] = Field(default_factory=list)
    chunk_count: int = 0
    errors: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class _McpApiKeyMiddleware:
    """Minimal service-to-service API-key guard for the mounted MCP app.

    OAuth/token-verifier integration can replace this adapter later.  Keeping
    the guard outside tool functions ensures every MCP method is protected and
    avoids forwarding the MCP credential to LightRAG's internal calls.
    """

    def __init__(
        self,
        app: ASGIApp,
        api_key: str,
        allowed_origins: tuple[str, ...] = (),
    ) -> None:
        self.app = app
        self.api_key = api_key.encode("utf-8")
        self.allowed_origins = frozenset(allowed_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        bearer = headers.get("authorization", "")
        provided = bearer[7:].strip() if bearer.lower().startswith("bearer ") else ""
        provided = provided or headers.get("x-api-key", "")
        origin = headers.get("origin", "")
        origin_allowed = not origin or origin in self.allowed_origins
        if not origin_allowed:
            response = JSONResponse(
                {"error": "MCP origin is not allowed"}, status_code=403
            )
            await response(scope, receive, send)
            return
        if not provided or not hmac.compare_digest(provided.encode("utf-8"), self.api_key):
            response = JSONResponse(
                {"error": "MCP authentication required"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class RetrievalMcpApplication:
    """Mounted ASGI app plus the lifecycle hook required by MCP sessions."""

    def __init__(self, app: ASGIApp, server: Any) -> None:
        self.app = app
        self.server = server

    @asynccontextmanager
    async def lifespan(self):
        async with self.server.session_manager.run():
            yield

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.app(scope, receive, send)


class LightRAGRetrievalFacade:
    """Translate MCP tool arguments into the existing LightRAG query API."""

    def __init__(
        self,
        rag: Any,
        workspace_manager: WorkspaceRuntimeManager,
        settings: RetrievalMcpSettings,
    ) -> None:
        self.rag = rag
        self.workspace_manager = workspace_manager
        self.settings = settings

    @asynccontextmanager
    async def _workspace(self, workspace_id: str | None):
        context = await self.workspace_manager.get_context(workspace_id or None)
        token = self.workspace_manager.activate(context)
        try:
            yield context
        finally:
            self.workspace_manager.deactivate(token)

    def _request(
        self,
        *,
        query: str,
        mode: QueryMode,
        top_k: int | None,
        chunk_top_k: int | None,
        max_total_tokens: int | None,
        conversation_history: list[dict[str, Any]] | None,
        response_type: str | None,
        include_chunk_content: bool,
    ) -> QueryRequest:
        normalized_query = query.strip()
        if not normalized_query:
            raise ValueError("query cannot be empty")
        if len(normalized_query) > self.settings.max_query_chars:
            raise ValueError(
                f"query exceeds {self.settings.max_query_chars} characters"
            )
        history = list(conversation_history or [])[: self.settings.max_history_messages]
        history_chars = 0
        bounded_history: list[dict[str, Any]] = []
        for message in history:
            if not isinstance(message, dict):
                raise ValueError("conversation_history items must be objects")
            role = str(message.get("role", "")).strip()
            content = str(message.get("content", ""))
            if not role:
                raise ValueError("conversation_history messages require a role")
            remaining = self.settings.max_history_chars - history_chars
            if remaining <= 0:
                break
            content = content[:remaining]
            history_chars += len(content)
            bounded_history.append({"role": role, "content": content})
        return QueryRequest(
            query=normalized_query,
            mode=mode,
            top_k=min(top_k, self.settings.max_top_k) if top_k else None,
            chunk_top_k=(
                min(chunk_top_k, self.settings.max_chunk_top_k)
                if chunk_top_k
                else None
            ),
            max_total_tokens=(
                min(max_total_tokens, self.settings.max_total_tokens)
                if max_total_tokens
                else self.settings.max_total_tokens
            ),
            conversation_history=bounded_history or None,
            response_type=response_type.strip() if response_type else None,
            include_references=True,
            include_chunk_content=include_chunk_content,
        )

    async def query(
        self,
        *,
        workspace_id: str | None,
        query: str,
        mode: QueryMode = "mix",
        top_k: int | None = None,
        chunk_top_k: int | None = None,
        max_total_tokens: int | None = None,
        conversation_history: list[dict[str, Any]] | None = None,
        response_type: str | None = None,
    ) -> QueryToolResult:
        request = self._request(
            query=query,
            mode=mode,
            top_k=top_k,
            chunk_top_k=chunk_top_k,
            max_total_tokens=max_total_tokens,
            conversation_history=conversation_history,
            response_type=response_type,
            include_chunk_content=False,
        )
        async with self._workspace(workspace_id):
            result = await self.rag.aquery_llm(
                request.query,
                param=request.to_query_params(False),
            )
        result = result if isinstance(result, dict) else {}
        llm_response = result.get("llm_response", {}) or {}
        data = result.get("data", {}) or {}
        return QueryToolResult(
            response=str(llm_response.get("content", "") or "No relevant context found for the query."),
            references=list(data.get("references", []) or []),
            metadata={"query_mode": request.mode, "retrieval": "lightrag"},
        )

    async def query_data(
        self,
        *,
        workspace_id: str | None,
        query: str,
        mode: QueryMode = "mix",
        top_k: int | None = None,
        chunk_top_k: int | None = None,
        max_entity_tokens: int | None = None,
        max_relation_tokens: int | None = None,
        max_total_tokens: int | None = None,
    ) -> QueryDataToolResult:
        request = self._request(
            query=query,
            mode=mode,
            top_k=top_k,
            chunk_top_k=chunk_top_k,
            max_total_tokens=max_total_tokens,
            conversation_history=None,
            response_type=None,
            include_chunk_content=True,
        )
        request = request.model_copy(
            update={
                "max_entity_tokens": max_entity_tokens,
                "max_relation_tokens": max_relation_tokens,
            }
        )
        if request.max_entity_tokens:
            request.max_entity_tokens = min(
                request.max_entity_tokens, self.settings.max_total_tokens
            )
        if request.max_relation_tokens:
            request.max_relation_tokens = min(
                request.max_relation_tokens, self.settings.max_total_tokens
            )
        async with self._workspace(workspace_id):
            result = await self.rag.aquery_data(
                request.query,
                param=request.to_query_params(False),
            )
        if not isinstance(result, dict):
            return QueryDataToolResult(
                status="failure",
                message="LightRAG returned an invalid structured result",
            )
        metadata = dict(result.get("metadata", {}) or {})
        metadata.setdefault("query_mode", request.mode)
        metadata["retrieval"] = "lightrag"
        return QueryDataToolResult(
            status=str(result.get("status", "failure")),
            message=str(result.get("message", "")),
            data=dict(result.get("data", {}) or {}),
            metadata=metadata,
        )

    async def query_stream(
        self,
        *,
        workspace_id: str | None,
        query: str,
        mode: QueryMode = "mix",
        top_k: int | None = None,
        chunk_top_k: int | None = None,
        max_total_tokens: int | None = None,
        conversation_history: list[dict[str, Any]] | None = None,
        response_type: str | None = None,
    ) -> QueryStreamToolResult:
        """Collect `/query/stream` into one bounded MCP tool result.

        MCP tool calls are request/response operations. The underlying
        LightRAG async iterator is consumed here so clients receive a stable
        structured result while the original streaming HTTP route remains
        unchanged. Progress notifications can be added later without
        changing this result contract.
        """

        request = self._request(
            query=query,
            mode=mode,
            top_k=top_k,
            chunk_top_k=chunk_top_k,
            max_total_tokens=max_total_tokens,
            conversation_history=conversation_history,
            response_type=response_type,
            include_chunk_content=False,
        )
        async with self._workspace(workspace_id):
            result = await self.rag.aquery_llm(
                request.query,
                param=request.to_query_params(True),
            )
        result = result if isinstance(result, dict) else {}
        llm_response = result.get("llm_response", {}) or {}
        data = result.get("data", {}) or {}
        references = list(data.get("references", []) or [])[: self.settings.max_top_k]
        errors: list[str] = []
        parts: list[str] = []
        response_chars = 0
        truncated = False
        response_iterator = llm_response.get("response_iterator")
        if response_iterator is not None:
            try:
                async for chunk in response_iterator:
                    text = str(chunk)
                    remaining = self.settings.max_response_chars - response_chars
                    if remaining <= 0:
                        truncated = True
                        break
                    bounded = text[:remaining]
                    parts.append(bounded)
                    response_chars += len(bounded)
                    truncated = truncated or len(bounded) < len(text)
            except Exception as exc:  # return partial output with explicit status
                errors.append(str(exc)[:500])
        else:
            response = str(llm_response.get("content", "") or "")
            parts.append(response[: self.settings.max_response_chars])
            truncated = len(response) > self.settings.max_response_chars

        response = "".join(parts)
        if not response and not errors:
            response = "No relevant context found for the query."
        status: Literal["success", "partial", "failure"] = "success"
        if errors:
            status = "partial" if response else "failure"
        return QueryStreamToolResult(
            status=status,
            response=response,
            references=references,
            chunk_count=len(parts),
            errors=errors,
            metadata={
                "query_mode": request.mode,
                "retrieval": "lightrag",
                "streamed": response_iterator is not None,
                "truncated": truncated,
            },
        )


def _workspace_id_from_context(ctx: Any) -> str | None:
    request = getattr(getattr(ctx, "request_context", None), "request", None)
    headers = getattr(request, "headers", None)
    return headers.get(WORKSPACE_HEADER) if headers is not None else None


def create_retrieval_mcp_app(
    rag: Any,
    workspace_manager: WorkspaceRuntimeManager,
    *,
    upstream_api_key: str | None = None,
    settings: RetrievalMcpSettings | None = None,
) -> RetrievalMcpApplication:
    """Create the mounted Streamable HTTP MCP application.

    The returned app is mounted by ``lightrag_server.create_app`` at the
    configured path.  The inner MCP route is ``/`` because the outer mount owns
    the public URL.
    """

    from mcp.server.fastmcp import Context, FastMCP
    from mcp.types import ToolAnnotations

    # FastMCP evaluates postponed annotations against the module namespace.
    # The SDK is optional for normal LightRAG startup, so bind the runtime
    # Context type only when this facade is enabled.
    globals()["Context"] = Context

    settings = settings or RetrievalMcpSettings.from_env(
        upstream_api_key=upstream_api_key
    )
    if not settings.api_key:
        raise RuntimeError(
            "LIGHTRAG_MCP_ENABLED requires LIGHTRAG_MCP_API_KEY or LIGHTRAG_API_KEY"
        )
    facade = LightRAGRetrievalFacade(rag, workspace_manager, settings)
    server = FastMCP(
        "LightRAG Retrieval",
        instructions=(
            "Read-only retrieval from the authenticated LightRAG workspace. "
            "The caller cannot select a workspace through tool arguments."
        ),
        host="0.0.0.0",
        streamable_http_path="/",
        json_response=True,
        stateless_http=True,
    )
    annotations = ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )

    @server.tool(
        name="lightrag.query",
        title="LightRAG synthesized retrieval",
        description=(
            "Run a read-only LightRAG query and return a synthesized answer with "
            "source references. Workspace scope comes from the authenticated request."
        ),
        annotations=annotations,
        structured_output=True,
    )
    async def query(
        ctx: Context,
        query: str,
        mode: QueryMode = "mix",
        top_k: int | None = None,
        chunk_top_k: int | None = None,
        max_total_tokens: int | None = None,
        conversation_history: list[dict[str, Any]] | None = None,
        response_type: str | None = None,
    ) -> QueryToolResult:
        return await facade.query(
            workspace_id=_workspace_id_from_context(ctx),
            query=query,
            mode=mode,
            top_k=top_k,
            chunk_top_k=chunk_top_k,
            max_total_tokens=max_total_tokens,
            conversation_history=conversation_history,
            response_type=response_type,
        )

    @server.tool(
        name="lightrag.query_data",
        title="LightRAG structured retrieval",
        description=(
            "Run a read-only LightRAG retrieval and return structured entities, "
            "relationships, chunks, and references for evidence construction."
        ),
        annotations=annotations,
        structured_output=True,
    )
    async def query_data(
        ctx: Context,
        query: str,
        mode: QueryMode = "mix",
        top_k: int | None = None,
        chunk_top_k: int | None = None,
        max_entity_tokens: int | None = None,
        max_relation_tokens: int | None = None,
        max_total_tokens: int | None = None,
    ) -> QueryDataToolResult:
        return await facade.query_data(
            workspace_id=_workspace_id_from_context(ctx),
            query=query,
            mode=mode,
            top_k=top_k,
            chunk_top_k=chunk_top_k,
            max_entity_tokens=max_entity_tokens,
            max_relation_tokens=max_relation_tokens,
            max_total_tokens=max_total_tokens,
        )

    @server.tool(
        name="lightrag.query_stream",
        title="LightRAG streaming retrieval",
        description=(
            "Run a read-only LightRAG streaming query. The NDJSON stream is "
            "collected into a bounded final response with references and status."
        ),
        annotations=annotations,
        structured_output=True,
    )
    async def query_stream(
        ctx: Context,
        query: str,
        mode: QueryMode = "mix",
        top_k: int | None = None,
        chunk_top_k: int | None = None,
        max_total_tokens: int | None = None,
        conversation_history: list[dict[str, Any]] | None = None,
        response_type: str | None = None,
    ) -> QueryStreamToolResult:
        return await facade.query_stream(
            workspace_id=_workspace_id_from_context(ctx),
            query=query,
            mode=mode,
            top_k=top_k,
            chunk_top_k=chunk_top_k,
            max_total_tokens=max_total_tokens,
            conversation_history=conversation_history,
            response_type=response_type,
        )

    app = server.streamable_http_app()
    return RetrievalMcpApplication(
        _McpApiKeyMiddleware(app, settings.api_key, settings.allowed_origins),
        server,
    )


__all__ = [
    "LightRAGRetrievalFacade",
    "QueryDataToolResult",
    "QueryToolResult",
    "QueryStreamToolResult",
    "RetrievalMcpSettings",
    "RetrievalMcpApplication",
    "create_retrieval_mcp_app",
]
