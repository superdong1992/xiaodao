import base64
import hashlib
from pathlib import Path

source = Path("/evidence/claude-code.asc")
target = Path("/tmp/attempt52-claude-code-keyring.gpg")
begin = "-----BEGIN PGP PUBLIC KEY BLOCK-----"
end = "-----END PGP PUBLIC KEY BLOCK-----"
lines = source.read_text(encoding="ascii").splitlines()
if lines.count(begin) != 1 or lines.count(end) != 1:
    raise SystemExit("expected exactly one ASCII-armored public-key block")
start = lines.index(begin)
stop = lines.index(end)
if start >= stop or stop != len(lines) - 1:
    raise SystemExit("invalid ASCII armor boundaries or trailing content")
block = lines[start + 1 : stop]
separator = block.index("")
headers = block[:separator]
if any(":" not in header for header in headers):
    raise SystemExit("invalid ASCII armor header")
body = [line for line in block[separator + 1 :] if line]
crc_lines = [line for line in body if line.startswith("=")]
if len(crc_lines) != 1 or body[-1] != crc_lines[0]:
    raise SystemExit("missing or misplaced ASCII armor CRC24")
payload = base64.b64decode("".join(body[:-1]), validate=True)
expected_crc = base64.b64decode(crc_lines[0][1:], validate=True)
crc = 0xB704CE
for octet in payload:
    crc ^= octet << 16
    for _ in range(8):
        crc <<= 1
        if crc & 0x1000000:
            crc ^= 0x1864CFB
crc &= 0xFFFFFF
if crc.to_bytes(3, "big") != expected_crc:
    raise SystemExit("ASCII armor CRC24 mismatch")
target.write_bytes(payload)
print("armor_validation=pass")
print("crc24_validation=pass")
print("keyring_sha256=" + hashlib.sha256(payload).hexdigest())
