import time
import threading
from typing import Dict, List, Tuple, Optional
from .virtual_bus import VirtualCanBus
from .iso_tp import IsoTpNode

# UDS Service IDs (SIDs)
SID_DIAGNOSTIC_SESSION_CONTROL = 0x10
SID_ECU_RESET = 0x11
SID_CLEAR_DIAGNOSTIC_INFORMATION = 0x14
SID_READ_DTC_INFORMATION = 0x19
SID_READ_DATA_BY_IDENTIFIER = 0x22
SID_SECURITY_ACCESS = 0x27
SID_COMMUNICATION_CONTROL = 0x28
SID_WRITE_DATA_BY_IDENTIFIER = 0x2E
SID_ROUTINE_CONTROL = 0x31
SID_REQUEST_DOWNLOAD = 0x34
SID_TRANSFER_DATA = 0x36
SID_REQUEST_TRANSFER_EXIT = 0x37
SID_TESTER_PRESENT = 0x3E
SID_CONTROL_DTC_SETTING = 0x85

# Negative Response Codes (NRCs)
NRC_SERVICE_NOT_SUPPORTED = 0x11
NRC_SUBFUNCTION_NOT_SUPPORTED = 0x12
NRC_INVALID_MESSAGE_LENGTH_OR_FORMAT = 0x13
NRC_CONDITIONS_NOT_CORRECT = 0x22
NRC_REQUEST_SEQUENCE_ERROR = 0x24
NRC_REQUEST_OUT_OF_RANGE = 0x31
NRC_SECURITY_ACCESS_DENIED = 0x33
NRC_INVALID_KEY = 0x35
NRC_EXCEEDED_NUMBER_OF_ATTEMPTS = 0x36
NRC_REQUIRED_TIME_DELAY_NOT_EXPIRED = 0x37
NRC_GENERAL_PROGRAMMING_FAILURE = 0x72
NRC_SERVICE_NOT_SUPPORTED_IN_ACTIVE_SESSION = 0x7F


