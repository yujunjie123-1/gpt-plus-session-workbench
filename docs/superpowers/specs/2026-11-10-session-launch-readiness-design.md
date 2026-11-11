# Session Launch Readiness Design

## Scope

Make account-pool Session acquisition visibly progress from isolated Edge preparation to helper connection. A task must not remain in `login` for the full ten-minute Session wait when its InPrivate window never connects to the workbench helper.

## Behavior

1. Keep the existing `session_only` request and automatic login behavior. Show a distinct account status after the InPrivate window has started and while its helper is connecting.
2. Bound both startup phases: isolated Edge preparation must return a window handle within 60 seconds, and the task's InPrivate helper must bind its one-time instance credentials within 60 seconds after launch.
3. If either phase exceeds its deadline, fail the login task with an actionable local message. Cancel pending preparation where possible, revoke the request, and close only that task's Edge process/profile through the existing cleanup path. Do not wait for the ten-minute Session handoff timeout.
4. A helper binding or login failure that arrives before the readiness deadline must be handled immediately. Once the helper binds, preserve the existing ten-minute login and Session handoff timeout, identity checks, and cleanup behavior.
5. Keep all diagnostics free of account credentials, Session values, bootstrap nonces, and request tokens.

## Testing

Use synthetic Edge handles and local workbench fixtures to verify the bound and unbound paths, status progression, prompt failure, per-task cleanup, and rejection of late handoffs. Run focused Python tests and the frontend production build. Do not open a real account login or change a running workbench process.
