import json
import os

import pytest

import quartapp


N8N_MISSING = not os.getenv("N8N_WEBHOOK_URL") or not os.getenv("N8N_BEARER_TOKEN")


@pytest.mark.asyncio
@pytest.mark.skipif(N8N_MISSING, reason="N8N_WEBHOOK_URL and N8N_BEARER_TOKEN are not configured")
async def test_n8n_integration_real_webhook(
    monkeypatch, mock_defaultazurecredential, mock_keyvault_secretclient, mock_login_required
):
    url = os.getenv("N8N_WEBHOOK_URL")
    token = os.getenv("N8N_BEARER_TOKEN")
    monkeypatch.setenv("CHAT_PROVIDER", "n8n")
    monkeypatch.setenv("N8N_WEBHOOK_URL", url)
    monkeypatch.setenv("N8N_BEARER_TOKEN", token)
    quart_app = quartapp.create_app()
    async with quart_app.test_app() as test_app:
        quart_app.config.update({"TESTING": True})
        client = test_app.test_client()

        first = await client.post("/chat/stream", json={"messages": [{"role": "user", "content": "Hello from test"}]})
        assert first.status_code == 200
        first_lines = [json.loads(line) for line in (await first.get_data()).splitlines() if line]
        first_content = "".join(
            [line.get("delta", {}).get("content") or "" for line in first_lines if line.get("delta")]
        ).strip()
        assert first_content

        history_assistant = first_content or "assistant response"
        second = await client.post(
            "/chat/stream",
            json={
                "messages": [
                    {"role": "user", "content": "Hello from test"},
                    {"role": "assistant", "content": history_assistant},
                    {"role": "user", "content": "Second turn"},
                ]
            },
        )
        assert second.status_code == 200
        second_lines = [json.loads(line) for line in (await second.get_data()).splitlines() if line]
        assert any(line.get("delta", {}).get("content") for line in second_lines)
        session_store = quart_app.blueprints["chat"].session_store
        assert session_store
        assert len(session_store.values()) == 1
