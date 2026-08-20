// Mapped PI/PO from e-Factura: ERPNext still calls apply_price_list after
// load_after_mapping (price_list_currency → plc_conversion_rate, buying_price_list).
// That overwrites XML line rates with last purchase / price list (e.g. 26.080000).
(function () {
	function patch_apply_price_list() {
		const Controller = window.erpnext && erpnext.TransactionController;
		if (!Controller || !Controller.prototype || !Controller.prototype.apply_price_list) {
			return false;
		}
		const proto = Controller.prototype;
		if (proto.apply_price_list.__efactura_mapped_skip) {
			return true;
		}
		const original = proto.apply_price_list;
		proto.apply_price_list = function (item, reset_plc_conversion) {
			if (this.frm && this.frm.doc && this.frm.doc.__onload && this.frm.doc.__onload.load_after_mapping) {
				return;
			}
			return original.call(this, item, reset_plc_conversion);
		};
		proto.apply_price_list.__efactura_mapped_skip = true;
		return true;
	}

	if (!patch_apply_price_list()) {
		$(document).on("app_ready", patch_apply_price_list);
	}
})();
