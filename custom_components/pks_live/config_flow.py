"""Config flow for PKS Live."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME

from .api import PksApiError, PksAuthError, PksClient
from .const import DOMAIN

SCHEMA = vol.Schema({vol.Required(CONF_USERNAME): str, vol.Required(CONF_PASSWORD): str})


class PksConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def _validate(self, user: str, pw: str) -> tuple[str | None, str | None]:
        client = PksClient(user, pw)
        try:
            await self.hass.async_add_executor_job(client.login)
        except PksAuthError:
            return None, "invalid_auth"
        except (PksApiError, Exception):  # noqa: BLE001
            return None, "cannot_connect"
        return client.customer_id, None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            cid, err = await self._validate(user_input[CONF_USERNAME], user_input[CONF_PASSWORD])
            if err:
                errors["base"] = err
            else:
                await self.async_set_unique_id(cid)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=f"PKS {cid}", data=user_input)
        return self.async_show_form(step_id="user", data_schema=SCHEMA, errors=errors)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            data = {CONF_USERNAME: entry.data[CONF_USERNAME], CONF_PASSWORD: user_input[CONF_PASSWORD]}
            cid, err = await self._validate(data[CONF_USERNAME], data[CONF_PASSWORD])
            if err:
                errors["base"] = err
            else:
                return self.async_update_reload_and_abort(entry, data=data)
        return self.async_show_form(step_id="reauth_confirm",
                                    data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}), errors=errors)
