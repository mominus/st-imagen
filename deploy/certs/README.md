# Cloudflare Origin CA certificates

Production servers place the Cloudflare Origin CA certificate at `origin.pem` and its
private key at `origin.key`. Both files are ignored by Git and must never be committed.

Keep both files owned by `root:root`. Use mode `0644` for `origin.pem` and mode `0600`
for `origin.key`. The nginx container drops all capabilities except
`NET_BIND_SERVICE`, so a mode-`0600` bind-mounted file owned by the deployment user is
not readable by its master process. Never make the private key world-readable to work
around a permissions error.
