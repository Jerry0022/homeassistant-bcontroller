"""BController — Autonomous crypto spot trading integration for Home Assistant.

Lifecycle:
  async_setup_entry  → initialise coordinator, run startup recovery, forward platforms
  async_unload_entry → unload platforms, clean up coordinator
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import (
    DATA_COORDINATOR,
    DOMAIN,
)
from .coordinator import BControllerCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up BController from a config entry.

    1. Create and initialise the coordinator (subsystems + startup recovery).
    2. Perform the first data refresh.
    3. Forward to sensor platform.
    """
    coordinator = BControllerCoordinator(hass, entry)

    try:
        await coordinator.async_setup()
    except Exception as exc:
        _LOGGER.error("BController setup failed: %s", exc)
        raise ConfigEntryNotReady(f"Setup failed: {exc}") from exc

    # Run the first refresh — this blocks until we have initial data
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        DATA_COORDINATOR: coordinator,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register an options update listener so parameter changes apply live
    entry.async_on_unload(entry.add_update_listener(async_update_options))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        _LOGGER.info("BController integration unloaded.")

    return unload_ok


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the config entry when options are updated."""
    await hass.config_entries.async_reload(entry.entry_id)
