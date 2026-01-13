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
    return (os.getenv("CHAT_PROVIDER") or "aoai").lower()


@bp.before_app_serving
async def configure_clients():
    client_args = {}
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
    else:
        bp.n8n_webhook_url = os.getenv("N8N_WEBHOOK_URL")
        bp.n8n_bearer_token = os.getenv("N8N_BEARER_TOKEN")
        timeout_ms = os.getenv("N8N_TIMEOUT_MS")
        try:
            timeout_ms = int(timeout_ms) if timeout_ms else 30000
        except ValueError:
            current_app.logger.warning("Invalid N8N_TIMEOUT_MS value %r, defaulting to 30000", timeout_ms)
            timeout_ms = 30000
        bp.n8n_timeout = timeout_ms / 1000
        bp.session_store = {}
        bp.session_store_lock = asyncio.Lock()

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
    request_messages = (await request.get_json())["messages"]

    @stream_with_context
    async def response_stream():
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
    user = context.get("user", {}) if context else {}
    if not hasattr(bp, "anonymous_user_key"):
        bp.anonymous_user_key = uuid.uuid4().hex
    fallback_key = bp.anonymous_user_key
    return user.get("oid") or user.get("id") or user.get("preferred_username") or user.get("name") or fallback_key


async def _session_id_for_user(context, messages):
    lock = getattr(bp, "session_store_lock", None)
    if lock:
        async with lock:
            return await _session_id_for_user_inner(context, messages)
    return await _session_id_for_user_inner(context, messages)


async def _session_id_for_user_inner(context, messages):
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
            pass
        bp.session_store = session_store
    return cached_session


async def stream_n8n(request_messages, context):
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
            message_text = _n8n_message_text(response.json())
            if not message_text:
                message_text = "The chat service returned an empty response."
            for chunk in build_assistant_message(message_text):
                yield chunk
    except Exception as e:
        current_app.logger.error(e)
        error_text = "Sorry, I could not reach the chat service. Please try again."
        for chunk in build_assistant_message(error_text):
            yield chunk


def build_assistant_message(text):
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
