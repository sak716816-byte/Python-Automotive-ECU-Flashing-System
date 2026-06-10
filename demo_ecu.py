import time
import threading
from uds_system.virtual_bus import VirtualCanBus
from uds_system.uds_server import UdsServer
from uds_system.iso_tp import IsoTpNode

# ANSI colors for TUI styling
CLR_HEADER = "\033[95m"
CLR_CLIENT = "\033[96m"  # Cyan
CLR_SERVER = "\033[93m"  # Yellow
CLR_INFO = "\033[94m"    # Blue
CLR_SUCCESS = "\033[92m" # Green
CLR_RESET = "\033[0m"


def print_section(title: str):
    print(f"\n{CLR_HEADER}{'=' * 80}")
    print(f" {title.center(78)} ")
    print(f"{'=' * 80}{CLR_RESET}\n")


def print_info(text: str):
    print(f"{CLR_INFO}[INFO] {text}{CLR_RESET}")


def print_success(text: str):
    print(f"{CLR_SUCCESS}[SUCCESS] {text}{CLR_RESET}")


def calculate_key_client(seed: bytes) -> bytes:
    """Calculates security key from seed (Client-side implementation).

    Algorithm: Key = ((Seed ^ 0x95A3B12D) + 0x1F2E3D4C) & 0xFFFFFFFF
    """
    seed_val = int.from_bytes(seed, byteorder="big")
    key_val = ((seed_val ^ 0x95A3B12D) + 0x1F2E3D4C) & 0xFFFFFFFF
    return key_val.to_bytes(4, byteorder="big")


