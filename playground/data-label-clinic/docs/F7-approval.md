# F7 — human-in-the-loop approval

**Clinic guides** — [Setup & index](TESTING_GUIDE.md) · [Labels & policy](labels-and-policy.md) · [F6 memory](F6-memory.md) · [F3 server trust](F3-server-trust.md) · [F7 approval](F7-approval.md) · [Namespacing](namespacing.md)

> Commands in this guide run from `playground/data-label-clinic/`, one level
> up from this file.

> Read [Labels & policy](labels-and-policy.md) first. This is a gate on the same
> tool call that Test 2 gates, one step later — and the scenarios below pair
> directly with [BM4](F6-memory.md), which denies the *same tool* by policy.

## What it adds over what already existed

Continuum had an approval workflow before this: `temporal/human_in_loop.py`,
`ApprovalRequest`, anti-spoofing checks. It fired only for a Temporal workflow
step of type `"approval"`. The default `AgentRunner.run()` path — the one every
quick-start uses — had no gate before any tool call.

Neither existing control covers the gap, and the difference is not a matter of
degree:

| control | question | when | sees arguments? |
|---|---|---|---|
| [tool-trust](F3-server-trust.md) | is this server serving the catalogue I vetted? | once, offline, per server | no — catalogue only |
| [policy gate](labels-and-policy.md) | is this run allowed this resource? | every call, by rule | no — `tool:{name}` only |
| **approval** | should *this* call, with *these* arguments, happen? | every declared call | **yes** |

So neither can tell these apart:

```
transfer_funds(amount=5)            # fine
transfer_funds(amount=5_000_000)    # ask someone
```

The policy gate is binary — allow always, or deny always. It cannot express
*"allow, but ask first"*, which is the whole point for an action that is
legitimate but consequential.

## Configuration

```python
AgentConfig(
    tool_approval={"pharmacy__check_interactions"},   # fnmatch, as policy resources
    approval_handler=ask_a_human,                     # async, app-supplied
    approval_timeout=30.0,
)
```

The SDK ships **no handler and no default list of tools**. `delete_*`, `send_*`,
`pay_*` reads as a sensible default and is not one: it blocks a harmless
`send_receipt` while missing `wire_funds`, and a gate nobody configured is
assumed to cover more than it does. Same stance as `tool_data_labels` (no PII
detector) and `pre_store_filter` (no content classifier).

`CLINIC_APPROVAL` picks who answers:

| value | handler |
|---|---|
| `off` (default) | none, and no tool is declared either — the gate is inert. The state a new project starts in |
| `auto` | approves programmatically. For scripted runs that need the approved path without a browser |
| `deny` | refuses programmatically. Shows what the model is told, without waiting for a person |
| `ask` | a real prompt in the web UI, blocking the turn while a reviewer answers |
| `queue` | refuse and resume — the turn ends at once saying *pending*, someone answers out of band, a later turn proceeds |

`CLINIC_APPROVAL_TIMEOUT` (default 30) is how long the gate waits. It is
switchable because the limit **is** the demo — see AP4.

### Why `check_interactions` and not `send_referral_email`

`send_referral_email` was the first choice: it is the egress tool, and "policy
denies it / a person is asked about it" pairs neatly. A live run rejected it,
in two different ways:

- **Ask for a referral email** and the model looks the patient up first. That
  taints the run `phi`, so `phi-no-exfiltration-tools` denies the call *before*
  approval is consulted. All three modes produce identical output.
- **Forbid the lookup** and the model declines to send mail at all. `tools: []`
  in all three modes.

Both were observed before anything was committed. It is the same trap
[BM4](F6-memory.md) documents — *a gate nothing reaches demonstrates nothing* —
which is why `check_interactions` carries the EXTERNAL rule there. It carries
the approval gate here for the same reason, and the pairing survives intact:
**BM4 shows the policy denying `check_interactions` to an EXTERNAL run; AP1–AP4
show a person being asked about it on a clean one.** Same action, two postures,
a rule deciding versus a human deciding.

---

## AP1 — a declared tool asks, and proceeds when approved

1. Start `web.py` with `CLINIC_APPROVAL=auto`.
2. Send **"Check for interactions between metformin and lisinopril."**

```
tools: ['pharmacy__check_interactions']
reply: There are no clinically significant interactions between metformin and
       lisinopril. They are commonly co-prescribed.
```

