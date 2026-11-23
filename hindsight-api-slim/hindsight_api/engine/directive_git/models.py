from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ..response_models import DIRECTIVE_PRIORITY_MAX, DIRECTIVE_PRIORITY_MIN


class Conflict(ValueError):
    """The caller's base revision or the live directive no longer matches."""


class PublicationPending(Conflict):
    def __init__(self, revision: str):
        self.revision = revision
        super().__init__(
            f"Git revision {revision} is committed; publication is pending. Preview or retry this revision."
        )


class Rule(BaseModel):
    """The complete, portable content of one managed directive."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    name: str
    content: str
    priority: int = Field(default=0, ge=DIRECTIVE_PRIORITY_MIN, le=DIRECTIVE_PRIORITY_MAX)
    is_active: bool = True
    tags: list[str] = Field(default_factory=list)
