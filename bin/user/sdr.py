#!/usr/bin/env python
# Copyright 2016-2024 Matthew Wall
# Distributed under the terms of the GNU Public License (GPLv3)
"""
Collect data from stl-sdr.  Run rtl_433 on a thread and push the output onto
a queue.

The SDR detects many different sensors and sensor types, so this driver
includes a mechanism to filter the incoming data, and to map the filtered
data onto the weewx database schema and identify the type of data from each
sensor.

Sensors are filtered based on a tuple that identifies uniquely each sensor.
A tuple consists of the observation name, a unique identifier for the hardware,
and the packet type, separated by periods:

  <observation_name>.<hardware_id>.<packet_type>

The filter and data types are specified in a sensor_map stanza in the driver
stanza.  For example:

[SDR]
    driver = user.sdr
    [[sensor_map]]
        inTemp = temperature.25A6.AcuriteTowerPacket
        outTemp = temperature.24A4.AcuriteTowerPacket
        rain_total = rain_total.A52B.Acurite5n1Packet

If no sensor_map is specified, no data will be collected.

The deltas stanza indicates which observations are cumulative measures and
how they should be split into delta measures.

[SDR]
    ...
    [[deltas]]
        rain = rain_total

In this case, the value for rain will be a delta calculated from sequential
rain_total observations.

To identify sensors, run the driver directly.  Alternatively, use the options
log_unknown_sensors and log_unmapped_sensors to see data from the SDR that are
not yet recognized by your configuration.

[SDR]
    driver = user.sdr
    log_unknown_sensors = True
    log_unmapped_sensors = True

The default for each of these is False.

In addition, there are two options to more granularly log debugging
information that can be helpful in bringing up a new system that may not
have sensor models known to this driver.

    log_packets            = False
    log_duplicate_readings = False

The default for these is True.  When debug=1, the default behavior is to
log the assembled packets as well as any duplicate received readings.  Many
sensors send duplicate messages that rtl_433 will read.  Setting these False
will suppress those messages from being logged if you otherwise want to see
normal debug=1 messages.

Eventually we would prefer to have all rtl_433 output as json.  Unfortunately,
many of the rtl_433 decoders do not emit this format yet (as of January 2017).
So this driver is designed to look for json first, then fall back to single-
or multi-line plain text format.

Battery Status

In the weewx database, a battery status of 1 indicates low battery.  This has
origins in the original battery indicators from davis vantage stations.  Some
devices report 'battery' where a value of 1 indicates that the battery is ok,
i.e., the battery is *not* low.  The rtl_433 output has been changed recently
to make this less ambiguous, so many devices now report 'battery_ok' instead
of just 'battery'.  There were also cases where the 'battery' value was a
string, typically just 'OK'.  FWIW, user fgonza2 reports that the Acurite low
battery indicator kicks in when the voltage hits about 4V.

WARNING: Handling of units and unit systems in rtl_433 is a mess, but it is
getting better.  Although there is an option to request SI units, there is no
indicate in the decoder output whether that option is respected, nor does
rtl_433 specify exactly which SI units are used for various types of measure.
There seems to be a pattern of appending a unit label to the observation name
in the JSON data, for example 'wind_speed_mph' instead of just 'wind_speed'.
"""

# FIXME: deprecate then eliminate the V2 acurite packets - single packet def
# can recognize anything rtl_433 spits out

from __future__ import with_statement
from calendar import timegm
try:
    # Python 3
    import queue
except ImportError:
    # Python 2:
    import Queue as queue
import fnmatch
import os
import re
import subprocess
import threading
import time
import copy

try:
    import cjson as json
    setattr(json, 'dumps', json.encode)
    setattr(json, 'loads', json.decode)
except (ImportError, AttributeError):
    try:
        import simplejson as json
    except ImportError:
        import json

import weewx.drivers
import weewx.units
from weeutil.weeutil import tobool

try:
    # New-style weewx logging
    import weeutil.logger
    import logging
    log = logging.getLogger(__name__)

    def logdbg(msg):
        log.debug(msg)

    def loginf(msg):
        log.info(msg)

    def logerr(msg):
        log.error(msg)

except ImportError:
    # Old-style weewx logging
    import syslog

    def logmsg(level, msg):
        syslog.syslog(level, 'sdr: %s: %s' %
                      (threading.currentThread().getName(), msg))

    def logdbg(msg):
        logmsg(syslog.LOG_DEBUG, msg)

    def loginf(msg):
        logmsg(syslog.LOG_INFO, msg)

    def logerr(msg):
        logmsg(syslog.LOG_ERR, msg)

DRIVER_NAME = 'SDR'
DRIVER_VERSION = '0.96b1'

# The default command requests json output from every decoder
# Use the -R option to indicate specific decoders

# -q      - suppress non-data messages (for older versions of rtl_433)
# -M utc  - print timestamps in UTC (-U for older versions of rtl_433)
# -F json - emit data in json format (not all rtl_433 decoders support this)
# -G      - emit data for all rtl decoders (only available in newer rtl_433)
#           as of early 2020, the syntax is '-G4', but use only for testing

# very old implmentations:
#DEFAULT_CMD = 'rtl_433 -q -U -F json -G'
# as of dec2018:
#DEFAULT_CMD = 'rtl_433 -M utc -F json -G'
# as of feb2020:
DEFAULT_CMD = 'rtl_433 -M utc -F json'

def loader(config_dict, _):
    return SDRDriver(**config_dict[DRIVER_NAME])

def confeditor_loader():
    return SDRConfigurationEditor()


# utilities for inline unit conversions.  respect the None!
def to_F(v):
    if v is not None:
        v  = v * 1.8 + 32
    return v

def to_C(v):
    if v is not None:
        v  = 5 / 9 * (v - 32)
    return v

def to_mph(v):
    if v is not None:
        v *= 0.621371
    return v

def to_in(v):
    if v is not None:
        v /= 25.4
    return v

def to_v(v):
    if v is not None:
        v /= 1000
    return v

def kmh_to_mps(v):
    if v is not None:
        v /= 3.6
    return v


class AsyncReader(threading.Thread):

    def __init__(self, fd, queue, label):
        threading.Thread.__init__(self)
        self._fd = fd
        self._queue = queue
        self._running = False
        self.setDaemon(True)
        self.setName(label)

    def run(self):
        logdbg("start async reader for %s" % self.getName())
        self._running = True
        for line in iter(self._fd.readline, ''):
            if line:
                self._queue.put(line)
            if not self._running:
                break

    def stop_running(self):
        self._running = False


class ProcManager(object):
    TS = re.compile('^\d\d\d\d-\d\d-\d\d \d\d:\d\d:\d\d[\s]+')

    def __init__(self):
        self._cmd = None
        self._process = None
        self.stdout_queue = queue.Queue()
        self.stdout_reader = None
        self.stderr_queue = queue.Queue()
        self.stderr_reader = None

    def startup(self, cmd, path=None, ld_library_path=None):
        self._cmd = cmd
        loginf("startup process '%s'" % self._cmd)
        env = os.environ.copy()
        if path:
            env['PATH'] = path + ':' + env['PATH']
        if ld_library_path:
            env['LD_LIBRARY_PATH'] = ld_library_path
        try:
            self._process = subprocess.Popen(cmd.split(' '),
                                             env=env,
                                             stdout=subprocess.PIPE,
                                             stderr=subprocess.PIPE)
            self.stdout_reader = AsyncReader(
                self._process.stdout, self.stdout_queue, 'stdout-thread')
            self.stdout_reader.start()
            self.stderr_reader = AsyncReader(
                self._process.stderr, self.stderr_queue, 'stderr-thread')
            self.stderr_reader.start()
        except (OSError, ValueError) as e:
            raise weewx.WeeWxIOError("failed to start process '%s': %s" %
                                     (cmd, e))

    def shutdown(self):
        loginf('shutdown process %s' % self._cmd)
        self._process.kill()
        logdbg("close stdout")
        self._process.stdout.close()
        logdbg("close stderr")
        self._process.stderr.close()
        logdbg('shutdown %s' % self.stdout_reader.getName())
        self.stdout_reader.stop_running()
        self.stdout_reader.join(0.5)
        logdbg('shutdown %s' % self.stderr_reader.getName())
        self.stderr_reader.stop_running()
        self.stderr_reader.join(0.5)
        if self._process.poll() is None:
            logerr('process did not respond to kill, shutting down anyway')
        self._process = None
        if self.stdout_reader.is_alive():
            loginf('timed out waiting for %s' % self.stdout_reader.getName())
        self.stdout_reader = None
        if self.stderr_reader.is_alive():
            loginf('timed out waiting for %s' % self.stderr_reader.getName())
        self.stderr_reader = None
        loginf('shutdown complete')

    def running(self):
        return self._process.poll() is None

    def get_stderr(self):
        lines = []
        while not self.stderr_queue.empty():
            lines.append(self.stderr_queue.get().decode())
        return lines

    def get_stdout(self):
        lines = []
        while self.running():
            try:
                # Fetch the output line. For it to be searched, Python 3
                # requires that it be decoded to unicode. Decoding does no
                # harm under Python 2:
                line = self.stdout_queue.get(True, 3).decode()
                m = ProcManager.TS.search(line)
                if m and lines:
                    yield lines
                    lines = []
                lines.append(line)
            except queue.Empty:
                yield lines
                lines = []
        yield lines


class Packet:

    def __init__(self):
        pass

    @staticmethod
    def parse_text(ts, payload, lines):
        return None

    @staticmethod
    def parse_json(obj):
        return None

    TS_PATTERN = re.compile('(\d\d\d\d-\d\d-\d\d \d\d:\d\d:\d\d)')

    @staticmethod
    def parse_time(line):
        ts = None
        try:
            m = Packet.TS_PATTERN.search(line)
            if m:
                utc = time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                ts = timegm(utc)
        except Exception as e:
            logerr("parse timestamp failed for '%s': %s" % (line, e))
        return ts

    @staticmethod
    def get_float(obj, key_):
        if key_ in obj:
            try:
                return float(obj[key_])
            except ValueError:
                pass
        return None

    @staticmethod
    def get_int(obj, key_):
        if key_ in obj:
            try:
                return int(obj[key_])
            except ValueError:
                pass
        return None

    @staticmethod
    def get_battery(obj):
        # deal with boolean battery status indicators.  older rtl_433 used a
        # field called 'battery' with string 'OK'.  later rtl_433 uses a field
        # called 'battery_ok' with value 1 to indicate battery is ok and 0 to
        # indicate battery is not ok.  at one point rtl_433 was emitting a
        # field called 'battery_low', so check for that too.
        #
        # for some sensors we see 'battery_ok' with a floating value.  not sure
        # what this means.
        #
        # map these to the WeeWX convention of boolean battery indicators of
        # 0=OK 1=notOK
        bs = None
        if 'battery_ok' in obj:
            level = Packet.get_float(obj, 'battery_ok')
            if level is not None:
                bs = 0 if level >= 1.0 else 1
        elif 'battery' in obj:
            bs = 0 if obj.get('battery') == 'OK' else 1
        elif 'battery_low' in obj:
            bs = Packet.get_int(obj, 'battery_low')
        return bs

    @staticmethod
    def parse_lines(lines, parseinfo=None):
        # parse each line, splitting on colon for name:value
        # tuple in parseinfo is label, pattern, lambda
        # if there is a label, use it to transform the name
        # if there is a pattern, use it to match the value
        # if there is a lamba, use it to convert the value
        if parseinfo is None:
            parseinfo = dict()
        packet = dict()
        for line in lines[1:]:
            if line.count(':') == 1:
                try:
                    (name, value) = [x.strip() for x in line.split(':')]
                    if name in parseinfo:
                        if parseinfo[name][1]:
                            m = parseinfo[name][1].search(value)
                            if m:
                                value = m.group(1)
                            else:
                                logdbg("regex failed for %s:'%s'" %
                                       (name, value))
                        if parseinfo[name][2]:
                            value = parseinfo[name][2](value)
                        if parseinfo[name][0]:
                            name = parseinfo[name][0]
                        packet[name] = value
                    else:
                        logdbg("ignoring %s:%s" % (name, value))
                except Exception as e:
                    logerr("parse failed for line '%s': %s" % (line, e))
            else:
                logdbg("skip line '%s'" % line)
        while lines:
            lines.pop(0)
        return packet

    @staticmethod
    def add_identifiers(pkt, sensor_id='', packet_type=''):
        # qualify each field name with details about the sensor.  not every
        # sensor has all three fields.
        # observation.<sensor_id>.<packet_type>
        packet = dict()
        if 'dateTime' in pkt:
            packet['dateTime'] = pkt.pop('dateTime', 0)
        if 'usUnits' in pkt:
            packet['usUnits'] = pkt.pop('usUnits', 0)
        for n in pkt:
            packet["%s.%s.%s" % (n, sensor_id, packet_type)] = pkt[n]
        return packet


class Acurite(object):
    @staticmethod
    def insert_ids(pkt, pkt_type):
        # there should be a sensor_id field in the packet to identify sensor.
        # ensure the sensor_id is upper-case - it should be 4 hex characters.
        sensor_id = str(pkt.pop('hardware_id', '0000')).upper()
        return Packet.add_identifiers(pkt, sensor_id, pkt_type)


class AcuriteAtlasPacket(Packet):
    # {"time": "2019-12-14 16:56:57", "model": "Acurite-Atlas", "id": 896, "channel": "A", "sequence_num": 0, "battery_ok": 1, "message_type": 37, "wind_avg_mi_h": 5.000, "temperature_F": 40.000, "humidity": 76, "byte8": 0, "byte9": 37, "byte89": 37}
    # {"time": "2019-12-14 16:57:07", "model": "Acurite-Atlas", "id": 896, "channel": "A", "sequence_num": 0, "battery_ok": 1, "message_type": 38, "wind_avg_mi_h": 6.000, "wind_dir_deg": 291.000, "rain_in": 0.290, "byte8": 0, "byte9": 37, "byte89": 37}}
    # {"time": "2019-12-14 16:57:58", "model": "Acurite-Atlas", "id": 896, "channel": "A", "sequence_num": 0, "battery_ok": 1, "message_type": 39, "wind_avg_mi_h": 6.000, "uv": 0, "lux": 22900, "byte8": 0, "byte9": 37, "byte89": 37}

    # for battery, 0 means OK (assuming that 1 for battery_ok means OK)
    # message types: 37, 38, 39
    #   37: wind_avg_mi_h, temperature_F, humidity
    #   38: wind_avg_mi_h, wind_dir_deg, rain_in
    #   39: wind_avg_mi_h, uv, lux

    IDENTIFIER = "Acurite-Atlas"

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['usUnits'] = weewx.US
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['model'] = obj.get('model')
        pkt['hardware_id'] = "%04x" % obj.get('id', 0)
        pkt['channel'] = obj.get('channel')
        pkt['sequence_num'] = Packet.get_int(obj, 'sequence_num')
        pkt['message_type'] = Packet.get_int(obj, 'message_type')
        if 'humidity' in obj:
            pkt['humidity'] = Packet.get_float(obj, 'humidity')
        if 'temperature_F' in obj:
            pkt['temperature'] = Packet.get_float(obj, 'temperature_F')
        elif 'temperature_C' in obj:
            pkt['temperature'] = to_F(Packet.get_float(obj, 'temperature_C'))
        if 'wind_avg_mi_h' in obj:
            pkt['wind_speed'] = Packet.get_float(obj, 'wind_avg_mi_h')
        elif 'wind_avg_km_h' in obj:
            pkt['wind_speed'] = to_mph(Packet.get_float(obj, 'wind_avg_km_h'))
        if 'wind_dir_deg' in obj:
            pkt['wind_dir'] = Packet.get_float(obj, 'wind_dir_deg')
        if 'rain_in' in obj:
            pkt['rain_total'] = Packet.get_float(obj, 'rain_in')
        elif 'rain_mm' in obj:
            pkt['rain_total'] = to_in(Packet.get_float(obj, 'rain_mm'))
        if 'uv' in obj:
            pkt['uv'] = Packet.get_int(obj, 'uv')
        if 'lux' in obj:
            pkt['lux'] = Packet.get_int(obj, 'lux')
        if 'strike_count' in obj:
            pkt['strike_count'] = Packet.get_int(obj, 'strike_count')
        if 'strike_distance' in obj:
            pkt['strike_distance'] = Packet.get_int(obj, 'strike_distance')
        if 'snr' in obj:
            pkt['snr'] = obj.get('snr')
        if 'rssi' in obj:
            pkt['rssi'] = obj.get('rssi')
        if 'noise' in obj:
            pkt['noise'] = obj.get('noise')
        pkt['battery'] = Packet.get_battery(obj)
        return Acurite.insert_ids(pkt, AcuriteAtlasPacket.__name__)


class Acurite3n1PacketV2(Packet):
    # sample json output from rtl_433
    # {"time" : "2021-12-27 02:53:38", "model" : "Acurite-3n1", "subtype" : 32, "id" : 7220, "channel" : "B", "sequence_num" : 1, "battery_ok" : 1, "wind_avg_mi_h" : 5.000, "temperature_F" : 5.100, "humidity" : 65, "mic" : "CHECKSUM"}

    IDENTIFIER = "Acurite-3n1"

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['usUnits'] = weewx.US
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['model'] = obj.get('model')
        pkt['hardware_id'] = "%04x" % obj.get('id', 0)
        pkt['channel'] = obj.get('channel')
        pkt['sequence_num'] = Packet.get_int(obj, 'sequence_num')
        pkt['battery'] = Packet.get_battery(obj)
        if 'subtype' in obj:
            pkt['msg_type'] = Packet.get_int(obj, 'subtype')
        elif 'message_type' in obj:
            pkt['msg_type'] = Packet.get_int(obj, 'message_type')
        if 'humidity' in obj:
            pkt['humidity'] = Packet.get_float(obj, 'humidity')
        if 'temperature_F' in obj:
            pkt['temperature'] = Packet.get_float(obj, 'temperature_F')
        elif 'temperature_C' in obj:
            pkt['temperature'] = to_F(Packet.get_float(obj, 'temperature_C'))
        if 'wind_avg_mi_h' in obj:
            pkt['wind_speed'] = Packet.get_float(obj, 'wind_avg_mi_h')
        elif 'wind_avg_km_h' in obj:
            pkt['wind_speed'] = to_mph(Packet.get_float(obj, 'wind_avg_km_h'))
        return Acurite.insert_ids(pkt, Acurite3n1PacketV2.__name__)


