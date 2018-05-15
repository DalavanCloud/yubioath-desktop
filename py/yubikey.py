#!/usr/bin/env python
# -*- coding: utf-8 -*-

import json
import logging
import types
import ykman.logging_setup

from base64 import b32encode, b64decode
from binascii import a2b_hex, b2a_hex

from ykman.descriptor import get_descriptors, Descriptor
from ykman.util import (TRANSPORT, parse_b32_key)
from ykman.driver_otp import YkpersError
from ykman.otp import OtpController
from ykman.driver_ccid import (
    APDUError, open_devices as open_ccid, READER_NAME_YK)
from ykman.oath import (ALGO, OATH_TYPE, OathController, CredentialData,
                        Credential, Code, SW)
from ykman.settings import Settings
from qr import qrparse, qrdecode


logger = logging.getLogger(__name__)


def as_json(f):
    def wrapped(*args):
        return json.dumps(f(*(json.loads(a) for a in args)))
    return wrapped


def cred_to_dict(cred):
    return {
        'key': cred.key.decode('utf8'),
        'issuer': cred.issuer,
        'name': cred.name,
        'oath_type': cred.oath_type.name,
        'period': cred.period,
        'touch': cred.touch
    }


def cred_from_dict(data):
    return Credential(
        data['key'].encode('utf-8'),
        OATH_TYPE[data['oath_type']],
        data['touch']
    )


def code_to_dict(code):
    return {
        'value': code.value,
        'valid_from': code.valid_from,
        'valid_to': min(code.valid_to, 9999999999)  # No Inf in JSON.
    } if code else None


def pair_to_dict(cred, code):
    return {
        'credential': cred_to_dict(cred),
        'code': code_to_dict(code)
    }


def credential_data_to_dict(credentialData):
    return {
        'secret': b32encode(credentialData.secret).decode(),
        'issuer': credentialData.issuer,
        'name': credentialData.name,
        'oath_type': credentialData.oath_type.name,
        'algorithm': credentialData.algorithm.name,
        'digits': credentialData.digits,
        'period': credentialData.period,
        'counter': credentialData.counter,
        'touch': credentialData.touch
    }


