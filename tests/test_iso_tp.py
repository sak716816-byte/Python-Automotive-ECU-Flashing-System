import time
import queue
import threading
import pytest
from uds_system.virtual_bus import VirtualCanBus
from uds_system.iso_tp import IsoTpNode, IsoTpTimeoutError, IsoTpProtocolError


def test_single_frame_transmission():
    """Verify single frame transmission (payload <= 7 bytes)."""
    bus = VirtualCanBus()
    
    sender = IsoTpNode(bus, tx_id=0x7E0, rx_id=0x7E8, name="SENDER")
    receiver = IsoTpNode(bus, tx_id=0x7E8, rx_id=0x7E0, name="RECEIVER")
    
    payload = b"\x10\x03"
    sender.send(payload)
    
    received_payload = receiver.recv(timeout=0.5)
    assert received_payload == payload


def test_multi_frame_transmission():
    """Verify multi-frame transmission and assembly with block sizes and flow control."""
    bus = VirtualCanBus()
    
    # Receiver requests block size (BS) of 4 and STmin of 5ms
    sender = IsoTpNode(bus, tx_id=0x7E0, rx_id=0x7E8, name="SENDER")
    receiver = IsoTpNode(bus, tx_id=0x7E8, rx_id=0x7E0, name="RECEIVER", bs=4, stmin_ms=5)
    
    # 25 bytes payload (requires FF, FC, and multiple CFs)
    payload = bytes(range(1, 26))
    
    # Send in a thread because it blocks waiting for FC
    def send_func():
        sender.send(payload)
        
    t = threading.Thread(target=send_func)
    t.start()
    
    received_payload = receiver.recv(timeout=1.0)
    t.join()
    
    assert received_payload == payload


def test_n_bs_timeout():
    """Verify that sender raises an IsoTpTimeoutError if no Flow Control frame is received."""
    bus = VirtualCanBus()
    
    # No receiver registered to reply with Flow Control
    sender = IsoTpNode(bus, tx_id=0x7E0, rx_id=0x7E8, name="SENDER", n_bs=0.2)
    
    large_payload = b"\xAA" * 15
    with pytest.raises(IsoTpTimeoutError):
        sender.send(large_payload)
