# The HTTP API (`cloud-coder api`) with the Google Cloud CLI, for Cloud Run (see infra/).
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg openssh-client \
 && curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg \
    | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg \
 && echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
    > /etc/apt/sources.list.d/google-cloud-sdk.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends google-cloud-cli python3-numpy \
 && apt-get purge -y curl gnupg && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

# gcloud runs on Debian's python3; numpy there speeds up IAP TCP forwarding.
ENV CLOUDSDK_PYTHON_SITEPACKAGES=1

COPY --from=ghcr.io/astral-sh/uv:0.10 /uv /bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable && rm /bin/uv
ENV PATH="/app/.venv/bin:$PATH"

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
ENTRYPOINT ["docker-entrypoint.sh"]
