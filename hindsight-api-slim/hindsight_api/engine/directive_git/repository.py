"""Read and publish immutable directive snapshots in an operator-provisioned bare Git repository."""

import hashlib
import io
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from .models import Conflict, Rule

ENROLLMENT_MESSAGE = "Enroll existing Hindsight directives"
_REF = "refs/heads/directives"
_MAX_RULES = 4096
_MAX_BLOB_BYTES = 1024 * 1024
_MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class _Entry:
    rule_id: UUID
    object_id: bytes
    size: int


def _encode(rule: Rule) -> bytes:
    fields = rule.model_dump(mode="json", exclude={"id", "content"})
    header = json.dumps(fields, ensure_ascii=False, sort_keys=True)
    return f"---\n{header}\n---\n{rule.content}".encode()


def _decode(rule_id: UUID, data: bytes) -> Rule:
    # A single JSON line is valid YAML front matter; splitting only the header
    # preserves arbitrary Markdown (including front matter) and trailing whitespace.
    parts = data.decode().split("\n", 3)
    if len(parts) != 4 or parts[0] != "---" or parts[2] != "---":
        raise ValueError("Invalid directive front matter")
    fields = json.loads(parts[1])
    if not isinstance(fields, dict) or fields.keys() != {"name", "is_active", "priority", "tags"}:
        raise ValueError("Invalid directive metadata fields")
    return Rule.model_validate({**fields, "id": rule_id, "content": parts[3]}, strict=True)


