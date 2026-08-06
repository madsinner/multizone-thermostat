"""Climate platform for Multizone Thermostat: virtual thermostats."""
from __future__ import annotations

import logging
from typing import Any
from datetime import timedelta

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    CONF_VIRTUAL_THERMOSTATS,
    CONF_VT_HEATER_SWITCH,
    CONF_VT_NAME,
    CONF_VT_TARGET_TEMP,
    CONF_VT_TEMP_SENSOR,
    CONF_VT_TOLERANCE,
    DEFAULT_VT_TARGET_TEMP,
    DEFAULT_VT_TOLERANCE,
    DOMAIN,
    CONF_VT_COOLER_SWITCH,
    CONF_VT_COOL_TOLERANCE,
    DEFAULT_VT_COOL_TOLERANCE,
    CONF_VT_PRESET_TEMPS_SUMMER,
    CONF_VT_PRESET_TEMPS_WINTER,
    GLOBAL_PRESETS,
    SEASON_SUMMER,
    SEASON_WINTER,
    CONF_SEASON,
    DEFAULT_SEASON,
    DEFAULT_SUMMER_PRESET_TEMPS,
    DEFAULT_WINTER_PRESET_TEMPS,
    CONF_ZONE_WINDOW_SENSOR,
    ZONE_MODE_BYPASS,
)
from .pwm_engine import PWMEngine

_LOGGER = logging.getLogger(__name__)

# ===== ЛОКАЛЬНАЯ ФУНКЦИЯ =====
def make_vt_entity_id(name: str) -> str:
    safe_name = name.lower().replace(" ", "_")
    safe_name = "".join(c for c in safe_name if c.isalnum() or c == "_")
    return f"climate.vt_{safe_name}"


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    virtual_thermostats = config_entry.data.get(CONF_VIRTUAL_THERMOSTATS, [])
    if not virtual_thermostats:
        return
    season = config_entry.data.get(CONF_SEASON, DEFAULT_SEASON)
    entities = []
    for vt_config in virtual_thermostats:
        entities.append(
            MultizoneVirtualThermostat(
                hass=hass,
                entry_id=config_entry.entry_id,
                name=vt_config[CONF_VT_NAME],
                temp_sensor=vt_config[CONF_VT_TEMP_SENSOR],
                heater_switch=vt_config[CONF_VT_HEATER_SWITCH],
                target_temp=vt_config.get(CONF_VT_TARGET_TEMP, DEFAULT_VT_TARGET_TEMP),
                tolerance=vt_config.get(CONF_VT_TOLERANCE, DEFAULT_VT_TOLERANCE),
                cooler_switch=vt_config.get(CONF_VT_COOLER_SWITCH),
                cool_tolerance=vt_config.get(CONF_VT_COOL_TOLERANCE, DEFAULT_VT_COOL_TOLERANCE),
                preset_temps_summer=vt_config.get(CONF_VT_PRESET_TEMPS_SUMMER, DEFAULT_SUMMER_PRESET_TEMPS),
                preset_temps_winter=vt_config.get(CONF_VT_PRESET_TEMPS_WINTER, DEFAULT_WINTER_PRESET_TEMPS),
                season=season,
            )
        )
    async_add_entities(entities)


