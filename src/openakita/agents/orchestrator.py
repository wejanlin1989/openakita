"""
AgentOrchestrator — central multi-agent coordinator.

Lightweight in-process design using asyncio.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from openakita.channels import MessageGateway

logger = logging.getLogger(__name__)

MAX_DELEGATION_DEPTH = 5
CHECK_INTERVAL = 3.0    # how often to poll progress (matches frontend polling)

# Defaults — overridden at runtime by settings when available
_DEFAULT_IDLE_TIMEOUT = 1200.0
_DEFAULT_HARD_TIMEOUT = 0  # 0 = disabled


@dataclass
class DelegationRequest:
    """A request to delegate work to another agent."""

    from_agent: str
    to_agent: str
    message: str
    session_key: str
    depth: int = 0
    parent_request_id: str | None = None


@dataclass
class AgentHealth:
    """Health metrics for an agent."""

    agent_id: str
    total_requests: int = 0
    successful: int = 0
    failed: int = 0
    total_latency_ms: float = 0.0
    last_error: str | None = None
    last_active: float = field(default_factory=time.time)

    @property
    def success_rate(self) -> float:
        return self.successful / max(self.total_requests, 1)

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / max(self.successful, 1)


class AgentMailbox:
    """Per-agent async message queue."""

    def __init__(self, agent_id: str, maxsize: int = 100):
        self.agent_id = agent_id
        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=maxsize)

    async def send(self, message: dict) -> None:
        await self._queue.put(message)

    async def receive(self, timeout: float = 300.0) -> dict | None:
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    @property
    def pending(self) -> int:
        return self._queue.qsize()


class AgentOrchestrator:
    """
    Central coordinator for multi-agent mode.

    Responsibilities:
    - Route messages to the correct agent based on session's agent_profile_id
    - Support agent delegation with depth limits
    - Handle timeouts, failures, cancellation
    - Track agent health metrics
    """

    def __init__(self) -> None:
        self._mailboxes: dict[str, AgentMailbox] = {}
        self._health: dict[str, AgentHealth] = {}
        self._active_tasks: dict[str, asyncio.Task] = {}

        # Lazy-initialised dependencies
        self._profile_store = None  # ProfileStore
        self._pool = None           # AgentInstancePool
        self._fallback = None       # FallbackResolver
        self._gateway: MessageGateway | None = None

        # Delegation log directory (fixed path for easy debugging)
        self._log_dir: Path | None = None

        # Live sub-agent states for frontend polling
        # Key: "{session_id}:{agent_profile_id}", Value: state dict
        self._sub_agent_states: dict[str, dict] = {}
        self._sub_cleanup_tasks: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------
    # External wiring
    # ------------------------------------------------------------------

    def set_gateway(self, gateway: MessageGateway | None) -> None:
        """Inject the MessageGateway reference (set after both are created)."""
        self._gateway = gateway

    # ------------------------------------------------------------------
    # Lazy dependency bootstrap
    # ------------------------------------------------------------------

    def _ensure_deps(self) -> None:
        """Lazily initialise ProfileStore, AgentInstancePool, FallbackResolver.

        Raises RuntimeError if any dependency fails to initialise.
        """
        try:
            if self._profile_store is None:
                from openakita.agents.profile import ProfileStore
                from openakita.config import settings

                self._profile_store = ProfileStore(settings.data_dir / "agents")

            if self._pool is None:
                from openakita.agents.factory import AgentFactory, AgentInstancePool

                self._pool = AgentInstancePool(AgentFactory())

            if self._fallback is None:
                from openakita.agents.fallback import FallbackResolver

                self._fallback = FallbackResolver(self._profile_store)

            if self._log_dir is None:
                from openakita.config import settings as _s
                self._log_dir = _s.data_dir / "delegation_logs"
                self._log_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.error(f"[Orchestrator] Failed to initialise dependencies: {e}", exc_info=True)
            raise RuntimeError(f"Orchestrator dependency init failed: {e}") from e

    # ------------------------------------------------------------------
    # Delegation JSONL logging
    # ------------------------------------------------------------------

    def _log_delegation(self, record: dict[str, Any]) -> None:
        """Append a delegation event to the daily JSONL log file.

        File: ``data/delegation_logs/YYYYMMDD.jsonl``
        Each line is a self-contained JSON object for easy grep/tail/analysis.
        """
        if self._log_dir is None:
            return
        try:
            today = datetime.now().strftime("%Y%m%d")
            path = self._log_dir / f"{today}.jsonl"
            record.setdefault("ts", datetime.now().isoformat())
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except Exception:
            logger.debug("[Orchestrator] Failed to write delegation log", exc_info=True)

    # ------------------------------------------------------------------
    # Mailbox / health helpers
    # ------------------------------------------------------------------

    def get_mailbox(self, agent_id: str) -> AgentMailbox:
        if agent_id not in self._mailboxes:
            self._mailboxes[agent_id] = AgentMailbox(agent_id)
        return self._mailboxes[agent_id]

    def _get_health(self, agent_id: str) -> AgentHealth:
        if agent_id not in self._health:
            self._health[agent_id] = AgentHealth(agent_id=agent_id)
        return self._health[agent_id]

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def handle_message(self, session: Any, message: str) -> str:
        """
        Main entry point — called from agent_handler in main.py.
        Routes the message to the appropriate agent based on session context.
        """
        self._ensure_deps()

        # Use session.id (UUID) as the canonical key for both the agent pool
        # and active-task tracking so we avoid mismatches.
        sid = session.id
        agent_profile_id = getattr(session.context, "agent_profile_id", "default")

        task = asyncio.create_task(
            self._dispatch(
                session=session,
                message=message,
                agent_profile_id=agent_profile_id,
                depth=0,
            )
        )
        self._active_tasks[sid] = task
        try:
            return await task
        finally:
            self._active_tasks.pop(sid, None)

    # ------------------------------------------------------------------
    # Dispatch with timeout / fallback / error handling
    # ------------------------------------------------------------------

    async def _dispatch(
        self,
        session: Any,
        message: str,
        agent_profile_id: str,
        depth: int,
        from_agent: str | None = None,
    ) -> str:
        """Dispatch a message to a specific agent with progress-aware timeout."""
        if depth >= MAX_DELEGATION_DEPTH:
            return f"⚠️ 委派深度超限 (max={MAX_DELEGATION_DEPTH})"

        if depth == 0:
            session.context.delegation_chain = []
        elif depth > 0:
            chain = getattr(session.context, "delegation_chain", [])
            chain.append({
                "from": from_agent or "parent",
                "to": agent_profile_id,
                "depth": depth,
                "timestamp": time.time(),
            })
            session.context.delegation_chain = chain

        health = self._get_health(agent_profile_id)
        health.total_requests += 1
        health.last_active = time.time()
        start = time.monotonic()

        session_key = getattr(session, "session_key", session.id)
        log_base = {
            "session": str(session_key),
            "agent": agent_profile_id,
            "from": from_agent,
            "depth": depth,
            "message_preview": message[:200],
        }
        self._log_delegation({**log_base, "event": "dispatch_start"})

        try:
            result = await self._run_with_progress_timeout(
                session, message, agent_profile_id,
                pass_gateway=(depth == 0),
            )
            elapsed_ms = (time.monotonic() - start) * 1000
            health.successful += 1
            health.total_latency_ms += elapsed_ms
            self._fallback.record_success(agent_profile_id)
            self._log_delegation({
                **log_base,
                "event": "dispatch_ok",
                "elapsed_ms": round(elapsed_ms),
                "result_preview": str(result)[:300],
            })
            return result

        except asyncio.TimeoutError:
            health.failed += 1
            health.last_error = "timeout_idle"
            self._fallback.record_failure(agent_profile_id)
            elapsed_s = time.monotonic() - start
            logger.warning(
                f"[Orchestrator] Agent {agent_profile_id} terminated after "
                f"{elapsed_s:.0f}s — no progress detected"
            )
            self._log_delegation({
                **log_base,
                "event": "dispatch_timeout",
                "elapsed_ms": round(elapsed_s * 1000),
                "reason": "idle_no_progress",
            })
            return await self._try_fallback_or(
                session, message, agent_profile_id, depth,
                default=(
                    f"⏱️ Agent `{agent_profile_id}` 已终止 — "
                    f"运行 {elapsed_s:.0f}s 后长时间无新进展"
                ),
            )

        except asyncio.CancelledError:
            health.failed += 1
            health.last_error = "cancelled"
            self._log_delegation({
                **log_base,
                "event": "dispatch_cancelled",
                "elapsed_ms": round((time.monotonic() - start) * 1000),
            })
            return "🚫 请求已取消"

        except Exception as e:
            health.failed += 1
            health.last_error = str(e)
            logger.error(
                f"[Orchestrator] Agent {agent_profile_id} failed: {e}",
                exc_info=True,
            )
            self._fallback.record_failure(agent_profile_id)
            self._log_delegation({
                **log_base,
                "event": "dispatch_error",
                "elapsed_ms": round((time.monotonic() - start) * 1000),
                "error": str(e)[:500],
            })
            return await self._try_fallback_or(
                session, message, agent_profile_id, depth,
                default=f"❌ Agent `{agent_profile_id}` 处理失败: {e}",
            )

    # ------------------------------------------------------------------
    # Progress-aware timeout
    # ------------------------------------------------------------------

    async def _run_with_progress_timeout(
        self,
        session: Any,
        message: str,
        agent_profile_id: str,
        *,
        pass_gateway: bool = False,
    ) -> str:
        """Run an agent with progress-aware timeout instead of a hard wall-clock limit.

        The agent is allowed to keep running as long as its ReAct iteration counter
        or task status keeps advancing.  It is killed only when:
        - No iteration progress for ``idle_timeout`` seconds, OR
        - Total elapsed time exceeds ``hard_timeout`` (only if configured > 0).
        """
        from openakita.config import settings

        idle_timeout = float(
            getattr(settings, "progress_timeout_seconds", 0) or _DEFAULT_IDLE_TIMEOUT
        )
        hard_timeout = float(
            getattr(settings, "hard_timeout_seconds", 0) or _DEFAULT_HARD_TIMEOUT
        )

        if self._profile_store is None or self._pool is None:
            return "⚠️ Orchestrator 未正确初始化，请检查日志"

        profile = self._profile_store.get(agent_profile_id)
        if profile is None:
            profile = self._profile_store.get("default")
        if profile is None:
            return f"⚠️ 无法找到 Agent Profile: {agent_profile_id}"

        agent = await self._pool.get_or_create(session.id, profile)
        gw = self._gateway if pass_gateway else None

        task = asyncio.create_task(
            self._call_agent(agent, session, message, gateway=gw)
        )

        start = time.monotonic()
        last_fingerprint: tuple[int, str, int] = (-1, "", 0)
        last_progress_time = start

        state_key = f"{session.id}:{agent_profile_id}"
        self._sub_agent_states[state_key] = {
            "agent_id": agent_profile_id,
            "profile_id": profile.id,
            "session_id": str(session.id),
            "status": "starting",
            "iteration": 0,
            "tools_executed": [],
            "tools_total": 0,
            "elapsed_s": 0,
            "last_progress_s": 0,
            "started_at": time.time(),
        }

        try:
            while not task.done():
                await asyncio.sleep(CHECK_INTERVAL)
                elapsed = time.monotonic() - start

                if hard_timeout > 0 and elapsed >= hard_timeout:
                    logger.warning(
                        f"[Orchestrator] Agent {agent_profile_id} hit hard cap "
                        f"({hard_timeout}s configured in settings.hard_timeout_seconds), "
                        f"killing. Set hard_timeout_seconds=0 to disable."
                    )
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
                    self._update_sub_state(state_key, "timeout", elapsed)
                    raise asyncio.TimeoutError()

                fp = self._get_progress_fingerprint(agent, session.id)
                if fp != last_fingerprint:
                    last_fingerprint = fp
                    last_progress_time = time.monotonic()
                    logger.debug(
                        f"[Orchestrator] Agent {agent_profile_id} progress: "
                        f"iter={fp[0]}, status={fp[1]}, tools={fp[2]}, "
                        f"elapsed={elapsed:.0f}s"
                    )
                    self._log_delegation({
                        "event": "progress",
                        "agent": agent_profile_id,
                        "session": str(getattr(session, "session_key", session.id)),
                        "iter": fp[0],
                        "status": fp[1],
                        "tools_count": fp[2],
                        "elapsed_s": round(elapsed),
                    })

                # Update live sub-agent state for frontend polling
                tools_list = self._get_tools_executed(agent, session.id)
                idle_s = time.monotonic() - last_progress_time
                self._sub_agent_states[state_key] = {
                    **self._sub_agent_states.get(state_key, {}),
                    "status": "running",
                    "iteration": fp[0] if fp[0] >= 0 else 0,
                    "tools_executed": tools_list[-5:],
                    "tools_total": len(tools_list),
                    "elapsed_s": round(elapsed),
                    "last_progress_s": round(idle_s),
                }

                if idle_s >= idle_timeout:
                    logger.warning(
                        f"[Orchestrator] Agent {agent_profile_id} idle for "
                        f"{idle_s:.0f}s with no progress "
                        f"(last fingerprint: iter={last_fingerprint[0]}, "
                        f"status={last_fingerprint[1]}, tools={last_fingerprint[2]}). "
                        f"Killing. Adjust settings.progress_timeout_seconds to change threshold."
                    )
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
                    self._update_sub_state(state_key, "timeout", elapsed)
                    raise asyncio.TimeoutError()

            self._update_sub_state(state_key, "completed", time.monotonic() - start)
            return task.result()
        except asyncio.CancelledError:
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            self._update_sub_state(state_key, "cancelled", time.monotonic() - start)
            raise

    def _update_sub_state(self, key: str, status: str, elapsed: float) -> None:
        """Update a sub-agent's terminal state and schedule cleanup.

        For ephemeral profiles, also removes the temporary profile from
        the ProfileStore once the task reaches a terminal state.
        """
        state_entry = self._sub_agent_states.get(key)
        if state_entry:
            state_entry["status"] = status
            state_entry["elapsed_s"] = round(elapsed)

        # Clean up ephemeral profile on terminal states
        profile_id = state_entry.get("profile_id", "") if state_entry else ""
        if profile_id and status in ("completed", "timeout", "cancelled", "error"):
            self._try_cleanup_ephemeral(profile_id)

        async def _delayed_cleanup() -> None:
            await asyncio.sleep(120)
            self._sub_agent_states.pop(key, None)
            self._sub_cleanup_tasks.pop(key, None)

        old_task = self._sub_cleanup_tasks.pop(key, None)
        if old_task and not old_task.done():
            old_task.cancel()
        try:
            self._sub_cleanup_tasks[key] = asyncio.create_task(_delayed_cleanup())
        except RuntimeError:
            self._sub_agent_states.pop(key, None)

    def _try_cleanup_ephemeral(self, profile_id: str) -> None:
        """Remove an ephemeral profile from ProfileStore if applicable."""
        try:
            if self._profile_store is None:
                return
            p = self._profile_store.get(profile_id)
            if p and getattr(p, "ephemeral", False):
                self._profile_store.remove_ephemeral(profile_id)
                logger.info(
                    f"[Orchestrator] Cleaned up ephemeral profile: {profile_id}"
                )
        except Exception as e:
            logger.warning(f"[Orchestrator] Failed to cleanup ephemeral {profile_id}: {e}")

    @staticmethod
    def _get_tools_executed(agent: Any, session_id: str) -> list[str]:
        """Return the list of tool names executed by the agent in the current task."""
        state = getattr(agent, "agent_state", None)
        if state is None:
            return []
        task = state.get_task_for_session(session_id)
        if task is None:
            task = state.current_task
        if task is None:
            return []
        return list(task.tools_executed) if task.tools_executed else []

    def get_sub_agent_states(self, session_id: str) -> list[dict]:
        """Return live sub-agent states for the given conversation.

        Accepts either a full session.id or a chat_id (conversation_id).
        The state keys are stored as '{session.id}:{agent_id}', where
        session.id = '{channel}_{chat_id}_{timestamp}_{uuid}'.
        Frontend passes the raw chat_id, so we match any key whose
        session portion contains the given id.
        """
        result = []
        for key, state in list(self._sub_agent_states.items()):
            sid_part = key.split(":")[0] if ":" in key else key
            if sid_part == session_id or session_id in sid_part:
                entry = dict(state)
                # Attach profile display info
                profile_id = entry.get("profile_id", "")
                if self._profile_store:
                    profile = self._profile_store.get(profile_id)
                    if profile:
                        entry["name"] = profile.get_display_name()
                        entry["icon"] = profile.icon or "🤖"
                    else:
                        entry.setdefault("name", profile_id)
                        entry.setdefault("icon", "🤖")
                else:
                    entry.setdefault("name", profile_id)
                    entry.setdefault("icon", "🤖")
                result.append(entry)
        return result

    @staticmethod
    def _get_progress_fingerprint(agent: Any, session_id: str) -> tuple[int, str, int]:
        """Return (iteration, status, tools_count) as a composite progress signal.

        Any change in this tuple means the agent is making progress.
        """
        state = getattr(agent, "agent_state", None)
        if state is None:
            return (-1, "", 0)
        task = state.get_task_for_session(session_id)
        if task is None:
            task = state.current_task
        if task is None:
            return (-1, "", 0)
        status_str = task.status.value if hasattr(task.status, "value") else str(task.status)
        return (task.iteration, status_str, len(task.tools_executed))

    @staticmethod
    async def _call_agent(
        agent: Any, session: Any, message: str, *, gateway: Any = None
    ) -> str:
        """Thin wrapper around agent.chat_with_session for use as a task target.

        Sets _is_sub_agent_call so that _finalize_session skips plan
        auto-close (the plan belongs to the parent agent, not this sub-agent).
        """
        agent._is_sub_agent_call = True
        try:
            session_messages = session.context.get_messages()
            return await agent.chat_with_session(
                message=message,
                session_messages=session_messages,
                session_id=session.id,
                session=session,
                gateway=gateway,
            )
        finally:
            agent._is_sub_agent_call = False

    async def _try_fallback_or(
        self,
        session: Any,
        message: str,
        agent_profile_id: str,
        depth: int,
        *,
        default: str,
    ) -> str:
        """
        If the FallbackResolver says we should degrade, dispatch to the
        fallback profile; otherwise return *default*.
        """
        if self._fallback.should_use_fallback(agent_profile_id):
            effective_id = self._fallback.get_effective_profile(agent_profile_id)
            if effective_id != agent_profile_id:
                logger.info(
                    f"[Orchestrator] Falling back from "
                    f"{agent_profile_id} to {effective_id}"
                )
                return await self._dispatch(
                    session, message, effective_id, depth + 1,
                    from_agent=agent_profile_id,
                )
        return default

    # ------------------------------------------------------------------
    # Delegation (called by agent tools)
    # ------------------------------------------------------------------

    async def delegate(
        self,
        session: Any,
        from_agent: str,
        to_agent: str,
        message: str,
        depth: int = 0,
        reason: str = "",
    ) -> str:
        """
        Delegate work from one agent to another.
        Called by agent tools (e.g. delegate_to_agent).
        """
        self._ensure_deps()
        logger.info(
            f"[Orchestrator] Delegation: {from_agent} -> {to_agent} (depth={depth})"
        )

        # Pre-register sub-agent state immediately so frontend polling
        # can pick it up before _run_with_progress_timeout starts
        state_key = f"{session.id}:{to_agent}"
        profile_name = to_agent
        profile_icon = "🤖"
        if self._profile_store:
            p = self._profile_store.get(to_agent)
            if p:
                profile_name = p.get_display_name()
                profile_icon = p.icon or "🤖"
        self._sub_agent_states[state_key] = {
            "agent_id": to_agent,
            "profile_id": to_agent,
            "session_id": str(session.id),
            "name": profile_name,
            "icon": profile_icon,
            "status": "starting",
            "iteration": 0,
            "tools_executed": [],
            "tools_total": 0,
            "elapsed_s": 0,
            "from_agent": from_agent,
            "reason": reason or "",
        }

        # Emit handoff event for SSE stream (session.context.handoff_events)
        if session and hasattr(session, "context") and hasattr(session.context, "handoff_events"):
            session.context.handoff_events.append({
                "from_agent": from_agent,
                "to_agent": to_agent,
                "reason": reason or "",
            })
        return await self._dispatch(
            session, message, to_agent, depth + 1, from_agent=from_agent
        )

    # ------------------------------------------------------------------
    # Multi-agent collaboration
    # ------------------------------------------------------------------

    async def start_collaboration(self, session: Any, agent_ids: list[str]) -> str:
        """Start a multi-agent collaboration session."""
        ctx = session.context
        ctx.active_agents = list(set(agent_ids))
        logger.info(
            f"[Orchestrator] Collaboration started: {agent_ids} in {session.session_key}"
        )
        return f"✅ Collaboration started with {len(agent_ids)} agents"

    async def get_active_agents(self, session: Any) -> list[str]:
        """Get currently active agents in a session."""
        return getattr(session.context, "active_agents", [])

    def get_delegation_chain(self, session: Any) -> list[dict]:
        """Get the delegation chain for the current session."""
        return getattr(session.context, "delegation_chain", [])

    # ------------------------------------------------------------------
    # Cancellation
    # ------------------------------------------------------------------

    def cancel_request(self, session_id: str) -> bool:
        """Cancel an active request for a session (by session.id UUID)."""
        task = self._active_tasks.get(session_id)
        if task and not task.done():
            task.cancel()
            return True
        return False

    # ------------------------------------------------------------------
    # Health / monitoring
    # ------------------------------------------------------------------

    def get_health_stats(self) -> dict[str, dict]:
        """Get health metrics for all agents."""
        return {
            agent_id: {
                "total_requests": h.total_requests,
                "successful": h.successful,
                "failed": h.failed,
                "success_rate": round(h.success_rate, 3),
                "avg_latency_ms": round(h.avg_latency_ms, 1),
                "last_error": h.last_error,
                "pending_messages": (
                    self._mailboxes[agent_id].pending
                    if agent_id in self._mailboxes
                    else 0
                ),
            }
            for agent_id, h in self._health.items()
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start background tasks (pool reaper, etc.)."""
        self._ensure_deps()
        await self._pool.start()
        logger.info("[Orchestrator] Started")

    async def shutdown(self) -> None:
        """Clean shutdown: cancel active tasks, release pool."""
        for task in self._active_tasks.values():
            if not task.done():
                task.cancel()
        self._active_tasks.clear()

        if self._pool:
            await self._pool.stop()

        logger.info("[Orchestrator] Shutdown complete")