class Repository:
    """The directives ref of a single bank. No checkout, index, hooks, or Git remote is used."""

    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        settings = self._git("rev-parse", "--is-bare-repository", "--show-object-format=storage").splitlines()
        if settings not in ([b"true", b"sha1"], [b"true", b"sha256"]):
            raise ValueError("Configure a bare Git repository for directives")
        self._hash_name = settings[1].decode("ascii")
        self._oid_length = 40 if self._hash_name == "sha1" else 64

    def _run(self, *args: str, data: bytes = b"") -> subprocess.CompletedProcess[bytes]:
        # Inherited GIT_DIR, alternates, namespace or config can redirect Git
        # away from the bank selected by the server.
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(
            GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_NO_REPLACE_OBJECTS="1", GIT_ALLOW_PROTOCOL=""
        )
        return subprocess.run(
            [
                "git",
                f"--git-dir={self.path}",
                "-c",
                f"core.hooksPath={os.devnull}",
                "-c",
                "user.name=Hindsight",
                "-c",
                "user.email=hindsight@localhost",
                *args,
            ],
            input=data,
            capture_output=True,
            timeout=30,
            env=env,
        )

    def _git(self, *args: str, data: bytes = b"") -> bytes:
        result = self._run(*args, data=data)
        if result.returncode:
            raise RuntimeError(f"Directive repository operation failed ({args[0]})")
        return result.stdout

    def _validate_revision(self, revision: str) -> None:
        if len(revision) != self._oid_length or not re.fullmatch("[0-9a-f]+", revision):
            raise ValueError("A full Git commit ID is required")

    def head(self) -> str | None:
        result = self._run("rev-parse", "--quiet", "--verify", _REF)
        if result.returncode == 1:
            return None
        if result.returncode:
            raise RuntimeError("Cannot read the directive ref")
        return result.stdout.decode("ascii").strip()

    def is_enrollment(self, revision: str) -> bool:
        """A retry of initial enrollment must still find every selected directive in the live bank."""
        self._validate_revision(revision)
        return self._git("log", "-1", "--format=%s", revision).rstrip(b"\n") == ENROLLMENT_MESSAGE.encode()

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        self._validate_revision(ancestor)
        self._validate_revision(descendant)
        result = self._run("merge-base", "--is-ancestor", ancestor, descendant)
        if result.returncode not in (0, 1):
            raise ValueError("Unknown directive revision")
        return result.returncode == 0

    def _entries(self, revision: str) -> list[_Entry]:
        self._validate_revision(revision)
        if self._git("cat-file", "-t", revision).strip() != b"commit":
            raise ValueError("Directive revision must be a commit")
        # Bound ls-tree output before decoding file names from an externally authored snapshot.
        if int(self._git("cat-file", "-s", f"{revision}^{{tree}}")) > _MAX_RULES * 96:
            raise ValueError("Directive snapshot tree is too large")
        listing = self._git("ls-tree", "-lz", revision)
        entries: list[_Entry] = []
        total = 0
        for line in listing.split(b"\0")[:-1]:
            info, name = line.split(b"\t", 1)
            mode, kind, object_id, size = info.split()
            if mode != b"100644" or kind != b"blob" or not name.endswith(b".md"):
                raise ValueError("Expected regular UUID-named Markdown files in the directive tree")
            rule_id = UUID(name[:-3].decode("ascii"))
            if name != f"{rule_id}.md".encode():
                raise ValueError("Expected canonical directive UUID")
            length = int(size)
            total += length
            if len(entries) >= _MAX_RULES or length > _MAX_BLOB_BYTES or total > _MAX_SNAPSHOT_BYTES:
                raise ValueError("Directive snapshot exceeds its size limit")
            entries.append(_Entry(rule_id, object_id, length))
        return entries

    def read(self, revision: str | None) -> dict[UUID, Rule]:
        if revision is None:
            return {}
        entries = self._entries(revision)
        # Read all blobs in one Git process. Sizes were checked before asking
        # Git to decompress any of the rule bodies.
        ids = b"".join(entry.object_id + b"\n" for entry in entries)
        blobs = io.BytesIO(self._git("cat-file", "--batch", data=ids))
        rules = {}
        for entry in entries:
            expected = b"%s blob %d\n" % (entry.object_id, entry.size)
            if blobs.readline() != expected:
                raise ValueError("Incomplete Git directive blob")
            rules[entry.rule_id] = _decode(entry.rule_id, blobs.read(entry.size))
            if blobs.read(1) != b"\n":
                raise ValueError("Invalid Git directive blob")
        return rules

    def _write_tree(self, expected_head: str | None, rules: dict[UUID, Rule]) -> str:
        if len(rules) > _MAX_RULES:
            raise ValueError("Too many rules in directive snapshot")
        # Most edits change one file. Reuse known objects so writing one edit costs
        # a fixed number of Git subprocesses, independent of the rulebook size.
        known = {entry.object_id for entry in self._entries(expected_head)} if expected_head else set()
        total = 0
        entries = bytearray()
        for rule_id, rule in rules.items():
            if rule_id != rule.id:
                raise ValueError("Directive ID does not match its filename")
            blob = _encode(rule)
            total += len(blob)
            if len(blob) > _MAX_BLOB_BYTES or total > _MAX_SNAPSHOT_BYTES:
                raise ValueError("Directive snapshot exceeds its size limit")
            digest = hashlib.new(self._hash_name, f"blob {len(blob)}\0".encode(), usedforsecurity=False)
            digest.update(blob)
            oid = digest.hexdigest().encode()
            if oid not in known:
                oid = self._git("hash-object", "-w", "--stdin", data=blob).strip()
            entries.extend(b"100644 blob %s\t%s.md\n" % (oid, str(rule_id).encode()))
        return self._git("mktree", data=bytes(entries)).decode("ascii").strip()

    def commit(
        self, expected_head: str | None, rules: dict[UUID, Rule], message: str, history_parent: str | None = None
    ) -> str:
        if expected_head is not None:
            self._validate_revision(expected_head)
        if self.head() != expected_head:
            raise Conflict("The directive branch changed; read its current revision")
        if history_parent is not None:
            self._validate_revision(history_parent)
            if expected_head is not None or self._git("cat-file", "-t", history_parent).strip() != b"commit":
                raise ValueError("A history parent must be a commit supplied during initial enrollment")
        tree = self._write_tree(expected_head, rules)
        if expected_head and self._git("rev-parse", f"{expected_head}^{{tree}}").decode("ascii").strip() == tree:
            revision = expected_head
        else:
            parent = expected_head or history_parent
            parents = ["-p", parent] if parent else []
            revision = (
                self._git("commit-tree", tree, *parents, "-F", "-", data=message.encode()).decode("ascii").strip()
            )
        # CAS applies even to no-op edits. A concurrent writer can win at any
        # point after our initial check, including while Git is building blobs.
        old = expected_head or "0" * self._oid_length
        result = self._run("update-ref", "--no-deref", _REF, revision, old)
        if result.returncode:
            if self.head() != expected_head:
                raise Conflict("The directive branch changed; read its current revision")
            raise RuntimeError("Cannot update the directive ref")
        return revision
