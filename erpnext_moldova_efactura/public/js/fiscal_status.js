// Shared Fiscalization rendering. Form indicator and list column both show
// the stored `fiscal_status` field — never a computed fallback like Pending.
frappe.provide("erpnext_moldova_efactura.fiscal_status");

(function (ns) {
	const COLORS = {
		Pending: "red",
		Partial: "red",
		"In Progress": "yellow",
		Completed: "green",
		Failed: "red",
		"Not Required": "gray",
		"Not Applicable": "gray",
		Unknown: "red",
	};

	ns.color = function (status) {
		if (!status) {
			return "gray";
		}
		const base = String(status).replace(/ \(Draft\)$/, "");
		return COLORS[base] || "gray";
	};

	ns.set_form_indicator = function (frm, status) {
		if (cint(frm.doc.docstatus) !== 1 || !status) {
			return;
		}
		frm.page.set_indicator(__("Fiscalization: {0}", [__(status)]), ns.color(status));
	};

	ns.list_pill = function (status, docstatus) {
		if (cint(docstatus) === 2) {
			return "";
		}
		if (cint(docstatus) !== 1 || !status) {
			return "";
		}
		return `<span class="indicator-pill no-indicator-dot ${ns.color(status)}">${__(
			status
		)}</span>`;
	};
})(erpnext_moldova_efactura.fiscal_status);
