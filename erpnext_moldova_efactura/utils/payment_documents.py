"""Outgoing e-Factura attached documents taken from Payment Entries."""

from __future__ import annotations

import base64
import os
import re
from datetime import datetime

import frappe
from frappe.utils import cint, flt, getdate

_MEV_URL = re.compile(r"https?://[^\s\"'<>]*mev\.sfs\.md[^\s\"'<>]*", re.IGNORECASE)
_FILE_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}
_RECEIPT_WIDTH_MM = 80
_A4_WIDTH_MM = 210
_A4_HEIGHT_MM = 297
_RECEIPT_DPI = 144


def document_names_by_mode() -> dict[str, str]:
	if not cint(frappe.db.get_single_value("eFactura Settings", "attach_payment_documents")):
		return {}
	rows = frappe.get_all(
		"eFactura Payment Document",
		filters={"parent": "eFactura Settings", "parenttype": "eFactura Settings"},
		fields=["mode_of_payment", "document_name"],
	)
	names = {}
	for row in rows:
		mode = (row.mode_of_payment or "").strip()
		title = (row.document_name or "").strip()
		if mode and title:
			names[mode] = title
	return names


def payments_for_sales_invoice(sales_invoice: str) -> list[dict]:
	if not sales_invoice:
		return []
	return frappe.db.sql(
		"""
		SELECT pe.name, pe.mode_of_payment, pe.reference_no, pe.reference_date,
			pe.remarks, pe.posting_date, pe.paid_amount, per.allocated_amount
		FROM `tabPayment Entry` pe
		INNER JOIN `tabPayment Entry Reference` per ON per.parent = pe.name
		WHERE per.reference_doctype = 'Sales Invoice'
			AND per.reference_name = %(sales_invoice)s
			AND pe.docstatus = 1
		ORDER BY pe.posting_date, pe.name
		""",
		{"sales_invoice": sales_invoice},
		as_dict=True,
	)


def mev_urls(*texts: str | None) -> list[str]:
	found = []
	seen = set()
	for text in texts:
		if not text:
			continue
		for match in _MEV_URL.findall(text):
			url = match.rstrip(".,);")
			if url not in seen:
				seen.add(url)
				found.append(url)
	return found


def comment_text(payment_entry: str) -> str:
	rows = frappe.get_all(
		"Comment",
		filters={
			"reference_doctype": "Payment Entry",
			"reference_name": payment_entry,
			"comment_type": "Comment",
		},
		fields=["content"],
	)
	return "\n".join((row.content or "") for row in rows)


_PDF_ROWS = (
	("Tipul plății", "Тип оплаты", "type"),
	("Numărul bonului / tranzacției", "Номер чека / транзакции", "number"),
	("Data bonului / tranzacției", "Дата чека / транзакции", "date_label"),
	("Suma plătită", "Оплаченная сумма", "paid_amount_label"),
	("Suma alocată", "Распределённая сумма", "allocated_amount_label"),
	("Link de verificare MEV", "Ссылка проверки MEV", "url"),
)
def payment_pdf_file_name(efactura) -> str:
	return f"situatia-platilor-{efactura.name}.pdf"


def attached_document_rows(sales_invoice: str) -> list[dict]:
	names = document_names_by_mode()
	if not names or not sales_invoice:
		return []
	rows = []
	for payment in payments_for_sales_invoice(sales_invoice):
		title = names.get((payment.mode_of_payment or "").strip())
		if not title:
			continue
		urls = mev_urls(payment.remarks, comment_text(payment.name))
		row = {
			"type": title,
			"number": (payment.reference_no or "").strip(),
			"paid_amount": flt(payment.paid_amount),
			"allocated_amount": flt(payment.allocated_amount),
			"url": urls[0] if urls else "",
		}
		if payment.reference_date:
			row["date"] = datetime.combine(getdate(payment.reference_date), datetime.min.time()).isoformat()
		row["payment_entry"] = payment.name
		rows.append(row)
	return rows


