"""An identity probe must not power down a card held by another PC/SC client."""
import ast
from types import SimpleNamespace
from pathlib import Path
import unittest
from unittest.mock import patch

from control.app import sim

ROOT = Path(__file__).resolve().parents[1]


class SharedCard:
    def __init__(self):
        self.powered = True
        self.pin_verified = True
        self.power_downs = 0

    def createConnection(self):
        card = self
        class Connection:
            def connect(self, *, disposition=2):  # pyscard defaults to SCARD_UNPOWER_CARD.
                self.disposition = disposition

            def disconnect(self):
                if self.disposition != 0:
                    card.powered = card.pin_verified = False
                    card.power_downs += 1

            def transmit(self, apdu):
                if not card.powered:
                    raise RuntimeError('another client powered off this card')
                if apdu[1] == 0xB0:
                    return [0] * 10, 0x90, 0
                return [], 0x90, 0
        return Connection()


class SharedReaderPowerTests(unittest.TestCase):
    def test_control_probe_preserves_the_existing_holder(self):
        card = SharedCard()
        holder = card.createConnection()
        holder.connect(disposition=0)
        with patch.object(sim, 'readers', return_value=[card]):
            self.assertTrue(sim.read_iccid(0))
        self.assertEqual(card.power_downs, 0)
        self.assertTrue(card.pin_verified)
        self.assertEqual(holder.transmit([0, 0xA4, 0, 0])[1:], (0x90, 0))

    def test_engine_identity_probes_preserve_a_pin_keepers_power_and_verification(self):
        for file in ('ami_usim.py', 'swu_ike.py'):
            with self.subTest(engine=file):
                card = SharedCard()
                # Execute the actual standalone connector without importing the IKE daemon.
                source = ast.parse((ROOT / 'engine' / file).read_text())
                names = (['open_usim', 'probe_foreign_card_once'] if file == 'ami_usim.py'
                         else ['read_iccid_at_index'])
                functions = [node for node in source.body if isinstance(node, ast.FunctionDef)
                             and node.name in names]
                self.assertEqual(len(functions), len(names))
                scope = {'readers': lambda: [card], 'SCARD_LEAVE_CARD': 0,
                         'os': SimpleNamespace(environ={}),
                         '_foreign_decided': False, '_foreign_verdict': None,
                         'foreign_iccid': lambda conn: None,
                         'toBytes': lambda value: list(bytes.fromhex(value)),
                         'toHexString': lambda value: bytes(value).hex(),
                         'bcd': lambda value: value, 'swap_nibbles': lambda value: value,
                         '_xfr': lambda conn, apdu: conn.transmit(apdu)}
                exec(compile(ast.Module(body=functions, type_ignores=[]), file, 'exec'), scope)
                scope[names[-1]](0)
                self.assertEqual(card.power_downs, 0)
                self.assertTrue(card.pin_verified)
