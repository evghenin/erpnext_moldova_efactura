// Preserve core ERPNext list view settings and extend them.

(() => {
  const existing = frappe.listview_settings['Sales Invoice'] || {};

  const custom = {
    formatters: Object.assign({}, existing.formatters || {}, {
      fiscal_status(value, field, doc) {
        // 1) No status → render empty cell
        if (!value) return '';

        // 2) Canceled invoices with "Pending" status → render empty cell 
        if (doc.docstatus == 2 && value == "Pending") return '';

        const color_map = {
          "Pending": "red",
          "In Progress": "yellow",
          "Partial": "red",
          "Completed": "green",
          "Failed": "red",
          "Not Required": "gray",
          "Not Applicable": "gray",
          "Unknown": "red",
        };

        const color = color_map[value] || "gray";

        // 3) Render clean badge (NO DOT)
        return `
          <span class="indicator-pill no-indicator-dot ${color}">
            ${__(value)}
          </span>
        `;
      }
    }),

    onload(listview) {
      // Keep any existing onload behavior
      if (typeof existing.onload === "function") {
        existing.onload(listview);
      }

      listview.page.add_action_item(
        __('Actualize Fiscal Status'),
        () => {
          const selected = listview.get_checked_items();

          if (!selected.length) {
            frappe.msgprint(__('Please select at least one Sales Invoice.'));
            return;
          }

          const names = selected.map(d => d.name);
          const total = names.length;

          // 1 show progress bar
          frappe.show_progress(
            __('Actualizing Fiscal Status'),
            0,
            total,
            __('Starting...')
          );

          // 2 subscribe for realtime-progress updates
          const progress_handler = data => {
            frappe.show_progress(
              __('Actualizing Fiscal Status'),
              data.current,
              data.total,
              __('Processing {0} of {1}', [data.current, data.total])
            );
          };

          const done_handler = data => {
            frappe.hide_progress();
            frappe.show_alert({
              message: __('Fiscal status updated for {0} invoices.', [data.updated]),
              indicator: 'green'
            });

            frappe.realtime.off('bulk_si_fiscal_status_progress', progress_handler);
            frappe.realtime.off('bulk_si_fiscal_status_done', done_handler);

            listview.refresh();
          };

          frappe.realtime.on('bulk_si_fiscal_status_progress', progress_handler);
          frappe.realtime.on('bulk_si_fiscal_status_done', done_handler);

          // 3 start background job
          frappe.call({
            method: 'erpnext_moldova_efactura.api.fiscal_status.start_bulk_si_job',
            args: { names }
          });
        }
      );
    }
  };

  frappe.listview_settings['Sales Invoice'] = Object.assign({}, existing, custom);
})();