def payment_files(payment_names: list[str]) -> dict[str, str]:
	"""First pdf/image File name for each Payment Entry, in posting order of payment_names."""
	if not payment_names:
		return {}
	files = frappe.get_all(
		"File",
		filters={"attached_to_doctype": "Payment Entry", "attached_to_name": ["in", payment_names]},
		fields=["name", "file_name", "attached_to_name"],
		order_by="creation asc",
	)
	found = {}
	for file_row in files:
		ext = os.path.splitext(file_row.file_name or "")[1].lower()
		if ext not in _FILE_EXTENSIONS:
			continue
		found.setdefault(file_row.attached_to_name, file_row.name)
	return found


def sync_attached_documents(doc) -> None:
	"""Fill an empty draft table from Payment Entries before the form opens. Existing rows stay as edited."""
	if cint(getattr(doc, "docstatus", 0)) != 0:
		return
	if doc.get("attached_documents"):
		return
	from erpnext_moldova_efactura.utils.si_link import sales_invoice_of

	source = attached_document_rows(sales_invoice_of(doc))
	if not source:
		return
	files = payment_files([row["payment_entry"] for row in source if row.get("payment_entry")])
	for row in source:
		doc.append(
			"attached_documents",
			{
				"payment_entry": row.get("payment_entry"),
				"document_type": row["type"],
				"document_number": row.get("number"),
				"paid_amount": row.get("paid_amount"),
				"allocated_amount": row.get("allocated_amount"),
				"verification_url": row.get("url"),
				"document_date": (row.get("date") or "")[:10] or None,
				"file": files.get(row.get("payment_entry")),
			},
		)


def xml_rows_from_doc(doc) -> list[dict]:
	if not cint(frappe.db.get_single_value("eFactura Settings", "attach_payment_documents")):
		return []
	rows = []
	for row in doc.get("attached_documents") or []:
		title = (row.document_type or "").strip()
		if not title:
			continue
		item = {"type": title, "payment_entry": row.payment_entry, "file": row.file}
		number = (row.document_number or "").strip()
		if number:
			item["number"] = number
		if row.document_date:
			item["date"] = datetime.combine(getdate(row.document_date), datetime.min.time()).isoformat()
			item["date_label"] = getdate(row.document_date).strftime("%d.%m.%Y")
		item["paid_amount_label"] = _money(getattr(row, "paid_amount", None))
		item["allocated_amount_label"] = _money(getattr(row, "allocated_amount", None))
		item["url"] = (getattr(row, "verification_url", None) or "").strip()
		rows.append(item)
	return rows


def append_attached_documents(supplier_info, rows: list[dict]) -> None:
	if not rows:
		return
	import xml.etree.ElementTree as ET

	parent = ET.SubElement(supplier_info, "AttachedDocuments")
	for row in rows:
		attrs = {"Type": row["type"]}
		if row.get("number"):
			attrs["Number"] = row["number"]
		if row.get("date"):
			attrs["Date"] = row["date"]
		ET.SubElement(parent, "Document", attrs)


def _money(value) -> str:
	if value is None or value == "":
		return ""
	return f"{flt(value):.2f}"


def _esc(value) -> str:
	return frappe.utils.escape_html(value or "")


def _cell_html(key: str, row: dict) -> str:
	if key != "url":
		return _esc(row.get(key))
	url = (row.get("url") or "").strip()
	if not url:
		return ""
	safe = _esc(url)
	return f'<a href="{safe}">{safe}</a>'


def cover_html(rows: list[dict]) -> str:
	blocks = []
	for index, row in enumerate(rows, start=1):
		body = []
		for ro, ru, key in _PDF_ROWS:
			body.append(
				"<tr>"
				f"<td class='label'><div class='ro'>{ro}</div><div class='ru'>{ru}</div></td>"
				f"<td class='value'>{_cell_html(key, row)}</td>"
				"</tr>"
			)
		heading = f"<h2>Document {index}</h2>" if len(rows) > 1 else ""
		blocks.append(heading + "<table>" + "".join(body) + "</table>")
	return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/>
