"""NetBox plugin configuration for the Diode NetBox plugin."""

from os import environ

PLUGINS = ["netbox_diode_plugin"]

PLUGINS_CONFIG = {
    "netbox_diode_plugin": {
        # gRPC address of the Diode ingress, reachable from inside the NetBox container.
        "diode_target_override": environ.get("DIODE_TARGET", "grpc://diode-nginx:80/diode"),
        # Address shown to users in the NetBox UI (the externally published port).
        "diode_target_display": environ.get("DIODE_TARGET_DISPLAY"),
        # NetBox user that owns every change applied by Diode (audit trail).
        "diode_username": "diode",
        # netbox-to-diode client secret is read from /run/secrets/netbox_to_diode.
    },
}
