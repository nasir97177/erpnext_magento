from __future__ import unicode_literals

import frappe

from erpnext_magento.erpnext_magento.exceptions import MagentoError
from erpnext_magento.erpnext_magento.utils import make_magento_log
from erpnext_magento.erpnext_magento.sync_customers import sync_magento_customer_addresses
from erpnext_magento.erpnext_magento.magento_requests import (
	get_request,
	get_magento_orders,
	get_magento_order_invoices,
	get_magento_order_shipments,
	get_magento_website_name_by_store_id,
	post_request
)
from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note, make_sales_invoice
from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
from frappe import _
from frappe.utils import flt, nowdate, cint


def sync_orders():
	magento_order_list = []
	sync_magento_orders(magento_order_list)
	frappe.local.form_dict.count_dict["erpnext_orders"] = len(magento_order_list)

	erpnext_order_list = []
	# sync_erpnext_orders(erpnext_order_list)
	frappe.local.form_dict.count_dict["magento_orders"] = len(erpnext_order_list)


def sync_magento_orders(magento_order_list):
	magento_settings = frappe.get_doc("Magento Settings", "Magento Settings")

	for magento_order in get_magento_orders():
		if magento_order.get("customer_is_guest") == 1:
			magento_order.update({"erpnext_guest_customer_name": get_erpnext_guest_customer_name(magento_order, magento_settings)})

		else:
			### Needed because of a bug in Magento. GitHub issue #5013.
			erpnext_customer_name = frappe.db.get_value("Customer", {"magento_customer_id": magento_order.get("customer_id")}, "name")
			erpnext_customer = frappe.get_doc("Customer", erpnext_customer_name)

			magento_order_addresses = []
			magento_order_addresses.append(magento_order.get("billing_address"))
			magento_order_addresses.append(magento_order.get("extension_attributes").get("shipping_assignments")[0].get("shipping").get("address"))

			sync_magento_customer_addresses(erpnext_customer, magento_order_addresses)

		try:
			if not frappe.db.get_value("Sales Order", {"magento_order_id": magento_order.get("entity_id")}, "name"):
				create_erpnext_sales_order(magento_order, magento_settings)
			
			#if cint(magento_settings.sync_delivery_note):
			#	sync_magento_shipments(magento_order, magento_settings)
			
			if cint(magento_settings.sync_sales_invoice):
				sync_magento_invoices(magento_order, magento_settings)

			magento_order_list.append(magento_order.get("entity_id"))

		except MagentoError as e:
			make_magento_log(status="Error", method="sync_magento_orders", message=frappe.get_traceback(),
				request_data=magento_order, exception=True)
	
		except Exception as e:
			if e.args and e.args[0] and e.args[0].decode("utf-8").startswith("402"):
				raise e
			else:
				make_magento_log(title=e.message, status="Error", method="sync_magento_orders", message=frappe.get_traceback(),
					request_data=magento_order, exception=True)


def get_erpnext_guest_customer_name(magento_order, magento_settings):
	erpnext_guest_customer_name = frappe.db.get_value("Customer", {"magento_customer_email": magento_order.get("customer_email")}, "name")
	erpnext_guest_customer_dict = {
		"doctype": "Customer",
		"customer_first_name": magento_order.get("customer_firstname"),
		"customer_last_name": magento_order.get("customer_lastname"),
		"customer_name": f'{magento_order.get("customer_firstname")} {magento_order.get("customer_lastname")}',
		"magento_customer_email" : magento_order.get("customer_email"),
		"customer_group": magento_settings.customer_group,
		"customer_details": "Magento Guest",
		"territory": frappe.utils.nestedset.get_root_of("Territory"),
		"customer_type": _("Individual")
	}

	try:
		if not erpnext_guest_customer_name:
			erpnext_guest_customer = frappe.get_doc(erpnext_guest_customer_dict)
			erpnext_guest_customer.flags.ignore_mandatory = True
			erpnext_guest_customer.insert()
			frappe.db.commit()

		else:
			erpnext_guest_customer = frappe.get_doc("Customer", erpnext_guest_customer_name)
			erpnext_guest_customer.update(erpnext_guest_customer_dict)
			erpnext_guest_customer.flags.ignore_mandatory = True
			erpnext_guest_customer.save()
			frappe.db.commit()

	except Exception as e:
		make_magento_log(title=e.message, status="Error", method="get_erpnext_guest_customer_name", message=frappe.get_traceback(),
			request_data=magento_order, exception=True)

	return erpnext_guest_customer.name