class Acurite5n1Packet(Packet):
    # 2016-08-31 16:41:39 Acurite 5n1 sensor 0x0BFA Ch C, Msg 31, Wind 15 kmph / 9.3 mph 270.0^ W (3), rain gauge 0.00 in
    # 2016-08-30 23:57:25 Acurite 5n1 sensor 0x0BFA Ch C, Msg 38, Wind 2 kmph / 1.2 mph, 21.3 C 70.3 F 70 % RH
    # 2016-09-27 17:09:34 Acurite 5n1 sensor 0x062C Ch A, Total rain fall since last reset: 2.00
    #
    # the 'rain fall since last reset' seems to be emitted once when rtl_433
    # starts up, then never again.  the rain measure in the type 31 messages
    # is a cumulative value, but not the same as rain since last reset.
    #
    # rtl_433 keeps using different labels and calculations for the rain
    # counter, so try to deal with the variants we have seen.

    IDENTIFIER = "Acurite 5n1 sensor"
    PATTERN = re.compile('0x([0-9a-fA-F]+) Ch ([A-C]), (.*)')
    RAIN = re.compile('Total rain fall since last reset: ([\d.]+)')
    MSG = re.compile('Msg (\d+), (.*)')
    MSG31 = re.compile('Wind ([\d.]+) kmph / ([\d.]+) mph ([\d.]+).*rain gauge ([\d.]+) in')
    MSG38 = re.compile('Wind ([\d.]+) kmph / ([\d.]+) mph, ([\d.-]+) C ([\d.-]+) F ([\d.]+) % RH')

    @staticmethod
    def parse_text(ts, payload, lines):
        pkt = dict()
        m = Acurite5n1Packet.PATTERN.search(lines[0])
        if m:
            pkt['dateTime'] = ts
            pkt['usUnits'] = weewx.METRIC
            pkt['hardware_id'] = m.group(1)
            pkt['channel'] = m.group(2)
            payload = m.group(3)
            m = Acurite5n1Packet.MSG.search(payload)
            if m:
                msg_type = m.group(1)
                payload = m.group(2)
                if msg_type == '31':
                    m = Acurite5n1Packet.MSG31.search(payload)
                    if m:
                        pkt['wind_speed'] = float(m.group(1))
                        pkt['wind_speed_mph'] = float(m.group(2))
                        pkt['wind_dir'] = float(m.group(3))
                        pkt['rain_total'] = float(m.group(4))
                    else:
                        loginf("Acurite5n1Packet: no match for type 31: '%s'"
                               % payload)
                elif msg_type == '38':
                    m = Acurite5n1Packet.MSG38.search(payload)
                    if m:
                        pkt['wind_speed'] = float(m.group(1))
                        pkt['wind_speed_mph'] = float(m.group(2))
                        pkt['temperature'] = float(m.group(3))
                        pkt['temperature_F'] = float(m.group(4))
                        pkt['humidity'] = float(m.group(5))
                    else:
                        loginf("Acurite5n1Packet: no match for type 38: '%s'"
                               % payload)
                else:
                    loginf("Acurite5n1Packet: unknown message type %s"
                           " in line '%s'" % (msg_type, lines[0]))
            else:
                m = Acurite5n1Packet.RAIN.search(payload)
                if m:
                    total = float(m.group(1))
                    pkt['rain_since_reset'] = total
                    loginf("Acurite5n1Packet: rain since reset: %s" % total)
                else:
                    loginf("Acurite5n1Packet: unknown message format: '%s'" %
                           lines[0])
        else:
            loginf("Acurite5n1Packet: unrecognized data: '%s'" % lines[0])
        lines.pop(0)
        return Acurite.insert_ids(pkt, Acurite5n1Packet.__name__)

    # sample json output from rtl_433 as of jan2017
    # {"time" : "2017-01-16 02:34:12", "model" : "Acurite 5n1 sensor", "sensor_id" : 3066, "channel" : "C", "sequence_num" : 1, "battery" : "OK", "message_type" : 49, "wind_speed" : 0.000, "wind_dir_deg" : 67.500, "wind_dir" : "ENE", "rainfall_accumulation" : 0.000, "raincounter_raw" : 8978}
    # {"time" : "2017-01-16 02:37:33", "model" : "Acurite 5n1 sensor", "sensor_id" : 3066, "channel" : "C", "sequence_num" : 1, "battery" : "OK", "message_type" : 56, "wind_speed" : 0.000, "temperature_F" : 27.500, "humidity" : 56}

    # some changes to rtl_433 as of dec2017
    # {"time" : "2017-12-24 02:07:00", "model" : "Acurite 5n1 sensor", "sensor_id" : 2662, "channel" : "A", "sequence_num" : 2, "battery" : "OK", "message_type" : 56, "wind_speed_mph" : 0.000, "temperature_F" : 47.500, "humidity" : 74}
    # {"time" : "2017-12-24 02:07:18", "model" : "Acurite 5n1 sensor", "sensor_id" : 2662, "channel" : "A", "sequence_num" : 2, "battery" : "OK", "message_type" : 49, "wind_speed_mph" : 0.000, "wind_dir_deg" : 157.500, "wind_dir" : "SSE", "rainfall_accumulation_inch" : 0.000, "raincounter_raw" : 421}

    # more changes to rtl_433 as of dec2018
    # {"time" : "2019-01-04 02:37:10", "model" : "Acurite 5n1 sensor", "sensor_id" : 2662, "channel" : "A", "sequence_num" : 1, "battery" : "OK", "message_type" : 56, "wind_speed_kph" : 0.000, "temperature_F" : 42.400, "humidity" : 83}
    # {"time" : "2019-01-04 02:37:28", "model" : "Acurite 5n1 sensor", "sensor_id" : 2662, "channel" : "A", "sequence_num" : 0, "battery" : "LOW", "message_type" : 49, "wind_speed_kph" : 0.000, "wind_dir_deg" : 180.000, "rain_inch" : 28.970}

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.US
        pkt['hardware_id'] = "%04x" % obj.get('sensor_id', 0)
        pkt['channel'] = obj.get('channel')
        pkt['battery'] = Packet.get_battery(obj)
        pkt['status'] = obj.get('status')
        msg_type = obj.get('message_type')
        if msg_type == 49: # 0x31
            pkt['wind_speed'] = Acurite5n1Packet.get_wind_speed(obj)
            pkt['wind_dir'] = Packet.get_float(obj, 'wind_dir_deg')
            pkt['rain_total'] = Acurite5n1Packet.get_rain_total(obj)
        elif msg_type == 56: # 0x38
            pkt['wind_speed'] = Acurite5n1Packet.get_wind_speed(obj)
            pkt['temperature'] = Packet.get_float(obj, 'temperature_F')
            pkt['humidity'] = Packet.get_float(obj, 'humidity')
        return Acurite.insert_ids(pkt, Acurite5n1Packet.__name__)

    @staticmethod
    def get_wind_speed(obj):
        ws = None
        if 'wind_speed_mph' in obj:
            ws = Packet.get_float(obj, 'wind_speed_mph')
        if 'wind_speed_kph' in obj:
            ws = Packet.get_float(obj, 'wind_speed_kph')
            if ws is not None:
                ws = weewx.units.kph_to_mph(ws)
        return ws

    @staticmethod
    def get_rain_total(obj):
        rain_total = None
        if 'raincounter_raw' in obj:
            rain_counter = Packet.get_int(obj, 'raincounter_raw')
            # put some units on the rain total - each tip is 0.01 inch
            if rain_counter is not None:
                rain_total = rain_counter * 0.01 # inch
        elif 'rain_inch' in obj:
            rain_total = Packet.get_float(obj, 'rain_inch')
        return rain_total


class Acurite5n1PacketV2(Packet):
    # Based on Acurite5n1Packet class, but implemented for unsupported format

    # sample json output from rtl_433
    # {"time" : "2019-07-29 07:46:22.482883", "protocol" : 40, "model" : "Acurite-5n1", "id" : 1234, "channel" : "B", "sequence_num" : 1, "battery_ok" : 1, "message_type" : 56, "wind_avg_km_h" : 0.000, "temperature_C" : 20.500, "humidity" : 93, "mod" : "ASK", "freq" : 433.934, "rssi" : -1.719, "snr" : 24.404, "noise" : -26.124}
    # {"time" : "2020-02-05 02:20:54", "model" : "Acurite-5n1", "subtype" : 56, "id" : 956, "channel" : "A", "sequence_num" : 2, "battery_ok" : 1, "wind_avg_km_h" : 3.483, "temperature_F" : 31.300, "humidity" : 66}
    # {"time" : "2020-10-26 22:09:12", "model" : "Acurite-5n1", "message_type" : 49, "id" : 2662, "channel" : "A", "sequence_num" : 0, "battery_ok" : 1, "wind_avg_km_h" : 15.900, "wind_dir_deg" : 337.500, "rain_in" : 7.290, "mic" : "CHECKSUM"}
    # {"time" : "2020-10-26 22:08:54", "model" : "Acurite-5n1", "message_type" : 56, "id" : 2662, "channel" : "A", "sequence_num" : 2, "battery_ok" : 1, "wind_avg_km_h" : 9.278, "temperature_F" : 76.100, "humidity" : 15, "mic" : "CHECKSUM"}

    IDENTIFIER = "Acurite-5n1"

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['usUnits'] = weewx.US
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['protocol'] = Packet.get_int(obj, 'protocol')
        pkt['model'] = obj.get('model')
        pkt['hardware_id'] = "%04x" % obj.get('id', 0)
        pkt['channel'] = obj.get('channel')
        pkt['sequence_num'] = Packet.get_int(obj, 'sequence_num')
        pkt['battery'] = Packet.get_battery(obj)
        # connection diagnostics depend on the version of rtl_433
        pkt['mod'] = obj.get('mod')  # apparently is ASK
        pkt['freq'] = Packet.get_float(obj, 'freq')
        pkt['rssi'] = Packet.get_float(obj, 'rssi')
        pkt['snr'] = Packet.get_float(obj, 'snr')
        pkt['noise'] = Packet.get_float(obj, 'noise')
        # the label for message type has changed in rtl_433
        if 'subtype' in obj:
            pkt['msg_type'] = Packet.get_int(obj, 'subtype')
        elif 'message_type' in obj:
            pkt['msg_type'] = Packet.get_int(obj, 'message_type')
        # each message type contains different information.  units vary
        # depending on the rtl_433 configuration, so be ready for anything.
        #   49 has wind_speed, wind_dir, and rain
        #   56 has wind_speed, temperature, humidity
        if 'wind_avg_km_h' in obj:
            pkt['wind_speed'] = to_mph(Packet.get_float(obj, 'wind_avg_km_h'))
        if 'wind_dir_deg' in obj:
            pkt['wind_dir'] = Packet.get_float(obj, 'wind_dir_deg')
        if 'rain_in' in obj:
            pkt['rain_total'] = Packet.get_float(obj, 'rain_in')
        elif 'rain_mm' in obj:
            pkt['rain_total'] = to_in(Packet.get_float(obj, 'rain_mm'))
        if 'temperature_F' in obj:
            pkt['temperature'] = Packet.get_float(obj, 'temperature_F')
        elif 'temperature_C' in obj:
            pkt['temperature'] = to_F(Packet.get_float(obj, 'temperature_C'))
        if 'humidity' in obj:
            pkt['humidity'] = Packet.get_float(obj, 'humidity')
        return Acurite.insert_ids(pkt, Acurite5n1PacketV2.__name__)


class AcuriteTowerPacket(Packet):
    # initial implementation was single-line
    # 2016-08-30 23:57:20 Acurite tower sensor 0x37FC Ch A: 26.7 C 80.1 F 16 % RH
    #
    # multi-line was introduced nov2016 - only single line is supported here
    # 2017-01-12 02:55:10 : Acurite tower sensor : 12391 : B
    # Temperature: 18.0 C
    # Humidity: 68
    # Battery: 0
    # : 68

    IDENTIFIER = "Acurite tower sensor"
    PATTERN = re.compile('0x([0-9a-fA-F]+) Ch ([A-C]): ([\d.-]+) C ([\d.-]+) F ([\d]+) % RH')

    @staticmethod
    def parse_text(ts, payload, lines):
        pkt = dict()
        m = AcuriteTowerPacket.PATTERN.search(lines[0])
        if m:
            pkt['dateTime'] = ts
            pkt['usUnits'] = weewx.METRIC
            pkt['hardware_id'] = m.group(1)
            pkt['channel'] = m.group(2)
            pkt['temperature'] = float(m.group(3))
            pkt['temperature_F'] = float(m.group(4))
            pkt['humidity'] = float(m.group(5))
            pkt = Acurite.insert_ids(pkt, AcuriteTowerPacket.__name__)
        else:
            loginf("AcuriteTowerPacket: unrecognized data: '%s'" % lines[0])
        lines.pop(0)
        return pkt

    # JSON format as of mid-2018
    # {"time" : "2018-07-21 01:53:56", "model" : "Acurite tower sensor", "id" : 13009, "sensor_id" : 13009, "channel" : "A", "temperature_C" : 15.000, "humidity" : 16, "battery_low" : 1}
    # {"time" : "2018-07-21 01:52:24", "model" : "Acurite tower sensor", "id" : 13009, "sensor_id" : 13009, "channel" : "A", "temperature_C" : 15.600, "humidity" : 16, "battery_low" : 0}

    # JSON format as of early 2017
    # {"time" : "2017-01-12 03:43:05", "model" : "Acurite tower sensor", "id" : 521, "channel" : "A", "temperature_C" : 0.800, "humidity" : 68, "battery" : 0, "status" : 68}
    # {"time" : "2017-01-12 03:43:11", "model" : "Acurite tower sensor", "id" : 5585, "channel" : "C", "temperature_C" : 21.100, "humidity" : 32, "battery" : 0, "status" : 68}

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.US
        pkt['hardware_id'] = "%04x" % obj.get('id', 0)
        pkt['channel'] = obj.get('channel')
        pkt['battery'] = Packet.get_battery(obj)
        pkt['status'] = obj.get('status')
        if 'temperature_F' in obj:
            pkt['temperature'] = Packet.get_float(obj, 'temperature_F')
        elif 'temperature_C' in obj:
            pkt['temperature'] = to_F(Packet.get_float(obj, 'temperature_C'))
        pkt['humidity'] = Packet.get_float(obj, 'humidity')
        return Acurite.insert_ids(pkt, AcuriteTowerPacket.__name__)


class AcuriteTowerPacketV2(Packet):
    # Based on AcuriteTowerPacket type, but implemented for unsupported format

    # Sample data:
    # {"time" : "2019-07-29 07:44:23.005624", "protocol" : 40, "model" : "Acurite-Tower", "id" : 1234, "sensor_id" : 1234, "channel" : "A", "temperature_C" : 22.600, "humidity" : 45, "battery_ok" : 0, "mod" : "ASK", "freq" : 433.938, "rssi" : -0.134, "snr" : 14.391, "noise" : -14.525}
    # {"time" : "2021-12-20 20:00:59", "model" : "Acurite-Tower", "id" : 11041, "channel" : "B", "battery_ok" : 1, "temperature_C" : -3.500, "humidity" : 71, "mic" : "CHECKSUM"}

    IDENTIFIER = "Acurite-Tower"

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['usUnits'] = weewx.US
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['protocol'] = Packet.get_int(obj, 'protocol') # 40
        pkt['model'] = obj.get('model') # model = Acurite-Tower
        pkt['hardware_id'] = "%04x" % obj.get('id', 0)
        pkt['sensor_id'] = "%04x" % obj.get('sensor_id', 0)
        pkt['channel'] = obj.get('channel')
        pkt['humidity'] = Packet.get_float(obj, 'humidity')
        if 'temperature_F' in obj:
            pkt['temperature'] = Packet.get_float(obj, 'temperature_F')
        elif 'temperature_C' in obj:
            pkt['temperature'] = to_F(Packet.get_float(obj, 'temperature_C'))
        pkt['battery'] = Packet.get_battery(obj)
        pkt['mod'] = obj.get('mod') # apparently mod = ASK
        pkt['freq'] = Packet.get_float(obj, 'freq')
        pkt['rssi'] = Packet.get_float(obj, 'rssi')
        pkt['snr'] = Packet.get_float(obj, 'snr')
        pkt['noise'] = Packet.get_float(obj, 'noise')
        return Acurite.insert_ids(pkt, AcuriteTowerPacketV2.__name__)


class Acurite606TXPacket(Packet):
    # 2017-03-20: Acurite 606TX Temperature Sensor
    # {"time" : "2017-03-04 16:18:12", "model" : "Acurite 606TX Sensor", "id" : 48, "battery" : "OK", "temperature_C" : -1.100}

    IDENTIFIER = "Acurite 606TX Sensor"

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.US
        sensor_id = obj.get('id')
        if 'temperature_F' in obj:
            pkt['temperature'] = Packet.get_float(obj, 'temperature_F')
        elif 'temperature_C' in obj:
            pkt['temperature'] = to_F(Packet.get_float(obj, 'temperature_C'))
        pkt['battery'] = Packet.get_battery(obj)
        pkt = Packet.add_identifiers(pkt, sensor_id, Acurite606TXPacket.__name__)
        return pkt


class Acurite606TXPacketV2(Packet):
    # 2021-02-23: Acurite 606TX Temperature Sensor
    # {"time" : "2021-02-23 16:24:07", "model" : "Acurite-606TX", "id" : 153, "battery_ok" : 1, "temperature_C" : 18.800, "mic" : "CHECKSUM"}
    # {"time" : "2021-10-26 23:39:49", "model" : "Acurite-606TX", "id" : 194, "battery_ok" : 1, "temperature_C" : 19.200, "mic" : "CHECKSUM"}

    IDENTIFIER = "Acurite-606TX"

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.US
        sensor_id = obj.get('id')
        if 'temperature_F' in obj:
            pkt['temperature'] = Packet.get_float(obj, 'temperature_F')
        elif 'temperature_C' in obj:
            pkt['temperature'] = to_F(Packet.get_float(obj, 'temperature_C'))
        pkt['battery'] = Packet.get_battery(obj)
        pkt = Packet.add_identifiers(pkt, sensor_id, Acurite606TXPacketV2.__name__)
        return pkt

      
class AcuriteRain899Packet(Packet):
    # Sample data:
    # {"time" : "2019-12-05 16:32:20", "model" : "Acurite-Rain899", "id" : 1699, "channel" : 0, "battery_ok" : 0, "rain_mm" : 6.096}
    # {"time" : "2019-12-05 16:32:20", "model" : "Acurite-Rain899", "id" : 1699, "channel" : 0, "battery_ok" : 0, "rain_mm" : 6.096}
    # {"time" : "2019-12-05 16:32:20", "model" : "Acurite-Rain899", "id" : 1699, "channel" : 0, "battery_ok" : 0, "rain_mm" : 6.096}

    IDENTIFIER = "Acurite-Rain899"

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['usUnits'] = weewx.US
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['model'] = obj.get('model')
        pkt['hardware_id'] = "%04x" % obj.get('id', 0)
        pkt['channel'] = obj.get('channel')
        pkt['battery'] = Packet.get_battery(obj)
        if 'rain_mm' in obj:
            pkt['rain_total'] = to_in(Packet.get_float(obj, 'rain_mm'))
        return Acurite.insert_ids(pkt, AcuriteRain899Packet.__name__)


class Acurite986Packet(Packet):
    # 2016-10-31 15:24:29 Acurite 986 sensor 0x2c87 - 2F: 16.7 C 62 F
    # 2016-10-31 15:23:54 Acurite 986 sensor 0x85ed - 1R: 16.7 C 62 F
    # {"time" : "2018-04-22 18:01:03", "model" : "Acurite 986 Sensor", "id" : 43248, "channel" : "1R", "temperature_F" : 69, "battery" : "OK", "status" : 0}
    # {"time" : "2020-10-19 07:00:32", "model" : "Acurite-986", "id" : 9534, "channel" : "2F", "battery_ok" : 1, "temperature_F" : -10.000, "status" : 0, "mic" : "CRC"}

    # The 986 hardware_id changes, so using the 2F and 1R as the hardware
    # identifer.  As long as you only have one set of sendors and your
    # close neighbors have none.

    # Older releases of rtl_433 used 'Acurite 986 sensor', while recent
    # versions use 'Acurite 986 Sensor'.  So we try to be compatible by
    # matching on the least that we can.

    # IDENTIFIER = "Acurite 986 sensor"
    # IDENTIFIER = "Acurite 986 Sensor"
    IDENTIFIER = "Acurite-986"
    PATTERN = re.compile('0x([0-9a-fA-F]+) - (1R|2F): ([\d.-]+) C ([\d.-]+) F')

    @staticmethod
    def parse_text(ts, payload, lines):
        pkt = dict()
        m = Acurite986Packet.PATTERN.search(lines[0])
        if m:
            pkt['dateTime'] = ts
            pkt['usUnits'] = weewx.METRIC
            pkt['hardware_id'] = m.group(1)
            pkt['channel'] = m.group(2)
            pkt['temperature'] = float(m.group(3))
            pkt['temperature_F'] = float(m.group(4))
        else:
            loginf("Acurite986Packet: unrecognized data: '%s'" % lines[0])
        lines.pop(0)
        return Acurite.insert_ids(pkt, Acurite986Packet.__name__)

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['hardware_id'] = obj.get('id', 0)
        pkt['channel'] = obj.get('channel')
        pkt['battery'] = Packet.get_battery(obj)
        if 'temperature_F' in obj:
            pkt['usUnits'] = weewx.US
            pkt['temperature'] = Packet.get_float(obj, 'temperature_F')
        else:
            pkt['usUnits'] = weewx.METRIC
            pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        return Acurite.insert_ids(pkt, Acurite986Packet.__name__)


class AcuriteLightningPacket(Packet):
    # with rtl_433 update of 19mar2017
    # 2017-03-19 16:48:31 Acurite lightning 0x976F Ch A Msg Type 0x02: 66.2 F 25 % RH Strikes 1 Distance 0 L_status 0x02 - c0 97* 6f  99  50  72  81  c0  62*
    # 2017-03-19 16:48:47 Acurite lightning 0x976F Ch A Msg Type 0x02: 66.2 F 25 % RH Strikes 1 Distance 0 L_status 0x02 - c0  97* 6f  99  50  72  81  c0  62*

    # pre-19mar2017
    # 2016-11-04 04:34:58 Acurite lightning 0x536F Ch A Msg Type 0x51: 15 C 58 % RH Strikes 50 Distance 69 - c0  53  6f  3a  d1  0f  b2  c5  13*
    # 2016-11-04 04:43:14 Acurite lightning 0x536F Ch A Msg Type 0x51: 15 C 58 % RH Strikes 55 Distance 5 - c0  53  6f  3a  d1  0f  b7  05  58*
    # 2016-11-04 04:43:22 Acurite lightning 0x536F Ch A Msg Type 0x51: 15 C 58 % RH Strikes 55 Distance 69 - c0  53  6f  3a  d1  0f  b7  c5  18
    # 2017-01-16 02:37:39 Acurite lightning 0x526F Ch A Msg Type 0x11: 67 C 38 % RH Strikes 47 Distance 81 - dd  52* 6f  a6  11  c3  af  d1  98*

    # April 21, 2018 - JSON support
    # {"time" : "2018-04-21 19:12:53", "model" : "Acurite Lightning 6045M", "id" : 151, "channel" : "C", "temperature_F" : 66.900, "humidity" : 33, "strike_count" : 47, "storm_dist" : 12, "active" : 1, "rfi" : 0, "ussb1" : 1, "battery" : "LOW", "exception" : 0, "raw_msg" : "0097af2150f9afcc2b"}
    # {"time" : "2020-10-13 22:49:34", "model" : "Acurite-6045M", "id" : 15431, "channel" : "A", "battery_ok" : 0, "temperature_F" : 91.800, "humidity" : 21, "strike_count" : 171, "storm_dist" : 12, "active" : 1, "rfi" : 0, "exception" : 0, "raw_msg" : "fc47af95d2de55cc58"}

#    IDENTIFIER = "Acurite lightning"
#    IDENTIFIER = "Acurite Lightning 6045M"
    IDENTIFIER = "Acurite-6045M"
    PATTERN = re.compile('0x([0-9a-fA-F]+) Ch (.) Msg Type 0x([0-9a-fA-F]+): ([\d.-]+) ([CF]) ([\d.]+) % RH Strikes ([\d]+) Distance ([\d.]+)')

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.US
        pkt['channel'] = obj.get('channel')
        pkt['hardware_id'] = "%04x" % obj.get('id', 0)
        pkt['temperature'] = obj.get('temperature_F')
        pkt['battery'] = Packet.get_battery(obj)
        pkt['humidity'] = obj.get('humidity')
        pkt['active'] = obj.get('active')
        pkt['rfi'] = obj.get('rfi')
        pkt['exception'] = obj.get('exception')
        pkt['strikes_total'] = obj.get('strike_count')
        pkt['distance'] = obj.get('storm_dist')
        return Acurite.insert_ids(pkt, AcuriteLightningPacket.__name__)

    @staticmethod
    def parse_text(ts, payload, lines):
        pkt = dict()
        m = AcuriteLightningPacket.PATTERN.search(lines[0])
        if m:
            pkt['dateTime'] = ts
            units = m.group(5)
            if units == 'C':
                pkt['usUnits'] = weewx.METRIC
            else:
                pkt['usUnits'] = weewx.US
            pkt['hardware_id'] = m.group(1)
            pkt['channel'] = m.group(2)
            pkt['msg_type'] = m.group(3)
            pkt['temperature'] = float(m.group(4))
            pkt['humidity'] = float(m.group(6))
            pkt['strikes_total'] = float(m.group(7))
            pkt['distance'] = float(m.group(8))
        else:
            loginf("AcuriteLightningPacket: unrecognized data: %s" % lines[0])
        lines.pop(0)
        return Acurite.insert_ids(pkt, AcuriteLightningPacket.__name__)