Identical to `CLINIC_APPROVAL=off`, and that is the point: an approved call is
indistinguishable from an ungated one. The gate is not a speed bump.

- **What it proves:** the gate fires on a declared tool and does not change the
  outcome when the answer is yes. Compare against `off` in the same session —
  if the two differ, the gate is doing something it should not.

## AP2 — a refusal is relayed, not raised

3. Restart with `CLINIC_APPROVAL=deny` and send the same message.

```
gate:  ⏸ TOOL — not approved: APPROVAL DENIED: 'pharmacy__check_interactions'
       was not approved by auto (CLINIC_APPROVAL=deny). Refused by the scripted
       reviewer.
reply: The action to check for interactions between metformin and lisinopril was
       not performed as it requires approval and was denied by the system.
```

- **What it proves:** a refusal becomes a **tool result**, so the model tells the
  user and the turn ends normally. The run does not crash over a decision the
  system made on purpose — the same ergonomics as `POLICY DENIED` in Test 2.
- **Note the ⏸ rather than 🛡️.** The panel separates the two because *a rule
  refused* and *a person refused* are different events, and which one happened
  is the part worth seeing. A refusal that did not appear in the panel would be
  indistinguishable from the tool quietly not running.

## AP3 — the prompt carries the arguments

4. Restart with `CLINIC_APPROVAL=ask` and send the same message. The chat pauses
   and a prompt appears in the transcript:

```
⏸ APPROVAL NEEDED
pharmacy__check_interactions
{ "medications": [ "metformin", "lisinopril" ] }
[approve] [deny]
```

5. Click **approve**. The turn resumes and the tool runs.

- **What it proves:** the handler receives the **arguments**, which is the
  capability this finding adds. A reviewer shown only a tool name is approving
  the name. Neither tool-trust nor the policy gate can see this payload, so
  neither could have told a routine lookup from a consequential one.
- The run's taint is shown alongside, when there is any. *"This run has read the
  public web"* is exactly what a reviewer wants to know before approving an
  outbound action, and it is not visible from the tool name or the arguments.

> **How the prompt reaches the browser, and why it looks like this.**
> `POST /chat` is already in flight and blocked inside the tool executor when the
> handler runs, so the decision cannot come back on that connection. The page
> opens a **second** request: it polls `/approval/pending` while the chat request
> is still open, and posts the answer to `/approval/decide`, which resolves the
> future the handler is parked on (`approval_ui.py`).
>
> That is the shape of every blocking-approval UI over HTTP — and also its
> limit. The chat request stays open the whole time.

## AP4 — nobody answers, and it fails closed

6. Restart with `CLINIC_APPROVAL=ask CLINIC_APPROVAL_TIMEOUT=3`, send the same
   message, and **do not click anything**.

```
gate:  ⏸ TOOL — not approved: APPROVAL DENIED: 'pharmacy__check_interactions'
       was not approved. No reviewer responded within 3.0s (timed out).
reply: The request ... was not completed because it requires approval.
```

Clicking after the timeout reports *"too late — the approval already timed out
and was denied"*, and the prompt clears itself from the panel rather than
sitting there claiming to be live.

- **What it proves:** the gate fails **closed** on every path — a handler that
  raises, one that never answers, one that returns something other than a
  decision, and a tool declared for approval with no handler wired. That last
  one is the configuration most easily mistaken for protection, so it refuses
  rather than quietly proceeding.
- **Why the timeout is short by default.** A blocked run holds the HTTP request
  open while the reviewer thinks, and browsers, proxies and serverless platforms
  give up long before a person does. 30s sits inside ordinary limits. Set
  `CLINIC_APPROVAL_TIMEOUT=3` and walk away to see what a reviewer who takes two
  minutes behind a proxy will actually experience.

## AP5 — refuse and resume, for a reviewer who is not watching

`ask` holds the HTTP request open while someone decides, which only works if
they are looking at the screen. `queue` is the other shape, and the one most
asynchronous deployments actually need.

7. Restart with `CLINIC_APPROVAL=queue` and send **"Check for interactions
   between metformin and lisinopril."** The turn ends immediately:

```
gate:  ⏳ TOOL — awaiting approval: APPROVAL PENDING:
       'pharmacy__check_interactions' has been sent for approval. Queued for a
       reviewer. Ask again once it has been answered.
reply: The request to check interactions between metformin and lisinopril is
       awaiting approval. Please ask again once it has been reviewed.
```

