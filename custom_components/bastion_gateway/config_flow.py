"""Config flow for Bastion Backup Gateway."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_TOKEN, CONF_URL, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_URL, description={"suggested_value": "http://192.168.1.50:8000"}): str,
        vol.Required(CONF_TOKEN): str,
    }
)


def _normalize_url(raw: str) -> str:
    url = (raw or "").strip().rstrip("/")
    if not url:
        raise ValueError("empty_url")
    if not url.startswith(("http://", "https://")):
        raise ValueError("invalid_scheme")
    return url


async def _async_validate(hass, url: str, token: str) -> str | None:
    """Hit the Bastion ingress healthz endpoint to validate URL + reachability.

    Returns an error key if validation failed, ``None`` on success.
    """
    session = async_get_clientsession(hass)
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with session.get(
            f"{url}/api/ingress/healthz",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status == 401:
                return "invalid_auth"
            if resp.status == 503:
                # Server reachable but ingress token not configured server-side.
                return "ingress_disabled"
            if resp.status >= 500:
                return "server_error"
            if resp.status >= 400:
                return "cannot_connect"
            await resp.read()
    except aiohttp.ClientError:
        return "cannot_connect"
    except TimeoutError:
        return "timeout"
    return None


class BastionConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Bastion Backup Gateway."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial user-driven setup step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                url = _normalize_url(user_input[CONF_URL])
            except ValueError as exc:
                errors["base"] = str(exc)
            else:
                token = user_input[CONF_TOKEN].strip()
                if not token:
                    errors["base"] = "empty_token"
                else:
                    await self.async_set_unique_id(url)
                    self._abort_if_unique_id_configured()

                    err = await _async_validate(self.hass, url, token)
                    if err:
                        errors["base"] = err
                    else:
                        return self.async_create_entry(
                            title=f"Bastion ({url})",
                            data={CONF_URL: url, CONF_TOKEN: token},
                        )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )
