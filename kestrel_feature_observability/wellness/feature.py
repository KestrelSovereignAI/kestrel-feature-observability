"""
Operational Wellness Feature for agent self-awareness.

Provides real-time awareness of the agent's operational state through
5 measurable dimensions:
1. Constitutional Friction - rate of denied/blocked actions
2. Context Pressure - context window utilization
3. Interaction Depth - quality of recent interactions
4. Session Continuity - engagement regularity
5. Memory Health - memory system status

The overall wellness score is a weighted average of these dimensions,
giving the agent (and its operator) a clear picture of operational health.
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from kestrel_sdk.features.base import Feature, tool
from kestrel_sdk.tools.base import ToolCategory
from kestrel_sdk.tools.result import ToolResult

from kestrel_feature_observability.wellness.metrics import (
    ContextPressureCalculator,
    FrictionCalculator,
    InteractionDepthCalculator,
    MemoryHealthCalculator,
    SessionContinuityCalculator,
)

logger = logging.getLogger(__name__)

# COUNCIL CONDITION: Wellness metrics are TELEMETRY-ONLY.
# They must NEVER be injected into the agent's system prompt or context window.
# The agent can observe its own metrics via tools, but metrics do not influence
# LLM reasoning directly.
# Ref: Council Session 82ce894a - unanimous condition from all 3 members.
WELLNESS_TELEMETRY_ONLY = True  # Enforced by council decision


class WellnessFeature(Feature):
    """
    Agent operational wellness monitoring (TELEMETRY-ONLY).

    Measures health across 5 dimensions and stores checkpoints
    for historical trend analysis. Designed to give the agent
    self-awareness of its operational state.

    COUNCIL CONDITION (Session 82ce894a):
    Wellness metrics are telemetry-only by default. They are accessible
    via tool calls (observation), but must NEVER be injected into the
    agent's system prompt or context window. This enforces a strict
    observation/action boundary -- the agent can read its own metrics,
    but the metrics do not directly influence LLM reasoning.
    """

    @property
    def tool_description(self) -> str:
        return (
            "Agent wellness - check operational health, context pressure, "
            "and interaction quality"
        )

    async def initialize(self):
        """Initialize the wellness feature and create checkpoint table."""
        self._db = None
        self._agent_id = ""

        # Get database from agent storage
        if hasattr(self.agent, "storage") and self.agent.storage:
            if hasattr(self.agent.storage, "db"):
                self._db = self.agent.storage.db
            elif hasattr(self.agent.storage, "database"):
                self._db = self.agent.storage.database

        # Fallback: raw storage
        if self._db is None and hasattr(self.agent, "_raw_storage"):
            raw = self.agent._raw_storage
            if hasattr(raw, "db"):
                self._db = raw.db

        # Agent identity (DID is the canonical source of truth)
        self._agent_id = self.agent.did

        # Create wellness_checkpoints table
        if self._db:
            try:
                await self._db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS wellness_checkpoints (
                        id TEXT PRIMARY KEY,
                        agent_id TEXT NOT NULL,
                        metrics_json TEXT NOT NULL,
                        overall_score REAL NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                await self._db.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_wellness_checkpoints_agent
                    ON wellness_checkpoints(agent_id, created_at DESC)
                    """
                )
                logger.info("WellnessFeature initialized")
            except Exception as e:
                logger.warning(f"WellnessFeature: could not create table: {e}")

        # Initialize calculators
        self._friction = FrictionCalculator()
        self._context_pressure = ContextPressureCalculator()
        self._interaction_depth = InteractionDepthCalculator()
        self._session_continuity = SessionContinuityCalculator()
        self._memory_health = MemoryHealthCalculator()

    @tool(
        "wellness_check",
        "Check agent operational wellness across 5 dimensions",
        category=ToolCategory.SYSTEM,
        command_prefix="!wellness",
    )
    async def wellness_check(self) -> ToolResult:
        """Run all metric calculators, compute overall score, save checkpoint.

        Returns wellness metrics as a tool response (telemetry-only).

        COUNCIL CONDITION: This data is returned to the tool caller only.
        It is NOT injected into the system prompt or agent context window.

        Returns:
            ToolResult with per-dimension metrics, overall score, and
            checkpoint id. ``ToolResult.partial`` if any dimension's
            calculator failed (the others still ran) so the LLM can't
            narrate a clean wellness check when some dimensions are
            unmeasured.
        """
        metrics: Dict[str, Any] = {}

        # 1. Constitutional Friction
        try:
            metrics["constitutional_friction"] = await self._friction.measure(
                self._db, self._agent_id
            )
        except Exception as e:
            logger.error(f"Friction calculator error: {e}")
            metrics["constitutional_friction"] = {
                "friction_rate": 0.0,
                "error": str(e),
            }

        # 2. Context Pressure
        try:
            metrics["context_pressure"] = await self._context_pressure.measure(
                self.agent
            )
        except Exception as e:
            logger.error(f"Context pressure calculator error: {e}")
            metrics["context_pressure"] = {"pressure": 0.0, "error": str(e)}

        # 3. Interaction Depth
        try:
            metrics["interaction_depth"] = await self._interaction_depth.measure(
                self._db, self._agent_id
            )
        except Exception as e:
            logger.error(f"Interaction depth calculator error: {e}")
            metrics["interaction_depth"] = {"depth_score": 0.0, "error": str(e)}

        # 4. Session Continuity
        try:
            metrics["session_continuity"] = await self._session_continuity.measure(
                self._db, self._agent_id
            )
        except Exception as e:
            logger.error(f"Session continuity calculator error: {e}")
            metrics["session_continuity"] = {
                "continuity_score": 0.0,
                "error": str(e),
            }

        # 5. Memory Health
        try:
            metrics["memory_health"] = await self._memory_health.measure(
                self._db, self._agent_id
            )
        except Exception as e:
            logger.error(f"Memory health calculator error: {e}")
            metrics["memory_health"] = {"health_score": 0.0, "error": str(e)}

        # Wrap the post-measurement phase (codex round-1 finding
        # #3): a calculator that returns a malformed non-dict (e.g.
        # ``None``, an int, a Pydantic model that doesn't expose
        # ``.get``) would otherwise raise out of
        # ``_calculate_overall`` or the failed_dims comprehension
        # and escape the @tool envelope. Catching here means that
        # adapter glitch lands in ToolResult.failed, not as a raised
        # exception.
        try:
            overall = self._calculate_overall(metrics)
            failed_dims = [
                name for name, data in metrics.items()
                if isinstance(data, dict) and data.get("error")
            ]
        except Exception as e:
            logger.error(f"Wellness post-calc phase failed: {e}", exc_info=True)
            return ToolResult.failed(
                f"Wellness post-calculation failed: {e}",
                data={
                    "agent_id": self._agent_id,
                    "raw_metrics": metrics,
                    "warning": (
                        "a calculator returned a malformed result; the "
                        "raw_metrics field shows what was returned"
                    ),
                },
            )

        # Save checkpoint
        checkpoint_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        checkpoint_save_error: Optional[str] = None
        if self._db:
            try:
                await self._db.execute(
                    """
                    INSERT INTO wellness_checkpoints
                    (id, agent_id, metrics_json, overall_score, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        checkpoint_id,
                        self._agent_id,
                        json.dumps(metrics),
                        overall,
                        now,
                    ),
                )
            except Exception as e:
                checkpoint_save_error = str(e)
                logger.warning(f"Failed to save wellness checkpoint: {e}")
        else:
            checkpoint_save_error = "no database available"
        data = {
            "checkpoint_id": checkpoint_id,
            "agent_id": self._agent_id,
            "overall_score": overall,
            "dimensions": metrics,
            "created_at": now,
            "dimensions_with_errors": failed_dims,
        }
        if checkpoint_save_error:
            data["checkpoint_save_error"] = checkpoint_save_error

        # Status downgrade: any per-dim failure OR a checkpoint
        # save failure → PARTIAL. Both work but the LLM must see
        # the partial signal to narrate honestly.
        #
        # Don't put the checkpoint_id in the confirmation when the
        # save failed — an operator querying history with that ID
        # would find nothing (claude review #4). Mention "not saved"
        # instead so the LLM can be honest about the missing
        # persistence.
        if failed_dims or checkpoint_save_error:
            errors_summary = []
            if failed_dims:
                errors_summary.append(
                    f"{len(failed_dims)} dimension(s) failed: {', '.join(failed_dims)}"
                )
            if checkpoint_save_error:
                errors_summary.append(
                    f"checkpoint save failed: {checkpoint_save_error}"
                )

            if checkpoint_save_error:
                confirmation_prefix = (
                    f"Wellness measured (overall {overall:.2f}; "
                    f"NOT saved to history)"
                )
            else:
                # Save succeeded; per-dim failures only. Include the
                # checkpoint_id so operators can find this record.
                confirmation_prefix = (
                    f"Wellness checkpoint {checkpoint_id[:8]} "
                    f"(overall {overall:.2f})"
                )
            return ToolResult.partial(
                confirmation=(
                    f"{confirmation_prefix} — partial: {'; '.join(errors_summary)}"
                ),
                error="; ".join(errors_summary),
                data=data,
            )

        return ToolResult.ok(
            confirmation=(
                f"Wellness checkpoint {checkpoint_id[:8]} saved "
                f"(overall {overall:.2f})"
            ),
            data=data,
        )

    @tool(
        "wellness_history",
        "View wellness trends over time",
        category=ToolCategory.SYSTEM,
        command_prefix="!wellness-history",
    )
    async def wellness_history(self, limit: int = 10) -> ToolResult:
        """Query wellness checkpoints ordered by created_at DESC.

        Returns wellness history as a tool response (telemetry-only).

        COUNCIL CONDITION: This data is returned to the tool caller only.
        It is NOT injected into the system prompt or agent context window.
        """
        if not self._db:
            return ToolResult.failed(
                "Database not available",
                data={"reason": "agent has no storage backend"},
            )

        # Cover both the DB query AND the per-row mapping — schema
        # drift could cause IndexError on row[N] (claude review #2).
        # Track json parse failures separately (codex round-1
        # finding #2): silently swallowing them was partial data
        # loss masquerading as OK; surface them so the LLM can
        # downgrade to PARTIAL.
        checkpoints = []
        parse_failures: List[Dict[str, Any]] = []
        try:
            exists = await self._db.table_exists("wellness_checkpoints")
            if not exists:
                return ToolResult.ok(
                    confirmation="No wellness checkpoints recorded yet",
                    data={"checkpoints": [], "count": 0, "trend": "no_data"},
                )

            rows = await self._db.fetchall(
                """
                SELECT id, overall_score, metrics_json, created_at
                FROM wellness_checkpoints
                WHERE agent_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (self._agent_id, limit),
            )
            for row in rows:
                try:
                    metrics = json.loads(row[2]) if row[2] else {}
                except (json.JSONDecodeError, TypeError) as parse_err:
                    metrics = {}
                    parse_failures.append({
                        "id": row[0],
                        "error": str(parse_err),
                    })
                checkpoints.append(
                    {
                        "id": row[0],
                        "overall_score": row[1],
                        "dimensions": metrics,
                        "created_at": row[3],
                    }
                )
        except Exception as e:
            logger.error(f"Failed to get wellness history: {e}")
            return ToolResult.failed(
                str(e), data={"limit_requested": limit}
            )

        # Trend: stable / improving / declining / no_data
        trend = "stable"
        if len(checkpoints) >= 2:
            latest = checkpoints[0]["overall_score"] or 0
            previous = checkpoints[1]["overall_score"] or 0
            diff = latest - previous
            if diff > 0.05:
                trend = "improving"
            elif diff < -0.05:
                trend = "declining"
        elif len(checkpoints) <= 1:
            # 1 sample is insufficient for trend; don't say "stable".
            trend = "insufficient_data"

        data = {
            "checkpoints": checkpoints,
            "count": len(checkpoints),
            "trend": trend,
            "limit_requested": limit,
        }

        # Parse failures = partial data loss. Surface them and
        # downgrade to PARTIAL so the LLM can warn the user that
        # some checkpoints had unreadable dimension data (codex
        # round-1 finding #2).
        if parse_failures:
            data["parse_failures"] = parse_failures
            return ToolResult.partial(
                confirmation=(
                    f"Wellness history: {len(checkpoints)} checkpoint(s), "
                    f"trend={trend}; "
                    f"{len(parse_failures)} checkpoint(s) had unreadable "
                    f"dimension JSON (treated as empty)"
                ),
                error=(
                    f"{len(parse_failures)} of {len(checkpoints)} "
                    f"checkpoints had corrupt metrics_json"
                ),
                data=data,
            )

        return ToolResult.ok(
            confirmation=(
                f"Wellness history: {len(checkpoints)} checkpoint(s), "
                f"trend={trend} (limit requested: {limit})"
            ),
            data=data,
        )

    @tool(
        "wellness_export",
        "Export wellness data for sovereignty packages",
        category=ToolCategory.SYSTEM,
    )
    async def wellness_export(self) -> ToolResult:
        """Return all wellness checkpoints for sovereignty export.

        Returns wellness export as a tool response (telemetry-only).

        COUNCIL CONDITION: This data is returned to the tool caller only.
        It is NOT injected into the system prompt or agent context window.
        """
        if not self._db:
            return ToolResult.failed(
                "Database not available",
                data={"reason": "agent has no storage backend"},
            )

        # Cover both the DB query AND the per-row mapping. Track
        # json parse failures separately so an export with corrupt
        # rows lands in PARTIAL — silently turning corrupt
        # metrics_json into ``{}`` is partial data loss
        # masquerading as a clean export (codex round-1 finding #2).
        checkpoints = []
        parse_failures: List[Dict[str, Any]] = []
        try:
            exists = await self._db.table_exists("wellness_checkpoints")
            if not exists:
                return ToolResult.ok(
                    confirmation=(
                        "No wellness checkpoints to export "
                        "(table does not exist yet)"
                    ),
                    data={
                        "checkpoints": [],
                        "count": 0,
                        "export_format": "v1",
                        "agent_id": self._agent_id,
                    },
                )

            rows = await self._db.fetchall(
                """
                SELECT id, agent_id, overall_score, metrics_json, created_at
                FROM wellness_checkpoints
                WHERE agent_id = ?
                ORDER BY created_at ASC
                """,
                (self._agent_id,),
            )
            for row in rows:
                try:
                    metrics = json.loads(row[3]) if row[3] else {}
                except (json.JSONDecodeError, TypeError) as parse_err:
                    metrics = {}
                    parse_failures.append({
                        "id": row[0],
                        "error": str(parse_err),
                    })
                checkpoints.append(
                    {
                        "id": row[0],
                        "agent_id": row[1],
                        "overall_score": row[2],
                        "dimensions": metrics,
                        "created_at": row[4],
                    }
                )
        except Exception as e:
            logger.error(f"Failed to export wellness data: {e}")
            return ToolResult.failed(
                str(e), data={"agent_id": self._agent_id}
            )

        data = {
            "checkpoints": checkpoints,
            "count": len(checkpoints),
            "export_format": "v1",
            "agent_id": self._agent_id,
        }

        if parse_failures:
            data["parse_failures"] = parse_failures
            return ToolResult.partial(
                confirmation=(
                    f"Exported {len(checkpoints)} wellness checkpoint(s) "
                    f"(format v1); {len(parse_failures)} had unreadable "
                    f"dimension JSON (treated as empty)"
                ),
                error=(
                    f"{len(parse_failures)} of {len(checkpoints)} "
                    f"exported checkpoints had corrupt metrics_json"
                ),
                data=data,
            )

        return ToolResult.ok(
            confirmation=(
                f"Exported {len(checkpoints)} wellness checkpoint(s) "
                f"(format v1)"
            ),
            data=data,
        )

    def _calculate_overall(self, metrics: Dict[str, Any]) -> float:
        """Calculate weighted overall wellness score.

        Weights:
            constitutional_friction: 0.30 (most important - inverted)
            interaction_depth:       0.25
            memory_health:           0.20
            session_continuity:      0.15
            context_pressure:        0.10 (inverted)

        For friction and context_pressure the raw rate is inverted
        since lower values are healthier.

        Args:
            metrics: Dict of dimension name -> measurement dict

        Returns:
            Weighted average clamped to 0.0-1.0
        """
        weights = {
            "constitutional_friction": 0.30,
            "interaction_depth": 0.25,
            "memory_health": 0.20,
            "session_continuity": 0.15,
            "context_pressure": 0.10,
        }

        total_weight = 0.0
        weighted_sum = 0.0

        for dimension, weight in weights.items():
            data = metrics.get(dimension, {})
            if not data or data.get("error"):
                continue

            if dimension == "constitutional_friction":
                # Lower friction = better health
                score = 1.0 - data.get("friction_rate", 0.0)
            elif dimension == "context_pressure":
                # Lower pressure = better health
                score = 1.0 - data.get("pressure", 0.0)
            elif dimension == "interaction_depth":
                score = data.get("depth_score", 0.0)
            elif dimension == "session_continuity":
                score = data.get("continuity_score", 0.0)
            elif dimension == "memory_health":
                score = data.get("health_score", 0.0)
            else:
                continue

            weighted_sum += weight * score
            total_weight += weight

        if total_weight == 0:
            return 0.0

        return round(max(0.0, min(weighted_sum / total_weight, 1.0)), 4)