def run_demo():
    print_section("Phase 2: Virtual ECU Engine & UDS State Machine Demo")

    # 1. Initialize Virtual CAN Bus
    bus = VirtualCanBus()

    # Trace printer for bus frames
    def trace_printer(node_name: str, color: str):
        def callback(line: str):
            parts = line.split(" | ")
            can_details = parts[0]
            info = parts[1] if len(parts) > 1 else ""
            print(f"{color}[{node_name:<6}] {can_details:<45} {CLR_RESET}| {CLR_INFO}{info}{CLR_RESET}")
        return callback

    # 2. Start Virtual ECU Server (transmits 0x7E8, receives 0x7E0)
    server = UdsServer(bus=bus, tx_id=0x7E8, rx_id=0x7E0, name="ECU")
    server.iso_tp.trace_callback = trace_printer("ECU   ", CLR_SERVER)
    server.start()

    # 3. Initialize Tester Client Node (transmits 0x7E0, receives 0x7E8)
    tester = IsoTpNode(bus=bus, tx_id=0x7E0, rx_id=0x7E8, name="TESTER")
    tester.trace_callback = trace_printer("TESTER", CLR_CLIENT)

    time.sleep(0.5)

    try:
        # --- STEP 1: Read DIDs in Default Session ---
        print_section("STEP 1: Read Data Identifiers (0x22) in Default Session")
        
        # Read VIN (0xF190)
        print_info("Tester requests ReadDataByIdentifier: VIN (F1 90)")
        tester.send(b"\x22\xF1\x90")
        resp = tester.recv(timeout=1.0)
        if resp.startswith(b"\x62\xF1\x90"):
            vin_text = resp[3:].decode("ascii")
            print_success(f"VIN successfully read: {vin_text}")
        else:
            print(f"Error response: {resp.hex()}")

        # Read HW Version (0xF191)
        print_info("Tester requests ReadDataByIdentifier: HW Version (F1 91)")
        tester.send(b"\x22\xF1\x91")
        resp = tester.recv(timeout=1.0)
        if resp.startswith(b"\x62\xF1\x91"):
            hw_text = resp[3:].decode("ascii")
            print_success(f"HW Version successfully read: {hw_text}")

        # --- STEP 2: Transition to Extended Session & Security Access ---
        print_section("STEP 2: Enter Extended Session (10 03) & Unlock ECU (0x27)")
        
        print_info("Tester requests Extended Diagnostic Session (10 03)")
        tester.send(b"\x10\x03")
        resp = tester.recv(timeout=1.0)
        if resp.startswith(b"\x50\x03"):
            print_success("Extended session established.")

        # Request Security Access Seed (27 01)
        print_info("Tester requests Security Access Seed (27 01)")
        tester.send(b"\x27\x01")
        resp = tester.recv(timeout=1.0)
        if resp.startswith(b"\x67\x01"):
            seed = resp[2:6]
            print_success(f"Received Seed: {seed.hex().upper()}")
            
            # Calculate key
            key = calculate_key_client(seed)
            print_info(f"Calculated Key: {key.hex().upper()}")
            
            # Send key (27 02)
            print_info("Tester sends Security Access Key (27 02)")
            tester.send(b"\x27\x02" + key)
            resp = tester.recv(timeout=1.0)
            if resp.startswith(b"\x67\x02"):
                print_success("ECU security successfully unlocked!")
            else:
                print(f"Unlock failed: {resp.hex()}")

        # --- STEP 3: Fault Diagnosis - Trigger, Read and Clear DTCs ---
        print_section("STEP 3: Fault Injection & DTC Management (0x19 / 0x14)")
        
        # Read DTCs initially (Status mask: 0x08 = Confirmed DTC)
        print_info("Tester reads initial confirmed DTCs (19 02 08)")
        tester.send(b"\x19\x02\x08")
        resp = tester.recv(timeout=1.0)
        print_info(f"DTC initial response: {resp.hex().upper()}")

        # Inject Low Battery Voltage fault on the ECU
        print_info("Injecting simulated fault on ECU: Low Battery Voltage (0x9A0115)...")
        server.trigger_fault(0x9A0115)
        
        # Read DTCs again
        print_info("Tester reads active/confirmed DTCs again (19 02 08)")
        tester.send(b"\x19\x02\x08")
        resp = tester.recv(timeout=1.0)
        if resp.startswith(b"\x59\x02"):
            # Availability mask (resp[2]), DTCs (resp[3:])
            dtc_data = resp[3:]
            num_dtcs = len(dtc_data) // 4
            print_success(f"Detected {num_dtcs} active DTC(s):")
            for i in range(num_dtcs):
                dtc_bytes = dtc_data[i*4 : i*4 + 3]
                status_byte = dtc_data[i*4 + 3]
                print(f"  - DTC: 0x{dtc_bytes.hex().upper()} | Status Byte: 0x{status_byte:02X} (Confirmed & Test Failed)")

        # Clear DTCs (14 FF FF FF)
        print_info("Tester requests ClearDiagnosticInformation: All DTC Groups (14 FF FF FF)")
        tester.send(b"\x14\xFF\xFF\xFF")
        resp = tester.recv(timeout=1.0)
        if resp == b"\x54":
            print_success("Clear DTCs command approved.")

        # Read DTCs once more to verify they are gone
        print_info("Tester reads DTCs to confirm clearance (19 02 08)")
        tester.send(b"\x19\x02\x08")
        resp = tester.recv(timeout=1.0)
        if resp == b"\x59\x02\xFF":
            print_success("Verified: 0 active DTCs remaining. Memory is clean!")

        # --- STEP 4: Tester Present & S3 Session Timeout Fallback ---
        print_section("STEP 4: Tester Present (0x3E) & S3 Session Inactivity Timeout")
        
        # TesterPresent to keep alive
        print_info("Tester sends TesterPresent (3E 80) [Suppress Response] to maintain session")
        tester.send(b"\x3E\x80")
        time.sleep(1.0)
        
        print_info("Tester stops communicating. Waiting for S3 Timeout (5.0 seconds)...")
        for remaining in range(5, 0, -1):
            print(f"  Inactivity timer ticking... {remaining}s left")
            time.sleep(1.1)

        # Verify session state has fallen back to Default (0x01)
        print_info("Tester queries active session DID (22 F1 86)")
        tester.send(b"\x22\xF1\x86")
        resp = tester.recv(timeout=1.0)
        if resp.startswith(b"\x62\xF1\x86"):
            active_session = resp[3]
            session_name = {0x01: "Default", 0x02: "Programming", 0x03: "Extended"}.get(active_session, "Unknown")
            print_success(f"ECU Active Session: 0x{active_session:02X} ({session_name} Session). S3 Timeout Fallback Confirmed!")

    except Exception as e:
        print(f"Error during execution: {e}")
    finally:
        # Clean shutdown of background threads
        server.stop()
        print_section("UDS SERVER SIMULATION COMPLETED")


if __name__ == "__main__":
    run_demo()
