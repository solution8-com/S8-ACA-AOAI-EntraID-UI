import asyncio
import json
import os
import time
import uuid
from functools import wraps

import azure.identity.aio
import httpx
import openai
import redis.asyncio as redis
from redis.exceptions import RedisError
from azure.keyvault.secrets.aio import SecretClient
from identity.quart import Auth
from quart import (
    Blueprint,
    Response,
    current_app,
    render_template,
    request,
    stream_with_context,
)

bp = Blueprint("chat", __name__, template_folder="templates", static_folder="static")


def get_azure_credential():
    if not hasattr(bp, "azure_credential"):
        bp.azure_credential = azure.identity.aio.DefaultAzureCredential(exclude_shared_token_cache_credential=True)
    return bp.azure_credential


async def setup_redis():
    """
    Configure and return a Redis client, choosing between Azure Redis (when RUNNING_IN_PRODUCTION is set) and a local Redis instance.
    
    This function sets bp.redis_username and, when using Azure Redis, obtains and stores bp.redis_token using the application Azure credential. It reads AZURE_REDIS_HOST and AZURE_REDIS_USER for Azure configuration; otherwise it uses localhost:6379 with no authentication. The returned Redis client has response decoding enabled.
    Returns:
        redis.Redis: A configured Redis client (decode_responses=True).
    """
    azure_scope = "https://redis.azure.com/.default"
    use_azure_redis = os.getenv("RUNNING_IN_PRODUCTION") is not None
    if use_azure_redis:
        host = os.getenv("AZURE_REDIS_HOST")
        bp.redis_username = os.getenv("AZURE_REDIS_USER")
        port = 6380
        ssl = True
    else:
        host = "localhost"
        port = 6379
        bp.redis_username = None
        password = None
        ssl = False
    if use_azure_redis:
        current_app.logger.info("Using Azure Redis with default credential")
        bp.redis_token = await get_azure_credential().get_token(azure_scope)
        password = bp.redis_token.token
    else:
        current_app.logger.info("Using Redis with username and password")
    return redis.Redis(
        host=host, ssl=ssl, port=port, username=bp.redis_username, password=password, decode_responses=True
    )


def _chat_provider():
    """
    Determine the configured chat provider.

    Returns:
        str: The chat provider name in lowercase; defaults to "aoai" when CHAT_PROVIDER is not set.
    """
    return (os.getenv("CHAT_PROVIDER") or "aoai").lower()


