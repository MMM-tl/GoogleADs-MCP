# Google Ads MCP Server on Azure

A remote MCP (Model Context Protocol) server that lets Claude query and analyze your Google Ads data. Deployed to **Azure Container Apps** via **GitHub Actions** — push to `main` and it ships.

## Architecture

```
GitHub (push to main)
   └─> GitHub Actions (OIDC login, no stored Azure passwords)
         └─> az acr build  ──> Azure Container Registry
         └─> az containerapp update ──> Azure Container Apps
                                            │
Claude (claude.ai connector) ── HTTPS ──> /mcp endpoint
                                            │
                              Managed Identity ──> Key Vault (secrets)
                                            │
                                     Google Ads API
```

## Tools exposed to Claude

| Tool | What it does |
|---|---|
| `run_gaql` | Run any GAQL query — the workhorse for custom analysis |
| `campaign_performance` | Clicks, cost, CTR, conversions per campaign |
| `search_terms_report` | Actual user queries with spend — find wasted budget |
| `keyword_performance` | Keyword metrics incl. quality score |
| `budget_pacing` | Month-to-date spend vs. daily budgets |
| `list_accessible_accounts` | Discover customer IDs |

All tools are **read-only**. No mutations.

## Setup

### 1. Google Ads credentials (one-time)

You need: a **developer token** (Google Ads UI → Tools → API Center), an **OAuth client ID/secret** (Google Cloud Console → Credentials → OAuth client, type "Web application" or "Desktop"), and a **refresh token** (generate once with Google's `generate_user_credentials.py` example or the OAuth playground).

### 2. Azure infrastructure (one-time)

Edit the variables at the top of `setup-azure.sh` (your GitHub username/repo, region), then:

```bash
az login
bash setup-azure.sh
```

This creates the resource group, ACR, Key Vault, Container Apps environment, the app itself, and a federated identity so GitHub Actions can deploy **without any stored Azure credentials**. It prints the exact GitHub secrets/variables to set.

Then load your Google Ads secrets into Key Vault (commands are printed by the script).

### 3. GitHub repo config (one-time)

In your repo → Settings → Secrets and variables → Actions, add the three **secrets** (`AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`) and three **variables** (`AZURE_RESOURCE_GROUP`, `CONTAINER_APP_NAME`, `ACR_NAME`) that the setup script printed.

### 4. Deploy

```bash
git init && git add . && git commit -m "initial"
git remote add origin git@github.com:YOU/google-ads-mcp.git
git push -u origin main
```

The workflow builds the image in ACR and rolls out the Container App. The app URL is printed at the end of the workflow run.

### 5. Connect to Claude

In claude.ai: **Settings → Connectors → Add custom connector**, URL:

```
https://<your-app-fqdn>/mcp
```

The server requires a bearer token (`mcp-auth-token` secret you set in Key Vault). For the most current details on adding custom connectors and authentication options, see https://support.claude.com.

Then just ask Claude things like *"pull last 30 days of campaign performance and tell me where I'm wasting spend"* or *"find search terms with cost over $50 and zero conversions."*

## Local development

```bash
pip install -r requirements.txt
export GOOGLE_ADS_DEVELOPER_TOKEN=... \
       GOOGLE_ADS_CLIENT_ID=... \
       GOOGLE_ADS_CLIENT_SECRET=... \
       GOOGLE_ADS_REFRESH_TOKEN=... \
       GOOGLE_ADS_USE_PROTO_PLUS=true
python server.py   # serves http://localhost:8000/mcp
```

Leave `MCP_AUTH_TOKEN` unset locally to skip auth.

## Notes

- **Costs come back in dollars**, not micros — conversion happens server-side.
- **Row caps** (500) keep responses inside Claude's context comfortably; ask Claude to paginate with `LIMIT`/`OFFSET` in GAQL for big pulls.
- **Manager (MCC) accounts:** set `GOOGLE_ADS_LOGIN_CUSTOMER_ID` env var on the Container App to your MCC ID.
- **API version:** pinned to `v19` in `server.py` — bump it as Google releases/retires versions.
- **Scale-to-zero** is on (`min-replicas 0`); first request after idle takes a few seconds to cold-start. Set min to 1 if that bothers you.