class MultizoneVirtualThermostat(RestoreEntity, ClimateEntity):
    _attr_has_entity_name = True
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.PRESET_MODE
    )
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_min_temp = 5.0
    _attr_max_temp = 35.0
    _attr_target_temperature_step = 0.5
    _attr_preset_modes = GLOBAL_PRESETS

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        name: str,
        temp_sensor: str,
        heater_switch: str,
        target_temp: float,
        tolerance: float,
        cooler_switch: str | None = None,
        cool_tolerance: float = DEFAULT_VT_COOL_TOLERANCE,
        preset_temps_summer: dict[str, float] = None,
        preset_temps_winter: dict[str, float] = None,
        season: str = DEFAULT_SEASON,
    ) -> None:
        self.hass = hass
        self._name = name
        self._temp_sensor = temp_sensor
        self._heater_switch = heater_switch
        self._tolerance = tolerance
        self._cooler_switch = cooler_switch
        self._cool_tolerance = cool_tolerance
        self._preset_temps_summer = preset_temps_summer or DEFAULT_SUMMER_PRESET_TEMPS
        self._preset_temps_winter = preset_temps_winter or DEFAULT_WINTER_PRESET_TEMPS
        self._season = season
        self._preset_temperatures = self._get_active_preset_temps()
        self._window_open = False

        # Ищем датчик окна для этой зоны
        self._window_sensor = None
        coordinator = hass.data[DOMAIN][entry_id]["coordinator"]
        vt_entity_id = make_vt_entity_id(name)
        for zone in coordinator.zones:
            if zone.get("climate_entity") == vt_entity_id:
                self._window_sensor = zone.get(CONF_ZONE_WINDOW_SENSOR)
                break

        if season == SEASON_WINTER:
            self._hvac_mode = HVACMode.HEAT
        else:
            self._hvac_mode = HVACMode.COOL if cooler_switch is not None else HVACMode.OFF

        self._target_temperature = target_temp
        self._current_temperature: float | None = None
        self._preset_mode: str | None = None
        self._coordinator = coordinator
        self._valve_pwm = PWMEngine(pwm_interval=900.0, min_on=0.0, min_off=0.0)
        self._heater_state: bool | None = None
        self._cooler_state: bool | None = None

        vt_entity_id = make_vt_entity_id(name)
        safe_name = name.lower().replace(" ", "_")
        safe_name = "".join(c for c in safe_name if c.isalnum() or c == "_")
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_vt_{safe_name}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_vt_{safe_name}")},
            name=f"{name} Thermostat",
            manufacturer="Custom Integration",
            model="Virtual Thermostat",
            via_device=(DOMAIN, entry_id),
        )
        self._unsub_listeners: list = []

    def _get_active_preset_temps(self) -> dict[str, float]:
        if self._season == SEASON_SUMMER:
            return self._preset_temps_summer
        else:
            return self._preset_temps_winter

    @property
    def name(self) -> str:
        return f"VT {self._name}"

    @property
    def hvac_modes(self) -> list[HVACMode]:
        modes = [HVACMode.OFF, HVACMode.HEAT]
        if self._cooler_switch is not None:
            modes.append(HVACMode.COOL)
            modes.append(HVACMode.HEAT_COOL)
        return modes

    @property
    def hvac_mode(self) -> HVACMode:
        return self._hvac_mode

    @property
    def hvac_action(self) -> HVACAction:
        if self._hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        if self._heater_state:
            return HVACAction.HEATING
        if self._cooler_state:
            return HVACAction.COOLING
        return HVACAction.IDLE

    @property
    def current_temperature(self) -> float | None:
        return self._current_temperature

    @property
    def target_temperature(self) -> float | None:
        return self._target_temperature

    @property
    def preset_mode(self) -> str | None:
        return self._preset_mode

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = {
            "temperature_sensor": self._temp_sensor,
            "heater_switch": self._heater_switch,
            "tolerance": self._tolerance,
            "virtual_thermostat": True,
            "season": self._season,
            "window_open": self._window_open,
        }
        if self._cooler_switch is not None:
            attrs["cooler_switch"] = self._cooler_switch
            attrs["cool_tolerance"] = self._cool_tolerance
        if self._preset_mode is not None:
            attrs["preset_mode"] = self._preset_mode
        if self._window_sensor:
            attrs["window_sensor"] = self._window_sensor
        return attrs

    def set_window_state(self, is_open: bool) -> None:
        self._window_open = is_open
        self.async_write_ha_state()
        self.hass.async_create_task(self._async_control())

    async def _is_window_open(self) -> bool:
        """Прямой опрос датчика окна (если он задан)."""
        if self._window_sensor is None:
            return self._window_open
        state = self.hass.states.get(self._window_sensor)
        if state is None:
            return self._window_open
        is_open = state.state == "on"
        # Обновляем кеш, чтобы синхронизировать
        if is_open != self._window_open:
            self._window_open = is_open
            self.async_write_ha_state()
        return is_open

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode not in self.hvac_modes:
            raise ValueError(f"Unsupported HVAC mode: {hvac_mode}")
        if hvac_mode == HVACMode.COOL and self._cooler_switch is None:
            raise ValueError("Cooling not supported")
        self._hvac_mode = hvac_mode
        self.async_write_ha_state()
        await self._async_control()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        self._target_temperature = temperature
        self._preset_mode = None
        self.async_write_ha_state()
        await self._async_control()

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        if preset_mode not in self._attr_preset_modes:
            raise ValueError(f"Unsupported preset mode: {preset_mode}")
        if preset_mode not in self._preset_temperatures:
            _LOGGER.warning("No temperature defined for preset %s, keeping current", preset_mode)
            self._preset_mode = preset_mode
            self.async_write_ha_state()
            return
        self._preset_mode = preset_mode
        self._target_temperature = self._preset_temperatures[preset_mode]
        self.async_write_ha_state()
        await self._async_control()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._coordinator.register_virtual_climate(self)

        last_state = await self.async_get_last_state()
        if last_state is not None:
            if last_state.state in (HVACMode.HEAT, HVACMode.COOL, HVACMode.HEAT_COOL, HVACMode.OFF):
                self._hvac_mode = HVACMode(last_state.state)
            if last_state.attributes.get(ATTR_TEMPERATURE) is not None:
                self._target_temperature = float(last_state.attributes[ATTR_TEMPERATURE])
            if last_state.attributes.get("preset_mode") in self._attr_preset_modes:
                self._preset_mode = last_state.attributes["preset_mode"]
            if last_state.attributes.get("window_open") is not None:
                self._window_open = last_state.attributes["window_open"]

        self._update_current_temp()

        self._unsub_listeners.append(
            async_track_state_change_event(
                self.hass,
                [self._temp_sensor],
                self._async_on_temp_changed,
            )
        )
        self._unsub_listeners.append(
            async_track_state_change_event(
                self.hass,
                [self._heater_switch],
                self._async_on_heater_changed,
            )
        )
        if self._cooler_switch is not None:
            self._unsub_listeners.append(
                async_track_state_change_event(
                    self.hass,
                    [self._cooler_switch],
                    self._async_on_cooler_changed,
                )
            )
        season_entity_id = f"select.{DOMAIN}_season"
        self._unsub_listeners.append(
            async_track_state_change_event(
                self.hass,
                [season_entity_id],
                self._async_on_season_changed,
            )
        )
        self._unsub_listeners.append(
            async_track_time_interval(
                self.hass,
                self._async_pwm_tick,
                timedelta(seconds=10)
            )
        )
        await self._async_control()

    async def async_will_remove_from_hass(self) -> None:
        self._coordinator.unregister_virtual_climate(self)
        for unsub in self._unsub_listeners:
            unsub()
        self._unsub_listeners.clear()

    @callback
    def _update_current_temp(self) -> None:
        state = self.hass.states.get(self._temp_sensor)
        if state and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            try:
                self._current_temperature = float(state.state)
            except (ValueError, TypeError):
                pass

    @callback
    def _async_on_temp_changed(self, event: Event) -> None:
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return
        try:
            self._current_temperature = float(new_state.state)
        except (ValueError, TypeError):
            return
        self.hass.async_create_task(self._async_control())
        self.async_write_ha_state()

    @callback
    def _async_on_heater_changed(self, event: Event) -> None:
        state = self.hass.states.get(self._heater_switch)
        if state is not None:
            self._heater_state = state.state == STATE_ON
        self.async_write_ha_state()

    @callback
    def _async_on_cooler_changed(self, event: Event) -> None:
        if self._cooler_switch is None:
            return
        state = self.hass.states.get(self._cooler_switch)
        if state is not None:
            self._cooler_state = state.state == STATE_ON
        self.async_write_ha_state()

    @callback
    def _async_on_season_changed(self, event: Event) -> None:
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state not in (SEASON_SUMMER, SEASON_WINTER):
            return
        self._season = new_state.state
        self._preset_temperatures = self._get_active_preset_temps()
        if self._preset_mode is not None and self._preset_mode in self._preset_temperatures:
            self._target_temperature = self._preset_temperatures[self._preset_mode]
        if self._season == SEASON_WINTER:
            if self._hvac_mode != HVACMode.HEAT:
                self._hvac_mode = HVACMode.HEAT
        else:
            if self._cooler_switch is not None:
                if self._hvac_mode != HVACMode.COOL:
                    self._hvac_mode = HVACMode.COOL
            else:
                if self._hvac_mode != HVACMode.OFF:
                    self._hvac_mode = HVACMode.OFF
        self.async_write_ha_state()
        self.hass.async_create_task(self._async_control())

    async def _async_control(self) -> None:
        if self._current_temperature is None:
            return

        # ===== ANTI-FROST (глобальный приоритет) =====
        if self._coordinator.is_anti_frost_enabled():
            frost_temp = self._coordinator.get_frost_protection_temp()
            if self._current_temperature < frost_temp:
                await self._async_set_cooler(False)
                await self._async_set_heater(True)
                self._coordinator.set_zone_demand(self.entity_id, 100.0)
                return

        # ===== НОРМАЛЬНАЯ ЛОГИКА =====
        target = self._target_temperature
        current = self._current_temperature
        need_heat = current < target - self._tolerance
        need_cool = current > target + self._cool_tolerance

        # Прямой опрос датчика окна (обновляет кеш)
        window_open = await self._is_window_open()

        if self._hvac_mode == HVACMode.OFF:
            await self._async_set_heater(False)
            await self._async_set_cooler(False)
            self._coordinator.set_zone_demand(self.entity_id, 0.0)

        elif self._hvac_mode == HVACMode.HEAT:
            # Если окно открыто – не включаем нагрев (кроме антифроста, но он уже отработал)
            if window_open:
                await self._async_set_heater(False)
                await self._async_set_cooler(False)
                self._coordinator.set_zone_demand(self.entity_id, 0.0)
            else:
                await self._async_set_cooler(False)
                if self._coordinator.get_master_state():
                    demand = 100.0 if need_heat else 0.0
                    self._coordinator.set_zone_demand(self.entity_id, demand)
                else:
                    self._coordinator.set_zone_demand(self.entity_id, 0.0)
                    await self._async_set_heater(False)

        elif self._hvac_mode == HVACMode.COOL:
            await self._async_set_heater(False)
            # Блокируем охлаждение, если окно открыто или зона в Bypass
            if window_open or self._coordinator.get_zone_mode(self.entity_id) == ZONE_MODE_BYPASS:
                await self._async_set_cooler(False)
                self._coordinator.set_zone_demand(self.entity_id, 0.0)
            else:
                await self._async_set_cooler(need_cool)

        elif self._hvac_mode == HVACMode.HEAT_COOL:
            if need_heat and not need_cool:
                # Блокируем нагрев, если окно открыто
                if window_open:
                    await self._async_set_heater(False)
                    await self._async_set_cooler(False)
                    self._coordinator.set_zone_demand(self.entity_id, 0.0)
                else:
                    await self._async_set_cooler(False)
                    if self._coordinator.get_master_state():
                        self._coordinator.set_zone_demand(self.entity_id, 100.0)
                    else:
                        self._coordinator.set_zone_demand(self.entity_id, 0.0)
                        await self._async_set_heater(False)
            elif need_cool and not need_heat:
                await self._async_set_heater(False)
                if window_open or self._coordinator.get_zone_mode(self.entity_id) == ZONE_MODE_BYPASS:
                    await self._async_set_cooler(False)
                    self._coordinator.set_zone_demand(self.entity_id, 0.0)
                else:
                    await self._async_set_cooler(True)
            else:
                await self._async_set_heater(False)
                await self._async_set_cooler(False)
                self._coordinator.set_zone_demand(self.entity_id, 0.0)

    async def _async_pwm_tick(self, now) -> None:
        """PWM tick for heating – также проверяем окно перед включением."""
        if self._hvac_mode != HVACMode.HEAT and self._hvac_mode != HVACMode.HEAT_COOL:
            await self._async_set_heater(False)
            return
        if self._hvac_mode == HVACMode.HEAT_COOL and self._cooler_state:
            await self._async_set_heater(False)
            return
        if not self._coordinator.get_master_state():
            await self._async_set_heater(False)
            return

        # Проверяем окно перед включением нагрева (PWM)
        if self._coordinator.is_anti_frost_enabled():
            frost_temp = self._coordinator.get_frost_protection_temp()
            if self._current_temperature is not None and self._current_temperature < frost_temp:
                # Антифрост разрешает включение даже при открытом окне
                pass
            else:
                window_open = await self._is_window_open()
                if window_open:
                    await self._async_set_heater(False)
                    return

        demand = self._coordinator.get_zone_demand(self.entity_id)
        if demand is None:
            demand = 0.0
        wanted_state = self._valve_pwm.calculate(demand)
        if wanted_state != self._heater_state:
            await self._async_set_heater(wanted_state)

    async def _async_set_heater(self, state: bool) -> None:
        if state == self._heater_state:
            return
        self._heater_state = state
        if state:
            await self.hass.services.async_call(
                "switch",
                SERVICE_TURN_ON,
                {ATTR_ENTITY_ID: self._heater_switch},
                blocking=False,
            )
        else:
            await self.hass.services.async_call(
                "switch",
                SERVICE_TURN_OFF,
                {ATTR_ENTITY_ID: self._heater_switch},
                blocking=False,
            )
        self.async_write_ha_state()

    async def _async_set_cooler(self, state: bool) -> None:
        if self._cooler_switch is None:
            return

        # Блокируем, если окно открыто (кеш + прямой опрос уже сделан, но на всякий случай)
        if state and self._window_open:
            _LOGGER.debug("Cooling blocked because window is open in %s", self._name)
            return

        # Блокируем, если зона в Bypass
        if state and self._coordinator.get_zone_mode(self.entity_id) == ZONE_MODE_BYPASS:
            _LOGGER.debug("Cooling blocked because zone is in Bypass mode in %s", self._name)
            return

        if state == self._cooler_state:
            return

        if state:
            if not self._coordinator.can_cooler_turn_on():
                return
        else:
            if not self._coordinator.can_cooler_turn_off():
                return

        self._cooler_state = state
        if state:
            await self.hass.services.async_call(
                "switch",
                SERVICE_TURN_ON,
                {ATTR_ENTITY_ID: self._cooler_switch},
                blocking=False,
            )
            self._coordinator.cooler_turned_on()
        else:
            await self.hass.services.async_call(
                "switch",
                SERVICE_TURN_OFF,
                {ATTR_ENTITY_ID: self._cooler_switch},
                blocking=False,
            )
            self._coordinator.cooler_turned_off()
        self.async_write_ha_state()
