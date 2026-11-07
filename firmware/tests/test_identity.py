"""The card's identity (docs/FIRMWARE.md 6.1): the flash id reader against a behavioural
configuration flash (GD25Q32 / W25Q32JV: command, dummy bytes, SPI mode 0, id assembly), and the fold to IP and MAC with its
clamps and the uid_valid flag."""
import os

import pytest
from migen import *
from migen.sim import run_simulation

from trafficlight.output.hub75 import protocol as P

from tlcard import constants as C
from tlcard.identity import CardAddress, FlashUidReader, ip_int, mac_for_ip
from simhelpers import FlashModel, fold_address

UID = bytes.fromhex("c3a5f00f12345678")


@pytest.mark.parametrize("sys_hz", [50_000_000, 25_000_000])
def test_reader_reads_the_id_once_in_mode_0(sys_hz):
    dut = FlashUidReader(sys_hz)
    flash = FlashModel(dut, UID)
    seen = {}

    def script():
        cycles = 0
        while not (yield dut.done):
            cycles += 1
            yield
        seen["done_at"] = cycles
        seen["uid"] = yield dut.uid
        for _ in range(2000):             # nothing more happens after the read
            assert (yield dut.done) and (yield dut.cs_n) and not (yield dut.sck)
            yield
    run_simulation(dut, [script(), flash.run()], vcd_name=os.environ.get("TLCARD_VCD"))

    assert flash.violations == []
    assert len(flash.sessions) == 1
    got = flash.received_bytes()
    assert len(flash.sessions[0]) == 8 * 13                    # command, 4 dummy bytes, 8 id bytes
    assert got[0] == C.FLASH_CMD_UID and got[1:5] == bytes(4)
    assert seen["uid"] == int.from_bytes(UID, "big")
    # SCK at most 12.5 MHz: every half period at least 40 ns.
    assert min(flash.halves) * 1e9 / sys_hz >= 40
    # A few microseconds after start, done well within a millisecond.
    assert 2e-6 <= flash.first_select / sys_hz <= 50e-6
    assert seen["done_at"] / sys_hz < 100e-6


def run_address(uids, fixed=None):
    """Load each uid in turn; returns (ip, mac, fold, uid_valid, ready) after each load."""
    dut = CardAddress(fixed=fixed)
    out = []

    def script():
        assert not (yield dut.ready)
        for u in uids:
            yield dut.uid.eq(int.from_bytes(u, "big"))
            yield dut.load.eq(1)
            yield
            yield dut.load.eq(0)
            yield
            yield
            out.append(((yield dut.ip), (yield dut.mac), (yield dut.fold), (yield dut.uid_valid), (yield dut.ready)))
    run_simulation(dut, script())
    return out


@pytest.mark.parametrize("uid, c, d, valid", [
    (UID, None, None, True),
    (bytes.fromhex("0000000000000000"), 1, 1, False),        # no answer, SO low: 0 -> 1
    (bytes.fromhex("ffffffffffffffff"), 1, 1, False),        # no answer, SO high: the fold is 0
    (bytes.fromhex("00000000000000ff"), 1, 254, True),       # c 0 -> 1, d 255 -> 254
    (bytes.fromhex("ff00000000000000"), 254, 1, True),       # c 255 -> 254, d 0 -> 1
    (bytes.fromhex("1234123400005678"), 0x56, 0x78, True),   # equal halves cancel
    (bytes.fromhex("0102030405060708"), 1, 8, True),         # c folds to 0 -> 1, d to 8
])
def test_fold_clamps_and_valid(uid, c, d, valid):
    fold, rc, rd, rvalid = fold_address(uid)
    if c is not None:
        assert (rc, rd) == (c, d)                # the spec's rule as the reference writes it
    assert rvalid == valid
    if hasattr(P, "address_from_uid"):
        assert P.address_from_uid(uid)[0] == f"10.75.{rc}.{rd}" and P.uid_valid(uid) == valid
    (ip, mac, gfold, gvalid, ready), = run_address([uid])
    assert ready and gfold == fold and gvalid == valid
    assert ip == ip_int(f"10.75.{rc}.{rd}")
    assert mac.to_bytes(6, "big") == bytes([0x02, 0x5A, 0x75, 0x00, rc, rd])


def test_address_never_changes_after_the_first_load():
    first, second = run_address([UID, bytes.fromhex("00000000000000ff")])
    assert first == second


def test_fixed_address_keeps_fold_and_valid_from_the_id():
    ip = ip_int("10.75.9.200")
    (gip, gmac, fold, valid, ready), = run_address([UID], fixed=(ip, mac_for_ip(ip)))
    assert (gip, gmac) == (ip, 0x025A750009C8)
    assert ready and valid and fold == fold_address(UID)[0]
