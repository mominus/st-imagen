# Cloudflare Origin CA certificates

Production servers place the Cloudflare Origin CA certificate at `origin.pem` and its
private key at `origin.key`. Both files are ignored by Git and must never be committed.
