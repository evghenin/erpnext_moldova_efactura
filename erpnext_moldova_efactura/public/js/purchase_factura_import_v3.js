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
				method: "erpnext_moldova_efactura.utils.pf_original.save_uploaded_original",
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
						timeout: 300,
						freeze: true,
						freeze_message: image
							? __("Reading factura with AI…")
							: __("Reading factura…"),
						callback: (r) =>
							r.message && frappe.set_route("Form", "Purchase Factura", r.message),
					});
				},
			});
			const upload_files = uploader.upload_files.bind(uploader);
			uploader.upload_files = () => {
				(uploader.uploader.files || []).forEach((file) => {
					file.optimize = false;
				});
				return upload_files();
			};
		},
		image ? __("Import Image") : __("Import PDF"),
		__("Upload Document")
	);
};

erpnext_moldova_efactura.pf.import_pdf = function () {
	erpnext_moldova_efactura.pf.import_document("pdf");
};

erpnext_moldova_efactura.pf.import_image = function () {
	if (!cint(frappe.boot && frappe.boot.moldova_efactura_paper_ai)) {
		erpnext_moldova_efactura.pf.show_gemini_setup();
		return;
	}
	erpnext_moldova_efactura.pf.import_document("image");
};

erpnext_moldova_efactura.pf.show_gemini_setup = function () {
	const studio = "https://aistudio.google.com/apikey";
	const settings = "/app/efactura-settings";
	const dialog = new frappe.ui.Dialog({
		title: __("Gemini API Key Required"),
		primary_action_label: __("Open eFactura Settings"),
		primary_action: () => {
			dialog.hide();
			frappe.set_route("Form", "eFactura Settings");
		},
	});
	dialog.$body.html(`
		<p>${__(
			"Image import uses Google Gemini. Create an API key in Google AI Studio, then save it in eFactura Settings."
		)}</p>
		<p>${__("Get the API key")}: <a href="${studio}" target="_blank" rel="noopener">${studio}</a></p>
		<p>${__("Set the key")}: <a href="${settings}">${__(
			"eFactura Settings → Purchase → Paper Import"
		)}</a></p>
	`);
	dialog.show();
};

erpnext_moldova_efactura.pf.bind_import_buttons = function (add_button) {
	add_button(__("Any Image with AI"), erpnext_moldova_efactura.pf.import_image);
	add_button(__("PDF Orange / Arax"), erpnext_moldova_efactura.pf.import_pdf);
};
