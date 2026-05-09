# AGENTS.md

This file defines the working agreement for agents operating in this repository.

## Scope

- Applies to the entire repository rooted at this directory.
- If a deeper subdirectory adds its own `AGENTS.md`, the deeper file overrides this one for files under that subtree.

## Working Style

- Follow TDD by default: write or update tests first, then implement, then run the relevant verification.
- Keep changes focused. Do not mix unrelated refactors into the same task.
- Do not revert user changes unless explicitly requested.
- Prefer small, reviewable patches over broad rewrites.

## Environment

- Use the Conda environment `scip_env` for Python commands.
- Prefer `conda run --no-capture-output -n scip_env <command>` over interactive activation.
- Current validated interpreter: `Python 3.13.5`.

## Test Policy

- For any behavior change or bug fix, add or adjust a failing test first when practical.
- After implementation, run the smallest relevant test set first.
- Before finishing, run a broader regression pass for the affected area when the project supports it.
- If tests cannot be run, state the reason clearly in the final report.

## Test Objectives

- Verify correctness of core scheduling, allocation, simulation, and API behavior.
- Add regression coverage for every confirmed bug fix when practical.
- Protect the stability of script entry points and API contracts.
- Prioritize critical paths over broad but shallow coverage.

## Test Timing

- Before implementation: add or update a test that captures the expected behavior or bug.
- After each small code change: run the smallest relevant test set for fast feedback.
- After completing the feature or fix: run regression tests for the affected module or flow.
- Before finishing the task: run a smoke test for the relevant entry point or API path.

## API Request Payloads

- Keep stable example request payloads for tests and debugging.
- Cover valid, boundary, and invalid payloads for important endpoints.
- When request schema changes, update tests and API documentation in the same change.
- Validate required fields, field types, defaults, and handling of missing or malformed input.
- Maintain one minimal payload and one full payload for each critical endpoint.

## API Response Payloads

- Verify response payloads, not only HTTP status codes.
- Validate response schema, required fields, field types, and key business values.
- Keep stable example success responses for important endpoints.
- Verify error code, error message, and useful diagnostic context for failure cases.
- When response schema changes, update tests and API documentation in the same change.

## OK/NG Rules

- Define clear pass (`OK`) and fail (`NG`) expectations for each important test case.
- `OK` cases should confirm the request is accepted and the response data matches business expectations.
- `NG` cases should confirm invalid input is rejected with the expected error structure and message.
- Do not treat a test as complete if it checks only status code without checking payload content.
- For API changes, keep at least one `OK` sample and one `NG` sample as regression baselines.

## Python Conventions

- Prefer `pytest` for test execution when tests are pytest-compatible.
- Keep new code simple and explicit. Avoid speculative abstractions.
- Match the existing project structure and naming unless there is a strong reason to change it.

## Repository Hints

- Main entry scripts include `run.py`, `run_daily.py`, and `run_api.py`.
- API-related behavior may also be covered by `test_api_flow.py`.
- Key directories include `simulation/`, `allocation/`, `schedule/`, `api/`, and `config/`.

## Final Reporting

- Summarize what changed.
- List verification actually performed.
- Call out any remaining risks, assumptions, or skipped checks.
