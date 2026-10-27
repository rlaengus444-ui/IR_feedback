# IR_feedback — Codex development and delegation rules

## Default model and conditional escalation
- Routine coding is handled by GPT-6.1 Terra Medium (parent agent).
  Examples: isolated UI/graph updates, docs, tests, simple refactors and
  packaging fixes. Ordinary helper agents also use Terra Medium.
- Escalate to the `sol_reviewer` custom agent (GPT-6.1 Sol Max) for:
  1. changes to layer-based IR control law, self-feedback or target profiles;
  2. updates affecting loop stability, delay, gains, saturation, bounds,
     oscillation, overshoot, layer-phase logic or numerical correctness;
  3. timestamp validation, stale/invalid sensor readings, CSV ingestion and
     Sequence write/Save semantics or anything near real device actuation;
  4. cross-module changes between parser, algorithm, session, GUI and
     Windows UI/Excel bridges, or backward-incompatible persistence changes;
  5. release-critical changes, major architecture alterations, or a failure
     left unresolved after two focused attempts.
- For a *new control algorithm* or safety-relevant architecture change, ask
  `sol_reviewer` for design critique BEFORE implementation, and request an
  independent diff-and-test review AFTER implementation. Resolve findings.
- Do not spawn Sol for ordinary requests. In the final report state whether
  `sol_reviewer` was actually invoked. These routing instructions guide
  delegation but do not technically guarantee the agent is spawned.

## Repository-specific requirements
- Read `README.md`, `app/README.ko.md`, `docs/VALIDATION.ko.md` before
  changing control behavior or claims about validation.
- Main source resides under `app/`. Key modules include
  `layer_algorithm.py`, `csv_source.py`, `session.py`,
  `sequence_persistence.py`, `windows_ui_adapter.py` and
  `standalone_ir.py`. Tests are `app/test_*.py`.
- Preserve the invalid thermometer marker `-1`: never treat it as a
  physical measurement. Normal-looking old readings may also be stale.
  CSV alone may not prove freshness. Clearly report this limitation.
- This release has NOT been validated against real S3 sensors, PLCs or lamp
  outputs. Do not enable live automatic IR application or bypass manual
  review / confirmation merely to make an automated test pass.
- Work on offline or mocked fixtures first; no unsolicited live UI automation
  against equipment, no heater setting writes, no dependency installs and no
  distribution or GitHub release without user authorization.
- Check parsing/invalid/missing/stale CSV cases, layer boundaries, stable
  clamped outputs, reproducibility and existing unit tests. Report actual
  test coverage and any hardware tests NOT performed.
- Protect binary release artifacts under `dist/`, `IRFeedback.exe` and
  validation evidence; do not replace binaries or rewrite evidence during
  routine coding.
