import struct
import time
import logging
from datetime import datetime
from Crypto.Cipher import AES
from Queue import Queue, Empty
from bluepy.btle import Peripheral, DefaultDelegate, ADDR_TYPE_RANDOM, BTLEException, BTLEDisconnectError
import crc16
import os
import struct

import base64
import hashlib
import json
import requests
import urllib

from constants import UUIDS, AUTH_STATES, ALERT_TYPES, QUEUE_TYPES

def get_hex(data):
    return ' '.join(x.encode('hex') for x in data)

def make_hex(a):
    a = [int(x, 16) for x in a]
    a = [chr(x) for x in a]
    a = ''.join(a)
    return a

def get_sign(rand):
    url = "https://api-mifit-us.huami.com/v1/device/binds.json?r=0&t=0&device_type=0&appid=0&callid=0&channel=0&country=0&cv=0&device=0&lang=0&publickeyhash=0&timezone=0&v=0"
    url += "&userid=3033099507"
    url += "&" + urllib.urlencode({'random': rand})
    
    headers = {'apptoken': <APP_TOKEN>}
    r = requests.get(url, headers=headers)
    sign = r.json()['data']['signature']
    return sign

def encrypt(key, message):
        aes = AES.new(key, AES.MODE_ECB)
        return aes.encrypt(message)

class AuthenticationDelegate(DefaultDelegate):

    """This Class inherits DefaultDelegate to handle the authentication process."""

    def __init__(self, device):
        DefaultDelegate.__init__(self)
        self.device = device

    def handleNotification(self, hnd, data):
        if hnd == 0x60:
            if data[:3] == b'\x10\x01\x81':
                const_num = data[3:]
                self.device._log.debug("const_num: " + get_hex(const_num))
            elif data[:3] == b'\x10\x82\x01':
                rnd_num = data[3:]
                self.device._log.debug("rnd_num: " + get_hex(rnd_num))
                if self.device.is_auth:
                    self.device._send_sign(rnd_num)
                else:
                    self.device._send_enc_rdn(rnd_num)
            elif data[:3] == b'\x10\x83\x01':
                if self.device.is_auth:
                    self.device._log.info("Signature Accepted")
                    self.device.waitForNotifications(self.device.timeout)
                else:
                    self.device._log.info("Paired!")
                    self.device.state = AUTH_STATES.PAIR_OK
            elif data[:3] == b'\x10\x83\x08':
                self.device._log.info("Authentication Failed")
                self.device.state = AUTH_STATES.AUTH_FAILED
            elif data[:3] == b'\x10\x01\x01':
                self.device._log.debug("Authentication Accepted")
                self.device.state = AUTH_STATES.AUTH_OK
            elif data[:3] == b'\x10\x01\x02':
                self.device._log.debug("Authentication Rejected")
                self.device.state = AUTH_STATES.AUTH_FAILED
            elif data[:3] == b'\x10\x06\x01':
                self.device._log.info("Unpaired")
                self.device.state = None
                self.device.writeCharacteristic(0x61, b'\x00\x00')
            else:
                self.device._log.error("Handle Notification Unknown: " + hex(hnd) + " " + get_hex(data))
        elif hnd == 0x4d:
            self.device._log.info("Choose on device...")
            if not self.device.waitForNotifications(30):
                self.device.state = AUTH_STATES.AUTH_FAILED

        elif hnd == self.device._char_heart_measure.getHandle():
            self.device.queue.put((QUEUE_TYPES.HEART, data))
        
        elif hnd == 0x38:
            # Not sure about this, need test
            if len(data) == 20 and struct.unpack('b', data[0])[0] == 1:
                self.device.queue.put((QUEUE_TYPES.RAW_ACCEL, data))
            elif len(data) == 16:
                self.device.queue.put((QUEUE_TYPES.RAW_HEART, data))
        
        else:
            self.device._log.error("Unhandled Response " + hex(hnd) + ": " +
                                   str(data.encode("hex")) + " len:" + str(len(data)))


