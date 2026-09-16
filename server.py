"""
Google Ads MCP Server
Remote MCP server (Streamable HTTP) exposing Google Ads reporting tools.
Designed for Azure Container Apps, deployed via GitHub Actions.
"""

import csv
import io
import os

from mcp.server.fastmcp import FastMCP
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route

# ---------------------------------------------------------------------------
# Auth middleware: simple bearer token check.
# Set MCP_AUTH_TOKEN in the Container App (sourced from Key Vault).
# ---------------------------------------------------------------------------

AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")


class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.url.path == "/health":
            return await call_next(request)
        if AUTH_TOKEN:
            auth = request.headers.get("authorization", "")
            if auth != f"Bearer {AUTH_TOKEN}":
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


mcp = FastMCP(
    "google-ads",
    stateless_http=True,
    host="0.0.0.0",
    port=int(os.environ.get("PORT", "8000")),
)

# ---------------------------------------------------------------------------
# Google Ads client — loaded lazily so the container starts even if
# credentials are misconfigured (health checks still pass, tools report
# a clear error instead of the app crash-looping).
# ---------------------------------------------------------------------------

_client = None


def get_client():
    global _client
    if _client is None:
        from google.ads.googleads.client import GoogleAdsClient

        # Reads GOOGLE_ADS_DEVELOPER_TOKEN, GOOGLE_ADS_CLIENT_ID,
        # GOOGLE_ADS_CLIENT_SECRET, GOOGLE_ADS_REFRESH_TOKEN,
        # GOOGLE_ADS_LOGIN_CUSTOMER_ID (optional) from env vars.
        _client = GoogleAdsClient.load_from_env()
    return _client


DEFAULT_CUSTOMER_ID = os.environ.get("GOOGLE_ADS_CUSTOMER_ID", "")

MAX_ROWS = 500  # keep responses compact for the model's context window


def _resolve_customer_id(customer_id: str | None) -> str:
    cid = (customer_id or DEFAULT_CUSTOMER_ID).replace("-", "").strip()
    if not cid:
        raise ValueError(
            "No customer_id provided and GOOGLE_ADS_CUSTOMER_ID is not set."
        )
    return cid


def _micros(value) -> float:
    return round(int(value) / 1_000_000, 2)


