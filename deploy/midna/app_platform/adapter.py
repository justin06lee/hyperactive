"""midna app platform: send-only. ``send`` POSTs the text to ``midna serve``'s ``/v1/deliver``,
which puts it in the app's conversation and outbox (push included). Nothing comes in this way:
the app's chats reach Hermes through the api_server.

Env: MIDNA_APP_URL (``http://127.0.0.1:7717``), MIDNA_APP_TOKEN (a midna device token of kind
``bridge``), MIDNA_APP_HOME_CHANNEL (default ``main``). midna keeps no quiet hours for these:
Hermes (the pulse) already decided it is time to send.
"""

import logging
import re
from typing import Any, Optional

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    httpx = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms._shared import get_scoped_secret as _get_scoped_secret, send_error
from gateway.platforms._shared import extra_or_secret as _extra_or_secret
from gateway.platforms.send_targets import parse_nonempty

logger = logging.getLogger(__name__)

NAME = "midna"
MAX_MESSAGE_LENGTH = 20000  # what midna's /v1/deliver takes
HOME = "main"


def _settings(extra: dict[str, Any]) -> tuple[str, str]:
    url = _extra_or_secret(extra, "url", "MIDNA_APP_URL").strip().rstrip("/")
    token = _extra_or_secret(extra, "token", "MIDNA_APP_TOKEN").strip()
    return url, token


# Hermes's cron wrapper (`cron.wrap_response`): the app shows the message itself.
_WRAP_HEAD = re.compile(r"\ACronjob Response: [^\n]*\n\(job_id: [^)\n]*\)\n-+\n+")
_WRAP_FOOT = re.compile(r"\n+To stop or manage this job, send me a new message[^\n]*\Z")


def unwrap(content: str) -> str:
    text = _WRAP_FOOT.sub("", _WRAP_HEAD.sub("", content))
    return text.strip() or content


async def _deliver(client, url: str, token: str, content: str, source: str) -> SendResult:
    """One POST to /v1/deliver; midna answers 202 with the outbox id."""
    body = {"text": unwrap(content)[:MAX_MESSAGE_LENGTH], "source": source}
    try:
        resp = await client.post(f"{url}/v1/deliver", json=body, headers={"Authorization": f"Bearer {token}"},
                                 timeout=15.0)
    except Exception as e:  # midna down: Hermes records the delivery as failed; Telegram still went out
        return SendResult(success=False, error=f"midna unreachable: {e}")
    if resp.status_code < 300:
        try:
            mid = str(resp.json().get("id", ""))
        except Exception:
            mid = ""
        return SendResult(success=True, message_id=mid or None)
    return SendResult(success=False, error=f"midna HTTP {resp.status_code}: {resp.text[:200]}")


def check_requirements() -> bool:
    return HTTPX_AVAILABLE and bool(_get_scoped_secret("MIDNA_APP_URL", "").strip()) \
        and bool(_get_scoped_secret("MIDNA_APP_TOKEN", "").strip())


def validate_config(config) -> bool:
    url, token = _settings(getattr(config, "extra", {}) or {})
    return bool(url and token)


def is_connected(config) -> bool:
    return validate_config(config)


class MidnaAppAdapter(BasePlatformAdapter):
    """Send-only adapter into midna's outbox."""

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH

    def __init__(self, config: PlatformConfig):
        super().__init__(config=config, platform=Platform(NAME))
        self._url, self._token = _settings(config.extra or {})
        self._client = None

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not HTTPX_AVAILABLE or not (self._url and self._token):
            logger.warning("[%s] MIDNA_APP_URL / MIDNA_APP_TOKEN not set (or httpx missing)", self.name)
            return False
        self._client = httpx.AsyncClient(timeout=15.0)
        self._mark_connected()
        logger.info("[%s] delivering to %s", self.name, self._url)
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()
        if self._client:
            await self._client.aclose()
            self._client = None

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[dict[str, Any]] = None) -> SendResult:
        if not self._client:
            return SendResult(success=False, error="not connected")
        return await _deliver(self._client, self._url, self._token, content, "hermes")

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": chat_id or HOME, "type": "dm"}


def _env_enablement() -> dict | None:
    url = _get_scoped_secret("MIDNA_APP_URL", "").strip()
    token = _get_scoped_secret("MIDNA_APP_TOKEN", "").strip()
    if not (url and token):
        return None
    home = _get_scoped_secret("MIDNA_APP_HOME_CHANNEL", "").strip() or HOME
    return {"url": url.rstrip("/"), "token": token, "home_channel": {"chat_id": home, "name": "midna app"}}


async def _standalone_send(pconfig, chat_id: str, message: str, *, thread_id: Optional[str] = None,
                           media_files: Optional[list[str]] = None, force_document: bool = False) -> dict[str, Any]:
    """Out-of-process delivery (cron without a live gateway adapter)."""
    if not HTTPX_AVAILABLE:
        return send_error("midna standalone send: httpx not installed")
    url, token = _settings(getattr(pconfig, "extra", {}) or {})
    if not (url and token):
        return send_error("midna standalone send: MIDNA_APP_URL / MIDNA_APP_TOKEN not configured")
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await _deliver(client, url, token, message, "hermes:cron")
    if not r.success:
        return send_error(f"midna standalone send: {r.error}")
    return {"success": True, "platform": NAME, "chat_id": chat_id or HOME, "message_id": r.message_id}


def register(ctx) -> None:
    ctx.register_platform(
        target_ref_syntax_fn=parse_nonempty,
        name=NAME, label="midna app", adapter_factory=lambda cfg: MidnaAppAdapter(cfg),
        dashboard_description="Deliver Hermes's own messages into the midna app (send-only).",
        check_fn=check_requirements, validate_config=validate_config, is_connected=is_connected,
        required_env=["MIDNA_APP_URL", "MIDNA_APP_TOKEN"], install_hint="pip install httpx   # already a Hermes dependency",
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="MIDNA_APP_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        max_message_length=MAX_MESSAGE_LENGTH, emoji="🌙",
        pii_safe=True,
        platform_hint="This is the midna app: plain text or light Markdown, phone-first.")