@bp.before_app_serving
async def configure_clients():
    """
    Initialize and configure external clients and application resources used by the chat blueprint.
    
    Sets bp.chat_provider and, depending on its value, configures either an OpenAI/Azure OpenAI client or n8n integration (webhook URL, bearer token, timeout, in-memory session store and lock). Initializes Redis and registers it as the Flask/Quart session store, determines the OAuth redirect URI for production vs. local development, retrieves the Azure auth client secret from Key Vault, and constructs bp.auth with that secret.
    Raises:
        ValueError: If using Azure OpenAI and either `AZURE_OPENAI_ENDPOINT` or `AZURE_OPENAI_CHATGPT_DEPLOYMENT` is not set.
    """
    client_args = {}
    bp.session_store = {}
    bp.session_store_lock = asyncio.Lock()
    bp.chat_provider = _chat_provider()
    if bp.chat_provider == "aoai":
        if os.getenv("LOCAL_OPENAI_ENDPOINT"):
            # Use a local endpoint like llamafile server
            current_app.logger.info("Using local OpenAI-compatible API with no key")
            client_args["api_key"] = "no-key-required"
            client_args["base_url"] = os.getenv("LOCAL_OPENAI_ENDPOINT")
            bp.openai_client = openai.AsyncOpenAI(
                **client_args,
            )
        else:
            # Use an Azure OpenAI endpoint instead,
            # either with a key or with keyless authentication
            if os.getenv("AZURE_OPENAI_KEY"):
                # Authenticate using an Azure OpenAI API key
                # This is generally discouraged, but is provided for developers
                # that want to develop locally inside the Docker container.
                current_app.logger.info("Using Azure OpenAI with key")
                client_args["api_key"] = os.getenv("AZURE_OPENAI_KEY")
            else:
                # Authenticate using the default Azure credential chain
                # See https://docs.microsoft.com/azure/developer/python/azure-sdk-authenticate#defaultazurecredential
                # This will *not* work inside a Docker container.
                current_app.logger.info("Using Azure OpenAI with default credential")
                client_args["azure_ad_token_provider"] = azure.identity.aio.get_bearer_token_provider(
                    get_azure_credential(), "https://cognitiveservices.azure.com/.default"
                )
            if not os.getenv("AZURE_OPENAI_ENDPOINT"):
                raise ValueError("AZURE_OPENAI_ENDPOINT is required for Azure OpenAI")
            if not os.getenv("AZURE_OPENAI_CHATGPT_DEPLOYMENT"):
                raise ValueError("AZURE_OPENAI_CHATGPT_DEPLOYMENT is required for Azure OpenAI")
            bp.openai_client = openai.AsyncAzureOpenAI(
                api_version=os.getenv("AZURE_OPENAI_API_VERSION") or "2024-02-15-preview",
                azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
                **client_args,
            )
    elif bp.chat_provider == "n8n":
        bp.n8n_webhook_url = os.getenv("N8N_WEBHOOK_URL")
        bp.n8n_bearer_token = os.getenv("N8N_BEARER_TOKEN")
        if not bp.n8n_webhook_url:
            raise ValueError("N8N_WEBHOOK_URL is required when CHAT_PROVIDER=n8n")
        if not bp.n8n_bearer_token:
            raise ValueError("N8N_BEARER_TOKEN is required when CHAT_PROVIDER=n8n")
        timeout_ms = os.getenv("N8N_TIMEOUT_MS")
        try:
            timeout_ms = int(timeout_ms) if timeout_ms else 30000
        except ValueError:
            current_app.logger.warning("Invalid N8N_TIMEOUT_MS value %r, defaulting to 30000", timeout_ms)
            timeout_ms = 30000
        bp.n8n_timeout = timeout_ms / 1000
    else:
        raise ValueError("CHAT_PROVIDER must be either 'aoai' or 'n8n'")

    bp.cache = await setup_redis()
    current_app.config["SESSION_TYPE"] = "redis"
    current_app.config["SESSION_REDIS"] = bp.cache

    redirect_uri = "http://localhost:50505/redirect"
    if os.getenv("RUNNING_IN_PRODUCTION"):
        redirect_uri = (
            f"https://{os.environ['CONTAINER_APP_NAME']}.{os.environ['CONTAINER_APP_ENV_DNS_SUFFIX']}/redirect"
        )
        current_app.logger.warn(f"Using production redirect URI: {redirect_uri}")

    AZURE_AUTH_CLIENT_SECRET_NAME = os.getenv("AZURE_AUTH_CLIENT_SECRET_NAME")
    AZURE_KEY_VAULT_NAME = os.getenv("AZURE_KEY_VAULT_NAME")
    async with SecretClient(
        vault_url=f"https://{AZURE_KEY_VAULT_NAME}.vault.azure.net", credential=get_azure_credential()
    ) as key_vault_client:
        auth_client_secret = (await key_vault_client.get_secret(AZURE_AUTH_CLIENT_SECRET_NAME)).value

    bp.auth = Auth(
        current_app,
        authority=os.getenv("AZURE_AUTH_AUTHORITY"),
        client_id=os.getenv("AZURE_AUTH_CLIENT_ID"),
        client_credential=auth_client_secret,
        redirect_uri=redirect_uri,
    )


def login_required(f):
    """Decorator to require login for a route."""

    @wraps(f)
    async def decorated_function(*args, **kwargs):
        return await bp.auth.login_required(f)(*args, **kwargs)

    return decorated_function


@bp.before_request
async def ensure_redis_token():
    if not hasattr(bp, "redis_token"):
        return
    redis_cache = bp.cache
    redis_token = bp.redis_token
    if redis_token.expires_on < time.time() + 60:
        current_app.logger.info("Refreshing token...")
        tmp_token = await get_azure_credential().get_token("https://redis.azure.com/.default")
        if tmp_token:
            azure_token = tmp_token
        await redis_cache.execute_command("AUTH", bp.redis_username, azure_token.token)
        current_app.logger.info("Successfully refreshed token.")


@bp.after_app_serving
async def shutdown_openai():
    """
    Close and release the OpenAI client and Azure credential stored on the blueprint.
    
    If an OpenAI client is attached to the blueprint as `bp.openai_client`, it will be closed; the blueprint's `bp.azure_credential` is then closed to release any held resources.
    """
    if hasattr(bp, "openai_client"):
        await bp.openai_client.close()
    await bp.azure_credential.close()


@bp.get("/")
@login_required
async def index(*, context):
    return await render_template("index.html", user=context["user"]["name"])


