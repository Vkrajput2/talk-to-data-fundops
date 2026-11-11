# Deploying to Azure Container Apps

The repository ships a manual GitHub Actions workflow (`.github/workflows/deploy-azure.yml`, `workflow_dispatch` only) and a Bicep
template (`infra/azure/main.bicep`). Nothing deploys automatically. The CI workflow (`ci.yml`) only tests and builds the image.

## What gets created

Azure Container Registry, a Log Analytics workspace, a Container Apps environment and one Container App (1 replica, port 8000,
HTTP liveness probe on `/health`). The LLM key is stored as a Container Apps secret (`llm-api-key`) and injected with `secretRef`.

## One-time setup

1. Create a resource group and an Entra app registration (or user-assigned identity) with a federated credential for this repo
   and the GitHub environment `azure` (OIDC, no client secret). Give it `Contributor` on the resource group.
2. In the GitHub repository settings add:
   - Secrets: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `LLM_API_KEY`
   - Variables: `AZURE_RESOURCE_GROUP`, `AZURE_ACR_NAME`, `AZURE_CONTAINER_APP`, `AZURE_LOCATION`
3. Create the ACR once (`az acr create ...`) or run `infra/azure/deploy.ps1`, which creates the group and registry and deploys.
4. Run the workflow: Actions > "Deploy to Azure Container Apps (manual)" > Run workflow.

To change provider or model on Azure, edit the Bicep parameters `llmProvider`, `llmModel`, `llmBaseUrl` (or pass them on the
command line) and redeploy; rotate the key by updating the `LLM_API_KEY` secret. No code or prompt changes are involved.

## Limits to know about

- Storage is ephemeral. The SQLite file is rebuilt from the bundled CSVs every time the container starts (a few seconds). Nothing
  users type is stored in it. For persistence mount an Azure Files share at `/data` or move to Azure SQL later.
- Chat sessions live in memory, so keep a single replica (`minReplicas = maxReplicas = 1`).
- The mock user dropdown is not authentication. Put real authentication (for example Container Apps built-in auth with Entra ID)
  in front of the app before sharing the URL, because anyone who can reach it can choose any mock user.
- The data is fictional demo data, so the public image contains no customer information.
