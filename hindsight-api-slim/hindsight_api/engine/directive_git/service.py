"""Coordinate the configured Git ref and the bank's last successfully published revision."""

import asyncio
import logging
from dataclasses import dataclass
from uuid import UUID, uuid4

from hindsight_api.config import get_config
from hindsight_api.schema_context import get_current_schema

from . import publication
from .models import Conflict, PublicationPending, Rule
from .repository import ENROLLMENT_MESSAGE, Repository

logger = logging.getLogger(__name__)


def manager_for_bank(bank_id: str) -> "Manager | None":
    path = get_config().directive_git_repositories.get(get_current_schema(), {}).get(bank_id)
    return Manager(path) if path else None


@dataclass(frozen=True)
class Manager:
    path: str

    @staticmethod
    def _check_base(head, applied, expected):
        if expected is not None and expected != applied:
            raise Conflict("The expected revision differs from the published revision")
        if head != applied:
            if head is None:
                raise Conflict("The configured directive ref is missing")
            raise PublicationPending(head)

    async def _save(
        self,
        engine,
        bank_id,
        repo,
        base,
        before,
        after,
        message,
        ctx,
        history_parent=None,
        return_id=None,
        single_id=None,
    ):
        enrollment = message == ENROLLMENT_MESSAGE
        await publication.publish(
            engine,
            bank_id,
            base,
            None,
            before,
            after,
            dry_run=True,
            request_context=ctx,
            enroll_only=enrollment,
            single_id=single_id,
        )
        if ctx.cancellation:
            ctx.cancellation.raise_if_cancelled()
        if ctx.api_key_id:
            # The extension supplies an opaque key identifier, never the credential itself.
            try:
                actor = UUID(ctx.api_key_id)
            except ValueError:
                pass
            else:
                message += f"\n\nHindsight-Key-ID: {actor}"
        rev = await asyncio.to_thread(repo.commit, base, after, message, history_parent=history_parent)
        try:
            return await publication.publish(
                engine,
                bank_id,
                base,
                rev,
                before,
                after,
                dry_run=False,
                request_context=ctx,
                return_id=return_id,
                enroll_only=enrollment,
                single_id=single_id,
            )
        except Exception as exc:
            # The Git compare-and-swap succeeded. Recovery must use precisely this revision.
            logger.exception("Could not publish committed directive revision %s", rev)
            raise PublicationPending(rev) from exc

    async def create(
        self, engine, bank_id, *, name, content, priority, is_active, tags, request_context, expected_revision=None
    ):
        repo = await asyncio.to_thread(Repository, self.path)
        base = await publication.current_revision(engine, bank_id)
        head = await asyncio.to_thread(repo.head)
        self._check_base(head, base, expected_revision)
        before = await asyncio.to_thread(repo.read, base)
        rule = Rule(id=uuid4(), name=name, content=content, priority=priority, is_active=is_active, tags=tags or [])
        result = await self._save(
            engine,
            bank_id,
            repo,
            base,
            before,
            {**before, rule.id: rule},
            f"Create directive {rule.id}",
            request_context,
            return_id=rule.id,
            single_id=rule.id,
        )
        return result["directive"]

    async def _edit(self, engine, bank_id, directive_id, changes, ctx, expected):
        key = UUID(str(directive_id))
        base = await publication.owned_revision(engine, bank_id, key)
        if base is None:
            # The unmanaged fallback checks ownership again under the publication lock.
            if expected is not None:
                raise Conflict("The directive is outside the Git-managed set")
            return None
        repo = await asyncio.to_thread(Repository, self.path)
        self._check_base(await asyncio.to_thread(repo.head), base, expected)
        before = await asyncio.to_thread(repo.read, base)
        if key not in before:
            raise Conflict("The repository and the published directive inventory disagree")
        after = before.copy()
        if changes is None:
            del after[key]
            action = "Delete"
        else:
            after[key] = Rule.model_validate(
                {**before[key].model_dump(), **{k: v for k, v in changes.items() if v is not None}}
            )
            action = "Update"
        return await self._save(
            engine,
            bank_id,
            repo,
            base,
            before,
            after,
            f"{action} directive {key}",
            ctx,
            return_id=key if changes is not None else None,
            single_id=key,
        )

    async def update_if_managed(
        self, engine, bank_id, directive_id, changes, *, request_context, expected_revision=None
    ):
        result = await self._edit(engine, bank_id, directive_id, changes, request_context, expected_revision)
        return result["directive"] if result is not None else None

    async def delete_if_managed(self, engine, bank_id, directive_id, *, request_context, expected_revision=None):
        if await self._edit(engine, bank_id, directive_id, None, request_context, expected_revision):
            return True
        return None


async def _authorized_manager(engine, bank_id, request_context):
    # Authorize visibility of bank-wide status before reading any Git ref or reporting a binding.
    applied = await publication.applied_revision(engine, bank_id, request_context)
    manager = manager_for_bank(bank_id)
    if manager is None:
        raise ValueError("Git publication is not configured for this bank")
    repo = await asyncio.to_thread(Repository, manager.path)
    return manager, repo, await asyncio.to_thread(repo.head), applied


async def sync_directives(engine, bank_id, *, revision=None, expected_revision=None, apply=False, request_context):
    _, repo, head, base = await _authorized_manager(engine, bank_id, request_context)
    if revision is None:
        if apply or expected_revision is not None:
            raise ValueError("A target revision is required")
        return {"head": head, "revision": base, "pending": head != base}
    if revision != head:
        raise Conflict("The requested revision is not the current directive ref")
    if base != revision and base != expected_revision:
        raise Conflict("The published revision differs from the expected revision")
    if base and not await asyncio.to_thread(repo.is_ancestor, base, revision):
        raise Conflict("To roll back a published directive change, commit a revert")
    before = await asyncio.to_thread(repo.read, base)
    after = await asyncio.to_thread(repo.read, revision)
    return await publication.publish(
        engine,
        bank_id,
        base,
        revision,
        before,
        after,
        dry_run=not apply,
        request_context=request_context,
        enroll_only=base is None and await asyncio.to_thread(repo.is_enrollment, revision),
    )


async def adopt_directives(engine, bank_id, *, directive_ids, history_parent, apply, request_context):
    """Establish a baseline from explicitly selected live rows, preserving their metadata and UUIDs."""
    manager, repo, head, base = await _authorized_manager(engine, bank_id, request_context)
    if head is not None or base is not None:
        raise Conflict("Adoption requires a new directive ref and an unpublished bank")
    if not directive_ids or len(set(directive_ids)) != len(directive_ids):
        raise ValueError("Select distinct existing directive IDs to adopt")
    after = {}
    for key in directive_ids:
        row = await engine.get_directive(bank_id, str(key), request_context=request_context)
        if row is None:
            raise ValueError(f"Unknown directive {key}")
        after[key] = Rule.model_validate({k: row[k] for k in Rule.model_fields})
    if not apply:
        return await publication.publish(
            engine, bank_id, None, None, {}, after, dry_run=True, request_context=request_context, enroll_only=True
        )
    return await manager._save(
        engine, bank_id, repo, None, {}, after, ENROLLMENT_MESSAGE, request_context, history_parent
    )


async def detach_directives(engine, bank_id, *, expected_revision: str, request_context):
    """Release database ownership only after the operator has quiesced this bank's writers."""
    _, _, head, applied = await _authorized_manager(engine, bank_id, request_context)
    if head != applied or applied != expected_revision:
        raise Conflict("Publish pending changes and supply the active Git revision before detaching")
    return await publication.detach(engine, bank_id, expected_revision, request_context)
