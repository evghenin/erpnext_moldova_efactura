frappe.listview_settings["Sales eFactura"] = {
	hide_name_column: false,
	add_fields: ["status", "total", "customer_party"],
	filters: [["status", "not in", ["Cancelled", "Canceled by Supplier"]]],
	get_indicator(doc) {
		const colors = {
			Draft: "gray",
			Cancelled: "darkgrey",
			"Pending Registration": "orange",
			"Registered as Draft": "orange",
			"Signed by Supplier": "blue",
			"Rejected by Customer": "red",
			"Accepted by Customer": "yellow",
			"Canceled by Supplier": "darkgrey",
			"Sent to Customer": "yellow",
			"Signed by Customer": "green",
			Transportation: "blue",
			"Cancellation Requested": "purple",
		};
		const status = doc.status || "";
		return [__(status), colors[status] || "gray", "status,=," + status];
	},
};
