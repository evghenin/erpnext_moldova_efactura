"""Local PDF signature inspection for Purchase Factura originals.

Detects the embedded SubFilter variants seen on Orange/ARAX samples and checks
CMS integrity over /ByteRange with OpenSSL ``cms -verify -noverify``.
Certificate trust, revocation, and timestamp authority checks stay Not Checked.
A passed integrity check is never mapped to overall Valid.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any

from erpnext_moldova_efactura.utils.factura_pdf import FacturaImportError

CHECKED = "Not Checked"
NOT_APPLICABLE = "Not Applicable"
PASSED = "Passed"
FAILED = "Failed"
INDETERMINATE = "Indeterminate"
INVALID = "Invalid"


def _text(value) -> str:
	return str(value or "").strip().lstrip("/")


def _dn_attr(subject: str, name: str) -> str:
	match = re.search(rf"(?:^|, )\s*{re.escape(name)}\s*=\s*(.+?)(?:,\s*[A-Za-z.]+\s*=|$)", subject)
	value = (match.group(1) if match else "").strip()
	return value.replace('\\"', '"').replace("\\(", "(").replace("\\)", ")")


def _format_datetime(stamp: datetime) -> str:
	return stamp.strftime("%a, ") + f"{stamp.day} " + stamp.strftime("%b %Y %H:%M:%S %z")


def _format_pdf_time(raw: str) -> str:
	match = re.match(
		r"D:(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(Z|[+-]\d{2}'?\d{2}'?)?",
		_text(raw),
	)
	if not match:
		return _text(raw)
	year, month, day, hour, minute, second, offset = match.groups()
	try:
		stamp = datetime(int(year), int(month), int(day), int(hour), int(minute), int(second))
	except ValueError:
		return _text(raw)
	if not offset or offset == "Z":
		stamp = stamp.replace(tzinfo=timezone.utc)
	else:
		sign = 1 if offset[0] == "+" else -1
		digits = re.sub(r"\D", "", offset[1:])
		minutes = int((digits + "00")[:4] or "0")
		stamp = stamp.replace(
			tzinfo=timezone(sign * timedelta(hours=minutes // 100, minutes=minutes % 100))
		)
	return _format_datetime(stamp)


def _pdf_tzinfo(raw: str):
	match = re.match(
		r"D:\d{14}(Z|[+-]\d{2}'?\d{2}'?)?",
		_text(raw),
	)
	if not match or not match.group(1) or match.group(1) == "Z":
		return timezone.utc
	offset = match.group(1)
	sign = 1 if offset[0] == "+" else -1
	digits = re.sub(r"\D", "", offset[1:])
	minutes = int((digits + "00")[:4] or "0")
	return timezone(sign * timedelta(hours=minutes // 100, minutes=minutes % 100))


def _format_cms_time(raw: str, declared_pdf_time: str) -> str:
	text = _text(raw)
	stamp = None
	for fmt in ("%y%m%d%H%M%SZ", "%Y%m%d%H%M%SZ"):
		try:
			stamp = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
			break
		except ValueError:
			continue
	if stamp is None:
		return text
	return _format_datetime(stamp.astimezone(_pdf_tzinfo(declared_pdf_time)))


def _openssl_text(cms_der: bytes, args: list[str]) -> str:
	der = cms_der.rstrip(b"\x00")
	if not der:
		return ""
	try:
		with tempfile.TemporaryDirectory() as tempdir:
			path = os.path.join(tempdir, "signature.der")
			with open(path, "wb") as handle:
				handle.write(der)
			result = subprocess.run(
				["openssl", *args, "-inform", "DER", "-in", path],
				capture_output=True,
				timeout=20,
				check=False,
			)
	except (FileNotFoundError, subprocess.TimeoutExpired):
		return ""
	return (result.stdout or b"").decode("utf-8", "replace")


def _leaf_certificate(cms_der: bytes) -> dict[str, str]:
	output = _openssl_text(cms_der, ["pkcs7", "-print_certs", "-noout"])
	subject = ""
	for line in output.splitlines():
		if line.startswith("subject="):
			subject = line[len("subject=") :].strip()
			break
	if not subject:
		return {}
	return {
		"signer_name": _dn_attr(subject, "CN"),
		"serial_number": _dn_attr(subject, "serialNumber"),
		"organization": _dn_attr(subject, "O"),
	}


def _cms_signing_time(cms_der: bytes) -> str:
	output = _openssl_text(cms_der, ["cms", "-cmsout", "-print"])
	match = re.search(r"signingTime[\s\S]{0,500}?(?:UTC|GENERALIZED)TIME\s+:([0-9]+Z)", output)
	return match.group(1) if match else ""


def _byte_range(signature) -> list[int] | None:
	raw = signature.get("/ByteRange")
	if raw is None:
		return None
	try:
		values = [int(part) for part in raw]
	except (TypeError, ValueError):
		return None
	if len(values) != 4 or any(part < 0 for part in values):
		return None
	return values


def _cms_der(signature) -> bytes:
	contents = signature.get("/Contents")
	if contents is None:
		return b""
	if hasattr(contents, "original_bytes"):
		return bytes(contents.original_bytes)
	return bytes(contents)


def _coverage(content: bytes, byte_range: list[int]) -> dict[str, Any]:
	start, first_len, second_start, second_len = byte_range
	signed_end = second_start + second_len
	trailing = content[signed_end:] if signed_end <= len(content) else b""
	gap_ok = second_start >= start + first_len
	in_file = signed_end <= len(content) and start + first_len <= len(content)
	all_zero = bool(trailing) and trailing == b"\x00" * len(trailing)
	note_parts = [
		f"signed revision {signed_end} of {len(content)} bytes",
	]
	if trailing:
		kind = "zero padding" if all_zero else "trailing bytes"
		note_parts.append(f"{len(trailing)} {kind} after the signed revision")
	if not gap_ok or not in_file:
		note_parts.append("ByteRange does not cover a valid signed revision")
	return {
		"byte_range": byte_range,
		"signed_revision_end": signed_end,
		"file_size": len(content),
		"trailing_bytes": len(trailing),
		"trailing_all_zero": all_zero,
		"byte_range_valid": gap_ok and in_file,
		"coverage": "; ".join(note_parts),
	}


def _signed_bytes(content: bytes, byte_range: list[int]) -> bytes:
	start, first_len, second_start, second_len = byte_range
	return content[start : start + first_len] + content[second_start : second_start + second_len]


def signatures_from_reader(reader, content: bytes) -> list[dict[str, Any]]:
	found: list[dict[str, Any]] = []
	for name, field in (reader.get_fields() or {}).items():
		if field.get("/FT") != "/Sig" or not field.get("/V"):
			continue
		signature = field["/V"].get_object()
		byte_range = _byte_range(signature)
		cms_der = _cms_der(signature)
		cert = _leaf_certificate(cms_der)
		declared = _text(signature.get("/M"))
		signing_time = _cms_signing_time(cms_der)
		entry: dict[str, Any] = {
			"field": name,
			"format": _text(signature.get("/SubFilter")),
			"declared_time": declared,
			"signing_time": signing_time,
			"time_display": (
				_format_cms_time(signing_time, declared) if signing_time else _format_pdf_time(declared)
			),
			"signer_name": cert.get("signer_name") or _text(signature.get("/Name")),
			"serial_number": cert.get("serial_number") or "",
			"organization": cert.get("organization") or "",
			"reason": _text(signature.get("/Reason")),
			"location": _text(signature.get("/Location")),
			"cms_der": cms_der,
			"byte_range": byte_range,
			"certificate_trust": CHECKED,
			"revocation": CHECKED,
			"timestamp": CHECKED,
			"integrity": CHECKED,
		}
		if byte_range:
			entry.update(_coverage(content, byte_range))
		else:
			entry["coverage"] = "No ByteRange on the signature field"
			entry["byte_range_valid"] = False
		found.append(entry)
	return found


def extract_pdf_signatures(content: bytes) -> list[dict[str, Any]]:
	from pypdf import PdfReader

	try:
		reader = PdfReader(io.BytesIO(content))
	except Exception as exc:
		raise FacturaImportError("Unable to read this PDF; register it manually") from exc
	return signatures_from_reader(reader, content)


def _openssl_cms_verify(signed: bytes, cms_der: bytes) -> tuple[bool, str]:
	try:
		with tempfile.TemporaryDirectory() as tempdir:
			sig_path = os.path.join(tempdir, "signature.der")
			data_path = os.path.join(tempdir, "signed-bytes.bin")
			with open(sig_path, "wb") as handle:
				handle.write(cms_der)
			with open(data_path, "wb") as handle:
				handle.write(signed)
			result = subprocess.run(
				[
					"openssl",
					"cms",
					"-verify",
					"-binary",
					"-inform",
					"DER",
					"-in",
					sig_path,
					"-content",
					data_path,
					"-noverify",
					"-out",
					os.devnull,
				],
				capture_output=True,
				text=True,
				timeout=20,
				check=False,
			)
	except FileNotFoundError as exc:
		raise FacturaImportError("OpenSSL is required to verify PDF signatures") from exc
	except subprocess.TimeoutExpired as exc:
		raise FacturaImportError("PDF signature verification timed out") from exc
	message = (result.stderr or result.stdout or "").strip()
	return result.returncode == 0, message


def summarize_signatures(signatures: list[dict[str, Any]], *, verified: bool) -> dict[str, Any]:
	if not signatures:
		return {
			"signature_status": NOT_APPLICABLE,
			"signature_integrity": NOT_APPLICABLE,
			"signature_format": "",
			"signature_field": "",
			"signature_declared_time": "",
			"signature_coverage": "No embedded PDF signature field",
			"signature_certificate_trust": NOT_APPLICABLE,
			"signature_revocation": NOT_APPLICABLE,
			"signature_timestamp_check": NOT_APPLICABLE,
			"signature_evidence": json.dumps({"signatures": [], "overall": NOT_APPLICABLE}),
		}
	primary = signatures[0]
	formats = ", ".join(filter(None, (row.get("format") for row in signatures)))
	fields = ", ".join(filter(None, (row.get("field") for row in signatures)))
	if verified:
		integrities = {row.get("integrity") for row in signatures}
		if FAILED in integrities:
			status, integrity = INVALID, FAILED
		else:
			status, integrity = INDETERMINATE, PASSED
		trust = CHECKED
		timestamps = {row.get("timestamp") for row in signatures}
		timestamp_status = FAILED if FAILED in timestamps else (PASSED if PASSED in timestamps else CHECKED)
	else:
		status, integrity, trust, timestamp_status = CHECKED, CHECKED, CHECKED, CHECKED
	public = []
	for row in signatures:
		public.append(
			{
				key: row[key]
				for key in (
					"field",
					"format",
					"declared_time",
					"time_display",
					"signer_name",
					"serial_number",
					"organization",
					"reason",
					"location",
					"byte_range",
					"signed_revision_end",
					"file_size",
					"trailing_bytes",
					"trailing_all_zero",
					"byte_range_valid",
					"coverage",
					"integrity",
					"openssl_message",
					"certificate_trust",
					"revocation",
					"timestamp",
				)
				if key in row
			}
		)
	return {
		"signature_status": status,
		"signature_integrity": integrity,
		"signature_format": formats,
		"signature_field": fields,
		"signature_declared_time": primary.get("declared_time") or "",
		"signature_coverage": primary.get("coverage") or "",
		"signature_certificate_trust": trust if signatures else NOT_APPLICABLE,
		"signature_revocation": trust if signatures else NOT_APPLICABLE,
		"signature_timestamp_check": timestamp_status if signatures else NOT_APPLICABLE,
		"signature_evidence": json.dumps({"signatures": public, "overall": status}, ensure_ascii=True),
	}


def inspect_pdf_signatures(content: bytes) -> dict[str, Any]:
	return summarize_signatures(extract_pdf_signatures(content), verified=False)


def verify_pdf_signatures(content: bytes) -> dict[str, Any]:
	signatures = extract_pdf_signatures(content)
	for row in signatures:
		byte_range = row.get("byte_range")
		cms_der = row.get("cms_der") or b""
		if not row.get("byte_range_valid") or not byte_range or not cms_der:
			row["integrity"] = FAILED
			row["openssl_message"] = "Missing CMS contents or invalid ByteRange"
			continue
		ok, message = _openssl_cms_verify(_signed_bytes(content, byte_range), cms_der)
		row["integrity"] = PASSED if ok else FAILED
		row["openssl_message"] = message
		if ok and row.get("signing_time"):
			row["timestamp"] = PASSED
		elif not ok:
			row["timestamp"] = FAILED
	return summarize_signatures(signatures, verified=True)
