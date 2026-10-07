#!/bin/sh
# Prepare gcloud's SSH key and the cloud-coder config, then serve the HTTP API.
#   CLOUD_CODER_SSH_KEY_FILE   the private key (mounted from Secret Manager)
#   CLOUD_CODER_CONFIG_YAML    the contents of config.yaml
#   CLOUD_CODER_API_*_TOKENS   bearer tokens (the server refuses to start without one)
#   CLOUD_CODER_PUBLIC_URL     the URL clients reach the API at (the OAuth issuer)
set -eu

key="$HOME/.ssh/google_compute_engine"
mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"
# cat, not cp: cp of the secret volume file failed on Cloud Run ("replaced while being copied").
cat "$CLOUD_CODER_SSH_KEY_FILE" > "$key"
chmod 600 "$key"
ssh-keygen -y -f "$key" > "$key.pub"

export CLOUD_CODER_CONFIG="$HOME/.config/cloud-coder/config.yaml"
mkdir -p "$(dirname "$CLOUD_CODER_CONFIG")"
printf '%s\n' "$CLOUD_CODER_CONFIG_YAML" > "$CLOUD_CODER_CONFIG"

exec cloud-coder api --host 0.0.0.0 --port "${PORT:-8080}"
