"""Synchronous client for PKS Live (GraphQL) and Priima Live (REST)."""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from .const import CLIENT_ID, GRAPHQL_URL, PRIIMA_LIVE_API, PRIIMA_LIVE_LOGIN, TENANT, USER_POOL

TZ = ZoneInfo("Europe/Helsinki")

Q_CONTRACTS = """
query Contracts($tenantId: ID!, $customerId: ID!) {
  contracts(tenantId: $tenantId, customerId: $customerId) {
    salesContracts(tenantId: $tenantId) {
      identifier contractType contractStatusCode productName fixedUntilDate meteringpointIdentifier
      contractPeriods { prices { salesProductComponentType priceWithVat unit start end } }
    }
  }
}"""

Q_COSTS = """
query Costs($tenantId: ID!, $customerId: ID!, $meteringPointId: ID!, $contractType: ContractTypeEnum!,
            $startDate: String!, $endDate: String!, $resolution: ResolutionDuration!) {
  consumption {
    costsByPricePerMeteringPoint(tenantId: $tenantId, customerId: $customerId, meteringPointId: $meteringPointId,
        contractType: $contractType, startDate: $startDate, endDate: $endDate, resolution: $resolution,
        includeExternalPrices: true) {
      periods { consumption costsByPrice { price { productComponentType priceWithVat unit } costWithVat } }
    }
  }
}"""

Q_RANGE = """
query Range($tenantId: ID!, $customerId: ID!, $meteringPointId: ID!, $contractType: ContractTypeEnum!,
            $measurementType: MeasurementType!, $period: ConsumptionRangePeriod!, $periodStart: String!, $periodEnd: String!) {
  consumption {
    range(tenantId: $tenantId, productIdentifier: "", customerId: $customerId, meteringPointId: $meteringPointId,
          contractType: $contractType, measurementType: $measurementType, period: $period,
          periodStart: $periodStart, periodEnd: $periodEnd, useOnlyDailySums: true, includeExternalPrices: true) {
      items { startTime sum status }
      lastAvailable
    }
  }
}"""

Q_MAGIC = """
query Magic($tenantId: String!, $baseUrl: String!) {
  auth { externalMagicLink(tenantId: $tenantId, baseUrl: $baseUrl) { url } }
}"""


SPOT_URL = "https://sahkotin.fi/prices"
VAT = 1.255


class PksAuthError(Exception):
    """Login failed."""


class PksApiError(Exception):
    """API call failed."""


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _r(v):
    return round(v, 3) if v is not None else None


def month_start(dt: datetime, delta: int = 0) -> datetime:
    loc = dt.astimezone(TZ)
    y, m = loc.year, loc.month + delta
    while m < 1:
        y, m = y - 1, m + 12
    while m > 12:
        y, m = y + 1, m - 12
    return datetime(y, m, 1, tzinfo=TZ)


