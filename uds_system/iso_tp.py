import time
import queue
import threading
from typing import Optional, Tuple
from .virtual_bus import VirtualCanBus, CanMessage


class IsoTpTimeoutError(TimeoutError):
    """Exception raised when an ISO-TP timeout occurs (e.g. N_Bs or N_Cr)."""
    pass


class IsoTpProtocolError(RuntimeError):
    """Exception raised when an ISO-TP protocol violation occurs (e.g. sequence number mismatch)."""
    pass


class IsoTpNode:
    """Simulates an ISO-TP (ISO 15765-2) protocol node.

    This node wraps the VirtualCanBus link layer to provide the application layer
    with reliable, segmented data transfer for payloads up to 4095 bytes.
    """

    def __init__(
        self,
        bus: VirtualCanBus,
        tx_id: int,
        rx_id: int,
        name: str = "Node",
        bs: int = 0,
        stmin_ms: int = 10,
        n_bs: float = 1.0,
        n_cr: float = 1.0,
        padding_byte: int = 0xAA,
        verbose: bool = False
    ):
        """Initializes the ISO-TP node.

        Args:
            bus: The VirtualCanBus instance.
            tx_id: The CAN ID used to transmit frames.
            rx_id: The CAN ID used to receive frames.
            name: Readable name of the node for logging.
            bs: Block Size requested when receiving multi-frame messages (0 = infinite).
            stmin_ms: Separation Time (ms) requested when receiving (0-127ms).
            n_bs: Timeout (seconds) waiting for Flow Control frame.
            n_cr: Timeout (seconds) waiting for Consecutive Frame.
            padding_byte: Value to pad unused bytes in CAN frames.
            verbose: If True, prints internal state changes to console.
        """
        self.bus = bus
        self.tx_id = tx_id
        self.rx_id = rx_id
        self.name = name
        self.bs = bs
        self.stmin_ms = stmin_ms
        self.n_bs = n_bs
        self.n_cr = n_cr
        self.padding_byte = padding_byte
        self.verbose = verbose

        # Thread-safe queue for fully assembled application-layer payloads
        self.rx_queue: queue.Queue[bytes] = queue.Queue()

        # Thread synchronization for Flow Control frames
        self.fc_received_event = threading.Event()
        self.fc_status: Tuple[int, int, int] = (0, 0, 0)  # (fs, bs, stmin)

        # Receiving state machine variables
        self.rx_buffer = bytearray()
        self.rx_total_len = 0
        self.rx_expected_sn = 1
        self.rx_cf_count = 0
        self.rx_lock = threading.Lock()
        self.rx_timer: Optional[float] = None

        # Diagnostic listener trace hook (set by visualizer)
        self.trace_callback: Optional[Callable[[str], None]] = None

        # Register bus listener
        self.bus.register_listener(self._on_can_message)

    def log(self, text: str) -> None:
        """Internal verbose logger."""
        if self.verbose:
            print(f"[{self.name}] {text}")

    def _trace(self, direction: str, msg: CanMessage, info: str) -> None:
        """Helper to invoke trace visualization callback."""
        if self.trace_callback:
            hex_str = " ".join(f"{b:02X}" for b in msg.data)
            self.trace_callback(f"{direction:<3} ID: 0x{msg.can_id:03X} DLC: {msg.dlc} DATA: {hex_str} | {info}")

    def _on_can_message(self, msg: CanMessage) -> None:
        """Callback triggered by the virtual CAN bus for every frame."""
        # Only process frames with the matching rx_id
        if msg.can_id != self.rx_id:
            return

        pci_type = (msg.data[0] >> 4) & 0x0F

        if pci_type == 0:  # Single Frame (SF)
            sf_dl = msg.data[0] & 0x0F
            if sf_dl > 7:
                self.log(f"Error: SF data length {sf_dl} > 7. Ignoring.")
                return
            payload = msg.data[1:1 + sf_dl]
            self._trace("Rx", msg, f"SF (Single Frame), Len={sf_dl}")
            self.rx_queue.put(payload)

        elif pci_type == 1:  # First Frame (FF)
            ff_dl = ((msg.data[0] & 0x0F) << 8) | msg.data[1]
            if ff_dl < 8:
                self.log(f"Error: FF data length {ff_dl} < 8. Ignoring.")
                return
            self._trace("Rx", msg, f"FF (First Frame), Total Len={ff_dl}")

            with self.rx_lock:
                self.rx_buffer = bytearray(msg.data[2:8])
                self.rx_total_len = ff_dl
                self.rx_expected_sn = 1
                self.rx_cf_count = 0
                self.rx_timer = time.time()

            # Send Flow Control frame (CTS)
            self._send_flow_control()

        elif pci_type == 2:  # Consecutive Frame (CF)
            sn = msg.data[0] & 0x0F
            self._trace("Rx", msg, f"CF (Consecutive Frame), SN={sn}")

            with self.rx_lock:
                if self.rx_total_len == 0:
                    self.log("Ignored CF: No active multi-frame assembly.")
                    return

                # Check timeout N_Cr
                now = time.time()
                if self.rx_timer and (now - self.rx_timer > self.n_cr):
                    self.log("N_Cr Timeout occurred while waiting for CF. Discarding block.")
                    self._reset_rx_state()
                    return

                if sn != self.rx_expected_sn:
                    self.log(f"Protocol Error: Expected SN {self.rx_expected_sn}, got {sn}.")
                    self._reset_rx_state()
                    return

                # Append data
                remaining = self.rx_total_len - len(self.rx_buffer)
                chunk_len = min(7, remaining)
                self.rx_buffer.extend(msg.data[1:1 + chunk_len])
                self.rx_expected_sn = (self.rx_expected_sn + 1) % 16
                self.rx_timer = time.time()
                self.rx_cf_count += 1

                # Check if assembly finished
                if len(self.rx_buffer) >= self.rx_total_len:
                    full_payload = bytes(self.rx_buffer)
                    self.log(f"Successfully assembled message, total size: {len(full_payload)} bytes.")
                    self.rx_queue.put(full_payload)
                    self._reset_rx_state()
                else:
                    # Check if we reached block size BS and need to send another FC
                    if self.bs > 0 and self.rx_cf_count >= self.bs:
                        self.rx_cf_count = 0
                        self._send_flow_control()

        elif pci_type == 3:  # Flow Control Frame (FC)
            fs = msg.data[0] & 0x0F
            bs = msg.data[1]
            stmin = msg.data[2]
            fs_str = {0: "CTS (Continue to Send)", 1: "WT (Wait)", 2: "OVFLW (Overflow)"}.get(fs, "Unknown")
            self._trace("Rx", msg, f"FC (Flow Control), FS={fs} ({fs_str}), BS={bs}, STmin={stmin}ms")

            self.fc_status = (fs, bs, stmin)
            self.fc_received_event.set()

    def _reset_rx_state(self) -> None:
        """Resets the receiving assembler state variables."""
        self.rx_buffer = bytearray()
        self.rx_total_len = 0
        self.rx_expected_sn = 1
        self.rx_cf_count = 0
        self.rx_timer = None

    def _send_flow_control(self) -> None:
        """Transmits a Flow Control frame to the sender."""
        fc_byte0 = 0x30  # Flow Status = 0 (CTS - Continue to Send)
        fc_frame = bytes([fc_byte0, self.bs, self.stmin_ms])
        
        # Build CanMessage object manually for the trace log
        padded_fc = bytearray(fc_frame)
        padded_fc.extend([self.padding_byte] * (8 - len(padded_fc)))
        temp_msg = CanMessage(time.time(), self.tx_id, bytes(padded_fc), 8)
        self._trace("Tx", temp_msg, f"FC (Flow Control) sent: BS={self.bs}, STmin={self.stmin_ms}ms")

        self.bus.write(self.tx_id, fc_frame)

    def send(self, data: bytes) -> None:
        """Sends data payload over ISO-TP, blocking until complete.

        Args:
            data: The bytes payload to transmit.

        Raises:
            IsoTpTimeoutError: If N_Bs or N_Cs timeouts occur.
            IsoTpProtocolError: If server responds with flow errors like overflow.
        """
        if not data:
            return

        total_len = len(data)

        if total_len <= 7:
            # Single Frame transmission
            sf_byte0 = total_len & 0x0F
            frame = bytes([sf_byte0]) + data
            
            padded_frame = bytearray(frame)
            padded_frame.extend([self.padding_byte] * (8 - len(padded_frame)))
            temp_msg = CanMessage(time.time(), self.tx_id, bytes(padded_frame), 8)
            self._trace("Tx", temp_msg, f"SF (Single Frame) sent, payload size={total_len}")

            self.bus.write(self.tx_id, frame)
            return

        # Multi-Frame transmission
        # 1. Send First Frame (FF)
        ff_byte0 = 0x10 | ((total_len >> 8) & 0x0F)
        ff_byte1 = total_len & 0xFF
        ff_frame = bytes([ff_byte0, ff_byte1]) + data[:6]

        self.fc_received_event.clear()
        
        padded_ff = bytearray(ff_frame)
        padded_ff.extend([self.padding_byte] * (8 - len(padded_ff)))
        temp_msg = CanMessage(time.time(), self.tx_id, bytes(padded_ff), 8)
        self._trace("Tx", temp_msg, f"FF (First Frame) sent, total payload size={total_len}")

        self.bus.write(self.tx_id, ff_frame)

        # 2. Wait for Flow Control (FC) frame (N_Bs timeout)
        self.log(f"Waiting {self.n_bs}s for Flow Control frame...")
        if not self.fc_received_event.wait(self.n_bs):
            raise IsoTpTimeoutError("N_Bs Timeout: Did not receive Flow Control frame from target.")

        fs, bs_param, stmin_param = self.fc_status
        
        # Handle Wait status (WT) loop
        while fs == 1:  # WT
            self.log("Target requested Wait. Clearing event and waiting again...")
            self.fc_received_event.clear()
            if not self.fc_received_event.wait(self.n_bs):
                raise IsoTpTimeoutError("N_Bs Timeout: Flow Control wait timed out.")
            fs, bs_param, stmin_param = self.fc_status

        if fs == 2:  # Overflow (OVFLW)
            raise IsoTpProtocolError("Buffer Overflow: Target indicates it cannot receive a payload of this size.")

        if fs != 0:  # Non-zero unexpected FS
            raise IsoTpProtocolError(f"Protocol Error: Received invalid Flow Status: {fs}")

        # 3. Transmit Consecutive Frames (CF)
        self.log(f"Flow Control CTS approved. BS={bs_param}, STmin={stmin_param}ms. Commencing Consecutive Frames.")
        offset = 6
        sn = 1
        cf_sent_count = 0

        while offset < total_len:
            # Separation Time (STmin) delay
            if stmin_param > 0:
                if stmin_param <= 127:
                    time.sleep(stmin_param / 1000.0)
                elif 0xF1 <= stmin_param <= 0xF9:
                    # 100us - 900us
                    us = (stmin_param - 0xF0) * 100
                    time.sleep(us / 1000000.0)

            # Assemble Consecutive Frame
            cf_byte0 = 0x20 | sn
            chunk = data[offset:offset + 7]
            cf_frame = bytes([cf_byte0]) + chunk

            padded_cf = bytearray(cf_frame)
            padded_cf.extend([self.padding_byte] * (8 - len(padded_cf)))
            temp_msg = CanMessage(time.time(), self.tx_id, bytes(padded_cf), 8)
            
            # Check if this frame will trigger Flow Control from the receiver
            # It triggers when cf_sent_count + 1 reaches the receiver's block size (bs_param)
            is_block_limit_frame = (bs_param > 0 and cf_sent_count == bs_param - 1)
            
            if is_block_limit_frame:
                self.fc_received_event.clear()

            self._trace("Tx", temp_msg, f"CF (Consecutive Frame) sent, SN={sn}, offset={offset}/{total_len}")
            self.bus.write(self.tx_id, cf_frame)

            sn = (sn + 1) % 16
            offset += len(chunk)
            cf_sent_count += 1

            # If we sent the last frame of the block and there is still data remaining, wait for Flow Control
            if is_block_limit_frame and offset < total_len:
                self.log("Block size limit reached. Waiting for next Flow Control...")
                if not self.fc_received_event.wait(self.n_bs):
                    raise IsoTpTimeoutError("N_Bs Timeout: Timeout waiting for flow control between blocks.")
                fs, bs_param, stmin_param = self.fc_status
                if fs != 0:
                    raise IsoTpProtocolError(f"Protocol Error: Unexpected Flow Status {fs} inside block transfer.")
                cf_sent_count = 0

        self.log("Multi-frame transfer completed successfully.")

    def recv(self, timeout: Optional[float] = None) -> bytes:
        """Blocks until a complete application-layer payload is received.

        Args:
            timeout: Maximum seconds to wait (None for blocking forever).

        Returns:
            The fully assembled bytes payload.

        Raises:
            queue.Empty: If timeout expires before a message is assembled.
        """
        return self.rx_queue.get(block=True, timeout=timeout)