class Controller(object):
    _descriptor = None
    _dev_info = {}
    _key = None

    def __init__(self):
        self.settings = Settings('oath')

        # Wrap all args and return values as JSON.
        for f in dir(self):
            if not f.startswith('_'):
                func = getattr(self, f)
                if isinstance(func, types.MethodType):
                    setattr(self, f, as_json(func))

    def _count_devices(self, otp_mode=False):
        descriptors = get_descriptors()
        if not otp_mode:
            readers = list(open_ccid(exclude=READER_NAME_YK))
            n_keys = len(descriptors) + len(readers)
        else:
            n_keys = len(descriptors)
        return n_keys

    def count_devices(self, otp_mode=False):
        return self._count_devices(otp_mode=otp_mode)

    def refresh(self, otp_mode=False):
        n_keys = self._count_devices(otp_mode=otp_mode)
        if (n_keys != 1):
            self._descriptor = None
            return None

        descriptors = get_descriptors()
        if not otp_mode:
            readers = list(open_ccid(exclude=READER_NAME_YK))

        if not otp_mode and readers:
            desc = Descriptor.from_driver(readers[0])
        else:
            desc = descriptors[0]

        unmatched_otp_mode = otp_mode and not desc.mode.has_transport(
            TRANSPORT.OTP)
        unmatched_ccid_mode = not otp_mode and not desc.mode.has_transport(
            TRANSPORT.CCID)

        if unmatched_otp_mode or unmatched_ccid_mode:
            return {
                'transports': [
                    t.name for t in TRANSPORT.split(desc.mode.transports)
                ],
                'usable': False,
            }

        if desc.fingerprint != (
                self._descriptor.fingerprint if self._descriptor else None) \
                or not otp_mode and not self._dev_info.get('version'):
            try:
                dev = desc.open_device(TRANSPORT.OTP if otp_mode
                                       else TRANSPORT.CCID)
                if otp_mode:
                    version = None
                else:
                    controller = OathController(dev.driver)
                    version = controller.version
            except Exception as e:
                logger.debug('Failed to refresh YubiKey', exc_info=e)
                return None

            self._descriptor = desc
            self._dev_info = {
                'usable': True,
                'name': dev.device_name,
                'version': version,
                'serial': dev.serial or '',
                'usb_interfaces_supported': [
                    t.name for t in TRANSPORT
                    if t & dev.config.usb_supported],
                'usb_interfaces_enabled': str(dev.mode).split('+')
            }

        return self._dev_info

    def _unlock(self, controller):
        if controller.locked:
            keys = self.settings.get('keys', {})
            if self._key is not None:
                controller.validate(self._key)
            elif controller.id in keys:
                controller.validate(a2b_hex(keys[controller.id]))
            else:
                return False
        return True

    def clear_key(self):
        self._key = None

    def refresh_credentials(self, timestamp):
        try:
            dev = self._descriptor.open_device(TRANSPORT.CCID)
            controller = OathController(dev.driver)
            self._unlock(controller)
            entries = controller.calculate_all(timestamp)
            return [pair_to_dict(cred, code) for (cred, code) in entries
                    if not cred.is_hidden]
        except Exception:
            return []

    def calculate(self, credential, timestamp):
        try:
            dev = self._descriptor.open_device(TRANSPORT.CCID)
            controller = OathController(dev.driver)
            self._unlock(controller)
        except Exception:
            return None
        code = controller.calculate(cred_from_dict(credential), timestamp)
        return code_to_dict(code)

    def calculate_slot_mode(self, slot, digits, timestamp):
        try:
            code = self._read_slot_code(
                slot, digits, timestamp, wait_for_touch=True)
            return pair_to_dict(Credential(self._slot_name(slot),
                                           OATH_TYPE.TOTP, True), code)
        except YkpersError as e:
            if e.errno == 4:
                logger.debug(
                    'Time out error, user probably did not touch the device.')
            else:
                logger.error(
                    'Failed to calculate code in slot mode', exc_info=e)
        except Exception as e:
            logger.error('Failed to calculate code in slot mode', exc_info=e)
        return None

    def refresh_slot_credentials(self, slots, digits, timestamp):
        result = []
        if slots[0]:
            entry = self._read_slot_cred(1, digits[0], timestamp)
            if entry:
                result.append(entry)
        if slots[1]:
            entry = self._read_slot_cred(2, digits[1], timestamp)
            if entry:
                result.append(entry)
        return [pair_to_dict(cred, code) for (cred, code) in result]

    def _read_slot_cred(self, slot, digits, timestamp):
        try:
            code = self._read_slot_code(
                slot, digits, timestamp, wait_for_touch=False)
            return (Credential(self._slot_name(slot), OATH_TYPE.TOTP, False),
                    code)
        except YkpersError as e:
            if e.errno == 11:
                return (Credential(self._slot_name(slot), OATH_TYPE.TOTP, True
                                   ), None)
        except Exception as e:
            return (Credential(str(e).encode(), OATH_TYPE.TOTP, True), None)
        return None

    def _read_slot_code(self, slot, digits, timestamp, wait_for_touch):
        with self._descriptor.open_device(TRANSPORT.OTP) as dev:
            controller = OtpController(dev.driver)
            code = controller.calculate(
                slot, challenge=timestamp, totp=True, digits=int(digits),
                wait_for_touch=wait_for_touch)
            valid_from = timestamp - (timestamp % 30)
            valid_to = valid_from + 30
            return Code(code, valid_from, valid_to)

    def _slot_name(self, slot):
        return "YubiKey Slot {}".format(slot).encode('utf-8')

    def needs_validation(self):
        try:
            dev = self._descriptor.open_device(TRANSPORT.CCID)
            return not self._unlock(OathController(dev.driver))
        except Exception:
            return True

    def get_oath_id(self):
        try:
            dev = self._descriptor.open_device(TRANSPORT.CCID)
            return OathController(dev.driver).id
        except Exception:
            return None

    def provide_password(self, password, remember=False):
        dev = self._descriptor.open_device(TRANSPORT.CCID)
        controller = OathController(dev.driver)
        self._key = controller.derive_key(password)
        try:
            controller.validate(self._key)
        except Exception:
            return False
        if remember:
            keys = self.settings.setdefault('keys', {})
            keys[controller.id] = b2a_hex(self._key).decode()
            self.settings.write()
        return True

    def set_password(self, new_password, remember=False):
        dev = self._descriptor.open_device(TRANSPORT.CCID)
        controller = OathController(dev.driver)
        self._unlock(controller)
        keys = self.settings.setdefault('keys', {})
        if new_password is not None:
            self._key = controller.set_password(new_password)
            if remember:
                keys[controller.id] = b2a_hex(self._key).decode()
            elif controller.id in keys:
                del keys[controller.id]
        else:
            controller.clear_password()
            del keys[controller.id]
            self._key = None
        self.settings.write()

    def add_credential(
            self, name, secret, issuer, oath_type, algo, digits,
            period, touch):
        dev = self._descriptor.open_device(TRANSPORT.CCID)
        controller = OathController(dev.driver)
        self._unlock(controller)
        try:
            secret = parse_b32_key(secret)
        except Exception as e:
            return str(e)
        try:
            controller.put(CredentialData(
                secret, issuer, name, OATH_TYPE[oath_type], ALGO[algo],
                int(digits), int(period), 0, touch
            ))
        except APDUError as e:
            # NEO doesn't return a no space error if full,
            # but a command aborted error. Assume it's because of
            # no space in this context.
            if e.sw in (SW.NO_SPACE, SW.COMMAND_ABORTED):
                return 'No space'
            else:
                raise

    def add_slot_credential(self, slot, key, touch):
        key = parse_b32_key(key)
        with self._descriptor.open_device(TRANSPORT.OTP) as dev:
            controller = OtpController(dev.driver)
            try:
                controller.program_chalresp(int(slot), key, touch)
            except Exception as e:
                return str(e)

    def delete_slot_credential(self, slot):
        with self._descriptor.open_device(TRANSPORT.OTP) as dev:
            controller = OtpController(dev.driver)
            controller.zap_slot(slot)

    def delete_credential(self, credential):
        dev = self._descriptor.open_device(TRANSPORT.CCID)
        controller = OathController(dev.driver)
        self._unlock(controller)
        controller.delete(cred_from_dict(credential))

    def parse_qr(self, screenshot):
        data = b64decode(screenshot['data'])
        image = PixelImage(data, screenshot['width'], screenshot['height'])
        for qr in qrparse.parse_qr_codes(image, 2):
            return credential_data_to_dict(
                CredentialData.from_uri(qrdecode.decode_qr_data(qr)))

    def reset(self):
        dev = self._descriptor.open_device(TRANSPORT.CCID)
        controller = OathController(dev.driver)
        controller.reset()

    def slot_status(self):
        with self._descriptor.open_device(TRANSPORT.OTP) as dev:
            controller = OtpController(dev.driver)
            return list(controller.slot_status)


class PixelImage(object):

    def __init__(self, data, width, height):
        self.data = data
        self.width = width
        self.height = height

    def get_line(self, line_number):
        return self.data[
            self.width * line_number:self.width * (line_number + 1)]


controller = None


def init_with_logging(log_level, log_file=None):
    logging_setup = as_json(ykman.logging_setup.setup)
    logging_setup(log_level, log_file)

    init()


def init():
    global controller
    controller = Controller()
