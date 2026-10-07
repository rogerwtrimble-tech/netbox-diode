"""Map snapshot records to Diode SDK entities and ingest them."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Protocol

from netboxlabs.diode.sdk.client import DiodeClient
from netboxlabs.diode.sdk.diode.v1.ingester_pb2 import Entity as EntityPB
from netboxlabs.diode.sdk.ingester import (
    VLAN,
    Cable,
    CustomField,
    CustomFieldValue,
    Device,
    Entity,
    GenericObject,
    Interface,
    IPAddress,
    Prefix,
    Site,
)

from . import __version__
from .config import Settings
from .models import PRESENT, STALE, CableEnd, CableRecord, DeviceRecord, PrefixRecord, VLANRecord
from .netbox import CF_STALE_SINCE, CF_STATE

log = logging.getLogger(__name__)
APP_NAME = "inventory-recon"


class Ingester(Protocol):
    """Anything with the DiodeClient.ingest signature (real client or test fake)."""

    def ingest(self, entities: Iterable[Any]) -> Any:
        """Send entities to Diode."""


def _cf(**values: CustomFieldValue) -> dict[str, CustomFieldValue]:
    return values


def bootstrap_entities() -> list[EntityPB]:
    """Custom fields that carry reconciliation state on devices (created/updated idempotently).

    Custom-field values are replaced on update, unlike tags which Diode merges, so a
    device that reappears can be cleared automatically.
    """
    return [
        Entity(
            custom_field=CustomField(
                name=CF_STATE,
                type="text",
                label="Reconciliation state",
                description="present | stale (set by inventory-recon via Diode)",
                object_types=["dcim.device"],
                group_name="Reconciliation",
            )
        ),
        Entity(
            custom_field=CustomField(
                name=CF_STALE_SINCE,
                type="date",
                label="Stale since",
                description="First day the device was absent from its source (retirement grace starts here)",
                object_types=["dcim.device"],
                group_name="Reconciliation",
            )
        ),
    ]


def _device_ref(d: DeviceRecord, source_tag: str) -> Device:
    """Full device object; also used nested under interfaces/IPs/cables so order never matters."""
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
        custom_fields=_cf(**{CF_STATE: CustomFieldValue(text=PRESENT)}),
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


def vlan_entity(v: VLANRecord, source: str) -> EntityPB:
    """A site VLAN (Diode matches on site + vid)."""
    return Entity(vlan=VLAN(site=v.site, vid=v.vid, name=v.name, status=v.status, tags=[f"src-{source}"]))


def prefix_entity(p: PrefixRecord, vlans: dict[str, VLANRecord], source: str) -> EntityPB:
    """A site-scoped prefix, bound to its VLAN when given."""
    vlan = vlans[f"{p.site}/{p.vlan}"] if p.vlan else None
    return Entity(
        prefix=Prefix(
            prefix=str(p.prefix),
            scope_site=Site(name=p.site),
            vlan=VLAN(site=vlan.site, vid=vlan.vid, name=vlan.name) if vlan else None,
            status=p.status,
            tags=[f"src-{source}"],
        )
    )


def cable_entity(c: CableRecord, devices: dict[str, DeviceRecord], source: str) -> EntityPB:
    """A cable between two interfaces (Diode matches on terminations, in either order).

    Ends reference devices by site + name only: device entities are always sent first,
    and nesting full device objects makes Diode reject cables whose two ends differ in
    platform ("Conflicting values ... merging duplicate dcim.platform").
    """

    def end(e: CableEnd) -> GenericObject:
        iface = next(i for i in devices[f"{e.site}/{e.device}"].interfaces if i.name == e.interface)
        return GenericObject(
            object_interface=Interface(
                name=iface.name, type=iface.type, enabled=iface.enabled, device=Device(name=e.device, site=e.site)
            )
        )

    return Entity(
        cable=Cable(a_terminations=[end(c.a)], b_terminations=[end(c.b)], status=c.status, tags=[f"src-{source}"])
    )


def stale_entity(site: str, name: str, status: str, since: date) -> EntityPB:
    """Partial update: Diode matches on site+name and only touches status + state fields."""
    return Entity(
        device=Device(
            name=name,
            site=site,
            status=status,
            custom_fields=_cf(
                **{
                    CF_STATE: CustomFieldValue(text=STALE),
                    CF_STALE_SINCE: CustomFieldValue(date=datetime(since.year, since.month, since.day, tzinfo=UTC)),
                }
            ),
        )
    )


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
