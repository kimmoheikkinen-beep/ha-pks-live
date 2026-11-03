# PKS Live for Home Assistant

Unofficial Home Assistant integration for Pohjois-Karjalan Sähkö (PKS) customers. It reads data from the PKS Live customer portal (pkslive.pks.fi) and Priima Live (live.pks.fi).

## Features

- Monthly consumption and electricity bill: current month (preliminary) and previous month (final, including the hedge outcome and consumption effect).
- Priima energy price per month, delivery fee and monthly fee.
- Price hedges: current hedge price and coverage, the list of active hedges, and the next unhedged quarter.
- Current-month consumption effect and energy price estimate, using PKS's published Priima formula:
  - consumption effect = own consumption-weighted average spot price − average spot price
  - energy price = hedge share × (hedge price + consumption effect) + (1 − hedge share) × own average price
  - Spot prices come from the public sahkotin.fi API (Nord Pool FI, VAT 25.5 %).
- **Ostosähkön rajahinta nyt** (marginal purchase price): the real energy cost of one extra purchased kWh this hour, i.e. spot + hedge share × (hedge price − monthly average spot) + delivery fee. Transfer fees and electricity tax are not included. The `tunnit` attribute lists upcoming hours for battery and load control.
- Sold solar electricity and revenue (Aurinkotuottaja contract).
- Hourly purchased and sold electricity imported into long-term statistics (`pks_live:kulutus`, `pks_live:myynti`), which can be added to the Energy dashboard. Metering data arrives about one day late.

## Installation

1. HACS → Custom repositories → add `https://github.com/kimmoheikkinen/ha-pks-live` (type: Integration) and install.
2. Restart Home Assistant.
3. Settings → Devices & services → Add integration → **PKS Live**, then sign in with your PKS Live email and password.

The integration is unofficial and uses the same interfaces as the web portal, so changes to the portal can break it.
