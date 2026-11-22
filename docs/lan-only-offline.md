# LAN-only Puzzary with offline travel play

Puzzary does not have to be reachable from the public internet to support its
installable web app and offline puzzles. You can keep the server private on a
home network, open its trusted HTTPS address while at home, download eligible
puzzles, and keep playing after leaving. When the device returns to the home
network, Puzzary can synchronize queued progress under the normal conflict and
access rules.

Port forwarding, a public inbound firewall rule, and a cloud-hosted Puzzary
server are not required for this workflow.

## What each layer provides

- **LAN access** lets a browser reach the server while connected to the home
  network. Plain HTTP may display the ordinary site, but it is not enough for
  reliable PWA installation or service-worker storage on another device. The
  browser exception for `http://localhost` applies only on the same device as
  the browser, not to a home server reached across the LAN.
- **Trusted HTTPS** gives the remote phone, tablet, or computer the secure
  browser context required by service workers. A certificate warning means the
  context is not trusted and must be fixed rather than bypassed.
- **PWA installation** makes Puzzary launch like an installed application, but
  does not by itself download private puzzle data.
- **Available offline** downloads an eligible puzzle and the module assets and
  validation data needed to solve it without the server.
- **Synchronization** happens after the device can reach its home server again.
  Content that was never downloaded remains unavailable while away.

Installation controls and background behavior vary by browser and operating
system. Browser storage can also be evicted, so downloaded puzzles are a travel
copy rather than a backup.

## Example: Caddy with a private hostname

The following pattern keeps Puzzary private while placing Caddy in front of its
HTTP listener:

1. Choose a private hostname, such as `puzzary.home.arpa`.
2. Configure the home DNS resolver so that hostname resolves to the LAN address
   of the machine running Caddy.
3. Let Caddy reach the Puzzary Compose service over the private Docker network.
4. Issue a certificate from Caddy's internal certificate authority.

```caddyfile
puzzary.home.arpa {
    tls internal
    reverse_proxy app:8790
}
```

Set Puzzary's canonical origin and trust only the reverse proxy's narrow source
address or container-network range:

```env
APP_BASE_URL=https://puzzary.home.arpa
PUZZARY_TRUSTED_PROXIES=<caddy-source-address-or-container-cidr>
PUZZARY_HTTPS_REDIRECT=true
PUZZARY_SECURE_COOKIES=auto
```

`app` is the service name in Puzzary's supplied Compose file. Attach Caddy to
that private Docker network and make only Caddy's HTTPS listener reachable from
the LAN. If Caddy runs directly on the host instead, use Puzzary's host-local
listener as the upstream and do not expose that backend listener to untrusted
networks.

### Trust the internal CA on every client

`tls internal` uses Caddy's local certificate authority. Export its root CA
certificate through the documented Caddy data-directory or `caddy trust`
workflow, transfer only the public root certificate, and install it as a trusted
root on every phone, tablet, and computer that will open Puzzary. Keep the CA
private key on the server.

The exact trust-installation steps are controlled by the client operating
system. Confirm that `https://puzzary.home.arpa` opens without a certificate
warning before installing the PWA or downloading puzzles. Clicking through a
warning is not a substitute for trust and may leave service workers unavailable.

## Alternative: a publicly trusted certificate, still private

An operator who owns a domain can use a public hostname and obtain a trusted
certificate with DNS-01 validation. Local DNS can resolve that hostname to the
private home-server address while the firewall continues to reject public
inbound traffic. DNS-01 proves domain control through DNS records, so neither
Puzzary nor Caddy needs to be exposed publicly for certificate issuance.

This approach requires a supported DNS provider integration and narrowly scoped
DNS credentials. Keep those credentials outside Git and configure the actual
provider according to its Caddy DNS module documentation. The resulting
certificate must cover the exact hostname configured in `APP_BASE_URL`.

## Before leaving and after returning

1. At home, connect the device to the LAN and open the trusted HTTPS URL.
2. Sign in and install the PWA if the browser offers installation.
3. Open each eligible puzzle and select **Available offline**, or use the
   **Offline** page to confirm its downloaded status.
4. Test with the device temporarily disconnected before leaving.
5. Play downloaded puzzles while away. Online-only hints, reveals, uploads,
   Group Solve, and uncached content remain unavailable.
6. After returning to the LAN, open Puzzary and review synchronization status or
   resolve any reported progress conflict.

See [Progressive Web App](progressive-web-app.md) for installation behavior,
[Offline puzzle play](offline-puzzle-play.md) for eligibility and security, and
[HTTPS and trusted reverse proxies](https-tls.md) for transport settings.