class Acurite00275MPacket(Packet):

    # {"time" : "2017-03-09 21:59:11", "model" : "00275rm", "probe" : 2, "id" : 3942, "battery" : "OK", "temperature_C" : 23.300, "humidity" : 34, "ptemperature_C" : 22.700, "crc" : "ok"}
    # {"time" : "2017-03-09 21:59:11", "model" : "00275rm", "probe" : 2, "id" : 3942, "battery" : "OK", "temperature_C" : 23.300, "humidity" : 34, "temperature_1_C" : 22.700, "crc" : "ok"}

    IDENTIFIER = "00275rm"

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRIC
        pkt['hardware_id'] = "%04x" % obj.get('id', 0)
        pkt['probe'] = obj.get('probe')
        pkt['battery'] = Packet.get_battery(obj)
        if 'temperature_1_C' in obj:
            pkt['temperature_probe'] = Packet.get_float(obj, 'temperature_1_C')
        else:
            pkt['temperature_probe'] = Packet.get_float(obj, 'ptemperature_C')
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        pkt['humidity'] = Packet.get_float(obj, 'humidity')
        return Acurite.insert_ids(pkt, Acurite00275MPacket.__name__)


class AcuriteWT450Packet(Packet):

    # {"time" : "2017-09-14 20:24:43", "model" : "WT450 sensor", "id" : 1, "channel" : 2, "battery" : "OK", "temperature_C" : 25.090, "humidity" : 49}
    # {"time" : "2017-09-14 20:24:44", "model" : "WT450 sensor", "id" : 1, "channel" : 2, "battery" : "OK", "temperature_C" : 25.110, "humidity" : 49}
    # {"time" : "2017-09-14 20:24:44", "model" : "WT450 sensor", "id" : 1, "channel" : 2, "battery" : "OK", "temperature_C" : 25.120, "humidity" : 49}

    IDENTIFIER = "WT450 sensor"

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRIC
        pkt['sid'] = Packet.get_int(obj, 'id')
        pkt['channel'] = Packet.get_int(obj, 'channel')
        pkt['battery'] = Packet.get_battery(obj)
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        pkt['humidity'] = Packet.get_float(obj, 'humidity')
        _id = "%s:%s" % (pkt['sid'], pkt['channel'])
        return Packet.add_identifiers(pkt, _id, AcuriteWT450Packet.__name__)


class Acurite515Packet(Packet):

    # refrigerator (XR) and freezer (XF) sensors
    # X is one of A, B, or C
    # "time" : "2022-01-21 21:55:54", "model" : "Acurite-515", "id" : 2375, "channel" : "BR", "battery_ok" : 1, "temperature_F" : 47.600, "mic" : "CHECKSUM"
    # "time" : "2022-01-21 21:55:44", "model" : "Acurite-515", "id" : 78, "channel" : "BF", "battery_ok" : 1, "temperature_F" : 47.100, "mic" : "CHECKSUM"

    IDENTIFIER = "Acurite-515"

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.US
        pkt['hardware_id'] = "%04x" % obj.get('id', 0)
        pkt['channel'] = Packet.get_int(obj, 'channel')
        pkt['battery'] = Packet.get_battery(obj)
        if 'temperature_F' in obj:
            pkt['temperature'] = Packet.get_float(obj, 'temperature_F')
        elif 'temperature_C' in obj:
            pkt['temperature'] = to_F(Packet.get_float(obj, 'temperature_C'))
        return Acurite.insert_ids(pkt, Acurite515Packet.__name__)


class AlectoV1TemperaturePacket(Packet):
    # {"time" : "2024-12-28 09:06:10", "model" : "AlectoV1-Temperature", "id" : 33, "channel" : 1, "battery_ok" : 1, "temperature_C" : -2.200, "humidity" : 51, "mic" : "CHECKSUM"}

    IDENTIFIER = "AlectoV1-Temperature"

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRICWX
        station_id = obj.get('id')
        pkt['channel'] = obj.get('channel')
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        pkt['humidity'] = Packet.get_float(obj, 'humidity')
        pkt['battery'] = Packet.get_battery(obj)
        pkt = Packet.add_identifiers(pkt, station_id, AlectoV1TemperaturePacket.__name__)
        return pkt


class AlectoV1WindPacket(Packet):
    # {"time" : "2024-12-28 09:06:41", "model" : "AlectoV1-Wind", "id" : 33, "channel" : 1, "battery_ok" : 1, "wind_avg_m_s" : 0.800, "wind_max_m_s" : 1.000, "wind_dir_deg" : 180, "mic" : "CHECKSUM"}

    IDENTIFIER = "AlectoV1-Wind"

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRICWX
        station_id = obj.get('id')
        pkt['channel'] = obj.get('channel')
        pkt['wind_speed'] = Packet.get_float(obj, 'wind_avg_m_s')
        pkt['wind_gust'] = Packet.get_float(obj, 'wind_max_m_s')
        pkt['wind_dir'] = Packet.get_int(obj, 'wind_dir_deg')
        pkt['battery'] = Packet.get_battery(obj)
        pkt = Packet.add_identifiers(pkt, station_id, AlectoV1WindPacket.__name__)
        return pkt


class AlectoV1RainPacket(Packet):
    # {"time" : "2024-12-28 09:06:31", "model" : "AlectoV1-Rain", "id" : 202, "channel" : 0, "battery_ok" : 1, "rain_mm" : 54.750, "mic" : "CHECKSUM"}

    IDENTIFIER = "AlectoV1-Rain"

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRICWX
        station_id = obj.get('id')
        pkt['channel'] = obj.get('channel')
        pkt['rain_total'] = Packet.get_float(obj, 'rain_mm')
        pkt['battery'] = Packet.get_battery(obj)
        pkt = Packet.add_identifiers(pkt, station_id, AlectoV1RainPacket.__name__)
        return pkt


class AmbientF007THPacket(Packet):
    # 2017-01-21 18:17:16 : Ambient Weather F007TH Thermo-Hygrometer
    # House Code: 80
    # Channel: 1
    # Temperature: 61.8
    # Humidity: 13 %

#    IDENTIFIER = "Ambient Weather F007TH Thermo-Hygrometer"
    IDENTIFIER = "Ambientweather-F007TH"
    PARSEINFO = {
        'House Code': ['house_code', None, lambda x: int(x)],
        'Channel': ['channel', None, lambda x: int(x)],
        'Temperature': [
            'temperature', re.compile('([\d.-]+) F'), lambda x: float(x)],
        'Humidity': ['humidity', re.compile('([\d.]+) %'), lambda x: float(x)]}

    @staticmethod
    def parse_text(ts, payload, lines):
        pkt = dict()
        pkt['dateTime'] = ts
        pkt['usUnits'] = weewx.METRIC
        pkt.update(Packet.parse_lines(lines, AmbientF007THPacket.PARSEINFO))
        house_code = pkt.pop('house_code', 0)
        channel = pkt.pop('channel', 0)
        sensor_id = "%s:%s" % (channel, house_code)
        pkt = Packet.add_identifiers(
            pkt, sensor_id, AmbientF007THPacket.__name__)
        return pkt

    # {"time" : "2017-01-21 13:01:30", "model" : "Ambient Weather F007TH Thermo-Hygrometer", "device" : 80, "channel" : 1, "temperature_F" : 61.800, "humidity" : 10}
    # as of 06feb2020:
    # {"time" : "2020-02-05 19:33:11", "model" : "Ambientweather-F007TH", "id" : 201, "channel" : 5, "battery_ok" : 1, "temperature_F" : 39.400, "humidity" : 60, "mic" : "CRC"}

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.US
        house_code = obj.get('id', 0)
        channel = obj.get('channel')
        pkt['temperature'] = Packet.get_float(obj, 'temperature_F')
        pkt['humidity'] = Packet.get_float(obj, 'humidity')
        sensor_id = "%s:%s" % (channel, house_code)
        pkt['battery'] = Packet.get_battery(obj)
        pkt['mod'] = obj.get('mod')
        pkt['freq'] = Packet.get_float(obj, 'freq')
        pkt['rssi'] = Packet.get_float(obj, 'rssi')
        pkt['snr'] = Packet.get_float(obj, 'snr')
        pkt['noise'] = Packet.get_float(obj, 'noise')
        pkt = Packet.add_identifiers(
            pkt, sensor_id, AmbientF007THPacket.__name__)
        return pkt


class AmbientTX8300Packet(Packet):

    # {"time" : "2021-06-14 21:38:43", "model" : "AmbientWeather-TX8300", "id" : 116, "channel" : 1, "battery" : 2, "temperature_C" : 28.500, "mic" : "CHECKSUM"}

    IDENTIFIER = "AmbientWeather-TX8300"

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRIC
        station_id = obj.get('id')
        pkt['channel'] = Packet.get_int(obj, 'channel')
        pkt['battery'] = Packet.get_battery(obj)
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        pkt = Packet.add_identifiers(pkt, station_id, AmbientTX8300Packet.__name__)
        return pkt


class AmbientWH31EPacket(Packet):

    # {"time" : "2019-02-14 17:24:41.259441", "protocol" : 113, "model" : "AmbientWeather-WH31E", "id" : 24, "channel" : 1, "battery" : "OK", "temperature_C" : 6.000, "humidity" : 42, "data" :"2f00000000", "mic" : "CRC", "mod" : "FSK", "freq1" : 914.984, "freq2" : 914.906, "rssi" : -13.328, "snr" : 13.197, "noise" : -26.525}

    IDENTIFIER = "AmbientWeather-WH31E"

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRICWX
        pkt['station_id'] = obj.get('id')
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        pkt['humidity'] = Packet.get_float(obj, 'humidity')
        pkt['battery'] = Packet.get_battery(obj)
        pkt['channel'] = Packet.get_int(obj, 'channel')
        pkt['rssi'] = Packet.get_int(obj, 'rssi')
        pkt['snr'] = Packet.get_float(obj, 'snr')
        pkt['noise'] = Packet.get_float(obj, 'noise')
        return AmbientWH31EPacket.insert_ids(pkt)

    @staticmethod
    def insert_ids(pkt):
        station_id = pkt.pop('station_id', '0000')
        pkt = Packet.add_identifiers(pkt, station_id, AmbientWH31EPacket.__name__)
        return pkt


class AmbientWH31BPacket(Packet):

    # {'time': '2024-03-04 17:36:20', 'model': 'AmbientWeather-WH31B', 'id': 196, 'channel': 3, 'battery_ok': 1, 'temperature_C': 21.6, 'humidity': 40, 'data': 'ea00000000', 'mic': 'CRC'}
    IDENTIFIER = "AmbientWeather-WH31B"

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRICWX
        pkt['station_id'] = obj.get('id')
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        pkt['humidity'] = Packet.get_float(obj, 'humidity')
        pkt['battery'] = Packet.get_battery(obj)
        pkt['channel'] = Packet.get_int(obj, 'channel')
        pkt['rssi'] = Packet.get_int(obj, 'rssi')
        pkt['snr'] = Packet.get_float(obj, 'snr')
        pkt['noise'] = Packet.get_float(obj, 'noise')
        return AmbientWH31BPacket.insert_ids(pkt)

    @staticmethod
    def insert_ids(pkt):
        station_id = pkt.pop('station_id', '0000')
        pkt = Packet.add_identifiers(pkt, station_id, AmbientWH31BPacket.__name__)
        return pkt


class AuriolHG02832Packet(Packet):

    # {"time" : "2017-09-14 20:24:43", "model" : "Auriol-HG02832", "id" : 1, "channel" : 2, "battery" : "OK", "temperature_C" : 25.090, "humidity" : 49}
    # {"time" : "2017-09-14 20:24:44", "model" : "Auriol-HG02832", "id" : 1, "channel" : 2, "battery" : "OK", "temperature_C" : 25.110, "humidity" : 49}
    # {"time" : "2017-09-14 20:24:44", "model" : "Auriol-HG02832", "id" : 1, "channel" : 2, "battery" : "OK", "temperature_C" : 25.120, "humidity" : 49}

    IDENTIFIER = "Auriol-HG02832"

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRIC
        pkt['sid'] = Packet.get_int(obj, 'id')
        pkt['channel'] = Packet.get_int(obj, 'channel')
        pkt['battery'] = Packet.get_battery(obj)
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        pkt['humidity'] = Packet.get_float(obj, 'humidity')
        _id = "%s:%s" % (pkt['sid'], pkt['channel'])
        pkt = Packet.add_identifiers(pkt, _id, AuriolHG02832Packet.__name__)
        return pkt


class Bresser5in1Packet(Packet):
    #  'time' => '2018-12-15 16:04:04',
    #  'model' => 'Bresser-5in1',
    #  'id' => 118,
    #  'temperature_C' => 6.4000000000000003552713678800500929355621337890625,
    #  'humidity' => 87,
    #  'wind_gust' => 2.79999999999999982236431605997495353221893310546875,
    #  'wind_speed' => 2.899999999999999911182158029987476766109466552734375,
    #  'wind_dir_deg' => 315,
    #  'rain_mm' => 10.800000000000000710542735760100185871124267578125,
    #  'data' => 'e7897fd71fd6ef9bff78f7feff18768028e02910640087080100',
    #  'mic' => 'CHECKSUM',

    # {"time" : "2018-12-15 16:04:04", "model" : "Bresser-5in1", "id" : 118,
    # "temperature_C" : 6.400, "humidity" : 87, "wind_gust" : 2.800,
    # "wind_speed" : 2.900, "wind_dir_deg" : 315.000, "rain_mm" : 10.800,
    # "data" : "e7897fd71fd6ef9bff78f7feff18768028e02910640087080100",
    # "mic" : "CHECKSUM"}#012

    # {"time" : "2020-04-20 20:58:46", "model" : "Bresser-5in1", "id" : 182,
    # "battery_ok" : 1, "temperature_C" : 17.000, "humidity" : 92,
    # "wind_max_m_s" : 4.000, "wind_avg_m_s" : 2.400, "wind_dir_deg" : 67.500,
    # "rain_mm" : 0.800, "mic" : "CHECKSUM"}

    IDENTIFIER = "Bresser-5in1"

    @staticmethod
    def parse_json(obj):
        station_id = obj.get('id')
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRICWX
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        pkt['humidity'] = Packet.get_float(obj, 'humidity')
        pkt['wind_dir'] = Packet.get_float(obj, 'wind_dir_deg')
        pkt['uv'] = Packet.get_float(obj, 'uv')
        pkt['uv_index'] = Packet.get_float(obj, 'uvi')
        pkt['battery'] = Packet.get_battery(obj)
        # deal with different labels from rtl_433
        for dst, src in [('wind_speed', 'wind_speed_ms'),
                         ('wind_speed', 'wind_speed'),
                         ('wind_speed', 'wind_avg_m_s'),
                         ('gust_speed', 'gust_speed_ms'),
                         ('gust_speed', 'gust_speed'),
                         ('rain_total', 'rainfall_mm'),
                         ('rain_total', 'rain_mm'),
                         ('wind_gust', 'gust_speed_ms'),
                         ('wind_gust', 'wind_gust'),
                         ('wind_gust', 'gust_speed'),
                         ('wind_gust', 'wind_max_m_s')]:
            if src in obj:
                pkt[dst] = Packet.get_float(obj, src)
        pkt = Packet.add_identifiers(pkt, station_id, Bresser5in1Packet.__name__)
        return pkt


class Bresser6in1Packet(Packet):
    #  'time' => '2018-12-15 16:04:04',
    #  'model' => 'Bresser-6in1',
    #  'id' => 118,
    #  'temperature_C' => 6.4000000000000003552713678800500929355621337890625,
    #  'humidity' => 87,
    #  'wind_gust' => 2.79999999999999982236431605997495353221893310546875,
    #  'wind_speed' => 2.899999999999999911182158029987476766109466552734375,
    #  'wind_dir_deg' => 315,
    #  'rain_mm' => 10.800000000000000710542735760100185871124267578125,
    #  'data' => 'e7897fd71fd6ef9bff78f7feff18768028e02910640087080100',
    #  'mic' => 'CHECKSUM',

    # {"time" : "2018-12-15 16:04:04", "model" : "Bresser-6in1", "id" : 118,
    # "temperature_C" : 6.400, "humidity" : 87, "wind_gust" : 2.800,
    # "wind_speed" : 2.900, "wind_dir_deg" : 315.000, "rain_mm" : 10.800,
    # "data" : "e7897fd71fd6ef9bff78f7feff18768028e02910640087080100",
    # "mic" : "CHECKSUM"}#012

    IDENTIFIER = "Bresser-6in1"

    @staticmethod
    def parse_json(obj):
        station_id = obj.get('id')
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRICWX
        pkt['battery'] = Packet.get_battery(obj)
        if 'temperature_C' in obj:
            pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        if 'humidity' in obj:
            pkt['humidity'] = Packet.get_float(obj, 'humidity')
        if 'wind_dir_deg' in obj:
            pkt['wind_dir'] = Packet.get_float(obj, 'wind_dir_deg')
        if 'wind_max_m_s' in obj:
            pkt['wind_gust'] = Packet.get_float(obj, 'wind_max_m_s')
        if 'wind_avg_m_s' in obj:
            pkt['wind_speed'] = Packet.get_float(obj, 'wind_avg_m_s')
        if 'uv' in obj:
            pkt['uv'] = Packet.get_float(obj, 'uv')
        if 'uv_index' in obj:
            pkt['uv_index'] = Packet.get_float(obj, 'uvi')
        # deal with different labels from rtl_433
        for dst, src in [('wind_speed', 'wind_speed_ms'),
                     ('gust_speed', 'gust_speed_ms'),
                     ('rain_total', 'rainfall_mm'),
                     ('wind_speed', 'wind_speed'),
                     ('gust_speed', 'gust_speed'),
                     ('rain_total', 'rain_mm')]:
           if src in obj:
               pkt[dst] = Packet.get_float(obj, src)
        pkt = Packet.add_identifiers(pkt, station_id, Bresser6in1Packet.__name__)
        return pkt


class Bresser7in1Packet(Packet):
    # {"time" : "2023-06-11 17:09:05", "model" : "Bresser-7in1", "id" : 50437,
    #  "temperature_C" : 23.500, "humidity" : 67, "wind_max_m_s" : 0.000,
    #  "wind_avg_m_s" : 0.000, "wind_dir_deg" : 102, "rain_mm" : 3.500,
    #  "light_klx" : 8.592, "light_lux" : 8592.000, "uv" : 1.000,
    #  "battery_ok" : 1, "mic " : "CRC"}

    IDENTIFIER = "Bresser-7in1"

    @staticmethod
    def parse_json(obj):
        station_id = obj.get('id')
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRICWX
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        pkt['humidity'] = Packet.get_float(obj, 'humidity')
        pkt['wind_gust'] = Packet.get_float(obj, 'wind_max_m_s')
        pkt['wind_speed'] = Packet.get_float(obj, 'wind_avg_m_s')
        pkt['wind_dir'] = Packet.get_float(obj, 'wind_dir_deg')
        pkt['rain_total'] = Packet.get_float(obj, 'rain_mm')
        pkt['lux'] = Packet.get_int(obj, 'light_lux')
        pkt['uv'] = Packet.get_float(obj, 'uv')
        pkt['battery'] = Packet.get_battery(obj)
        pkt = Packet.add_identifiers(pkt, station_id, Bresser7in1Packet.__name__)
        return pkt


class BresserProRainGaugePacket(Packet):
    # {"time" : "2021-03-14 15:30:28", "model" : "Bresser-ProRainGauge",
    # "id" : 17, "battery_ok" : 1, "temperature_C" : 9.800,
    # "rain_mm" : 122.000, "mic" : "CHECKSUM"

    IDENTIFIER = "Bresser-ProRainGauge"

    @staticmethod
    def parse_json(obj):
        sensor_id = obj.get('id')
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRICWX
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        pkt['rain_total'] = Packet.get_float(obj, 'rain_mm')
        pkt['battery'] = Packet.get_battery(obj)
        pkt = Packet.add_identifiers(pkt, sensor_id, BresserProRainGaugePacket.__name__)
        return pkt


class CalibeurRF104Packet(Packet):
    # 2016-11-01 01:25:28 :Calibeur RF-104
    # ID: 1
    # Temperature: 1.8 C
    # Humidity: 71 %

    # 2016-11-04 05:16:39 :Calibeur RF-104
    # ID: 1
    # Temperature: -2.2 C
    # Humidity: 71 %

    IDENTIFIER = "Calibeur RF-104"
    PARSEINFO = {
        'ID': ['id', None, lambda x: int(x)],
        'Temperature': [
            'temperature', re.compile('([\d.-]+) C'), lambda x: float(x)],
        'Humidity': ['humidity', re.compile('([\d.]+) %'), lambda x: float(x)]}

    @staticmethod
    def parse_text(ts, payload, lines):
        pkt = dict()
        pkt['dateTime'] = ts
        pkt['usUnits'] = weewx.METRIC
        pkt.update(Packet.parse_lines(lines, CalibeurRF104Packet.PARSEINFO))
        pkt_id = pkt.pop('id', 0)
        sensor_id = "%s" % pkt_id
        pkt = Packet.add_identifiers(
            pkt, sensor_id, CalibeurRF104Packet.__name__)
        return pkt


