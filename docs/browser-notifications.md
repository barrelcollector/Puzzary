# Browser notifications

Puzzary can notify signed-in users when a public puzzle becomes available. The
feature combines a durable in-app notification list with standards-based Web
Push for browsers that support the Push API, Notifications API, and service
workers.

## User setup

Open **Account -> Manage notifications**. New-puzzle notifications are opt-in:

1. Save the desired puzzle-type, theme, tag, and creator filters.
2. Select **Enable notifications** for each browser or device.
3. Accept the browser permission prompt. Puzzary requests permission only from
   that explicit button action.

Each browser is stored as a separate subscription and can be disabled without
affecting the others. Turning off the account preference suppresses all browser
deliveries while preserving device registrations. If permission is denied or
the browser lacks Web Push, matching notifications remain in the account's
in-app list and unread badge.

Notifications contain only the puzzle title, type, theme, and canonical link.
They do not include prompts, clues, answers, solutions, play data, or print
payloads. Draft, future-scheduled, unlisted, archived, password-protected, and
locked sequential-series puzzles are not delivered. Account status,
permissions, filters, and puzzle access are checked again immediately before
each push attempt.

## Publication behavior

Manual, generated, and imported puzzles create one publication event when they
are published. Scheduled puzzles create the event when their publication time
arrives, including after a server restart. A database uniqueness constraint
prevents duplicate notifications for the same user and puzzle; a second
constraint prevents duplicate delivery to one browser subscription.

Publication creates durable delivery work rather than requiring open browser
tabs to poll. The background worker processes pending deliveries every ten
seconds by default. Temporary push-service failures retry up to three times;
HTTP 404 or 410 responses automatically disable expired subscriptions.

## Server configuration and security

Web Push requires a secure browser context: use HTTPS for non-localhost
installations. See [Optional HTTPS and trusted reverse
proxies](https-tls.md). The generated service worker is served at
`/service-worker.js` with root scope.

Puzzary generates a P-256 VAPID key on first startup and stores the private key
in the `app_settings` table so container rebuilds do not invalidate browser
subscriptions. Include the database in backups. Operators may instead provide
`PUZZARY_VAPID_PRIVATE_KEY`; use `\n` escapes when placing a PEM value in an
environment file. Changing the key requires users to subscribe their browsers
again. `PUZZARY_VAPID_SUBJECT` defaults to
`mailto:notifications@puzzary.local` and may be set to an operational contact.

Optional tuning:

```text
PUZZARY_VAPID_PRIVATE_KEY=
PUZZARY_VAPID_SUBJECT=mailto:notifications@example.com
PUZZARY_PUSH_LOOP_SECONDS=10
```

Subscription endpoints and encryption keys are stored because they are needed
for delivery, but are never shown in the UI, notification payloads, or logs.
Only public HTTPS endpoints are accepted, and resolved private, loopback, link
local, and reserved addresses are rejected before delivery.

The implementation follows the [W3C Push API](https://www.w3.org/TR/push-api/)
and [WHATWG Notifications API](https://notifications.spec.whatwg.org/), using
the maintained [pywebpush](https://github.com/web-push-libs/pywebpush) library
for encrypted payload delivery and VAPID authorization.
