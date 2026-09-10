# Private service HTTPS

`openbase-coder service publish NAME PORT` publishes a loopback HTTP app at `https://NAME.<account-namespace>.vpn.obs.so/`. Port 80 redirects to HTTPS. Every app owns its root; subpath rewriting, public ingress, and Funnel are unsupported. Apps must listen on `127.0.0.1`, not a LAN or wildcard address.

Cloud allocates account-private DNS records. The signed VPN helper forwards VPN TCP port 443 to a shared TLS ingress on `127.0.0.1:59443`; it does not open a public web listener. The ingress requires an allocated hostname, matching TLS SNI and HTTP Host, and a running per-service gateway. Forwarding headers are replaced at this boundary. Stale session publications do not become active when another app starts.

The device obtains a Let's Encrypt DNS-01 wildcard certificate for `*.<account-namespace>.vpn.obs.so`. Certificate Transparency exposes that random account namespace, but not individual service names. The namespace is an address, not an authentication credential: account isolation is enforced by VPN policy and private DNS. HTTP apps should still use their own application authentication.

ACME account keys and certificate private keys remain on the serving device in owner-only files and directories. Only the SHA-256 DNS validation value goes to the authenticated Cloud broker. Cloud derives the TXT record name from the signed-in account, checks device ownership and service allocation, and never accepts an arbitrary DNS name or provider record ID. Cloudflare credentials remain server-side.

Renewal runs while the HTTPS ingress is active, at two-thirds of the certificate lifetime. A failed renewal retains the current certificate and retries after five minutes. Restarting a publication also renews a due certificate. Certificate replacement is atomic and new TLS connections pick up the new certificate without interrupting existing streams. Interrupted DNS validation is cleaned up on the next attempt. No extra startup job is installed for TLS: `--persist` remains a separate, explicit opt-in for restoring a service at login.

Old HTTP publications can be listed and unpublished, but new publications require an HTTPS-capable Cloud backend and signed helper. To migrate an existing publication, unpublish it, then publish it again with the same name and local port. Update the app's allowed origins to the HTTPS URL and configure trusted loopback proxy headers as appropriate; do not disable certificate validation.
