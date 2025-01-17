"""Support for Tuya Smart devices."""
from __future__ import annotations

from typing import Any, NamedTuple

import requests

from tuya_sharing import (
    CustomerDevice,
    Manager,
    SharingDeviceListener,
    SharingTokenListener,
)

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import dispatcher_send
from homeassistant.const import __version__
from homeassistant.loader import async_get_integration

from .const import (
    CONF_APP_TYPE,
    DOMAIN,
    LOGGER,
    PLATFORMS,
    TUYA_DISCOVERY_NEW,
    TUYA_HA_SIGNAL_UPDATE_ENTITY,
    CONF_TERMINAL_ID,
    CONF_TOKEN_INFO,
    CONF_USER_CODE,
    TUYA_CLIENT_ID,
    CONF_ENDPOINT
)


class HomeAssistantTuyaData(NamedTuple):
    """Tuya data stored in the Home Assistant data object."""

    manager: Manager
    listener: SharingDeviceListener


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Async setup hass config entry."""
    hass.data.setdefault(DOMAIN, {})

    if CONF_APP_TYPE in entry.data:
        raise ConfigEntryAuthFailed("Authentication failed. Please re-authenticate.")

    if hass.data[DOMAIN].get(entry.entry_id) is None:
        token_listener = TokenListener(hass, entry)
        manager = Manager(
            TUYA_CLIENT_ID,
            entry.data[CONF_USER_CODE],
            entry.data[CONF_TERMINAL_ID],
            entry.data[CONF_ENDPOINT],
            entry.data[CONF_TOKEN_INFO],
            token_listener
        )

        listener = DeviceListener(hass, manager)
        manager.add_device_listener(listener)
        hass.data[DOMAIN][entry.entry_id] = HomeAssistantTuyaData(
            manager=manager,
            listener=listener
        )
    else:
        tuya: HomeAssistantTuyaData = hass.data[DOMAIN][entry.entry_id]
        manager = tuya.manager

    await report_version(hass, manager)

    # Get devices & clean up device entities
    await hass.async_add_executor_job(manager.update_device_cache)
    await cleanup_device_registry(hass, manager)

    # Register known device IDs
    device_registry = dr.async_get(hass)
    for device in manager.device_map.values():
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, device.id)},
            manufacturer="Tuya",
            name=device.name,
            model=f"{device.product_name} (unsupported)",
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # If the device does not register any entities, the device does not need to subscribe
    # So the subscription is here
    await hass.async_add_executor_job(manager.refresh_mq)
    return True


async def report_version(hass: HomeAssistant, manager: Manager):
    integration = await async_get_integration(hass, DOMAIN)
    manifest = integration.manifest
    tuya_version = manifest.get('version', 'unknown')
    sdk_version = manifest.get('requirements', 'unknown')
    sharing_sdk = ""
    for item in sdk_version:
        if "device-sharing-sdk" in item:
            sharing_sdk = item.split("==")[1]
    await hass.async_add_executor_job(manager.report_version, __version__, tuya_version, sharing_sdk)


async def cleanup_device_registry(
        hass: HomeAssistant, device_manager: Manager
) -> None:
    """Remove deleted device registry entry if there are no remaining entities."""
    device_registry = dr.async_get(hass)
    for dev_id, device_entry in list(device_registry.devices.items()):
        for item in device_entry.identifiers:
            if item[0] == DOMAIN and item[1] not in device_manager.device_map:
                device_registry.async_remove_device(dev_id)
                break


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unloading the Tuya platforms."""

    LOGGER.debug("unload entry id = %s", entry.entry_id)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove a config entry."""
    LOGGER.debug("remove entry id = %s", entry.entry_id)
    tuya: HomeAssistantTuyaData = hass.data[DOMAIN][entry.entry_id]

    if tuya.manager.mq is not None:
        tuya.manager.mq.stop()
    tuya.manager.remove_device_listener(tuya.listener)
    await hass.async_add_executor_job(tuya.manager.unload)
    hass.data[DOMAIN].pop(entry.entry_id)
    if not hass.data[DOMAIN]:
        hass.data.pop(DOMAIN)
    pass


class DeviceListener(SharingDeviceListener):
    """Device Update Listener."""

    def __init__(
            self,
            hass: HomeAssistant,
            manager: Manager,
    ) -> None:
        """Init DeviceListener."""
        self.hass = hass
        self.manager = manager

    def update_device(self, device: CustomerDevice) -> None:
        """Update device status."""
        LOGGER.debug(
            "Received update for device %s: %s",
            device.id,
            self.manager.device_map[device.id].status,
        )
        dispatcher_send(self.hass, f"{TUYA_HA_SIGNAL_UPDATE_ENTITY}_{device.id}")

    def add_device(self, device: CustomerDevice) -> None:
        """Add device added listener."""
        # Ensure the device isn't present stale
        self.hass.add_job(self.async_remove_device, device.id)

        dispatcher_send(self.hass, TUYA_DISCOVERY_NEW, [device.id])

    def remove_device(self, device_id: str) -> None:
        """Add device removed listener."""
        self.hass.add_job(self.async_remove_device, device_id)

    @callback
    def async_remove_device(self, device_id: str) -> None:
        """Remove device from Home Assistant."""
        LOGGER.debug("Remove device: %s", device_id)
        device_registry = dr.async_get(self.hass)
        device_entry = device_registry.async_get_device(
            identifiers={(DOMAIN, device_id)}
        )
        if device_entry is not None:
            device_registry.async_remove_device(device_entry.id)


class TokenListener(SharingTokenListener):
    def __init__(
            self,
            hass: HomeAssistant,
            entry: ConfigEntry,
    ) -> None:
        """Init TokenListener."""
        self.hass = hass
        self.entry = entry

    def update_token(self, token_info: [str, Any]):
        data = {**self.entry.data, "token_info": token_info}
        LOGGER.debug("update token info : %s", data)
        self.hass.config_entries.async_update_entry(self.entry, data=data)
