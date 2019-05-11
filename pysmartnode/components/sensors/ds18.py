'''
Created on 2018-06-25

@author: Kevin Köck
'''

"""
example config:
# DS18 general controller:
{
    package: .sensors.ds18
    component: DS18_Controller
    constructor_args: {
        pin: 5                    # pin number or label (on NodeMCU)
        # interval: 600           # optional, defaults to 600. controller reads units in this interval
        # auto_discovery: False   # optional, if True then one object for each found DS18 Unit will be created. Only use this for reading all sensors.
    }
}
# Controller doesn't publish discovery messages and only reads configured DS18 units.

{
    package: .sensors.ds18
    component: DS18
    constructor_args: {
        rom: "28FF016664160383"   # ROM of the specific DS18 unit, can be string or bytearray (in json bytearray not possible)
        # controller: "ds18"      # optional, name of controller instance. Not needed if only one instance created)
        # precision_temp: 2       # precision of the temperature value published
        # offset_temp: 0          # offset for temperature to compensate bad sensor reading offsets
        # mqtt_topic: sometopic   # optional, defaults to home/<controller-id>/DS18/<ROM> 
        # friendly_name: null     # optional, friendly name shown in homeassistant gui with mqtt discovery
    }
}
# Specific DS18 unit. 
"""

__updated__ = "2019-05-11"
__version__ = "2.2"

from pysmartnode import config
from pysmartnode import logging
import uasyncio as asyncio
import gc
from pysmartnode.components.machine.pin import Pin
from pysmartnode.utils.component import Component, DISCOVERY_SENSOR

####################
# import your library here
import ds18x20
import onewire

# choose a component name that will be used for logging (not in leightweight_log) and
# a default mqtt topic that can be changed by received or local component configuration
_component_name = "DS18"
# define the type of the component according to the homeassistant specifications
_component_type = "sensor"
####################

_log = logging.getLogger(_component_name)
_mqtt = config.getMQTT()
gc.collect()

_ds18_controller = None
_instances = []


class DS18_Controller(ds18x20.DS18X20):
    def __init__(self, pin, interval=None, auto_discovery=False):
        """
        The DS18 onewire controller. Reads all connected (and configured) units.
        :param pin: Pin object or integer or name
        :param interval: how often the sensors are read and published
        :param auto_discovery: if True then one object for each found DS18 unit will be created. This is only useful if
        the Units are not going to be used in other components and only the read temperature is interesting.
        """
        self._interval = interval or config.INTERVAL_SEND_SENSOR
        ds18x20.DS18X20.__init__(self, onewire.OneWire(Pin(pin)))
        gc.collect()
        self._lock = config.Lock()
        global _ds18_controller
        _ds18_controller = self
        asyncio.get_event_loop().create_task(self._loop(auto_discovery))

    async def _loop(self, auto_discovery=False):
        if auto_discovery is True:
            roms = []
            for _ in range(4):
                roms_n = self.scan()
                for rom in roms_n:
                    if rom not in roms:
                        roms.append(rom)
                await asyncio.sleep_ms(100)
            for rom in roms:
                DS18(rom)
                await asyncio.sleep_ms(100)  # give discovery time to publish
        interval = self._interval
        await asyncio.sleep(1)
        while True:
            async with self._lock:
                await asyncio.sleep_ms(100)  # just in case lock has been released before single sensor has been read
                self.convert_temp()  # This way all sensors convert temp only once instead of for every sensor
                await asyncio.sleep_ms(750)
                for ds in _instances:
                    await ds.temperature(single_sensor=False)
            await asyncio.sleep(interval)

    async def read(self, rom: bytearray, prec: int, offs: float, publish=True, single_sensor=True) -> float:
        # Won't scan for available sensors. Missing or defective ones are recognized when reading temperature
        if single_sensor:
            async with self._lock:
                self.convert_temp()
                await asyncio.sleep_ms(750)
        value = None
        err = None
        for _ in range(3):
            try:
                value = self.read_temp(rom)
            except Exception as e:
                await asyncio.sleep_ms(100)
                err = e
                continue
        if value is None:
            await _log.asyncLog("error", "Sensor rom {!s} got no value, {!s}".format(rom, err))
        if value is not None:
            if value == 85.0:
                await _log.asyncLog("error", "Sensor rom {!s} got value 85.00 [not working correctly]".format(rom))
                value = None
            try:
                value = round(value, prec)
                value += offs
            except Exception as e:
                await _log.asyncLog("error", "Error rounding value {!s} of rom {!s}".format(value, rom))
                value = None
        if publish:
            if value is not None:
                await _mqtt.publish("{!s}/{!s}".format(self._topic, self.rom2str(rom)),
                                    ("{0:." + str(prec) + "f}").format(value))
        return value

    @staticmethod
    def rom2str(rom: bytearray) -> str:
        return ''.join('%02X' % i for i in iter(rom))

    @staticmethod
    def str2rom(rom: str) -> bytearray:
        a = bytearray(8)
        for i in range(8):
            a[i] = int(rom[i * 2:i * 2 + 2], 16)
        return a


class DS18(Component):
    """
    Helping class to use a singluar DS18 unit.
    This is not a full component object in terms of mqtt and discovery. This is handled by the controller.
    It can be used as a temperature component object.
    """

    def __init__(self, rom, precision_temp=2, offset_temp=0, mqtt_topic=None, friendly_name=None,
                 controller: DS18_Controller = None):
        """
        Class for a single ds18 unit to provide an interface to a single unit not needing to specify
        the ROM on temperature read calls.
        :param rom: str or bytearray, device specific ROM
        :param controller: DS18 object. If ds18 are connected on different pins, different DS18 objects are needed
        """
        super().__init__()
        if controller is None:
            global _ds18_controller
            if _ds18_controller is None:
                raise TypeError("No DS18 object, create the onewire ds18 controller instance first")
            self._ds = _ds18_controller
        else:
            self._ds = controller
        self._r = rom if type(rom) == bytearray else self._ds.str2rom(rom)
        self._topic = mqtt_topic  # can be None instead of default to save RAM
        self._frn = friendly_name
        _instances.append(self)

        ##############################
        # adapt to your sensor by extending/removing unneeded values like in the constructor arguments
        self._prec_temp = int(precision_temp)
        ###
        self._offs_temp = float(offset_temp)
        ##############################

    async def _discovery(self):
        # not scanning for available roms
        sens = DISCOVERY_SENSOR.format("temperature",  # device_class
                                       "°C",  # unit_of_measurement
                                       "{{ value|float }}")  # value_template
        rom = self._ds.rom2str(self._r)
        topic = self._topic or _mqtt.getDeviceTopic("DS18/{!s}".format(rom))
        name = "{!s}_{!s}".format(_component_name, rom)
        await self._publishDiscovery(_component_type, topic, name, sens, self._frn or "Temperature")
        del rom, topic, name, sens
        gc.collect()

    def __str__(self):
        return "DS18({!s})".format(self._ds.rom2str(self._r))

    __repr__ = __str__

    async def temperature(self, publish=True, single_sensor=True) -> float:
        """
        Read temperature of DS18 unit
        :param publish: bool, publish the read value
        :param single_sensor: only used by the controller to optimize reading of multiple sensors.
        :return: float
        """
        return await self._ds.read(self._r, self._prec_temp, self._offs_temp, publish, single_sensor)
