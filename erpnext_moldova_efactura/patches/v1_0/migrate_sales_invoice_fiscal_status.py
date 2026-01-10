import frappe


def execute():
    from erpnext_moldova_efactura.utils.fiscal_status import determine_fiscal_status

    frappe.logger().info("[eFactura] Migration: start Sales Invoice fiscal_status")

    # Берём ТОЛЬКО submitted SI
    si_names = frappe.get_all(
        "Sales Invoice",
        filters={"docstatus": 1},
        pluck="name",
    )

    total = len(si_names)
    updated = 0
    skipped = 0

    for name in si_names:
        try:
            si = frappe.get_doc("Sales Invoice", name)

            new_status = determine_fiscal_status(si)

            # Ничего не делаем, если статус пустой
            if not new_status:
                skipped += 1
                continue

            # Не обновляем, если статус уже совпадает
            if si.get("fiscal_status") == new_status:
                skipped += 1
                continue

            si.db_set("fiscal_status", new_status, update_modified=False)
            updated += 1

        except frappe.ValidationError:
            # Например: не настроена Fiscal Territory
            skipped += 1
            continue

        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"eFactura migration failed for Sales Invoice {name}",
            )
            skipped += 1
            continue

    frappe.logger().info(
        f"[eFactura] Migration completed: total={total}, updated={updated}, skipped={skipped}"
    )