class Cotech367959Packet(Packet):
    # Cotech 36-7959 weather station
    # Also: SwitchDoc Labs Weather FT020T.
    # Also: Sainlogic Weather Station WS019T
    # Also: Sainlogic Weather Station FT0300
    # Also: Sainlogic Weather Station WS0310 (all current Sainlogic models ?)
    # Also: Ragova WiFi Weather Station FT-0310
    # Also: NicetyMeter Weather Station 0366 (without Lux or UV index)
    #
    # thanks to user gremlin205

    #{"time" : "2022-03-01 14:11:42", "model" : "Cotech-367959", "id" : 24, "battery_ok" : 1, "temperature_F" : 46.900, "humidity" : 62, "rain_mm" : 18.600, "wind_dir_deg" : 16, "wind_avg_m_s" : 0.600, "wind_max_m_s" : 0.700, "mic" : "CRC"}

    IDENTIFIER = "Cotech-367959"

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRICWX
        sensor_id = obj.get('id')
        pkt['battery'] = Packet.get_battery(obj)
        if 'temperature_F' in obj:
            pkt['temperature'] = to_C(Packet.get_float(obj, 'temperature_F'))
        elif 'temperature_C' in obj:
            pkt['temperature'] = Packet.get_float(obj, 'temperature_F')
        pkt['humidity'] = Packet.get_float(obj, 'humidity')
        pkt['wind_gust'] = Packet.get_float(obj, 'wind_max_m_s')
        pkt['wind_speed'] = Packet.get_float(obj, 'wind_avg_m_s')
        pkt['wind_dir'] = Packet.get_float(obj, 'wind_dir_deg')
        pkt['rain_total'] = Packet.get_float(obj, 'rain_mm')
        pkt['uv_index'] = Packet.get_float(obj, 'uv')
        pkt['luminosity'] = Packet.get_float(obj, 'light_lux')
        pkt = Packet.add_identifiers(pkt, sensor_id, Cotech367959Packet.__name__)
        return pkt


class EcoWittWH40Packet(Packet):
    # This is for a WH40 rain sensor

    # {"time" : "2020-02-05 12:37:05", "model" : "EcoWitt-WH40", "id" : 52591, "rain_mm" : 0.800, "data" : "0002ed0000", "mic" : "CRC"}

    IDENTIFIER = "EcoWitt-WH40"

    @staticmethod
    def parse_json(obj):
        sensor_id = obj.get('id')
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRICWX
        pkt['rain_total'] = Packet.get_float(obj, 'rain_mm')
        pkt['battery'] = Packet.get_battery(obj)
        pkt['supplyVoltage'] = Packet.get_float(obj, 'battery_V')
        pkt['freq1'] = Packet.get_float(obj, 'freq1')
        pkt['freq2'] = Packet.get_float(obj, 'freq2')
        pkt['rssi'] = Packet.get_float(obj, 'rssi')
        pkt['snr'] = Packet.get_float(obj, 'snr')
        pkt['noise'] = Packet.get_float(obj, 'noise')
        pkt = Packet.add_identifiers(pkt, sensor_id, EcoWittWH40Packet.__name__)
        return pkt


class EcoWittWS68Packet(Packet):
    # This is for a WS68 wind/solar sensor

    # {"time" : "2022-09-26 00:47:41", "model" : "EcoWitt-WS68", "id" : 388, "battery_raw" : 90, "battery_ok" : 1, "lux_raw" : 0, "wind_avg_raw" : 0, "wind_max_raw" : 0, "wind_dir_deg" : 157, "data" : "00 210", "mic" : "CRC"}

    IDENTIFIER = "EcoWitt-WS68"

    @staticmethod
    def parse_json(obj):
        sensor_id = obj.get('id')
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRICWX
        pkt['battery'] = Packet.get_battery(obj)
        x = Packet.get_float(obj, 'battery_raw')
        if x is not None:
            pkt['supplyVoltage'] = 2 * x
        pkt['luminosity'] = Packet.get_float(obj, 'lux_raw')
        x = Packet.get_float(obj, 'wind_avg_raw')
        if x is not None:
            pkt['wind_speed'] = x / 10
        x = Packet.get_float(obj, 'wind_max_raw')
        if x is not None:
            pkt['wind_gust'] = x / 10
        pkt['wind_dir'] = Packet.get_float(obj, 'wind_dir_deg')
        pkt = Packet.add_identifiers(pkt, sensor_id, EcoWittWS68Packet.__name__)
        return pkt


class EM3551HPacket(Packet):
    # The EMAX-EM3551H sensor cluster, used in the raddy weather station

    # {"time" : "2024-10-25 17:51:33", "model" : "Emax-EM3551H", "id" : 1001, "channel" : 4, "battery_ok" : 1, "temperature_F" : 55.700,"humidity" : 95, "wind_avg_km_h" : 0.000, "wind_max_km_h" : 0.000, "wind_dir_deg" : 169, "rain_mm" : 0.000, "mic" : "CHECKSUM"}

    IDENTIFIER = "Emax-EM3551H"

    @staticmethod
    def parse_json(obj):
        sensor_id = obj.get('id') # changes when the outdoor sensor is reset
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRIC
        pkt['battery'] = Packet.get_battery(obj)
        pkt['temperature'] = to_C(Packet.get_float(obj, 'temperature_F'))
        pkt['humidity'] = Packet.get_float(obj, 'humidity')
        pkt['wind_gust'] = Packet.get_float(obj, 'wind_max_km_h')
        pkt['wind_speed'] = Packet.get_float(obj, 'wind_avg_km_h')
        pkt['wind_dir'] = Packet.get_float(obj, 'wind_dir_deg')
        pkt['rain_total'] = Packet.get_float(obj, 'rain_mm')
        pkt = Packet.add_identifiers(pkt, sensor_id, EM3551HPacket.__name__)
        return pkt


class EsperanzaEWSPacket(Packet):
    # This is for a Esperanza-EWS temperature humidity sensor

    # {"time" : "2022-06-30 08:29:25", "model" : "Esperanza-EWS", "id" : 198, "channel" : 2, "temperature_F" : 69.200, "humidity" : 0, "mic" : "CRC"}

    IDENTIFIER = "Esperanza-EWS"

    @staticmethod
    def parse_json(obj):
        sensor_id = obj.get('id')
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.US
        pkt['temperature'] = Packet.get_float(obj, 'temperature_F')
        pkt['humidity'] = Packet.get_float(obj, 'humidity')
        pkt['channel'] = obj.get('channel')
        pkt = Packet.add_identifiers(pkt, sensor_id, EsperanzaEWSPacket.__name__)
        return pkt


class FOWH1080Packet(Packet):
    # 2016-09-02 22:26:05 :Fine Offset WH1080 weather station
    # Msg type: 0
    # StationID: 0026
    # Temperature: 19.9 C
    # Humidity: 78 %
    # Wind string: E
    # Wind degrees: 90
    # Wind avg speed: 0.00
    # Wind gust: 1.22
    # Total rainfall: 144.3
    # Battery: OK

    # {"time" : "2016-11-04 14:40:38", "model" : "Fine Offset WH1080 weather station", "msg_type" : 0, "id" : 38, "temperature_C" : 12.500, "humidity" : 68, "direction_str" : "E", "direction_deg" : "90", "speed" : 8.568, "gust" : 12.240, "rain" : 249.600, "battery" : "OK"}

    # this assumes rain total is in mm
    # this assumes wind speed is kph

    IDENTIFIER = "Fine Offset WH1080 weather station"
    PARSEINFO = {
#        'Msg type': ['msg_type', None, None],
        'StationID': ['station_id', None, None],
        'Temperature': [
            'temperature', re.compile('([\d.-]+) C'), lambda x: float(x)],
        'Humidity': [
            'humidity', re.compile('([\d.]+) %'), lambda x: float(x)],
#        'Wind string': ['wind_dir_ord', None, None],
        'Wind degrees': ['wind_dir', None, lambda x: int(x)],
        'Wind avg speed': ['wind_speed', None, lambda x: float(x)],
        'Wind gust': ['wind_gust', None, lambda x: float(x)],
        'Total rainfall': ['rain_total', None, lambda x: float(x)],
        'Battery': ['battery', None, lambda x: 0 if x == 'OK' else 1]}

    @staticmethod
    def parse_text(ts, payload, lines):
        pkt = dict()
        pkt['dateTime'] = ts
        pkt['usUnits'] = weewx.METRIC
        pkt.update(Packet.parse_lines(lines, FOWH1080Packet.PARSEINFO))
        return FOWH1080Packet.insert_ids(pkt)

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRIC
        pkt['station_id'] = obj.get('id')
        pkt['msg_type'] = Packet.get_int(obj, 'msg_type')
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        pkt['humidity'] = Packet.get_float(obj, 'humidity')
        pkt['wind_dir'] = Packet.get_float(obj, 'direction_deg')
        pkt['wind_speed'] = Packet.get_float(obj, 'speed')
        pkt['wind_gust'] = Packet.get_float(obj, 'gust')
        rain_total = Packet.get_float(obj, 'rain')
        if rain_total is not None:
            pkt['rain_total'] = rain_total / 10.0 # convert to cm
        pkt['battery'] = Packet.get_battery(obj)
        return FOWH1080Packet.insert_ids(pkt)

    @staticmethod
    def insert_ids(pkt):
        station_id = pkt.pop('station_id', '0000')
        return Packet.add_identifiers(pkt, station_id, FOWH1080Packet.__name__)


class FOWHx080Packet(Packet):
    # 2017-05-15 11:58:31: Fine Offset Electronics WH1080 / WH3080 Weather Station
    # Msg type: 0
    # Station ID: 236
    # Temperature: 23.9 C
    # Humidity: 48%
    # Wind string: NE
    # Wind degrees: 45
    # Wind Avg Speed: 1.22
    # Wind gust: 2.45
    # Total rainfall: 525.3
    # Battery: OK

    # 2017-05-15 12:04:48: Fine Offset Electronics WH1080 / WH3080 Weather Station
    # Msg type: 1
    # Station ID: 173
    # Signal Type: WWVB / MSF
    # Hours: 21
    # Minutes: 71
    # Seconds: 11
    # Year: 2165
    # Month: 25
    # Day: 70


    # apparently there are different identifiers for the same packet, depending
    # on which version of rtl_433 is running.  one version has extra spaces,
    # while another version does not.  so for now, and until rtl_433
    # stabilizes, match on something unique to these packets that still matches
    # the strings from different rtl_433 versions.

    # this assumes rain total is in mm (as of dec 2019)
    # this assumes wind speed is kph (as of dec 2019)

    # {"time" : "2020-10-13 14:04:48", "model" : "Fine Offset Electronics WH1080/WH3080 Weather Station", "msg_type" : 0, "id" : 14, "battery" : "OK", "temperature_C" : 24.400, "humidity" : 35, "direction_deg" : 225, "speed" : 0.000, "gust" : 0.000, "rain" : 41.400, "mic" : "CRC"}
    # todays rtl_433 output
    # {"time" : "2020-10-13 14:04:48", "model" : "Fineoffset-WHx080", "subtype" : 0, "id" : 14, "battery_ok" : 1, "temperature_C" : 24.400, "humidity" : 35, "wind_dir_deg" : 225, "wind_avg_km_h" : 0.000, "wind_max_km_h" : 0.000, "rain_mm" : 41.400, "mic" : "CRC"}

    #{"time" : "2022-08-17 15:58:42", "model" : "Fineoffset-WHx080", "subtype" : 0, "id" : 14, "battery_ok" : 1, "temperature_C" : 28.100, "humidity" : 36, "wind_dir_deg" : 338, "wind_avg_km_h" : 0.000, "wind_max_km_h" : 1.224, "rain_mm" : 614.400, "mic" : "CRC"}
    #{"time" : "2022-08-14 17:22:30", "model" : "Fineoffset-WHx080", "subtype" : 2, "uv_sensor_id" : 225, "uv_status" : "OK", "uv_index" : 1, "lux" : 2223.200, "wm" : 3.255, "mic" : "CRC"}

    #IDENTIFIER = "Fine Offset Electronics WH1080 / WH3080 Weather Station"
    #IDENTIFIER = "Fine Offset Electronics WH1080/WH3080 Weather Station"
    #IDENTIFIER = "Fine Offset Electronics WH1080"
    IDENTIFIER = "Fineoffset-WHx080"

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRIC
        msg_type = obj.get('subtype')
        pkt['msg_type'] = msg_type

        if msg_type == 0:
            pkt['station_id'] = obj.get('id')
            pkt['battery'] = Packet.get_battery(obj)
            pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
            pkt['humidity'] = Packet.get_float(obj, 'humidity')
            pkt['wind_dir'] = Packet.get_float(obj, 'wind_dir_deg')
            pkt['wind_speed'] = Packet.get_float(obj, 'wind_avg_km_h')
            pkt['wind_gust'] = Packet.get_float(obj, 'wind_max_km_h')
            rain_total = Packet.get_float(obj, 'rain_mm')
            if rain_total is not None:
                pkt['rain_total'] = rain_total / 10.0 # convert to cm

        if msg_type == 2:
            pkt['station_id'] = obj.get('uv_sensor_id')
            pkt['uv_status'] = 0 if obj.get('uv_status') == 'OK' else 1
            pkt['uv_index'] = Packet.get_float(obj, 'uv_index')
            pkt['luminosity'] = Packet.get_float(obj, 'lux')
            pkt['radiation'] = Packet.get_float(obj, 'wm')

        return FOWHx080Packet.insert_ids(pkt)

    @staticmethod
    def insert_ids(pkt):
        station_id = pkt.pop('station_id', '0000')
        return Packet.add_identifiers(pkt, station_id, FOWHx080Packet.__name__)


class FOWH3080Packet(Packet):
    # 2017-05-15 11:58:08: Fine Offset Electronics WH3080 Weather Station
    # Msg type: 2
    # UV Sensor ID: 225
    # Sensor Status: OK
    # UV Index: 8
    # Lux: 120160.5
    # Watts / m: 175.93
    # Foot-candles: 11167.33

    # {"time" : "2017-05-15 17:21:07", "model" : "Fine Offset Electronics WH3080 Weather Station", "msg_type" : 2, "uv_sensor_id" : 225, "uv_status" : "OK", "uv_index" : 1, "lux" : 7837.000, "wm" : 11.474, "fc" : 728.346}

    IDENTIFIER = "Fine Offset Electronics WH3080 Weather Station"

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRIC
        pkt['station_id'] = obj.get('uv_sensor_id')
        pkt['msg_type'] = Packet.get_int(obj, 'msg_type')
        pkt['uv_index'] = Packet.get_float(obj, 'uv_index')
        pkt['luminosity'] = Packet.get_float(obj, 'lux')
        pkt['radiation'] = Packet.get_float(obj, 'wm')
        pkt['illumination'] = Packet.get_float(obj, 'fc')
        pkt['uv_status'] = 0 if obj.get('uv_status') == 'OK' else 1
        return FOWH3080Packet.insert_ids(pkt)

    @staticmethod
    def insert_ids(pkt):
        station_id = pkt.pop('station_id', '0000')
        return Packet.add_identifiers(pkt, station_id, FOWH3080Packet.__name__)


class FOWH2Packet(Packet):
    # {"time" : "2018-08-29 17:08:33", "model" : "Fine Offset Electronics, WH2 Temperature/Humidity sensor", "id" : 129, "temperature_C" : 24.200, "mic" : "CRC"}

    IDENTIFIER = "Fine Offset Electronics, WH2"
    PARSEINFO = {
        'ID': ['station_id', None, lambda x: int(x)],
        'Temperature':
            ['temperature', re.compile('([\d.-]+) C'), lambda x: float(x)]
        }

    @staticmethod
    def parse_text(ts, payload, lines):
        pkt = dict()
        pkt['dateTime'] = ts
        pkt['usUnits'] = weewx.METRIC
        pkt.update(Packet.parse_lines(lines, FOWH2Packet.PARSEINFO))
        return FOWH2Packet.insert_ids(pkt)

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRIC
        pkt['station_id'] = obj.get('id')
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        return FOWH2Packet.insert_ids(pkt)

    @staticmethod
    def insert_ids(pkt):
        station_id = pkt.pop('station_id', '0000')
        return Packet.add_identifiers(pkt, station_id, FOWH2Packet.__name__)


class FOWH5Packet(Packet):
    # {"time" : "2019-10-27 14:51:21", "model" : "Fine Offset WH5 sensor", "id" : 48, "temperature_C" : 11.700, "humidity" : 62, "mic" : "CRC"}

    IDENTIFIER = "Fine Offset WH5 sensor"
    PARSEINFO = {
        'ID': ['station_id', None, lambda x: int(x)],
        'Temperature': ['temperature', re.compile('([\d.-]+) C'), lambda x: float(x)]
    }

    @staticmethod
    def parse_text(ts, payload, lines):
        pkt = dict()
        pkt['dateTime'] = ts
        pkt['usUnits'] = weewx.METRIC
        pkt.update(Packet.parse_lines(lines, FOWH5Packet.PARSEINFO))
        return FOWH5Packet.insert_ids(pkt)

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRIC
        pkt['station_id'] = obj.get('id')
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        pkt['humidity'] = Packet.get_float(obj, 'humidity')
        return FOWH5Packet.insert_ids(pkt)

    @staticmethod
    def insert_ids(pkt):
        station_id = pkt.pop('station_id', '0000')
        return Packet.add_identifiers(pkt, station_id, FOWH5Packet.__name__)


class FOWH24Packet(Packet):
    # This is for a WH24 which is the sensor array for several station models

    # {"time" : "2019-02-11 03:44:32", "model" : "Fine Offset WH24", "id" : 140, "temperature_C" : 12.600, "humidity" : 80, "wind_dir_deg" : 111, "wind_speed_ms" : 0.280, "gust_speed_ms" : 1.120, "rainfall_mm" : 1150.800, "uv" : 1, "uvi" : 0, "light_lux" : 0.000, "battery" : "OK", "mic" : "CRC"}
    # {"time" : "2019-02-11 03:44:48", "model" : "Fine Offset WH24", "id" : 140, "temperature_C" : 12.600, "humidity" : 80, "wind_dir_deg" : 109, "wind_speed_ms" : 0.980, "gust_speed_ms" : 1.120, "rainfall_mm" : 1150.800, "uv" : 1, "uvi" : 0, "light_lux" : 0.000, "battery" : "OK", "mic" : "CRC"}

    IDENTIFIER = "Fine Offset WH24"

    @staticmethod
    def parse_json(obj):
        sensor_id = obj.get('id')
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRICWX
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        pkt['humidity'] = Packet.get_float(obj, 'humidity')
        pkt['wind_dir'] = Packet.get_float(obj, 'wind_dir_deg')
        pkt['wind_speed'] = Packet.get_float(obj, 'wind_speed_ms')
        pkt['wind_gust'] = Packet.get_float(obj, 'gust_speed_ms')
        pkt['rain_total'] = Packet.get_float(obj, 'rainfall_mm')
        pkt['uv_index'] = Packet.get_float(obj, 'uvi')
        pkt['light'] = Packet.get_float(obj, 'light_lux')
        pkt['battery'] = Packet.get_battery(obj)
        pkt = Packet.add_identifiers(pkt, sensor_id, FOWH24Packet.__name__)
        return pkt


class FOWH24BPacket(Packet):
    # different mappings for the WH24 sensor

    # {"time" : "2020-08-01 14:03:52", "model" : "Fineoffset-WH24", "id" : 247, "battery_ok" : 1, "temperature_C" : 30.600, "humidity" : 45, "wind_dir_deg" : 149, "wind_avg_m_s" : 0.000, "wind_max_m_s" : 0.000, "rain_mm" : 6.600, "uv" : 783, "uvi" : 1, "light_lux" : 28025.000, "mic" : "CRC"}

    IDENTIFIER = "Fineoffset-WH24"

    @staticmethod
    def parse_json(obj):
        sensor_id = obj.get('id')
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRICWX
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        pkt['humidity'] = Packet.get_float(obj, 'humidity')
        pkt['wind_dir'] = Packet.get_float(obj, 'wind_dir_deg')
        pkt['wind_speed'] = Packet.get_float(obj, 'wind_avg_m_s')
        pkt['wind_gust'] = Packet.get_float(obj, 'wind_max_m_s')
        pkt['rain_total'] = Packet.get_float(obj, 'rain_mm')
        pkt['uv_index'] = Packet.get_float(obj, 'uvi')
        pkt['light'] = Packet.get_float(obj, 'light_lux')
        pkt['battery'] = Packet.get_battery(obj)
        pkt = Packet.add_identifiers(pkt, sensor_id, FOWH24BPacket.__name__)
        return pkt


class FOWH25Packet(Packet):
    # 2016-09-02 22:26:05 :   Fine Offset Electronics, WH25
    # ID:     239
    # Temperature: 19.9 C
    # Humidity: 78 %
    # Pressure: 1007.9 hPa
    #
    # 2018-10-09 19:45:12 :   Fine Offset Electronics, WH25
    # id : 21
    # temperature_C : 20.900
    # humidity : 65
    # pressure_hPa : 980.400
    # battery : OK
    # mic : CHECKSUM

    # {"time" : "2017-03-25 05:33:57", "model" : "Fine Offset Electronics, WH25", "id" : 239, "temperature_C" : 30.200, "humidity" : 68, "pressure" : 1008.000}
    # {"time" : "2018-10-10 13:37:11", "model" : "Fine Offset Electronics, WH25", "id" : 21, "temperature_C" : 21.600, "humidity" : 66, "pressure_hPa" : 972.800, "battery" : "OK", "mic" : "CHECKSUM"}
    # {"time" : "2020-10-13 23:29:35", "model" : "Fineoffset-WH25", "id" : 170, "battery_ok" : 0, "temperature_C" : 26.200, "humidity" : 36, "pressure_hPa" : 1009.900, "mic" : "CRC"}

    # {"time" : "2021-04-08 18:11:01", "model" : "Fineoffset-WH25", "id" : 121, "battery_ok" : 1, "temperature_C" : 20.000, "humidity" : 48, "pressure_hPa" : 979.100, "mic" : "CRC"}
    # {"time" : "2020-08-01 14:03:16", "model" : "Fineoffset-WH25", "id" : 19, "battery_ok" : 1, "temperature_C" : 26.100, "humidity" : 49, "pressure_hPa" : 987.800, "mic" : "CRC"}

    IDENTIFIER = "Fineoffset-WH25"

    PARSEINFO = {
        'ID': ['station_id', None, lambda x: int(x)],
        'Temperature':
            ['temperature', re.compile('([\d.-]+) C'), lambda x: float(x)],
        'Humidity': ['humidity', re.compile('([\d.]+) %'), lambda x: float(x)],
        'Pressure':
            ['pressure', re.compile('([\d.-]+) hPa'), lambda x: float(x)]}

    @staticmethod
    def parse_text(ts, payload, lines):
        pkt = dict()
        pkt['dateTime'] = ts
        pkt['usUnits'] = weewx.METRIC
        pkt.update(Packet.parse_lines(lines, FOWH25Packet.PARSEINFO))
        return FOWH25Packet.insert_ids(pkt)

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRIC
        pkt['station_id'] = obj.get('id')
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        pkt['humidity'] = Packet.get_float(obj, 'humidity')
        pkt['pressure'] = Packet.get_float(obj, 'pressure_hPa')
        pkt['battery'] = Packet.get_battery(obj)
        return FOWH25Packet.insert_ids(pkt)

    @staticmethod
    def insert_ids(pkt):
        station_id = pkt.pop('station_id', '0000')
        return Packet.add_identifiers(pkt, station_id, FOWH25Packet.__name__)


