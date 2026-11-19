# Optional OpenID Connect sign-in

Puzzary's built-in accounts remain the default account system. OpenID Connect (OIDC) is an optional integration for deployments that already operate an identity provider.

## Safety model

- New installations use **Puzzary accounts only** until an administrator deliberately changes the login mode.
- An OIDC provider must complete a signed test login before it can be enabled.
- The local `admin` account and the administrator recovery login remain available in every mode.
- The recommended provisioning mode is **explicit linking**. A user signs in with their existing Puzzary account, opens **Account**, and links the external identity.
- Optional just-in-time provisioning creates only a normal **Player** account. External claims never grant Puzzary administrator permissions.
- Accounts are mapped by the validated OIDC `iss` and `sub` pair. Puzzary never merges accounts only because email addresses match.
- Client secrets are encrypted at rest using the server's `SESSION_SECRET` and are never returned by the web UI or API. Back up `SESSION_SECRET` with the application configuration; changing it requires re-entering OIDC client secrets.

## Configure a provider

1. Set Puzzary's public base URL to its HTTPS origin under **Admin > Settings**.
2. Copy the exact callback URL shown under **OpenID Connect Providers**.
3. In the identity provider, create a confidential OIDC client using authorization code flow and PKCE S256. Register only the displayed callback URL.
4. In Puzzary, add the issuer URL, client ID, client secret, and `openid profile email` scopes.
5. Save the provider, select **Test Provider**, complete the identity-provider flow, then enable it.
6. Choose either **Puzzary accounts and single sign-on** or **Single sign-on for users; local admin recovery**.

Provider discovery, authorization, token, user-info, and JWKS endpoints must use HTTPS. HTTP is accepted only for localhost test providers.

## Authentik example

- Application slug: `puzzary-test`
- Provider type: OAuth2/OpenID Connect
- Client type: confidential
- Grant types: authorization code and refresh token
- Redirect URI: the exact callback URL shown by Puzzary
- Scopes: `openid profile email`
- Issuer example: `https://auth.example.com/application/o/puzzary-test/`

## Login modes

- `local`: built-in Puzzary accounts only; this is the default.
- `mixed`: built-in accounts and enabled OIDC providers appear together.
- `oidc`: regular users use OIDC. The local form is hidden behind **Administrator recovery login**, and the server rejects local login for non-administrators.

Logging out ends the Puzzary session. It does not end the identity provider's browser session. Disabling a Puzzary account invalidates its Puzzary sessions; disabling an external identity prevents new OIDC logins but does not retroactively end an existing Puzzary session, so administrators should also revoke that user's Puzzary sessions.

## API

Administrators with `settings.manage` can use:

- `GET /api/admin/oidc/providers`
- `POST /api/admin/oidc/providers`
- `PATCH /api/admin/oidc/providers/{id}`
- `DELETE /api/admin/oidc/providers/{id}`
- `POST /api/admin/oidc/providers/{id}/test`
- `PUT /api/admin/oidc/settings`

Provider responses are redacted and never include client secrets. The test endpoint returns an authorization URL that must be completed interactively in a browser.

## Recovery

Open `/login?local=1` to reach the local administrator form. Keep at least one active local administrator with a tested password. In single-sign-on-only mode, regular users must retain at least one linked identity; in mixed mode, a local password or another linked identity is sufficient.