def create_erpnext_sales_order(magento_order, magento_settings):
	erpnext_sales_order = frappe.get_doc({
		"doctype": "Sales Order",
		"naming_series": magento_settings.sales_order_series or "SO-MAGENTO-",
		"magento_order_id": magento_order.get("entity_id"),
		"magento_order_payment_method": magento_order.get("payment").get("method"),
		"customer": magento_order.get("erpnext_guest_customer_name") or frappe.db.get_value("Customer", {"magento_customer_id": magento_order.get("customer_id")}, "name"),
		"address": get_sales_order_erpnext_address("billing", magento_order),
		"shipping_address_name": get_sales_order_erpnext_address("shipping", magento_order),
		"delivery_date": nowdate(),
		"company": magento_settings.company,
		"selling_price_list": get_price_list(magento_order, magento_settings),
		"ignore_pricing_rule": 1,
		"items": get_order_items(magento_order.get("items"), magento_settings),
		"taxes": get_order_taxes(magento_order, magento_settings),
		"apply_discount_on": "Grand Total",
		"discount_amount": magento_order.get("discount_amount") * -1
	})
	
	erpnext_sales_order.flags.ignore_mandatory = True
	erpnext_sales_order.save()
	erpnext_sales_order.submit()	
	frappe.db.commit()


def get_sales_order_erpnext_address(address_type, magento_order):
	if address_type == "billing": 
		magento_order_address = magento_order.get("billing_address")

	elif address_type == "shipping":
		magento_order_address = magento_order.get("extension_attributes").get("shipping_assignments")[0].get("shipping").get("address")

	else:
		frappe.throw(f'Address type "{address_type}" not valid for function "get_sales_order_erpnext_address".')

	if magento_order_address.get("customer_address_id"):
		return frappe.db.get_value("Address", {"magento_address_id": magento_order_address.get("customer_address_id")}, "name")

	elif frappe.db.get_value("Address", {"address_first_name": magento_order_address.get("firstname"), "address_last_name": magento_order_address.get("lastname"),
		"pincode": magento_order_address.get("postcode"), "address_line1": magento_order_address.get("street")[0]}, "name"):
		return frappe.db.get_value("Address", {"address_first_name": magento_order_address.get("firstname"), "address_last_name": magento_order_address.get("lastname"),
		"pincode": magento_order_address.get("postcode"), "address_line1": magento_order_address.get("street")[0]}, "name")

	else:
		erpnext_customer_name = magento_order.get("erpnext_guest_customer_name") or frappe.db.get_value("Customer", {"magento_customer_id": magento_order.get("customer_id")}, "name")
		erpnext_customer = frappe.get_doc("Customer", erpnext_customer_name)

		sync_magento_customer_addresses(erpnext_customer, [magento_order_address])

		return frappe.db.get_value("Address", {"address_first_name": magento_order_address.get("firstname"), "address_last_name": magento_order_address.get("lastname"),
		"pincode": magento_order_address.get("postcode"), "address_line1": magento_order_address.get("street")[0]}, "name")


def get_price_list(magento_order, magento_settings):
	for price_list in magento_settings.price_lists:
		if price_list.magento_website_name == get_magento_website_name_by_store_id(magento_order.get("store_id")):
			return price_list.price_list


def get_order_items(order_items, magento_settings):
	items = []

	for magento_item in order_items:
		if magento_item.get("product_type") != "configurable":
			items.append({
				"item_code": frappe.db.get_value("Item", {"magento_product_id": magento_item.get("product_id")}, "item_code"),
				"item_name": magento_item.get("name"),
				"magento_order_item_id": magento_item.get("parent_item_id") or magento_item.get("item_id"),
				"rate": magento_item.get("price"),
				"delivery_date": nowdate(),
				"qty": magento_item.get("qty_ordered"),
				"magento_sku": magento_item.get("sku"),
			})

	return items


def get_order_taxes(magento_order, magento_settings):
	taxes = []

	for tax in magento_order.get("extension_attributes").get("applied_taxes"):
		taxes.append({
			"charge_type": _("On Net Total"),
			"account_head": get_tax_account_head(tax),
			"description": f'{tax.get("code")} - {tax.get("percent")}%',
			"rate": tax.get("percent"),
			"included_in_print_rate": 1,
			"cost_center": magento_settings.cost_center
		})

	return taxes


def get_tax_account_head(tax):
	tax_account =  frappe.db.get_value("Magento Tax Account", {"parent": "Magento Settings", "magento_tax": tax.get("code")}, "tax_account")

	if not tax_account:
		frappe.throw(f'Tax Account not specified for Magento Tax {tax.get("code")}')

	return tax_account


def sync_magento_shipments(magento_order, magento_settings):
	erpnext_sales_order_name = frappe.db.get_value("Sales Order", {"magento_order_id": magento_order.get("entity_id")}, "name")
	erpnext_sales_order = frappe.get_doc("Sales Order", erpnext_sales_order_name)

	for shipment in get_magento_order_shipments(magento_order.get("entity_id")):
		if not frappe.db.get_value("Delivery Note", {"magento_shipment_id": shipment.get("entity_id")}, "name")	and erpnext_sales_order.docstatus == 1:
			delivery_note = make_delivery_note(erpnext_sales_order.name)
			delivery_note.magento_order_id = shipment.get("order_id")
			delivery_note.magento_shipment_id = shipment.get("entity_id")
			delivery_note.naming_series = magento_settings.delivery_note_series or "delivery_note-Magento-"
			delivery_note.items = get_magento_shipment_items(delivery_note.items, shipment.get("items"), magento_settings)
			delivery_note.flags.ignore_mandatory = True
			delivery_note.save()
			delivery_note.submit()
			frappe.db.commit()


