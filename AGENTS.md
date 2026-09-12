# AI Agent Instructions

## Output Rules

- Be concise and direct.
- Omit introductory and concluding filler.
- Return only the requested code, focused diff, or brief bullet points.
- Show only changed sections unless full context is required for correctness.
- Include necessary test results, assumptions, warnings, and blockers.

## Context-Efficient Workflow

- Start from the user's named file, symbol, failing test, command, or diagnostic.
- Form one local hypothesis before exploring broadly and identify the cheapest check that can disprove it.
- Search narrowly and read only the nearby code needed to understand the controlling path.
- Reuse existing utilities, DocType methods, and analogous tests before adding abstractions.
- For an error, begin with the supplied snippet and immediate diagnostic; inspect one nearby dependency only when necessary.
- Avoid broad repository mapping, repeated reads, unrelated refactors, and architectural redesigns unless requested.
- Do not rewrite unchanged file sections.
- Make the smallest testable patch, then run the narrowest relevant validation before further edits.
- Do not skip tests, validation, security checks, or required domain reasoning to save tokens.

## Project Stack

- Python 3.10+ with Frappe/ERPNext v15.
- Moldova e-Factura integration using Frappe DocTypes, hooks, utilities, scheduled tasks, and API clients.
- Put business rules in the owning DocType or existing utility module. Preserve Frappe permissions and lifecycle behavior.
- Use `FrappeTestCase` for database and DocType behavior; use `unittest.TestCase` for pure parsers and utilities.
- Paper image import uses Gemini; keep identity, line arithmetic, and totals fail-closed on the server. Do not treat model output as trusted.
- Follow `pyproject.toml`, `.pre-commit-config.yaml`, and `.editorconfig`: Ruff, 110-character Python lines, Python 3.10 syntax, double quotes, tabs for source files, and two-space JSON indentation.

## Validation

- ERPNext/Frappe runs in Docker under WSL. `bench` is not on the WSL host PATH.
- Identify the running stack with `docker ps` or `docker compose -f .devcontainer/docker-compose.yml ps` from the `erpnext-dev` repo root.
- Run all `bench` commands and tests inside the Frappe container (`devcontainer-frappe-1` / service `frappe`), via `docker compose … exec frappe` or `docker exec`. Do not treat host `bench: command not found` as a test failure or a reason to skip validation.
- Prefer a focused test for the changed behavior.
- Use site `test.localhost` for all `bench run-tests` and other test-only commands. Do not use `development.localhost` (live development data).
- Run the full app suite when the change crosses shared behavior or when requested:
  `bench --site test.localhost run-tests --app erpnext_moldova_efactura`
- Run repository checks when formatting or multiple file types are affected:
  `pre-commit run --all-files`
- Report failures briefly with the command and relevant diagnostic. Do not hide unrelated pre-existing failures.

## Domain Invariants

- Preserve original documents, files, bytes, hashes, and provenance; derivatives must not replace originals.
- Keep original/source values separate from converted ERP values, including currency, quantity, UOM, and amounts.
- Keep signature verification separate from business review; an uploaded PDF is not automatically trusted or valid.
- Preserve issue, delivery, receipt, and posting dates as distinct values.
- Prevent duplicate imports and accounting documents, including repeated or concurrent requests.
- Sales/Purchase Factura records provide fiscal evidence and coverage; they must not create accounting or stock ledger entries directly.
- Preserve existing SEF/PEF behavior, permissions, migrations, and API semantics.
- Document activity logs must be Info timeline records (not Comment cards). Store English msgid + args via `log_event` (deferred translation); desk JS translates for the viewer's language. Do not `_().format(...)` before writing log text.

## References

- Use `README.md` for project behavior, workflows, roles, and detailed constraints.
- Use `docs/factura-example-analysis.md` for source-document and import requirements.
- Keep this file short; link to existing documentation instead of copying it.

## Scope Boundaries

- Keep changes limited to files relevant to the requested task.
- Add hooks, custom agents, or new dependencies only when technically required by the task.
- If a required change affects an unrelated-looking file, explain why before proceeding.
- Do not commit changes or create branches unless explicitly requested.
