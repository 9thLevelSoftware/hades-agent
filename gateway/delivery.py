"""
Delivery routing for cron job outputs and agent responses.

Routes messages to the appropriate destination based on:
- Explicit targets (e.g., "telegram:123456789")
- Platform home channels (e.g., "telegram" → home channel)
- Origin (back to where the job was created)
- Local (always saved to files)
"""

import hashlib
import json
import logging
import os
import re
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from hermes_cli.config import get_hermes_home

if TYPE_CHECKING:  # pragma: no cover - type-only
    from agent.operation_journal import OperationJournal

logger = logging.getLogger(__name__)

# Cap before gateway-level truncation of cron output for non-chunking platform
# delivery.  Telegram's hard API limit is 4096; the headroom covers the "full
# output saved to …" footer appended on truncation.  Adapters that split long
# messages natively (BasePlatformAdapter.splits_long_messages) bypass this
# entirely — the adapter chunks in its own send() and the full output is
# preserved.
MAX_PLATFORM_OUTPUT = 4000

# Matches strings that are *only* a "silence" narration with optional markdown
# wrappers. Covers: *(silent)*, _silent_, `silent`, ~silent~, (silent), silent,
# 🔇, a bare ".", "…", and the whitespace/marker-padded variants seen in the
# wild. Anchored to start/end so substantive messages that merely *contain* the
# word "silent" are never matched.
_SILENCE_NARRATION = re.compile(
    r'^[\s*_~`]*\(?\s*(silent|silence|no\s+response|no\s+reply)\s*\.?\)?[\s*_~`]*$'
    r'|^[\s*_~`]*[\U0001F507\.\u2026]+[\s*_~`]*$',
    re.IGNORECASE,
)


def _is_silence_narration(content: Optional[str]) -> bool:
    """Return True when ``content`` is *only* a silence-narration token.

    Length-guarded (real messages are longer) and anchored to the whole string
    so legitimate prose like "The deployment ran silently" or "Silence is
    golden — here is the plan..." is never flagged.
    """
    if not content:
        return False
    stripped = content.strip()
    if not stripped or len(stripped) > 64:  # length guard
        return False
    return bool(_SILENCE_NARRATION.match(stripped))

from .config import Platform, GatewayConfig
from .session import SessionSource
from .dead_targets import DeadTargetRegistry


def looks_like_telegram_private_chat_id(chat_id: Optional[str]) -> bool:
    """True when ``chat_id`` is a positive int — Telegram's private-chat shape.

    Telegram private chats use positive chat IDs; groups/channels/supergroups
    use negative IDs. This is the single source of truth for that heuristic,
    reused by the handoff seed path in ``gateway/run.py`` so handoff-created
    DM topics key the same way as inbound DM-topic messages.
    """
    if chat_id is None:
        return False
    try:
        return int(chat_id) > 0
    except (TypeError, ValueError):
        return False


def _looks_like_int(value: Optional[str]) -> bool:
    if value is None:
        return False
    try:
        int(value)
        return True
    except (TypeError, ValueError):
        return False


def _send_result_failed(result: Any) -> bool:
    if isinstance(result, dict):
        return result.get("success") is False
    return getattr(result, "success", True) is False


def _send_result_error(result: Any) -> Optional[str]:
    if isinstance(result, dict):
        error = result.get("error")
    else:
        error = getattr(result, "error", None)
    return str(error) if error else None


def _is_thread_not_found_delivery_error(result: Any) -> bool:
    error = _send_result_error(result)
    return bool(error and "thread not found" in error.lower())


def _send_result_error_kind(result: Any) -> Optional[str]:
    """Return the machine-readable error_kind from a SendResult/dict, if any."""
    if isinstance(result, dict):
        kind = result.get("error_kind")
    else:
        kind = getattr(result, "error_kind", None)
    return str(kind) if kind else None


