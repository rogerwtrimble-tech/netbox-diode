# NetBox (netbox-docker) with the Diode NetBox plugin baked in.
ARG NETBOX_IMAGE=netboxcommunity/netbox:v4.7.2-5.1.1@sha256:3cf9d3bd77aa186233b84d0ea1cf1755581d5e5678fa7d86ec3be62f79d0c643
FROM ${NETBOX_IMAGE}

COPY plugin_requirements.txt /opt/netbox/plugin_requirements.txt
# Optional corporate/TLS-inspecting proxy CA bundle (compose build secret
# "extra_ca", set EXTRA_CA_BUNDLE in .env). Used only at build time.
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -s /run/secrets/extra_ca ]; then export SSL_CERT_FILE=/run/secrets/extra_ca; fi; \
    /usr/local/bin/uv pip install --python /opt/netbox/venv/bin/python \
    -r /opt/netbox/plugin_requirements.txt

# Bind IPv4 so NetBox also starts on IPv4-only hosts/networks (upstream binds "::").
RUN sed -i 's|--host "::"|--host "${GRANIAN_HOST:-0.0.0.0}"|' /opt/netbox/launch-netbox.sh \
    && grep -q 'GRANIAN_HOST' /opt/netbox/launch-netbox.sh

COPY configuration/plugins.py /etc/netbox/config/plugins.py

# Plugin static assets must be collected at build time (netbox-docker convention).
RUN DEBUG="true" SECRET_KEY="build-only-not-a-secret-build-only-not-a-secret-xx" \
    /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py collectstatic --no-input
