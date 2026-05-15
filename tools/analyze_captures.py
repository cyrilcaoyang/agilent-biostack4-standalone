"""Summarize Gen5/BioStack serial sniffer CSV exports.

The capture directory is git-ignored; this script prints derived frame summaries only.
"""

from __future__ import annotations

import argparse
import csv
import pathlib
import re
from dataclasses import dataclass


HEX_RE = re.compile(r"[0-9a-fA-F]{2}")


@dataclass(frozen=True)
class Row:
    index: int
    function: str
    direction: str
    data: bytes


def parse_hex(data: str | None) -> bytes:
    return bytes(int(part, 16) for part in HEX_RE.findall(data or ""))


def hex_bytes(data: bytes) -> str:
    return data.hex(" ")


def read_rows(path: pathlib.Path) -> list[Row]:
    with path.open(encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        return [
            Row(
                index=int(row["#"]),
                function=row["Function"],
                direction=row["Direction"],
                data=parse_hex(row.get("Data")),
            )
            for row in reader
        ]


def payload_len(header: bytes) -> int:
    if len(header) != 11:
        return 0
    return header[7] | (header[8] << 8)


def checksum_ok(header: bytes, payload: bytes = b"") -> bool:
    if len(header) != 11:
        return False
    checksum = header[9] | (header[10] << 8)
    return (sum(header[:9]) + sum(payload) + checksum) & 0xFFFF == 0


def print_capture(path: pathlib.Path) -> None:
    rows = read_rows(path)
    writes = [
        row
        for row in rows
        if row.function == "IRP_MJ_WRITE" and row.direction == "DOWN" and row.data
    ]
    read_ups = [
        row
        for row in rows
        if row.function == "IRP_MJ_READ" and row.direction == "UP" and row.data
    ]

    print(f"\n=== {path.name} ===")
    print(f"rows={len(rows)} writes={len(writes)} read_responses={len(read_ups)}")

    command_number = 1
    write_index = 0
    while write_index < len(writes):
        header = writes[write_index]
        length = payload_len(header.data)
        payload = b""
        consumed = 1

        if length and write_index + 1 < len(writes):
            candidate = writes[write_index + 1]
            if len(candidate.data) == length:
                payload = candidate.data
                consumed = 2

        next_write_index = write_index + consumed
        end_index = (
            writes[next_write_index].index
            if next_write_index < len(writes)
            else rows[-1].index + 1
        )
        responses = [
            row.data for row in read_ups if header.index < row.index < end_index
        ]

        address = header.data[1] if len(header.data) >= 2 else None
        command = header.data[2] if len(header.data) >= 3 else None
        checksum = "ok" if checksum_ok(header.data, payload) else "bad"
        print(
            f"{command_number:02d} row#{header.index:<4} "
            f"addr={address:02x} cmd={command:02x} payload_len={length:<2} "
            f"checksum={checksum} header={hex_bytes(header.data)}"
        )
        if payload:
            print(f"      payload={hex_bytes(payload)}")
        for response in responses:
            print(f"      resp={hex_bytes(response)}")

        command_number += 1
        write_index += consumed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "capture_dir",
        nargs="?",
        default="port-communication-captures",
        type=pathlib.Path,
    )
    args = parser.parse_args()

    for path in sorted(args.capture_dir.glob("*.csv")):
        print_capture(path)


if __name__ == "__main__":
    main()