def _rows_to_csv(header: list[str], rows: list[list]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue()


def _run_search(customer_id: str, query: str):
    client = get_client()
    ga_service = client.get_service("GoogleAdsService")
    return ga_service.search(customer_id=customer_id, query=query)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_accessible_accounts() -> str:
    """List all Google Ads accounts accessible with the configured credentials.
    Returns customer IDs you can pass to the other tools."""
    client = get_client()
    svc = client.get_service("CustomerService")
    resp = svc.list_accessible_customers()
    ids = [r.split("/")[-1] for r in resp.resource_names]
    return "Accessible customer IDs:\n" + "\n".join(ids)


@mcp.tool()
def run_gaql(query: str, customer_id: str = "") -> str:
    """Run an arbitrary GAQL (Google Ads Query Language) SELECT query and
    return the results as CSV. Use this for any custom analysis. Cost fields
    ending in _micros are returned raw; divide by 1,000,000 for currency.
    Results are capped at 500 rows — use ORDER BY and LIMIT for large tables.
    """
    cid = _resolve_customer_id(customer_id)
    response = _run_search(cid, query)

    rows, header = [], None
    for i, row in enumerate(response):
        if i >= MAX_ROWS:
            break
        flat = _flatten_row(row)
        if header is None:
            header = list(flat.keys())
        rows.append([flat.get(h, "") for h in header])

    if not rows:
        return "Query returned no rows."
    note = f"\n({MAX_ROWS} row cap reached)" if len(rows) == MAX_ROWS else ""
    return _rows_to_csv(header, rows) + note


def _flatten_row(row) -> dict:
    """Flatten a GoogleAdsRow (proto-plus) into a flat dict of populated fields."""
    d = type(row).to_dict(row, preserving_proto_field_name=True)
    out: dict = {}
    _flatten_dict(d, "", out)
    return out


def _flatten_dict(d: dict, prefix: str, out: dict):
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            _flatten_dict(v, key, out)
        elif isinstance(v, list):
            out[key] = "; ".join(str(x) for x in v)
        else:
            out[key] = v


@mcp.tool()
def campaign_performance(
    customer_id: str = "", date_range: str = "LAST_30_DAYS"
) -> str:
    """Campaign-level performance: clicks, impressions, CTR, cost, conversions,
    cost-per-conversion. date_range accepts GAQL ranges like LAST_7_DAYS,
    LAST_30_DAYS, THIS_MONTH, LAST_MONTH."""
    cid = _resolve_customer_id(customer_id)
    query = f"""
        SELECT
          campaign.name,
          campaign.status,
          campaign.advertising_channel_type,
          metrics.impressions,
          metrics.clicks,
          metrics.ctr,
          metrics.average_cpc,
          metrics.cost_micros,
          metrics.conversions,
          metrics.cost_per_conversion
        FROM campaign
        WHERE segments.date DURING {date_range}
          AND campaign.status != 'REMOVED'
        ORDER BY metrics.cost_micros DESC
    """
    response = _run_search(cid, query)
    header = [
        "campaign", "status", "channel", "impressions", "clicks", "ctr",
        "avg_cpc", "cost", "conversions", "cost_per_conversion",
    ]
    rows = []
    for row in response:
        m = row.metrics
        rows.append([
            row.campaign.name,
            row.campaign.status.name,
            row.campaign.advertising_channel_type.name,
            m.impressions,
            m.clicks,
            round(m.ctr, 4),
            _micros(m.average_cpc),
            _micros(m.cost_micros),
            round(m.conversions, 2),
            _micros(m.cost_per_conversion),
        ])
    return _rows_to_csv(header, rows) if rows else "No campaign data found."


@mcp.tool()
def search_terms_report(
    customer_id: str = "",
    date_range: str = "LAST_30_DAYS",
    min_cost: float = 0.0,
) -> str:
    """Search terms report: what users actually typed, with cost and
    conversions. Great for finding wasted spend (high cost, zero conversions).
    min_cost filters out terms below that spend (in account currency)."""
    cid = _resolve_customer_id(customer_id)
    query = f"""
        SELECT
          search_term_view.search_term,
          campaign.name,
          metrics.impressions,
          metrics.clicks,
          metrics.cost_micros,
          metrics.conversions
        FROM search_term_view
        WHERE segments.date DURING {date_range}
        ORDER BY metrics.cost_micros DESC
        LIMIT {MAX_ROWS}
    """
    response = _run_search(cid, query)
    header = ["search_term", "campaign", "impressions", "clicks", "cost", "conversions"]
    rows = []
    for row in response:
        cost = _micros(row.metrics.cost_micros)
        if cost < min_cost:
            continue
        rows.append([
            row.search_term_view.search_term,
            row.campaign.name,
            row.metrics.impressions,
            row.metrics.clicks,
            cost,
            round(row.metrics.conversions, 2),
        ])
    return _rows_to_csv(header, rows) if rows else "No search term data found."


@mcp.tool()
def keyword_performance(
    customer_id: str = "", date_range: str = "LAST_30_DAYS"
) -> str:
    """Keyword-level performance including quality score, cost, conversions.
    Useful for bid/QS audits."""
    cid = _resolve_customer_id(customer_id)
    query = f"""
        SELECT
          ad_group_criterion.keyword.text,
          ad_group_criterion.keyword.match_type,
          ad_group_criterion.quality_info.quality_score,
          campaign.name,
          ad_group.name,
          metrics.impressions,
          metrics.clicks,
          metrics.cost_micros,
          metrics.conversions
        FROM keyword_view
        WHERE segments.date DURING {date_range}
          AND ad_group_criterion.status != 'REMOVED'
        ORDER BY metrics.cost_micros DESC
        LIMIT {MAX_ROWS}
    """
    response = _run_search(cid, query)
    header = [
        "keyword", "match_type", "quality_score", "campaign", "ad_group",
        "impressions", "clicks", "cost", "conversions",
    ]
    rows = []
    for row in response:
        rows.append([
            row.ad_group_criterion.keyword.text,
            row.ad_group_criterion.keyword.match_type.name,
            row.ad_group_criterion.quality_info.quality_score or "",
            row.campaign.name,
            row.ad_group.name,
            row.metrics.impressions,
            row.metrics.clicks,
            _micros(row.metrics.cost_micros),
            round(row.metrics.conversions, 2),
        ])
    return _rows_to_csv(header, rows) if rows else "No keyword data found."


@mcp.tool()
def budget_pacing(customer_id: str = "") -> str:
    """Month-to-date spend per campaign vs. daily budget — spot campaigns
    that are over- or under-pacing."""
    cid = _resolve_customer_id(customer_id)
    query = """
        SELECT
          campaign.name,
          campaign_budget.amount_micros,
          metrics.cost_micros
        FROM campaign
        WHERE segments.date DURING THIS_MONTH
          AND campaign.status = 'ENABLED'
        ORDER BY metrics.cost_micros DESC
    """
    response = _run_search(cid, query)
    header = ["campaign", "daily_budget", "mtd_spend"]
    rows = []
    for row in response:
        rows.append([
            row.campaign.name,
            _micros(row.campaign_budget.amount_micros),
            _micros(row.metrics.cost_micros),
        ])
    return _rows_to_csv(header, rows) if rows else "No enabled campaigns found."


# ---------------------------------------------------------------------------
# App entrypoint
# ---------------------------------------------------------------------------


async def health(request):
    return JSONResponse({"status": "ok"})


# Build the Starlette app, register /health, then attach auth middleware
app = mcp.streamable_http_app()
app.router.routes.insert(0, Route("/health", health, methods=["GET"]))
app.user_middleware.insert(0, Middleware(BearerAuthMiddleware))
app.middleware_stack = app.build_middleware_stack()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
