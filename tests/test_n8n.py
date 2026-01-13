import json

import httpx
import pytest

import quartapp


class MockResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class MockAsyncClient:
    def __init__(self, *args, **kwargs):
        self.calls = []
        self.response_payloads = kwargs.pop("response_payloads", None) or [{"reply": "hello"}]
        self.call_index = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False

    async def post(self, url, headers=None, json=None):
        payload = self.response_payloads[min(self.call_index, len(self.response_payloads) - 1)]
        self.calls.append({"url": url, "headers": headers or {}, "json": json})
        self.call_index += 1
        return MockResponse(payload)


@pytest.mark.asyncio
async def test_n8n_request_and_headers(monkeypatch, mock_defaultazurecredential, mock_keyvault_secretclient, mock_login_required):
    mock_client = MockAsyncClient()
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: mock_client)
    monkeypatch.setenv("CHAT_PROVIDER", "n8n")
    monkeypatch.setenv("N8N_WEBHOOK_URL", "https://example.test/webhook")
    monkeypatch.setenv("N8N_BEARER_TOKEN", "token-value")

    quart_app = quartapp.create_app()
    async with quart_app.test_app() as test_app:
        quart_app.config.update({"TESTING": True})
        client = test_app.test_client()

        response = await client.post("/chat/stream", json={"messages": [{"role": "user", "content": "Hello there"}]})

        assert response.status_code == 200
        assert mock_client.calls
        request_payload = mock_client.calls[0]["json"]
        assert request_payload["chatInput"] == "Hello there"
        assert "sessionId" in request_payload and request_payload["sessionId"]
        assert mock_client.calls[0]["headers"]["Authorization"] == "Bearer token-value"
        result_lines = [json.loads(line) for line in (await response.get_data()).splitlines()]
        assert any(line.get("delta", {}).get("content") == "hello" for line in result_lines)


@pytest.mark.asyncio
async def test_session_id_reused_and_reset(monkeypatch, mock_defaultazurecredential, mock_keyvault_secretclient, mock_login_required):
    mock_client = MockAsyncClient(response_payloads=[{"reply": "first"}, {"reply": "second"}, {"reply": "new"}])
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: mock_client)
    monkeypatch.setenv("CHAT_PROVIDER", "n8n")
    monkeypatch.setenv("N8N_WEBHOOK_URL", "https://example.test/webhook")
    monkeypatch.setenv("N8N_BEARER_TOKEN", "token-value")

    quart_app = quartapp.create_app()
    async with quart_app.test_app() as test_app:
        quart_app.config.update({"TESTING": True})
        client = test_app.test_client()

        await client.post("/chat/stream", json={"messages": [{"role": "user", "content": "Hi"}]})
        await client.post(
            "/chat/stream",
            json={
                "messages": [
                    {"role": "user", "content": "Hi"},
                    {"role": "assistant", "content": "first"},
                    {"role": "user", "content": "How are you?"},
                ]
            },
        )
        await client.post("/chat/stream", json={"messages": [{"role": "user", "content": "Start over"}]})

    first_session = mock_client.calls[0]["json"]["sessionId"]
    second_session = mock_client.calls[1]["json"]["sessionId"]
    third_session = mock_client.calls[2]["json"]["sessionId"]

    assert first_session == second_session
    assert third_session != first_session