class PksClient:
    def __init__(self, username: str, password: str) -> None:
        self.username = username
        self.password = password
        self.token: str | None = None
        self.customer_id: str | None = None

    def login(self) -> None:
        from pycognito import Cognito

        try:
            cog = Cognito(USER_POOL, CLIENT_ID, username=self.username)
            cog.authenticate(password=self.password)
        except Exception as err:
            name = type(err).__name__
            if "NotAuthorized" in name or "UserNotFound" in name or "NotAuthorized" in str(err):
                raise PksAuthError(str(err)) from err
            raise PksApiError(f"{name}: {err}") from err
        self.token = cog.id_token
        payload = self.token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        ids = json.loads(claims.get("custom:customerIds") or "{}")
        for tenant in ids.get("tenants", []):
            if tenant.get("tenantId") == TENANT and tenant.get("customerIds"):
                self.customer_id = tenant["customerIds"][0]
                return
        raise PksApiError("PKS-asiakasnumeroa ei löytynyt tunnukselta")

    def gql(self, query: str, **variables):
        if not self.token:
            self.login()
        resp = requests.post(GRAPHQL_URL, json={"query": query, "variables": variables},
                             headers={"Authorization": self.token}, timeout=60)
        if resp.status_code in (401, 403):
            raise PksAuthError(f"HTTP {resp.status_code}")
        resp.raise_for_status()
        body = resp.json()
        if body.get("errors"):
            msg = "; ".join(e.get("message", "?") for e in body["errors"])
            if any((e.get("extensions") or {}).get("code") == "UNAUTHENTICATED" for e in body["errors"]):
                raise PksAuthError(msg)
            raise PksApiError(msg)
        return body["data"]

    def contracts(self) -> list[dict]:
        data = self.gql(Q_CONTRACTS, tenantId=TENANT, customerId=self.customer_id)
        return (data.get("contracts") or {}).get("salesContracts") or []

    def costs(self, mp: str, ctype: str, start: datetime, end: datetime) -> dict:
        data = self.gql(Q_COSTS, tenantId=TENANT, customerId=self.customer_id, meteringPointId=mp, contractType=ctype,
                        startDate=_iso(start), endDate=_iso(end), resolution="P1MT")
        periods = ((data.get("consumption") or {}).get("costsByPricePerMeteringPoint") or {}).get("periods") or []
        kwh, parts = 0.0, {}
        for p in periods:
            kwh += p.get("consumption") or 0
            for c in p.get("costsByPrice") or []:
                key = c["price"]["productComponentType"]
                parts[key] = parts.get(key, 0.0) + (c.get("costWithVat") or 0)
        return {"kwh": round(kwh, 3), "eur": round(sum(parts.values()), 2),
                "parts": {k: round(v, 2) for k, v in parts.items()}}

    def hourly(self, mp: str, ctype: str, start: datetime, end: datetime) -> list[tuple[datetime, float]]:
        out: list[tuple[datetime, float]] = []
        cur = start
        while cur < end:
            nxt = min(cur + timedelta(days=7), end)
            data = self.gql(Q_RANGE, tenantId=TENANT, customerId=self.customer_id, meteringPointId=mp, contractType=ctype,
                            measurementType="ActivePower", period="Day", periodStart=_iso(cur), periodEnd=_iso(nxt))
            for it in ((data.get("consumption") or {}).get("range") or {}).get("items") or []:
                if it.get("status") is None:
                    continue
                ts = _parse(it["startTime"])
                if start <= ts < end:
                    out.append((ts, float(it.get("sum") or 0)))
            cur = nxt
        dedup = dict(out)
        return sorted(dedup.items())

    def spot(self, start: datetime, end: datetime) -> dict[datetime, float]:
        """Hourly Nord Pool FI prices in c/kWh incl. VAT, keyed by UTC hour."""
        r = requests.get(SPOT_URL, params={"start": _iso(start).replace("Z", ".000Z"), "end": _iso(end).replace("Z", ".000Z")},
                         timeout=60)
        r.raise_for_status()
        return {_parse(x["date"]): x["value"] / 10 * VAT for x in r.json().get("prices") or [] if x.get("value") is not None}

    def hedges(self) -> list[dict]:
        url = self.gql(Q_MAGIC, tenantId=TENANT, baseUrl=PRIIMA_LIVE_LOGIN)["auth"]["externalMagicLink"]["url"]
        with requests.Session() as s:
            s.get(url, timeout=60).raise_for_status()
            r = s.get(PRIIMA_LIVE_API + "Customer/Transactions/", timeout=60,
                      headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"})
            r.raise_for_status()
            return r.json()

    def fetch(self) -> dict:
        self.login()
        now = datetime.now(timezone.utc)
        cur_m, prev_m, next_m = month_start(now), month_start(now, -1), month_start(now, 1)
        contracts = self.contracts()

        def pick(ctype):
            cands = [c for c in contracts if c.get("contractType") == ctype]
            act = [c for c in cands if c.get("contractStatusCode") == "A"]
            return (act or cands or [None])[0]

        sales, prod = pick("Sales"), pick("SalesProduction")
        if not sales:
            raise PksApiError("Sähkön myyntisopimusta ei löytynyt")
        data: dict = {"customer_id": self.customer_id, "product": sales.get("productName"),
                      "fixed_until": sales.get("fixedUntilDate"), "sales_mp": sales["meteringpointIdentifier"],
                      "production_mp": prod["meteringpointIdentifier"] if prod else None}

        prices = [p for per in sales.get("contractPeriods") or [] for p in per.get("prices") or []]

        def price_at(kind, when):
            for p in prices:
                if p["salesProductComponentType"] == kind and _parse(p["start"]) <= when < _parse(p["end"]):
                    return p["priceWithVat"]
            return None

        data["margin"] = price_at("EnergyPriceMargin", now)
        data["monthly_fee"] = price_at("MonthlyBasicPrice", now)
        data["energy_price_prev"] = price_at("EnergyPrice", prev_m + timedelta(days=1))
        data["energy_price_cur"] = price_at("EnergyPrice", cur_m + timedelta(days=1))
        data["energy_prices"] = {
            _parse(p["start"]).astimezone(TZ).strftime("%Y-%m"): p["priceWithVat"]
            for p in prices if p["salesProductComponentType"] == "EnergyPrice"
        }

        data["cur"] = self.costs(sales["meteringpointIdentifier"], "Sales", cur_m, next_m)
        data["prev"] = self.costs(sales["meteringpointIdentifier"], "Sales", prev_m, cur_m)
        if prod:
            data["prod_cur"] = self.costs(prod["meteringpointIdentifier"], "SalesProduction", cur_m, next_m)
            data["prod_prev"] = self.costs(prod["meteringpointIdentifier"], "SalesProduction", prev_m, cur_m)

        hedges = self.hedges()
        rows = []
        for h in hedges:
            rows.append({"jakso": h.get("PeriodName"), "tuote": h.get("InstrumentName"),
                         "alku": _parse(h["PeriodStart"]).astimezone(TZ).date().isoformat(),
                         "loppu": _parse(h["PeriodStop"]).astimezone(TZ).date().isoformat(),
                         "osuus": h.get("Percentage") or 0, "hinta": h.get("PriceWithVat"),
                         "tehty": h.get("Time", "")[:10],
                         "_s": _parse(h["PeriodStart"]), "_e": _parse(h["PeriodStop"])})
        rows.sort(key=lambda r: r["_s"])

        def coverage(when):
            act = [r for r in rows if r["_s"] <= when < r["_e"]]
            pct = sum(r["osuus"] for r in act)
            avg = sum(r["osuus"] * (r["hinta"] or 0) for r in act) / pct if pct else None
            return pct, avg, act

        pct, avg, act = coverage(now)
        data["hedge_pct"] = pct
        data["hedge_price"] = round(avg, 3) if avg is not None else None
        data["hedge_period"] = act[0]["jakso"] if act else None
        gap = None
        q = datetime(now.astimezone(TZ).year, 3 * ((now.astimezone(TZ).month - 1) // 3) + 1, 1, tzinfo=TZ)
        for _ in range(12):
            p, _a, _x = coverage(q + timedelta(days=1))
            if p < 100:
                gap = {"alku": q.date().isoformat(), "osuus": p, "nimi": f"Q{(q.month - 1) // 3 + 1}-{q.year % 100:02d}"}
                break
            q = month_start(q, 3)
        data["hedge_gap"] = gap
        data["hedges"] = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows if r["_e"] > now]
        data["month"] = self._month_estimate(data["sales_mp"], cur_m, next_m, now, pct, avg, data["margin"] or 0)
        return data

    def _month_estimate(self, mp, cur_m, next_m, now, pct, hedge, margin) -> dict:
        """PKS Priima: kulutusvaikutus = oma keskihinta - spot-keskiarvo;
        energian hinta = p * (kiinnityshinta + kulutusvaikutus) + (1 - p) * oma keskihinta."""
        spot = self.spot(cur_m, now + timedelta(days=2))
        month_spot = [v for t, v in spot.items() if cur_m <= t < next_m]
        spot_avg = sum(month_spot) / len(month_spot) if month_spot else None
        share = min(pct, 100) / 100
        out: dict = {"spot_avg": _r(spot_avg), "spot_hours": len(month_spot), "hedge_share": share}
        rows = [(t, k, spot[t]) for t, k in self.hourly(mp, "Sales", cur_m, now) if t in spot]
        kwh = sum(k for _, k, _ in rows)
        if kwh > 0:
            own = sum(k * v for _, k, v in rows) / kwh
            spot_same = sum(v for _, _, v in rows) / len(rows)
            kv = own - spot_same
            energy = share * ((hedge or 0) + kv) + (1 - share) * own
            out.update(own_avg=_r(own), spot_avg_metered=_r(spot_same), consumption_effect=_r(kv),
                       energy_price=_r(energy), total_price=_r(energy + margin), metered_kwh=round(kwh, 1),
                       metered_hours=len(rows), metered_until=_iso(rows[-1][0]))
        if spot_avg is not None:
            offset = share * ((hedge or 0) - spot_avg) + margin
            out["marginal_offset"] = _r(offset)
            out["marginal"] = {_iso(t): _r(v + offset) for t, v in sorted(spot.items()) if t >= now - timedelta(hours=1)}
        return out
