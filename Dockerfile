# inventory-recon CLI image (runs inside the compose network).
FROM python:3.13.16-slim@sha256:bf44cdfcb76cd3b41e879bc058fc37ec5872002ccfde7fcb765e218cde0cd79c

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
# Optional corporate/TLS-inspecting proxy CA bundle (compose build secret "extra_ca").
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -s /run/secrets/extra_ca ]; then export PIP_CERT=/run/secrets/extra_ca; fi; \
    pip install . && useradd --uid 10001 --no-create-home recon

USER recon
ENTRYPOINT ["recon"]
CMD ["--help"]
