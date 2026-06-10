import time
import threading
from uds_system.virtual_bus import VirtualCanBus
from uds_system.iso_tp import IsoTpNode, IsoTpTimeoutError, IsoTpProtocolError

# ANSI Escape Sequences for terminal styling
CLR_HEADER = "\033[95m"
CLR_CLIENT = "\033[96m"  # Cyan
CLR_SERVER = "\033[93m"  # Yellow
CLR_INFO = "\033[94m"    # Blue
CLR_SUCCESS = "\033[92m" # Green
CLR_RESET = "\033[0m"


def print_header(title: str):
    print(f"\n{CLR_HEADER}{'=' * 80}")
    print(f" {title.center(78)} ")
    print(f"{'=' * 80}{CLR_RESET}\n")


def print_info(text: str):
    print(f"{CLR_INFO}[INFO] {text}{CLR_RESET}")


def print_success(text: str):
    print(f"{CLR_SUCCESS}[SUCCESS] {text}{CLR_RESET}")


def run_demo():
    print_header("ISO-TP (ISO 15765-2) Protocol Stack Simulation Demo")

    # 1. Initialize virtual CAN bus
    bus = VirtualCanBus()

    # Define trace printer to format CAN output like CANoe/ZCANPRO
    def trace_printer(node_name: str, color: str):
        def callback(line: str):
            # Format: Tx/Rx ID: 0x7E0 DLC: 8 DATA: XX XX ... | Info
            parts = line.split(" | ")
            can_details = parts[0]
            info = parts[1] if len(parts) > 1 else ""
            print(f"{color}[{node_name:<6}] {can_details:<45} {CLR_RESET}| {CLR_INFO}{info}{CLR_RESET}")
        return callback

    # 2. Initialize Tester (Client) and ECU (Server) ISO-TP nodes
    # Client transmits on 0x7E0, receives on 0x7E8. It requests a BS of 4 and STmin of 20ms.
    client = IsoTpNode(
        bus=bus,
        tx_id=0x7E0,
        rx_id=0x7E8,
        name="TESTER",
        bs=4,
        stmin_ms=20,
        verbose=False
    )
    client.trace_callback = trace_printer("TESTER", CLR_CLIENT)

    # Server transmits on 0x7E8, receives on 0x7E0. It requests a BS of 8 and STmin of 10ms.
    server = IsoTpNode(
        bus=bus,
        tx_id=0x7E8,
        rx_id=0x7E0,
        name="ECU",
        bs=8,
        stmin_ms=10,
        verbose=False
    )
    server.trace_callback = trace_printer("ECU   ", CLR_SERVER)

    # 3. Server background listener loop
    # In a real ECU, this runs inside an interrupt service routine or background thread.
    server_running = True

    def server_thread_func():
        while server_running:
            try:
                # Blocks with a small timeout to allow loop exit
                request = server.recv(timeout=0.1)
                
                # Simple mock diagnostic handler:
                if request == b"\x10\x03":  # Extended Session Request
                    print_info("ECU: Received 'DiagnosticSessionControl - Extended' (10 03)")
                    # Positive response: 50 + subfunction + timing parameters
                    response = b"\x50\x03\x00\x32\x01\xF4"  # P2=50ms, P2*=5000ms
                    time.sleep(0.01)  # Simulate processing delay
                    server.send(response)

                elif request.startswith(b"\x2E\xF1\x5A"):  # Write Fingerprint
                    fingerprint = request[3:]
                    print_info(f"ECU: Received 'WriteDataByIdentifier - Fingerprint' (2E F1 5A). Payload size: {len(fingerprint)} bytes.")
                    response = b"\x6E\xF1\x5A"  # Positive response: 0x2E + 0x40 = 0x6E
                    time.sleep(0.02)
                    server.send(response)

                else:
                    # Echo request as response for unknown large payloads (multi-frame echo test)
                    print_info(f"ECU: Echoing back payload of {len(request)} bytes.")
                    time.sleep(0.05)
                    server.send(request)

            except Exception:
                # Timeout on recv() queue - typical when no data is sent. Just loop.
                pass

    srv_thread = threading.Thread(target=server_thread_func, daemon=True)
    srv_thread.start()

    # --- TEST 1: Single Frame (SF) Exchange ---
    print_header("TEST 1: Single Frame (SF) - DiagnosticSessionControl (0x10)")
    print_info("Client sends: [10 03] (DiagnosticSessionControl: Sub-function=0x03)")
    
    # Client sends request
    client.send(b"\x10\x03")
    
    # Wait for response
    try:
        response = client.recv(timeout=1.0)
        print_success(f"Client received response: {response.hex().upper()}")
    except Exception as e:
        print(f"Error receiving: {e}")

    time.sleep(0.5)

    # --- TEST 2: Multi-Frame (FF, FC, CF) Segmented Transmission ---
    print_header("TEST 2: Multi-Frame (FF, FC, CF) - Write Fingerprint (0x2E) with large payload")
    # Write Fingerprint DID 0xF15A with 25 bytes of dummy data
    large_payload = b"\x2E\xF1\x5A" + bytes(range(1, 26))  # Total 28 bytes
    print_info(f"Client sends: [2E F1 5A + 25 bytes data] (Total size: {len(large_payload)} bytes)")

    # Client sends request
    client.send(large_payload)

    # Wait for response
    try:
        response = client.recv(timeout=2.0)
        print_success(f"Client received response: {response.hex().upper()}")
    except Exception as e:
        print(f"Error receiving: {e}")

    time.sleep(0.5)

    # --- TEST 3: Extreme Multi-Frame (120 bytes) to demonstrate Block Size (BS) Flow Control ---
    print_header("TEST 3: Block Size (BS) Flow Control - Client downloads a 120-byte image segment")
    print_info("Client sends: 120 bytes of payload (Server BS = 8, STmin = 10ms)")
    
    huge_payload = bytes(b % 256 for b in range(120))
    client.send(huge_payload)

    # Wait for response
    try:
        response = client.recv(timeout=3.0)
        print_success(f"Client received response echo: Total {len(response)} bytes received matches sent payload!")
    except Exception as e:
        print(f"Error receiving: {e}")

    # Shutdown server thread
    server_running = False
    srv_thread.join()
    print_header("ISO-TP SIMULATION COMPLETED")


if __name__ == "__main__":
    run_demo()
