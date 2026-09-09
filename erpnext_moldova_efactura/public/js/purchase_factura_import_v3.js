frappe.provide("erpnext_moldova_efactura.pf");

erpnext_moldova_efactura.pf.import_document = function (kind) {
	const image = kind === "image";
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
			const uploader = new frappe.ui.FileUploader({
				allow_multiple: false,
				make_attachments_public: false,
				allow_toggle_optimize: false,
				restrictions: {
					allowed_file_types: image
						? [".pdf", "image/*", ".jpg", ".jpeg", ".png"]
						: [".pdf"],
					max_file_size: 15 * 1024 * 1024,
				},
				on_success(file) {
					const args = { file_url: file.file_url, company };
					if (image) {
						args.use_ai = 1;
					}
					frappe.call({
						method: "erpnext_moldova_efactura.moldova_efactura.doctype.purchase_factura.purchase_factura.import_pdf",
						args,
						freeze: true,
						freeze_message: image
							? __("Reading factura with AI…")
							: __("Reading factura…"),
						callback: (r) =>
							r.message && frappe.set_route("Form", "Purchase Factura", r.message),
					});
				},
			});
			if (image && uploader.uploader?.add_files) {
				const add_files = uploader.uploader.add_files;
				uploader.uploader.add_files = (file_array) => {
					add_files(file_array);
					(uploader.uploader.files || []).forEach((file) => {
						file.optimize = false;
					});
				};
			}
		},
		image ? __("Import Image") : __("Import PDF"),
		__("Upload Document")
	);
};

erpnext_moldova_efactura.pf.import_pdf = function () {
	erpnext_moldova_efactura.pf.import_document("pdf");
};

erpnext_moldova_efactura.pf.import_image = function () {
	erpnext_moldova_efactura.pf.import_document("image");
};
