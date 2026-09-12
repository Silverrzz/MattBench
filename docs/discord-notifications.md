# Discord notifications

Notifications are disabled by default, both platform-wide and per account. No historical lifecycle events are replayed. Existing event API kinds and fields remain available; `data.subject_owner_id` identifies the recipient and `data.actor_id` identifies an acting user, independently of the legacy `owner` field.

## Setup

1. Apply migrations and collect static files using the deployment environment.
2. Run `python manage.py discord_notifications` as a separate supervised service. `Deploy/openbench-discord.service` is a systemd example; adjust paths, account, database service name and environment file for the installation. It does not require the training coordinator to run. `--once` processes at most one eligible delivery for diagnostics.
3. Use the **same existing** `MATTBENCH_CREDENTIAL_KEY_FILE` as the web server. The webhook is encrypted with that credential key. Back up the key separately from the database. Do not generate a replacement for an installation that already has credentials.
4. An active superuser opens **Manage → Notifications**, supplies the canonical HTTPS Mattbench origin (for example `https://devbench.nocturn9x.space`), destination label and webhook URL, and saves. Create the webhook in the desired ordinary Discord text channel. Only canonical `https://discord.com/api/webhooks/ID/TOKEN` URLs (or the v10 equivalent) are accepted. Queries, fragments, alternate hosts and redirects are rejected. Forum/thread routing is unsupported.
5. Queue the labelled, mention-free test message while notifications remain disabled. The separate sender delivers it. Verify the channel and sender status, then enable platform delivery.
6. Users opt in under **Profile → Discord notifications**, select event modes, and optionally supply their numeric Discord user ID. Selected summaries are visible to other members of the shared channel. User ID ownership is not verified; Discord account/channel settings determine push delivery.

Configuration validation reads Discord metadata; web requests never deliver messages themselves. The displayed server/channel IDs come from Discord, while the destination label is administrator-supplied. Webhook metadata does not expose the destination channel's type, so the test message is the final check that the selected text channel accepts delivery. No bot or OAuth application is needed. See the [Discord webhook reference](https://docs.discord.com/developers/resources/webhook).

## Event semantics

- Tests: created, approved, first assignment, passed/failed using the existing SPRT or fixed-game rules, execution error, stopped, restarted, deleted and restored. Natural completion produces one outcome. SPSA and datagen use completed instead of passed/failed. Existing tests with prior assignments do not receive a historical start alert.
- Execution errors cover failed builds and bench mismatches and state whether work remains active. They consolidate per execution and reset after restart. Individual game errors remain in the existing error log.
- Training: created, initially queued, started once per run, completed through either the legacy or final-chunk path, failed, cancellation requested, cancelled, interrupted, recovered, continued into a separate run from a checkpoint, deleted and restored. Routine chunk completion/requeue and checkpoint saves are silent. Recovery episodes are keyed by the expired claim, and recovery is reported when a replacement allocation is claimed. Legacy recovery creates a separate run.
- Dataset uploads: published, or failed when no automatic retries remain. Routine retries are silent.
- Workers: registered, explicitly disconnected, pause requested and resumed. Registering a training capability on an existing machine does not send another registration alert. Pause requests let current work finish; resumed means eligible for assignments.

Successful outcomes, failures, execution errors and training interruptions default to Message; other events default to Off. Admin actions target the subject owner and identify the actor. Test authors that cannot be resolved to accounts are skipped with an internal diagnostic containing the test ID.

## Delivery and operations

State transitions, lifecycle events and eligible outbox rows commit together. Summaries contain an allowlist of names, counts and metrics plus links to existing access-controlled Mattbench pages. Raw logs, detailed errors, dataset paths and credentials are excluded. User text is escaped and automatic mention parsing is disabled; only the selected numeric user ID can be mentioned. See [Discord allowed mentions](https://docs.discord.com/developers/resources/message#allowed-mentions-object).

The sender claims rows with database leases and serializes the shared webhook, preserving order per subject while allowing unrelated subjects to proceed during a retry. Production uses PostgreSQL. Leases expire after two minutes so a supervised restart can reclaim unfinished work. Manage shows the sender heartbeat, pending count, last success, and sanitized delivery failures.

Every delivery rechecks configuration generation and account/event settings. Disabling an account, event or platform skips queued deliveries permanently. Removing a ping selection suppresses its mention. Replacing/removing the webhook skips deliveries for the old configuration; re-enabling never replays them. A message already in flight may finish before a setting change takes effect.

Discord 429 `retry_after`/`Retry-After` and exhausted-bucket reset headers delay the shared sender. Network errors, missing confirmations and 5xx responses use bounded exponential backoff, with at most eight attempts and a 24-hour lifetime. Other HTTP errors are permanent and visible in Manage. See [Discord rate limits](https://docs.discord.com/developers/topics/rate-limits).

Requests use `wait=true` to obtain a message ID. Database deduplication prevents repeated internal processing from creating additional outbox rows, but **exactly-once external delivery is not guaranteed**: a connection timeout or process crash after Discord accepts a message and before its ID is saved can cause a duplicate on retry.

Stop the Discord sender alongside web/training services before restoring a database backup, and skip old pending deliveries after a restore if they may already have been sent. Preserve the credential key with the existing backup procedure. This feature adds no worker protocol requirements.

## Verification

`python manage.py test OpenBench.tests` covers event routing, controls, both training completion paths, recovery, cancellation, upload exhaustion, rollback, preferences, masked secrets, mentions, retries, rate limits and sender restart. PostgreSQL enables the concurrency tests. The separate GPU integration fixture is opt-in and is not required for notification changes. Discord HTTP responses are mocked; the administrator test-message action is the final live integration check.
