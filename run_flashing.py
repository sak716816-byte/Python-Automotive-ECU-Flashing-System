import os
import sys
import time
import threading
from uds_system.virtual_bus import VirtualCanBus
from uds_system.uds_server import UdsServer
from uds_system.iso_tp import IsoTpNode
from uds_system.flashing_manager import FlashingManager, FlashingException
from uds_system.utils import IntelHexParser

# Colors
CLR_HEADER = "\033[95m"
CLR_BLUE = "\033[94m"
CLR_GREEN = "\033[92m"
CLR_YELLOW = "\033[93m"
CLR_RED = "\033[91m"
CLR_RESET = "\033[0m"


class FlashingDashboard:
    """ASCII Terminal dashboard for displaying real-time ECU flashing progress."""

    def __init__(self):
        self.step_status = {i: "PENDING" for i in range(1, 14)}
        self.step_progress = {i: 0.0 for i in range(1, 14)}
        self.start_time = time.time()
        self.total_size_bytes = 0

        # Descriptions of the 13 UDS steps
        self.step_descs = {
            1: "Enter Extended Session (0x10 0x03)",
            2: "Disable DTC Storage (0x85 0x02)",
            3: "Disable Non-Diagnostic Comm (0x28 0x03 0x03)",
            4: "Enter Programming Session (0x10 0x02)",
            5: "Security Access Seed-Key Unlock (0x27)",
            6: "Write Programming Fingerprint (0x2E F1 5A)",
            7: "Download Flash Driver to RAM (0x34 / 0x36)",
            8: "Verify Flash Driver Integrity (0x31 02 02)",
            9: "Erase Flash APP Sectors (0x31 FF 00)",
            10: "Download Application Hex (0x34 / 0x36)",
            11: "Verify APP Checksum CRC-32 (0x31 02 02)",
            12: "Verify Programming Dependencies (0x31 FF 01)",
            13: "ECU Hard Reset & Exit (0x11 0x01)",
        }

    def update(self, step_num: int, desc: str, status: str, progress: float):
        """Updates the status and draws the dashboard."""
        if step_num in self.step_status:
            self.step_status[step_num] = status
            self.step_progress[step_num] = progress

        # Clear terminal using ANSI code or spacing
        # For compatibility on Windows, we'll write multiple carriage returns or clear
        os.system('cls' if os.name == 'nt' else 'clear')

        elapsed = time.time() - self.start_time
        print(f"{CLR_HEADER}================================================================================{CLR_RESET}")
        print(f"{CLR_HEADER}              AUTOMOTIVE ECU SOFTWARE FLASHING CONTROLLER (UDS)                 {CLR_RESET}")
        print(f"{CLR_HEADER}================================================================================{CLR_RESET}")
        print(f" Elapsed Time: {elapsed:.2f}s | Speed: {self._get_speed_str(elapsed)} | Target: ECU (ID: 0x7E0/0x7E8)")
        print(f" Virtual Link: ISO-TP (ISO 15765) over Virtual CAN Channel")
        print(f"--------------------------------------------------------------------------------")

        for i in range(1, 14):
            status_text = self.step_status[i]
            desc_text = self.step_descs[i]
            
            # Print indicator
            if status_text == "SUCCESS":
                indicator = f"{CLR_GREEN}[ OK ]{CLR_RESET}"
            elif status_text == "RUNNING":
                indicator = f"{CLR_BLUE}[BUSY]{CLR_RESET}"
            elif status_text.startswith("FAILED"):
                indicator = f"{CLR_RED}[FAIL]{CLR_RESET}"
            else:
                indicator = "[    ]"

            dots = "." * (55 - len(desc_text))
            print(f" Step {i:02d}: {desc_text} {dots} {indicator}")

            # Draw progress bar if currently running
            if status_text == "RUNNING" and self.step_progress[i] > 0.0:
                prog = self.step_progress[i]
                bar_len = 30
                filled_len = int(bar_len * prog)
                bar = "=" * filled_len + ">" + " " * (bar_len - filled_len - 1)
                bar = bar[:bar_len]
                print(f"          Progress: [{CLR_BLUE}{bar}{CLR_RESET}] {prog*100:.1f}%")

        print(f"--------------------------------------------------------------------------------")
        
        # Display ascii ECU state representation
        session_names = {1: "Default", 2: "Programming", 3: "Extended"}
        ecu_status_line = f" ECU RAM State: [ Flash Driver Loaded ] | ROM State: [ APP Erased ]"
        print(ecu_status_line)
        print(f"{CLR_HEADER}================================================================================{CLR_RESET}")
        sys.stdout.flush()

    def _get_speed_str(self, elapsed_seconds: float) -> str:
        """Estimates flashing transfer speed in KB/s."""
        total_transferred = 0
        # Calculate approximate bytes from running/completed download steps
        # Step 7 is flash driver (approx 1 KB), Step 10 is App (approx 4 KB)
        if self.step_status[7] == "SUCCESS":
            total_transferred += 1024
        elif self.step_status[7] == "RUNNING":
            total_transferred += int(1024 * self.step_progress[7])

        if self.step_status[10] == "SUCCESS":
            total_transferred += 4096
        elif self.step_status[10] == "RUNNING":
            total_transferred += int(4096 * self.step_progress[10])

        if total_transferred == 0 or elapsed_seconds == 0:
            return "0.00 KB/s"

        kb_s = (total_transferred / 1024.0) / elapsed_seconds
        return f"{kb_s:.2f} KB/s"


