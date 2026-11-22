from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from lightrag.mcp.retrieval import (
    LightRAGRetrievalFacade,
    RetrievalMcpSettings,
    create_retrieval_mcp_app,
)


WORKSPACE_A = "11111111-1111-1111-1111-111111111111"


class FakeWorkspaceManager:
    def __init__(self):
        self.seen_workspace_ids = []

    async def get_context(self, workspace_id):
        self.seen_workspace_ids.append(workspace_id)
        return SimpleNamespace(workspace=SimpleNamespace(id=workspace_id or "default"))

    def activate(self, context):
        return context

    def deactivate(self, token):
        return None


class FakeRAG:
    async def aquery_llm(self, query, *, param):
        if param.stream:
            async def chunks():
                yield "streamed "
                yield f"answer for {query}"

            return {
                "llm_response": {
                    "is_streaming": True,
                    "response_iterator": chunks(),
                },
                "data": {"references": [{"reference_id": "r1", "file_path": "a.pdf"}]},
            }
        return {
            "llm_response": {"content": f"answer for {query}"},
            "data": {"references": [{"reference_id": "r1", "file_path": "a.pdf"}]},
        }

    async def aquery_data(self, query, *, param):
        return {
            "status": "success",
            "message": "Query executed successfully",
            "data": {
                "chunks": [
                    {
                        "chunk_id": "chunk-a",
                        "content": f"evidence for {query}",
                        "file_path": "a.pdf",
                        "reference_id": "r1",
                    }
                ],
                "references": [{"reference_id": "r1", "file_path": "a.pdf"}],
            },
            "metadata": {"query_mode": param.mode},
        }


def _app():
    manager = FakeWorkspaceManager()
    runtime = create_retrieval_mcp_app(
        FakeRAG(),
        manager,
        settings=RetrievalMcpSettings(enabled=True, api_key="secret", max_top_k=5),
    )

    @asynccontextmanager
    async def lifespan(app):
        async with runtime.lifespan():
            yield

    app = FastAPI(lifespan=lifespan)
    app.state.manager = manager
    app.mount("/mcp/retrieval", runtime)
    return app, manager


def test_mcp_requires_auth_and_exposes_only_two_tools():
    app, _manager = _app()
    with TestClient(app) as client:
        denied = client.post(
            "/mcp/retrieval/",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        assert denied.status_code == 401

        response = client.post(
            "/mcp/retrieval/",
            headers={
                "Authorization": "Bearer secret",
                "Accept": "application/json, text/event-stream",
                "X-LightRAG-Workspace-ID": WORKSPACE_A,
            },
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        assert response.status_code == 200
        names = {tool["name"] for tool in response.json()["result"]["tools"]}
        assert names == {
            "lightrag.query",
            "lightrag.query_data",
            "lightrag.query_stream",
        }
        for tool in response.json()["result"]["tools"]:
            assert "workspace_id" not in tool["inputSchema"]["properties"]
            assert tool["annotations"]["readOnlyHint"] is True


def test_facade_translates_query_and_enforces_limits():
    import pytest

    manager = FakeWorkspaceManager()
    facade = LightRAGRetrievalFacade(
        FakeRAG(),
        manager,
        RetrievalMcpSettings(max_total_tokens=10, max_query_chars=8),
    )

    async def run():
        result = await facade.query_data(workspace_id=WORKSPACE_A, query="evidence")
        assert result.status == "success"
        assert result.data["chunks"][0]["reference_id"] == "r1"
        streamed = await facade.query_stream(
            workspace_id=WORKSPACE_A,
            query="evidence",
        )
        assert streamed.status == "success"
        assert streamed.response == "streamed answer for evidence"
        assert streamed.chunk_count == 2

    import asyncio

    asyncio.run(run())
    assert manager.seen_workspace_ids == [WORKSPACE_A, WORKSPACE_A]

    with pytest.raises(ValueError, match="exceeds"):
        facade._request(
            query="too long query",
            mode="mix",
            top_k=None,
            chunk_top_k=None,
            max_total_tokens=None,
            conversation_history=None,
            response_type=None,
            include_chunk_content=False,
        )
