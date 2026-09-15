---
phase: 04-repository-minimization
type: execute
status: complete
started: "2026-08-11"
completed: "2026-08-11"
requirements:
  - SEC-04
  - MAINT-01
  - MAINT-02
  - MAINT-03
  - MAINT-04
must_haves:
  truths:
    - "Every retained implementation file belongs to the worker MVP"
    - "The worker can restart without any removed legacy module"
    - "Only one local Compose topology remains"
    - "The repository skill still executes the production filter"
    - "All Harness Engineering evidence remains available"
---

# Objective

Minimize the repository around the deployed Kipu MVP while preserving runtime
continuity, reproducible verification and Harness Engineering traceability.

# Tasks

1. Inventory every source, test, configuration, documentation and generated file.
2. Trace the worker's runtime imports and operational dependencies.
3. Extract the useful Kipu contract and filter policy from legacy modules.
4. Remove unused HTTP/evidence code, packages, tests and examples.
5. Align Docker, Compose, environment, README, operational docs and skill.
6. Preserve the live worker process, `.venv`, AWS infrastructure and all prior
   `.planning` artifacts.
7. Run the reduced verification suite and record the evidence.
