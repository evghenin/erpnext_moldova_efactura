import frappe
from frappe import _


def protect_purchase_factura_original(doc, method=None):
	"""Keep a PF original until that PF is deleted, then remove its own file."""
	if not doc.file_url or not frappe.db.table_exists("Purchase Factura"):
		return
	if frappe.flags.get("pf_copying_original") == doc.attached_to_name:
		return
	deleting = frappe.flags.get("pf_deleting")
	if deleting and doc.attached_to_doctype == "Purchase Factura" and doc.attached_to_name == deleting:
		if not frappe.db.exists("File", {"file_url": doc.file_url, "name": ["!=", doc.name]}):
			try:
				doc.delete_file_from_filesystem()
			except OSError:
				pass
		return
	if (
		doc.attached_to_doctype == "Purchase Factura"
		and doc.attached_to_field == "original_file"
		and doc.attached_to_name
		and frappe.db.exists("Purchase Factura", doc.attached_to_name)
	):
		frappe.throw(
			_("File is the preserved original for Purchase Factura {0} and cannot be deleted").format(
				doc.attached_to_name
			)
		)
	holders = frappe.get_all("Purchase Factura", {"original_file": doc.file_url}, pluck="name")
	holders = [name for name in holders if name != deleting]
	if holders:
		frappe.throw(
			_("File is the preserved original for Purchase Factura {0} and cannot be deleted").format(
				holders[0]
			)
		)


def copy_original_file(doc):
	"""Write a private copy of the original onto this PF instead of sharing a file URL."""
	if not doc.original_file or not doc.name:
		return doc.original_file
	from erpnext_moldova_efactura.moldova_efactura.doctype.purchase_factura.purchase_factura import (
		_read_original,
	)

	source_url = doc.original_file
	source = _read_original(source_url)
	content = source.get_content()
	if isinstance(content, str):
		content = content.encode("utf-8")
	file_name = source.file_name or "original.bin"
	if not file_name.startswith(f"{doc.name}-"):
		file_name = f"{doc.name}-{file_name}"
	existing = frappe.db.get_value(
		"File",
		{
			"attached_to_doctype": "Purchase Factura",
			"attached_to_name": doc.name,
			"attached_to_field": "original_file",
			"file_url": ["!=", source_url],
		},
		"file_url",
	)
	if existing:
		return existing
	copied = frappe.new_doc("File")
	copied.file_name = file_name
	copied.is_private = 1
	copied.attached_to_doctype = "Purchase Factura"
	copied.attached_to_name = doc.name
	copied.attached_to_field = "original_file"
	copied.flags.ignore_duplicate_entry_error = True
	save_file = copied.save_file

	def save_unique(*args, **kwargs):
		kwargs["ignore_existing_file_check"] = True
		return save_file(*args, **kwargs)

	copied.save_file = save_unique
	copied.content = content
	copied.insert(ignore_permissions=True)
	# File.insert may re-encode JPEGs (EXIF strip). Keep the imported bytes byte-for-byte.
	from pathlib import Path

	from frappe.core.doctype.file.utils import get_content_hash

	path = Path(copied.get_full_path())
	if path.read_bytes() != content:
		path.write_bytes(content)
		copied.db_set("content_hash", get_content_hash(content), update_modified=False)
		copied.db_set("file_size", len(content), update_modified=False)
	frappe.flags.pf_copying_original = doc.name
	try:
		for extra in frappe.get_all(
			"File",
			filters={
				"name": ["!=", copied.name],
				"attached_to_doctype": "Purchase Factura",
				"attached_to_name": doc.name,
				"file_url": source_url,
			},
			pluck="name",
		):
			frappe.delete_doc("File", extra, ignore_permissions=True, force=True)
	finally:
		frappe.flags.pf_copying_original = None
	return copied.file_url
