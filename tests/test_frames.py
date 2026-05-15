"""Unit tests for the binary frame codec.

The reference bytes are lifted from real Gen5 captures so the codec is
pinned to actual observed behavior, not just to itself.
"""

from __future__ import annotations

import pytest

from agilent_biostack4 import frames
from agilent_biostack4.exceptions import BioStackProtocolError


@pytest.mark.parametrize(
    ("address", "command", "payload", "expected_hex"),
    [
        # 0_test_comm_com8.csv #38
        (0x01, 0xBD, b"", "01 01 bd 0d 01 01 00 00 00 32 ff"),
        # plate_stacker_home_all_axis.csv #38
        (0x02, 0xC0, b"", "01 02 c0 0d 01 01 00 00 00 2e ff"),
        # move_plate_from_input_to_output_1.csv #284
        (0x02, 0xE2, b"", "01 02 e2 0d 01 01 00 00 00 0c ff"),
        # move_plate_from_output_to_input_1.csv #334
        (0x02, 0xBC, b"", "01 02 bc 0d 01 01 00 00 00 32 ff"),
        # move_plate_from_input_to_output_1.csv #136 (eb with 12-byte payload)
        (
            0x01,
            0xEB,
            bytes.fromhex("00 00 00 00 fb 7f 00 00 04 00 5b 4a"),
            "01 01 eb 0d 01 01 00 0c 00 d5 fc",
        ),
        # move_plate_from_input_to_output_1.csv #186 (f1 with 9-byte payload)
        (
            0x01,
            0xF1,
            bytes.fromhex("00 49 5b 4a 71 02 00 00 08"),
            "01 01 f1 0d 01 01 00 09 00 8c fd",
        ),
    ],
)
def test_encode_request_matches_captured_headers(address, command, payload, expected_hex):
    """Encoded headers must byte-equal what Gen5 actually wrote on COM8."""
    request = frames.encode_request(address, command, payload)
    expected_header = bytes.fromhex(expected_hex)
    assert request.raw[: frames.HEADER_LENGTH] == expected_header
    assert request.raw[frames.HEADER_LENGTH :] == payload
    assert request.address == address
    assert request.command == command


def test_checksum_protocol_invariant_holds_for_captured_response():
    """A real captured response header must verify."""
    # move_plate_from_input_to_output_1.csv #41 response header.
    header = bytes.fromhex("01 00 bd 0d 02 00 00 04 00 af fe")
    payload = bytes.fromhex("00 80 00 00")
    assert frames.verify_checksum(header, payload) is True


def test_parse_response_success_payload():
    header = bytes.fromhex("01 00 bd 0d 02 00 00 04 00 af fe")
    payload = bytes.fromhex("00 80 00 00")
    parsed = frames.parse_response(header, payload, expected_command=0xBD)
    assert parsed.command == 0xBD
    assert parsed.success is True
    assert parsed.status_payload == frames.SUCCESS_STATUS


def test_parse_response_failure_payload():
    header = bytes.fromhex("01 00 cd 0d 02 00 00 04 00 86 fe")
    payload = bytes.fromhex("01 80 01 17")
    parsed = frames.parse_response(header, payload, expected_command=0xCD)
    assert parsed.success is False
    assert parsed.status_payload == payload


def test_parse_response_rejects_command_mismatch():
    header = bytes.fromhex("01 00 bd 0d 02 00 00 04 00 af fe")
    payload = bytes.fromhex("00 80 00 00")
    with pytest.raises(BioStackProtocolError):
        frames.parse_response(header, payload, expected_command=0xCD)


def test_parse_response_rejects_bad_checksum():
    header = bytearray.fromhex("01 00 bd 0d 02 00 00 04 00 af fe")
    header[-1] ^= 0xFF
    with pytest.raises(BioStackProtocolError):
        frames.parse_response(bytes(header), bytes.fromhex("00 80 00 00"), expected_command=0xBD)


def test_parse_response_rejects_wrong_payload_length():
    header = bytes.fromhex("01 00 bd 0d 02 00 00 04 00 af fe")
    with pytest.raises(BioStackProtocolError):
        frames.parse_response(header, b"\x00", expected_command=0xBD)


def test_encode_request_rejects_oversize_payload():
    with pytest.raises(ValueError):
        frames.encode_request(0x01, 0xBD, b"\x00" * (0xFFFF + 1))