<style>
body {{ font-family: DejaVu Sans, sans-serif; font-size: 12pt; color: #111; }}
table {{ width: 100%; border-collapse: collapse; margin-bottom: 28px; }}
td {{ border: 1px solid #ccc; padding: 10px 12px; vertical-align: top; }}
td.label {{ width: 46%; }}
.ro {{ font-weight: 700; }}
.ru {{ font-weight: 400; color: #444; margin-top: 2px; }}
td.value {{ font-size: 13pt; }}
h2 {{ font-size: 13pt; margin: 0 0 8px; }}
</style></head><body>
<h1>Documente anexate</h1>
{''.join(blocks)}
</body></html>"""


def _mm_to_px(mm: float, dpi: int = _RECEIPT_DPI) -> int:
	return max(1, round(mm / 25.4 * dpi))


def _layout_receipt_pages(image):
	"""Fit a receipt to 80mm wide. Slice overflow to A4 height and place strips left to right."""
	from PIL import Image

	if image.mode not in ("RGB", "L"):
		image = image.convert("RGB")
	page_w = _mm_to_px(_A4_WIDTH_MM)
	page_h = _mm_to_px(_A4_HEIGHT_MM)
	receipt_w = _mm_to_px(_RECEIPT_WIDTH_MM)
	scaled_h = max(1, round(image.height * receipt_w / image.width))
	image = image.resize((receipt_w, scaled_h), Image.Resampling.LANCZOS)
	strips = []
	top = 0
	while top < scaled_h:
		strips.append(image.crop((0, top, receipt_w, min(top + page_h, scaled_h))))
		top += page_h
	columns = max(1, page_w // receipt_w)
	pages = []
	for start in range(0, len(strips), columns):
		page = Image.new("RGB", (page_w, page_h), "white")
		for index, strip in enumerate(strips[start : start + columns]):
			page.paste(strip, (index * receipt_w, 0))
		pages.append(page)
	return pages


def _as_pdf(content: bytes, file_name: str) -> bytes | None:
	if not content:
		return None
	ext = os.path.splitext(file_name or "")[1].lower()
	if ext == ".pdf" or content[:4] == b"%PDF":
		return content
	if ext not in _FILE_EXTENSIONS:
		return None
	from io import BytesIO

	from PIL import Image

	pages = _layout_receipt_pages(Image.open(BytesIO(content)))
	out = BytesIO()
	pages[0].save(
		out,
		format="PDF",
		save_all=True,
		append_images=pages[1:],
		resolution=_RECEIPT_DPI,
	)
	return out.getvalue()


def merge_pdfs(cover: bytes, sources: list[bytes]) -> bytes:
	from io import BytesIO

	from pypdf import PdfReader, PdfWriter

	writer = PdfWriter()
	for page in PdfReader(BytesIO(cover)).pages:
		writer.add_page(page)
	for source in sources:
		if not source:
			continue
		for page in PdfReader(BytesIO(source)).pages:
			writer.add_page(page)
	out = BytesIO()
	writer.write(out)
	return out.getvalue()


def source_pdf(file_name: str | None) -> bytes | None:
	if not file_name:
		return None
	file_doc = frappe.get_doc("File", file_name)
	content = file_doc.get_content()
	if isinstance(content, str):
		content = content.encode()
	return _as_pdf(content or b"", file_doc.file_name)


def payment_qr(data: str) -> str:
	"""Data-URI PNG for a verification URL. Available in print formats as payment_qr()."""
	if not data:
		return ""
	from base64 import b64encode
	from io import BytesIO

	from pyqrcode import create as qrcreate

	buffer = BytesIO()
	qrcreate(str(data)).png(buffer, scale=4)
	return "data:image/png;base64," + b64encode(buffer.getvalue()).decode()


_CSS_URL = re.compile(r"url\(\s*[^)]+\)", re.IGNORECASE)


def _drop_fetched_css_urls(css: str) -> str:
	def replace(match: re.Match) -> str:
		if "data:" in match.group(0):
			return match.group(0)
		return "none"

	return _CSS_URL.sub(replace, css)


def _inline_print_styles(html: str) -> str:
	"""Keep Frappe print CSS, but read it from disk so wkhtmltopdf does not resolve the site host."""
	from bs4 import BeautifulSoup

	soup = BeautifulSoup(html, "html.parser")
	sites = frappe.local.sites_path
	for link in list(soup.find_all("link")):
		href = link.get("href") or ""
		marker = "/assets/"
		if marker not in href:
			continue
		relative = href.split(marker, 1)[1].split("?", 1)[0]
		path = os.path.join(sites, "assets", relative)
		css = ""
		if os.path.isfile(path):
			with open(path, encoding="utf-8", errors="ignore") as handle:
				css = _drop_fetched_css_urls(handle.read())
		style = soup.new_tag("style")
		style.string = css
		link.replace_with(style)
	for img in soup.find_all("img"):
		src = img.get("src") or ""
		if src.startswith(("http://", "https://")):
			img.decompose()
	return str(soup)


def _cover_pdf(efactura, rows: list[dict]) -> bytes:
	from frappe.utils.pdf import get_pdf

	print_format = (frappe.db.get_single_value("eFactura Settings", "payment_print_format") or "").strip()
	if not print_format:
		return get_pdf(cover_html(rows))
	html = frappe.get_print(
		"Sales eFactura",
		efactura.name,
		print_format=print_format,
		doc=efactura,
		as_pdf=False,
		no_letterhead=1,
	)
	return get_pdf(
		_inline_print_styles(html),
		options={
			"load-error-handling": "ignore",
			"load-media-error-handling": "ignore",
		},
	)


def build_payment_pdf(efactura, rows: list[dict]) -> bytes:
	cover = _cover_pdf(efactura, rows)
	sources = [source_pdf(row.get("file")) for row in rows]
	return merge_pdfs(cover, [src for src in sources if src])


def save_payment_pdf(efactura, content: bytes):
	file_name = payment_pdf_file_name(efactura)
	existing = frappe.get_all(
		"File",
		filters={
			"attached_to_doctype": "Sales eFactura",
			"attached_to_name": efactura.name,
			"file_name": file_name,
		},
		pluck="name",
	)
	for name in existing:
		frappe.delete_doc("File", name, ignore_permissions=True, force=True)
	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": file_name,
			"attached_to_doctype": "Sales eFactura",
			"attached_to_name": efactura.name,
			"is_private": 1,
			"content": content,
		}
	)
	file_doc.save(ignore_permissions=True)
	return file_doc


def attach_payment_pdf(efactura) -> None:
	"""Create situatia-platilor-{name}.pdf on the Sales eFactura when it is submitted."""
	rows = xml_rows_from_doc(efactura)
	if not rows:
		return
	save_payment_pdf(efactura, build_payment_pdf(efactura, rows))


def payment_pdf_attachment(efactura) -> dict | None:
	"""Send the PDF already stored on the submitted document."""
	stored_name = payment_pdf_file_name(efactura)
	file_name = frappe.db.get_value(
		"File",
		{
			"attached_to_doctype": "Sales eFactura",
			"attached_to_name": efactura.name,
			"file_name": stored_name,
		},
		"name",
	)
	if not file_name:
		return None
	content = frappe.get_doc("File", file_name).get_content()
	if isinstance(content, str):
		content = content.encode()
	if not content:
		return None
	return {
		"FileName": stored_name,
		"FileContent": base64.b64encode(content).decode(),
	}


