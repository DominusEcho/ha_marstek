"""Client for the Marstek (Hame) cloud API.

The cloud API is the only way to read devices that do not expose the local
Open API, such as the Marstek micro-inverters. Logging in with the account
e-mail and password returns a token; the device list is then polled with that
token. Tokens expire, so the client re-logs in and retries once on an
authentication failure.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import logging
import time
from typing import Any, Final

from aiohttp import ClientError, ClientSession

_LOGGER = logging.getLogger(__name__)

LOGIN_URL: Final = "https://eu.hamedata.com/app/Solar/v2_get_device.php"
DEVICE_LIST_URL: Final = "https://eu.hamedata.com/ems/api/v1/getDeviceList"
REQUEST_TIMEOUT: Final = 15

# The cloud reports the battery level in tenths of a percent.
SOC_SCALE: Final = 10

# How long a fetched device list is reused. The per-device coordinators share
# one client and poll on the same interval, so a short cache collapses their
# near-simultaneous polls into a single cloud request.
CACHE_TTL: Final = 10.0


class MarstekCloudError(Exception):
    """A cloud request failed."""


class MarstekCloudAuthError(MarstekCloudError):
    """The cloud rejected the credentials or token."""


def hash_password(password: str) -> str:
    """Return the MD5 hex digest the cloud expects for a password."""
    return hashlib.md5(password.encode()).hexdigest()  # noqa: S324


@dataclass(frozen=True)
class MarstekCloudDevice:
    """A device as reported by the cloud device list."""

    devid: str
    name: str
    device_type: str
    mac: str
    version: str
    pv_power: int | None
    grid_power: int | None
    load_power: int | None
    charge_power: int | None
    discharge_power: int | None
    battery_soc: float | None
    battery_num: int
    report_time: int | None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> MarstekCloudDevice:
        """Build a device from one entry of the cloud device list."""

        def watts(key: str) -> int | None:
            value = data.get(key)
            return int(value) if isinstance(value, (int, float)) else None

        soc = data.get("soc")
        return cls(
            devid=str(data.get("devid", "")),
            name=str(data.get("name") or data.get("devid") or "Marstek device"),
            device_type=str(data.get("type", "")),
            mac=str(data.get("mac", "")),
            version=str(data.get("version", "")),
            pv_power=watts("pv"),
            grid_power=watts("grid"),
            load_power=watts("load"),
            charge_power=watts("charge"),
            discharge_power=watts("discharge"),
            battery_soc=(
                round(soc / SOC_SCALE, 1)
                if isinstance(soc, (int, float))
                else None
            ),
            battery_num=int(data.get("battery_num", 0) or 0),
            report_time=(
                int(data["report_time"])
                if isinstance(data.get("report_time"), (int, float))
                else None
            ),
        )


class MarstekCloudClient:
    """Shared cloud client for one account.

    Instances are reused across the device coordinators of the same account so
    the device list is fetched once per poll and the token is shared.
    """

    def __init__(
        self,
        session: ClientSession,
        mailbox: str,
        password_hash: str,
        token: str | None = None,
    ) -> None:
        """Initialize the client with an account and, optionally, a saved token."""
        self._session = session
        self._mailbox = mailbox
        self._password_hash = password_hash
        self._token = token
        self._lock = asyncio.Lock()
        self._cache: dict[str, MarstekCloudDevice] | None = None
        self._cache_time = 0.0

    @property
    def token(self) -> str | None:
        """Return the current token, or None before the first login."""
        return self._token

    async def async_login(self) -> str:
        """Log in and return a fresh token."""
        try:
            async with self._session.get(
                LOGIN_URL,
                params={"pwd": self._password_hash, "mailbox": self._mailbox},
                timeout=REQUEST_TIMEOUT,
            ) as response:
                response.raise_for_status()
                payload = await response.json(content_type=None)
        except ClientError as err:
            raise MarstekCloudError(f"Cloud login request failed: {err}") from err

        token = payload.get("token")
        if not token:
            raise MarstekCloudAuthError(
                f"Cloud login was rejected: {payload.get('msg', 'no token returned')}"
            )
        self._token = token
        _LOGGER.debug("Cloud login succeeded for %s", self._mailbox)
        return token

    async def async_get_devices(
        self, *, force: bool = False
    ) -> dict[str, MarstekCloudDevice]:
        """Return the account's devices keyed by device id, refreshing the token.

        A recent result is reused unless ``force`` is set. Logs in first if
        there is no token yet, and once more if the token has expired, so a
        saved token that has gone stale recovers on its own.
        """
        async with self._lock:
            if (
                not force
                and self._cache is not None
                and time.monotonic() - self._cache_time < CACHE_TTL
            ):
                return self._cache

            if self._token is None:
                await self.async_login()
            try:
                devices = await self._async_fetch_devices()
            except MarstekCloudAuthError:
                _LOGGER.debug(
                    "Cloud token rejected for %s, logging in again", self._mailbox
                )
                await self.async_login()
                devices = await self._async_fetch_devices()

            self._cache = devices
            self._cache_time = time.monotonic()
            return devices

    async def _async_fetch_devices(self) -> dict[str, MarstekCloudDevice]:
        """Fetch and parse the device list with the current token."""
        try:
            async with self._session.get(
                DEVICE_LIST_URL,
                params={"mailbox": self._mailbox, "token": self._token},
                timeout=REQUEST_TIMEOUT,
            ) as response:
                response.raise_for_status()
                payload = await response.json(content_type=None)
        except ClientError as err:
            raise MarstekCloudError(f"Cloud device list failed: {err}") from err

        # code 1 is success; anything else (commonly an expired token) is a
        # soft failure returned with HTTP 200.
        if payload.get("code") != 1:
            raise MarstekCloudAuthError(
                f"Cloud device list was rejected: {payload.get('msg', 'unknown error')}"
            )

        devices = {
            device.devid: device
            for raw in payload.get("data", [])
            if (device := MarstekCloudDevice.from_json(raw)).devid
        }
        _LOGGER.debug("Cloud returned %d devices for %s", len(devices), self._mailbox)
        return devices
