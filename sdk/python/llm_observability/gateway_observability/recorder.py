"""GatewayEventRecorder — fixed-event recording with a whitelisted attribute set.

Records gateway.* events onto a span with attributes restricted to the
contract whitelist. Event-add failure is fail-open (never alters span state or
business outcome).
"""
import logging
from typing import Optional

from . import events as event_names
from .events import filter_event_attributes

logger = logging.getLogger("llm_obs.gateway.recorder")


class GatewayEventRecorder:
    """Record fixed gateway lifecycle events on a target span.

    Args:
        span: The span (Router or Attempt) events are recorded onto.
        privacy: Optional PrivacyGuard used to sanitize event values.
    """

    def __init__(self, span=None, privacy=None):
        self._span = span
        self._privacy = privacy

    def bind(self, span):
        """Bind the recorder to a span (used before events fire)."""
        self._span = span
        return self

    @property
    def span(self):
        return self._span

    def record(self, name: str, attributes: Optional[dict] = None) -> bool:
        """Record one fixed gateway event with whitelisted attributes.

        Fail-open: any failure is logged and returns False.
        """
        if self._span is None:
            return False
        try:
            allowed = filter_event_attributes(attributes or {})
            if self._privacy is not None:
                allowed = self._privacy.sanitize_attributes(allowed)
            self._span.add_event(name, attributes=allowed)
            return True
        except Exception as e:
            logger.error("Gateway event recording failed (%s): %s", name, e)
            return False

    # ── lifecycle helpers ──

    def route_selected(self, channel_id=None, provider=None, resolved_model=None, reason=None) -> bool:
        attrs = {}
        if channel_id is not None:
            attrs["channel_id"] = channel_id
        if provider is not None:
            attrs["provider"] = provider
        if resolved_model is not None:
            attrs["resolved_model"] = resolved_model
        if reason is not None:
            attrs["reason"] = reason
        return self.record(event_names.EVENT_ROUTE_SELECTED, attrs)

    def retry_scheduled(self, attempt_index=None, delay_ms=None, reason=None) -> bool:
        attrs = {}
        if attempt_index is not None:
            attrs["attempt_index"] = attempt_index
        if delay_ms is not None:
            attrs["delay_ms"] = delay_ms
        if reason is not None:
            attrs["reason"] = reason
        return self.record(event_names.EVENT_RETRY_SCHEDULED, attrs)

    def fallback_selected(self, channel_id=None, reason=None) -> bool:
        """Record the to-channel + reason for a fallback transition.

        The from-channel is conveyed by the caller via ``from_channel_id`` and
        verified to differ from ``channel_id`` (see RouterSpan.fallback_selected).
        """
        attrs = {}
        if channel_id is not None:
            attrs["channel_id"] = channel_id
        if reason is not None:
            attrs["reason"] = reason
        return self.record(event_names.EVENT_FALLBACK_SELECTED, attrs)

    def attempt_started(self, attempt_index=None, channel_id=None, provider=None, resolved_model=None) -> bool:
        attrs = {}
        if attempt_index is not None:
            attrs["attempt_index"] = attempt_index
        if channel_id is not None:
            attrs["channel_id"] = channel_id
        if provider is not None:
            attrs["provider"] = provider
        if resolved_model is not None:
            attrs["resolved_model"] = resolved_model
        return self.record(event_names.EVENT_ATTEMPT_STARTED, attrs)

    def attempt_completed(self, attempt_index=None, http_status_code=None) -> bool:
        attrs = {}
        if attempt_index is not None:
            attrs["attempt_index"] = attempt_index
        if http_status_code is not None:
            attrs["http_status_code"] = http_status_code
        return self.record(event_names.EVENT_ATTEMPT_COMPLETED, attrs)

    def attempt_failed(self, attempt_index=None, error_category=None, http_status_code=None) -> bool:
        attrs = {}
        if attempt_index is not None:
            attrs["attempt_index"] = attempt_index
        if error_category is not None:
            attrs["error_category"] = error_category
        if http_status_code is not None:
            attrs["http_status_code"] = http_status_code
        return self.record(event_names.EVENT_ATTEMPT_FAILED, attrs)

    def stream_started(self) -> bool:
        return self.record(event_names.EVENT_STREAM_STARTED, {})

    def stream_first_token(self) -> bool:
        return self.record(event_names.EVENT_STREAM_FIRST_TOKEN, {})

    def stream_completed(self) -> bool:
        return self.record(event_names.EVENT_STREAM_COMPLETED, {})

    def stream_cancelled(self, error_category=None) -> bool:
        attrs = {}
        if error_category is not None:
            attrs["error_category"] = error_category
        return self.record(event_names.EVENT_STREAM_CANCELLED, attrs)

    def cache_hit(self) -> bool:
        return self.record(event_names.EVENT_CACHE_HIT, {})

    def cache_miss(self) -> bool:
        return self.record(event_names.EVENT_CACHE_MISS, {})

    def cache_bypass(self) -> bool:
        return self.record(event_names.EVENT_CACHE_BYPASS, {})

    def rate_limit_rejected(self) -> bool:
        return self.record(event_names.EVENT_RATE_LIMIT_REJECTED, {})

    def response_completed(self, http_status_code=None) -> bool:
        attrs = {}
        if http_status_code is not None:
            attrs["http_status_code"] = http_status_code
        return self.record(event_names.EVENT_RESPONSE_COMPLETED, attrs)

    def response_failed(self, error_category=None, http_status_code=None) -> bool:
        attrs = {}
        if error_category is not None:
            attrs["error_category"] = error_category
        if http_status_code is not None:
            attrs["http_status_code"] = http_status_code
        return self.record(event_names.EVENT_RESPONSE_FAILED, attrs)