def _classify_dead_from_error_text(error_text: Optional[str]) -> Optional[str]:
    """Best-effort dead-target classification from a raised error's text.

    ``_deliver_to_platform`` raises (it does not return a SendResult) on a hard
    failure, so the ``deliver()`` loop only has the exception string.  Reuse the
    platform-neutral classifier to recover the error_kind from that text.
    """
    if not error_text:
        return None
    try:
        from .platforms.base import classify_send_error, is_chat_level_not_found
    except Exception:  # pragma: no cover - import guard
        return None
    kind = classify_send_error(None, error_text=error_text)
    if not DeadTargetRegistry.is_dead_error_kind(kind):
        return None
    # ``not_found`` collapses chat-level and thread/topic/message-level failures.
    # Only a whole-chat not_found means the target is dead — a deleted forum topic
    # or an edited-away message must not mark the entire chat (and all of its future
    # deliveries) dead.  See gateway.dead_targets' documented scope.
    if kind == "not_found" and not is_chat_level_not_found(error_text=error_text):
        return None
    return kind


@dataclass
class DeliveryTarget:
    """
    A single delivery target.
    
    Represents where a message should be sent:
    - "origin" → back to source
    - "local" → save to local files
    - "telegram" → Telegram home channel
    - "telegram:123456" → specific Telegram chat
    """
    platform: Platform
    chat_id: Optional[str] = None  # None means use home channel
    thread_id: Optional[str] = None
    is_origin: bool = False
    is_explicit: bool = False  # True if chat_id was explicitly specified
    
    @classmethod
    def parse(cls, target: str, origin: Optional[SessionSource] = None) -> "DeliveryTarget":
        """
        Parse a delivery target string.
        
        Formats:
        - "origin" → back to source
        - "local" → local files only
        - "telegram" → Telegram home channel
        - "telegram:123456" → specific Telegram chat
        """
        target_stripped = target.strip()
        target_lower = target_stripped.lower()
        
        if target_lower == "origin":
            if origin:
                return cls(
                    platform=origin.platform,
                    chat_id=origin.chat_id,
                    thread_id=origin.thread_id,
                    is_origin=True,
                )
            else:
                # Fallback to local if no origin
                return cls(platform=Platform.LOCAL, is_origin=True)
        
        if target_lower == "local":
            return cls(platform=Platform.LOCAL)
        
        # Check for platform:chat_id or platform:chat_id:thread_id format
        # Use the original case for chat_id/thread_id to preserve case-sensitive IDs
        if ":" in target_stripped:
            parts = target_stripped.split(":", 2)
            platform_str = parts[0].lower()  # Platform names are case-insensitive
            chat_id = parts[1] if len(parts) > 1 else None
            thread_id = parts[2] if len(parts) > 2 else None
            try:
                platform = Platform(platform_str)
                return cls(platform=platform, chat_id=chat_id, thread_id=thread_id, is_explicit=True)
            except ValueError:
                # Unknown platform, treat as local
                return cls(platform=Platform.LOCAL)
        
        # Just a platform name (use home channel)
        try:
            platform = Platform(target_lower)
            return cls(platform=platform)
        except ValueError:
            # Unknown platform, treat as local
            return cls(platform=Platform.LOCAL)
    
    def to_string(self) -> str:
        """Convert back to string format."""
        if self.is_origin:
            return "origin"
        if self.platform == Platform.LOCAL:
            return "local"
        if self.chat_id and self.thread_id:
            return f"{self.platform.value}:{self.chat_id}:{self.thread_id}"
        if self.chat_id:
            return f"{self.platform.value}:{self.chat_id}"
        return self.platform.value


