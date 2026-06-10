import time
from dataclasses import dataclass
from typing import List, Callable


@dataclass
class CanMessage:
    """Represents a CAN 2.0 data frame."""
    timestamp: float
    can_id: int
    data: bytes
    dlc: int

    def __str__(self) -> str:
        hex_data = " ".join(f"{b:02X}" for b in self.data)
        return f"{self.timestamp:.6f} ID: 0x{self.can_id:03X} DLC: {self.dlc} DATA: {hex_data}"


class VirtualCanBus:
    """A simple in-memory virtual CAN bus for routing messages between nodes."""

    def __init__(self):
        self.listeners: List[Callable[[CanMessage], None]] = []

    def register_listener(self, callback: Callable[[CanMessage], None]) -> None:
        """Register a callback function to receive all messages sent on the bus."""
        if callback not in self.listeners:
            self.listeners.append(callback)

    def unregister_listener(self, callback: Callable[[CanMessage], None]) -> None:
        """Unregister a callback function."""
        if callback in self.listeners:
            self.listeners.remove(callback)

    def write(self, can_id: int, data: bytes) -> None:
        """Writes a message onto the bus.

        Pads data to 8 bytes if it is shorter, in accordance with typical CAN 2.0 rules
        where padding (e.g. 0xAA or 0x55) is used in automotive systems.
        """
        # Truncate if longer than 8 bytes, pad with 0xAA if shorter
        raw_data = bytearray(data[:8])
        if len(raw_data) < 8:
            raw_data.extend([0xAA] * (8 - len(raw_data)))

        msg = CanMessage(
            timestamp=time.time(),
            can_id=can_id,
            data=bytes(raw_data),
            dlc=8
        )

        # Dispatch to all listeners
        for listener in self.listeners:
            try:
                listener(msg)
            except Exception as e:
                # Log or handle listener exceptions gracefully to prevent bus crashes
                pass
