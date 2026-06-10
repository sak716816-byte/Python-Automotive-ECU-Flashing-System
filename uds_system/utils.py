import os
from typing import List, Tuple


class IntelHexParser:
    """A standard Intel Hex file parser for automotive flashing systems.

    Translates .hex text records (including 16-bit offset and 32-bit linear address records)
    into contiguous chunks of binary memory blocks (address, data).
    """

    @staticmethod
    def parse(filepath: str) -> List[Tuple[int, bytes]]:
        """Parses an Intel Hex file and returns a list of contiguous (start_address, data) tuples."""
        blocks: List[Tuple[int, bytes]] = []
        
        current_block_addr = None
        current_block_data = bytearray()
        
        upper_linear_address = 0
        
        if not os.path.exists(filepath):
            raise FileNotFoundError(f"Hex file not found: {filepath}")

        with open(filepath, "r") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                if not line.startswith(":"):
                    raise ValueError(f"Line {line_num}: Record must start with ':'")

                # Parse fields
                try:
                    byte_count = int(line[1:3], 16)
                    address_offset = int(line[3:7], 16)
                    record_type = int(line[7:9], 16)
                    
                    data_hex = line[9:9 + byte_count * 2]
                    data_bytes = bytes.fromhex(data_hex)
                    
                    checksum = int(line[9 + byte_count * 2: 9 + byte_count * 2 + 2], 16)
                except ValueError:
                    raise ValueError(f"Line {line_num}: Malformed hex record format.")

                # Verify checksum: sum of all bytes + checksum byte should equal 00 (mod 256)
                record_bytes = bytes.fromhex(line[1:-2])
                calc_sum = sum(record_bytes) & 0xFF
                expected_checksum = (256 - calc_sum) & 0xFF
                if checksum != expected_checksum:
                    raise ValueError(f"Line {line_num}: Checksum mismatch. Got 0x{checksum:02X}, expected 0x{expected_checksum:02X}")

                if record_type == 0x00:  # Data Record
                    phys_addr = upper_linear_address + address_offset
                    
                    if current_block_addr is None:
                        current_block_addr = phys_addr
                        current_block_data = bytearray(data_bytes)
                    else:
                        # Check if contiguous with the current block
                        expected_next_addr = current_block_addr + len(current_block_data)
                        if phys_addr == expected_next_addr:
                            current_block_data.extend(data_bytes)
                        else:
                            # Not contiguous: flush old block, start new one
                            blocks.append((current_block_addr, bytes(current_block_data)))
                            current_block_addr = phys_addr
                            current_block_data = bytearray(data_bytes)

                elif record_type == 0x01:  # End of File Record
                    if current_block_addr is not None:
                        blocks.append((current_block_addr, bytes(current_block_data)))
                        current_block_addr = None
                    break

                elif record_type == 0x02:  # Extended Segment Address Record
                    # Multiplies value by 16 (shift left 4) for paragraph boundary
                    segment = int(data_hex, 16)
                    upper_linear_address = segment << 4

                elif record_type == 0x04:  # Extended Linear Address Record
                    # Multiplies value by 65536 (shift left 16)
                    upper_linear_address = int(data_hex, 16) << 16

                # Types 03 and 05 (start address records) are typically ignored for physical memory layouts
                
        return blocks

    @staticmethod
    def write_mock_hex(filepath: str, start_addr: int, payload: bytes) -> None:
        """Helper to output binary data as a standard formatted Intel Hex file."""
        os.makedirs(os.path.dirname(filepath) if os.path.dirname(filepath) else ".", exist_ok=True)
        
        with open(filepath, "w") as f:
            # 1. Write Extended Linear Address if start_addr is > 65535
            upper_addr = (start_addr >> 16) & 0xFFFF
            if upper_addr > 0:
                addr_hex = f"{upper_addr:04X}"
                # :02000004[upper_addr][checksum]
                checksum = (0x02 + 0x00 + 0x00 + 0x04 + ((upper_addr >> 8) & 0xFF) + (upper_addr & 0xFF)) & 0xFF
                checksum = (256 - checksum) & 0xFF
                f.write(f":02000004{addr_hex}{checksum:02X}\n")

            # 2. Write Data Records (16 bytes per line)
            offset = 0
            while offset < len(payload):
                chunk = payload[offset:offset+16]
                line_offset = (start_addr + offset) & 0xFFFF
                
                # Check if offset overflowed 64KB boundary during block write
                if offset > 0 and line_offset == 0:
                    # Write new Extended Linear Address
                    new_upper = ((start_addr + offset) >> 16) & 0xFFFF
                    addr_hex = f"{new_upper:04X}"
                    checksum = (0x02 + 0x00 + 0x00 + 0x04 + ((new_upper >> 8) & 0xFF) + (new_upper & 0xFF)) & 0xFF
                    checksum = (256 - checksum) & 0xFF
                    f.write(f":02000004{addr_hex}{checksum:02X}\n")

                byte_count = len(chunk)
                data_hex = chunk.hex().upper()
                
                # Calculate checksum
                sum_val = byte_count + ((line_offset >> 8) & 0xFF) + (line_offset & 0xFF) + 0x00 + sum(chunk)
                checksum = (256 - (sum_val & 0xFF)) & 0xFF
                
                f.write(f":{byte_count:02X}{line_offset:04X}00{data_hex}{checksum:02X}\n")
                offset += byte_count

            # 3. Write End of File Record
            f.write(":00000001FF\n")
