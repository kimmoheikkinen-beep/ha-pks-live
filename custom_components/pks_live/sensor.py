"""Sensors for PKS Live."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription
from homeassistant.const import PERCENTAGE, UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_utc_time_change
from homeassistant.util import dt as dt_util
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import PksCoordinator

CENT = "c/kWh"


def _g(*path):
    def f(d):
        for p in path:
            if d is None:
                return None
            d = d.get(p)
        return d
    return f


@dataclass(frozen=True, kw_only=True)
class PksDesc(SensorEntityDescription):
    value: Callable[[dict], Any]
    attrs: Callable[[dict], dict] | None = None


def _energy(key, name, path):
    return PksDesc(key=key, name=name, value=_g(*path), native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                   device_class=SensorDeviceClass.ENERGY, suggested_display_precision=1)


def _money(key, name, path, attrs=None):
    return PksDesc(key=key, name=name, value=_g(*path), native_unit_of_measurement="EUR",
                   device_class=SensorDeviceClass.MONETARY, suggested_display_precision=2, attrs=attrs)


def _avg(key):
    def f(d):
        c = d.get(key) or {}
        return round(c["eur"] / c["kwh"] * 100, 2) if c.get("kwh") else None
    return f


DESCS: tuple[PksDesc, ...] = (
    _energy("kulutus_kuukausi", "Ostosähkö kuluva kuukausi", ("cur", "kwh")),
    _money("kustannus_kuukausi", "Sähkölasku kuluva kuukausi (alustava)", ("cur", "eur"), lambda d: (d.get("cur") or {}).get("parts", {})),
    _energy("kulutus_edellinen", "Ostosähkö edellinen kuukausi", ("prev", "kwh")),
    _money("kustannus_edellinen", "Sähkölasku edellinen kuukausi", ("prev", "eur"), lambda d: (d.get("prev") or {}).get("parts", {})),
    PksDesc(key="kokonaishinta_edellinen", name="Sähkön kokonaishinta edellinen kuukausi", value=_avg("prev"),
            native_unit_of_measurement=CENT, suggested_display_precision=2),
    PksDesc(key="energian_hinta_edellinen", name="Energian hinta edellinen kuukausi", value=_g("energy_price_prev"),
            native_unit_of_measurement=CENT, suggested_display_precision=3,
            attrs=lambda d: {"kuukausittain": d.get("energy_prices")}),
    PksDesc(key="kiinnityshinta", name="Kiinnityshinta nyt", value=_g("hedge_price"), native_unit_of_measurement=CENT,
            suggested_display_precision=3,
            attrs=lambda d: {"jakso": d.get("hedge_period"), "kiinnitykset": d.get("hedges")}),
    PksDesc(key="kiinnitysaste", name="Kiinnitysaste nyt", value=_g("hedge_pct"), native_unit_of_measurement=PERCENTAGE),
    PksDesc(key="kiinnittamaton_jakso", name="Seuraava kiinnittämätön jakso",
            value=lambda d: (d.get("hedge_gap") or {}).get("nimi") or "ei ole",
            attrs=lambda d: d.get("hedge_gap") or {}),
    PksDesc(key="toimitusmaksu", name="Toimitusmaksu", value=_g("margin"), native_unit_of_measurement=CENT,
            suggested_display_precision=3),
    PksDesc(key="kuukausimaksu", name="Kuukausimaksu", value=_g("monthly_fee"), native_unit_of_measurement="EUR",
            device_class=SensorDeviceClass.MONETARY, suggested_display_precision=2),
    _energy("myyty_kuukausi", "Myyty sähkö kuluva kuukausi", ("prod_cur", "kwh")),
    _money("myyntitulo_kuukausi", "Myyntitulo kuluva kuukausi", ("prod_cur", "eur")),
    _energy("myyty_edellinen", "Myyty sähkö edellinen kuukausi", ("prod_prev", "kwh")),
    _money("myyntitulo_edellinen", "Myyntitulo edellinen kuukausi", ("prod_prev", "eur")),
    PksDesc(key="kulutusvaikutus_kuukausi", name="Kulutusvaikutus kuluva kuukausi", value=_g("month", "consumption_effect"),
            native_unit_of_measurement=CENT, suggested_display_precision=2,
            attrs=lambda d: {k: (d.get("month") or {}).get(k) for k in
                             ("own_avg", "spot_avg_metered", "metered_kwh", "metered_hours", "metered_until")}),
    PksDesc(key="energian_hinta_ennuste", name="Energian hinta-ennuste kuluva kuukausi", value=_g("month", "energy_price"),
            native_unit_of_measurement=CENT, suggested_display_precision=2,
            attrs=lambda d: {k: (d.get("month") or {}).get(k) for k in
                             ("total_price", "hedge_share", "spot_avg", "spot_hours", "consumption_effect")}),
    PksDesc(key="viimeisin_mittaus", name="Viimeisin mittaustunti", value=_g("last_hour"),
            device_class=SensorDeviceClass.TIMESTAMP),
    PksDesc(key="sopimus", name="Sopimus", value=_g("product"),
            attrs=lambda d: {"maaraaikainen_asti": d.get("fixed_until"), "kayttopaikka": d.get("sales_mp"),
                             "tuotantokayttopaikka": d.get("production_mp")}),
)


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    coord: PksCoordinator = entry.runtime_data
    async_add_entities([*(PksSensor(coord, d) for d in DESCS), PksMarginalSensor(coord)])


class PksSensor(CoordinatorEntity[PksCoordinator], SensorEntity):
    _attr_has_entity_name = True
    entity_description: PksDesc

    def __init__(self, coord: PksCoordinator, desc: PksDesc) -> None:
        super().__init__(coord)
        self.entity_description = desc
        cid = coord.data["customer_id"]
        self._attr_unique_id = f"{cid}_{desc.key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, cid)}, name="PKS Live", manufacturer="Pohjois-Karjalan Sähkö",
                                            model=coord.data.get("product"), entry_type=DeviceEntryType.SERVICE)

    @property
    def native_value(self):
        return self.entity_description.value(self.coordinator.data)

    @property
    def extra_state_attributes(self):
        f = self.entity_description.attrs
        return f(self.coordinator.data) if f else None


MARGINAL = PksDesc(key="ostosahkon_rajahinta", name="Ostosähkön rajahinta nyt", value=lambda d: None,
                   native_unit_of_measurement=CENT, suggested_display_precision=2)


class PksMarginalSensor(PksSensor):
    """Real energy cost of one extra purchased kWh this hour (spot + hedge effect + delivery fee), excl. transfer."""

    def __init__(self, coord: PksCoordinator) -> None:
        super().__init__(coord, MARGINAL)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_track_utc_time_change(self.hass, self._tick, minute=0, second=5))

    @callback
    def _tick(self, _now) -> None:
        self.async_write_ha_state()

    def _prices(self) -> dict:
        return (self.coordinator.data.get("month") or {}).get("marginal") or {}

    @property
    def native_value(self):
        hour = dt_util.utcnow().replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
        return self._prices().get(hour)

    @property
    def extra_state_attributes(self):
        now = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        upcoming = {k: v for k, v in self._prices().items() if dt_util.parse_datetime(k) >= now}
        return {"lisa_spottiin": (self.coordinator.data.get("month") or {}).get("marginal_offset"), "tunnit": upcoming}
