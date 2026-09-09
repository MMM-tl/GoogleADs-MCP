#!/usr/bin/env bash
# ============================================================
# One-time Azure setup for the Google Ads MCP server.
# Creates: resource group, ACR, Key Vault, Container Apps env,
# the Container App itself, and GitHub OIDC federated identity.
#
# Edit the variables below, then run:  bash setup-azure.sh
# Requires: az CLI, logged in (az login), and GitHub repo created.
# ============================================================
set -euo pipefail

# ---- EDIT THESE ----
LOCATION="eastus2"
RG="google-ads-mcp-rg"
ACR="gadsmcpacr$RANDOM"            # must be globally unique, alphanumeric
KEYVAULT="gads-mcp-kv-$RANDOM"     # must be globally unique
ENV_NAME="gads-mcp-env"
APP_NAME="google-ads-mcp"
GITHUB_ORG="YOUR_GITHUB_USERNAME"
GITHUB_REPO="google-ads-mcp"
# --------------------

SUBSCRIPTION_ID=$(az account show --query id -o tsv)
TENANT_ID=$(az account show --query tenantId -o tsv)

echo "==> Resource group"
az group create -n "$RG" -l "$LOCATION" -o none

echo "==> Container registry"
az acr create -n "$ACR" -g "$RG" --sku Basic -o none

echo "==> Key Vault (RBAC mode)"
az keyvault create -n "$KEYVAULT" -g "$RG" -l "$LOCATION" \
  --enable-rbac-authorization true -o none

echo "==> Add your Google Ads secrets to Key Vault now, e.g.:"
echo "    az keyvault secret set --vault-name $KEYVAULT --name gads-developer-token --value '...'"
echo "    az keyvault secret set --vault-name $KEYVAULT --name gads-client-id --value '...'"
echo "    az keyvault secret set --vault-name $KEYVAULT --name gads-client-secret --value '...'"
echo "    az keyvault secret set --vault-name $KEYVAULT --name gads-refresh-token --value '...'"
echo "    az keyvault secret set --vault-name $KEYVAULT --name mcp-auth-token --value \"\$(openssl rand -hex 32)\""

echo "==> Container Apps environment"
az containerapp env create -n "$ENV_NAME" -g "$RG" -l "$LOCATION" -o none

echo "==> Container App (placeholder image; GitHub Actions deploys the real one)"
az containerapp create \
  -n "$APP_NAME" -g "$RG" \
  --environment "$ENV_NAME" \
  --ingress external --target-port 8000 \
  --image mcr.microsoft.com/k8se/quickstart:latest \
  --registry-server "$ACR.azurecr.io" \
  --registry-identity system \
  --system-assigned \
  --min-replicas 0 --max-replicas 2 \
  -o none

APP_IDENTITY=$(az containerapp show -n "$APP_NAME" -g "$RG" \
  --query identity.principalId -o tsv)

echo "==> Grant the app's managed identity read access to Key Vault"
az role assignment create \
  --assignee "$APP_IDENTITY" \
  --role "Key Vault Secrets User" \
  --scope "$(az keyvault show -n "$KEYVAULT" --query id -o tsv)" -o none

echo "==> Wire Key Vault secrets into the app as env vars"
KV_URI="https://$KEYVAULT.vault.azure.net/secrets"
az containerapp secret set -n "$APP_NAME" -g "$RG" --secrets \
  gads-developer-token="keyvaultref:$KV_URI/gads-developer-token,identityref:system" \
  gads-client-id="keyvaultref:$KV_URI/gads-client-id,identityref:system" \
  gads-client-secret="keyvaultref:$KV_URI/gads-client-secret,identityref:system" \
  gads-refresh-token="keyvaultref:$KV_URI/gads-refresh-token,identityref:system" \
  mcp-auth-token="keyvaultref:$KV_URI/mcp-auth-token,identityref:system" -o none

az containerapp update -n "$APP_NAME" -g "$RG" --set-env-vars \
  GOOGLE_ADS_DEVELOPER_TOKEN=secretref:gads-developer-token \
  GOOGLE_ADS_CLIENT_ID=secretref:gads-client-id \
  GOOGLE_ADS_CLIENT_SECRET=secretref:gads-client-secret \
  GOOGLE_ADS_REFRESH_TOKEN=secretref:gads-refresh-token \
  MCP_AUTH_TOKEN=secretref:mcp-auth-token \
  GOOGLE_ADS_USE_PROTO_PLUS=true \
  -o none

echo "==> App registration + federated credential for GitHub Actions OIDC"
APP_ID=$(az ad app create --display-name "$APP_NAME-github-deploy" \
  --query appId -o tsv)
az ad sp create --id "$APP_ID" -o none || true

az ad app federated-credential create --id "$APP_ID" --parameters "{
  \"name\": \"github-main\",
  \"issuer\": \"https://token.actions.githubusercontent.com\",
  \"subject\": \"repo:$GITHUB_ORG/$GITHUB_REPO:ref:refs/heads/main\",
  \"audiences\": [\"api://AzureADTokenExchange\"]
}" -o none

echo "==> Grant the deploy identity Contributor on the resource group"
az role assignment create \
  --assignee "$APP_ID" \
  --role Contributor \
  --scope "/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$RG" -o none

echo "==> Grant it AcrPush on the registry"
az role assignment create \
  --assignee "$APP_ID" \
  --role AcrPush \
  --scope "$(az acr show -n "$ACR" --query id -o tsv)" -o none

FQDN=$(az containerapp show -n "$APP_NAME" -g "$RG" \
  --query properties.configuration.ingress.fqdn -o tsv)

cat <<EOF

============================================================
DONE. Now configure your GitHub repo:

Secrets (Settings -> Secrets and variables -> Actions -> Secrets):
  AZURE_CLIENT_ID        = $APP_ID
  AZURE_TENANT_ID        = $TENANT_ID
  AZURE_SUBSCRIPTION_ID  = $SUBSCRIPTION_ID

Variables (Settings -> Secrets and variables -> Actions -> Variables):
  AZURE_RESOURCE_GROUP   = $RG
  CONTAINER_APP_NAME     = $APP_NAME
  ACR_NAME               = $ACR

Your MCP endpoint (after first deploy):
  https://$FQDN/mcp

Don't forget to set the Key Vault secrets listed above before
the first deploy, and optionally GOOGLE_ADS_CUSTOMER_ID /
GOOGLE_ADS_LOGIN_CUSTOMER_ID as plain env vars on the app.
============================================================
EOF