class FOWH32Packet(Packet):

    # {'time': '2024-03-04 17:41:55', 'model': 'Fineoffset-WH32', 'id': 35, 'battery_ok': 1, 'temperature_C': 3.2, 'humidity': 91, 'mic': 'CRC'}

    IDENTIFIER = "Fineoffset-WH32"

    @staticmethod
    def parse_json(obj):
        sensor_id = obj.get('id')
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRICWX
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        pkt['humidity'] = Packet.get_float(obj, 'humidity')
        pkt['battery'] = Packet.get_battery(obj)
        pkt['channel'] = Packet.get_int(obj, 'channel')
        pkt = Packet.add_identifiers(pkt, sensor_id, FOWH32Packet.__name__)
        return pkt


class FOWH32BPacket(Packet):
    # This is for a WH32B which is the indoors sensor array for an Ambient
    # Weather WS-2902A. The same sensor array is used for several models.

    # time      : 2019-04-08 00:48:02
    # model     : Fineoffset-WH32B
    # ID        : 146
    # Temperature: 17.5 C
    # Humidity  : 60 %
    # Pressure  : 1001.2 hPa
    # Battery   : OK
    # Integrity : CHECKSUM

    # {"time" : "2019-04-08 07:06:03", "model" : "Fineoffset-WH32B", "id" : 146, "temperature_C" : 16.900, "humidity" : 59, "pressure_hPa" : 1001.300, "battery" : "OK", "mic" : "CHECKSUM"}
    # {"time" : "2022-03-24 02:27:27", "model" : "Fineoffset-WH32B", "id" : 114, "battery_ok" : 1, "temperature_C" : 20.700, "humidity" : 49, "pressure_hPa" : 960.300, "mic" : "CRC", "mod" : "FSK", "freq1" : 914.964, "freq2" : 915.026, "rssi" : -0.118, "snr" : 20.295, "noise" : -20.412}

    IDENTIFIER = "Fineoffset-WH32B"

    @staticmethod
    def parse_json(obj):
        sensor_id = obj.get('id')
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRIC
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        pkt['humidity'] = Packet.get_float(obj, 'humidity')
        pkt['pressure'] = Packet.get_float(obj, 'pressure_hPa')
        pkt['battery'] = Packet.get_battery(obj)
        pkt['freq1'] = Packet.get_float(obj, 'freq1')
        pkt['freq2'] = Packet.get_float(obj, 'freq2')
        pkt['rssi'] = Packet.get_float(obj, 'rssi')
        pkt['snr'] = Packet.get_float(obj, 'snr')
        pkt['noise'] = Packet.get_float(obj, 'noise')
        pkt = Packet.add_identifiers(pkt, sensor_id, FOWH32BPacket.__name__)
        return pkt


class FOWH45Packet(Packet):
    # This is for a WH45 Air Quality Monitor

    #{"time" : "2023-07-08 13:06:14", "model" : "Fineoffset-WH45", "id" : 18034, "battery_ok" : 1.000, "temperature_C" : 20.400, "humidity" : 84, "pm2_5_ug_m3" : 1.800, "pm10_ug_m3" : 1.800, "co2_ppm" : 718, "ext_power" : 1, "mic" : "CRC"}

    IDENTIFIER = "Fineoffset-WH45"

    @staticmethod
    def parse_json(obj):
        sensor_id = obj.get('id')
        pkt = dict()
        pkt['usUnits'] = weewx.METRIC
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['battery'] = Packet.get_battery(obj)
        pkt['co2_atm'] = Packet.get_float(obj, 'co2_ppm')
        pkt['pm2_5_atm'] = Packet.get_float(obj, 'pm2_5_ug_m3')
        pkt['pm10_0_atm'] = Packet.get_float(obj, 'pm10_ug_m3')
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        pkt['humidity'] = Packet.get_float(obj, 'humidity')
        pkt = Packet.add_identifiers(pkt, sensor_id, FOWH45Packet.__name__)
        return pkt


class FOWH51Packet(Packet):
    # This is for a WH051 Soil Moisture Sensor (Fine Offset / Ecowitt WH51)
    #{"time" : "2021-04-15 15:07:05", "model" : "Fineoffset-WH51", "id" : "00df73", "battery_ok" : 1.000, "battery_mV" : 1600, "moisture" : 0, "boost" : 0, "ad_raw" : 17, "mic" : "CRC", "mod" : "FSK", "freq1" : 915.024, "freq2" : 914.970, "rssi" : -2.258, "snr" : 35.115, "noise" : -37.373}

    IDENTIFIER = "Fineoffset-WH51"

    @staticmethod
    def parse_json(obj):
        sensor_id = obj.get('id')
        pkt = dict()
        pkt['usUnits'] = weewx.METRIC
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['soil_moisture_percent'] = Packet.get_float(obj, 'moisture')
        pkt['boost'] = Packet.get_float(obj, 'boost')
        pkt['soil_moisture_raw'] = Packet.get_float(obj, 'ad_raw')
        pkt['freq1'] = Packet.get_float(obj, 'freq1')
        pkt['freq2'] = Packet.get_float(obj, 'freq2')
        pkt['battery'] = Packet.get_battery(obj)
        pkt['battery_mV'] = Packet.get_float(obj, 'battery_mV')
        pkt['snr'] = Packet.get_float(obj, 'snr')
        pkt['rssi'] = Packet.get_float(obj, 'rssi')
        pkt['noise'] = Packet.get_float(obj, 'noise')
        pkt = Packet.add_identifiers(pkt, sensor_id, FOWH51Packet.__name__)
        return pkt


class FOWH65BPacket(Packet):
    # This is for a WH65B which is the sensor array for an Ambient Weather
    # WS-2902A. The same sensor array is used for several models.

    # 2018-10-10 13:37:02 :   Fine Offset WH65B
    # id : 89
    # temperature_C : 17.600
    # humidity : 93
    # wind_dir_deg : 224
    # wind_speed_ms : 1.540
    # gust_speed_ms : 2.240
    # rainfall_mm : 325.500
    # uv : 130
    # uvi : 0
    # light_lux : 13454.000
    # battery : OK
    # mic : CRC

    # {"time" : "2018-10-10 13:37:02", "model" : "Fine Offset WH65B", "id" : 89, "temperature_C" : 17.600, "humidity" : 93, "wind_dir_deg" : 224, "wind_speed_ms" : 1.540, "gust_speed_ms" : 2.240, "rainfall_mm" : 325.500, "uv" : 130, "uvi" : 0, "light_lux" : 13454.000, "battery" : "OK", "mic" : "CRC"}

    IDENTIFIER = "Fine Offset WH65B"

    @staticmethod
    def parse_json(obj):
        sensor_id = obj.get('id')
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRICWX
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        pkt['humidity'] = Packet.get_float(obj, 'humidity')
        pkt['wind_dir'] = Packet.get_float(obj, 'wind_dir_deg')
        pkt['wind_speed'] = Packet.get_float(obj, 'wind_speed_ms')
        pkt['wind_gust'] = Packet.get_float(obj, 'gust_speed_ms')
        pkt['rain_total'] = Packet.get_float(obj, 'rainfall_mm')
        pkt['uv'] = Packet.get_float(obj, 'uv')
        pkt['uv_index'] = Packet.get_float(obj, 'uvi')
        pkt['light'] = Packet.get_float(obj, 'light_lux')
        pkt['battery'] = Packet.get_battery(obj)
        pkt = Packet.add_identifiers(pkt, sensor_id, FOWH65BPacket.__name__)
        return pkt


class FOWH65BAltPacket(Packet):
    # This is for a WH65B sensor array that identifies itself as
    # Fineoffset-WH65B. Several mappings are also different from the other
    # WH65B. This configuration was tested on an Ambient Weather WS-2902A kit.

    # time : 2020-04-26 23:21:42
    # model : Fineoffset-WH65B
    # id : 16
    # temperature_C : 15.400
    # humidity : 51
    # wind_dir_deg : 323
    # wind_avg_m_s : 1.020
    # wind_max_m_s : 2.040
    # rain_mm : 76.453
    # uv : 701
    # uvi : 2
    # light_lux : 14616.000
    # battery_ok : OK
    # mic : CRC

    # {"time" : "2020-04-26 19:41:10", "model" : "Fineoffset-WH65B", "id" : 16, "battery_ok" : 1, "temperature_C" : 14.800, "humidity" : 50, "wind_dir_deg" : 336, "wind_avg_m_s" : 1.658, "wind_max_m_s" : 3.060, "rain_mm" : 76.454, "uv" : 1982, "uvi" : 4, "light_lux" : 69130.000, "mic" : "CRC"}
    # {"time" : "2020-07-22 04:47:47", "model" : "Fineoffset-WH65B", "id" : 73, "battery_ok" : 1, "temperature_C" : 24.900, "humidity" : 53, "wind_dir_deg" : 21, "wind_avg_m_s" : 0.000, "wind_max_m_s" : 0.000, "rain_mm" : 7.874, "uv" : 1, "uvi" : 0, "light_lux" : 0.000, "mic" : "CRC"}
    # {"time" : "2022-03-24 02:27:26", "model" : "Fineoffset-WH65B", "id" : 86, "battery_ok" : 1, "temperature_C" : 2.400, "humidity" : 94, "wind_dir_deg" : 268, "wind_avg_m_s" : 0.701, "wind_max_m_s" : 1.020, "rain_mm" : 2411.222, "uv" : 2, "uvi" : 0, "light_lux" : 0.000, "mic" : "CRC", "mod" : "FSK", "freq1" : 914.965, "freq2" : 915.019, "rssi" : -0.120, "snr" : 20.011, "noise" : -20.130}

    IDENTIFIER = "Fineoffset-WH65B"

    @staticmethod
    def parse_json(obj):
        sensor_id = obj.get('id')
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRICWX
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        pkt['humidity'] = Packet.get_float(obj, 'humidity')
        pkt['wind_dir'] = Packet.get_float(obj, 'wind_dir_deg')
        pkt['wind_speed'] = Packet.get_float(obj, 'wind_avg_m_s')
        pkt['wind_gust'] = Packet.get_float(obj, 'wind_max_m_s')
        pkt['rain_total'] = Packet.get_float(obj, 'rain_mm')
        pkt['uv'] = Packet.get_float(obj, 'uv') # superfluous?
        pkt['uv_index'] = Packet.get_float(obj, 'uvi')
        pkt['light'] = Packet.get_float(obj, 'light_lux')
        pkt['battery'] = Packet.get_battery(obj)
        pkt['rssi'] = Packet.get_float(obj, 'rssi')
        pkt['snr'] = Packet.get_float(obj, 'snr')
        pkt['noise'] = Packet.get_float(obj, 'noise')
        pkt = Packet.add_identifiers(pkt, sensor_id, FOWH65BAltPacket.__name__)
        return pkt


class FOWH0290Packet(Packet):
    # This is for a WH0290 Air Quality Monitor (Ambient Weather PM25)

    #{"time" : "@0.084044s", "model" : "Fine Offset Electronics, WH0290", "id" : 204, "pm2_5_ug_m3" : 9, "pm10_0_ug_m3" : 10, "mic" : "CHECKSUM"}
    #{"time": "2022-09-08 19:48:38", "model": "Endoffset-WH0290", " id ": 142," battery_ok ": 0.800," pm2_5_ug_m3 ": 2," estimated_pm10_0_ug_m3 ": 2," family ": 65," unknown1 ": 0," mic ":" CRC "}

    IDENTIFIER = "Fineoffset-WH0290"

    @staticmethod
    def parse_json(obj):
        sensor_id = obj.get('id')
        pkt = dict()
        pkt['usUnits'] = weewx.METRIC
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['battery'] = Packet.get_battery(obj)
        pkt['pm2_5_atm'] = Packet.get_float(obj, 'pm2_5_ug_m3')
        pkt['pm10_0_atm'] = Packet.get_float(obj, 'estimated_pm10_0_ug_m3')
        pkt = Packet.add_identifiers(pkt, sensor_id, FOWH0290Packet.__name__)
        return pkt


class FOWH31LPacket(Packet):
    # This is for a WH31L lightning detector

    #{"time" : "2021-06-30 20:37:11", "model" : "FineOffset-WH31L", "id" : 67016, "battery_ok" : 0, "state" : 8, "flags" : 56, "storm_dist_km" : 10, "strike_count" : 2, "mic" : "CRC"}

    IDENTIFIER = "FineOffset-WH31L"

    @staticmethod
    def parse_json(obj):
        sensor_id = obj.get('id')
        pkt = dict()
        pkt['usUnits'] = weewx.METRIC
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['battery'] = Packet.get_battery(obj)
        pkt['strikes_total'] = obj.get('strike_count')
        pkt['distance'] = obj.get('storm_dist_km')
        pkt['flags'] = obj.get('flags')
        pkt['state'] = obj.get('state')
        pkt = Packet.add_identifiers(pkt, sensor_id, FOWH31LPacket.__name__)
        return pkt


class FOWS80Packet(Packet):
    # This is for a Fine Offset Electronics WS80 weather station

    #{"time" : "2022-07-06 21:06:18", "model" : "Fineoffset-WS80", "id" : 589862, "battery_ok" : 1.170, "battery_mV" : 3280, "temperature_C" : 17.700, "humidity" : 67, "wind_dir_deg" : 268, "wind_avg_m_s" : 1.300, "wind_max_m_s" : 1.800, "uvi" : 0.000, "light_lux" : 0.000, "flags" : 170, "mic" : "CRC"}

    IDENTIFIER = "Fineoffset-WS80"

    @staticmethod
    def parse_json(obj):
        sensor_id = obj.get('id')
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRICWX
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        pkt['humidity'] = Packet.get_float(obj, 'humidity')
        pkt['wind_dir'] = Packet.get_float(obj, 'wind_dir_deg')
        pkt['wind_speed'] = Packet.get_float(obj, 'wind_avg_m_s')
        pkt['wind_gust'] = Packet.get_float(obj, 'wind_max_m_s')
        pkt['rain_total'] = Packet.get_float(obj, 'rainfall_mm')
        pkt['uv_index'] = Packet.get_float(obj, 'uvi')
        pkt['light'] = Packet.get_float(obj, 'light_lux')
        #pkt['battery'] = 0 if obj.get('battery_ok') == 1 else 1
        pkt['voltage'] = to_v(Packet.get_float(obj, 'battery_mV'))
        pkt = Packet.add_identifiers(pkt, sensor_id, FOWS80Packet.__name__)
        return pkt


class FOWS90Packet(Packet):
    # time : 2020-04-26 23:21:42
    # model : Fineoffset-WS90
    # id : 16
    # temperature_C : 15.400
    # humidity : 51
    # wind_dir_deg : 323
    # wind_avg_m_s : 1.020
    # wind_max_m_s : 2.040
    # rain_mm : 76.453
    # uvi : 2
    # light_lux : 14616.000
    # supercap_V: 3.200
    # battery_ok: OK
    # battery_mV: 3280
    # mic : CRC

    # {"time" : "2023-03-08 22:00:38", "model" : "Fineoffset-WS90", "id" : 13355, "battery_ok" : 1.0, "battery_mV" : 3280, "temperature_C" : 5.700, "humidity" : 75, "wind_dir_deg" : 87, "wind_avg_m_s" : 1.300, "wind_max_m_s" : 1.600, "uvi" : 0.000, "light_lux" : 55300.000, "flags" : 129, "rain_mm" : 12.800, "supercap_V" : 3.200, "data" : "01c00000192000fe7ff0ff0082", "mic" : "CRC", "mod" : "FSK", "freq1" : 914.945, "freq2" : 915.039, "rssi" : -0.123, "snr" : 32.990, "noise" : -33.113}

    IDENTIFIER = "Fineoffset-WS90"

    @staticmethod
    def parse_json(obj):
        sensor_id = obj.get('id')
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRICWX
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        pkt['humidity'] = Packet.get_float(obj, 'humidity')
        pkt['wind_dir'] = Packet.get_float(obj, 'wind_dir_deg')
        pkt['wind_speed'] = Packet.get_float(obj, 'wind_avg_m_s')
        pkt['wind_gust'] = Packet.get_float(obj, 'wind_max_m_s')
        pkt['rain_total'] = Packet.get_float(obj, 'rain_mm')
        pkt['uv_index'] = Packet.get_float(obj, 'uvi')
        pkt['light'] = Packet.get_float(obj, 'light_lux') # superfluous?
        pkt['battery'] = Packet.get_battery(obj)
        v = Packet.get_float(obj, 'battery_mV')
        if v is not None:
            v = round( v * .001, 2 )
            pkt['supplyVoltage'] = v
        pkt['referenceVoltage'] = Packet.get_float(obj, 'supercap_V')
        pkt['freq1'] = Packet.get_float(obj, 'freq1')
        pkt['freq2'] = Packet.get_float(obj, 'freq2')
        pkt['rssi'] = Packet.get_float(obj, 'rssi')
        pkt['snr'] = Packet.get_float(obj, 'snr')
        pkt['noise'] = Packet.get_float(obj, 'noise')
        pkt = Packet.add_identifiers(pkt, sensor_id, FOWS90Packet.__name__)
        return pkt


class FOWN34Packet(Packet):
    # time: 2025-06-27 12:20:15,
    # model: Fineoffset-WN34,
    # id: 34320,
    # battery_ok: 0.750,
    # battery_mV: 1420,
    # temperature_C: 26.600,
    # mic: CRC

    # {"time" : "2025-06-27 12:20:15", "model" : "Fineoffset-WN34", "id" : 34320, "battery_ok" : 0.750, "battery_mV" : 1420, "temperature_C" : 26.600, "mic" : "CRC"}

    IDENTIFIER = "Fineoffset-WN34"

    @staticmethod
    def parse_json(obj):
        sensor_id = obj.get('id')
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRICWX
        pkt['battery'] = Packet.get_battery(obj)
        v = Packet.get_float(obj, 'battery_mV')
        if v is not None:
            v = round( v * .001, 2 )
            pkt['supplyVoltage'] = v
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        pkt['freq1'] = Packet.get_float(obj, 'freq1')
        pkt['freq2'] = Packet.get_float(obj, 'freq2')
        pkt['rssi'] = Packet.get_float(obj, 'rssi')
        pkt['snr'] = Packet.get_float(obj, 'snr')
        pkt['noise'] = Packet.get_float(obj, 'noise')
        pkt = Packet.add_identifiers(pkt, sensor_id, FOWN34Packet.__name__)
        return pkt


class Hideki(object):
    @staticmethod
    def insert_ids(pkt, pkt_type):
        channel = pkt.pop('channel', 0)
        code = pkt.pop('rolling_code', 0)
        sensor_id = "%s:%s" % (channel, code)
        pkt = Packet.add_identifiers(pkt, sensor_id, pkt_type)
        return pkt


class HidekiTS04Packet(Packet):
    # 2016-08-31 17:41:30 :   HIDEKI TS04 sensor
    # Rolling Code: 9
    # Channel: 1
    # Battery: OK
    # Temperature: 27.30 C
    # Humidity: 60 %

    # {"time" : "2016-11-04 14:44:37", "model" : "HIDEKI TS04 sensor", "rc" : 9, "channel" : 1, "battery" : "OK", "temperature_C" : 12.400, "humidity" : 61}
    # {"time" : "2020-10-15 07:13:33", "model" : "Hideki-TS04", "id" : 14, "channel" : 1, "battery_ok" : 1, "temperature_C" : 20.700, "humidity" : 10, "mic" : "CRC"}

    IDENTIFIER = "Hideki-TS04"
    PARSEINFO = {
        'Rolling Code': ['rolling_code', None, lambda x: int(x)],
        'Channel': ['channel', None, lambda x: int(x)],
        'Battery': ['battery', None, lambda x: 0 if x == 'OK' else 1],
        'Temperature': [
            'temperature', re.compile('([\d.-]+) C'), lambda x: float(x)],
        'Humidity': ['humidity', re.compile('([\d.]+) %'), lambda x: float(x)]}

    @staticmethod
    def parse_text(ts, payload, lines):
        pkt = dict()
        pkt['dateTime'] = ts
        pkt['usUnits'] = weewx.METRIC
        pkt.update(Packet.parse_lines(lines, HidekiTS04Packet.PARSEINFO))
        return Hideki.insert_ids(pkt, HidekiTS04Packet.__name__)

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRIC
        pkt['rolling_code'] = obj.get('id')
        pkt['channel'] = obj.get('channel')
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        pkt['humidity'] = Packet.get_float(obj, 'humidity')
        pkt['battery'] = Packet.get_battery(obj)
        return Hideki.insert_ids(pkt, HidekiTS04Packet.__name__)


