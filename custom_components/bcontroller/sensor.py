"""Sensor platform for BController.

Exposes five sensor entities:
  - btc_price          — current BTC/USDT price
  - portfolio_value    — total portfolio value in USDT
  - savings_balance    — locked savings in USDT
  - system_status      — active / paused / halted / stopped / monitoring / error
  - last_decision      — human-readable string of the last Claude decision
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfCurrency
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DATA_COORDINATOR,
    DOMAIN,
    SENSOR_BTC_PRICE,
    SENSOR_LAST_DECISION,
    SENSOR_PORTFOLIO_VALUE,
    SENSOR_SAVINGS_BALANCE,
    SENSOR_SYSTEM_STATUS,
)
from .coordinator import BControllerCoordinator

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class BControllerSensorDescription(SensorEntityDescription):
    """Extended sensor description with a value extractor callable."""

    value_fn: Callable[[dict[str, Any]], Any]
    extra_attrs_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None


SENSOR_DESCRIPTIONS: tuple[BControllerSensorDescription, ...] = (
    BControllerSensorDescription(
        key=SENSOR_BTC_PRICE,
        name="BTC Price",
        icon="mdi:bitcoin",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="USDT",
        value_fn=lambda data: round(data.get(SENSOR_BTC_PRICE, 0.0), 2),
        suggested_display_precision=2,
    ),
    BControllerSensorDescription(
        key=SENSOR_PORTFOLIO_VALUE,
        name="Portfolio Value",
        icon="mdi:wallet",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement="USDT",
        value_fn=lambda data: round(data.get(SENSOR_PORTFOLIO_VALUE, 0.0), 2),
        suggested_display_precision=2,
        extra_attrs_fn=lambda data: {
            "open_positions": data.get("open_positions", 0),
            "binance_weight_1m": data.get("binance_weight", 0),
        },
    ),
    BControllerSensorDescription(
        key=SENSOR_SAVINGS_BALANCE,
        name="Savings Balance",
        icon="mdi:piggy-bank",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement="USDT",
        value_fn=lambda data: round(data.get(SENSOR_SAVINGS_BALANCE, 0.0), 4),
        suggested_display_precision=2,
        extra_attrs_fn=lambda data: {
            "note": "Held as USDT on Binance. Not in own custody."
        },
    ),
    BControllerSensorDescription(
        key=SENSOR_SYSTEM_STATUS,
        name="System Status",
        icon="mdi:shield-check",
        device_class=None,
        state_class=None,
        native_unit_of_measurement=None,
        value_fn=lambda data: data.get(SENSOR_SYSTEM_STATUS, "unknown"),
        extra_attrs_fn=lambda data: {
            "last_error": data.get("last_error"),
        },
    ),
    BControllerSensorDescription(
        key=SENSOR_LAST_DECISION,
        name="Last Decision",
        icon="mdi:brain",
        device_class=None,
        state_class=None,
        native_unit_of_measurement=None,
        value_fn=lambda data: data.get(SENSOR_LAST_DECISION, "No decision yet"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up BController sensor entities from a config entry."""
    coordinator: BControllerCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]

    async_add_entities(
        [
            BControllerSensor(coordinator, entry, description)
            for description in SENSOR_DESCRIPTIONS
        ]
    )


class BControllerSensor(CoordinatorEntity[BControllerCoordinator], SensorEntity):
    """A single BController sensor entity backed by the coordinator."""

    entity_description: BControllerSensorDescription

    def __init__(
        self,
        coordinator: BControllerCoordinator,
        entry: ConfigEntry,
        description: BControllerSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_has_entity_name = True
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "BController",
            "manufacturer": "BController",
            "model": "Autonomous Crypto Trader",
            "sw_version": "0.1.0",
            "configuration_url": "https://github.com/Jerry0022/homeassistant-bcontroller",
        }

    @property
    def native_value(self) -> Any:
        """Return the sensor value extracted from coordinator data."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return additional attributes if defined for this sensor."""
        if (
            self.coordinator.data is None
            or self.entity_description.extra_attrs_fn is None
        ):
            return None
        return self.entity_description.extra_attrs_fn(self.coordinator.data)

    @property
    def available(self) -> bool:
        """Sensor is unavailable only if coordinator has never succeeded."""
        return self.coordinator.last_update_success or self.coordinator.data is not None
