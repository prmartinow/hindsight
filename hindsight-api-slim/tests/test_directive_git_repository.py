import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

from hindsight_api.engine.directive_git.models import Conflict, Rule
from hindsight_api.engine.directive_git.repository import Repository


def git(path: Path, *args: str, data: bytes | None = None) -> str:
    return (
        subprocess.check_output(
            ["git", "-C", str(path), "-c", "user.name=Tester", "-c", "user.email=tester@example.org", *args],
            input=data,
        )
        .decode()
        .strip()
    )


@pytest.fixture
def bare(tmp_path):
    path = tmp_path / "bank.git"
    git(tmp_path, "init", "--quiet", "--bare", str(path))
    return path


def test_immutable_snapshot_and_noop(bare, monkeypatch):
    repository = Repository(bare)
    monkeypatch.setenv("GIT_NAMESPACE", "unrelated")
    original = Rule(
        id=uuid4(), name="Résumé", content="---\nDraft \n---\n### A\n\n", priority=-15, is_active=False, tags=["é", "a"]
    )
    unchanged = Rule(id=uuid4(), name="two", content="body")
    first = repository.commit(None, {original.id: original, unchanged.id: unchanged}, "initial")
    assert repository.read(first) == {original.id: original, unchanged.id: unchanged}
    assert repository.commit(first, {unchanged.id: unchanged, original.id: original}, "same") == first
    renamed = original.model_copy(update={"name": "new", "tags": []})
    second = repository.commit(first, {renamed.id: renamed}, "rename and remove")
    assert second != first
    assert repository.is_ancestor(first, second)
    assert not repository.is_ancestor(second, first)
    assert repository.read(second) == {renamed.id: renamed}
    assert repository.read(first)[original.id] == original
    assert repository.head() == second


def test_competing_writers_publish_one_snapshot(bare):
    repository = Repository(bare)
    first = repository.commit(None, {}, "baseline")

    def edit(index):
        rule = Rule(id=uuid4(), name=f"r{index}", content="text")
        try:
            return repository.commit(first, {rule.id: rule}, "edit")
        except Conflict:
            return None

    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = list(executor.map(edit, range(8)))
    assert [sha for sha in outcomes if sha] == [repository.head()]
    assert len(repository.read(repository.head())) == 1
    with pytest.raises(Conflict):
        repository.commit(None, {}, "stale initialize")


def test_bad_snapshots_fail_before_ref_changes(bare):
    repository = Repository(bare)
    rule = Rule(id=uuid4(), name="example", content="a" * (1024 * 1024))
    with pytest.raises(ValueError, match="exceeds"):
        repository.commit(None, {rule.id: rule}, "too big")
    with pytest.raises(ValueError, match="filename"):
        repository.commit(None, {uuid4(): rule}, "bad map key")
    with pytest.raises(ValueError, match="full Git"):
        repository.read("--help")
    assert repository.head() is None


def test_migrating_from_existing_history(bare):
    blob = git(bare, "hash-object", "-w", "--stdin", data=b"old format\n")
    tree = git(bare, "mktree", data=f"100644 blob {blob}\tpolicy.md\n".encode())
    old = git(bare, "commit-tree", tree, "-m", "Original rulebook")
    git(bare, "update-ref", "refs/heads/original", old)
    repository = Repository(bare)
    with pytest.raises(ValueError):
        repository.read(old)
    first = repository.commit(None, {}, "Enroll existing Hindsight directives", history_parent=old)
    assert repository.is_enrollment(first)
    assert not repository.is_enrollment(old)
    assert repository.is_ancestor(old, first)
    assert git(bare, "rev-parse", "refs/heads/original") == old
    with pytest.raises(ValueError, match="initial enrollment"):
        repository.commit(first, {}, "replace parent", history_parent=old)


def test_does_not_touch_a_working_checkout(tmp_path):
    path = tmp_path / "checkout"
    git(tmp_path, "init", "--quiet", str(path))
    with pytest.raises(ValueError, match="bare Git"):
        Repository(path / ".git")


def test_accepts_sha256_repositories(tmp_path):
    path = tmp_path / "sha256.git"
    git(tmp_path, "init", "--quiet", "--bare", "--object-format=sha256", str(path))
    repo = Repository(path)
    rule = Rule(id=uuid4(), name="A", content="Some text")
    first = repo.commit(None, {rule.id: rule}, "Initial")
    assert len(first) == 64
    assert repo.read(first) == {rule.id: rule}
    assert repo.commit(first, {rule.id: rule}, "repeat") == first


def test_invalid_metadata_in_an_external_commit_is_rejected(bare):
    payload = b'---\n{"name":"x","priority":0,"is_active":"false","tags":[]}\n---\nBody'
    blob = git(bare, "hash-object", "-w", "--stdin", data=payload)
    tree = git(bare, "mktree", data=f"100644 blob {blob}\t{uuid4()}.md\n".encode())
    commit = git(bare, "commit-tree", tree, "-m", "external")
    with pytest.raises(ValueError, match="is_active"):
        Repository(bare).read(commit)