class HidekiWindPacket(Packet):
    # 2017-01-16 05:39:42 : HIDEKI Wind sensor
    # Rolling Code: 0
    # Channel: 4
    # Battery: OK
    # Temperature: -5.0 C
    # Wind Strength: 2.57 km/h
    # Direction: 45.0 \xc2\xb0

    # {"time" : "2017-01-16 04:38:39", "model" : "HIDEKI Wind sensor", "rc" : 0, "channel" : 4, "battery" : "OK", "temperature_C" : -4.400, "windstrength" : 2.897, "winddirection" : 292.500}
    # {"time" : "2019-11-24 19:13:41", "model" : "HIDEKI Wind sensor", "rc" : 3, "channel" : 4, "battery" : "OK", "temperature_C" : 11.000, "wind_speed_mph" : 1.300, "gust_speed_mph" : 0.100, "wind_approach" : 1, "wind_direction" : 270.000, "mic" : "CRC"}
    # {"time" : "2021-02-07 03:44:54", "model" : "Hideki-Wind", "id" : 8, "channel" : 4, "battery_ok" : 1, "temperature_C" : 15.200, "wind_avg_mi_h" : 2.600, "wind_max_mi_h" : 2.900, "wind_approach" : 1, "wind_dir_deg" : 337.500, "mic" : "CRC"}

#    IDENTIFIER = "HIDEKI Wind sensor"
    IDENTIFIER = "Hideki-Wind"

    PARSEINFO = {
        'Rolling Code': ['rolling_code', None, lambda x: int(x)],
        'Channel': ['channel', None, lambda x: int(x)],
        'Battery': ['battery', None, lambda x: 0 if x == 'OK' else 1],
        'Temperature': [
            'temperature', re.compile('([\d.-]+) C'), lambda x: float(x)],
        'Wind Strength': ['wind_speed', re.compile('([\d.]+) km/h'), lambda x: float(x)],
        'Direction': ['wind_dir', re.compile('([\d.]+) '), lambda x: float(x)]}

    @staticmethod
    def parse_text(ts, payload, lines):
        pkt = dict()
        pkt['dateTime'] = ts
        pkt['usUnits'] = weewx.METRIC
        pkt.update(Packet.parse_lines(lines, HidekiWindPacket.PARSEINFO))
        return Hideki.insert_ids(pkt, HidekiWindPacket.__name__)

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRIC
        pkt['rolling_code'] = obj.get('id')
        pkt['channel'] = obj.get('channel')
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        if 'wind_avg_mi_h' in obj:
            v = Packet.get_float(obj, 'wind_avg_mi_h')
            if v is not None:
                v /= weewx.units.MILE_PER_KM
            pkt['wind_speed'] = v
        elif 'wind_speed_mph' in obj:
            v = Packet.get_float(obj, 'wind_speed_mph')
            if v is not None:
                v /= weewx.units.MILE_PER_KM
            pkt['wind_speed'] = v
        else:
            pkt['wind_speed'] = Packet.get_float(obj, 'windstrength')
        if 'wind_dir_deg' in obj:
            pkt['wind_dir'] = Packet.get_float(obj, 'wind_dir_deg')
        elif 'wind_direction' in obj:
            pkt['wind_dir'] = Packet.get_float(obj, 'wind_direction')
        else:
            pkt['wind_dir'] = Packet.get_float(obj, 'winddirection')
        if 'wind_max_mi_h' in obj:
            v = Packet.get_float(obj, 'wind_max_mi_h')
            if v is not None:
                v /= weewx.units.MILE_PER_KM
            pkt['wind_gust'] = v
        elif 'gust_speed_mph' in obj:
            v = Packet.get_float(obj, 'gust_speed_mph')
            if v is not None:
                v /= weewx.units.MILE_PER_KM
            pkt['wind_gust'] = v
        pkt['battery'] = Packet.get_battery(obj)
        return Hideki.insert_ids(pkt, HidekiWindPacket.__name__)


class HidekiRainPacket(Packet):
    # 2017-01-16 05:39:42 : HIDEKI Rain sensor
    # Rolling Code: 0
    # Channel: 4
    # Battery: OK
    # Rain: 2622.900

    # {"time" : "2017-01-16 04:38:50", "model" : "HIDEKI Rain sensor", "rc" : 0, "channel" : 4, "battery" : "OK", "rain" : 2622.900}
    # {"time" : "2019-11-24 19:13:52", "model" : "HIDEKI Rain sensor", "rc" : 0, "channel" : 4, "battery" : "OK", "rain_mm" : 274.400, "mic" : "CRC"}
    # {"time" : "2021-02-07 03:45:10", "model" : "Hideki-Rain", "id" : 0, "channel" : 4, "battery_ok" : 1, "rain_mm" : 1382.500, "mic" : "CRC"}
    # {"time" : "2023-11-10 14:42:06", "model" : "Hideki-Rain", "id" : 0, "channel" : 4, "battery_ok" : 1, "rain_mm" : 2255.400, "mic" : "CRC"}

#    IDENTIFIER = "HIDEKI Rain sensor"
    IDENTIFIER = "Hideki-Rain"

    PARSEINFO = {
        'Rolling Code': ['rolling_code', None, lambda x: int(x)],
        'Channel': ['channel', None, lambda x: int(x)],
        'Battery': ['battery', None, lambda x: 0 if x == 'OK' else 1],
        'Rain': ['rain_total', re.compile('([\d.]+) '), lambda x: float(x)]}

    @staticmethod
    def parse_text(ts, payload, lines):
        pkt = dict()
        pkt['dateTime'] = ts
        pkt['usUnits'] = weewx.METRIC
        pkt.update(Packet.parse_lines(lines, HidekiRainPacket.PARSEINFO))
        return Hideki.insert_ids(pkt, HidekiRainPacket.__name__)

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRIC
        pkt['rolling_code'] = obj.get('id')
        pkt['channel'] = obj.get('channel')
        if 'rain_mm' in obj:
            pkt['rain_total'] = Packet.get_float(obj, 'rain_mm')
        else:
            pkt['rain_total'] = Packet.get_float(obj, 'rain')
        pkt['battery'] = Packet.get_battery(obj)
        return Hideki.insert_ids(pkt, HidekiRainPacket.__name__)


class HolmanWS5029Packet(Packet):
    # {"time" : "2019-08-07 10:35:07", "model" : "Holman Industries WS5029 weather station", "id" : 53761, "temperature_C" : 9.100, "humidity" : 102, "rain_mm" : 39.500, "wind_avg_km_h" : 0, "direction_deg" : 338}

    IDENTIFIER = "Holman Industries WS5029 weather station"

    @staticmethod
    def parse_json(obj):
        sensor_id = obj.get('id')
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRICWX
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        pkt['humidity'] = Packet.get_float(obj, 'humidity')
        pkt['wind_dir'] = Packet.get_float(obj, 'direction_deg')
        pkt['wind_speed'] = Packet.get_float(obj, 'wind_avg_km_h')
        pkt['rain_total'] = Packet.get_float(obj, 'rain_mm')
        pkt = Packet.add_identifiers(pkt, sensor_id, HolmanWS5029Packet.__name__)
        return pkt


class InFactoryTHPacket(Packet):
    # {"time" : "2021-03-03 10:19:53", "model" : "inFactory-TH", "id" : 195, "channel" : 1, "battery_ok" : 1, "temperature_F" : 73.200, "humidity" : 55, "mic" : "CRC"}

    IDENTIFIER = "nFactory-TH"

    @staticmethod
    def parse_json(obj):
        sensor_id = obj.get('id')
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.US
        pkt['temperature'] = Packet.get_float(obj, 'temperature_F')
        pkt['humidity'] = Packet.get_float(obj, 'humidity')
        pkt['battery'] = Packet.get_battery(obj)
        pkt['channel'] = obj.get('channel')
        pkt = Packet.add_identifiers(pkt, sensor_id, InFactoryTHPacket.__name__)
        return pkt


class KedsumTHPacket(Packet):
    # {"time" : "2022-06-17 00:23:59", "model" : "Kedsum-TH", "id" : 235, "channel" : 1, "battery_ok" : 0, "flags" : 8, "temperature_F" : 59.000, "humidity" : 74, "mic" : "CRC"}

    IDENTIFIER = "Kedsum-TH"

    @staticmethod
    def parse_json(obj):
        sensor_id = obj.get('id')
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.US
        pkt['temperature'] = Packet.get_float(obj, 'temperature_F')
        pkt['humidity'] = Packet.get_float(obj, 'humidity')
        pkt['battery'] = Packet.get_battery(obj)
        pkt['channel'] = obj.get('channel')
        pkt['flags'] = obj.get('flags')
        pkt = Packet.add_identifiers(pkt, sensor_id, KedsumTHPacket.__name__)
        return pkt


class LaCrosseBreezeProPacket(Packet):
    # sample json output from rtl_433
    # {"time" : "2020-12-14 22:22:21", "model" : "LaCrosse-BreezePro", "id" : 561556, "seq" : 2, "flags" : 0, "temperature_C" : 19.800, "humidity" : 50, "wind_avg_km_h" : 0.000, "wind_dir_deg" : 262, "mic" : "CRC"}\n']

    IDENTIFIER = "LaCrosse-BreezePro"

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['usUnits'] = weewx.METRIC
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['model'] = obj.get('model')
        pkt['hardware_id'] = "%d" % obj.get('id', 0)
        pkt['sequence_num'] = Packet.get_int(obj, 'seq')
        pkt['wind_speed'] = Packet.get_float(obj, 'wind_avg_km_h')
        pkt['wind_dir'] = Packet.get_float(obj, 'wind_dir_deg')
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        pkt['humidity'] = Packet.get_float(obj, 'humidity')
        sensor_id = str(pkt.pop('hardware_id', '0000')).upper()
        return Packet.add_identifiers(pkt, sensor_id, LaCrosseBreezeProPacket.__name__)


class LaCrosseWSPacket(Packet):
    # 2016-09-08 00:43:52 :LaCrosse WS :9 :202
    # Temperature: 21.0 C
    # 2016-09-08 00:43:53 :LaCrosse WS :9 :202
    # Humidity: 92
    # 2016-09-08 00:43:53 :LaCrosse WS :9 :202
    # Wind speed: 0.0 m/s
    # Direction: 67.500
    # 2016-11-03 17:43:20 :LaCrosse WS :9 :202
    # Rainfall: 850.04 mm

    # {"time" : "2016-11-04 14:42:49", "model" : "LaCrosse WS", "ws_id" : 9, "id" : 202, "temperature_C" : 12.100}
    # {"time" : "2016-11-04 14:44:58", "model" : "LaCrosse WS", "ws_id" : 9, "id" : 202, "humidity" : 67}
    # {"time" : "2016-11-04 14:49:16", "model" : "LaCrosse WS", "ws_id" : 9, "id" : 202, "wind_speed_ms" : 0.800, "wind_direction" : 270.000}

    IDENTIFIER = "LaCrosse WS"
    PARSEINFO = {
        'Wind speed': [
            'wind_speed', re.compile('([\d.]+) m/s'), lambda x: float(x)],
        'Direction': ['wind_dir', None, lambda x: float(x)],
        'Temperature': [
            'temperature', re.compile('([\d.-]+) C'), lambda x: float(x)],
        'Humidity': ['humidity', None, lambda x: int(x)],
        'Rainfall': [
            'rain_total', re.compile('([\d.]+) mm'), lambda x: float(x)]}

    @staticmethod
    def parse_text(ts, payload, lines):
        pkt = dict()
        pkt['dateTime'] = ts
        pkt['usUnits'] = weewx.METRICWX
        pkt.update(Packet.parse_lines(lines, LaCrosseWSPacket.PARSEINFO))
        parts = payload.split(':')
        if len(parts) == 3:
            pkt['ws_id'] = parts[1].strip()
            pkt['hw_id'] = parts[2].strip()
        return LaCrosseWSPacket.insert_ids(pkt)

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRICWX
        pkt['ws_id'] = obj.get('ws_id')
        pkt['hw_id'] = obj.get('id')
        if 'temperature_C' in obj:
            pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        if 'humidity' in obj:
            pkt['humidity'] = Packet.get_float(obj, 'humidity')
        if 'wind_speed_ms' in obj:
            pkt['wind_speed'] = Packet.get_float(obj, 'wind_speed_ms')
        if 'wind_direction' in obj:
            pkt['wind_dir'] = Packet.get_float(obj, 'wind_direction')
        if 'rain' in obj:
            pkt['rain_total'] = Packet.get_float(obj, 'rain')
        return LaCrosseWSPacket.insert_ids(pkt)

    @staticmethod
    def insert_ids(pkt):
        ws_id = pkt.pop('ws_id', 0)
        hardware_id = pkt.pop('hw_id', 0)
        sensor_id = "%s:%s" % (ws_id, hardware_id)
        pkt = Packet.add_identifiers(pkt, sensor_id, LaCrosseWSPacket.__name__)
        return pkt


class LaCrosseTX141Bv3Packet(Packet):

    # {"time" : "2023-03-29 20:55:22", "model" : "LaCrosse-TX141Bv3", "id" : 172, "channel" : 1, "battery_ok" : 1, "temperature_C" : 3.700, "test" : "No"}

    IDENTIFIER = "LaCrosse-TX141Bv3"

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRIC
        sensor_id = obj.get('id')
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        pkt['battery'] = Packet.get_battery(obj)
        pkt = Packet.add_identifiers(pkt, sensor_id, LaCrosseTX141Bv3Packet.__name__)
        return pkt


class LaCrosseTX141THBv2Packet(Packet):

    # {"time" : "2017-01-16 15:24:43", "temperature" : 54.140, "humidity" : 34, "id" : 221, "model" : "LaCrosse TX141TH-Bv2 sensor", "battery" : "OK", "test" : "Yes"}
    # {"time" : "2020-10-28 00:22:25", "model" : "LaCrosse-TX141THBv2", "id" : 50, "channel" : 0, "battery_ok" : 1, "temperature_C" : -0.600, "humidity" : 60, "test" : "No"}
    IDENTIFIER = "LaCrosse-TX141THBv2"

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRICWX
        sensor_id = obj.get('id')
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        pkt['humidity'] = Packet.get_float(obj, 'humidity')
        pkt['battery'] = Packet.get_battery(obj)
        pkt = Packet.add_identifiers(pkt, sensor_id, LaCrosseTX141THBv2Packet.__name__)
        return pkt


class LaCrosseTXPacket(Packet):
    # {"time" : "2017-07-30 21:11:19", "model" : "LaCrosse TX Sensor", "id" : 127, "humidity" : 34.000}
    # {"time" : "2017-07-30 21:11:19", "model" : "LaCrosse TX Sensor", "id" : 127, "temperature_C" : 27.100}

    IDENTIFIER = "LaCrosse TX Sensor"

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRIC
        sensor_id = obj.get('id')
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        pkt['humidity'] = Packet.get_float(obj, 'humidity')
        pkt = Packet.add_identifiers(pkt, sensor_id, LaCrosseTXPacket.__name__)
        return pkt


class LaCrosseTX18Packet(Packet):

    # {"time" : "2020-04-21 05:21:19", "model" : "LaCrosse-WS3600", "id" : 184, "temperature_C" : 9.400}
    # {"time" : "2020-04-21 05:21:19", "model" : "LaCrosse-WS3600", "id" : 184, "humidity" : 52}
    # {"time" : "2020-04-21 05:21:20", "model" : "LaCrosse-WS3600", "id" : 184, "rain_mm" : 0.000}

    IDENTIFIER = "LaCrosse-WS3600"

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRIC
        sensor_id = obj.get('id')
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        pkt['humidity'] = Packet.get_float(obj, 'humidity')
        pkt = Packet.add_identifiers(pkt, sensor_id, LaCrosseTX18Packet.__name__)
        return pkt


class LaCrosseLTVR3Packet(Packet):

    # "time" : "2022-01-16 04:43:25", "model" : "LaCrosse-R3", "id" : 7417878, "battery_ok" : 1, "seq" : 1, "rain_mm" : 10921.750, "rain2_mm" : 10921.750, "mic" : "CRC"

    IDENTIFIER = "LaCrosse-R3"

    @staticmethod
    def parse_json(obj):
        sensor_id = obj.get('id')
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRIC
        pkt['rain_total'] = Packet.get_float(obj, 'rain_mm')
        pkt['rain2_total'] = Packet.get_float(obj, 'rain2_mm')
        pkt['battery'] = Packet.get_battery(obj)
        pkt = Packet.add_identifiers(pkt, sensor_id, LaCrosseLTVR3Packet.__name__)
        return pkt


class RubicsonTempPacket(Packet):
    # 2017-01-15 14:49:03 : Rubicson Temperature Sensor
    # House Code: 14
    # Channel: 1
    # Battery: OK
    # Temperature: 4.5 C
    # CRC: OK

    IDENTIFIER = "Rubicson Temperature Sensor"
    PARSEINFO = {
        'House Code': ['house_code', None, lambda x: int(x)],
        'Channel': ['channel', None, lambda x: int(x)],
        'Battery': ['battery', None, lambda x: 0 if x == 'OK' else 1],
        'Temperature': ['temperature', re.compile('([\d.-]+) C'), lambda x: float(x)]}

    @staticmethod
    def parse_text(ts, payload, lines):
        pkt = dict()
        pkt['dateTime'] = ts
        pkt['usUnits'] = weewx.METRIC
        pkt.update(Packet.parse_lines(lines, RubicsonTempPacket.PARSEINFO))
        channel = pkt.pop('channel', 0)
        code = pkt.pop('house_code', 0)
        sensor_id = "%s:%s" % (channel, code)
        return Packet.add_identifiers(pkt, sensor_id, RubicsonTempPacket.__name__)

    # {"time" : "2017-01-17 20:47:41", "model" : "Rubicson Temperature Sensor", "id" : 14, "channel" : 1, "battery" : "OK", "temperature_C" : -1.800, "crc" : "OK"}

    @staticmethod
    def parse_json(obj):
        channel = obj.get('channel', 0)
        code = obj.get('id', 0)
        sensor_id = "%s:%s" % (channel, code)
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRIC
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        pkt['battery'] = Packet.get_battery(obj)
        return Packet.add_identifiers(pkt, sensor_id, RubicsonTempPacket.__name__)


class RubicsonTempPacketV2(Packet):
    # {"time" : "2023-04-04 19:57:28", "protocol" : 2, "model" : "Rubicson-Temperature", "id" : 183, "channel" : 3, "battery_ok" : 1, "temperature_C" : 21.700, "mic" : "CRC"}

    IDENTIFIER = "Rubicson-Temperature"

    @staticmethod
    def parse_json(obj):
        channel = obj.get('channel', 0)
        code = obj.get('id', 0)
        sensor_id = "%s:%s" % (channel, code)
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRIC
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        pkt['battery'] = Packet.get_battery(obj)
        return Packet.add_identifiers(pkt, sensor_id, RubicsonTempPacketV2.__name__)


class OS(object):
    @staticmethod
    def insert_ids(pkt, pkt_type):
        channel = pkt.pop('channel', 0)
        code = pkt.pop('house_code', 0)
        sensor_id = "%s:%s" % (channel, code)
        return Packet.add_identifiers(pkt, sensor_id, pkt_type)


class OSPCR800Packet(Packet):
    # 2016-11-03 04:36:23 : OS : PCR800
    # House Code: 93
    # Channel: 0
    # Battery: OK
    # Rain Rate: 0.0 in/hr
    # Total Rain: 41.0 in

    #IDENTIFIER = "PCR800"
    IDENTIFIER = "Oregon-PCR800"
    PARSEINFO = {
        'House Code': ['house_code', None, lambda x: int(x)],
        'Channel': ['channel', None, lambda x: int(x)],
        'Battery': ['battery', None, lambda x: 0 if x == 'OK' else 1],
        'Rain Rate':
            ['rain_rate', re.compile('([\d.]+) in'), lambda x: float(x)],
        'Total Rain':
            ['rain_total', re.compile('([\d.]+) in'), lambda x: float(x)]}

    @staticmethod
    def parse_text(ts, payload, lines):
        pkt = dict()
        pkt['dateTime'] = ts
        pkt['usUnits'] = weewx.US
        pkt.update(Packet.parse_lines(lines, OSPCR800Packet.PARSEINFO))
        return OS.insert_ids(pkt, OSPCR800Packet.__name__)

    # {"time" : "2018-08-04 15:29:27", "brand" : "OS", "model" : "PCR800",        "id" : 236, "channel" : 0, "battery" : "OK", "rain_rate" : 0.000, "rain_total" : 109.594}
    # {"time" : "2020-08-19 19:31:13", "brand" : "OS", "model" : "Oregon-PCR800", "id" : 80, "channel" : 0, "battery_ok" : 1, "rain_rate_in_h" : 0.000, "rain_in" : 27.741}
    # {"time" : "2020-06-06 20:15:17", "brand" : "OS", "model" : "Oregon-PCR800", "id" : 32, "channel" : 0, "battery_ok" : 1, "rain_rate_in_h" : 0.150, "rain_in" : 0.082}
    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.US
        pkt['house_code'] = obj.get('id')
        pkt['channel'] = obj.get('channel')
        pkt['battery'] = Packet.get_battery(obj)
        pkt['rain_rate'] = Packet.get_float(obj, 'rain_rate_in_h')
        pkt['rain_total'] = Packet.get_float(obj, 'rain_in')
        return OS.insert_ids(pkt, OSPCR800Packet.__name__)

class OSBTHR918Packet(Packet):
    IDENTIFIER = "BTHR918"
    PARSEINFO = {
        'House Code': ['house_code', None, lambda x: int(x)],
        'Channel': ['channel', None, lambda x: int(x)],
        'Battery': ['battery', None, lambda x: 0 if x == 'OK' else 1],
        'Temperature': ['temperature', re.compile('([\d.-]+) C'), lambda x: float(x)],
        'Humidity': ['humidity', re.compile('([\d.]+) %'), lambda x: float(x)],
        'Pressure': ['pressure', re.compile('([\d.]+) mbar'), lambda x: float(x)]}

    @staticmethod
    def parse_text(ts, payload, lines):
        pkt = dict()
        pkt['dateTime'] = ts
        pkt['usUnits'] = weewx.METRIC
        pkt.update(Packet.parse_lines(lines, OSBTHR918Packet.PARSEINFO))
        return OS.insert_ids(pkt, OSBTHR918Packet.__name__)

    # original rtl_433 output
    # {"time" : "2021-07-25 15:11:11", "model" : "Oregon-BTHR918", "id" : 20, "channel" : 0, "battery_ok" : 1, "temperature_C" : 22.200, "humidity" : 58, "pressure_hPa" : 1009.000}

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRIC
        pkt['house_code'] = obj.get('id')
        pkt['channel'] = obj.get('channel')
        pkt['battery'] = Packet.get_battery(obj)
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        pkt['humidity'] = Packet.get_float(obj, 'humidity')
        if 'pressure' in obj:
            pkt['pressure'] = Packet.get_float(obj, 'pressure_hPa')
        elif 'pressure_hPa' in obj:
            pkt['pressure'] = Packet.get_float(obj, 'pressure_hPa')
        return OS.insert_ids(pkt, OSBTHR918Packet.__name__)