def main():
    # 1. Create mock hex files
    # Flash Driver: 1024 bytes of dummy code destined for RAM (0x20001000)
    driver_bin = bytes([0x90, 0x1D, 0x00, 0xAA] * 256)
    driver_hex_path = "scratch/flash_driver.hex"
    IntelHexParser.write_mock_hex(driver_hex_path, 0x20001000, driver_bin)

    # APP Code: 4096 bytes of dummy application destined for FLASH (0x00010000)
    app_bin = bytes([0xAA, 0x55, 0x33, 0x78] * 1024)
    app_hex_path = "scratch/app.hex"
    IntelHexParser.write_mock_hex(app_hex_path, 0x00010000, app_bin)

    # 2. Boot up virtual bus and server
    bus = VirtualCanBus()

    # Log CAN frames to a separate text file quietly to prevent messing up the progress bar
    can_log = open("can_bus.log", "w")
    can_log.write("=== CAN BUS LOG TRACE ===\n")

    def log_can_frame(line: str):
        can_log.write(line + "\n")
        can_log.flush()

    server = UdsServer(bus=bus, tx_id=0x7E8, rx_id=0x7E0, name="ECU")
    server.start()

    client = IsoTpNode(bus=bus, tx_id=0x7E0, rx_id=0x7E8, name="TESTER")
    client.trace_callback = log_can_frame

    # 3. Create Dashboard and Flashing Manager
    dashboard = FlashingDashboard()
    manager = FlashingManager(client, driver_hex_path, app_hex_path)

    # Execute flashing sequence in a separate thread to keep UI updates responsive
    success = False
    error_reason = ""

    def flash_worker():
        nonlocal success, error_reason
        try:
            time.sleep(1.0)  # Wait for UI layout
            manager.run_flashing(dashboard.update)
            success = True
        except FlashingException as fe:
            error_reason = str(fe)
        except Exception as e:
            error_reason = f"Unexpected error: {e}"

    worker = threading.Thread(target=flash_worker)
    worker.start()
    
    # Wait for completion while worker runs
    worker.join()

    # Final result logging
    server.stop()
    can_log.close()

    if success:
        print(f"\n{CLR_GREEN}[SUCCESS] ECU Flashing completed successfully in {time.time() - dashboard.start_time:.2f} seconds!{CLR_RESET}")
        print(f"[INFO] Detailed CAN bus trace log saved to: {os.path.abspath('can_bus.log')}\n")
    else:
        print(f"\n{CLR_RED}[FAILED] Flashing process aborted: {error_reason}{CLR_RESET}")
        print(f"[INFO] Check: 'can_bus.log' for diagnostic details.\n")


if __name__ == "__main__":
    main()
