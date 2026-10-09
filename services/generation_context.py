from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class GenerationContext:
    identity: dict
    endpoint: str
    idempotency_key: str = ""
    task_id: str = ""
    prepare_only: bool = False
    request: object = None
    client_task_ids: list[str] = field(default_factory=list)


class PreparedImageRequest(Exception):
    """Internal control flow used to normalize a web task before enqueueing."""


@dataclass
class ExecutionCheckpoint:
    persist: Callable[..., None]
    raw_writer: Callable[[list], None]
    phase: str = "preparing"
    conversation_id: str = ""
    account_id: str = ""
    attempt: int = 0
    details: dict = field(default_factory=dict)

    def update(self, phase: str | None = None, **values) -> None:
        if phase:
            self.phase = phase
        if "conversation_id" in values:
            self.conversation_id = values["conversation_id"]
        if values.get("account_id"):
            self.account_id = values["account_id"]
        self.details.update(values)
        self.persist(phase=self.phase, **values)


execution_checkpoint: ContextVar[ExecutionCheckpoint | None] = ContextVar("image_execution_checkpoint", default=None)


def checkpoint(phase: str | None = None, **values) -> None:
    current = execution_checkpoint.get()
    if current:
        current.update(phase, **values)


def save_raw_images(items: list) -> None:
    current = execution_checkpoint.get()
    if current:
        current.raw_writer(items)
        current.update("raw_saved")