@bp.post("/chat/stream")
@login_required
async def chat_handler(*, context):
    """
    Handle an incoming chat request and return a streaming HTTP response of assistant message chunks.
    
    Processes the JSON request body for "messages" and streams back assistant responses as JSON lines. When the blueprint is configured with provider "n8n", responses are produced by the configured n8n webhook and streamed through the handler. Otherwise, messages are sent to the configured OpenAI/Azure OpenAI chat completion endpoint and the first choice from each streamed event is emitted as a JSON line. On error, a single JSON line containing an "error" field is emitted.
    
    Parameters:
        context (dict): Request context containing authenticated user information and other request-scoped state.
    
    Returns:
        quart.Response: A streaming HTTP response that yields newline-terminated JSON objects representing assistant delta/content chunks or an error object.
    """
    body = await request.get_json()
    messages = body.get("messages") if isinstance(body, dict) else None
    if not isinstance(messages, list) or not messages:
        return Response(json.dumps({"error": "messages array is required"}), status=400, mimetype="application/json")
    if messages[-1].get("role") != "user" or "content" not in messages[-1]:
        return Response(
            json.dumps({"error": "last message must be a user message with content"}),
            status=400,
            mimetype="application/json",
        )
    request_messages = messages

    @stream_with_context
    async def response_stream():
        """
        Produce an asynchronous stream of newline-terminated JSON strings representing chat response chunks or an error.
        
        When the configured chat provider is "n8n", yields events produced by stream_n8n(request_messages, context). Otherwise, requests a streamed completion from the OpenAI/Azure client and yields the first choice from each streaming event as a JSON line; on failure yields a single JSON error line.
         
        Returns:
            Newline-terminated JSON strings (`str`) for each streamed assistant chunk or a single error object.
        """
        if getattr(bp, "chat_provider", "aoai") == "n8n":
            async for event in stream_n8n(request_messages, context):
                yield event
        else:
            # This sends all messages, so API request may exceed token limits
            all_messages = [
                {"role": "system", "content": "You are a helpful assistant."},
            ] + request_messages

            chat_coroutine = bp.openai_client.chat.completions.create(
                # Azure Open AI takes the deployment name as the model name
                model=os.environ["AZURE_OPENAI_CHATGPT_DEPLOYMENT"],
                messages=all_messages,
                stream=True,
            )
            try:
                async for event in await chat_coroutine:
                    event_dict = event.model_dump()
                    if event_dict["choices"]:
                        yield json.dumps(event_dict["choices"][0], ensure_ascii=False) + "\n"
            except Exception as e:
                current_app.logger.error(e)
                yield json.dumps({"error": str(e)}, ensure_ascii=False) + "\n"

    return Response(response_stream())


def _n8n_message_text(n8n_response):
    """
    Extracts a human-readable text reply from an n8n webhook response payload.
    
    Parameters:
        n8n_response (str | dict): The raw response body returned by an n8n webhook, either a plain string or a dictionary that may contain text in several common keys or inside a "body" sub-dictionary.
    
    Returns:
        str: The first found string value from one of the keys "reply", "response", "answer", "text", "message", "output", or "result" (searched at the top level, then inside `body`), or the `body` string if `body` is a string. Returns an empty string if no suitable text is found.
    """
    def _extract_from_dict(source):
        for key in ("reply", "response", "answer", "text", "message", "output", "result"):
            value = source.get(key)
            if isinstance(value, str):
                return value
        return None

    if isinstance(n8n_response, str):
        return n8n_response
    if isinstance(n8n_response, dict):
        direct_value = _extract_from_dict(n8n_response)
        if direct_value:
            return direct_value
        body = n8n_response.get("body")
        if isinstance(body, dict):
            nested_value = _extract_from_dict(body)
            if nested_value:
                return nested_value
        if isinstance(body, str):
            return body
    return ""


def _user_key_from_context(context):
    """
    Derives a stable user key from the provided request context.
    
    Parameters:
        context (dict | None): A context mapping that may contain a "user" mapping with identity fields.
    
    Returns:
        str: The first available identifier from the user mapping in this order: `oid`, `id`, `preferred_username`, `name`. If none are present, returns a per-process fallback key (a hex UUID) stored on the module blueprint object; the fallback key is created and cached on first use.
    """
    user = context.get("user", {}) if context else {}
    if not hasattr(bp, "anonymous_user_key"):
        bp.anonymous_user_key = uuid.uuid4().hex
    fallback_key = bp.anonymous_user_key
    return user.get("oid") or user.get("id") or user.get("preferred_username") or user.get("name") or fallback_key


async def _session_id_for_user(context, messages):
    """
    Obtain or create a session ID for the current user, using the blueprint's session lock if configured to serialize access.
    
    Parameters:
        context (dict): Request context containing user information used to identify the user.
        messages (list): List of chat messages for the current request used to determine whether this is a new chat.
    
    Returns:
        str: A session ID associated with the user; returns an existing session ID when available or creates and returns a new one.
    """
    lock = getattr(bp, "session_store_lock", None)
    if lock:
        async with lock:
            return await _session_id_for_user_inner(context, messages)
    return await _session_id_for_user_inner(context, messages)