class MiBand4(Peripheral):

    def __init__(self, mac_address, timeout=1, debug=True):
        FORMAT = '%(asctime)-15s %(name)s (%(levelname)s) > %(message)s'
        logging.basicConfig(format=FORMAT)
        log_level = logging.WARNING if not debug else logging.DEBUG
        self._log = logging.getLogger(self.__class__.__name__)
        self._log.setLevel(log_level)

        self.is_auth = False
        self.is_pair = False
        self.num = b'\x00'

        self._log.info('Connecting to ' + mac_address)
        Peripheral.__init__(self, mac_address)
        self._log.info('Connected')
        
        self.timeout = timeout
        self.mac_address = mac_address
        self.state = None
        self.queue = Queue()
        self.heart_measure_callback = None
        self.heart_raw_callback = None
        self.accel_raw_callback = None

        self.svc_1 = self.getServiceByUUID(UUIDS.SERVICE_MIBAND1)
        self.svc_2 = self.getServiceByUUID(UUIDS.SERVICE_MIBAND2)
        self.svc_heart = self.getServiceByUUID(UUIDS.SERVICE_HEART_RATE)

        self._char_auth = self.svc_2.getCharacteristics(UUIDS.CHARACTERISTIC_AUTH)[0]
        self._desc_auth = self._char_auth.getDescriptors(forUUID=UUIDS.NOTIFICATION_DESCRIPTOR)[0]
        
        self._char_heart_ctrl = self.svc_heart.getCharacteristics(UUIDS.CHARACTERISTIC_HEART_RATE_CONTROL)[0]
        self._char_heart_measure = self.svc_heart.getCharacteristics(UUIDS.CHARACTERISTIC_HEART_RATE_MEASURE)[0]

        self.setDelegate(AuthenticationDelegate(self))
        
    # Auth helpers ######################################################################

    def auth(self):
        self._log.info('Authenticating...')
        self.is_auth = True
        
        self._auth_notif()
        self._req_rdn()

    def pair(self):
        if self.state != AUTH_STATES.AUTH_OK:
            self._log.error('No Auth!')
            return
        self._log.info('Pairing...')
        self.is_auth = False
        self.is_pair = True
        
        self._req_rdn()
        
    def unpair(self):
        if self.state != AUTH_STATES.PAIR_OK:
            self._log.error('No Pair!')
            return
        self._log.info('Unpairing...')
        self.is_auth = False
        self.is_pair = False
        
        self._req_rdn()


    def _auth_notif(self):
        self._log.debug("Enabling Auth Service notifications status...")
        self.writeCharacteristic(0x4e, b'\x01\x00', True)

    def _req_rdn(self):
        self._log.debug("Requesting random number...")
        self.writeCharacteristic(0x61, b'\x01\x00', True)
        self.writeCharacteristic(0x60, b'\x82' + self.num + b'\x02')
        self.waitForNotifications(self.timeout)
        
    def _send_enc_rdn(self, rnd_num):
        self._log.debug("Sending encrypted random number")                
        enc_rnd = encrypt(self._KEY, rnd_num)
        if self.is_pair:
            cmd = b'\x83' + self.num + enc_rnd
        else:
            cmd = b'\x06' + self.num + enc_rnd
        self.writeCharacteristic(0x60, cmd)
        self.waitForNotifications(self.timeout)

    def _send_sign(self, rnd_num):
        self._log.debug("Sending signature")      
        mac = make_hex(self.mac_address.split(":"))
        new_rnd = mac + rnd_num
        sha = hashlib.sha256(new_rnd).hexdigest()
        
        key = sha[:32]
        key = make_hex(['0x'+key[i:i+2] for i in range(0, len(key), 2)])
        self._KEY = key
        self._log.info("Auth Key: " + get_hex(self._KEY))

        rand = make_hex(['0x'+sha[i:i+2] for i in range(0, len(sha), 2)])
        rand = base64.b64encode(rand)

        sign = get_sign(rand)
        s = base64.b64decode(sign)
        
        s0 = s[:15]
        s1 = s[15:15+17]
        s2 = s[15+17:15+17+17]
        s3 = s[15+17+17:15+17+17+15]
        
        self.writeCharacteristic(0x4d, b'\x00\x04\x00\x83' + self.num + s0)
        self.writeCharacteristic(0x4d, b'\x00\x44\x01' + s1)
        self.writeCharacteristic(0x4d, b'\x00\x44\x02' + s2)
        self.writeCharacteristic(0x4d, b'\x00\x44\x03' + s3 + b'\x00\x00')
        self.writeCharacteristic(0x4d, b'\x00\x84\x04\x00\x00')
        self.waitForNotifications(self.timeout)
                    

    # Parse helpers ###################################################################

    def _parse_raw_accel(self, bytes):
        res = []
        for i in xrange(3):
            g = struct.unpack('hhh', bytes[2 + i * 6:8 + i * 6])
            res.append({'x': g[0], 'y': g[1], 'wtf': g[2]})
        return res

    def _parse_raw_heart(self, bytes):
        res = struct.unpack('HHHHHHH', bytes[2:])
        return res

    def _parse_date(self, bytes):
        year = struct.unpack('h', bytes[0:2])[0] if len(bytes) >= 2 else None
        month = struct.unpack('b', bytes[2])[0] if len(bytes) >= 3 else None
        day = struct.unpack('b', bytes[3])[0] if len(bytes) >= 4 else None
        hours = struct.unpack('b', bytes[4])[0] if len(bytes) >= 5 else None
        minutes = struct.unpack('b', bytes[5])[0] if len(bytes) >= 6 else None
        seconds = struct.unpack('b', bytes[6])[0] if len(bytes) >= 7 else None
        day_of_week = struct.unpack('b', bytes[7])[0] if len(bytes) >= 8 else None
        fractions256 = struct.unpack('b', bytes[8])[0] if len(bytes) >= 9 else None

        return {"date": datetime(*(year, month, day, hours, minutes, seconds)), "day_of_week": day_of_week, "fractions256": fractions256}

    def _parse_battery_response(self, bytes):
        level = struct.unpack('b', bytes[1])[0] if len(bytes) >= 2 else None
        last_level = struct.unpack('b', bytes[19])[0] if len(bytes) >= 20 else None
        status = 'normal' if struct.unpack('b', bytes[2])[0] == 0 else "charging"
        datetime_last_charge = self._parse_date(bytes[11:18])
        datetime_last_off = self._parse_date(bytes[3:10])

        res = {
            "status": status,
            "level": level,
            "last_level": last_level,
            "last_level": last_level,
            "last_charge": datetime_last_charge,
            "last_off": datetime_last_off
        }
        return res

    # Queue ###################################################################

    def _get_from_queue(self, _type):
        try:
            res = self.queue.get(False)
        except Empty:
            return None
        if res[0] != _type:
            self.queue.put(res)
            return None
        return res[1]

    def _parse_queue(self):
        while True:
            try:
                res = self.queue.get(False)
                _type = res[0]
                if self.heart_measure_callback and _type == QUEUE_TYPES.HEART:
                    self.heart_measure_callback(struct.unpack('bb', res[1])[1])
                elif self.heart_raw_callback and _type == QUEUE_TYPES.RAW_HEART:
                    self.heart_raw_callback(self._parse_raw_heart(res[1]))
                elif self.accel_raw_callback and _type == QUEUE_TYPES.RAW_ACCEL:
                    self.accel_raw_callback(self._parse_raw_accel(res[1]))
            except Empty:
                break

    # API ####################################################################
        
    # def initialize(self):
    #     print("initialize")
    #     self.setDelegate(AuthenticationDelegate(self))
    #     self._req_rdn()
    #     self._send_key()

    #     while True:
    #         self.waitForNotifications(0.1)
    #         if self.state == AUTH_STATES.AUTH_OK:
    #             self._log.info('Initialized')
    #             self._auth_notif(False)
    #             return True
    #         elif self.state is None:
    #             continue

    #         self._log.error(self.state)
    #         return False

    # def authenticate(self):
    #     print("authenticate")
    #     self.setDelegate(AuthenticationDelegate(self))
    #     self._req_rdn()

    #     while True:
    #         self.waitForNotifications(0.1)
    #         if self.state == AUTH_STATES.AUTH_OK:
    #             self._log.info('Authenticated')
    #             return True
    #         elif self.state is None:
    #             continue

    #         self._log.error(self.state)
    #         return False

    def get_battery_info(self):
        char = self.svc_1.getCharacteristics(UUIDS.CHARACTERISTIC_BATTERY)[0]
        return self._parse_battery_response(char.read())

    def get_current_time(self):
        char = self.svc_1.getCharacteristics(UUIDS.CHARACTERISTIC_CURRENT_TIME)[0]
        return self._parse_date(char.read()[0:9])

    def get_revision(self):
        svc = self.getServiceByUUID(UUIDS.SERVICE_DEVICE_INFO)
        char = svc.getCharacteristics(UUIDS.CHARACTERISTIC_REVISION)[0]
        data = char.read()
        return data

    def get_hrdw_revision(self):
        svc = self.getServiceByUUID(UUIDS.SERVICE_DEVICE_INFO)
        char = svc.getCharacteristics(UUIDS.CHARACTERISTIC_HRDW_REVISION)[0]
        data = char.read()
        return data

    def set_encoding(self, encoding="en_US"):
        char = self.svc_1.getCharacteristics(UUIDS.CHARACTERISTIC_CONFIGURATION)[0]
        packet = struct.pack('5s', encoding)
        packet = b'\x06\x17\x00' + packet
        return char.write(packet)

    def set_heart_monitor_sleep_support(self, enabled=True, measure_minute_interval=1):
        char_m = self.svc_heart.getCharacteristics(UUIDS.CHARACTERISTIC_HEART_RATE_MEASURE)[0]
        char_d = char_m.getDescriptors(forUUID=UUIDS.NOTIFICATION_DESCRIPTOR)[0]
        char_d.write(b'\x01\x00', True)
        self._char_heart_ctrl.write(b'\x15\x00\x00', True)
        # measure interval set to off
        self._char_heart_ctrl.write(b'\x14\x00', True)
        if enabled:
            self._char_heart_ctrl.write(b'\x15\x00\x01', True)
            # measure interval set
            self._char_heart_ctrl.write(b'\x14' + str(measure_minute_interval).encode(), True)
        char_d.write(b'\x00\x00', True)

    def get_serial(self):
        svc = self.getServiceByUUID(UUIDS.SERVICE_DEVICE_INFO)
        char = svc.getCharacteristics(UUIDS.CHARACTERISTIC_SERIAL)[0]
        data = char.read()
        serial = struct.unpack('12s', data[-12:])[0] if len(data) == 12 else None
        return serial

    def get_steps(self):
        char = self.svc_1.getCharacteristics(UUIDS.CHARACTERISTIC_STEPS)[0]
        a = char.read()
        steps = struct.unpack('h', a[1:3])[0] if len(a) >= 3 else None
        meters = struct.unpack('h', a[5:7])[0] if len(a) >= 7 else None
        fat_gramms = struct.unpack('h', a[2:4])[0] if len(a) >= 4 else None
        # why only 1 byte??
        callories = struct.unpack('b', a[9])[0] if len(a) >= 10 else None
        return {
            "steps": steps,
            "meters": meters,
            "fat_gramms": fat_gramms,
            "callories": callories
        }

    def send_alert(self, _type):
        svc = self.getServiceByUUID(UUIDS.SERVICE_ALERT)
        char = svc.getCharacteristics(UUIDS.CHARACTERISTIC_ALERT)[0]
        char.write(_type)

    def send_custom_alert(self, type):
        if type == 5:
            base_value = '\x05\x01'
        elif type == 4:
            base_value = '\x04\x01'
        elif type == 3:
            base_value = '\x03\x01'
        phone = "Mom" #raw_input('Sender Name or Caller ID')
        svc = self.getServiceByUUID(UUIDS.SERVICE_ALERT_NOTIFICATION)
        char = svc.getCharacteristics(UUIDS.CHARACTERISTIC_CUSTOM_ALERT)[0]
        char.write(base_value+phone, withResponse=True)

    def change_date(self):
        print('Change date and time')
        svc = self.getServiceByUUID(UUIDS.SERVICE_MIBAND1)
        char = svc.getCharacteristics(UUIDS.CHARACTERISTIC_CURRENT_TIME)[0]
        # date = raw_input('Enter the date in dd-mm-yyyy format\n')
        # time = raw_input('Enter the time in HH:MM:SS format\n')
        #
        # day = int(date[:2])
        # month = int(date[3:5])
        # year = int(date[6:10])
        # fraction = year / 256
        # rem = year % 256
        #
        # hour = int(time[:2])
        # minute = int(time[3:5])
        # seconds =  int(time[6:])
        #
        # write_val =  format(rem, '#04x') + format(fraction, '#04x') + format(month, '#04x') + format(day, '#04x') + format(hour, '#04x') + format(minute, '#04x') + format(seconds, '#04x') + format(5, '#04x') + format(0, '#04x') + format(0, '#04x') +'0x16'
        # write_val = write_val.replace('0x', '\\x')
        # print(write_val)
        char.write('\xe2\x07\x01\x1e\x00\x00\x00\x00\x00\x00\x16', withResponse=True)
        raw_input('Date Changed, press any key to continue')
    
    def dfuUpdate(self, fileName):
        print('Update Firmware/Resource')
        svc = self.getServiceByUUID(UUIDS.SERVICE_DFU_FIRMWARE)
        char = svc.getCharacteristics(UUIDS.CHARACTERISTIC_DFU_FIRMWARE)[0]
        extension = os.path.splitext(fileName)[1][1:]
        fileSize = os.path.getsize(fileName)
        # calculating crc checksum of firmware
        #crc16
        crc = 0xFFFF
        with open(fileName) as f:
            while True:
                c = f.read(1)
                if not c:
                    break
                cInt = int(c.encode('hex'), 16) #converting hex to int
                # now calculate crc
                crc = ((crc >> 8) | (crc << 8)) & 0xFFFF
                crc ^= (cInt & 0xff)
                crc ^= ((crc & 0xff) >> 4)
                crc ^= (crc << 12) & 0xFFFF
                crc ^= ((crc & 0xFF) << 5) & 0xFFFFFF
        crc &= 0xFFFF
        print('CRC Value is-->', crc)
        raw_input('Press Enter to Continue')
        if extension.lower() == "res":
            # file size hex value is
            char.write('\x01'+ struct.pack("<i", fileSize)[:-1] +'\x02', withResponse=True)
        elif extension.lower() == "fw":
            char.write('\x01' + struct.pack("<i", fileSize)[:-1], withResponse=True)
        char.write("\x03", withResponse=True)
        char1 = svc.getCharacteristics(UUIDS.CHARACTERISTIC_DFU_FIRMWARE_WRITE)[0]
        with open(fileName) as f:
          while True:
            c = f.read(20) #takes 20 bytes :D
            if not c:
              print "Update Over"
              break
            print('Writing Resource', c.encode('hex'))
            char1.write(c)
        # after update is done send these values
        char.write(b'\x00', withResponse=True)
        self.waitForNotifications(0.5)
        print('CheckSum is --> ', hex(crc & 0xFF), hex((crc >> 8) & 0xFF))
        checkSum = b'\x04' + chr(crc & 0xFF) + chr((crc >> 8) & 0xFF)
        char.write(checkSum, withResponse=True)
        if extension.lower() == "fw":
            self.waitForNotifications(0.5)
            char.write('\x05', withResponse=True)
        print('Update Complete')
        raw_input('Press Enter to Continue')
    
    def start_raw_data_realtime(self, heart_measure_callback=None, heart_raw_callback=None, accel_raw_callback=None):
            char_m = self.svc_heart.getCharacteristics(UUIDS.CHARACTERISTIC_HEART_RATE_MEASURE)[0]
            char_d = char_m.getDescriptors(forUUID=UUIDS.NOTIFICATION_DESCRIPTOR)[0]
            char_ctrl = self.svc_heart.getCharacteristics(UUIDS.CHARACTERISTIC_HEART_RATE_CONTROL)[0]

            if heart_measure_callback:
                self.heart_measure_callback = heart_measure_callback
            if heart_raw_callback:
                self.heart_raw_callback = heart_raw_callback
            if accel_raw_callback:
                self.accel_raw_callback = accel_raw_callback

            char_sensor = self.svc_1.getCharacteristics(UUIDS.CHARACTERISTIC_SENSOR)[0]

            # stop heart monitor continues & manual
            char_ctrl.write(b'\x15\x02\x00', True)
            char_ctrl.write(b'\x15\x01\x00', True)
            # WTF
            # char_sens_d1.write(b'\x01\x00', True)
            # enabling accelerometer & heart monitor raw data notifications
            char_sensor.write(b'\x01\x03\x19')
            # IMO: enablee heart monitor notifications
            char_d.write(b'\x01\x00', True)
            # start hear monitor continues
            char_ctrl.write(b'\x15\x01\x01', True)
            # WTF
            char_sensor.write(b'\x02')
            t = time.time()
            x = 3
            while x > 0:
                self.waitForNotifications(0.5)
                self._parse_queue()
                # send ping request every 12 sec
                if (time.time() - t) >= 12:
                    char_ctrl.write(b'\x16', True)
                    t = time.time()
                    x -= 1
            self.stop_realtime()

    def stop_realtime(self):
            char_m = self.svc_heart.getCharacteristics(UUIDS.CHARACTERISTIC_HEART_RATE_MEASURE)[0]
            char_d = char_m.getDescriptors(forUUID=UUIDS.NOTIFICATION_DESCRIPTOR)[0]
            char_ctrl = self.svc_heart.getCharacteristics(UUIDS.CHARACTERISTIC_HEART_RATE_CONTROL)[0]

            char_sensor1 = self.svc_1.getCharacteristics(UUIDS.CHARACTERISTIC_HZ)[0]
            char_sens_d1 = char_sensor1.getDescriptors(forUUID=UUIDS.NOTIFICATION_DESCRIPTOR)[0]

            char_sensor2 = self.svc_1.getCharacteristics(UUIDS.CHARACTERISTIC_SENSOR)[0]

            # stop heart monitor continues
            char_ctrl.write(b'\x15\x01\x00', True)
            char_ctrl.write(b'\x15\x01\x00', True)
            # IMO: stop heart monitor notifications
            char_d.write(b'\x00\x00', True)
            # WTF
            char_sensor2.write(b'\x03')
            # IMO: stop notifications from sensors
            char_sens_d1.write(b'\x00\x00', True)

            self.heart_measure_callback = None
            self.heart_raw_callback = None
            self.accel_raw_callback = None

    def start_get_previews_data(self, start_timestamp):
            self._auth_previews_data_notif(True)
            self.waitForNotifications(0.1)
            print("Trigger activity communication")
            year = struct.pack("<H", start_timestamp.year)
            month = struct.pack("<H", start_timestamp.month)[0]
            day = struct.pack("<H", start_timestamp.day)[0]
            hour = struct.pack("<H", start_timestamp.hour)[0]
            minute = struct.pack("<H", start_timestamp.minute)[0]
            ts = year + month + day + hour + minute
            trigger = b'\x01\x01' + ts + b'\x00\x08'
            self._char_fetch.write(trigger, False)
            self.active = True
