import pytest
from uds_system.virtual_bus import VirtualCanBus, CanMessage


def test_bus_routing():
    """Verify that CAN messages are correctly routed to registered listeners."""
    bus = VirtualCanBus()
    received_msgs = []

    def callback(msg: CanMessage):
        received_msgs.append(msg)

    bus.register_listener(callback)
    
    # Write message
    bus.write(0x7E0, b"\x10\x03")
    
    assert len(received_msgs) == 1
    msg = received_msgs[0]
    assert msg.can_id == 0x7E0
    assert msg.dlc == 8
    # Data should be padded with 0xAA up to 8 bytes
    assert msg.data == b"\x10\x03\xAA\xAA\xAA\xAA\xAA\xAA"

    # Unregister listener
    bus.unregister_listener(callback)
    bus.write(0x7E0, b"\x11")
    
    # Received messages count should stay 1
    assert len(received_msgs) == 1
