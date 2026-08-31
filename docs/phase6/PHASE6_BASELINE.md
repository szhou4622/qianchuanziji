# Phase 6 Baseline — Execution Safety Domain

Phase 6 begins at an **APPROVED Candidate** and builds the safe execution boundary before any platform write is allowed.

## Initial Phase 6 chain

```text
APPROVED Candidate
→ deterministic Execution Task
→ frozen object/parameter evidence
→ durable EXECUTION_PREPARE
→ PENDING Execution
→ durable EXECUTION_PREFLIGHT
→ official GET-only server verification
→ APPROVED or CANCELLED Execution
```

## Hard boundary for the initial Phase 6 implementation

**千川 business POST remains blocked.**

The initial Phase 6 implementation must not:

- call control-task create/status/budget/duration write APIs;
- create `execution_attempt` rows;
- infer a successful action from local state;
- cancel an Execution merely because a network/Token/permission/response-contract read failed;
- blindly retry a possible platform write.

## Preflight safety rules

- server facts outrank stale local facts;
- plan must be verified and currently `DELIVERY_OK`;
- CREATE_RETARGET materials must still be returned by a complete official read and still be `DELIVERY_OK`;
- PAUSE/UPDATE control task must still be returned and `PROCESSING`;
- an active tool-created PROCESSING retarget task remains a duplicate-retarget guard;
- deterministic stale business facts cancel only the affected Execution;
- network/Token/permission/contract failures leave the Execution `PENDING` for bounded durable retry;
- user-disabled account/plan or candidate no longer approved cancels the affected Execution.

## Known blocker before UPDATE_BUDGET / UPDATE_DURATION POST can ever be enabled

Phase 5 froze the candidate metric snapshot and generic `before_state_json`, but did not yet persist a dedicated candidate-time control-task budget/duration server baseline.

Therefore initial Phase 6 may verify task identity/status and record the fresh server budget/duration, but it must mark:

```text
external_change_baseline_complete = false
post_blocker = CANDIDATE_FREEZE_MISSING_CONTROL_BUDGET_DURATION_BASELINE
```

until that freeze contract is hardened.

## Future Phase 6 write gate

Only after PREPARE/PREFLIGHT are stable and tested may a later Phase 6 step introduce:

```text
write-ahead execution_attempt
→ one platform POST
→ SUBMITTED / UNKNOWN
→ GET reconciliation
→ proof-based compensation at most once
```

Total platform sends for one logical action must never exceed two, and an unknown outcome must never be blindly resent.
