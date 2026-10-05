"""Protocol regressions for 10427 and the earlier DUPLO train generation."""
import unittest
from lego_protocol import decode_packet, light_packet, motor_packet, sound_packet, subscribe, power_off_packet, mode_info_packet


class LegoProtocolTests(unittest.TestCase):
    def test_10427_outputs(self):
        self.assertEqual(motor_packet(0x21, 35), bytes.fromhex('08 00 81 32 11 51 00 23'))
        self.assertEqual(motor_packet(0x21, -35), bytes.fromhex('08 00 81 32 11 51 00 dd'))
        self.assertEqual(light_packet(0x21, 'blue'), bytes.fromhex('0a 00 81 34 11 51 01 04 01 0f'))
        self.assertEqual(sound_packet(0x21, 'horn'), bytes.fromhex('09 00 81 34 11 51 01 07 01'))
        self.assertEqual(sound_packet(0x21, 'starting'), bytes.fromhex('09 00 81 34 11 51 01 06 01'))
        self.assertEqual(subscribe(54, 2), bytes.fromhex('0a 00 41 36 02 01 00 00 00 01'))

    def test_older_train_uses_its_own_ports(self):
        self.assertEqual(motor_packet(0x20, -100), bytes.fromhex('08 00 81 00 11 51 00 9c'))
        self.assertEqual(light_packet(0x20, 'blue'), bytes.fromhex('08 00 81 11 11 51 00 03'))
        self.assertEqual(sound_packet(0x20, 'brake'), bytes.fromhex('08 00 81 01 11 51 01 03'))

    def test_new_color_ids_from_official_app_capture(self):
        for color, value in {'white': 1, 'green': 7, 'yellow': 8, 'purple': 10, 'red': 12, 'blue': 15}.items():
            self.assertEqual(light_packet(0x21, color)[-1], value)

    def test_power_off_and_purple_brick(self):
        self.assertEqual(power_off_packet(), bytes.fromhex('04 00 02 01'))
        self.assertEqual(decode_packet(bytes.fromhex('04 00 02 30')), ('Train is powering off', {'hub_action': 48}))
        self.assertEqual(decode_packet(bytes.fromhex('06 00 45 33 44 01'))[1], {'tag': 324, 'tag_name': 'Purple · star'})

    def test_read_only_capability_responses(self):
        changes = decode_packet(bytes.fromhex('0b 00 43 34 01 03 02 01 00 02 00'))[1]
        self.assertEqual(changes['port_modes'][1]['count'], 2)
        self.assertEqual(changes['port_modes'][1]['outputs'], 2)
        self.assertEqual(decode_packet(bytes.fromhex('0a 00 44 34 01 00 54 45 53 54'))[1], {'mode_name': (52, 1, 'TEST')})
        self.assertEqual(mode_info_packet(52, 1, 0x80), bytes.fromhex('06 00 22 34 01 80'))
        self.assertEqual(decode_packet(bytes.fromhex('0a 00 44 34 01 80 04 01 05 00'))[1],
                         {'mode_format': (52, 1, {'datasets': 4, 'type': 1, 'figures': 5, 'decimals': 0})})

    def test_rejects_invalid_and_unsupported_commands(self):
        for power in (True, 1.5, None, -101, 101):
            with self.assertRaises(ValueError):
                motor_packet(0x21, power)
        with self.assertRaises(ValueError):
            light_packet(0x21, 'orange')
        with self.assertRaises(ValueError):
            sound_packet(0x21, 'brake')

    def test_properties_and_sensors(self):
        self.assertEqual(decode_packet(bytes.fromhex('06 00 01 06 06 64'))[1], {'battery': 100})
        self.assertEqual(decode_packet(bytes.fromhex('06 00 01 05 06 ce'))[1], {'rssi': -50})
        self.assertEqual(decode_packet(bytes.fromhex('06 00 45 36 ff ff'))[1], {'measured_speed': -1})
        self.assertEqual(decode_packet(bytes.fromhex('06 00 45 33 41 01'))[1], {'tag': 321, 'tag_name': 'Blue'})
        self.assertTrue(decode_packet(bytes.fromhex('05 00 05 81 06'))[1]['protocol_error'])

    def test_unknown_malformed_and_detached_messages(self):
        self.assertEqual(decode_packet(b'\x05\x00')[1], {})
        self.assertEqual(decode_packet(bytes.fromhex('03 00 ff')), ('LEGO message 0xff', {}))
        self.assertEqual(decode_packet(bytes.fromhex('05 00 04 32 00'))[1], {'port': (50, None)})


if __name__ == '__main__':
    unittest.main()