8. Answer it out of band — no turn is waiting on this:

```bash
curl -s localhost:8910/approval/queued
curl -sX POST localhost:8910/approval/answer -H 'Content-Type: application/json' \
     -d '{"key":"<key from above>","approved":true,"reviewer":"tom"}'
```

9. Ask the same question again. It proceeds:

```
tools: ['pharmacy__check_interactions']
reply: There are no clinically significant interactions between metformin and
       lisinopril. They are commonly co-prescribed.
```

- **What it proves:** a third outcome exists. Without it a deferred call and a
  refused one are the same value (`approved=False`), so the model says APPROVAL
  DENIED for something that is merely waiting — and a user told their request
  was *refused* does not go looking for an approver. The panel separates them
  too: **⏳** for pending, **⏸** for declined. One is resumable and the other is
  not, which is the whole reason the state exists.
- **Only a handler may defer.** A timeout or a crash is a refusal, never a
  deferral: nobody answered and nothing is queued, so reporting it as pending
  would promise a resumption that nothing is going to deliver.
- **The resume half is the application's.** The SDK remembers nothing between
  runs — a cached approval could authorise an execution the reviewer never saw —
  so `approval_ui.py` supplies the store and its rules. It keys on
  *(tool, arguments)*, because the second turn is a different run asking the
  same question and there is no request id to carry over. An answer authorises
  **one** execution and is then forgotten; asking a third time queues again. A
  standing permit would be exactly the hazard the SDK declines to build in.

---

## What this layer does and doesn't cover

- **Covers:** the gate firing on a declared tool, an approval proceeding, a
  refusal relayed as a tool result, a real prompt carrying the arguments,
  failing closed on a timeout, and the refuse-and-resume round trip — live, with
  the ungated `off` mode as the control for each.

- **Doesn't cover: a durable, hours-long wait.** This is the honest limit, and
  it is structural rather than missing glue. `HumanInLoopManager` is the
  decision-*submission* side — `approve`, `reject`, `submit_decision` — the API
  a reviewer's UI calls. The waiting lives inside the Temporal workflow, whose
  `_run_approval_step` appends to `_pending_approvals` and blocks on a signal;
  nothing outside the workflow can register a request. And an approval handler
  runs wherever the tool call runs, which under Temporal is inside an
  **activity**, where workflow APIs are unavailable by design.
  Wiring the two together needs workflow-side support that does not exist: a
  signal to register an ad-hoc approval, and a query to read its decision.
  `TestTheTemporalRouteIsNotAvailableYet` asserts both halves of that constraint
  so it is a tested fact rather than a note.

- **Approvals are serialised, not batched.** Tool calls run through
  `asyncio.gather`, so without a lock two handlers fire at once — two prompts
  racing for one terminal. Serialising costs wall-clock when several calls in
  one turn need approval, and it means a reviewer sees one call at a time rather
  than *"this turn wants to do these three things"*. Batching is the better shape
  and a breaking change to the handler signature, so it is named here rather
  than retrofitted later.

- **Approvals are not remembered.** A retried run asks again. Idempotency sounds
  obviously right and is a loaded gun: a cached "approve" authorises a second
  execution the reviewer never saw, and whether a retry is the same action or a
  new one depends on the tool — transferring money and sending a reminder want
  opposite answers. `run_id` and `arguments` are on the request so an app can
  build the policy it needs; the SDK does not pick one, because half-built
  idempotency looks like protection while quietly authorising repeats.

## Where each scenario hits the SDK

| Scenario | SDK code path exercised |
|---|---|
| AP1 | the gate in `ToolExecutor.execute_tool_call` — after the policy check, before argument injection, outside the tool-execution `asyncio.wait_for` |
| AP2 | `ToolApprovalDeniedError` (a `PolicyDeniedError`) rendered by `ToolExecutor._process_tool_results` as `APPROVAL DENIED` |
| AP3 | `ToolApprovalRequest` carrying `arguments` and `data_labels`; `request_approval` serialising on a per-event-loop lock |
| AP4 | `request_approval`'s fail-closed paths — timeout, raise, wrong return type, and no handler wired |
| AP5 | `ToolApprovalDecision(deferred=True)` → `APPROVAL PENDING` rather than `APPROVAL DENIED`; the resume store is the app's (`approval_ui.py`) |
| all | `build_approval_settings` reading `AgentConfig`, passed at both `ToolService` call sites |
