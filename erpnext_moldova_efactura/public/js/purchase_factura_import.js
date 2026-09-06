frappe.provide("erpnext_moldova_efactura.pf");

erpnext_moldova_efactura.pf.import_pdf = function () {
	frappe.prompt(
		[
			{
				fieldname: "company",
				fieldtype: "Link",
				options: "Company",
				label: __("Company"),
				reqd: 1,
				default: frappe.defaults.get_user_default("Company"),
			},
		],
		({ company }) => {
			new frappe.ui.FileUploader({
				allow_multiple: false,
				make_attachments_public: false,
				restrictions: { allowed_file_types: [".pdf"], max_file_size: 15 * 1024 * 1024 },
				on_success(file) {
					frappe.call({
						method: "erpnext_moldova_efactura.moldova_efactura.doctype.purchase_factura.purchase_factura.import_pdf",
						args: { file_url: file.file_url, company },
						freeze: true,
						freeze_message: __("Reading factura PDF…"),
						callback: (r) =>
							r.message && frappe.set_route("Form", "Purchase Factura", r.message),
					});
				},
			});
		},
		__("Import Orange / ARAX PDF"),
		__("Upload PDF")
	);
};