# apparently rtl_433 uses BHTR968 when it should be BTHR968
class OSBTHR968Packet(Packet):
    # Added 2017-04-22 ALG
    # 2017-09-12 21:44:55     :       OS :    BHTR968
    # House Code:      111
    # Channel:         0
    # Battery:         OK
    # Celcius:         26.20 C
    # Fahrenheit:      79.16 F
    # Humidity:        36 %
    # Pressure:        1012 mbar

    IDENTIFIER = "BHTR968"
    PARSEINFO = {
        'House Code': ['house_code', None, lambda x: int(x)],
        'Channel': ['channel', None, lambda x: int(x)],
        'Battery': ['battery', None, lambda x: 0 if x == 'OK' else 1],
        'Temperature': ['temperature', re.compile('([\d.-]+) C'), lambda x: float(x)],
        'Humidity': ['humidity', re.compile('([\d.]+) %'), lambda x: float(x)],
        'Pressure': ['pressure', re.compile('([\d.]+) mbar'), lambda x: float(x)]}

    @staticmethod
    def parse_text(ts, payload, lines):
        pkt = dict()
        pkt['dateTime'] = ts
        pkt['usUnits'] = weewx.METRIC
        pkt.update(Packet.parse_lines(lines, OSBTHR968Packet.PARSEINFO))
        return OS.insert_ids(pkt, OSBTHR968Packet.__name__)

    # original rtl_433 output
    # {"time" : "2017-01-18 14:56:03", "brand" : "OS", "model" :"BHTR968", "id" : 111, "channel" : 0, "battery" : "OK", "temperature_C" : 27.200, "temperature_F" : 80.960,  "humidity" : 46, "pressure" : 1013}
    # by 06mar2019
    # {"time" : "2019-03-06 13:27:23", "brand" : "OS", "model" : "BHTR968", "id" : 179, "channel" : 0, "battery" : "LOW", "temperature_C" : 19.800, "humidity" : 54, "pressure_hPa" : 974.000}
    # by 03mar2022
    # out:['{"time" : "2022-03-03 15:44:25", "model" : "Oregon-BHTR968", "id" : 219, "channel" : 0, "battery_ok" : 1, "temperature_C" : 21.700, "humidity" : 40, "pressure_hPa" : 990.000}

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRIC
        pkt['house_code'] = obj.get('id')
        pkt['channel'] = obj.get('channel')
        pkt['battery'] = Packet.get_battery(obj)
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        pkt['humidity'] = Packet.get_float(obj, 'humidity')
        if 'pressure' in obj:
            pkt['pressure'] = Packet.get_float(obj, 'pressure_hPa')
        elif 'pressure_hPa' in obj:
            pkt['pressure'] = Packet.get_float(obj, 'pressure_hPa')
        return OS.insert_ids(pkt, OSBTHR968Packet.__name__)


class OSTHGR122NPacket(Packet):
    # 2016-09-12 21:44:55     :       OS :    THGR122N
    # House Code:      96
    # Channel:         3
    # Battery:         OK
    # Temperature:     27.30 C
    # Humidity:        36 %

    IDENTIFIER = "THGR122N"
    PARSEINFO = {
        'House Code': ['house_code', None, lambda x: int(x)],
        'Channel': ['channel', None, lambda x: int(x)],
        'Battery': ['battery', None, lambda x: 0 if x == 'OK' else 1],
        'Temperature': [
            'temperature', re.compile('([\d.-]+) C'), lambda x: float(x)],
        'Humidity': ['humidity', re.compile('([\d.]+) %'), lambda x: float(x)]}

    @staticmethod
    def parse_text(ts, payload, lines):
        pkt = dict()
        pkt['dateTime'] = ts
        pkt['usUnits'] = weewx.METRIC
        pkt.update(Packet.parse_lines(lines, OSTHGR122NPacket.PARSEINFO))
        return OS.insert_ids(pkt, OSTHGR122NPacket.__name__)

    # {"time" : "2017-01-18 14:56:03", "brand" : "OS", "model" :"THGR122N", "id" : 211, "channel" : 1, "battery" : "LOW", "temperature_C" : 7.900, "humidity" : 27}

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRIC
        pkt['house_code'] = obj.get('id')
        pkt['channel'] = obj.get('channel')
        pkt['battery'] = Packet.get_battery(obj)
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        pkt['humidity'] = Packet.get_float(obj, 'humidity')
        return OS.insert_ids(pkt, OSTHGR122NPacket.__name__)


class OSTHGR810Packet(Packet):
    # rtl_433 circa jul 2016 emits this
    # 2016-09-01 22:05:47 :Weather Sensor THGR810
    # House Code: 122
    # Channel: 1
    # Battery: OK
    # Celcius: 26.70 C
    # Fahrenheit: 80.06 F
    # Humidity: 58 %

    # rtl_433 circa nov 2016 emits this
    # 2016-11-04 02:21:37 :OS :THGR810
    # House Code: 122
    # Channel: 1
    # Battery: OK
    # Celcius: 22.20 C
    # Fahrenheit: 71.96 F
    # Humidity: 57 %

    IDENTIFIER = "THGR810"
    PARSEINFO = {
        'House Code': ['house_code', None, lambda x: int(x)],
        'Channel': ['channel', None, lambda x: int(x)],
        'Battery': ['battery', None, lambda x: 0 if x == 'OK' else 1],
        'Celcius': [
            'temperature', re.compile('([\d.-]+) C'), lambda x: float(x)],
        'Fahrenheit': [
            'temperature_F', re.compile('([\d.-]+) F'), lambda x: float(x)],
        'Humidity': ['humidity', re.compile('([\d.]+) %'), lambda x: float(x)]}

    @staticmethod
    def parse_text(ts, payload, lines):
        pkt = dict()
        pkt['dateTime'] = ts
        pkt['usUnits'] = weewx.METRIC
        pkt.update(Packet.parse_lines(lines, OSTHGR810Packet.PARSEINFO))
        return OS.insert_ids(pkt, OSTHGR810Packet.__name__)

    # {"time" : "2020-06-06 20:08:12", "brand" : "OS", "model" : "Oregon-THGR810", "id" : 153, "channel" : 1, "battery_ok" : 1, "temperature_C" : 18.200, "humidity" : 49}

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRIC
        pkt['house_code'] = obj.get('id')
        pkt['channel'] = obj.get('channel')
        pkt['battery'] = Packet.get_battery(obj)
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        pkt['humidity'] = Packet.get_float(obj, 'humidity')
        return OS.insert_ids(pkt, OSTHGR810Packet.__name__)


class OSTHR128Packet(Packet):
    # 2019-04-30:   Thermo Sensor THR128
    # House Code:      5
    # Channel:         1
    # Battery:         OK
    # Temperature:     18.800 C

    IDENTIFIER = "OSv1 Temperature Sensor"
    PARSEINFO = {
        'House Code': ['house_code', None, lambda x: int(x)],
        'Channel': ['channel', None, lambda x: int(x)],
        'Battery': ['battery', None, lambda x: 0 if x == 'OK' else 1],
        'Temperature':
            ['temperature', re.compile('([\d.-]+) C'), lambda x : float(x)]}

    @staticmethod
    def parse_text(ts, payload, lines):
        pkt = dict()
        pkt['dateTime'] = ts
        pkt['usUnits'] = weewx.METRIC
        pkt.update(Packet.parse_lines(lines, OSTHR128Packet.PARSEINFO))
        return OS.insert_ids(pkt, OSTHR128Packet.__name__)

    # {"time" : "2019-04-30 20:44:00", "brand" : "OS", "model" : "OSv1 Temperature Sensor", "sid" : 5, "channel" : 1, "battery" : "OK", "temperature_C" : 18.800}
    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRIC
        pkt['house_code'] = obj.get('sid')
        pkt['channel'] = obj.get('channel')
        pkt['battery'] = Packet.get_battery(obj)
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        return OS.insert_ids(pkt, OSTHR128Packet.__name__)


class OSTHR228NPacket(Packet):
    # 2016-09-09 11:59:10 :   Thermo Sensor THR228N
    # House Code:      111
    # Channel:         2
    # Battery:         OK
    # Temperature:     24.70 C

    IDENTIFIER = "Oregon-THR228N"
    PARSEINFO = {
        'House Code': ['house_code', None, lambda x: int(x)],
        'Channel': ['channel', None, lambda x: int(x)],
        'Battery': ['battery', None, lambda x: 0 if x == 'OK' else 1],
        'Temperature':
            ['temperature', re.compile('([\d.-]+) C'), lambda x : float(x)]}

    @staticmethod
    def parse_text(ts, payload, lines):
        pkt = dict()
        pkt['dateTime'] = ts
        pkt['usUnits'] = weewx.METRIC
        pkt.update(Packet.parse_lines(lines, OSTHR228NPacket.PARSEINFO))
        return OS.insert_ids(pkt, OSTHR228NPacket.__name__)

    # "time" : "2022-06-07 08:17:09", "model" : "Oregon-THR228N", "id" : 211, "channel" : 1, "battery_ok" : 0, "temperature_C" : -19.900}
    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRIC
        pkt['house_code'] = obj.get('id')
        pkt['channel'] = obj.get('channel')
        pkt['battery'] = Packet.get_battery(obj)
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        return OS.insert_ids(pkt, OSTHR228NPacket.__name__)

class OSUV800Packet(Packet):
    # 2017-01-30 22:00:12 : OS : UV800
    # House Code: 207
    # Channel: 1
    # Battery: OK
    # UV Index: 0

    IDENTIFIER = "UV800"
    PARSEINFO = {
        'House Code': ['house_code', None, lambda x: int(x)],
        'Channel': ['channel', None, lambda x: int(x)],
        'Battery': ['battery', None, lambda x: 0 if x == 'OK' else 1],
        'UV Index':
            ['uv_index', re.compile('([\d.-]+) C'), lambda x : float(x)]}

    @staticmethod
    def parse_text(ts, payload, lines):
        pkt = dict()
        pkt['dateTime'] = ts
        pkt['usUnits'] = weewx.METRIC
        pkt.update(Packet.parse_lines(lines, OSUV800Packet.PARSEINFO))
        return OS.insert_ids(pkt, OSUV800Packet.__name__)

    # {"time" : "2017-01-30 22:19:40", "brand" : "OS", "model" : "UV800", "id" : 207, "channel" : 1, "battery" : "OK", "uv" : 0}
    # on 03mar2022
    # {"time" : "2022-03-03 15:51:53", "model" : "Oregon-UV800", "id" : 255, "channel" : 1, "battery_ok" : 1, "uv" : 0}

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRIC
        pkt['house_code'] = obj.get('id')
        pkt['channel'] = obj.get('channel')
        pkt['battery'] = Packet.get_battery(obj)
        pkt['uv_index'] = Packet.get_float(obj, 'uv')
        return OS.insert_ids(pkt, OSUV800Packet.__name__)


class OSUVR128Packet(Packet):
    # 2019-11-05 07:07:07 : Oregon Scientific UVR128
    # House Code: 116
    # UV Index: 0
    # Battery: OK

    IDENTIFIER = "UVR128"
    PARSEINFO = {
        'House Code': ['house_code', None, lambda x: int(x)],
        'UV Index': ['uv_index', re.compile('([\d.-]+) C'), lambda x: float(x)],
        'Battery': ['battery', None, lambda x: 0 if x == 'OK' else 1]}

    @staticmethod
    def parse_text(ts, payload, lines):
        pkt = dict()
        pkt['dateTime'] = ts
        pkt['usUnits'] = weewx.METRIC
        pkt.update(Packet.parse_lines(lines, OSUVR128Packet.PARSEINFO))
        return OS.insert_ids(pkt, OSUVR128Packet.__name__)

    # {"time" : "2019-11-05 07:07:07", "model" : "Oregon-UVR128", "id" : 116, "uv" : 0, "battery" : "OK"}
    # {"time" : "2019-11-19 06:44:53", "model" : "Oregon-UVR128", "id" : 116, "uv" : 0, "battery" : "OK"}

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRIC
        pkt['house_code'] = obj.get('id')
        pkt['uv_index'] = Packet.get_float(obj, 'uv')
        pkt['battery'] = Packet.get_battery(obj)
        return OS.insert_ids(pkt, OSUVR128Packet.__name__)


class OSWGR800Packet(Packet):
    # 2016-11-03 04:36:34 : OS : WGR800
    # House Code: 85
    # Channel: 0
    # Battery: OK
    # Gust: 1.1 m/s
    # Average: 1.1 m/s
    # Direction: 22.5 degrees

    IDENTIFIER = "WGR800"
    PARSEINFO = {
        'House Code': ['house_code', None, lambda x: int(x)],
        'Channel': ['channel', None, lambda x: int(x)],
        'Battery': ['battery', None, lambda x: 0 if x == 'OK' else 1],
        'Gust': [
            'wind_gust', re.compile('([\d.]+) m'), lambda x: float(x)],
        'Average': [
            'wind_speed', re.compile('([\d.]+) m'), lambda x: float(x)],
        'Direction': [
            'wind_dir', re.compile('([\d.]+) degrees'), lambda x: float(x)]}

    @staticmethod
    def parse_text(ts, payload, lines):
        pkt = dict()
        pkt['dateTime'] = ts
        pkt['usUnits'] = weewx.METRICWX
        pkt.update(Packet.parse_lines(lines, OSWGR800Packet.PARSEINFO))
        return OS.insert_ids(pkt, OSWGR800Packet.__name__)

    # {"time" : "2020-06-06 21:44:43", "brand" : "OS", "model" : "Oregon-WGR800", "id" : 245, "channel" : 0, "battery_ok" : 1, "wind_max_m_s" : 3.100, "wind_avg_m_s" : 0.000, "wind_dir_deg" : 90.000}
    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRICWX
        pkt['house_code'] = obj.get('id')
        pkt['channel'] = obj.get('channel')
        pkt['battery'] = Packet.get_battery(obj)
        pkt['wind_gust'] = Packet.get_float(obj, 'wind_max_m_s')
        pkt['wind_speed'] = Packet.get_float(obj, 'wind_avg_m_s')
        pkt['wind_dir'] = Packet.get_float(obj, 'wind_dir_deg')
        return OS.insert_ids(pkt, OSWGR800Packet.__name__)


class OSTHN802Packet(Packet):
    # 2017-08-03 17:24:08     :       OS :    THN802
    # House Code:      157
    # Channel:         3
    # Battery:         OK
    # Celcius:         26.60 C

    IDENTIFIER = "THN802"
    PARSEINFO = {
        'House Code': ['house_code', None, lambda x: int(x)],
        'Channel': ['channel', None, lambda x: int(x)],
        'Battery': ['battery', None, lambda x: 0 if x == 'OK' else 1],
        'Celcius': ['temperature', re.compile('([\d.-]+) C'), lambda x: float(x)]}

    @staticmethod
    def parse_text(ts, payload, lines):
        pkt = dict()
        pkt['dateTime'] = ts
        pkt['usUnits'] = weewx.METRIC
        pkt.update(Packet.parse_lines(lines, OSTHN802Packet.PARSEINFO))
        return OS.insert_ids(pkt, OSTHN802Packet.__name__)

    # {"time" : "2017-08-03 17:41:24", "brand" : "OS", "model" : "THN802", "id" : 157, "channel" : 3, "battery" : "OK", "temperature_C" : 26.700}

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRIC
        pkt['house_code'] = obj.get('id')
        pkt['channel'] = obj.get('channel')
        pkt['battery'] = Packet.get_battery(obj)
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        return OS.insert_ids(pkt, OSTHN802Packet.__name__)


class OSBTHGN129Packet(Packet):
    # 2017-08-03 17:24:03     :       OS :    BTHGN129
    # House Code:      146
    # Channel:         5
    # Battery:         OK
    # Celcius:         32.00 C
    # Humidity:        50 %
    # Pressure:        959.36 mPa

    IDENTIFIER = "BTHGN129"
    PARSEINFO = {
        'House Code': ['house_code', None, lambda x: int(x)],
        'Channel': ['channel', None, lambda x: int(x)],
        'Battery': ['battery', None, lambda x: 0 if x == 'OK' else 1],
        'Celcius': ['temperature', re.compile('([\d.-]+) C'), lambda x: float(x)],
        'Humidity': ['humidity', re.compile('([\d.]+) %'), lambda x: float(x)],
        'Pressure': ['pressure', re.compile('([\d.]+) mPa'), lambda x: float(x)]}

    @staticmethod
    def parse_text(ts, payload, lines):
        pkt = dict()
        pkt['dateTime'] = ts
        pkt['usUnits'] = weewx.METRIC
        pkt.update(Packet.parse_lines(lines, OSBTHGN129Packet.PARSEINFO))
        return OS.insert_ids(pkt, OSBTHGN129Packet.__name__)

    # {"time" : "2017-08-03 17:41:48", "brand" : "OS", "model" : "BTHGN129", "id" : 146, "channel" : 5, "battery" : "OK", "temperature_C" : 31.700, "humidity" : 52, "pressure_hPa" : 959.364}

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRIC
        pkt['house_code'] = obj.get('id')
        pkt['channel'] = obj.get('channel')
        pkt['battery'] = Packet.get_battery(obj)
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        pkt['humidity'] = Packet.get_float(obj, 'humidity')
        pkt['pressure'] = Packet.get_float(obj, 'pressure_hPa')
        return OS.insert_ids(pkt, OSBTHGN129Packet.__name__)


class OSTHGR968Packet(Packet):
    # {"time" : "2019-02-15 13:43:25", "brand" : "OS", "model" : "THGR968", "id" : 187, "channel" : 1, "battery" : "OK", "temperature_C" : 16.500, "humidity" : 11}
    # '{"time" : "2019-02-15 13:43:26", "brand" : "OS", "model" : "THGR968", "id" : 187, "channel" : 1, "battery" : "OK", "temperature_C" : 16.500, "humidity" : 11}

    IDENTIFIER = "THGR968"

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRIC
        pkt['house_code'] = obj.get('id')
        pkt['channel'] = Packet.get_int(obj, 'channel')
        pkt['battery'] = Packet.get_battery(obj)
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        pkt['humidity'] = Packet.get_float(obj, 'humidity')
        return OS.insert_ids(pkt, OSTHGR968Packet.__name__)


class OSRGR968Packet(Packet):
    # {"time" : "2019-02-15 14:32:51", "brand" : "OS", "model" : "RGR968", "id" : 48, "channel" : 0, "battery" : "OK", "rain_rate" : 0.000, "total_rain" : 6935.100}
    # {"time" : "2019-02-15 14:32:51", "brand" : "OS", "model" : "RGR968", "id" : 48, "channel" : 0, "battery" : "OK", "rain_rate" : 0.000, "total_rain" : 6935.100}

    IDENTIFIER = "RGR968"

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRIC
        pkt['house_code'] = obj.get('id')
        pkt['channel'] = Packet.get_int(obj, 'channel')
        pkt['battery'] = Packet.get_battery(obj)
        pkt['rain_rate'] = Packet.get_float(obj, 'rain_rate')
        pkt['rain_total'] = Packet.get_float(obj, 'total_rain')
        return OS.insert_ids(pkt, OSRGR968Packet.__name__)


class ProloguePacket(Packet):
    # 2017-03-19 : Prologue Temperature and Humidity Sensor
    # {"time" : "2017-03-15 20:14:19", "model" : "Prologue sensor", "id" : 5, "rid" : 166, "channel" : 1, "battery" : "OK", "button" : 0, "temperature_C" : -0.700, "humidity" : 49}

    IDENTIFIER = "Prologue sensor"

    @staticmethod
    def parse_json(obj):
        sensor_id = obj.get('rid')
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRIC
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        pkt['humidity'] = Packet.get_float(obj, 'humidity')
        pkt['battery'] = Packet.get_battery(obj)
        pkt['channel'] = obj.get('channel')
        pkt = Packet.add_identifiers(pkt, sensor_id, ProloguePacket.__name__)
        return pkt


class PrologueTHPacket(Packet):
    # 2021-09-03 : Prologue-TH Temperature and Humidity Sensor
    # out:[u'{"time" : "2021-09-02 23:47:40", "model" : "Prologue-TH", "subtype" : 5, "id" : 70, "channel" : 1, "battery_ok" : 1, "temperature_C" : 24.800, "humidity" : 49, "button" : 0}\n']

    IDENTIFIER = "Prologue-TH"

    @staticmethod
    def parse_json(obj):
        sensor_id = obj.get('id')
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRIC
        pkt['model'] = obj.get('model')
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        pkt['humidity'] = Packet.get_float(obj, 'humidity')
        pkt['battery'] = Packet.get_battery(obj)
        pkt['channel'] = obj.get('channel')
        pkt = Packet.add_identifiers(pkt, sensor_id, PrologueTHPacket.__name__)
        return pkt