class DeliveryRouter:
    """
    Routes messages to appropriate destinations.
    
    Handles the logic of resolving delivery targets and dispatching
    messages to the right platform adapters.
    """
    
    def __init__(self, config: GatewayConfig, adapters: Dict[Platform, Any] = None,
                 dead_targets: Optional[DeadTargetRegistry] = None,
                 journal: Optional["OperationJournal"] = None):
        """
        Initialize the delivery router.

        Args:
            config: Gateway configuration
            adapters: Dict mapping platforms to their adapter instances
            dead_targets: Optional shared registry of confirmed-unreachable
                targets.  When omitted, a profile-local registry is created.
            journal: Optional OperationJournal for durable delivery
                tracking.  When set, callers MUST pass a stable
                ``metadata["delivery_id"]`` so re-runs after a crash can
                dedupe (Task 9).  Omit for legacy / non-cron callers.
        """
        self.config = config
        self.adapters = adapters or {}
        self.output_dir = get_hermes_home() / "cron" / "output"
        self.dead_targets = dead_targets or DeadTargetRegistry()
        self.journal = journal

    @staticmethod
    def _payload_hash(content: str) -> str:
        """SHA-256 hex of the outbound payload — used as the OperationJournal
        identity component for dedup.  ``hashlib.sha256`` is stdlib and
        collision-resistant enough that a re-send with the same hash is
        safe to treat as the same operation."""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _journal_receipt(self, target: DeliveryTarget, result: Any) -> Dict[str, Any]:
        """Build the bounded receipt metadata for a successful send.

        Stores ONLY the fields needed to recover from a crash (message_id,
        platform, success flag, target chat).  NEVER stores the full
        content, NEVER stores credentials or adapter kwargs.  This is the
        durable proof-of-flight row.
        """
        receipt: Dict[str, Any] = {
            "platform": target.platform.value,
            "chat_id": target.chat_id,
            "success": True,
        }
        if result is not None:
            message_id = getattr(result, "message_id", None)
            if message_id is None and isinstance(result, dict):
                message_id = result.get("message_id")
            if message_id is not None:
                receipt["message_id"] = str(message_id)
            continuation = getattr(result, "continuation_message_ids", None)
            if continuation:
                receipt["continuation_message_ids"] = [
                    str(m) for m in continuation
                ]
        return receipt
    
    async def deliver(
        self,
        content: str,
        targets: List[DeliveryTarget],
        job_id: Optional[str] = None,
        job_name: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Deliver content to all specified targets.
        
        Args:
            content: The message/output to deliver
            targets: List of delivery targets
            job_id: Optional job ID (for cron jobs)
            job_name: Optional job name
            metadata: Additional metadata to include
        
        Returns:
            Dict with delivery results per target
        """
        results = {}
        
        for target in targets:
            # Skip targets we've already proven permanently unreachable
            # (deleted group, blocked/kicked bot, deactivated user). Re-sending
            # to them on every tick wastes a send against flood control and
            # spams logs. Self-healing: a later successful send clears the flag.
            # LOCAL/origin-without-chat targets are never dead-tracked.
            if (
                target.platform != Platform.LOCAL
                and target.chat_id
                and self.dead_targets.is_dead(target.platform.value, target.chat_id)
            ):
                logger.info(
                    "Skipping delivery to known-dead target %s:%s "
                    "(send to it again to clear)",
                    target.platform.value, target.chat_id,
                )
                results[target.to_string()] = {
                    "success": False,
                    "skipped": "dead_target",
                    "error": "target previously confirmed unreachable",
                }
                continue
            try:
                if target.platform == Platform.LOCAL:
                    result = self._deliver_local(content, job_id, job_name, metadata)
                else:
                    result = await self._deliver_to_platform(target, content, metadata)
                    # Successful platform delivery — clear any stale dead flag.
                    if target.chat_id and not _send_result_failed(result):
                        self.dead_targets.clear(target.platform.value, target.chat_id)
                
                results[target.to_string()] = {
                    "success": True,
                    "result": result
                }
            except Exception as e:
                # A hard failure raises here. If the platform reported a
                # whole-chat death, record it so future deliveries short-circuit.
                if target.platform != Platform.LOCAL and target.chat_id:
                    dead_kind = _classify_dead_from_error_text(str(e))
                    if dead_kind:
                        self.dead_targets.mark_dead(
                            target.platform.value, target.chat_id,
                            reason=f"{dead_kind}: {str(e)[:120]}",
                        )
                results[target.to_string()] = {
                    "success": False,
                    "error": str(e)
                }
        
        return results
    
    def _deliver_local(
        self,
        content: str,
        job_id: Optional[str],
        job_name: Optional[str],
        metadata: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Save content to local files."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        if job_id:
            output_path = self.output_dir / job_id / f"{timestamp}.md"
        else:
            output_path = self.output_dir / "misc" / f"{timestamp}.md"
        
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Build the output document
        lines = []
        if job_name:
            lines.append(f"# {job_name}")
        else:
            lines.append("# Delivery Output")
        
        lines.append("")
        lines.append(f"**Timestamp:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        
        if job_id:
            lines.append(f"**Job ID:** {job_id}")
        
        if metadata:
            for key, value in metadata.items():
                lines.append(f"**{key}:** {value}")
        
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append(content)
        
        output_path.write_text("\n".join(lines))
        
        return {
            "path": str(output_path),
            "timestamp": timestamp
        }
    
    def _save_full_output(self, content: str, job_id: str) -> Path:
        """Save full cron output to disk and return the file path."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = get_hermes_home() / "cron" / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{job_id}_{timestamp}.txt"
        path.write_text(content)
        return path

    def _filter_silence_narration_enabled(self) -> bool:
        """Whether the outbound silence-narration filter is active.

        ``HERMES_FILTER_SILENCE_NARRATION`` env var overrides config when set;
        otherwise the ``gateway.filter_silence_narration`` config flag wins
        (default True).
        """
        env = os.getenv("HERMES_FILTER_SILENCE_NARRATION")
        if env is not None:
            return env.strip().lower() in ("1", "true", "yes", "on")
        return bool(getattr(self.config, "filter_silence_narration", True))

    async def _deliver_to_platform(
        self,
        target: DeliveryTarget,
        content: str,
        metadata: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Deliver content to a messaging platform.

        When the router was constructed with a ``journal``, this method
        records each send as an ``agent_operations`` row (kind=
        ``outbound_delivery``) so a process restart cannot double-send.
        Caller must supply a stable ``metadata["delivery_id"]``; the
        content is hashed to detect identity reuse (same id, different
        payload) which is a programmer error and raises.
        """
        # ── Journal preamble ────────────────────────────────────────────
        # Local/file delivery is not a transport round-trip; no journal row.
        # Without a journal, behavior is byte-identical to legacy callers.
        delivery_id: Optional[str] = None
        if self.journal is not None and target.platform != Platform.LOCAL:
            delivery_id = (metadata or {}).get("delivery_id")
            if not delivery_id or not isinstance(delivery_id, str):
                raise ValueError(
                    "DeliveryRouter with a journal requires a stable "
                    "metadata['delivery_id'] (got "
                    f"{delivery_id!r}) for {target.platform.value}:"
                    f"{target.chat_id}"
                )
            destination = (
                f"{target.platform.value}:{target.chat_id}"
                f"{':' + target.thread_id if target.thread_id else ''}"
            )
            payload_hash = self._payload_hash(content)
            existing = self.journal.get(delivery_id)
            journal = self.journal
            if existing is not None:
                if (
                    existing.kind != "outbound_delivery"
                    or existing.destination != destination
                    or existing.payload_hash != payload_hash
                ):
                    raise ValueError(
                        f"delivery_id {delivery_id!r} already identifies "
                        "a different operation "
                        f"(kind={existing.kind}, destination="
                        f"{existing.destination}, payload_hash="
                        f"{existing.payload_hash[:12]}…)"
                    )
                # Same identity — if already terminal, short-circuit so a
                # restart-reattempt does not duplicate the wire send.
                if existing.state in ("confirmed", "unknown", "cancelled"):
                    logger.info(
                        "Skipping outbound delivery %s — already %s",
                        delivery_id, existing.state,
                    )
                    dedup_result: Dict[str, Any] = {
                        "success": True,
                        "deduped": True,
                        "delivery_id": delivery_id,
                        "state": existing.state,
                    }
                    # Surface the prior message_id when we know it, so the
                    # caller's response payload is byte-comparable to a
                    # fresh send.
                    if existing.result_json:
                        try:
                            prior = json.loads(existing.result_json)
                            if isinstance(prior, dict) and prior.get("message_id"):
                                dedup_result["message_id"] = prior["message_id"]
                        except Exception:  # noqa: BLE001
                            pass
                    return dedup_result
                if existing.state == "failed":
                    self.journal.transition(
                        delivery_id,
                        from_states={"failed"},
                        to_state="running",
                        effect_disposition="none",
                    )
                    journal = self.journal
                else:
                    # In-flight on disk from before this process started: the
                    # boot-time reconcile_after_restart() should have flipped
                    # it to unknown. If a caller forgot to reconcile, treat it
                    # as unknown here too — never auto-resume mid-flight.
                    try:
                        self.journal.transition(
                            delivery_id,
                            from_states={"running", "dispatched"},
                            to_state="unknown",
                            effect_disposition="unknown",
                            error="resume attempted without reconcile_after_restart",
                        )
                    except Exception as journal_exc:  # noqa: BLE001 — never crash the caller
                        logger.warning(
                            "resume-time journal transition to unknown failed for %s: %s",
                            delivery_id,
                            journal_exc,
                        )
                    return {
                        "success": True,
                        "deduped": True,
                        "delivery_id": delivery_id,
                        "state": "unknown",
                    }
            if existing is None:
                # Brand-new delivery — create the row in pending state, then
                # advance to running (an in-memory-only state useful for crash
                # forensics; the on-wire dispatch happens next).
                self.journal.create(
                    operation_id=delivery_id,
                    kind="outbound_delivery",
                    destination=destination,
                    payload_hash=payload_hash,
                )
                self.journal.transition(
                    delivery_id,
                    from_states={"pending"},
                    to_state="running",
                    effect_disposition="none",
                )
                journal = self.journal
        else:
            journal = None

        adapter = self.adapters.get(target.platform)

        # ── Pre-dispatch failure path ──────────────────────────────────
        # If the failure happens BEFORE we hand the content to the adapter,
        # we know it never flew — record ``failed/none`` so the caller
        # (or a restart) can deterministically know.
        def _record_pre_dispatch_failed(err: str) -> None:
            if journal is None or delivery_id is None:
                return
            try:
                journal.transition(
                    delivery_id,
                    from_states={"running"},
                    to_state="failed",
                    effect_disposition="none",
                    error=err,
                )
            except Exception as journal_exc:  # noqa: BLE001 — never crash the caller
                logger.warning(
                    "journal transition to failed/none failed for %s: %s",
                    delivery_id, journal_exc,
                )

        if not adapter:
            _record_pre_dispatch_failed(f"no adapter for {target.platform.value}")
            raise ValueError(f"No adapter configured for {target.platform.value}")

        if not target.chat_id:
            _record_pre_dispatch_failed(f"no chat_id for {target.platform.value}")
            raise ValueError(f"No chat ID for {target.platform.value} delivery")

        # Guard: handle oversized cron output.
        #
        # Two independent decisions:
        #   1. AUDIT SAVE — when content exceeds MAX_PLATFORM_OUTPUT, the full
        #      output is always written to disk as a recoverable audit trail.
        #      This fires regardless of adapter capability (best-effort).
        #   2. TRUNCATION — for non-chunking adapters, content above the cap is
        #      truncated with a footer pointing to the saved file.  Chunking-
        #      capable adapters (splits_long_messages=True) receive the full
        #      payload and split natively in their send().
        job_id = (metadata or {}).get("job_id", "unknown")
        saved_path: Optional[Path] = None

        if len(content) > MAX_PLATFORM_OUTPUT:
            # Step 1 — audit save (best-effort).  The save is a side-effect
            # audit trail, not essential to delivery.  If it fails (full disk,
            # permissions), delivery proceeds — the content reaches the adapter
            # regardless.
            try:
                saved_path = self._save_full_output(content, job_id)
            except OSError as exc:
                logger.warning(
                    "Audit save failed for cron output (%d chars, job=%s): %s — "
                    "delivery proceeds without audit copy",
                    len(content), job_id, exc,
                )

            # Step 2 — truncation (only for non-chunking adapters).
            if getattr(adapter, "splits_long_messages", False):
                # Adapter chunks natively — deliver full payload.
                if saved_path:
                    logger.info(
                        "Cron output preserved for chunking adapter (%d chars) — "
                        "full output saved to %s",
                        len(content), saved_path,
                    )
            else:
                # Non-chunking adapter — truncate with footer.  The footer
                # needs a valid path, so if the best-effort save above failed,
                # retry it here (a failure now is a real delivery problem).
                if saved_path is None:
                    saved_path = self._save_full_output(content, job_id)
                footer = f"\n\n... [truncated, full output saved to {saved_path}]"
                visible = max(0, MAX_PLATFORM_OUTPUT - len(footer))
                logger.info(
                    "Cron output truncated (%d chars) — full output: %s",
                    len(content), saved_path,
                )
                content = content[:visible] + footer
        
        # Substrate-level anti-loop guard: drop hallucinated "silence narration"
        # (*(silent)*, 🔇, a bare ".", etc.) before it ever reaches the adapter.
        # In bot-to-bot channels these tokens mirror back and forth until a
        # model crashes with "no content after all retries". Behavioral prompt
        # rules drift across providers; this single chokepoint covers every
        # platform adapter regardless of which persona's prompt failed.
        # Local/file delivery (_deliver_local) is a separate path and is never
        # filtered — saved silence has no loop risk.
        if self._filter_silence_narration_enabled() and _is_silence_narration(content):
            logger.warning(
                "Dropped silence-narration outbound to %s (chat=%s): %r",
                target.platform.value,
                target.chat_id,
                content[:40],
            )
            # The filter is a deterministic, known outcome: the message never
            # flies. Record ``confirmed/none`` so the caller / restart does not
            # redeliver. The bounded receipt captures the filter decision and
            # nothing more — no content, no credentials.
            if journal is not None and delivery_id is not None:
                try:
                    journal.transition(
                        delivery_id,
                        from_states={"running"},
                        to_state="confirmed",
                        effect_disposition="none",
                        result={"filtered": "silence_narration"},
                    )
                except Exception as journal_exc:  # noqa: BLE001
                    logger.warning(
                        "journal transition to confirmed/none (silence) failed "
                        "for %s: %s",
                        delivery_id, journal_exc,
                    )
            return {
                "success": True,
                "filtered": "silence_narration",
                "delivered": False,
            }

        send_metadata = dict(metadata or {})
        is_named_telegram_private_topic = False
        named_telegram_private_topic_name: Optional[str] = None
        if target.thread_id:
            has_explicit_direct_topic = (
                "direct_messages_topic_id" in send_metadata
                or "telegram_direct_messages_topic_id" in send_metadata
            )
            target_thread_id = target.thread_id
            is_named_telegram_private_topic = (
                target.platform == Platform.TELEGRAM
                and looks_like_telegram_private_chat_id(target.chat_id)
                and not _looks_like_int(target_thread_id)
                and "thread_id" not in send_metadata
                and "message_thread_id" not in send_metadata
                and not has_explicit_direct_topic
            )
            if is_named_telegram_private_topic:
                named_telegram_private_topic_name = target_thread_id
                ensure_dm_topic = getattr(adapter, "ensure_dm_topic", None)
                if ensure_dm_topic is None:
                    raise RuntimeError(
                        "Telegram adapter cannot create named private DM topics"
                    )
                created_thread_id = await ensure_dm_topic(target.chat_id, target_thread_id)
                if not created_thread_id:
                    raise RuntimeError(
                        f"Failed to create Telegram private DM topic '{target_thread_id}'"
                    )
                target_thread_id = str(created_thread_id)
                send_metadata["thread_id"] = target_thread_id
                send_metadata["telegram_dm_topic_created_for_send"] = True
            elif (
                target.platform == Platform.TELEGRAM
                and looks_like_telegram_private_chat_id(target.chat_id)
                and "thread_id" not in send_metadata
                and "message_thread_id" not in send_metadata
                and not has_explicit_direct_topic
            ):
                # Legacy private topic/thread ids that were not created by this
                # send path may still need a reply anchor to stay visible in the
                # requested lane. Named targets are created above via
                # createForumTopic and can use message_thread_id directly.
                reply_anchor = send_metadata.get("telegram_reply_to_message_id")
                if reply_anchor is None:
                    raise RuntimeError(
                        "Telegram private DM topic delivery requires telegram_reply_to_message_id; "
                        "send to the bare chat or provide a reply anchor"
                    )
                send_metadata["thread_id"] = target_thread_id
                send_metadata["telegram_dm_topic_reply_fallback"] = True
            elif "thread_id" not in send_metadata and "message_thread_id" not in send_metadata and not has_explicit_direct_topic:
                send_metadata["thread_id"] = target_thread_id

        # ── On-wire dispatch (journaled) ───────────────────────────────
        # transition to ``dispatched`` BEFORE the adapter call so a crash
        # mid-send leaves the row recoverable: ``reconcile_after_restart``
        # flips dispatched→unknown on the next boot.
        if journal is not None and delivery_id is not None:
            try:
                journal.transition(
                    delivery_id,
                    from_states={"running"},
                    to_state="dispatched",
                    effect_disposition="unknown",
                )
            except Exception as journal_exc:  # noqa: BLE001
                logger.warning(
                    "journal transition to dispatched/unknown failed for %s: %s",
                    delivery_id, journal_exc,
                )

        try:
            result = await adapter.send(target.chat_id, content, metadata=send_metadata or None)
        except BaseException as dispatch_exc:
            # CancelledError / KeyboardInterrupt / generic exception: we
            # did NOT get a confirmed response from the adapter, so we
            # cannot claim success. Record ``unknown/unknown`` so a
            # subsequent restart cannot auto-retry (which would risk a
            # duplicate). The bounded error captures the dispatch path,
            # not the content.
            if journal is not None and delivery_id is not None:
                try:
                    err_text = type(dispatch_exc).__name__
                    if getattr(dispatch_exc, "args", None):
                        err_text = f"{err_text}: {dispatch_exc}"
                    journal.transition(
                        delivery_id,
                        from_states={"running", "dispatched"},
                        to_state="unknown",
                        effect_disposition="unknown",
                        error=err_text,
                    )
                except Exception as journal_exc:  # noqa: BLE001
                    logger.warning(
                        "journal transition to unknown/unknown failed for %s: %s",
                        delivery_id, journal_exc,
                    )
            raise

        if _send_result_failed(result):
            if (
                is_named_telegram_private_topic
                and named_telegram_private_topic_name
                and _is_thread_not_found_delivery_error(result)
            ):
                ensure_dm_topic = getattr(adapter, "ensure_dm_topic", None)
                if ensure_dm_topic is None:
                    raise RuntimeError(
                        "Telegram adapter cannot refresh named private DM topics"
                    )
                refreshed_thread_id = await ensure_dm_topic(
                    target.chat_id,
                    named_telegram_private_topic_name,
                    force_create=True,
                )
                if not refreshed_thread_id:
                    raise RuntimeError(
                        f"Failed to refresh Telegram private DM topic '{named_telegram_private_topic_name}'"
                    )
                send_metadata["thread_id"] = str(refreshed_thread_id)
                send_metadata["telegram_dm_topic_created_for_send"] = True
                result = await adapter.send(target.chat_id, content, metadata=send_metadata or None)
            if _send_result_failed(result):
                # Adapter explicitly reported failure — ``failed/none`` is
                # honest: it didn't fly, but we know it didn't fly.
                if journal is not None and delivery_id is not None:
                    try:
                        journal.transition(
                            delivery_id,
                            from_states={"running", "dispatched"},
                            to_state="failed",
                            effect_disposition="none",
                            error=_send_result_error(result) or "send failed",
                        )
                    except Exception as journal_exc:  # noqa: BLE001
                        logger.warning(
                            "journal transition to failed/none failed for %s: %s",
                            delivery_id, journal_exc,
                        )
                raise RuntimeError(
                    _send_result_error(result) or f"{target.platform.value} delivery failed"
                )

        # Success path — record ``confirmed/landed`` with a bounded
        # receipt (message_id + platform + chat_id; never the content
        # or any credentials).
        if journal is not None and delivery_id is not None:
            try:
                journal.transition(
                    delivery_id,
                    from_states={"running", "dispatched"},
                    to_state="confirmed",
                    effect_disposition="landed",
                    result=self._journal_receipt(target, result),
                )
            except Exception as journal_exc:  # noqa: BLE001
                logger.warning(
                    "journal transition to confirmed/landed failed for %s: %s",
                    delivery_id, journal_exc,
                )
        return result




