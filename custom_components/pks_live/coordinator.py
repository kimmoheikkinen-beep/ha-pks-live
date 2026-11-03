"""Data update coordinator for PKS Live."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import async_add_external_statistics, get_last_statistics
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import PksApiError, PksAuthError, PksClient
from .const import DOMAIN, STAT_CONSUMPTION, STAT_PRODUCTION

_LOGGER = logging.getLogger(__name__)
BACKFILL_DAYS = 90


class PksCoordinator(DataUpdateCoordinator[dict]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=timedelta(hours=3), config_entry=entry)
        self.client = PksClient(entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD])

    async def _async_update_data(self) -> dict:
        try:
            data = await self.hass.async_add_executor_job(self.client.fetch)
        except PksAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (PksApiError, OSError, ValueError) as err:
            raise UpdateFailed(str(err)) from err
        except Exception as err:  # requests exceptions
            raise UpdateFailed(f"{type(err).__name__}: {err}") from err
        last = None
        for stat_id, name, mp, ctype in (
            (STAT_CONSUMPTION, "PKS ostosähkö", data["sales_mp"], "Sales"),
            (STAT_PRODUCTION, "PKS myyty sähkö", data.get("production_mp"), "SalesProduction"),
        ):
            if not mp:
                continue
            try:
                ts = await self._import_stats(stat_id, name, mp, ctype)
            except Exception as err:  # statistics are best effort; sensors still update
                _LOGGER.warning("Tuntidatan tuonti epäonnistui (%s): %s", stat_id, err)
                continue
            if stat_id == STAT_CONSUMPTION:
                last = ts
        data["last_hour"] = last
        return data

    async def _import_stats(self, stat_id: str, name: str, mp: str, ctype: str) -> datetime | None:
        rec = get_instance(self.hass)
        prev = await rec.async_add_executor_job(get_last_statistics, self.hass, 1, stat_id, True, {"sum"})
        now = datetime.now(timezone.utc)
        if prev.get(stat_id):
            row = prev[stat_id][0]
            total = row.get("sum") or 0.0
            start = datetime.fromtimestamp(row["start"], timezone.utc) + timedelta(hours=1)
        else:
            total = 0.0
            start = (now - timedelta(days=BACKFILL_DAYS)).replace(minute=0, second=0, microsecond=0)
        if start >= now - timedelta(hours=1):
            return start - timedelta(hours=1)
        rows = await self.hass.async_add_executor_job(self.client.hourly, mp, ctype, start, now)
        if not rows:
            return start - timedelta(hours=1)
        stats: list[StatisticData] = []
        for ts, kwh in rows:
            total += kwh
            stats.append(StatisticData(start=ts, state=kwh, sum=total))
        meta = StatisticMetaData(
            mean_type=StatisticMeanType.NONE, has_sum=True, name=name, source=DOMAIN, statistic_id=stat_id,
            unit_class="energy", unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        )
        async_add_external_statistics(self.hass, meta, stats)
        return rows[-1][0]
