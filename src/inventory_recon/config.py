"""Runtime configuration, loaded from RECON_* environment variables."""

from pathlib import Path
from typing import Literal

from pydantic import Field, HttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All tunables for a reconciliation run."""

    model_config = SettingsConfigDict(env_prefix="RECON_", extra="ignore")

    netbox_url: HttpUrl = HttpUrl("http://localhost:8000")
    netbox_token: SecretStr
    netbox_timeout_s: float = 30.0

    diode_target: str = "grpc://localhost:8080/diode"
    diode_client_id: str = "diode-ingest"
    diode_client_secret: SecretStr

    data_dir: Path = Path("data")
    reports_dir: Path = Path("reports")

    # Devices in scope but absent from the snapshot: "flag" sets status=offline
    # and tag recon-stale through Diode; "report" only lists them. Never deletes.
    stale_action: Literal["flag", "report"] = "flag"
    stale_status: str = "offline"
    stale_tag: str = "recon-stale"

    # Diode applies asynchronously; poll NetBox until it converges.
    converge_timeout_s: float = Field(default=600.0, gt=0)
    poll_interval_s: float = Field(default=5.0, gt=0)
