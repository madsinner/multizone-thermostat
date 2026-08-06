"""Coordinator for Multizone Thermostat: handles all heating logic."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, time as dt_time
import logging
import time
from typing import Any

import homeassistant.util.dt as dt_util

from homeassistant.components.climate import (
    ClimateEntityFeature,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_PRESET_MODE,
    DOMAIN as CLIMATE_DOMAIN,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_ON,
    STATE_OFF,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store

from .const import (
    CONF_BOILER_SWITCH,
    CONF_ZONES,
    CONF_PRESENCE_SENSOR,
    CONF_ANTI_SEIZE_ENABLED,
    CONF_ANTI_SEIZE_IDLE_DAYS,
    CONF_ANTI_SEIZE_DURATION,
    CONF_ANTI_SEIZE_BOILER,
    CONF_ZONE_ANTI_SEIZE,
    CONF_ZONE_CLIMATE,
    CONF_ZONE_TRV_SYNC,
    CONF_ZONE_WINDOW_SENSOR,
    DOMAIN,
    GLOBAL_PRESET_MANUAL,
    HVAC_ACTION_HEATING,
    HVAC_MODE_HEAT,
    HVAC_MODE_OFF,
    PRESET_MANUAL,
    PRESET_OFF,
    KEY_NIGHT_TIME,
    KEY_MORNING_TIME,
    KEY_AUTO_NIGHT_MODE,
    KEY_GEOFENCING_TOGGLE,
    KEY_PRE_AWAY_PRESET,
    KEY_PRE_NIGHT_PRESET,
    KEY_ANTI_SEIZE_ENABLED,
    KEY_ANTI_SEIZE_IDLE_DAYS,
    KEY_ANTI_SEIZE_DURATION,
    CONF_MIN_CYCLE_ON,
    CONF_MIN_CYCLE_OFF,
    CONF_VALVE_DELAY,
    DEFAULT_MIN_CYCLE_ON,
    DEFAULT_MIN_CYCLE_OFF,
    DEFAULT_VALVE_DELAY,
    GLOBAL_PRESET_SLEEP,
    GLOBAL_PRESET_AWAY,
    GLOBAL_PRESET_COMFORT,
    ZONE_MODE_PRIMARY,
    ZONE_MODE_SECONDARY,
    ZONE_MODE_BYPASS,
    GLOBAL_PRESETS,
    CONF_ANTI_FROST_ENABLED,
    DEFAULT_ANTI_FROST_ENABLED,
    CONF_FROST_PROTECTION_TEMP,
    DEFAULT_FROST_PROTECTION_TEMP,
)
from .pwm_engine import PWMEngine

_LOGGER = logging.getLogger(__name__)

ATTR_HVAC_ACTION = "hvac_action"
ATTR_HVAC_MODE = "hvac_mode"
ATTR_PRESET_MODE = "preset_mode"

WINDOW_STORAGE_VERSION = 1
WINDOW_STORAGE_KEY = f"{DOMAIN}.window_states"
PRESET_STORAGE_KEY = f"{DOMAIN}.presets"
PRESET_STORAGE_VERSION = 1
SETTINGS_STORAGE_KEY = f"{DOMAIN}.settings"
SETTINGS_STORAGE_VERSION = 1


class MultizoneCoordinator:
    """Central coordinator that manages all heating logic."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: Any,
    ) -> None:
        """Initialize the coordinator."""
        self.hass = hass
        self.entry_id = entry.entry_id
        self._entry = entry
        self.boiler_switch = entry.data.get(CONF_BOILER_SWITCH)
        self.zones = entry.data.get(CONF_ZONES, [])
        self.presence_sensor = entry.data.get(CONF_PRESENCE_SENSOR)
        self.weather_sensor_id = entry.data.get("weather_sensor")
        
        # Internal state tracking
        self._master_state: bool = False
        self._zone_modes: dict[str, str] = {
            z[CONF_ZONE_CLIMATE]: ZONE_MODE_PRIMARY for z in self.zones
        }
        self._zone_demands: dict[str, float] = {
            z[CONF_ZONE_CLIMATE]: 0.0 for z in self.zones
        }
        self._pre_window_state: dict[str, str] = {}
        self._pre_anti_seize_state: dict[str, str] = {}
        self._select_entities = {}
        self._min_cycle_on = 0.0
        self._min_cycle_off = 0.0
        self._valve_delay = 0.0
        self._boiler_state = STATE_OFF
        self._last_boiler_change = 0.0
        self._last_active_time = time.time()
        self._anti_seize_running = False
        
        # Cooler state tracking
        self._cooler_state = False
        self._cooler_last_on = None
        self._cooler_last_off = None
        
        # Frost protection settings
        self._anti_frost_enabled = entry.data.get(CONF_ANTI_FROST_ENABLED, DEFAULT_ANTI_FROST_ENABLED)
        self._frost_protection_temp = entry.data.get(CONF_FROST_PROTECTION_TEMP, DEFAULT_FROST_PROTECTION_TEMP)
        
        self._store = Store(hass, WINDOW_STORAGE_VERSION, WINDOW_STORAGE_KEY)

        self._pending_boiler_task: asyncio.Task | None = None

        self._boiler_pwm = PWMEngine(pwm_interval=900.0, min_on=self._min_cycle_on * 60, min_off=self._min_cycle_off * 60)
        
        self._unsub_listeners: list = []
        self._select_entities: dict[str, Any] = {}
        
        # Centralized PIDs
        from .pid_wrapper import MultizonePID
        from .autotune import PassiveAutotuneObserver
        self._pids: dict[str, MultizonePID] = {}
        self._autotuners: dict[str, PassiveAutotuneObserver] = {}
        for zone in self.zones:
            climate_id = zone[CONF_ZONE_CLIMATE]
            self._pids[climate_id] = MultizonePID(kp=100.0, ki=0.0, kd=0.0, out_min=0.0, out_max=100.0, sensor_timeout=7200.0)
            self._autotuners[climate_id] = PassiveAutotuneObserver(climate_id, required_cycles=3)

        self._preset_store = Store(hass, PRESET_STORAGE_VERSION, PRESET_STORAGE_KEY)
        self._presets: dict[str, dict[str, dict[str, Any]]] = {}
        self._current_global_preset: str = GLOBAL_PRESET_MANUAL
        
        self._settings_store = Store(hass, SETTINGS_STORAGE_VERSION, SETTINGS_STORAGE_KEY)
        self._settings: dict[str, Any] = {}
        
        self._last_night_trigger_date: datetime.date | None = None
        self._last_morning_trigger_date: datetime.date | None = None

        # Registered virtual climates
        self._virtual_climates: list = []

    # ===== REGISTRATION =====
    def register_virtual_climate(self, climate_entity) -> None:
        if climate_entity not in self._virtual_climates:
            self._virtual_climates.append(climate_entity)
            _LOGGER.debug("Registered climate %s", climate_entity.entity_id)

    def unregister_virtual_climate(self, climate_entity) -> None:
        if climate_entity in self._virtual_climates:
            self._virtual_climates.remove(climate_entity)
            _LOGGER.debug("Unregistered climate %s", climate_entity.entity_id)

    # ===== FROST PROTECTION =====
    def is_anti_frost_enabled(self) -> bool:
        return self._anti_frost_enabled

    def get_frost_protection_temp(self) -> float:
        return self._frost_protection_temp

    async def async_set_anti_frost_enabled(self, enabled: bool) -> None:
        self._anti_frost_enabled = enabled
        data = dict(self._entry.data)
        data[CONF_ANTI_FROST_ENABLED] = enabled
        self.hass.config_entries.async_update_entry(self._entry, data=data)
        await self.async_set_persistent_data(CONF_ANTI_FROST_ENABLED, enabled)

    async def async_set_frost_protection_temp(self, value: float) -> None:
        self._frost_protection_temp = value
        data = dict(self._entry.data)
        data[CONF_FROST_PROTECTION_TEMP] = value
        self.hass.config_entries.async_update_entry(self._entry, data=data)
        await self.async_set_persistent_data(CONF_FROST_PROTECTION_TEMP, value)

    # ===== WINDOW STATE NOTIFICATION =====
    def notify_window_state(self, climate_entity: str, is_open: bool) -> None:
        for climate in self._virtual_climates:
            if climate.entity_id == climate_entity:
                climate.set_window_state(is_open)
                break

    # ===== COOLER PROTECTION =====
    def can_cooler_turn_on(self) -> bool:
        if self._cooler_last_off is None:
            return True
        elapsed = time.time() - self._cooler_last_off
        min_off_sec = self._min_cycle_off * 60
        if elapsed < min_off_sec:
            _LOGGER.debug("Cooler off-time not met (%.1f sec < %d sec)", elapsed, min_off_sec)
            return False
        return True

    def can_cooler_turn_off(self) -> bool:
        if self._cooler_last_on is None:
            return True
        elapsed = time.time() - self._cooler_last_on
        min_on_sec = self._min_cycle_on * 60
        if elapsed < min_on_sec:
            _LOGGER.debug("Cooler on-time not met (%.1f sec < %d sec)", elapsed, min_on_sec)
            return False
        return True

    def cooler_turned_on(self) -> None:
        self._cooler_state = True
        self._cooler_last_on = time.time()
        _LOGGER.debug("Cooler turned ON, last_on updated")

    def cooler_turned_off(self) -> None:
        self._cooler_state = False
        self._cooler_last_off = time.time()
        _LOGGER.debug("Cooler turned OFF, last_off updated")

    # ===== PERSISTENT DATA =====
    def get_persistent_data(self, key: str, default: Any = None) -> Any:
        return self._settings.get(key, default)

    async def async_set_persistent_data(self, key: str, value: Any) -> None:
        self._settings[key] = value
        await self._settings_store.async_save(self._settings)

    async def async_load_storage(self) -> None:
        stored = await self._store.async_load()
        if stored and isinstance(stored, dict):
            self._pre_window_state = stored
        preset_stored = await self._preset_store.async_load()
        if preset_stored and isinstance(preset_stored, dict):
            self._presets = preset_stored
        settings_stored = await self._settings_store.async_load()
        if settings_stored and isinstance(settings_stored, dict):
            self._settings = settings_stored
            autotune_data = self._settings.get("autotuners", {})
            for climate_id, tuner in self._autotuners.items():
                if climate_id in autotune_data:
                    tuner.load_state(autotune_data[climate_id])
                    if tuner.state == tuner.STATE_COMPLETED:
                        self._pids[climate_id].set_pid_param(kp=tuner.kp, ki=tuner.ki, kd=tuner.kd)
                        _LOGGER.info("Restored Smart PID for %s: Kp=%.1f, Ki=%.4f, Kd=%.1f", climate_id, tuner.kp, tuner.ki, tuner.kd)

    async def _async_save_storage(self) -> None:
        await self._store.async_save(self._pre_window_state)

    async def _async_save_autotuner_states(self) -> None:
        data = {climate_id: tuner.dump_state() for climate_id, tuner in self._autotuners.items()}
        await self.async_set_persistent_data("autotuners", data)

    async def _async_save_presets_storage(self) -> None:
        await self._preset_store.async_save(self._presets)

    def register_select(self, key: str, select_entity: Any) -> None:
        self._select_entities[key] = select_entity

    def set_master_state(self, state: bool) -> None:
        self._master_state = state

    def get_zone_mode(self, climate_entity: str) -> str:
        return self._zone_modes.get(climate_entity, ZONE_MODE_PRIMARY)

    def get_zone_demand(self, climate_id: str) -> float:
        return self._zone_demands.get(climate_id, 0.0)

    def set_zone_demand(self, climate_id: str, demand: float) -> None:
        self._zone_demands[climate_id] = demand

    def get_pid(self, climate_id: str):
        return self._pids.get(climate_id)

    def set_zone_mode(self, climate_entity: str, mode: str) -> None:
        self._zone_modes[climate_entity] = mode
        if self._current_global_preset:
            if self._current_global_preset not in self._presets:
                self._presets[self._current_global_preset] = {}
            if climate_entity not in self._presets[self._current_global_preset]:
                self._presets[self._current_global_preset][climate_entity] = {}
            self._presets[self._current_global_preset][climate_entity]["mode"] = mode
            self.hass.async_create_task(self._async_save_presets_storage())

    @property
    def current_global_preset(self) -> str:
        return self._current_global_preset

    def set_global_preset(self, preset: str) -> None:
        self._current_global_preset = preset

    def get_global_preset(self) -> str:
        return self._current_global_preset

    async def async_set_global_preset(self, preset: str) -> None:
        if preset not in GLOBAL_PRESETS:
            _LOGGER.warning("Invalid global preset: %s", preset)
            return
        self._current_global_preset = preset
        if "global_preset" in self._select_entities:
            self._select_entities["global_preset"].async_write_ha_state()

        if self._virtual_climates:
            tasks = []
            for climate in self._virtual_climates:
                if hasattr(climate, 'preset_modes') and preset in climate.preset_modes:
                    tasks.append(climate.async_set_preset_mode(preset))
                else:
                    _LOGGER.warning("Climate %s does not support preset %s, skipping", climate.entity_id, preset)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
                _LOGGER.debug("Applied global preset %s to %d thermostats", preset, len(tasks))

        if preset in self._presets:
            preset_data = self._presets[preset]
            for climate_entity, data in preset_data.items():
                is_virtual = any(cl.entity_id == climate_entity for cl in self._virtual_climates)
                if is_virtual:
                    continue
                if "target_temp" in data:
                    try:
                        await self.hass.services.async_call(
                            "climate",
                            "set_temperature",
                            {"entity_id": climate_entity, "temperature": data["target_temp"]},
                            blocking=False,
                        )
                    except Exception as ex:
                        _LOGGER.warning("Could not set temperature for %s: %s", climate_entity, ex)
                mode = ZONE_MODE_PRIMARY
                if "mode" in data:
                    mode = data["mode"]
                elif "bypassed" in data:
                    mode = ZONE_MODE_BYPASS if data["bypassed"] else ZONE_MODE_PRIMARY
                zone_select = self._select_entities.get(f"zone_mode_{climate_entity}")
                if zone_select:
                    try:
                        await zone_select.async_select_option(mode)
                    except Exception as ex:
                        _LOGGER.warning("Could not set zone mode for %s: %s", climate_entity, ex)

    def get_master_state(self) -> bool:
        return self._master_state

    @callback
    def async_setup_listeners(self) -> None:
        climate_entities = [z[CONF_ZONE_CLIMATE] for z in self.zones]
        if climate_entities:
            self._unsub_listeners.append(
                async_track_state_change_event(
                    self.hass,
                    climate_entities,
                    self._async_on_climate_state_changed,
                )
            )
        window_sensors = [
            z[CONF_ZONE_WINDOW_SENSOR] for z in self.zones 
            if z.get(CONF_ZONE_WINDOW_SENSOR) and z[CONF_ZONE_WINDOW_SENSOR] != "none"
        ]
        if window_sensors:
            self._unsub_listeners.append(
                async_track_state_change_event(
                    self.hass,
                    window_sensors,
                    self._async_on_window_state_changed,
                )
            )
        from datetime import timedelta
        self._unsub_listeners.append(
            async_track_time_interval(
                self.hass,
                self._async_pwm_tick,
                timedelta(seconds=10)
            )
        )
        if self.presence_sensor:
            self._unsub_listeners.append(
                async_track_state_change_event(
                    self.hass,
                    [self.presence_sensor],
                    self._async_on_presence_changed,
                )
            )
        self._unsub_listeners.append(
            async_track_time_interval(
                self.hass,
                self._async_check_schedule,
                timedelta(minutes=1),
            )
        )
        self._unsub_listeners.append(
            async_track_time_interval(
                self.hass,
                self._async_check_anti_seize,
                timedelta(hours=1),
            )
        )

    @callback
    def async_teardown_listeners(self) -> None:
        for unsub in self._unsub_listeners:
            unsub()
        self._unsub_listeners.clear()

    async def _async_on_climate_state_changed(self, event: Event) -> None:
        entity_id = event.data.get("entity_id")
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        if not new_state:
            return

        current_temp = new_state.attributes.get("current_temperature")
        target_temp = new_state.attributes.get("temperature")
        if current_temp is not None and target_temp is not None:
            hvac_mode = new_state.state
            zone_mode = self.get_zone_mode(entity_id)
            if hvac_mode == HVAC_MODE_HEAT and zone_mode != ZONE_MODE_BYPASS:
                tuner = self._autotuners[entity_id]
                was_completed = (tuner.state == tuner.STATE_COMPLETED)
                tuner.update(current_temp, self.get_zone_demand(entity_id) > 0)
                is_completed = (tuner.state == tuner.STATE_COMPLETED)
                if is_completed and not was_completed:
                    self._pids[entity_id].set_pid_param(kp=tuner.kp, ki=tuner.ki, kd=tuner.kd)
                    _LOGGER.info("Autotuning completed for %s! Smart PID activated.", entity_id)
                    await self._async_save_autotuner_states()
                if tuner.state != tuner.STATE_COMPLETED:
                    tolerance = 0.3
                    current_demand = self.get_zone_demand(entity_id)
                    if current_temp <= target_temp - tolerance:
                        demand = 100.0
                    elif current_temp >= target_temp + tolerance:
                        demand = 0.0
                    else:
                        demand = current_demand
                else:
                    demand = self._pids[entity_id].calc(current_temp, target_temp)
                    curve_val = self.get_persistent_data("weather_curve", 0.0)
                    if curve_val > 0.0 and self.weather_sensor_id:
                        weather_state = self.hass.states.get(self.weather_sensor_id)
                        if weather_state and weather_state.state not in ("unavailable", "unknown"):
                            try:
                                outdoor_temp = float(weather_state.state)
                                ff_demand = (20.0 - outdoor_temp) * curve_val
                                demand = min(100.0, max(0.0, demand + ff_demand))
                            except ValueError:
                                pass
            else:
                demand = 0.0
            self.set_zone_demand(entity_id, demand)
            tuner = self._autotuners[entity_id]
            if tuner.state != tuner.STATE_COMPLETED:
                tuner.update(current_temp, demand > 0)

        if old_state is not None and self._current_global_preset:
            new_temp = new_state.attributes.get("temperature")
            old_temp = old_state.attributes.get("temperature")
            if new_temp is not None and new_temp != old_temp:
                if self._current_global_preset not in self._presets:
                    self._presets[self._current_global_preset] = {}
                if entity_id not in self._presets[self._current_global_preset]:
                    self._presets[self._current_global_preset][entity_id] = {}
                self._presets[self._current_global_preset][entity_id]["target_temp"] = new_temp
                self.hass.async_create_task(self._async_save_presets_storage())

        zone = self._get_zone(entity_id)
        if zone and zone.get(CONF_ZONE_TRV_SYNC, False):
            self.hass.async_create_task(
                self._async_sync_trv_preset(entity_id, new_state.state)
            )

    @callback
    def _async_on_window_state_changed(self, event: Event) -> None:
        sensor_id = event.data.get("entity_id")
        new_state = event.data.get("new_state")
        if new_state is None:
            return
        matching_zones = [z for z in self.zones if z.get(CONF_ZONE_WINDOW_SENSOR) == sensor_id]
        if not matching_zones:
            return
        for zone in matching_zones:
            climate_id = zone[CONF_ZONE_CLIMATE]
            zone_select = self._select_entities.get(f"zone_mode_{climate_id}")
            if new_state.state == "on":
                if climate_id not in self._pre_window_state:
                    current_mode = self.get_zone_mode(climate_id)
                    self._pre_window_state[climate_id] = current_mode
                    self.hass.async_create_task(self._async_save_storage())
                if zone_select and self.get_zone_mode(climate_id) != ZONE_MODE_BYPASS:
                    self.hass.async_create_task(zone_select.async_select_option(ZONE_MODE_BYPASS))
                self.notify_window_state(climate_id, True)
            elif new_state.state == "off":
                if climate_id in self._pre_window_state:
                    was_mode = self._pre_window_state.pop(climate_id)
                    self.hass.async_create_task(self._async_save_storage())
                    if zone_select and self.get_zone_mode(climate_id) != was_mode:
                        self.hass.async_create_task(zone_select.async_select_option(was_mode))
                self.notify_window_state(climate_id, False)

    @callback
    def _async_on_presence_changed(self, event: Event) -> None:
        if not self.get_persistent_data(KEY_GEOFENCING_TOGGLE, True):
            return
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        if new_state is None or old_state is None:
            return
        old_val = old_state.state
        new_val = new_state.state
        if old_val == new_val:
            return
        is_away = new_val in ("0", "not_home", "off")
        was_away = old_val in ("0", "not_home", "off")
        if is_away and not was_away:
            self.hass.async_create_task(self.async_set_persistent_data(KEY_PRE_AWAY_PRESET, self._current_global_preset))
            self.hass.async_create_task(self.async_set_global_preset(GLOBAL_PRESET_AWAY))
        elif not is_away and was_away:
            night_time_str = self.get_persistent_data(KEY_NIGHT_TIME, "22:30")
            morning_time_str = self.get_persistent_data(KEY_MORNING_TIME, "07:00")
            is_night = False
            try:
                n_hour, n_minute = map(int, night_time_str.split(":"))
                m_hour, m_minute = map(int, morning_time_str.split(":"))
                now = dt_util.now().time()
                night_time = dt_time(n_hour, n_minute)
                morning_time = dt_time(m_hour, m_minute)
                if night_time > morning_time:
                    if now >= night_time or now <= morning_time:
                        is_night = True
                else:
                    if now >= night_time and now <= morning_time:
                        is_night = True
            except Exception:
                pass
            if is_night:
                pre_away = self.get_persistent_data(KEY_PRE_AWAY_PRESET, GLOBAL_PRESET_COMFORT)
                self.hass.async_create_task(self.async_set_persistent_data(KEY_PRE_NIGHT_PRESET, pre_away))
                self.hass.async_create_task(self.async_set_global_preset(GLOBAL_PRESET_SLEEP))
            else:
                pre_away = self.get_persistent_data(KEY_PRE_AWAY_PRESET, GLOBAL_PRESET_COMFORT)
                self.hass.async_create_task(self.async_set_global_preset(pre_away))

    @callback
    def _async_check_schedule(self, now: datetime) -> None:
        if not self.get_persistent_data(KEY_AUTO_NIGHT_MODE, False):
            return
        local_now = dt_util.now()
        current_date = local_now.date()
        night_time_str = self.get_persistent_data(KEY_NIGHT_TIME, "22:30")
        try:
            hour, minute = map(int, night_time_str.split(":"))
            if local_now.hour == hour and local_now.minute == minute:
                if self._last_night_trigger_date != current_date:
                    self._last_night_trigger_date = current_date
                    if self._current_global_preset != GLOBAL_PRESET_SLEEP:
                        self.hass.async_create_task(self.async_set_persistent_data(KEY_PRE_NIGHT_PRESET, self._current_global_preset))
                        self.hass.async_create_task(self.async_set_global_preset(GLOBAL_PRESET_SLEEP))
        except Exception:
            pass
        morning_time_str = self.get_persistent_data(KEY_MORNING_TIME, "07:00")
        try:
            hour, minute = map(int, morning_time_str.split(":"))
            if local_now.hour == hour and local_now.minute == minute:
                if self._last_morning_trigger_date != current_date:
                    self._last_morning_trigger_date = current_date
                    pre_night = self.get_persistent_data(KEY_PRE_NIGHT_PRESET, GLOBAL_PRESET_COMFORT)
                    self.hass.async_create_task(self.async_set_global_preset(pre_night))
        except Exception:
            pass

    def _get_zone(self, climate_entity: str) -> dict | None:
        for zone in self.zones:
            if zone[CONF_ZONE_CLIMATE] == climate_entity:
                return zone
        return None

    def set_min_cycle_on(self, value: int) -> None:
        self._min_cycle_on = value
        self._boiler_pwm.set_params(min_on=value * 60)

    def set_min_cycle_off(self, value: int) -> None:
        self._min_cycle_off = value
        self._boiler_pwm.set_params(min_off=value * 60)

    def set_valve_delay(self, value: int) -> None:
        self._valve_delay = value

    async def _async_pwm_tick(self, now: datetime) -> None:
        if not self._master_state or self._anti_seize_running:
            return
        peak_demand = 0.0
        for zone in self.zones:
            climate_id = zone[CONF_ZONE_CLIMATE]
            mode = self.get_zone_mode(climate_id)
            demand = self.get_zone_demand(climate_id)
            # Учитываем спрос от Bypass-зон только если он >0 (защита от заморозки)
            if mode == ZONE_MODE_BYPASS and demand == 0:
                continue
            if mode == ZONE_MODE_PRIMARY and demand > peak_demand:
                peak_demand = demand
            elif mode == ZONE_MODE_BYPASS and demand > 0 and demand > peak_demand:
                peak_demand = demand  # приоритет защиты
        wanted_state = self._boiler_pwm.calculate(peak_demand)
        boiler_state = self.hass.states.get(self.boiler_switch)
        current_boiler_on = boiler_state is not None and boiler_state.state == STATE_ON
        if current_boiler_on or peak_demand > 0:
            self._last_active_time = time.time()
        if wanted_state and not current_boiler_on:
            time_since_change = time.time() - self._last_boiler_change
            min_off_sec = self._min_cycle_off * 60
            if time_since_change < min_off_sec:
                return
            if self._valve_delay > 0:
                if not self._pending_boiler_task:
                    self._schedule_boiler_check(self._valve_delay, True)
            else:
                await self._force_boiler_on()
        elif not wanted_state:
            time_since_change = time.time() - self._last_boiler_change
            min_on_sec = self._min_cycle_on * 60
            if current_boiler_on and time_since_change < min_on_sec:
                return
            if self._pending_boiler_task:
                self._pending_boiler_task.cancel()
                self._pending_boiler_task = None
            if current_boiler_on:
                await self._force_boiler_off()

    async def _async_update_boiler(self, emergency_off: bool = False) -> None:
        if emergency_off:
            if self._pending_boiler_task:
                self._pending_boiler_task.cancel()
                self._pending_boiler_task = None
            await self._force_boiler_off()

    def _schedule_boiler_check(self, delay_seconds: float, turn_on: bool) -> None:
        if self._pending_boiler_task:
            self._pending_boiler_task.cancel()
        async def _delayed_check():
            try:
                await asyncio.sleep(delay_seconds)
                if turn_on:
                    await self._force_boiler_on()
                else:
                    await self._force_boiler_off()
            except asyncio.CancelledError:
                pass
        self._pending_boiler_task = self.hass.async_create_task(_delayed_check())

    async def _force_boiler_on(self) -> None:
        if self._pending_boiler_task:
            self._pending_boiler_task.cancel()
            self._pending_boiler_task = None
        await self.hass.services.async_call(
            "switch",
            SERVICE_TURN_ON,
            {ATTR_ENTITY_ID: self.boiler_switch},
            blocking=False,
        )
        self._last_boiler_change = time.time()

    async def _force_boiler_off(self) -> None:
        if self._pending_boiler_task:
            self._pending_boiler_task.cancel()
            self._pending_boiler_task = None
        await self.hass.services.async_call(
            "switch",
            SERVICE_TURN_OFF,
            {ATTR_ENTITY_ID: self.boiler_switch},
            blocking=False,
        )
        self._last_boiler_change = time.time()

    async def _async_sync_trv_preset(self, climate_entity: str, hvac_mode: str) -> None:
        if hvac_mode == HVAC_MODE_HEAT:
            preset = PRESET_MANUAL
        elif hvac_mode == HVAC_MODE_OFF:
            preset = PRESET_OFF
        else:
            return
        state = self.hass.states.get(climate_entity)
        if state is None:
            return
        supported_features = state.attributes.get("supported_features", 0)
        if not (supported_features & ClimateEntityFeature.PRESET_MODE):
            return
        current_preset = state.attributes.get(ATTR_PRESET_MODE)
        if current_preset == preset:
            return
        await self.hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_PRESET_MODE,
            {ATTR_ENTITY_ID: climate_entity, ATTR_PRESET_MODE: preset},
            blocking=False,
        )

    async def async_apply_master_on(self) -> None:
        for zone in self.zones:
            climate_id = zone[CONF_ZONE_CLIMATE]
            mode = self._zone_modes.get(climate_id, ZONE_MODE_PRIMARY)
            if mode != ZONE_MODE_BYPASS:
                await self._async_set_hvac_mode(climate_id, HVAC_MODE_HEAT)
            else:
                await self._async_set_hvac_mode(climate_id, HVAC_MODE_OFF)

    async def async_apply_master_off(self) -> None:
        for zone in self.zones:
            climate_id = zone[CONF_ZONE_CLIMATE]
            await self._async_set_hvac_mode(climate_id, HVAC_MODE_OFF)
        await self._async_update_boiler(emergency_off=True)

    async def _async_set_hvac_mode(self, climate_entity: str, mode: str) -> None:
        state = self.hass.states.get(climate_entity)
        if state is None:
            return
        current_mode = state.state
        if current_mode == mode:
            return
        await self.hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_HVAC_MODE,
            {ATTR_ENTITY_ID: climate_entity, ATTR_HVAC_MODE: mode},
            blocking=False,
        )

    @callback
    def _async_check_anti_seize(self, now: datetime) -> None:
        if not self.get_persistent_data(KEY_ANTI_SEIZE_ENABLED, False):
            return
        if self._anti_seize_running:
            return
        idle_seconds = time.time() - self._last_active_time
        idle_days = idle_seconds / 86400.0
        anti_seize_idle_days = self.get_persistent_data(KEY_ANTI_SEIZE_IDLE_DAYS, 15)
        if idle_days >= anti_seize_idle_days:
            self.hass.async_create_task(self._async_execute_anti_seize())

    async def _async_execute_anti_seize(self) -> None:
        self._anti_seize_running = True
        try:
            self._pre_anti_seize_state.clear()
            zones_to_open = []
            for zone in self.zones:
                climate_id = zone[CONF_ZONE_CLIMATE]
                if not zone.get(CONF_ZONE_ANTI_SEIZE, True):
                    continue
                state = self.hass.states.get(climate_id)
                if state:
                    self._pre_anti_seize_state[climate_id] = state.state
                    zones_to_open.append(climate_id)
            if not zones_to_open:
                self._last_active_time = time.time()
                return
            for climate_id in zones_to_open:
                await self._async_set_hvac_mode(climate_id, HVAC_MODE_HEAT)
            if self._valve_delay > 0:
                await asyncio.sleep(self._valve_delay)
            anti_seize_boiler = self.get_persistent_data(CONF_ANTI_SEIZE_BOILER, False)
            if anti_seize_boiler:
                await self.hass.services.async_call(
                    "switch",
                    SERVICE_TURN_ON,
                    {ATTR_ENTITY_ID: self.boiler_switch},
                    blocking=False,
                )
            anti_seize_duration = self.get_persistent_data(KEY_ANTI_SEIZE_DURATION, 2)
            await asyncio.sleep(anti_seize_duration * 60)
            if anti_seize_boiler:
                await self.hass.services.async_call(
                    "switch",
                    SERVICE_TURN_OFF,
                    {ATTR_ENTITY_ID: self.boiler_switch},
                    blocking=False,
                )
            for climate_id, old_state in self._pre_anti_seize_state.items():
                await self._async_set_hvac_mode(climate_id, old_state)
            self._last_active_time = time.time()
        except Exception as e:
            _LOGGER.error("Error during anti-seize routine: %s", e)
        finally:
            self._anti_seize_running = False