class NexusTemperaturePacket(Packet):
    # 2018-06-30 01:12:12 :   Nexus Temperature
    #         House Code:      55
    #         Battery:         OK
    #         Channel:         1
    #         Temperature:     27.10 C
    # 2018-08-01 22:03:11 :   Nexus Temperature/Humidity
    #    House Code:      180
    #    Battery:         OK
    #    Channel:         1
    #    Temperature:     20.10 C
    #    Humidity:        42 %

    IDENTIFIER = "Nexus Temperature"
    PARSEINFO = {
        'House Code': ['house_code', None, lambda x: int(x)],
        'Battery': ['battery', None, lambda x: 0 if x == 'OK' else 1],
                'Channel': ['channel', None, lambda x: int(x)],
        'Temperature':
            ['temperature', re.compile('([\d.-]+) C'), lambda x : float(x)],
        'Humidity':
            ['humidity', re.compile('([\d.-]+) %'), lambda x : float(x)]}

    @staticmethod
    def parse_text(ts, payload, lines):
        pkt = dict()
        pkt['dateTime'] = ts
        pkt['usUnits'] = weewx.METRIC
        pkt.update(Packet.parse_lines(lines, NexusTemperaturePacket.PARSEINFO))
        return OS.insert_ids(pkt, NexusTemperaturePacket.__name__)

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRIC
        pkt['house_code'] = obj.get('id')
        pkt['battery'] = Packet.get_battery(obj)
        pkt['channel'] = obj.get('channel')
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        if 'humidity' in obj:
            pkt['humidity'] = Packet.get_float(obj, 'humidity')
        return OS.insert_ids(pkt, NexusTemperaturePacket.__name__)


class SpringfieldTMPacket(Packet):
    # {"time" : "2019-01-20 11:14:00", "model" : "Springfield Temperature & Moisture", "sid" : 224, "channel" : 3, "battery" : "OK", "transmit" : "MANUAL", "temperature_C" : -204.800, "moisture" : 0, "mic" : "CHECKSUM"}

    IDENTIFIER = "Springfield Temperature & Moisture"

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRIC
        sensor_id = obj.get('sid')
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        pkt['moisture'] = Packet.get_float(obj, 'moisture')
        pkt['battery'] = Packet.get_battery(obj)
        pkt['channel'] = obj.get('channel')
        pkt['transmit'] = obj.get('transmit')
        pkt = Packet.add_identifiers(pkt, sensor_id, SpringfieldTMPacket.__name__)
        return pkt


class TFADropPacket(Packet):

    # {"time" : "2022-06-19 09:18:57", "model" : "TFA-Drop", "id" : 549565, "battery_ok" : 1, "rain_mm" : 0.000, "mic" : "CHECKSUM"}
    # {"time" : "2024-08-24 13:51:38", "model" : "TFA-Drop", "id" : 899964, "battery_ok" : 1, "rain_mm" : 17.780, "mic" : "CHECKSUM"}

    IDENTIFIER = "TFA-Drop"

    @staticmethod
    def parse_json(obj):
        sensor_id = obj.get('id', '0000')
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRICWX
        pkt['rain_total'] = Packet.get_float(obj, 'rain_mm')
        pkt['battery'] = Packet.get_battery(obj)
        pkt = Packet.add_identifiers(pkt, sensor_id, TFADropPacket.__name__)
        return pkt


class TFATwinPlus303049Packet(Packet):
    # 2019-09-25 17:15:12 :   TFA-Twin-Plus-30.3049
    # Channel: 1
    # Battery: OK
    # Temperature: 8.40 C
    # Humidity: 91 %

    # {"time" : "2019-09-25 17:15:12", "model" : "TFA-Twin-Plus-30.3049", "id" : 13, "channel" : 1, "battery" : "OK", "temperature_C" : 8.400, "humidity" : 91, "mic" : "CHECK  SUM"} 

    IDENTIFIER = "TFA-Twin-Plus-30.3049"
    PARSEINFO = {
        'Channel': ['channel', None, lambda x: int(x)],
        'Battery': ['battery', None, lambda x: 0 if x == 'OK' else 1],
        'Temperature': [
            'temperature', re.compile('([\d.-]+) C'), lambda x: float(x)],
        'Humidity': ['humidity', re.compile('([\d.]+) %'), lambda x: float(x)]}

    @staticmethod
    def parse_text(ts, payload, lines):
        sensor_id = '0000' # FIXME - no id in text output?
        pkt = dict()
        pkt['dateTime'] = ts
        pkt['usUnits'] = weewx.METRIC
        pkt.update(Packet.parse_lines(lines, TFATwinPlus303049Packet.PARSEINFO))
        return Packet.add_identifiers(pkt, sensor_id, TFATwinPlus303049Packet.__name__)

    @staticmethod
    def parse_json(obj):
        sensor_id = obj.get('id', '0000')
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRIC
        pkt['channel'] = obj.get('channel')
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        pkt['humidity'] = Packet.get_float(obj, 'humidity')
        pkt['battery'] = Packet.get_battery(obj)
        return Packet.add_identifiers(pkt, sensor_id, TFATwinPlus303049Packet.__name__)


class TSFT002Packet(Packet):
    # time : 2019-12-22 16:57:58
    # model : TS-FT002 Id : 127
    # Depth : 186 Temperature: 20.9 C Transmit Interval: 180 Battery Flag?: 8 MIC : CHECKSUM

    # {"time" : "2019-12-22 22:54:58", "model" : "TS-FT002", "id" : 127, "depth_cm" : 186, "temperature_C" : 20.700, "transmit_s" : 180, "flags" : 8, "mic" : "CHECKSUM"}

    IDENTIFIER = "TS-FT002"

    @staticmethod
    def parse_json(obj):
        sensor_id = obj.get('id', '0000')
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRIC
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        pkt['depth'] = Packet.get_float(obj, 'depth_cm')
        pkt['transmit'] = Packet.get_float(obj, 'transmit_s')
        pkt['flags'] = Packet.get_int(obj, 'flags')
        pkt = Packet.add_identifiers(pkt, sensor_id, TSFT002Packet.__name__)
        return pkt


class Vevor7in1Packet(Packet):
    # Vevor 7-in-1 weather station, thanks to ivan
    # https://sh.com.hr/weather-station/your-vevor-7-in-1-wi-fi-weather-station-yt60234-in-weewx/

    # {"time" : "2024-11-13 13:27:59", "model" : "Vevor-7in1", "id" : 52266, "channel" : 0, "battery_ok" : 1, "temperature_C" : 5.400, "humidity" : 76, "wind_avg_km_h" : 0.700, "wind_max_km_h" : 2.667, "wind_dir_deg" : 87, "rain_mm" : 12.116, "uv" : 0, "light_lux" : 7213, "mic" : "CHECKSUM"}'

    IDENTIFIER = "Vevor-7in1"

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRICWX
        station_id = Packet.get_int(obj, 'id')
        pkt['battery'] = Packet.get_battery(obj)
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        pkt['humidity'] = Packet.get_float(obj, 'humidity')
        pkt['wind_gust'] = kmh_to_mps(Packet.get_float(obj, 'wind_max_km_h'))
        pkt['wind_speed'] = kmh_to_mps(Packet.get_float(obj, 'wind_avg_km_h'))
        pkt['wind_dir'] = Packet.get_float(obj, 'wind_dir_deg')
        pkt['rain_total'] = Packet.get_float(obj, 'rain_mm')
        pkt['light_lux'] = Packet.get_int(obj, 'light_lux')
        pkt['uv'] = Packet.get_float(obj, 'uv')
        pkt = Packet.add_identifiers(pkt, station_id, Vevor7in1Packet.__name__)
        return pkt


class WS2032Packet(Packet):
    #{"time" : "2020-10-19 22:41:24", "model" : "WS2032", "id" : 11768, "temperature_C" : 3.800, "humidity" : 48, "wind_dir_deg" : 315.000, "wind_avg_km_h" : 7.740, "wind_max_km_h" : 15.480, "maybe_flags" : 0, "maybe_rain" : 256, "mic" : "CRC"}
    
    IDENTIFIER = "WS2032"

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRIC
        sensor_id = obj.get('id')
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        pkt['humidity'] = Packet.get_float(obj, 'humidity')
        pkt['wind_gust'] = Packet.get_float(obj, 'wind_max_km_h')
        pkt['wind_speed'] = Packet.get_float(obj, 'wind_avg_km_h')
        pkt['wind_dir'] = Packet.get_float(obj, 'wind_dir_deg')
        pkt['rain_total'] = Packet.get_float(obj, 'maybe_rain')
        pkt = Packet.add_identifiers(pkt, sensor_id, WS2032Packet.__name__)
        return pkt


class WT0124Packet(Packet):
    # 2019-04-23: WT0124 Pool Thermometer
    # {"time" : "2019-04-23 12:28:52", "model" : "WT0124 Pool Thermometer", "rid" : 122, "channel" : 1, "temperature_C" : 22.800, "mic" : "CHECKSUM", "data" : 172}

    IDENTIFIER = "WT0124 Pool Thermometer"

    @staticmethod
    def parse_json(obj):
        pkt = dict()
        pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRIC
        sensor_id = obj.get('rid')
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        pkt = Packet.add_identifiers(pkt, sensor_id, WT0124Packet.__name__)
        return pkt

class PacketFactory(object):

    # known packets will be lazy-loaded by introspecting at first request
    KNOWN_PACKETS = []

    @staticmethod
    def known_packets():
        if not PacketFactory.KNOWN_PACKETS:
            import sys, inspect
            objs = inspect.getmembers(sys.modules[__name__], inspect.isclass)
            for name, obj in objs:
                if hasattr(obj, 'IDENTIFIER'):
                    PacketFactory.KNOWN_PACKETS.append(obj)
        return PacketFactory.KNOWN_PACKETS

    @staticmethod
    def create(lines):
        # return a list of packets from the specified lines
        while lines:
            pkt = None
            if lines[0].startswith('{'):
                pkt = PacketFactory.parse_json(lines)
                if pkt is None:
                    logdbg("punt unrecognized line '%s'" % lines[0])
                lines.pop(0)
            else:
                pkt = PacketFactory.parse_text(lines)
            if pkt is not None:
                yield pkt

    @staticmethod
    def parse_json(lines):
        try:
            obj = json.loads(lines[0])
            if 'model' in obj:
                for parser in PacketFactory.known_packets():
                    if obj['model'].find(parser.IDENTIFIER) >= 0:
                        return parser.parse_json(obj)
                logdbg("parse_json: unknown model %s" % obj['model'])
        except ValueError as e:
            logdbg("parse_json failed: %s" % e)
        return None

    @staticmethod
    def parse_text(lines):
        ts, payload = PacketFactory.parse_firstline(lines[0])
        if ts and payload:
            logdbg("parse_text: ts=%s payload=%s" % (ts, payload))
            for parser in PacketFactory.known_packets():
                if payload.find(parser.IDENTIFIER) >= 0:
                    pkt = parser.parse_text(ts, payload, lines)
                    logdbg("pkt=%s" % pkt)
                    return pkt
            logdbg("parse_text: unknown format: ts=%s payload=%s" %
                   (ts, payload))
        logdbg("parse_text failed: ts=%s payload=%s line=%s" %
               (ts, payload, lines[0]))
        lines.pop(0)
        return None

    TS_PATTERN = re.compile('(\d\d\d\d-\d\d-\d\d \d\d:\d\d:\d\d)[\s]+:*(.*)')

    @staticmethod
    def parse_firstline(line):
        ts = payload = None
        try:
            m = PacketFactory.TS_PATTERN.search(line)
            if m:
                utc = time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                ts = timegm(utc)
                payload = m.group(2).strip()
        except Exception as e:
            logerr("parse timestamp failed for '%s': %s" % (line, e))
        return ts, payload


class SDRConfigurationEditor(weewx.drivers.AbstractConfEditor):
    @property
    def default_stanza(self):
        return """
[SDR]
    # This section is for the software-defined radio driver.

    # The driver to use
    driver = user.sdr

    # How to invoke the rtl_433 command
#    cmd = %s

    # The sensor map associates observations with database fields.  Each map
    # element consists of a tuple on the left and a database field name on the
    # right.  The tuple on the left consists of:
    #
    #   <observation_name>.<sensor_identifier>.<packet_type>
    #
    # The sensor_identifier is hardware-specific.  For example, Acurite sensors
    # have a 4 character hexadecimal identifier, whereas fine offset sensor
    # clusters have a 4 digit identifier.
    #
    # glob-style pattern matching is supported for the sensor_identifier.
    #
# map data from any fine offset sensor cluster to database field names
#    [[sensor_map]]
#        windGust = wind_gust.*.FOWH1080Packet
#        outBatteryStatus = battery.*.FOWH1080Packet
#        rain_total = rain_total.*.FOWH1080Packet
#        windSpeed = wind_speed.*.FOWH1080Packet
#        windDir = wind_dir.*.FOWH1080Packet
#        outHumidity = humidity.*.FOWH1080Packet
#        outTemp = temperature.*.FOWH1080Packet

""" % DEFAULT_CMD


class SDRDriver(weewx.drivers.AbstractDevice):

    # map the counter total to the counter delta.  for example, the pair
    #   rain:rain_total
    # will result in a delta called 'rain' from the cumulative 'rain_total'.
    # these are applied to mapped packets.
    DEFAULT_DELTAS = {
        'rain': 'rain_total',
        'lightning_strike_count': 'strikes_total'}

    # what is the difference in timestamp values at which we consider two
    # data samples to be different?  some hardware emits duplicate data, and
    # sometimes the rtl_433/rtl-sdr emits duplicate data.  when all of the
    # data are identical, including timestamp, we can easily reject duplicates.
    # when the data are identical but vary only by timestamp, then we can
    # reject duplicates only if the difference in timestamp is smaller than
    # the sensor sampling period.  there is no way to know this, so we make
    # that number a configurable option.  the default is 1 second.
    TIMESTAMP_MATCH_THRESHHOLD = 1

    def __init__(self, **stn_dict):
        loginf('driver version is %s' % DRIVER_VERSION)
        self._model = stn_dict.get('model', 'SDR')
        loginf('model is %s' % self._model)
        self._log_lines = tobool(stn_dict.get('log_lines', False))
        self._log_unknown = tobool(stn_dict.get('log_unknown_sensors', False))
        self._log_unmapped = tobool(stn_dict.get('log_unmapped_sensors', False))
        self._log_packets  = tobool(stn_dict.get('log_packets', True))
        self._log_dups     = tobool(stn_dict.get('log_duplicate_readings', True))
        self._sensor_map = stn_dict.get('sensor_map', {})
        loginf('sensor map is %s' % self._sensor_map)
        self._deltas = stn_dict.get('deltas', SDRDriver.DEFAULT_DELTAS)
        loginf('deltas is %s' % self._deltas)
        self._ts_delta = stn_dict.get('timestamp_match_threshhold', SDRDriver.TIMESTAMP_MATCH_THRESHHOLD)
        self._counter_values = dict()
        cmd = stn_dict.get('cmd', DEFAULT_CMD)
        path = stn_dict.get('path', None)
        ld_library_path = stn_dict.get('ld_library_path', None)
        self._last_pkt = None # avoid duplicate sequential packets
        self._mgr = ProcManager()
        self._mgr.startup(cmd, path, ld_library_path)

    def closePort(self):
        self._mgr.shutdown()

    @property
    def hardware_name(self):
        return self._model

    def genLoopPackets(self):
        while self._mgr.running():
            for lines in self._mgr.get_stdout():
                if self._log_lines:
                    loginf("lines: %s" % lines)
                for packet in PacketFactory.create(lines):
                    if packet:
                        pkt = self.map_to_fields(packet, self._sensor_map)
                        if pkt:
                            if not self._packets_match(pkt, self._last_pkt):
                                if self._log_packets:
                                    logdbg("packet=%s" % pkt)
                                self._last_pkt = pkt
                                self._calculate_deltas(pkt)
                                yield pkt
                            else:
                                if self._log_dups:
                                    logdbg("ignoring duplicate packet %s" % pkt)
                        elif self._log_unmapped:
                            loginf("unmapped: %s" % packet)
                    elif self._log_unknown:
                        loginf("unparsed: %s" % lines)
            # report any errors
            for line in self._mgr.get_stderr():
                logerr(line)
        else:
            for line in self._mgr.get_stderr():
                logerr(line)
            raise weewx.WeeWxIOError("rtl_433 process is not running")

    def _packets_match(self, pkt1, pkt2):
        # see if two packets match.  this is more than just a direct comparison
        # of packets.  if the data match, but the timestamps are different,
        # then that is considered different, but only if the timestamp is
        # bigger than the sampling period for the hardware.
        if pkt1 != pkt2:
            return False
        a = copy.deepcopy(pkt1)
        b = copy.deepcopy(pkt2)
        a_ts = a.pop('dateTime')
        b_ts = b.pop('dateTime')
        if a != b:
            return False
        if abs(a_ts - b_ts) > self._ts_delta:
            return False
        return True

    def _calculate_deltas(self, pkt):
        for k in self._deltas:
            label = self._deltas[k]
            if label in pkt:
                pkt[k] = self._calculate_delta(
                    label, pkt[label], self._counter_values.get(label))
                self._counter_values[label] = pkt[label]

    @staticmethod
    def _calculate_delta(label, newtotal, oldtotal):
        delta = None
        if newtotal is not None and oldtotal is not None:
            if newtotal >= oldtotal:
                delta = newtotal - oldtotal
            else:
                loginf("%s decrement ignored:"
                       " new: %s old: %s" % (label, newtotal, oldtotal))
        return delta

    @staticmethod
    def map_to_fields(pkt, sensor_map):
        # selectively get elements from the packet using the specified sensor
        # map.  if the identifier is found, then use its value.  if not, then
        # skip it completely (it is not given a None value).  include the
        # time stamp and unit system only if we actually got data.
        packet = dict()
        for n in sensor_map.keys():
            label = SDRDriver._find_match(sensor_map[n], pkt.keys())
            if label:
                packet[n] = pkt.get(label)
        if packet:
            for k in ['dateTime', 'usUnits']:
                packet[k] = pkt[k]
        return packet

    @staticmethod
    def _find_match(pattern, keylist):
        # find the first key in pkt that matches the specified pattern.
        # the general form of a pattern is:
        #   <observation_name>.<sensor_id>.<packet_type>
        # do glob-style matching.
        if pattern in keylist:
            return pattern
        match = None
        pparts = pattern.split('.')
        if len(pparts) == 3:
            for k in keylist:
                kparts = k.split('.')
                if (len(kparts) == 3 and
                    SDRDriver._part_match(pparts[0], kparts[0]) and
                    SDRDriver._part_match(pparts[1], kparts[1]) and
                    SDRDriver._part_match(pparts[2], kparts[2])):
                    match = k
                    break
                elif pparts[0] == k:
                    match = k
                    break
        return match

    @staticmethod
    def _part_match(pattern, value):
        # use glob matching for parts of the tuple
        matches = fnmatch.filter([value], pattern)
        return True if matches else False


def main():
    import optparse
    import syslog

    usage = """%prog [--debug] [--help] [--version]
        [--action=(show-packets | show-detected | list-supported)]
        [--cmd=RTL_CMD] [--path=PATH] [--ld_library_path=LD_LIBRARY_PATH]

Actions:
  show-packets: display each packet (default)
  show-detected: display a running count of the number of each packet type
  list-supported: show a list of the supported packet types

Hide:
  This is a comma-separate list of the types of data that should not be
  displayed.  Default is to show everything."""

    syslog.openlog('sdr', syslog.LOG_PID | syslog.LOG_CONS)
    syslog.setlogmask(syslog.LOG_UPTO(syslog.LOG_INFO))
    parser = optparse.OptionParser(usage=usage)
    parser.add_option('--version', dest='version', action='store_true',
                      help='display driver version')
    parser.add_option('--debug', dest='debug', action='store_true',
                      help='display diagnostic information while running')
    parser.add_option('--cmd', dest='cmd', default=DEFAULT_CMD,
                      help='rtl command with options')
    parser.add_option('--path', dest='path',
                      help='value for PATH')
    parser.add_option('--ld_library_path', dest='ld_library_path',
                      help='value for LD_LIBRARY_PATH')
    parser.add_option('--config',
                      help='configuration file with sensor map')
    parser.add_option('--hide', dest='hidden', default='empty',
                      help='output to be hidden as comma-delimited list: out, parsed, unparsed, mapped, unmapped, empty')
    parser.add_option('--action', dest='action', default='show-packets',
                      help='actions include show-packets, show-detected, list-supported')

    (options, args) = parser.parse_args()

    if options.version:
        print("sdr driver version %s" % DRIVER_VERSION)
        exit(1)

    if options.debug:
        syslog.setlogmask(syslog.LOG_UPTO(syslog.LOG_DEBUG))

    sensor_map = dict()
    if options.config:
        import weecfg
        config_path, config_dict = weecfg.read_config(options.config)
        sensor_map = config_dict.get('SDR', {}).get('sensor_map', {})

    if options.action == 'list-supported':
        pkt_names = PacketFactory.known_packets()
        print("%s known packet types" % len(pkt_names))
        for pt in pkt_names:
            print("%s '%s'" % (pt.__name__, pt.IDENTIFIER))
    elif options.action == 'show-detected':
        # display identifiers for detected sensors
        mgr = ProcManager()
        mgr.startup(options.cmd, path=options.path,
                    ld_library_path=options.ld_library_path)
        detected = dict()
        for lines in mgr.get_stdout():
            # print("out: %s" % lines)
            for p in PacketFactory.create(lines):
                if p:
                    del p['usUnits']
                    del p['dateTime']
                    keys = p.keys()
                    label = re.sub(r'^[^\.]+', '', keys[0])
                    if label not in detected:
                        detected[label] = 0
                    detected[label] += 1
                print(detected)
    else:
        # display output and parsed/unparsed packets
        hidden = [x.strip() for x in options.hidden.split(',')]
        mgr = ProcManager()
        mgr.startup(options.cmd, path=options.path,
                    ld_library_path=options.ld_library_path)
        for lines in mgr.get_stdout():
            if 'out' not in hidden and (
                    'empty' not in hidden or len(lines)):
                print("out: %s" % lines)
            for p in PacketFactory.create(lines):
                if p:
                    if 'parsed' not in hidden:
                        print('parsed: %s' % p)
                    if sensor_map:
                        m = SDRDriver.map_to_fields(p, sensor_map)
                        if m:
                            if 'mapped' not in hidden:
                                print('mapped: %s' % m)
                        else:
                            if 'unmapped' not in hidden:
                                print('unmapped: %s' % p)
                else:
                    if 'unparsed' not in hidden and (
                            'empty' not in hidden or len(lines)):
                        print("unparsed: %s" % lines)
        for line in mgr.get_stderr():
            line = line.rstrip()
            print("err: %s" % line)


if __name__ == '__main__':
    main()
