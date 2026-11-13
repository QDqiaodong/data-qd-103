# Source and license

All bundled data is original synthetic demonstration data for fictional entities.
It is not real customer information, official measurements, live service history,
or copied third-party content. It is distributed under this repository's MIT
license. No third-party source code is vendored; installed dependencies retain
their own licenses. No paid service, real account or external dataset is required.

The `data/consensus/` files are likewise original synthetic data written only to
exercise the v2 multi-logger consensus policy. Sensor identifiers (A..V) are
fictional labels, temperatures and uncertainty half-widths are hand-picked
values, and timestamps are arbitrary whole UTC seconds; they do not represent
any real shipment, device or measurement.

The `examples/handoff/` files (source trips/assignments/readings and the
hand-off CSV) are original synthetic data for the hand-off fulfillment
marketplace. Trip identifiers (HA/HB/HC), hand-off identifiers (H1..H4) and
carrier names (Northwind, Polar, Acme) are fictional; the readings, uncertainty
half-widths, custody windows and UTC-midnight crossings are hand-picked to
exercise attribution across carrier changes, unassigned holes, a fully unknown
trip and a multi-component trip. They describe no real shipment, carrier,
device or liability decision, and the marketplace output is fulfillment
evidence only, never a compensation or liability ruling.
