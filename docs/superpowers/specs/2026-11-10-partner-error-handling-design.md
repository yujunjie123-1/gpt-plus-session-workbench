# Partner Error Handling Fix Design

## Scope

Keep the existing GPT-only account-pool flow and the current Session direct-charge product behavior. Do not change Grok handling. Fix three independent contract gaps: non-JSON 429 responses, lookup-batch size validation, and deterministic redemption rejection handling.

## Behavior

1. HTTP 429 is classified before JSON decoding. Read operations remain retryable and honor a bounded `Retry-After`; redemption submission remains single-shot and is followed by original-card lookup.
2. The Python `lookup_batch` method rejects non-array input, empty input, more than 100 codes, and malformed code values locally. `client.mjs` is supplied API reference material and is not modified.
3. A redemption error that is explicitly non-uncertain is not treated as an accepted transaction. Account-pool and Session-direct flows check the original card state without submitting again. A confirmed reusable card becomes a failed, retryable local task and releases the reservation. A used card is never attributed to the rejected submission; pending or unconfirmed states remain protected and are queried by original card. Network errors, malformed responses, 5xx responses, and `uncertain:true` retain the existing pending/no-resubmit behavior.

## Testing

Add synthetic tests for HTML 429 responses, 100/101 lookup-batch boundaries, deterministic 422 redemption rejection in both submission flows, and used-card non-attribution. Preserve existing tests for uncertain submissions, payment-risk quarantine, and queued results.
