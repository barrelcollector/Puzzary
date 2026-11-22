# Public account registration

Puzzary uses administrator-created local accounts by default. An administrator
can optionally allow people to create their own local accounts under **Admin >
Settings > Public Account Registration**. Registration remains off until it is
explicitly enabled.

## Before enabling registration

Configure the public base URL first. It must use HTTPS so passwords are not
exposed in transit; plain HTTP is accepted only for localhost testing. Email
verification is optional and off by default. If it is enabled, also supply an
SMTP host, port, security mode, From address, and credentials if the mail server
requires them. The saved SMTP password is encrypted and is never displayed
again.

Choose a default role from the list shown in the web interface. Puzzary only
offers roles whose permissions are no more powerful than the built-in Player
role, preventing public signups from receiving administrative access. Choose a
verification-link lifetime between one hour and seven days, then enable the
feature and save the settings.

When enabled, the login page shows **Create account**. A new user supplies a
display name, email address, and strong password. When email verification is
required, the account remains inactive until its one-time email link is opened;
otherwise it can sign in immediately. Display names and email addresses are
compared using normalized, case-insensitive forms so visually equivalent
registrations cannot create duplicate identities.

## Security behavior

- Signup and resend requests always return generic success messages where an
  account's existence could otherwise be revealed.
- When verification is required, links are stored as hashes, expire at the
  configured time, and become unusable after verification or resend.
- Signup and resend requests are limited by both network address and normalized
  email identity. Resends also have a minimum delay.
- Unverified self-registrations older than seven days are anonymized, releasing
  their display name and email while retaining an auditable record.
- Registration, email delivery, verification, and configuration changes are
  written to the audit history. One-time tokens are removed from access logs.
- Turning registration off immediately removes the signup link and rejects new
  web and API requests. Existing verified accounts continue to work.

Public registration creates local accounts only. It does not create or link an
OpenID Connect identity, and an OIDC provider is never matched to a local user
by email automatically. Users link an external identity only through Puzzary's
explicit account-linking workflow. Public local registration is unavailable in
single-sign-on-only mode.

## API support

The web interface is the primary configuration and signup experience. Clients
can support the same public flow with:

- `POST /api/register` using `display_name`, `email`, and `password`.
- `POST /api/register/resend` using `email` when verification is required.
- `GET /register/verify?token=...` from the emailed one-time link.

The two POST endpoints return `202 Accepted` with generic messages when the
request can be processed. They return `403` when public registration is not
available, `422` for invalid input, and `429` when a request limit is reached.
