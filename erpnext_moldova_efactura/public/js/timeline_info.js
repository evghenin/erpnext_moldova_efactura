(() => {
	const EF_LOG_JSON_PREFIX = "efj:";
	const EF_LOG_LEGACY_PREFIX = "ef:";

	function user_link(user) {
		const text = (frappe.user_info(user) || {}).fullname || "";
		return frappe.utils.get_form_link("User", user, true, frappe.utils.xss_sanitise(text));
	}

	function user_message(user, message_self, message_other) {
		return frappe.utils.is_current_user(user) ? message_self : message_other;
	}

	function format_ef_log_message(raw) {
		if (!raw) {
			return "";
		}
		if (raw.startsWith(EF_LOG_JSON_PREFIX)) {
			try {
				const payload = JSON.parse(raw.slice(EF_LOG_JSON_PREFIX.length));
				const msgid = payload.msgid || "";
				const args = Array.isArray(payload.args) ? payload.args.slice() : [];
				const translate_args = Array.isArray(payload.translate_args)
					? payload.translate_args
					: [];
				for (const idx of translate_args) {
					if (args[idx] != null && args[idx] !== "") {
						args[idx] = __(cstr(args[idx]));
					}
				}
				return __(msgid, args, "eFactura timeline");
			} catch (e) {
				return raw.slice(EF_LOG_JSON_PREFIX.length);
			}
		}
		if (raw.startsWith(EF_LOG_LEGACY_PREFIX)) {
			// Writer-language plain text from older builds.
			return raw.slice(EF_LOG_LEGACY_PREFIX.length);
		}
		return null;
	}

	function cstr(value) {
		return value == null ? "" : String(value);
	}

	function patch_timeline_proto(proto) {
		if (!proto || proto.__efactura_info_patched || typeof proto.get_info_timeline_contents !== "function") {
			return false;
		}
		proto.get_info_timeline_contents = function () {
			const items = [];
			(this.doc_info.info_logs || []).forEach((info_log) => {
				const rendered = format_ef_log_message(info_log.content || "");
				if (rendered != null) {
					items.push({
						creation: info_log.creation,
						content: user_message(
							info_log.owner,
							__("You {0}", [rendered], "Form timeline"),
							__("{0} {1}", [user_link(info_log.owner), rendered], "Form timeline")
						),
					});
					return;
				}
				items.push({
					creation: info_log.creation,
					content: `${user_link(info_log.owner)} ${info_log.content || ""}`,
				});
			});
			return items;
		};
		proto.__efactura_info_patched = true;
		return true;
	}

	function patch_open_timeline() {
		const timeline = typeof cur_frm !== "undefined" ? cur_frm?.timeline : null;
		if (!timeline) {
			return;
		}
		if (patch_timeline_proto(Object.getPrototypeOf(timeline))) {
			timeline.refresh();
		}
	}

	function patch_footer() {
		const Footer = frappe.ui?.form?.Footer;
		if (!Footer || Footer.prototype.__efactura_timeline_patched) {
			return false;
		}
		const original = Footer.prototype.make_timeline;
		if (typeof original !== "function") {
			return false;
		}
		Footer.prototype.make_timeline = function () {
			original.apply(this, arguments);
			const timeline = this.frm?.timeline;
			if (timeline && patch_timeline_proto(Object.getPrototypeOf(timeline))) {
				timeline.refresh();
			}
		};
		Footer.prototype.__efactura_timeline_patched = true;
		patch_open_timeline();
		return true;
	}

	const try_patch = () => {
		if (typeof frappe === "undefined") {
			setTimeout(try_patch, 200);
			return;
		}
		if (patch_footer()) {
			return;
		}
		setTimeout(try_patch, 200);
	};
	try_patch();
})();
