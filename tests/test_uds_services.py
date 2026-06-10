import time
import pytest
from uds_system.virtual_bus import VirtualCanBus
from uds_system.uds_server import UdsServer
from uds_system.iso_tp import IsoTpNode


@pytest.fixture
def uds_setup():
    """Fixture to set up Virtual Bus, ECU UdsServer, and Tester Client."""
    bus = VirtualCanBus()
    server = UdsServer(bus, tx_id=0x7E8, rx_id=0x7E0, name="ECU")
    server.start()
    
    tester = IsoTpNode(bus, tx_id=0x7E0, rx_id=0x7E8, name="TESTER")
    
    yield server, tester
    
    server.stop()


def test_session_control(uds_setup):
    """Test 0x10 DiagnosticSessionControl session transitions."""
    server, tester = uds_setup
    
    # 1. Request Extended Session (10 03)
    tester.send(b"\x10\x03")
    resp = tester.recv(timeout=0.5)
    assert resp.startswith(b"\x50\x03")
    assert server.current_session == 0x03

    # 2. Request Programming Session (10 02)
    tester.send(b"\x10\x02")
    resp = tester.recv(timeout=0.5)
    assert resp.startswith(b"\x50\x02")
    assert server.current_session == 0x02


def test_read_data_by_identifier(uds_setup):
    """Test 0x22 ReadDataByIdentifier with supported and unsupported DIDs."""
    server, tester = uds_setup
    
    # Read VIN (F1 90)
    tester.send(b"\x22\xF1\x90")
    resp = tester.recv(timeout=0.5)
    assert resp.startswith(b"\x62\xF1\x90")
    assert resp[3:] == b"ANTIGRAVITYECU123"

    # Read unsupported DID (F1 99) -> NRC 0x31 (RequestOutOfRange)
    tester.send(b"\x22\xF1\x99")
    resp = tester.recv(timeout=0.5)
    assert resp == b"\x7F\x22\x31"


def test_security_access_flow(uds_setup):
    """Test 0x27 SecurityAccess positive and negative paths."""
    server, tester = uds_setup
    
    # Request seed in Default Session -> NRC 0x7F (not supported in session)
    tester.send(b"\x27\x01")
    resp = tester.recv(timeout=0.5)
    assert resp == b"\x7F\x27\x7F"

    # Switch to Extended Session
    tester.send(b"\x10\x03")
    tester.recv(timeout=0.5)

    # 1. Positive Path: Seed -> Key Validation
    tester.send(b"\x27\x01")
    resp = tester.recv(timeout=0.5)
    assert resp.startswith(b"\x67\x01")
    seed = resp[2:6]

    # Calculate key
    seed_val = int.from_bytes(seed, byteorder="big")
    key_val = ((seed_val ^ 0x95A3B12D) + 0x1F2E3D4C) & 0xFFFFFFFF
    key = key_val.to_bytes(4, byteorder="big")

    tester.send(b"\x27\x02" + key)
    resp = tester.recv(timeout=0.5)
    assert resp == b"\x67\x02"
    assert server.security_unlocked is True


def test_security_access_brute_force_lock(uds_setup):
    """Test that security locks out after 3 failed attempts."""
    server, tester = uds_setup
    
    # Switch to Extended Session
    tester.send(b"\x10\x03")
    tester.recv(timeout=0.5)

    # Request Seed
    tester.send(b"\x27\x01")
    resp = tester.recv(timeout=0.5)
    seed = resp[2:6]

    # Send 3 wrong keys
    wrong_key = b"\x00\x00\x00\x00"
    for i in range(2):
        # Request seed again (since server resets active seed after each key submission)
        tester.send(b"\x27\x01")
        tester.recv(timeout=0.5)
        
        tester.send(b"\x27\x02" + wrong_key)
        resp = tester.recv(timeout=0.5)
        assert resp == b"\x7F\x27\x35"  # NRC 0x35 (Invalid Key)

    # 3rd attempt: triggers cooldown lockout
    tester.send(b"\x27\x01")
    tester.recv(timeout=0.5)
    tester.send(b"\x27\x02" + wrong_key)
    resp = tester.recv(timeout=0.5)
    assert resp == b"\x7F\x27\x36"  # NRC 0x36 (Exceeded Attempts)

    # Subsequent request seed should be blocked by delay timer -> NRC 0x37
    tester.send(b"\x27\x01")
    resp = tester.recv(timeout=0.5)
    assert resp == b"\x7F\x27\x37"  # NRC 0x37 (Time Delay Not Expired)


def test_dtc_management(uds_setup):
    """Test DTC triggering, querying (19 02), and clearing (14)."""
    server, tester = uds_setup

    # Query DTCs initially (Status mask: 0x08 = Confirmed)
    tester.send(b"\x19\x02\x08")
    resp = tester.recv(timeout=0.5)
    assert resp == b"\x59\x02\xFF"  # empty list

    # Trigger DTC 0x9A0115 (Low battery) on server
    server.trigger_fault(0x9A0115)

    # Query DTCs again
    tester.send(b"\x19\x02\x08")
    resp = tester.recv(timeout=0.5)
    # response format: 59 02 FF [DTC bytes] [status byte]
    assert resp.startswith(b"\x59\x02\xFF")
    assert b"\x9A\x01\x15" in resp
    
    # Clear DTCs
    tester.send(b"\x14\xFF\xFF\xFF")
    resp = tester.recv(timeout=0.5)
    assert resp == b"\x54"

    # Query DTCs again to verify cleared
    tester.send(b"\x19\x02\x08")
    resp = tester.recv(timeout=0.5)
    assert resp == b"\x59\x02\xFF"
