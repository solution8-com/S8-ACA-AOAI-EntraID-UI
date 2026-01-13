import json

import httpx
import pytest

import quartapp


class MockResponse:
    def __init__(self, payload):
        """
        Initialize a mock HTTP response containing the given payload and a 200 status code.
        
        Parameters:
            payload: JSON-serializable object to be returned by the response's `json()` method.
        """
        self._payload = payload
        self.status_code = 200

    def json(self):
        """
        Retrieve the stored JSON payload.
        
        Returns:
            The payload object that was provided to the MockResponse when created (the stored JSON-parsable value).
        """
        return self._payload

    def raise_for_status(self):
        """
        Do nothing when called; intentionally does not raise an exception for error HTTP statuses.
        
        Used in tests to simulate an HTTP response whose status is ignored.
        """
        return


class MockAsyncClient:
    def __init__(self, *args, **kwargs):
        """
        Initialize the mock async client.
        
        Initializes an empty call history, sets the sequence of response payloads (defaults to [{"reply": "hello"}] if not provided), and resets the call index to 0.
        
        Parameters:
            *args: Ignored positional arguments for compatibility with httpx.AsyncClient.
            **kwargs: May include `response_payloads` (list) to specify the mock responses returned by `post`.
        """
        self.calls = []
        response_payloads = kwargs.pop("response_payloads", None)
        self.response_payloads = [{"reply": "hello"}] if response_payloads is None else response_payloads
        self.call_index = 0

    async def __aenter__(self):
        """
        Enter the async context manager for the mock HTTP client.
        
        Returns:
            The mock async client instance (`self`) to be used within the async with block.
        """
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """
        Async context manager exit that allows exceptions to propagate.
        
        Returns:
            False: indicates the context manager does not suppress exceptions.
        """
        return False

    async def post(self, url, headers=None, json=None):
        """
        Send a POST-like request using the mock client and record the call.
        
        Parameters:
            url (str): The target request URL.
            headers (dict | None): Optional request headers; empty dict is recorded if None.
            json (Any | None): Optional JSON payload to include in the recorded call.
        
        Returns:
            MockResponse: A response object whose payload is selected from the client's configured response_payloads.
        
        Notes:
            The call is recorded to `self.calls` as a dict with keys `url`, `headers`, and `json`, and the client's internal `call_index` is incremented. If `call_index` exceeds the last response payload index, the last payload is reused.
        """
        payload = self.response_payloads[min(self.call_index, len(self.response_payloads) - 1)]
        self.calls.append({"url": url, "headers": headers or {}, "json": json})
        self.call_index += 1
        return MockResponse(payload)


@pytest.mark.asyncio
async def test_n8n_request_and_headers(monkeypatch, mock_defaultazurecredential, mock_keyvault_secretclient, mock_login_required):
    """
    Verify that a POST to /chat/stream produces the correct n8n webhook request and returns the webhook's streamed reply.
    
    Asserts that the outgoing webhook JSON contains the user's message as `chatInput` and a non-empty `sessionId`, that the request includes an `Authorization: Bearer <token>` header, and that the streamed response body contains at least one line with `delta.content` equal to "hello".
    """
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
    """
    Verifies that sessionId is reused for consecutive requests within the same conversation and that a new sessionId is generated after a reset.
    
    Uses a mocked n8n HTTP client that returns a sequence of replies, sends three /chat/stream requests (two continuing the session and one that starts a new session), and asserts that the sessionId in the first two requests is the same and that the third request's sessionId differs.
    """
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