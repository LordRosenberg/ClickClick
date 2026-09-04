"""Auto-generated self-signed TLS cert for the console.

HTTPS matters because browsers disable WebCodecs outside secure contexts:
over plain ``http://`` a remote LAN viewer can load the console, but the
LIVE mirror can never decode H.264. The cert is generated locally at
startup, covers the machine's current LAN IPs, and is regenerated
automatically when that set changes — no manual step, and nothing needs
to be installed on client devices (they click through the warning once).
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import logging
import os
import socket
from pathlib import Path

logger = logging.getLogger(__name__)

_CERT_DAYS = 3650
_RENEW_BEFORE_DAYS = 30


def detect_lan_ips() -> list[str]:
    """Best-effort list of this machine's non-loopback IPv4 addresses."""
    ips: list[str] = []

    def _add(raw: str) -> None:
        try:
            addr = ipaddress.ip_address(raw)
        except ValueError:
            return
        if addr.is_loopback or addr.is_link_local or not (addr.is_private or addr.is_global):
            return
        if raw not in ips:
            ips.append(raw)

    # UDP "connect" picks the outbound source address without sending traffic.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 80))  # TEST-NET-1, never routed
            _add(sock.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            _add(info[4][0])
    except OSError:
        pass
    return ips


def _write_cert(cert_path: Path, key_path: Path, lan_ips: list[str]) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = dt.datetime.now(dt.timezone.utc)
    san: list[x509.GeneralName] = [
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
    ]
    san += [x509.IPAddress(ipaddress.ip_address(ip)) for ip in lan_ips]
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "clickclick-console")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=1))
        .not_valid_after(now + dt.timedelta(days=_CERT_DAYS))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    os.chmod(key_path, 0o600)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def _cert_covers(cert_path: Path, lan_ips: list[str]) -> bool:
    """True when the existing cert covers all current LAN IPs and is fresh."""
    try:
        from cryptography import x509

        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        covered = {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}
        if not set(lan_ips) <= covered:
            return False
        remaining = cert.not_valid_after_utc - dt.datetime.now(dt.timezone.utc)
        return remaining > dt.timedelta(days=_RENEW_BEFORE_DAYS)
    except Exception:  # noqa: BLE001 — any parse problem → regenerate
        return False


def ensure_dev_cert(certfile: str, keyfile: str) -> None:
    """Generate the console cert when missing, stale, or IP coverage changed."""
    cert_path, key_path = Path(certfile), Path(keyfile)
    lan_ips = detect_lan_ips()
    if cert_path.exists() and key_path.exists() and _cert_covers(cert_path, lan_ips):
        return
    _write_cert(cert_path, key_path, lan_ips)
    suffix = "".join(f", {ip}" for ip in lan_ips)
    logger.info(
        "generated self-signed console TLS cert at %s (SAN: localhost, 127.0.0.1%s)",
        cert_path,
        suffix,
    )
