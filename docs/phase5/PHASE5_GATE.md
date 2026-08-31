# Phase 5 Gate — Candidate Freeze & Feishu Confirmation

Status: **CODE_GATE = PASS**

Baseline commit:

```text
bff79e09807ba66e3a2bbac5d8a63b237ec1d1d6
```

GitHub Actions:

```text
run #150
run_id: 33398738945
Ubuntu  + Python 3.11  PASS
Ubuntu  + Python 3.12  PASS
Windows + Python 3.11  PASS
Windows + Python 3.12  PASS
```

Linux 3.11 test summary at the gate baseline:

```text
138 passed, 3 skipped, 0 failed
```

## Gate facts

Phase 5 code gate confirms:

- only unsuppressed valid HIT can enter Candidate;
- Candidate freezes strategy version, metric snapshot, execution parameters and object set;
- merged retarget grouping is capped at 20 materials per group;
- Candidate creation is deterministic/idempotent;
- active tool-created PROCESSING retarget task blocks duplicate material retarget;
- MANUAL candidate enters `WAITING_CONFIRMATION` and expires after 30 minutes;
- AUTO candidate is locally `APPROVED` without a confirmation card;
- rejection cooldown is scoped to the same strategy/object;
- Feishu Outbox/Inbox are persistent and idempotent;
- confirmation card uses frozen Candidate content and does not reselect objects at click time;
- expired cards cannot approve a Candidate;
- real Feishu transport adapter uses WebSocket/CardAction through `lark-channel-sdk==1.0.0`;
- each Outbox send uses stable `uuid=outbox_id` and SDK internal retry is kept at one attempt; local Outbox owns retry cadence;
- Feishu network lifecycle is gated by commercial license state and failure is isolated from 千川 collection/runtime;
- application runtime integration was tested with a fake channel transport;
- **no 千川 business POST is enabled and no `execution_attempt` is created in Phase 5**.

## Release-environment item not claimed by this gate

This automated code gate does **not** claim a live production Feishu credential test was performed.

Before final commercial release, a manual/live E2E must still verify with an actual Feishu App ID/App Secret and target chat:

```text
Candidate
→ real Feishu card send
→ real WebSocket CardAction
→ Inbox persistence
→ Candidate APPROVED / REJECTED
```

That release-environment validation is separate from the Phase 5 automated code gate.
