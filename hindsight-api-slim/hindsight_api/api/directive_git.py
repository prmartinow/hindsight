"""Bank administration endpoints for optional Git-backed directives."""

from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from hindsight_api.engine.directive_git.models import Conflict, PublicationPending
from hindsight_api.engine.directive_git.service import adopt_directives, detach_directives, sync_directives
from hindsight_api.extensions.operation_validator import OperationValidationError
from hindsight_api.models import RequestContext


class GitSyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: str | None = None
    expected_revision: str | None = None
    apply: bool = False


class GitAdoptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    directive_ids: list[UUID] = Field(min_length=1, max_length=4096)
    history_parent: str | None = None
    apply: bool = False


class GitDetachRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: str


class GitDetachResponse(BaseModel):
    revision: None
    detached_revision: str


class GitPublicationStatus(BaseModel):
    head: str | None
    revision: str | None
    pending: bool


class GitPublicationPlan(BaseModel):
    revision: str | None
    created: list[UUID]
    updated: list[UUID]
    deleted: list[UUID]
    enrolled: list[UUID]


def conflict_response(exc: Conflict) -> HTTPException:
    if isinstance(exc, PublicationPending):
        return HTTPException(409, detail={"state": "pending", "revision": exc.revision, "message": str(exc)})
    return HTTPException(409, detail=str(exc))


def register_directive_git_routes(app: FastAPI, get_request_context, audited) -> None:
    @app.post(
        "/v1/default/banks/{bank_id}/directive-git/sync",
        response_model=GitPublicationStatus | GitPublicationPlan,
        operation_id="sync_git_directives",
        tags=["Directives"],
        summary="Inspect, preview, or publish the configured Git revision",
    )
    @audited("sync_directives", request_param="body")
    async def sync(bank_id: str, body: GitSyncRequest, request_context: RequestContext = Depends(get_request_context)):
        try:
            return await sync_directives(
                app.state.memory, bank_id, **body.model_dump(), request_context=request_context
            )
        except Conflict as exc:
            raise conflict_response(exc) from exc
        except OperationValidationError as exc:
            raise HTTPException(exc.status_code, detail=exc.reason) from exc
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc)) from exc

    @app.post(
        "/v1/default/banks/{bank_id}/directive-git/adopt",
        response_model=GitPublicationPlan,
        operation_id="adopt_git_directives",
        tags=["Directives"],
        summary="Enroll existing directives by UUID in a new Git publication",
    )
    @audited("adopt_git_directives", request_param="body")
    async def adopt(
        bank_id: str, body: GitAdoptRequest, request_context: RequestContext = Depends(get_request_context)
    ):
        try:
            return await adopt_directives(
                app.state.memory, bank_id, **body.model_dump(), request_context=request_context
            )
        except Conflict as exc:
            raise conflict_response(exc) from exc
        except OperationValidationError as exc:
            raise HTTPException(exc.status_code, detail=exc.reason) from exc
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc)) from exc

    @app.post(
        "/v1/default/banks/{bank_id}/directive-git/detach",
        response_model=GitDetachResponse,
        operation_id="detach_git_directives",
        tags=["Directives"],
        summary="Release Git ownership while keeping the published directives",
    )
    @audited("detach_git_directives", request_param="body")
    async def detach(
        bank_id: str, body: GitDetachRequest, request_context: RequestContext = Depends(get_request_context)
    ):
        try:
            return await detach_directives(
                app.state.memory, bank_id, expected_revision=body.expected_revision, request_context=request_context
            )
        except Conflict as exc:
            raise conflict_response(exc) from exc
        except OperationValidationError as exc:
            raise HTTPException(exc.status_code, detail=exc.reason) from exc
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc)) from exc
