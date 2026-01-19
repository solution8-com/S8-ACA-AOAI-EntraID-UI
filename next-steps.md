## Running the real n8n webhook integration test locally

1. Export environment variables:
   - `CHAT_PROVIDER=n8n`
   - `N8N_WEBHOOK_URL=<your webhook URL>`
   - `N8N_BEARER_TOKEN=N8N_BEARER_TOKEN__REPLACE_ME`
   - Optional: `N8N_TIMEOUT_MS` if you need a custom timeout.
2. Ensure your n8n workflow is reachable from the test environment.
3. Run the gated test:

   ```bash
   pytest tests/test_n8n_integration.py
   ```

If the variables are not set, the test is skipped. Replace `N8N_BEARER_TOKEN__REPLACE_ME` with a valid bearer token before running.