async def _session_id_for_user_inner(context, messages):
    """
    Determine or create a per-user chat session ID based on the provided user context and message history.
    
    If an existing session ID is present in the in-memory session store or Redis, that value is returned. If no session exists or the messages indicate a new chat (no assistant turn present), a new session ID (UUID hex) is generated, saved to the in-memory store and (if available) persisted to Redis. Redis errors are ignored and result in using the in-memory fallback.
    
    Parameters:
        context (dict): Request context containing user identity fields used to derive a user key.
        messages (list): Sequence of message objects (dict-like) where each message may include a "role" key.
    
    Returns:
        str: The session ID (UUID hex) associated with the user for this chat.
    """
    user_key = _user_key_from_context(context)
    session_store = getattr(bp, "session_store", {})
    # A new chat is inferred when there are no assistant turns yet in the submitted history.
    new_chat = not any(message.get("role") == "assistant" for message in messages)
    cached_session = session_store.get(user_key)
    redis_key = f"chat_session:{user_key}"
    try:
        cached_session = await bp.cache.get(redis_key) or cached_session
    except RedisError as exc:  # pragma: no cover - fallback path for missing redis
        current_app.logger.warning("Redis session tracking unavailable, using in-memory fallback (%s)", exc.__class__.__name__)
    if new_chat or not cached_session:
        cached_session = uuid.uuid4().hex
        session_store[user_key] = cached_session
        try:
            await bp.cache.set(redis_key, cached_session)
        except RedisError:
            # If Redis is unavailable, fall back to in-memory session tracking.
            pass
    return cached_session


async def stream_n8n(request_messages, context):
    """
    Stream a response from the configured n8n webhook as assistant message chunks.
    
    Sends the last user message and a session ID to the configured n8n webhook, yields a three-part streaming assistant message constructed by build_assistant_message for the webhook's reply, and yields a friendly error message if the webhook is not configured or the request fails.
    
    Parameters:
        request_messages (list): Sequence of message dicts from the client; the last message's "content" is sent as the chat input.
        context (dict): Request context containing user information used to resolve or create a session ID.
    
    Returns:
        Iterator[str]: An async iterator that yields JSON-line strings representing streaming assistant chunks (start, content, end).
    """
    if not getattr(bp, "n8n_webhook_url", None):
        error_text = "n8n webhook URL is not configured."
        for chunk in build_assistant_message(error_text):
            yield chunk
        return

    session_id = await _session_id_for_user(context, request_messages)
    chat_input = request_messages[-1].get("content") if request_messages else ""
    payload = {"chatInput": chat_input, "sessionId": session_id}
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if bp.n8n_bearer_token:
        headers["Authorization"] = f"Bearer {bp.n8n_bearer_token}"
    try:
        async with httpx.AsyncClient(timeout=bp.n8n_timeout) as client:
            response = await client.post(bp.n8n_webhook_url, headers=headers, json=payload)
            response.raise_for_status()
            try:
                parsed = response.json()
            except json.JSONDecodeError as exc:
                current_app.logger.error("n8n response JSON decode error: %s", exc)
                parsed = None
            message_text = _n8n_message_text(parsed) if parsed is not None else ""
            if not message_text:
                current_app.logger.warning("n8n response missing expected text content")
                message_text = "The chat service returned an empty response."
            for chunk in build_assistant_message(message_text):
                yield chunk
    except httpx.HTTPError as e:
        current_app.logger.error("n8n HTTP error: %s", e)
        error_text = "Sorry, I could not reach the chat service. Please try again."
        for chunk in build_assistant_message(error_text):
            yield chunk


def build_assistant_message(text):
    """
    Yield three JSON-formatted streaming chunks that represent an assistant message.
    
    Produces a start chunk (metadata and role), a content chunk containing `text`, and a final chunk with finish_reason "stop". Each yielded value is a JSON string (ensure_ascii=False) terminated with a newline.
    
    Parameters:
        text (str): The assistant message content to emit in the content chunk.
    
    Returns:
        generator: Yields three JSON string lines: start chunk, content chunk (containing `text`), and end chunk.
    """
    start_chunk = {
        "delta": {"content": None, "function_call": None, "role": "assistant", "tool_calls": None},
        "finish_reason": None,
        "index": 0,
        "logprobs": None,
        "content_filter_results": {},
    }
    yield json.dumps(start_chunk, ensure_ascii=False) + "\n"
    content_chunk = {
        "delta": {"content": text, "function_call": None, "role": None, "tool_calls": None},
        "finish_reason": None,
        "index": 0,
        "logprobs": None,
        "content_filter_results": {},
    }
    yield json.dumps(content_chunk, ensure_ascii=False) + "\n"
    end_chunk = {
        "delta": {"content": None, "function_call": None, "role": None, "tool_calls": None},
        "finish_reason": "stop",
        "index": 0,
        "logprobs": None,
        "content_filter_results": {},
    }
    yield json.dumps(end_chunk, ensure_ascii=False) + "\n"
