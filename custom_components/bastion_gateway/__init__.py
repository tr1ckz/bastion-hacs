"""Bastion Backup Gateway integration.

Registers Bastion as a native Home Assistant ``BackupAgent`` so the user's
existing backup workflow (UI button, automations, scheduled backups) can push
the resulting ``.tar`` archive into the Bastion ingress API.
"""
from __future__ import annotations

import logging
from typing import Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN

_LOGGER: Final = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Bastion Backup Gateway from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = entry

    # Tell the core backup integration that the set of backup agents has
    # changed so our agent appears in the UI immediately.
    _async_notify_backup_listeners(hass)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Bastion Backup Gateway config entry."""
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    _async_notify_backup_listeners(hass)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the integration when the config entry changes (URL/token edit)."""
    await hass.config_entries.async_reload(entry.entry_id)


@callback
def _async_notify_backup_listeners(hass: HomeAssistant) -> None:
    """Fire all listeners registered by the core backup integration."""
    for listener in hass.data.get(DATA_BACKUP_AGENT_LISTENERS, []):
        try:
            listener()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Backup agent listener raised")


# Key used by ``homeassistant.components.backup`` to store agent-listener
# callables. Defined here as a string so we don't have to import the backup
# component at module import time (it's a soft dependency at platform load).
DATA_BACKUP_AGENT_LISTENERS = "backup_agent_listeners"
