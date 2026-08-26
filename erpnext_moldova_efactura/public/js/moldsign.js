// Local MoldSign agent (desktop). Used by Sales/Purchase eFactura form and list.
frappe.provide("erpnext_moldova_efactura.moldsign");

(function () {
	const MOLDSIGN_BASE = "http://localhost:8999";
	const GET_FOR_SIGN =
		"erpnext_moldova_efactura.moldova_efactura.doctype.sales_efactura.sales_efactura.get_for_sign";
	const PROCESS_SIGNED =
		"erpnext_moldova_efactura.moldova_efactura.doctype.sales_efactura.sales_efactura.process_signed_xml";
	const FILTER_SIGNABLE =
		"erpnext_moldova_efactura.moldova_efactura.doctype.sales_efactura.sales_efactura.filter_signable";
	const GET_XML_PEF =
		"erpnext_moldova_efactura.moldova_efactura.doctype.purchase_efactura.purchase_efactura.get_xml_for_sign";
	const PROCESS_SIGNED_PEF =
		"erpnext_moldova_efactura.moldova_efactura.doctype.purchase_efactura.purchase_efactura.process_signed_xml";
	const FILTER_SIGNABLE_PEF =
		"erpnext_moldova_efactura.moldova_efactura.doctype.purchase_efactura.purchase_efactura.filter_signable";
	const FILTER_ACCEPTABLE_PEF =
		"erpnext_moldova_efactura.moldova_efactura.doctype.purchase_efactura.purchase_efactura.filter_acceptable";
	const ACCEPT_PEF =
		"erpnext_moldova_efactura.moldova_efactura.doctype.purchase_efactura.purchase_efactura.accept_invoice";
	const SEND_UNSIGNED =
		"erpnext_moldova_efactura.moldova_efactura.doctype.sales_efactura.sales_efactura.send_unsigned";

	const ns = erpnext_moldova_efactura.moldsign;

	class MoldSignCancelledError extends Error {
		constructor(message) {
			super(message || __("Signing cancelled."));
			this.name = "MoldSignCancelledError";
		}
	}

	ns.CancelledError = MoldSignCancelledError;

	ns.is_cancelled = function (err) {
		if (!err) {
			return false;
		}
		if (err instanceof MoldSignCancelledError || err.name === "MoldSignCancelledError") {
			return true;
		}
		return /cancel/i.test(err.message || String(err));
	};

	function sleep(ms) {
		return new Promise((resolve) => setTimeout(resolve, ms));
	}

	function extract_error(err) {
		const r = err && (err._server_messages ? err : err.responseJSON || err);
		if (r && r._server_messages) {
			try {
				const list = JSON.parse(r._server_messages);
				const parts = list.map((m) => {
					const obj = typeof m === "string" ? JSON.parse(m) : m;
					return obj.message || obj;
				});
				if (parts.length) {
					return parts.join("\n");
				}
			} catch (e) {
				// fall through
			}
		}
		return (err && err.message) || String(err);
	}

	function call_method(method, args) {
		return frappe
			.call({ method, args, silent: true })
			.catch((err) => {
				throw new Error(extract_error(err));
			});
	}

	async function ms_fetch(urlOrPath, options = {}) {
		const url = urlOrPath.startsWith("http") ? urlOrPath : `${MOLDSIGN_BASE}${urlOrPath}`;
		const resp = await fetch(url, {
			method: options.method || "GET",
			headers: options.headers || {},
			body: options.body,
			mode: "cors",
		});
		const text = await resp.text();
		const contentType = resp.headers.get("content-type") || "";
		let data = null;
		if (text && contentType.includes("application/json")) {
			try {
				data = JSON.parse(text);
			} catch (e) {
				data = null;
			}
		}
		return { resp, text, data };
	}

	ns.ping = async function () {
		const { resp, text } = await ms_fetch("/certificates?private_only=true", {
			headers: { Accept: "application/json" },
		});
		if (!resp.ok) {
			throw new Error(`MoldSign not available: HTTP ${resp.status} ${text || ""}`.trim());
		}
	};

	ns.get_private_certs = async function () {
		const { resp, data, text } = await ms_fetch("/certificates?private_only=true", {
			headers: { Accept: "application/json" },
		});
		if (!resp.ok) {
			throw new Error(`MoldSign certificates error: HTTP ${resp.status} ${text || ""}`.trim());
		}
		const list = (data && data.certificateModel) || [];
		return list.filter((c) => c.privateKeyPresent);
	};

	ns.start_sign_session = async function ({ hash_base64, certificate }) {
		const payload = {
			algorithm: "SHA-1",
			signatureType: "Embedded",
			signFormat: "XAdES-T",
			contentType: "Text",
			data: hash_base64,
			certificate: certificate,
		};
		const { resp, text } = await ms_fetch("/sign/data", {
			method: "POST",
			headers: {
				Accept: "application/json",
				"Content-Type": "application/json",
			},
			body: JSON.stringify(payload),
		});
		if (resp.status !== 201) {
			throw new Error(`MoldSign start sign error: HTTP ${resp.status} ${text || ""}`.trim());
		}
		const location = resp.headers.get("location");
		if (!location) {
			throw new Error("MoldSign start sign error: Missing Location header.");
		}
		return location;
	};

	ns.poll_result = async function (location, { timeout_ms = 120000, interval_ms = 800 } = {}) {
		const started = Date.now();
		while (true) {
			if (Date.now() - started > timeout_ms) {
				throw new Error("MoldSign signing timeout.");
			}
			const { resp, data, text } = await ms_fetch(location, {
				headers: { Accept: "application/json" },
			});
			if (resp.ok) {
				return {
					status: resp.status,
					headers: {
						error: resp.headers.get("error"),
						sessionId: resp.headers.get("sessionId"),
						location: resp.headers.get("Location"),
					},
					data: data,
					text: text,
				};
			}
			if (resp.status >= 400 && resp.status < 500) {
				const errHeader = resp.headers.get("error");
				const msg = errHeader || text || `HTTP ${resp.status}`;
				throw new MoldSignCancelledError(`MoldSign signing failed: ${msg}`.trim());
			}
			await sleep(interval_ms);
		}
	};

	ns.choose_certificate = async function (certs) {
		const options = certs.map((c) => ({
			label: c.certificateName,
			value: c.certificateId,
		}));
		return new Promise((resolve, reject) => {
			let settled = false;
			const finish = (ok, value) => {
				if (settled) {
					return;
				}
				settled = true;
				if (ok) {
					resolve(value);
				} else {
					reject(new MoldSignCancelledError());
				}
			};
			const d = new frappe.ui.Dialog({
				title: __("Select certificate"),
				fields: [
					{
						fieldname: "cert",
						fieldtype: "Select",
						label: __("Certificate"),
						options: options,
						default: options[0] ? options[0].value : null,
						reqd: 1,
					},
				],
				primary_action_label: __("Sign"),
				primary_action() {
					const certId = d.get_value("cert");
					const selected = certs.find((c) => c.certificateId === certId) || certs[0];
					finish(true, selected);
					d.hide();
				},
				onhide() {
					finish(false);
				},
			});
			d.set_secondary_action_label(__("Cancel"));
			d.set_secondary_action(() => {
				d.hide();
				finish(false);
			});
			d.show();
		});
	};

	ns.sign_document = async function (name, opts = {}) {
		const freeze = opts.freeze !== false;
		const freeze_msg = opts.freeze_message || __("Signing via MoldSign...");
		const show_alert = opts.show_alert !== false;
		let frozen = false;
		const freeze_on = () => {
			if (freeze && !frozen) {
				frappe.dom.freeze(freeze_msg);
				frozen = true;
			}
		};
		const freeze_off = () => {
			if (frozen) {
				frappe.dom.unfreeze();
				frozen = false;
			}
		};

		try {
			freeze_on();
			await ns.ping();

			const r1 = await call_method(opts.get_method, opts.get_args);
			const xml_base64 = r1.message && r1.message.xml_base64;
			const hash_base64 = r1.message && r1.message.hash_base64;
			if (!xml_base64 || !hash_base64) {
				throw new Error("Backend did not return XML properties.");
			}

			let certificate = opts.certificate;
			if (!certificate) {
				const certs = await ns.get_private_certs();
				if (!certs.length) {
					throw new Error("No private certificates found in MoldSign.");
				}
				freeze_off();
				certificate = await ns.choose_certificate(certs);
				freeze_on();
			}

			const location = await ns.start_sign_session({
				hash_base64: hash_base64,
				certificate: certificate,
			});
			const result = await ns.poll_result(location);
			const signature = result && result.data && result.data.base64File;
			if (!signature) {
				throw new Error("MoldSign did not return a signature.");
			}
			if (show_alert) {
				frappe.show_alert({ message: __("Signed successfully"), indicator: "green" });
			}

			const result2 = await call_method(opts.process_method, {
				name: name,
				signature: signature,
				content: xml_base64,
			});
			if (show_alert && result2.message && result2.message.message) {
				frappe.show_alert({ message: result2.message.message, indicator: "green" });
			}
			return { certificate, message: result2.message };
		} finally {
			freeze_off();
		}
	};

	ns.sign_sales_efactura = function (name, opts = {}) {
		return ns.sign_document(
			name,
			Object.assign({}, opts, {
				get_method: GET_FOR_SIGN,
				get_args: { efactura_name: name },
				process_method: PROCESS_SIGNED,
			})
		);
	};

	ns.sign_purchase_efactura = function (name, opts = {}) {
		return ns.sign_document(
			name,
			Object.assign({}, opts, {
				get_method: GET_XML_PEF,
				get_args: { name: name },
				process_method: PROCESS_SIGNED_PEF,
			})
		);
	};

	function row_name(row) {
		return typeof row === "string" ? row : row.name;
	}

	function row_label(row) {
		if (typeof row === "string") {
			return row;
		}
		const status = row.status ? ` — ${__(row.status)}` : "";
		return `${row.name}${status}`;
	}

	function skipped_items_html(skipped) {
		const escape = frappe.utils.escape_html;
		return skipped
			.map((row) => `<li>${escape(row.name)}: ${escape(row.reason || "")}</li>`)
			.join("");
	}

	function confirm_affected_docs(title, rows, intro, skipped) {
		const escape = frappe.utils.escape_html;
		const eligible_items = rows.map((row) => `<li>${escape(row_label(row))}</li>`).join("");
		let body = "";
		if (skipped && skipped.length) {
			body +=
				`<p>${__("The following documents are not eligible and will be skipped:")}</p>` +
				`<ul>${skipped_items_html(skipped)}</ul>`;
		}
		body += `<p>${escape(intro)}</p><ul>${eligible_items}</ul>`;
		return new Promise((resolve) => {
			let settled = false;
			const finish = (ok) => {
				if (settled) {
					return;
				}
				settled = true;
				resolve(ok);
			};
			const d = new frappe.ui.Dialog({
				title: title,
				fields: [
					{
						fieldtype: "HTML",
						fieldname: "body",
						options: `<div style="max-height: 360px; overflow: auto;">${body}</div>`,
					},
				],
				primary_action_label: __("Confirm"),
				primary_action() {
					finish(true);
					d.hide();
				},
				onhide() {
					finish(false);
				},
			});
			d.set_secondary_action_label(__("Cancel"));
			d.set_secondary_action(() => {
				d.hide();
				finish(false);
			});
			d.show();
		});
	}

	function show_bulk_summary(opts) {
		const done = opts.done || [];
		const failed = opts.failed || [];
		const skipped = opts.skipped || [];
		const escape = frappe.utils.escape_html;
		const parts = [`<p>${__(opts.done_label, [done.length])}</p>`];
		if (failed.length) {
			parts.push(`<p>${__("Failed: {0}", [failed.length])}</p>`);
			parts.push(
				"<ul>" +
					failed
						.map((row) => `<li>${escape(row.name)}: ${escape(row.error || "")}</li>`)
						.join("") +
					"</ul>"
			);
		}
		if (skipped.length) {
			parts.push(`<p>${__("Skipped: {0}", [skipped.length])}</p>`);
			parts.push("<ul>" + skipped_items_html(skipped) + "</ul>");
		}
		if (opts.aborted && opts.abort_message) {
			parts.push(`<p>${opts.abort_message}</p>`);
		}
		frappe.msgprint({
			title: opts.title,
			indicator: failed.length || opts.aborted ? "orange" : "green",
			message: parts.join(""),
		});
	}

	async function choose_bulk_certificate() {
		await ns.ping();
		const certs = await ns.get_private_certs();
		if (!certs.length) {
			throw new Error("No private certificates found in MoldSign.");
		}
		return { certificate: await ns.choose_certificate(certs) };
	}

	ns.run_bulk = async function (names, opts) {
		const r = await call_method(opts.filter_method, { names });
		const payload = r.message || {};
		const eligible_rows = payload[opts.eligible_key] || [];
		const skipped = (payload.skipped || []).slice();
		const title = opts.title;
		const eligible = eligible_rows.map(row_name);

		if (!eligible.length) {
			let message = `<p>${opts.none_message}</p>`;
			if (skipped.length) {
				message += `<ul>${skipped_items_html(skipped)}</ul>`;
			}
			frappe.msgprint({
				title: title,
				indicator: "orange",
				message: message,
			});
			return { done: [], failed: [], skipped, aborted: false };
		}

		const confirmed = await confirm_affected_docs(
			title,
			eligible_rows,
			opts.confirm_intro,
			skipped
		);
		if (!confirmed) {
			return null;
		}

		let ctx = {};
		if (opts.before) {
			try {
				ctx = (await opts.before()) || {};
			} catch (e) {
				if (ns.is_cancelled(e)) {
					return null;
				}
				throw e;
			}
		}

		const done = [];
		const failed = [];
		let aborted = false;

		for (let i = 0; i < eligible.length; i++) {
			const name = eligible[i];
			frappe.show_progress(
				title,
				i,
				eligible.length,
				opts.progress_message(i + 1, eligible.length, name)
			);
			try {
				await opts.run(name, ctx);
				done.push(name);
			} catch (e) {
				failed.push({ name, error: e.message || String(e) });
				if (opts.abort_on_cancel !== false && ns.is_cancelled(e)) {
					aborted = true;
					for (const rest of eligible.slice(i + 1)) {
						skipped.push({ name: rest, reason: __("Signing cancelled.") });
					}
					break;
				}
			}
		}

		frappe.hide_progress();
		show_bulk_summary({
			title: title,
			done_label: opts.done_label,
			done: done,
			failed: failed,
			skipped: skipped,
			aborted: aborted,
			abort_message: opts.abort_message,
		});
		return { done, failed, skipped, aborted };
	};

	ns.bulk_sign_sales_efactura = function (names) {
		return ns
			.run_bulk(names, {
				title: __("Register Signed"),
				filter_method: FILTER_SIGNABLE,
				eligible_key: "signable",
				none_message: __(
					"None of the selected documents can be signed. Only submitted Sales eFactura in Pending Registration are eligible."
				),
				confirm_intro: __(
					"The following Sales eFactura documents will be signed and registered:"
				),
				progress_message: (i, total, name) =>
					__("Signing {0} of {1}: {2}", [i, total, name]),
				done_label: "Signed: {0}",
				abort_message: __("Signing cancelled. Remaining documents were not processed."),
				before: choose_bulk_certificate,
				run: (name, ctx) =>
					ns.sign_sales_efactura(name, {
						certificate: ctx.certificate,
						freeze: false,
						show_alert: false,
					}),
			})
			.then((result) => {
				if (!result) {
					return null;
				}
				return {
					signed: result.done,
					failed: result.failed,
					skipped: result.skipped,
					aborted: result.aborted,
				};
			});
	};

	ns.bulk_register_unsigned_sales_efactura = function (names) {
		return ns.run_bulk(names, {
			title: __("Register Unsigned"),
			filter_method: FILTER_SIGNABLE,
			eligible_key: "signable",
			none_message: __(
				"None of the selected documents can be registered. Only submitted Sales eFactura in Pending Registration are eligible."
			),
			confirm_intro: __(
				"The following Sales eFactura documents will be registered without a signature:"
			),
			progress_message: (i, total, name) =>
				__("Registering {0} of {1}: {2}", [i, total, name]),
			done_label: "Registered: {0}",
			abort_on_cancel: false,
			run: (name) => call_method(SEND_UNSIGNED, { efactura_name: name }),
		});
	};

	ns.bulk_sign_purchase_efactura = function (names) {
		return ns.run_bulk(names, {
			title: __("Sign"),
			filter_method: FILTER_SIGNABLE_PEF,
			eligible_key: "signable",
			none_message: __(
				"None of the selected documents can be signed. Only submitted Purchase eFactura in Sent to Buyer, Signed by Supplier, or Accepted status are eligible."
			),
			confirm_intro: __("The following Purchase eFactura documents will be signed:"),
			progress_message: (i, total, name) => __("Signing {0} of {1}: {2}", [i, total, name]),
			done_label: "Signed: {0}",
			abort_message: __("Signing cancelled. Remaining documents were not processed."),
			before: choose_bulk_certificate,
			run: (name, ctx) =>
				ns.sign_purchase_efactura(name, {
					certificate: ctx.certificate,
					freeze: false,
					show_alert: false,
				}),
		});
	};

	ns.bulk_accept_purchase_efactura = function (names) {
		return ns.run_bulk(names, {
			title: __("Accept"),
			filter_method: FILTER_ACCEPTABLE_PEF,
			eligible_key: "acceptable",
			none_message: __(
				"None of the selected documents can be accepted. Only submitted Purchase eFactura in Sent to Buyer or Signed by Supplier status are eligible."
			),
			confirm_intro: __("The following Purchase eFactura documents will be accepted:"),
			progress_message: (i, total, name) => __("Accepting {0} of {1}: {2}", [i, total, name]),
			done_label: "Accepted: {0}",
			abort_on_cancel: false,
			run: (name) => call_method(ACCEPT_PEF, { name }),
		});
	};
})();
