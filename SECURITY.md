# Security boundaries

This is an experimental reference implementation. Studio trusts its local
operator and does not implement a general multi-user server permission model.
Host/Origin checks and the optional Tailscale owner check do not turn it into a
public administration application. Keep Studio bound to loopback.

The proof service exposes a smaller set of routes, separate review credentials,
source-gallery lifecycle checks, revision checks and approval/export constraints.
Those controls are useful foundations, not a claim of a complete security audit.
Deploying it externally needs TLS/proxy configuration, process/network isolation,
private storage, retention/backup controls and a review of the intended audience.
Gallery discovery must remain behind the gallery's own authorisation; do not
expose `/internal/` endpoints. Never expose original uploads or export archives.

Do not commit `data/`, credentials, environment files, client photos, proofs,
password hashes, logs, backups or generated print files. Use a separate private
data directory and protect its filesystem permissions and backups. Review any
files before adding them to Git; `.gitignore` and pattern checks are safeguards,
not comprehensive data-loss prevention.

Optional PicPeak/CRM adapters execute code inside a locally configured container
and depend on its privileges, internals and storage. They need native scoped
authorisation before general multi-user use. Remote publishing is opt-in; no SSH
destination, private identity or production credential is bundled. The payment
adapter is test-only and must not be used with live credentials.

Please do not post credentials, client information or working proof links in
public issues. For a sensitive report, use GitHub's private vulnerability reporting
if enabled; otherwise ask the repository owner for a private reporting channel
without including exploit details or customer material.
