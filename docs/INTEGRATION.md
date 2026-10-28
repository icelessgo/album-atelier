# Configuration and integration

The manual-upload/synthetic workflow is standalone. The following adapters are
optional, experimental integration points; no service is provisioned automatically.

## Local Studio

- `ALBUM_PROTOTYPE_PORT`: loopback port, default `8775`.
- `ALBUM_DATA_DIR`: absolute private data directory; default `<checkout>/data`.
- The Studio starts its isolated Client Preview listener on a free loopback port.
- The data directory contains originals, generated renditions, designs, proof
  credentials, catalogue choices and approvals. Do not serve it as a static folder.

## Optional PicPeak bridge

`ALBUM_PICPEAK_PROCESSOR` names the local container used by the legacy bridge;
the placeholder default is `picpeak-processor`. The bridge expects deployment
internals and an S3/R2-compatible storage setup. It is not a supported public API
client and does not provide native album ownership/permissions for other studios.
Keep it disabled in an untrusted environment. The same limitation applies to the
optional CRM quote adapter. A future native integration should use PicPeak's
authorised media, storage, customer and worker APIs.

## Optional external review publication

`publisher.py` can copy explicitly published, sanitised proof snapshots to a
separately managed host using SSH/rsync. Configuration uses
`ALBUM_PROOF_REMOTE=user@example-host:/srv/album-proofs` and
`ALBUM_PUBLIC_REVIEW_BASE=https://albums.example.com/review`. These are examples,
not working destinations. Remote directory ownership/permissions and the worker
UID must be chosen for the actual deployment. No SSH key is distributed.

The independent listener defaults to `ALBUM_PROOF_BIND=127.0.0.1` and
`ALBUM_PROOF_PORT=8776`. The hosting operator must configure its private proof
storage, active pointers, permitted proxy routes, TLS, network isolation and
lifecycle lookup. No automatic reverse proxy, DNS change or container deployment
is included. Do not publish the local Studio itself.

For linked galleries, `ALBUM_PICPEAK_INFO_BASE` selects the authorised deployment's
gallery-information endpoint. Publication and review use the configured lifecycle
checks; gallery expiry/revocation must not silently grant continued access.

## Payments, fulfilment and profiles

Payments are optional and unconfigured. The retained Graphistudio adapter accepts
test credentials only; Saal payment/order placement remains manual. Do not add
live payment keys to this prototype or to Git. Commercial pricing, seller details
and delivery rules for a real service need their own approved integration.

The included manufacturing profiles are factual presets from the tested workflow.
Their geometry is deliberately separate from the editable retail catalogue. Check
the current supplier templates, output contract and physical sample before use.
