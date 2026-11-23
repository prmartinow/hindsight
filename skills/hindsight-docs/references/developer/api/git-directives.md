# Git-backed directives

A bank can publish its standing rules from a local bare Git repository. Each published rule keeps its Hindsight UUID, text, name, tags, priority and activation state. Reflect and the directive read APIs use the last snapshot published to the database.

## Set up a bank

Create a persistent bare repository owned by the Hindsight service account. Configure every writer serving the bank with the same repository and ref. The outer configuration key is the authenticated tenant's database schema. Each repository belongs to exactly one schema and bank.

```sh
git init --bare /var/lib/hindsight/rules.git
export HINDSIGHT_API_DIRECTIVE_GIT_REPOSITORIES='{"public":{"example":"/var/lib/hindsight/rules.git"}}'
```

For an existing rulebook, a bare clone of its repository preserves its objects. At enrollment, supply its previous full commit ID as `history_parent` to retain that ancestry on the new `directives` branch. Verify the selected live rule text against the old files before enrollment. Make a backup of the database and repository together. All service instances should run the migration and feature-capable code before enrolling a bank.

## Enroll selected rules

Send these requests to the authenticated bank path, for example `/v1/default/banks/example`. The bank must already exist. Publication requires bank-wide directive read access and authority for the proposed directive writes. A tag-scoped caller cannot publish a whole rulebook.

1. List existing directives, including inactive ones, and select the UUIDs to manage.
2. POST `/directive-git/adopt` with `{"directive_ids":["<uuid>"]}`. Verify that the preview lists the requested IDs under `enrolled` and that `created`, `updated`, and `deleted` are empty.
3. Repeat with `"apply":true`. Hindsight commits the selected rows to `refs/heads/directives` and records the active revision. Adoption runs once on an empty directive ref. Unselected existing rows keep their ordinary write path.
4. Use normal directive create, edit and delete actions in the Control Plane, REST or MCP. New directives created in this bank join the Git-managed set. To reject an edit based on a stale revision, supply the last observed SHA in the REST header `X-Hindsight-Expected-Revision` or the MCP argument `expected_revision`.

## Preview, publish and recover

POST `/directive-git/sync` with `{}` for the configured branch head, applied revision and pending flag. Agents can use `sync_directives` when an operator includes it in the server MCP allowlist.

For a pending change, send `{"revision":"<head>","expected_revision":"<applied SHA>"}` to preview the live changes. Use JSON `null` for a bank with no previously published revision. Repeat with `"apply":true` to publish. Hindsight checks authorization, ownership and previous values, then writes the changed rows and revision in one transaction. Repeating the same revision is safe after a restart or a lost response. A normal edit that committed to Git but failed to publish returns HTTP 409 with the pending SHA. During recovery, readers see the last successful publication.

Each file on the directives branch is named `<uuid>.md`. A single-line JSON object between front-matter delimiters carries the metadata. The remaining text, including its trailing newline when present, is the directive content. A rename changes the `name` field. Removing an enrolled UUID from a snapshot deletes that directive. A snapshot containing an ambiguous name, unrelated ID collision, or independently changed managed row is rejected.

```markdown
---
{"name":"Style","priority":10,"is_active":true,"tags":["writing"]}
---
Use short paragraphs.
```

Operators can commit directly to the configured branch and publish a reviewed SHA using the same preview and apply operations. To roll back a change, commit a revert on that branch and publish the resulting SHA. A bank-template import uses ordinary directive writes for its entries. Use a Git snapshot to publish several changes atomically. Full-bank archive restore and clone into a configured destination are guarded to protect the rulebook.

## Return to ordinary database writes

Quiesce writers for the bank and publish pending changes. With bank configuration write permission, POST `/directive-git/detach` with `{"expected_revision":"<published SHA>"}`, then remove the bank binding from every instance before resuming writes. Detaching preserves directive IDs and values as well as Git history. Removing the configuration while the bank still has a publication receipt blocks subsequent directive writes and bank deletion until an operator restores the binding.