class UdsServer:
    """Simulates a physical ECU application layer supporting UDS (ISO 14229)."""

    def __init__(self, bus: VirtualCanBus, tx_id: int = 0x7E8, rx_id: int = 0x7E0, name: str = "ECU"):
        self.bus = bus
        self.tx_id = tx_id
        self.rx_id = rx_id
        self.name = name

        # ISO-TP layer
        self.iso_tp = IsoTpNode(
            bus=bus,
            tx_id=tx_id,
            rx_id=rx_id,
            name=name,
            bs=8,
            stmin_ms=10,
            verbose=False
        )

        # UDS State Machine
        self.current_session = 0x01  # 0x01: Default, 0x02: Programming, 0x03: Extended
        self.security_unlocked = False
        self.security_attempts = 0
        self.cooldown_end_time = 0.0
        self.last_request_time = time.time()
        self.session_active = True
        
        # Communication states
        self.rx_tx_communication_enabled = True
        self.dtc_storage_enabled = True

        # DIDs database
        self.dids: Dict[int, bytes] = {
            0xF190: b"ANTIGRAVITYECU123",  # VIN (17 chars)
            0xF186: b"\x01",               # Active Session (updated dynamically)
            0xF191: b"HW-V1.0.0",          # Hardware Version
            0xF193: b"SW-V2.0.1",          # Software Version
            0xF15A: b"\x00\x00\x00\x00",  # Fingerprint (Written in programming session)
        }

        # DTC Database (DTC Code -> (Status Byte, Snapshot/Freeze Frame dict))
        # Status byte bits representation:
        # bit 0: testFailed
        # bit 2: confirmedDTC
        # bit 3: testFailedSinceLastClear
        self.dtcs: Dict[int, Tuple[int, Dict[str, str]]] = {
            0x9A0115: (0x24, {"Voltage": "9.5V", "Temp": "25C", "EngineRun": "120s"}),  # Low battery
            0x9A0213: (0x00, {"Sensor": "O2", "Resistance": "Open"}),                 # O2 sensor open circuit
        }

        # RAM simulation for Flash Driver (for Phase 3)
        self.ram_flash_driver = bytearray()
        self.flash_driver_valid = False
        self.flash_erased = False

        # Threading workers
        self.s3_thread: Optional[threading.Thread] = None
        self.server_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Starts the ECU server diagnostic processing threads."""
        self.session_active = True
        self.last_request_time = time.time()

        # S3 Timer Monitor (fallback to default session after 5s of inactivity)
        def s3_supervisor():
            while self.session_active:
                time.sleep(0.1)
                now = time.time()
                # S3 timeout triggers only in non-default sessions
                if self.current_session != 0x01:
                    if now - self.last_request_time > 5.0:
                        self.log("S3 Timeout: Inactivity limit reached. Resetting to Default Session.")
                        self.current_session = 0x01
                        self.security_unlocked = False
                        self.rx_tx_communication_enabled = True
                        self.dtc_storage_enabled = True
                        self.dids[0xF186] = bytes([self.current_session])

        self.s3_thread = threading.Thread(target=s3_supervisor, daemon=True)
        self.s3_thread.start()

        # Main Server processing loop
        def main_loop():
            while self.session_active:
                try:
                    request = self.iso_tp.recv(timeout=0.1)
                    self.last_request_time = time.time()
                    self._process_uds_request(request)
                except Exception:
                    # Timeout waiting for queue - normal loop behavior
                    pass

        self.server_thread = threading.Thread(target=main_loop, daemon=True)
        self.server_thread.start()
        self.log("ECU Server running. Listening for diagnostic requests...")

    def stop(self) -> None:
        """Stops all threads."""
        self.session_active = False
        if self.s3_thread:
            self.s3_thread.join()
        if self.server_thread:
            self.server_thread.join()
        self.log("ECU Server stopped.")

    def log(self, text: str) -> None:
        print(f"\033[93m[{self.name}]\033[0m {text}")

    def trigger_fault(self, dtc: int) -> None:
        """Simulates a sensor failure triggering a DTC status update."""
        if dtc in self.dtcs:
            status, freeze_frame = self.dtcs[dtc]
            # Set bit 0 (testFailed) and bit 2 (confirmedDTC)
            new_status = status | 0x2D  # Active & Confirmed
            self.dtcs[dtc] = (new_status, freeze_frame)
            self.log(f"Fault triggered: DTC 0x{dtc:06X} status updated to 0x{new_status:02X}")

    def _send_negative_response(self, sid: int, nrc: int) -> None:
        """Helper to send standard UDS negative response (7F [SID] [NRC])."""
        self.log(f"Negative Response: SID=0x{sid:02X}, NRC=0x{nrc:02X}")
        self.iso_tp.send(bytes([0x7F, sid, nrc]))

    def _process_uds_request(self, request: bytes) -> None:
        """Decodes and dispatches UDS request frames."""
        if not request:
            return

        sid = request[0]

        # 1. Check if the session supports this service
        # In a real ECU, some services are restricted in certain sessions
        if sid in [SID_REQUEST_DOWNLOAD, SID_TRANSFER_DATA, SID_REQUEST_TRANSFER_EXIT] and self.current_session != 0x02:
            self._send_negative_response(sid, NRC_SERVICE_NOT_SUPPORTED_IN_ACTIVE_SESSION)
            return

        if sid == SID_DIAGNOSTIC_SESSION_CONTROL:
            self._handle_session_control(request)
        elif sid == SID_ECU_RESET:
            self._handle_ecu_reset(request)
        elif sid == SID_SECURITY_ACCESS:
            self._handle_security_access(request)
        elif sid == SID_READ_DATA_BY_IDENTIFIER:
            self._handle_read_did(request)
        elif sid == SID_WRITE_DATA_BY_IDENTIFIER:
            self._handle_write_did(request)
        elif sid == SID_TESTER_PRESENT:
            self._handle_tester_present(request)
        elif sid == SID_READ_DTC_INFORMATION:
            self._handle_read_dtc(request)
        elif sid == SID_CLEAR_DIAGNOSTIC_INFORMATION:
            self._handle_clear_dtc(request)
        elif sid == SID_COMMUNICATION_CONTROL:
            self._handle_communication_control(request)
        elif sid == SID_CONTROL_DTC_SETTING:
            self._handle_control_dtc_setting(request)
        elif sid == SID_ROUTINE_CONTROL:
            self._handle_routine_control(request)
        elif sid == SID_REQUEST_DOWNLOAD:
            self._handle_request_download(request)
        elif sid == SID_TRANSFER_DATA:
            self._handle_transfer_data(request)
        elif sid == SID_REQUEST_TRANSFER_EXIT:
            self._handle_transfer_exit(request)
        else:
            self._send_negative_response(sid, NRC_SERVICE_NOT_SUPPORTED)

    # --- UDS Service Handlers ---

    def _handle_session_control(self, req: bytes) -> None:
        """Handles 0x10 DiagnosticSessionControl."""
        if len(req) < 2:
            self._send_negative_response(SID_DIAGNOSTIC_SESSION_CONTROL, NRC_INVALID_MESSAGE_LENGTH_OR_FORMAT)
            return

        sub_func = req[1]
        if sub_func not in [0x01, 0x02, 0x03]:
            self._send_negative_response(SID_DIAGNOSTIC_SESSION_CONTROL, NRC_SUBFUNCTION_NOT_SUPPORTED)
            return

        self.current_session = sub_func
        self.dids[0xF186] = bytes([sub_func])
        
        # Changing session locks security automatically
        self.security_unlocked = False
        
        # P2 timing response (P2 = 50ms (0x0032), P2* = 5000ms (0x01F4))
        resp = bytes([0x50, sub_func, 0x00, 0x32, 0x01, 0xF4])
        self.log(f"Session changed to: 0x{sub_func:02X}")
        self.iso_tp.send(resp)

    def _handle_ecu_reset(self, req: bytes) -> None:
        """Handles 0x11 ECUReset."""
        if len(req) < 2:
            self._send_negative_response(SID_ECU_RESET, NRC_INVALID_MESSAGE_LENGTH_OR_FORMAT)
            return

        sub_func = req[1]
        if sub_func not in [0x01, 0x03]:  # 0x01: Hard Reset, 0x03: Soft Reset
            self._send_negative_response(SID_ECU_RESET, NRC_SUBFUNCTION_NOT_SUPPORTED)
            return

        # Requires extended or programming session
        if self.current_session == 0x01:
            self._send_negative_response(SID_ECU_RESET, NRC_SERVICE_NOT_SUPPORTED_IN_ACTIVE_SESSION)
            return

        self.log(f"Performing ECU Reset type: 0x{sub_func:02X}...")
        resp = bytes([0x51, sub_func])
        self.iso_tp.send(resp)
        
        # Simulate reset by fall-back to defaults
        time.sleep(0.1)
        self.current_session = 0x01
        self.security_unlocked = False
        self.ram_flash_driver = bytearray()
        self.flash_driver_valid = False
        self.flash_erased = False
        self.rx_tx_communication_enabled = True
        self.dtc_storage_enabled = True
        self.dids[0xF186] = bytes([self.current_session])
        self.log("ECU Reset completed. Back to Default Session.")

    def _calculate_key(self, seed: bytes) -> bytes:
        """Implements the Seed-Key security algorithm.

        Algorithm: Key = ((Seed ^ 0x95A3B12D) + 0x1F2E3D4C) & 0xFFFFFFFF
        """
        seed_val = int.from_bytes(seed, byteorder="big")
        key_val = ((seed_val ^ 0x95A3B12D) + 0x1F2E3D4C) & 0xFFFFFFFF
        return key_val.to_bytes(4, byteorder="big")

    def _handle_security_access(self, req: bytes) -> None:
        """Handles 0x27 SecurityAccess."""
        if len(req) < 2:
            self._send_negative_response(SID_SECURITY_ACCESS, NRC_INVALID_MESSAGE_LENGTH_OR_FORMAT)
            return

        sub_func = req[1]
        now = time.time()

        # Cooldown check for locked state
        if now < self.cooldown_end_time:
            self._send_negative_response(SID_SECURITY_ACCESS, NRC_REQUIRED_TIME_DELAY_NOT_EXPIRED)
            return

        if sub_func == 0x01:  # Request Seed
            # Check session
            if self.current_session == 0x01:
                self._send_negative_response(SID_SECURITY_ACCESS, NRC_SERVICE_NOT_SUPPORTED_IN_ACTIVE_SESSION)
                return

            if self.security_unlocked:
                # Already unlocked: return seed 00 00 00 00
                resp = bytes([0x67, 0x01, 0x00, 0x00, 0x00, 0x00])
                self.log("Security already unlocked. Seed bytes are 0.")
                self.iso_tp.send(resp)
            else:
                # Generate unique seed (e.g. mock seed using monotonic ticks)
                seed_val = int(time.monotonic() * 1000) & 0xFFFFFFFF
                self.active_seed = seed_val.to_bytes(4, byteorder="big")
                resp = bytes([0x67, 0x01]) + self.active_seed
                self.log(f"Generated Seed: {self.active_seed.hex().upper()}")
                self.iso_tp.send(resp)

        elif sub_func == 0x02:  # Send Key
            if len(req) != 6:
                self._send_negative_response(SID_SECURITY_ACCESS, NRC_INVALID_MESSAGE_LENGTH_OR_FORMAT)
                return

            if not hasattr(self, 'active_seed') or not self.active_seed:
                self._send_negative_response(SID_SECURITY_ACCESS, NRC_REQUEST_SEQUENCE_ERROR)
                return

            submitted_key = req[2:6]
            expected_key = self._calculate_key(self.active_seed)

            # Reset active seed so it cannot be reused
            self.active_seed = None

            if submitted_key == expected_key:
                self.security_unlocked = True
                self.security_attempts = 0
                resp = bytes([0x67, 0x02])
                self.log("Security Access Granted: Level 1 Unlocked!")
                self.iso_tp.send(resp)
            else:
                self.security_attempts += 1
                self.log(f"Security Access Denied: Invalid Key. Attempt {self.security_attempts}/3.")
                if self.security_attempts >= 3:
                    self.log("Exceeded maximum security attempts. Cooldown of 10s triggered.")
                    self.cooldown_end_time = now + 10.0  # 10 seconds cooldown
                    self.security_attempts = 0
                    self._send_negative_response(SID_SECURITY_ACCESS, NRC_EXCEEDED_NUMBER_OF_ATTEMPTS)
                else:
                    self._send_negative_response(SID_SECURITY_ACCESS, NRC_INVALID_KEY)

        else:
            self._send_negative_response(SID_SECURITY_ACCESS, NRC_SUBFUNCTION_NOT_SUPPORTED)

    def _handle_read_did(self, req: bytes) -> None:
        """Handles 0x22 ReadDataByIdentifier."""
        if len(req) != 3:
            self._send_negative_response(SID_READ_DATA_BY_IDENTIFIER, NRC_INVALID_MESSAGE_LENGTH_OR_FORMAT)
            return

        did = (req[1] << 8) | req[2]
        if did in self.dids:
            data = self.dids[did]
            resp = bytes([0x62, req[1], req[2]]) + data
            self.log(f"Reading DID: 0x{did:04X} -> {data.hex().upper()}")
            self.iso_tp.send(resp)
        else:
            self._send_negative_response(SID_READ_DATA_BY_IDENTIFIER, NRC_REQUEST_OUT_OF_RANGE)

    def _handle_write_did(self, req: bytes) -> None:
        """Handles 0x2E WriteDataByIdentifier."""
        if len(req) < 4:
            self._send_negative_response(SID_WRITE_DATA_BY_IDENTIFIER, NRC_INVALID_MESSAGE_LENGTH_OR_FORMAT)
            return

        did = (req[1] << 8) | req[2]
        payload = req[3:]

        # Check conditions
        if self.current_session != 0x02:  # Must be Programming session to write fingerprint
            self._send_negative_response(SID_WRITE_DATA_BY_IDENTIFIER, NRC_SERVICE_NOT_SUPPORTED_IN_ACTIVE_SESSION)
            return

        if not self.security_unlocked:
            self._send_negative_response(SID_WRITE_DATA_BY_IDENTIFIER, NRC_SECURITY_ACCESS_DENIED)
            return

        if did in self.dids:
            self.dids[did] = payload
            resp = bytes([0x6E, req[1], req[2]])
            self.log(f"Writing DID: 0x{did:04X} with payload: {payload.hex().upper()}")
            self.iso_tp.send(resp)
        else:
            self._send_negative_response(SID_WRITE_DATA_BY_IDENTIFIER, NRC_REQUEST_OUT_OF_RANGE)

    def _handle_tester_present(self, req: bytes) -> None:
        """Handles 0x3E TesterPresent."""
        if len(req) < 2:
            self._send_negative_response(SID_TESTER_PRESENT, NRC_INVALID_MESSAGE_LENGTH_OR_FORMAT)
            return

        sub_func = req[1]
        if sub_func not in [0x00, 0x80]:  # 0x00: Respond, 0x80: Suppress Response
            self._send_negative_response(SID_TESTER_PRESENT, NRC_SUBFUNCTION_NOT_SUPPORTED)
            return

        # Keep alive - update timer
        self.last_request_time = time.time()

        if sub_func == 0x00:
            resp = bytes([0x7E, 0x00])
            self.iso_tp.send(resp)

    def _handle_read_dtc(self, req: bytes) -> None:
        """Handles 0x19 ReadDTCInformation."""
        if len(req) < 2:
            self._send_negative_response(SID_READ_DTC_INFORMATION, NRC_INVALID_MESSAGE_LENGTH_OR_FORMAT)
            return

        sub_func = req[1]
        
        if sub_func == 0x02:  # Report DTC by Status Mask
            if len(req) < 3:
                self._send_negative_response(SID_READ_DTC_INFORMATION, NRC_INVALID_MESSAGE_LENGTH_OR_FORMAT)
                return
            
            mask = req[2]
            # Availability mask: 0xFF indicates all bits are supported
            dtc_list = []
            for dtc, (status, _) in self.dtcs.items():
                if status & mask:
                    dtc_bytes = dtc.to_bytes(3, byteorder="big")
                    dtc_list.extend(dtc_bytes + bytes([status]))
            
            resp = bytes([0x59, 0x02, 0xFF]) + bytes(dtc_list)
            self.log(f"Reading DTCs by mask 0x{mask:02X} -> found {len(dtc_list)//4} DTCs.")
            self.iso_tp.send(resp)
        else:
            self._send_negative_response(SID_READ_DTC_INFORMATION, NRC_SUBFUNCTION_NOT_SUPPORTED)

    def _handle_clear_dtc(self, req: bytes) -> None:
        """Handles 0x14 ClearDiagnosticInformation."""
        if len(req) != 4:
            self._send_negative_response(SID_CLEAR_DIAGNOSTIC_INFORMATION, NRC_INVALID_MESSAGE_LENGTH_OR_FORMAT)
            return

        group = (req[1] << 16) | (req[2] << 8) | req[3]
        if group == 0xFFFFFF:  # Clear all DTCs
            for dtc in self.dtcs:
                # Reset status byte to 0x00 (testNotFailed, not active, not confirmed)
                self.dtcs[dtc] = (0x00, self.dtcs[dtc][1])
            resp = bytes([0x54])
            self.log("All Diagnostic Trouble Codes (DTCs) cleared successfully.")
            self.iso_tp.send(resp)
        else:
            self._send_negative_response(SID_CLEAR_DIAGNOSTIC_INFORMATION, NRC_REQUEST_OUT_OF_RANGE)

    def _handle_communication_control(self, req: bytes) -> None:
        """Handles 0x28 CommunicationControl."""
        if len(req) < 3:
            self._send_negative_response(SID_COMMUNICATION_CONTROL, NRC_INVALID_MESSAGE_LENGTH_OR_FORMAT)
            return

        control_type = req[1]
        comm_type = req[2]

        if self.current_session == 0x01:
            self._send_negative_response(SID_COMMUNICATION_CONTROL, NRC_SERVICE_NOT_SUPPORTED_IN_ACTIVE_SESSION)
            return

        if control_type == 0x03:  # Disable Rx and Tx
            self.rx_tx_communication_enabled = False
            self.log("Non-diagnostic CAN communication disabled.")
            resp = bytes([0x68, control_type])
            self.iso_tp.send(resp)
        else:
            self._send_negative_response(SID_COMMUNICATION_CONTROL, NRC_SUBFUNCTION_NOT_SUPPORTED)

    def _handle_control_dtc_setting(self, req: bytes) -> None:
        """Handles 0x85 ControlDTCSetting."""
        if len(req) < 2:
            self._send_negative_response(SID_CONTROL_DTC_SETTING, NRC_INVALID_MESSAGE_LENGTH_OR_FORMAT)
            return

        sub_func = req[1]
        
        if self.current_session == 0x01:
            self._send_negative_response(SID_CONTROL_DTC_SETTING, NRC_SERVICE_NOT_SUPPORTED_IN_ACTIVE_SESSION)
            return

        if sub_func == 0x02:  # OFF (Disable DTC updates)
            self.dtc_storage_enabled = False
            self.log("DTC storage disabled.")
            resp = bytes([0xC5, sub_func])  # 0x85 + 0x40 = 0xC5
            self.iso_tp.send(resp)
        else:
            self._send_negative_response(SID_CONTROL_DTC_SETTING, NRC_SUBFUNCTION_NOT_SUPPORTED)

    # --- Routine Control & Flashing Services (For Phase 3 Preview) ---

    def _handle_routine_control(self, req: bytes) -> None:
        """Handles 0x31 RoutineControl."""
        if len(req) < 4:
            self._send_negative_response(SID_ROUTINE_CONTROL, NRC_INVALID_MESSAGE_LENGTH_OR_FORMAT)
            return

        action = req[1]
        routine_id = (req[2] << 8) | req[3]

        if not self.security_unlocked:
            self._send_negative_response(SID_ROUTINE_CONTROL, NRC_SECURITY_ACCESS_DENIED)
            return

        if action == 0x01:  # StartRoutine
            if routine_id == 0x0202:  # Verify Checksum/Flash Driver
                # Sub-argument validation
                if routine_id == 0x0202 and len(req) >= 6:
                    expected_signature = req[4:6]
                    if expected_signature == b"\x88\x1D":  # ZCANPRO Flash Driver Check signature
                        self.flash_driver_valid = True
                        self.log("Routine 0202: Flash Driver RAM verification successful.")
                        self.iso_tp.send(bytes([0x71, 0x01, 0x02, 0x02, 0x00]))
                    elif expected_signature == b"\x33\x78":  # ZCANPRO APP Check signature
                        self.log("Routine 0202: App sector CRC checksum verification successful.")
                        self.iso_tp.send(bytes([0x71, 0x01, 0x02, 0x02, 0x00]))
                    else:
                        self.log(f"Routine 0202: Verification failed. Invalid signature {expected_signature.hex()}")
                        self._send_negative_response(SID_ROUTINE_CONTROL, NRC_GENERAL_PROGRAMMING_FAILURE)
                else:
                    self._send_negative_response(SID_ROUTINE_CONTROL, NRC_INVALID_MESSAGE_LENGTH_OR_FORMAT)

            elif routine_id == 0xFF00:  # Erase Memory
                if self.flash_driver_valid:
                    self.flash_erased = True
                    self.log("Routine FF00: APP sector erased successfully.")
                    self.iso_tp.send(bytes([0x71, 0x01, 0xFF, 0x00]))
                else:
                    self.log("Routine FF00 failed: Flash Driver must be loaded and verified first!")
                    self._send_negative_response(SID_ROUTINE_CONTROL, NRC_CONDITIONS_NOT_CORRECT)

            elif routine_id == 0xFF01:  # Check Programming Dependency
                if self.flash_erased:
                    self.log("Routine FF01: App programming dependencies verified successfully.")
                    self.iso_tp.send(bytes([0x71, 0x01, 0xFF, 0x01]))
                else:
                    self._send_negative_response(SID_ROUTINE_CONTROL, NRC_CONDITIONS_NOT_CORRECT)
            else:
                self._send_negative_response(SID_ROUTINE_CONTROL, NRC_REQUEST_OUT_OF_RANGE)
        else:
            self._send_negative_response(SID_ROUTINE_CONTROL, NRC_SUBFUNCTION_NOT_SUPPORTED)

    def _handle_request_download(self, req: bytes) -> None:
        """Handles 0x34 RequestDownload."""
        # Request format: 34 [dfi] [alf] [address] [length]
        if len(req) < 7:
            self._send_negative_response(SID_REQUEST_DOWNLOAD, NRC_INVALID_MESSAGE_LENGTH_OR_FORMAT)
            return

        if not self.security_unlocked:
            self._send_negative_response(SID_REQUEST_DOWNLOAD, NRC_SECURITY_ACCESS_DENIED)
            return

        dfi = req[1]  # Data Format Identifier
        alf = req[2]  # Address And Length Format Identifier
        
        # Parse sizes
        addr_size = alf & 0x0F
        len_size = (alf >> 4) & 0x0F

        if len(req) != 3 + addr_size + len_size:
            self._send_negative_response(SID_REQUEST_DOWNLOAD, NRC_INVALID_MESSAGE_LENGTH_OR_FORMAT)
            return

        addr_offset = 3
        len_offset = 3 + addr_size
        
        self.transfer_address = int.from_bytes(req[addr_offset:len_offset], byteorder="big")
        self.transfer_length = int.from_bytes(req[len_offset:len_offset+len_size], byteorder="big")

        self.log(f"Download Request Accepted. Address: 0x{self.transfer_address:08X}, Size: {self.transfer_length} bytes.")
        
        # Reset transfer buffers
        self.transfer_received = bytearray()
        self.transfer_seq = 1

        # Positive response: 74 + maxNumberOfBlockLength (typically 0x00FF bytes allowed per 0x36 frame)
        self.max_block_length = 255
        resp = bytes([0x74, 0x20, 0x00, 0xFF])  # length of block format = 2 bytes, length = 0x00FF
        self.iso_tp.send(resp)

    def _handle_transfer_data(self, req: bytes) -> None:
        """Handles 0x36 TransferData."""
        # Request format: 36 [sequenceNumber] [data...]
        if len(req) < 2:
            self._send_negative_response(SID_TRANSFER_DATA, NRC_INVALID_MESSAGE_LENGTH_OR_FORMAT)
            return

        seq = req[1]
        data_block = req[2:]

        if seq != self.transfer_seq:
            self.log(f"Transfer error: Expected sequence {self.transfer_seq}, got {seq}.")
            self._send_negative_response(SID_TRANSFER_DATA, NRC_REQUEST_SEQUENCE_ERROR)
            return

        self.transfer_received.extend(data_block)
        
        # Echo sequence number in positive response
        resp = bytes([0x76, seq])
        self.iso_tp.send(resp)
        
        self.transfer_seq = (self.transfer_seq + 1) % 256

    def _handle_transfer_exit(self, req: bytes) -> None:
        """Handles 0x37 RequestTransferExit."""
        if len(self.transfer_received) != self.transfer_length:
            self.log(f"Transfer Exit error: Expected {self.transfer_length} bytes, received {len(self.transfer_received)}.")
            self._send_negative_response(SID_REQUEST_TRANSFER_EXIT, NRC_CONDITIONS_NOT_CORRECT)
            return

        self.log(f"Data transfer successfully exited. Assembled block size: {len(self.transfer_received)} bytes.")
        resp = bytes([0x77])
        self.iso_tp.send(resp)
