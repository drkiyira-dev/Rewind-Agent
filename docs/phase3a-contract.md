# Phase 3A contract

Phase 3A is one serial tool loop for the OpenAI Responses API. The research conclusion remains `engineering_packaging`. This is not a new agent framework and not a new recovery planner.

## Checked protocol subset

Tested SDK: `openai` 3.46.0, with `httpx2` 2.16.0, on macOS arm64 and Python 3.14.3. The client is constructed with `max_retries=0`, an explicit timeout, `stream=False`, `parallel_tool_calls=False`, `store=False`, and `tool_choice="auto"`. The HTTP client disables environment proxies and does not follow redirects.

`store=False` means this adapter keeps continuation locally and does not send `previous_response_id`. It is not a statement that a provider stores nothing.

Supported response items are `message`, `function_call`, and opaque `reasoning` items. Reasoning fields are stored and sent back without being parsed. One `function_call` is accepted. `function_call_output` carries the same `call_id`. A second function call, a hosted tool, a custom tool, `incomplete`, `failed`, or a refusal stops the turn. None of those execute a tool or publish success.

The model name, tool schemas, and endpoint come from the caller. Local mode accepts only `http://127.0.0.1` or `http://localhost` and uses the fixed non-secret credential `rewind-local-test-credential`. It does not read `OPENAI_API_KEY`. Live mode returns `live_not_authorized` and does not open a provider connection.

## Recovery rules

The accepted response, call id, arguments, and child run id are committed before the tool runs. Resume uses that record. It does not ask the model for a replacement call. A response that was received but not committed stays `model_request_unknown`. An explicit retry is a new attempt, keeps the old attempt, and can repeat provider work. It is not the default.

Pure tools and the one append effect go through the existing durable kernel. A separate agent coordinator lock (`.agent.lock`) remains held throughout the operation. Only the shared durable executor lock (`.lock`) is released while the child kernel runs, then reacquired before adopting its result. A second agent cannot enter during that handoff. Direct durable writers still use the executor lock. The child run id is chosen before the tool runs and is reused after a restart.

This example authorizes one effect slot, `primary_record`, chosen when the session is created. The same slot and the same summary adopt the existing effect. A different summary conflicts. Another slot is not created. Older workflows that allow two logical operations with the same payload are unchanged.

`unknown` does not publish, does not request the next model turn, and does not let the model change the slot or the budget. `reconcile` only adopts an existing effect. `status` and `inspect` do not request a model, post an effect, or write the database. `inspect` hides reasoning ciphertext.

A final sentence does not publish the task. Publication requires the stored summary, the confirmed append receipt, and the saved final response. A published resume returns the same publication id.

Budgets for model attempts, accepted turns, and tool calls persist across restarts. A changed model, tool registry, snapshot, or budget is refused. A new run is not used to recover an uncertain effect.

## What this phase does not claim

Local fixture passage is not live model validation. `actual_provider_api_calls` stays 0 for the local fixture, while SDK requests to loopback are counted separately. Fixture usage fields are not token counts. The loop is not exactly-once for model billing, not a sandbox, and not safe automatic recovery for arbitrary third-party APIs.

## 2026-11-15 maintenance

- Existing child results are validated against the expected workflow, input and trusted tool identity, including already published children and reconcile. A coincidentally matching child run ID is insufficient evidence.
- A new response cannot reuse an accepted `call_id`. Recovery reuses the saved call without another response. Different call IDs may adopt the same authorized effect slot.
- Pure tools in this bounded loop use an empty argument object and the imported snapshot. Nonempty pure argument schemas are refused rather than silently ignored.
- Session SQLite paths are URI-encoded. Connection and fixture-server initialization failures close their resources. Schema columns and stored snapshot/registry/provider/publication digests are checked before use. These checks detect inconsistent records; they are not authentication against deliberate database rewriting.
- Completed response envelopes do not authorize incomplete calls, malformed messages, empty call IDs or unsupported message content. Invalid endpoints, URL credentials and nonfinite timeouts are refused before creating the SDK client.
- `include=["reasoning.encrypted_content"]` accompanies `store=False`; opaque reasoning is retained for continuation and redacted in inspect. SDK construction failures close the HTTP client. Proxies and redirects remain disabled, and live mode remains a refusal.
- CLI budget flags apply only to `start`. Resume retains stored budgets and refuses overrides. All supported budgets up to 20 turns are covered by the transition bound.

The local protocol subset was checked against installed OpenAI SDK 3.46.0 and httpx2 2.16.0, and the official [Responses migration guidance](https://developers.openai.com/api/docs/guides/migrate-to-responses#additional-differences). The SDK's installed Responses parameter documentation describes encrypted reasoning continuation. These references establish protocol behavior, not experimental acceptance results.

## Additional recovery boundaries

The follow-up adds 12 regression tests. All provider traffic uses the local synthetic protocol. The transaction worker is test-only; no new crash hooks or schema changes are added to the runtime.

| Window or fault | Checked outcome |
| --- | --- |
| Request intent before COMMIT | SIGKILL rolls back the attempt; resume may make the first request. |
| Intent committed, dispatch not reached | Still unknown; only explicit retry starts another recorded attempt. |
| Call acceptance before COMMIT | Response, call and child binding roll back together; no tool executes. Explicit retry retains the uncertain attempt. |
| Effect-output adoption before COMMIT, including reconcile | Repeated SIGKILL leaves the existing effect intact; recovery adopts one output, with one business POST in total. |
| Final response before COMMIT | Reconcile does not erase model uncertainty; resume does not request again without explicit retry. |
| Publication before COMMIT | Saved final response survives; reconcile does not publish it, and resume publishes once without another request. |
| Hot rollback journal after dirty-page spill | Read-only status/inspect return `unknown / store_recovery_required` without modifying database or journal. Locked resume rolls back and completes without another effect. |
| Executor occupied after child completion or reconcile | Return busy before adoption; after release, the same child is adopted once. A separate file-lock descriptor exercises this contention; the existing tests also cover a competing agent process. |
| Damaged saved arguments, unknown tool or changed child binding | Resume/reconcile reject the checked inconsistency before child execution; original database bytes remain unchanged. |
| Retry budget exhausted with uncertain attempts | No additional SDK call or child is created. Both uncertain attempt records remain; refusal does not claim that either request never happened. |

The hot-journal test uses a small SQLite cache and an uncommitted synthetic padding row to force dirty-page spill; it verifies that the padding also disappears during rollback. Other transaction tests kill immediately before COMMIT, and the existing tests kill after committed boundaries. This is process-crash evidence on the verified local filesystem, not a power-loss, fsync-failure, network-filesystem or arbitrary-disk-corruption guarantee. Saved-record checks are bounded consistency checks, not database authentication or a repair facility. Timeouts, uncertain provider billing and live model behavior remain subject to the existing local-only scope; no live behavior is inferred from fixture success.
