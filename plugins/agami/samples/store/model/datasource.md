# About this database

<!-- This is the human narrative for the model — what the business is, who the
     users are, and what the key terms mean. agami reads it as domain context on
     every question, alongside the schema-derived summary. Edit it to say what
     only you would know. -->

**Acme Store** is a small synthetic online retailer, shipped with agami as a
no-setup sample so you can see the agent work without connecting a real
database. Everything here is fabricated — generic product names, `@example.com`
customers — and lives in a local SQLite file; nothing leaves your machine.

The business has two sides, modelled as one subject area:

- **Commerce** — one-time purchases. Customers browse `products` (grouped into
  `categories`), place `orders` made up of `order_items`, and pay via
  `payments`. Returns are tracked in `refunds`.
- **Subscriptions** — recurring revenue. Customers sign up for `plans`, which
  creates a `subscription`; its lifecycle is logged in `subscription_events`,
  and each billing cycle issues an `invoice`. A single customer can have both
  one-time orders and an active subscription.

## What you can ask

Sales and revenue, top customers, product and category performance, refund
rates, order-status and channel breakdowns, month-over-month trends, and
subscription health (active subscriptions, MRR, paid-invoice revenue).

## Key terms

- **Revenue** is measured at the **order grain**: sum `orders.total_amount`, the
  pre-computed total for each order. Do **not** sum it across a join to
  `order_items` (line-item grain) or `payments` — that double-counts. agami's
  pre-flight catches this fan trap; the *"top customers by spend"* demo is built
  to show it.
- **MRR** (monthly recurring revenue) is the monthly price of all subscriptions
  currently in the `active` state — not annualised, not including one-time
  orders.
- An **active subscription** is one whose current state is `active` (a sign-up
  that hasn't churned or expired).
- **Refunds** are stored as positive amounts in `refunds`; net revenue is order
  revenue minus refunds.

## A note on privacy

Customer `email` and `phone` are flagged **sensitive**: agami will use them to
count, filter, and join, but won't print raw values in a result. Use
`customer_id` (or the non-sensitive `full_name` label) to identify a customer in
output.
