"""Map snapshot records to Diode SDK entities and ingest them."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from netboxlabs.diode.sdk.client import DiodeClient
from netboxlabs.diode.sdk.diode.v1.ingester_pb2 import Entity as EntityPB
from netboxlabs.diode.sdk.ingester import Device, Entity, Interface, IPAddress

from . import __version__
from .config import Settings
from .models import DeviceRecord

log = logging.getLogger(__name__)
APP_NAME = "inventory-recon"


class Ingester(Protocol):
    """Anything with the DiodeClient.ingest signature (real client or test fake)."""

    def ingest(self, entities: Iterable[Any]) -> Any:
        """Send entities to Diode."""


def _device_ref(d: DeviceRecord, source_tag: str) -> Device:
    """Full device object; also used nested under interfaces/IPs so order never matters."""
    return Device(
        name=d.name,
        site=d.site,
        role=d.role,
        manufacturer=d.manufacturer,
        device_type=d.model,
        platform=d.platform,
        serial=d.serial,
        status=d.status,
        tags=[source_tag],
    )


def device_entities(d: DeviceRecord, source: str) -> list[EntityPB]:
    """Entities for one device: the device, its interfaces and their IPs."""
    tag = f"src-{source}"
    dev = _device_ref(d, tag)
    out = [Entity(device=dev)]
    for i in d.interfaces:
        iface = Interface(name=i.name, type=i.type, enabled=i.enabled, device=dev)
        out.append(Entity(interface=iface))
        if i.ip:
            out.append(
                Entity(
                    ip_address=IPAddress(
                        address=str(i.ip), status="active", assigned_object_interface=iface, tags=[tag]
                    )
                )
            )
    return out


def stale_entity(site: str, name: str, status: str, tag: str) -> EntityPB:
    """Partial update: Diode matches on site+name and only touches status/tags."""
    return Entity(device=Device(name=name, site=site, status=status, tags=[tag]))


@dataclass
class IngestResult:
    """Outcome of sending a batch of entities to Diode."""

    requests: int = 0
    entities: int = 0
    errors: list[str] = field(default_factory=list)


def ingest(client: Ingester, batches: Sequence[list[EntityPB]]) -> IngestResult:
    """Send one request per batch; collect, never raise on, per-request errors."""
    result = IngestResult()
    for batch in batches:
        if not batch:
            continue
        try:
            resp = client.ingest(entities=batch)
            result.errors.extend(str(e) for e in (resp.errors or []))
        except Exception as exc:  # gRPC/auth failure: record and continue with next batch
            log.exception("Diode ingest request failed")
            result.errors.append(f"{type(exc).__name__}: {exc}")
        result.requests += 1
        result.entities += len(batch)
    return result


def open_client(settings: Settings) -> DiodeClient:
    """Authenticated Diode client (OAuth2 client-credentials via diode-auth)."""
    return DiodeClient(
        target=settings.diode_target,
        app_name=APP_NAME,
        app_version=__version__,
        client_id=settings.diode_client_id,
        client_secret=settings.diode_client_secret.get_secret_value(),
    )
