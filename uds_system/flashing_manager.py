import time
from typing import Callable, Optional
from .iso_tp import IsoTpNode
from .utils import IntelHexParser

# Routine check signatures
SIGNATURE_VERIFY_DRIVER = b"\x88\x1D"
SIGNATURE_VERIFY_APP = b"\x33\x78"


class FlashingException(Exception):
    """Exception raised when any step in the ECU flashing sequence fails."""
    pass


class FlashingManager:
    """Manages the full 13-step automated ECU flashing sequence.

    Uses an IsoTpNode as the diagnostic client to communicate with the ECU.
    """

    def __init__(self, client: IsoTpNode, driver_hex_path: str, app_hex_path: str):
        self.client = client
        self.driver_hex_path = driver_hex_path
        self.app_hex_path = app_hex_path

    def run_flashing(self, progress_callback: Optional[Callable[[int, str, str, float], None]] = None) -> None:
        """Executes the 13 steps of the UDS flashing sequence.

        Args:
            progress_callback: A callback of format: fn(step_num, step_description, status, progress_fraction)

        Raises:
            FlashingException: If any step fails or receives a negative response.
        """
        def update_progress(step: int, desc: str, status: str, val: float = 0.0):
            if progress_callback:
                progress_callback(step, desc, status, val)

        # Helper to send request and assert positive response
        def send_and_expect(step: int, desc: str, request: bytes, expected_prefix: bytes) -> bytes:
            update_progress(step, desc, "RUNNING")
            try:
                self.client.send(request)
                resp = self.client.recv(timeout=2.0)
            except Exception as e:
                update_progress(step, desc, f"FAILED: {e}")
                raise FlashingException(f"Step {step} failed: {e}")

            if not resp.startswith(expected_prefix):
                # Check if it is a negative response (7F [SID] [NRC])
                if len(resp) >= 3 and resp[0] == 0x7F:
                    nrc = resp[2]
                    err_msg = f"ECU returned Negative Response Code (NRC) 0x{nrc:02X}"
                else:
                    err_msg = f"ECU returned unexpected response: {resp.hex().upper()}"
                update_progress(step, desc, f"FAILED: {err_msg}")
                raise FlashingException(f"Step {step} failed: {err_msg}")

            update_progress(step, desc, "SUCCESS", 1.0)
            return resp

        # Step 1: Extended Session
        send_and_expect(1, "Enter Extended Session", b"\x10\x03", b"\x50\x03")
        time.sleep(0.1)

        # Step 2: Disable DTC Storage
        send_and_expect(2, "Disable DTC Storage (ControlDTCSetting)", b"\x85\x02", b"\xC5\x02")
        time.sleep(0.1)

        # Step 3: Disable non-diagnostic communication
        send_and_expect(3, "Disable Non-Diagnostic Comm (CommunicationControl)", b"\x28\x03\x03", b"\x68\x03")
        time.sleep(0.1)

        # Step 4: Programming Session
        send_and_expect(4, "Enter Programming Session", b"\x10\x02", b"\x50\x02")
        time.sleep(0.1)

        # Step 5: Security Unlock
        update_progress(5, "Security Access Unlock", "RUNNING")
        try:
            # 5a. Request Seed
            self.client.send(b"\x27\x01")
            resp = self.client.recv(timeout=1.0)
            if not resp.startswith(b"\x67\x01"):
                raise FlashingException(f"Seed request denied: {resp.hex().upper()}")
            
            seed = resp[2:6]
            
            # Calculate key: Key = ((Seed ^ 0x95A3B12D) + 0x1F2E3D4C) & 0xFFFFFFFF
            seed_val = int.from_bytes(seed, byteorder="big")
            key_val = ((seed_val ^ 0x95A3B12D) + 0x1F2E3D4C) & 0xFFFFFFFF
            key = key_val.to_bytes(4, byteorder="big")

            # 5b. Send Key
            self.client.send(b"\x27\x02" + key)
            resp = self.client.recv(timeout=1.0)
            if not resp.startswith(b"\x67\x02"):
                raise FlashingException(f"Key verification denied: {resp.hex().upper()}")
                
            update_progress(5, "Security Access Unlock", "SUCCESS", 1.0)
        except Exception as e:
            update_progress(5, "Security Access Unlock", f"FAILED: {e}")
            raise FlashingException(f"Step 5 failed: {e}")
        time.sleep(0.1)

        # Step 6: Write Fingerprint DID 0xF15A
        # Write b"\x17\x06\x11\xAA" representing Date/Tester ID
        send_and_expect(6, "Write Programming Fingerprint", b"\x2E\xF1\x5A\x17\x06\x11\xAA", b"\x6E\xF1\x5A")
        time.sleep(0.1)

        # Helper to execute download transfer loops
        def download_blocks(step: int, desc: str, blocks: list) -> None:
            update_progress(step, desc, "RUNNING", 0.0)
            
            total_bytes = sum(len(data) for _, data in blocks)
            bytes_transferred = 0

            for block_index, (address, data) in enumerate(blocks, 1):
                # 1. Request Download (0x34)
                addr_bytes = address.to_bytes(4, byteorder="big")
                len_bytes = len(data).to_bytes(4, byteorder="big")
                
                # 34 + dfi(00) + alf(44) + address + length
                req_download = b"\x34\x00\x44" + addr_bytes + len_bytes
                
                try:
                    self.client.send(req_download)
                    resp = self.client.recv(timeout=2.0)
                except Exception as e:
                    update_progress(step, desc, f"FAILED on 0x34: {e}")
                    raise FlashingException(f"Download request failed: {e}")

                if not resp.startswith(b"\x74"):
                    raise FlashingException(f"RequestDownload negative response: {resp.hex().upper()}")

                # 2. Transfer Data (0x36) in chunks (e.g. 240 bytes)
                chunk_size = 240
                offset = 0
                seq = 1

                while offset < len(data):
                    chunk = data[offset:offset + chunk_size]
                    req_transfer = bytes([0x36, seq]) + chunk
                    
                    try:
                        self.client.send(req_transfer)
                        resp = self.client.recv(timeout=2.0)
                    except Exception as e:
                        update_progress(step, desc, f"FAILED on 0x36 (seq {seq}): {e}")
                        raise FlashingException(f"TransferData failed: {e}")

                    if not resp.startswith(bytes([0x76, seq])):
                        raise FlashingException(f"TransferData unexpected response: {resp.hex().upper()}")

                    seq = (seq + 1) % 256
                    offset += len(chunk)
                    bytes_transferred += len(chunk)
                    
                    progress_val = min(1.0, bytes_transferred / total_bytes)
                    update_progress(step, desc, "RUNNING", progress_val)
                    time.sleep(0.01)  # Small sleep to simulate bus speed and render UI smoothly

                # 3. Request Transfer Exit (0x37)
                try:
                    self.client.send(b"\x37")
                    resp = self.client.recv(timeout=2.0)
                except Exception as e:
                    update_progress(step, desc, f"FAILED on 0x37: {e}")
                    raise FlashingException(f"RequestTransferExit failed: {e}")

                if resp != b"\x77":
                    raise FlashingException(f"Transfer exit negative response: {resp.hex().upper()}")

            update_progress(step, desc, "SUCCESS", 1.0)

        # Step 7: Download Flash Driver to RAM (Address 0x20001000)
        driver_blocks = IntelHexParser.parse(self.driver_hex_path)
        download_blocks(7, "Download Flash Driver into RAM", driver_blocks)
        time.sleep(0.1)

        # Step 8: Verify Flash Driver Signature
        send_and_expect(8, "Verify Flash Driver Integrity", b"\x31\x01\x02\x02" + SIGNATURE_VERIFY_DRIVER, b"\x71\x01\x02\x02")
        time.sleep(0.1)

        # Step 9: Erase Memory APP Sector (Routine FF00)
        send_and_expect(9, "Erase Flash APP Sectors", b"\x31\x01\xFF\x00", b"\x71\x01\xFF\x00")
        time.sleep(0.1)

        # Step 10: Download Application Code to Flash (Address 0x00010000)
        app_blocks = IntelHexParser.parse(self.app_hex_path)
        download_blocks(10, "Download Application Hex (APP)", app_blocks)
        time.sleep(0.1)

        # Step 11: Verify APP Checksum (CRC-32 Routine 0202)
        send_and_expect(11, "Verify APP Checksum (CRC32)", b"\x31\x01\x02\x02" + SIGNATURE_VERIFY_APP, b"\x71\x01\x02\x02")
        time.sleep(0.1)

        # Step 12: Check Programming Dependency
        send_and_expect(12, "Verify Programming Dependencies", b"\x31\x01\xFF\x01", b"\x71\x01\xFF\x01")
        time.sleep(0.1)

        # Step 13: ECU Hard Reset
        send_and_expect(13, "ECU Hard Reset (Reset to Default Session)", b"\x11\x01", b"\x51\x01")
        time.sleep(0.1)

        update_progress(14, "ECU Flashing Completed", "SUCCESS", 1.0)
