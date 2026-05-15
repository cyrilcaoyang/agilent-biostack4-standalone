# BioStack 4 Serial Protocol Notes

Initial notes from Gen5 COM8 sniffer exports in `port-communication-captures/`.

Raw captures are intentionally git-ignored. These notes record only derived protocol observations.

## Serial Settings

Gen5 configures COM8 as:

- Baud: `9600` (`80 25 00 00`, little-endian `0x2580`)
- Line control: `02 00 08`
  - Windows `SERIAL_LINE_CONTROL`: stop bits `2`, parity `0`, word length `8`
  - Interpreted as `9600 8N2`
- DTR: set
- RTS: cleared
- XON/XOFF characters: `11 13`

This is not an ASCII command protocol.

## Frame Shape

Gen5 writes binary command frames, not text such as `STATUS\r`.

Request header:

```text
01 <address> <command> 0d 01 01 00 <payload_len_le16> <checksum_le16>
```

If `payload_len` is non-zero, Gen5 immediately writes that many payload bytes as a second write operation.

Checksum hypothesis:

```text
sum(header_without_checksum + payload + checksum_le16) & 0xffff == 0
```

The checksum is little-endian two's-complement over the first 9 header bytes plus the payload.

Response behavior:

- Device ACK byte is `06`.
- Fast commands may return `06` prepended to the response frame in one read.
- Motion commands often return `06` first, then a delayed response frame after the movement completes.

Response header:

```text
01 00 <command> 0d 02 00 00 <payload_len_le16> <checksum_le16>
```

Observed response payload length is `4`.

Observed success payload:

```text
00 80 00 00
```

Observed terminal/error payload when repeating "move all" after plates are exhausted:

```text
01 80 00 16
```

Observed failed pickup payload from `4_verify_step2_failed_pickup.csv`:

```text
01 80 01 17
```

Treat non-success status meanings as provisional until confirmed against more captures.

## Observed Commands

| Command | Address | Payload | Observed action |
| --- | --- | --- | --- |
| `bd` | `01` | none | Common status/query before and after actions |
| `be` | `01` | 18 bytes | Common setup/config command; payload changes between runs |
| `eb` | `01` | 12 bytes | Common setup/config command |
| `f1` | `01` | 9 bytes | Common setup/config command |
| `b0` | `01` | none | Observed during save-position flow |
| `ad` | `02` | 20 bytes | Alignment command |
| `ae` | `02` | 22 bytes | Z move-down command; requested distance appears at payload bytes 2-3 as little-endian |
| `b9` | `02` | none | Verify/drop-related command |
| `c1` | `02` | 18 bytes | Alignment/save-position command |
| `c0` | `02` | none | Home all axes |
| `cd` | `02` | 26 bytes | Verify pickup/store-related command; payload begins with ASCII `PASSWORD` |
| `e2` | `02` | none | Move one plate from input stack to output stack |
| `bc` | `02` | none | Move one plate from output stack to input stack |

The "move all" captures repeat the one-plate command until the device reports a non-success payload.

## Calibration Workflow Notes

The alignment captures map to this Gen5 workflow:

0. Test COM8 connection: `bd`.
1. Home gripper and plate holder: setup/status commands followed by `c0`.
2. Align Z axis to the drop/pickup location: `c1`, `ad`, and repeated `ae` move-down commands.
3. Save Z location: `b0` plus `c1`.
4. Verify drop/pickup: `b9` then `cd`.

For `ae`, the second little-endian 16-bit value in the payload matches the requested move-down amount:

| Capture | Payload bytes | Value |
| --- | --- | --- |
| `2_movedown_1.csv` | `01 00` | `1` |
| `2_movedown_5.csv` | `05 00` | `5` |
| `2_movedown_20.csv` | `14 00` | `20` |
| `2_movedown_100.csv` | `64 00` | `100` |
| `2_movedown_400.csv` | `90 01` | `400` |

`4_verify_step1.csv` succeeded with status `00 80 00 00`. `4_verify_step2_failed_pickup.csv` returned `01 80 01 17` when no plate was picked up.

## Standalone Control Assessment

The captures support standalone COM8 control without Gen5 or Cytation. Gen5 appears to be a serial client that opens COM8, applies standard serial settings, writes binary frames, and waits for ACK/status frames from the BioStack.

This is enough evidence to build a standalone driver for known operations:

- connection/status check
- home all axes
- stack-to-stack single-plate moves
- repeated "move all" behavior
- Z alignment move-down by numeric step
- save/verify calibration flows, with caution around the `PASSWORD` payload in `cd`

The remaining unknowns are the complete status/error code table, exact meaning of setup/config payloads, and which calibration commands are safe to expose as normal operations.

## Mechanical Workflow Notes

For `move_plate_from_input_to_output_1`, the plate carrier moves to the input stack, takes the bottom plate, moves to the output stack, and inserts the plate into the bottom slot.

For `move_plate_from_output_to_input_1`, the same bottom-plate transfer happens in the opposite direction: output stack to input stack.

So `e2` and `bc` should be treated as stack-to-stack transfer commands, not as commands that present a plate to an external reader nest or arbitrary handoff position.

## Safety Notes

Do not send active commands to the BioStack unless someone is physically present, the moving parts are clear, and a plate jam can be handled immediately.

The first implementation should be a decoder/replayer in dry-run mode. Active command sending should require an explicit unsafe flag and should start with status-only commands.
