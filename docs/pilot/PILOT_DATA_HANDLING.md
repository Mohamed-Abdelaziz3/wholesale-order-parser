# Pilot Data Handling

## Purpose and scope

This note describes the actual data flow for the single-merchant assisted
pilot. It is an operational disclosure, not a legal opinion, a privacy policy,
or a claim of regulatory compliance.

The deployment is intentionally isolated: one merchant uses one Railway
service, one SQLite database, and one persistent volume. It is not a
multi-merchant service. Every authenticated operator of that deployment can
access that merchant's operational data.

## What is sent to Gemini

The operator manually copies a customer order from WhatsApp and pastes it into
the application. There is no WhatsApp Business API connection, webhook, or
automatic chat import.

When the operator chooses **Process**, the application sends the pasted order
message, together with an extraction instruction, to Google Gemini. The
extractor trims the message and limits the provider submission to the first
6,000 characters. The local API may accept up to 20,000 characters. For a
successfully saved order, text beyond that provider limit remains local and is
retained with the order.

The Gemini request does **not** include the merchant catalog, catalog prices,
shop settings, database contents, session cookie, application password, or
other orders. Gemini returns only an advisory extraction. Product identity,
SKU, commercial price, and final approval are determined locally from the
catalog and an explicit human review; Gemini is never commercial authority.

The application makes no claim that free-form WhatsApp text is automatically
redacted before it reaches Gemini. Egyptian order messages can mix product
descriptions with names, phone numbers, addresses, and delivery instructions;
an unreliable regular-expression filter could both miss personal data and
damage the extraction. Operators must therefore paste only the information
needed for the pilot order and should remove unnecessary personal information
before submission where practical.

Google's handling of data sent to Gemini is governed by the merchant's and
operator's applicable Google service terms and configuration. This application
does not make a separate retention or deletion promise on Google's behalf.

## What stays in the deployment

The persistent SQLite database can contain:

- The complete pasted order message and the raw/extracted line fragments.
- Extracted quantities and units, deterministic catalog-match candidates, and
  any text the extractor marks unresolved.
- Customer name, phone number, and address only when an operator enters them.
- Human review decisions, SKU corrections, price overrides, discounts,
  approvals, immutable approved snapshots, export events, and audit events.
- The merchant catalog, catalog version metadata, shop settings, and operator
  identifiers recorded in the audit trail.
- Request idempotency identifiers and payload fingerprints used to make a
  retry of the same processing request safe. These do not replace the order
  record, which retains the original pasted message.

Catalog-recovery backups are operational recovery artifacts. They contain only
catalog rows and recovery metadata; they do not contain order history or the
customer data held in the database. They do not replace, delete, or roll back
order history. Their bounded retention is not a general personal-data retention
policy.

Any separate SQLite-file, Railway-volume, or full-database backup made by an
operator can contain all database-held data listed above, including pasted
messages and customer contact details. Such copies need their own access,
retention, and deletion process.

## What is exported

Exports are available only after human approval and use the immutable approved
snapshot, rather than the live catalog.

- The CSV and XLSX operational exports include the approved product lines,
  quantities, units, prices, totals, and any entered customer name, phone, and
  address. They also include the pasted customer message in their header area.
- The HTML dispatch/picking document includes the approved lines and entered
  customer details. It is an operational document, not a tax invoice.

Once a file is downloaded, printed, emailed, or forwarded, it can exist on
devices and systems outside this deployment. The application cannot recall or
delete those copies.

## Deletion and retention limitations

There is no self-service data-subject deletion workflow, scheduled order-data
retention job, or complete backup purge workflow in this pilot. Approved
snapshots and audit evidence are intentionally preserved for operational
traceability. Do not promise immediate or comprehensive deletion to a customer
without an operator-led review of the database, generated exports, and any
applicable backup artifacts.

For the pilot, the safest practice is data minimisation: do not paste personal
data that is not needed to identify, review, dispatch, or support the specific
test order.

## Operator rules

- Use the isolated deployment only for its assigned merchant.
- Do not paste payment credentials, national IDs, card data, passwords, or
  unrelated customer conversations.
- Restrict application and Railway access to authorised pilot operators.
- Do not place production secrets or customer data in source control, issue
  trackers, screenshots, or support messages.
- Use the Arabic merchant disclosure in
  `docs/pilot/MERCHANT_PILOT_DISCLOSURE_AR.md` before the pilot begins.
