# Optional HTTPS and trusted reverse proxies

Puzzary keeps plain HTTP as the default for local and trusted-network installations. Public deployments should use HTTPS, either at a reverse proxy or directly in Puzzary. Transport settings are environment-owned and take effect after the application restarts.

The implementation follows Uvicorn's [HTTPS and forwarded-header settings](https://www.uvicorn.org/settings/) and Starlette's [session and HTTPS redirect middleware contracts](https://www.starlette.io/middleware/).

## Configuration reference

| Variable | Default | Purpose |
| --- | --- | --- |
| `APP_BASE_URL` | `http://localhost:8790` | Canonical external origin used in links, QR codes, invitations, exports, and API responses. Existing installations may also have an Admin setting that overrides link generation; keep it aligned with this value. |
| `PUZZARY_TLS_CERT_FILE` | empty | PEM certificate chain for native HTTPS. Must be paired with the key file. |
| `PUZZARY_TLS_KEY_FILE` | empty | Matching PEM private key for native HTTPS. |
| `PUZZARY_TLS_KEY_PASSWORD` | empty | Optional encrypted-key password. Keep it out of version control and logs. |
| `PUZZARY_TRUSTED_PROXIES` | empty | Comma-separated proxy IP addresses or CIDRs allowed to supply forwarded client/scheme headers. |
| `PUZZARY_HTTPS_REDIRECT` | `false` | Redirect requests whose trusted effective scheme is HTTP to HTTPS. Requires an HTTPS base URL and either native TLS or trusted proxies. |
| `PUZZARY_SECURE_COOKIES` | `auto` | `auto`, `true`, or `false`. Auto enables the Secure flag when the environment base URL is HTTPS or native TLS is configured. |
| `PUZZARY_HOST` | `0.0.0.0` | Application listener address. |
| `PUZZARY_PORT` | `8790` | Application listener and Compose-published port. |

The container now starts through `python -m app.server`, which validates the transport configuration before Uvicorn imports the application. A missing certificate/key half, unreadable or mismatched PEM files, invalid proxy range, invalid port, unsafe HTTPS cookie override, or incompatible redirect setting stops startup with an actionable error instead of falling back to HTTP.

The `*` trusted-proxy value is supported by Uvicorn but intentionally produces an administrator warning. Prefer the narrow address or container-network CIDR of the actual proxy. Forwarded headers from every other client are ignored.

## Existing local HTTP deployment

No TLS variables are required:

```env
APP_BASE_URL=http://localhost:8790
PUZZARY_TLS_CERT_FILE=
PUZZARY_TLS_KEY_FILE=
PUZZARY_TRUSTED_PROXIES=
PUZZARY_HTTPS_REDIRECT=false
PUZZARY_SECURE_COOKIES=auto
```

This preserves the existing `http://localhost:8790` behavior. The Admin settings page warns that passwords, API tokens, guest links, and session cookies must not cross an untrusted network over HTTP.

## Recommended reverse-proxy HTTPS

Terminate TLS at Caddy, Nginx, Traefik, Cloudflare Tunnel, or another maintained proxy. Keep Puzzary's backend listener on HTTP, set the canonical public origin to HTTPS, and trust only the proxy source range:

```env
APP_BASE_URL=https://puzzary.example.test
PUZZARY_TRUSTED_PROXIES=198.51.100.0/24
PUZZARY_HTTPS_REDIRECT=true
PUZZARY_SECURE_COOKIES=auto
```

`198.51.100.0/24` is a documentation-only placeholder. Replace it with the narrow source address or CIDR of your proxy or container network.

For an existing installation, first change **Admin > Settings > Public base URL** to the same HTTPS origin, then update the environment and restart. This avoids generating old HTTP links after the transport change. If the proxy already performs HTTP-to-HTTPS redirects, `PUZZARY_HTTPS_REDIRECT=false` is also valid.

Minimal Caddy configuration when Caddy can reach the Puzzary service by name:

```caddyfile
puzzary.example.test {
    reverse_proxy puzzary:8790
}
```

Minimal Nginx location configuration:

```nginx
location / {
    proxy_pass http://puzzary:8790;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
}
```

The proxy must pass WebSocket upgrades for Group Solve. Browser clients derive `wss://` from the HTTPS page origin; Uvicorn accepts forwarded scheme/client data only from `PUZZARY_TRUSTED_PROXIES`. Test both normal login and a two-browser Group Solve connection through the public hostname.

For Cloudflare Tunnel, point the tunnel service at the Puzzary HTTP listener and use the tunnel connector's exact source address or container-network CIDR if forwarded headers are needed. Do not disable TLS verification when the tunnel connects to an HTTPS origin unless that risk is explicitly accepted and temporary; using Puzzary's HTTP listener on a private Docker network avoids a second certificate boundary.

## Native HTTPS

Mount a certificate chain and matching private key read-only into the application container. A Compose override can add the mount without changing the HTTP-compatible base file:

```yaml
services:
  app:
    volumes:
      - ./certs:/run/puzzary-tls:ro
```

Configure the matching paths and HTTPS origin:

```env
APP_BASE_URL=https://puzzary.example.test:8790
PUZZARY_TLS_CERT_FILE=/run/puzzary-tls/fullchain.pem
PUZZARY_TLS_KEY_FILE=/run/puzzary-tls/privkey.pem
PUZZARY_TLS_KEY_PASSWORD=
PUZZARY_SECURE_COOKIES=auto
```

Then recreate the application container and verify `https://puzzary.example.test:8790/health`. Native TLS serves HTTPS on the configured application port; it does not also open a plaintext port. Use a reverse proxy or separate listener when port 80 redirects are required.

Uvicorn loads the certificate at process startup. After certificate renewal, restart only the Puzzary application container so it loads the renewed files. Preserve the database and upload mounts, and do not restart PostgreSQL merely to reload a certificate.

## Verification checklist

1. Confirm the configured public base URL and Admin setting both use the expected HTTPS origin.
2. Request `/health` through the external HTTPS hostname.
3. Sign in and confirm the session cookie has `Secure`, `HttpOnly`, and `SameSite=Lax` attributes.
4. Confirm generated puzzle, issue, guest-link, setup, print, and API URLs use `https://`.
5. Open Group Solve in two browsers and confirm its presence socket connects with `wss://`.
6. Send a forged `X-Forwarded-Proto: https` header directly to the backend from an untrusted address and confirm it does not bypass redirect/scheme handling.
7. Review startup/application logs for transport warnings or redirect loops.

`scripts/verify_transport_security.py` covers HTTP compatibility, configuration failures, redirect behavior, trusted/untrusted forwarded headers, secure cookies, HTTPS URL generation, and WebSocket URL selection in an isolated environment. It does not validate a production certificate chain, proxy ACL, firewall, DNS, or physical network path.