def get_magento_shipment_items(delivery_note_items, shipment_items, magento_settings):
	return [delivery_note_item.update({"qty": item.get("qty_shipped")}) for item in shipment_items for delivery_note_item in delivery_note_items\
			if frappe.db.get_value("Item", {"magento_product_id": item.get("product_id")}, "item_code") == delivery_note_item.item_code]


def sync_magento_invoices(magento_order, magento_settings):
	erpnext_sales_order_name = frappe.db.get_value("Sales Order", {"magento_order_id": magento_order.get("entity_id")}, "name")
	erpnext_sales_order = frappe.get_doc("Sales Order", erpnext_sales_order_name)

	for invoice in get_magento_order_invoices(magento_order.get("entity_id")):
		erpnext_sales_invoice_name = frappe.db.get_value("Sales Invoice", {"magento_order_id": magento_order.get("entity_id")}, "name")
		
		if not erpnext_sales_invoice_name and erpnext_sales_order.docstatus==1:
			erpnext_sales_invoice = make_sales_invoice(erpnext_sales_order.name)
			erpnext_sales_invoice.magento_order_id = magento_order.get("entity_id")
			erpnext_sales_invoice.naming_series = magento_settings.sales_invoice_series or "erpnext_sales_invoice-Magento-"
			erpnext_sales_invoice.flags.ignore_mandatory = True
			set_cost_center(erpnext_sales_invoice.items, magento_settings.cost_center)
			erpnext_sales_invoice.save()
			frappe.db.commit()
		
		else:
			erpnext_sales_invoice = frappe.get_doc("Sales Invoice", erpnext_sales_invoice_name)

		if invoice.get("state") == 2:
			erpnext_sales_invoice.submit()
			frappe.db.commit()

			make_payament_entry_against_sales_invoice(erpnext_sales_invoice, magento_settings)


def set_cost_center(items, cost_center):
	for item in items:
		item.cost_center = cost_center


def make_payament_entry_against_sales_invoice(doc, magento_settings):
	if not doc.status == "Paid":
		payemnt_entry = get_payment_entry(doc.doctype, doc.name, bank_account=magento_settings.cash_bank_account)
		payemnt_entry.flags.ignore_mandatory = True
		payemnt_entry.reference_no = doc.name
		payemnt_entry.reference_date = nowdate()
		payemnt_entry.submit()
		frappe.db.commit()


def sync_erpnext_orders(erpnext_order_list):
	for erpnext_delivery_note in get_erpnext_delivery_notes():
		magento_shipment_dict = {
			"items": get_erpnex_delivery_note_items(erpnext_delivery_note.get("delivery_note_name")),
			"notify": 1
		}

		try:
			request_response = post_request(f'rest/V1/order/{erpnext_delivery_note.get("magento_order_id")}/ship', magento_shipment_dict)

			save_magento_properties_to_erpnext(erpnext_delivery_note, request_response)

			# set_order_as_complete_in_magento(magento_order)

			if erpnext_delivery_note.get("sales_order_name") not in erpnext_order_list:
				erpnext_order_list.append(erpnext_delivery_note.get("sales_order_name"))

		except Exception as e:
			make_magento_log(title=e.message, status="Error", method="sync_erpnext_orders", message=frappe.get_traceback(),
				request_data=magento_shipment_dict, exception=True)


def get_erpnext_delivery_notes():
	magento_settings = frappe.get_doc("Magento Settings", "Magento Settings")

	last_sync_condition = ""
	if magento_settings.last_sync_datetime:
		last_sync_condition = f"and dn.modified >= '{magento_settings.last_sync_datetime}' "

	delivery_note_querry = f"""SELECT so.name as sales_order_name, so.magento_order_id, dn.name as delivery_note_name
		FROM `tabSales Order` so, `tabDelivery Note` dn, `tabDelivery Note Item` dni
		WHERE so.magento_order_id IS NOT NULL AND so.name = dni.against_sales_order AND dn.name = dni.parent
		AND magento_shipment_id IS NULL {last_sync_condition}
		GROUP BY dn.name"""

	return frappe.db.sql(delivery_note_querry, as_dict=1)


def get_erpnex_delivery_note_items(delivery_note_name):
	delivery_note_item_querry = f"""SELECT CONVERT(magento_order_item_id, INT) as order_item_id, qty
		FROM `tabDelivery Note Item` WHERE parent = '{delivery_note_name}'""" 

	return frappe.db.sql(delivery_note_item_querry, as_dict=1)


def save_magento_properties_to_erpnext(erpnext_delivery_note, request_response):
	delivery_note = frappe.get_doc("Delivery Note", erpnext_delivery_note.get("delivery_note_name"))

	delivery_note.magento_shipment_id = request_response
	delivery_note.magento_order_id = frappe.db.get_value("Sales Order", {"name": erpnext_delivery_note.get("sales_order_name")}, "magento_order_id")
	delivery_note.save()
	frappe.db.commit()


def set_order_as_complete_in_magento(magento_order):
	magento_order_dict = {
		"entity_id": magento_order.get("entity_id"),
		"status": "complete"
	}

	post_request("rest/V1/orders", {"entity": magento_order_dict})


