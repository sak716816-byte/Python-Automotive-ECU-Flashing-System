import os
import time
import pytest
from uds_system.virtual_bus import VirtualCanBus
from uds_system.uds_server import UdsServer
from uds_system.iso_tp import IsoTpNode
from uds_system.flashing_manager import FlashingManager, FlashingException
from uds_system.utils import IntelHexParser


@pytest.fixture
def flashing_setup():
    """Sets up hex files, UDS server, and Client Tester node."""
    # Create temp hex files in scratch
    os.makedirs("scratch", exist_ok=True)
    driver_hex = "scratch/test_driver.hex"
    app_hex = "scratch/test_app.hex"
    
    IntelHexParser.write_mock_hex(driver_hex, 0x20001000, b"\x90\x1D" * 32)
    IntelHexParser.write_mock_hex(app_hex, 0x00010000, b"\x33\x78" * 64)

    bus = VirtualCanBus()
    server = UdsServer(bus, tx_id=0x7E8, rx_id=0x7E0, name="ECU")
    server.start()

    tester = IsoTpNode(bus, tx_id=0x7E0, rx_id=0x7E8, name="TESTER")

    yield server, tester, driver_hex, app_hex

    server.stop()


def test_positive_flashing_flow(flashing_setup):
    """Verify that the full 13-step flashing flow executes successfully."""
    server, tester, driver_hex, app_hex = flashing_setup
    
    manager = FlashingManager(tester, driver_hex, app_hex)
    
    # Run flashing (should complete without exception)
    manager.run_flashing()
    
    # Assert ECU is back to default session
    assert server.current_session == 0x01
    assert server.security_unlocked is False


def test_flashing_fails_without_unlock(flashing_setup):
    """Verify that flashing fails if Security Access fails or is skipped."""
    server, tester, driver_hex, app_hex = flashing_setup
    
    # Subclass manager to bypass security step and inject security failure
    class InsecureFlashingManager(FlashingManager):
        def run_flashing(self, progress_callback=None):
            # Step 1: Extended Session
            self.client.send(b"\x10\x03")
            self.client.recv(timeout=0.5)

            # Step 2: Disable DTC Storage
            self.client.send(b"\x85\x02")
            self.client.recv(timeout=0.5)

            # Step 3: Disable Communication
            self.client.send(b"\x28\x03\x03")
            self.client.recv(timeout=0.5)

            # Step 4: Programming Session
            self.client.send(b"\x10\x02")
            self.client.recv(timeout=0.5)

            # Skip Step 5 (Unlock)
            
            # Step 6: Write Fingerprint -> expect security failure (NRC 0x33)
            self.client.send(b"\x2E\xF1\x5A\x17\x06\x11\xAA")
            resp = self.client.recv(timeout=0.5)
            if resp == b"\x7F\x2E\x33":
                raise FlashingException("ECU security validation failed (NRC 0x33)")

    insecure_manager = InsecureFlashingManager(tester, driver_hex, app_hex)
    with pytest.raises(FlashingException) as excinfo:
        insecure_manager.run_flashing()
    
    assert "NRC 0x33" in str(excinfo.value)
