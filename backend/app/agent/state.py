"""
State models for the orchestration engine using Pydantic v2.

These models define the data structures for task decomposition, execution tracking,
and conversation context management.
"""

import uuid
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class IntentResult(BaseModel):
    """Output of the intent router."""

    model_config = ConfigDict(frozen=False)

    task_type: Literal["simple_action", "multi_step", "query", "continuation"]
    domains: list[str] = Field(default_factory=list)  # e.g. ["filesystem", "desktop"]
    complexity: Literal["simple", "multi_step"]
    reasoning: str  # brief explanation of the classification


class PlanStep(BaseModel):
    """A single step in an execution plan."""

    model_config = ConfigDict(frozen=False)

    step_id: str
    description: str  # human-readable
    tool_hints: list[str] = Field(default_factory=list)  # suggested tool names
    depends_on: list[str] = Field(default_factory=list)  # step_ids that must complete first
    success_criteria: str  # what "done" means for this step
    status: Literal["pending", "active", "done", "failed", "retrying"] = "pending"


class ExecutionPlan(BaseModel):
    """Full task decomposition."""

    model_config = ConfigDict(frozen=False)

    plan_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    original_message: str
    steps: list[PlanStep] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        """Serialize to dict for SSE transmission."""
        return {
            "plan_id": self.plan_id,
            "original_message": self.original_message,
            "steps": [step.model_dump() for step in self.steps],
            "created_at": self.created_at.isoformat(),
        }

    def get_current_step(self) -> "PlanStep | None":
        """Return the active PlanStep or None."""
        for step in self.steps:
            if step.status in ("pending", "active", "retrying"):
                return step
        return None

    def update_step_status(self, step_id: str, status: Literal["pending", "active", "done", "failed", "retrying"]) -> bool:
        """Set a step's status by step_id. Returns True if found and updated."""
        for step in self.steps:
            if step.step_id == step_id:
                step.status = status
                return True
        return False


class StepResult(BaseModel):
    """Result of executing one plan step."""

    model_config = ConfigDict(frozen=False)

    step_id: str
    tool_name: str | None = None
    tool_input: dict | None = None
    tool_output: str | None = None
    observation: str  # post-verification result
    verified: bool
    retry_count: int = 0
    error: str | None = None


class TaskState(BaseModel):
    """Tracks in-progress task execution."""

    model_config = ConfigDict(frozen=False)

    task_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    plan: ExecutionPlan | None = None
    results: list[StepResult] = Field(default_factory=list)
    current_step_index: int = 0
    status: Literal["pending", "running", "done", "failed"] = "pending"
    summary: str = ""

    def to_dict(self) -> dict:
        """Serialize to dict for SSE transmission."""
        return {
            "task_id": self.task_id,
            "plan": self.plan.to_dict() if self.plan else None,
            "results": [result.model_dump() for result in self.results],
            "current_step_index": self.current_step_index,
            "status": self.status,
            "summary": self.summary,
        }


class ConversationState(BaseModel):
    """Per-conversation persistent state."""

    model_config = ConfigDict(frozen=False)

    conversation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    title: str = ""  # first-message preview shown in sidebar
    recent_messages: list[dict] = Field(default_factory=list)  # working window fed to LLM context
    all_messages: list[dict] = Field(default_factory=list)  # full history for persistence
    summary: str = ""  # rolling summary of older messages
    active_task: TaskState | None = None
    tool_outcomes: list[dict] = Field(default_factory=list)  # recent tool name+result pairs for context
    context_tokens_estimate: int = 0  # approximate token count of current context window
    task_goal: str = ""
    pending_steps: list[dict] = Field(default_factory=list)
    completed_steps: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
