# One-time setup helper (run by a human with Azure CLI logged in). Creates the resource group and deploys main.bicep.
# The key is read from the environment variable LLM_API_KEY and passed as a secure parameter; it is never echoed.
param(
  [Parameter(Mandatory = $true)][string]$ResourceGroup,
  [Parameter(Mandatory = $true)][string]$AcrName,
  [string]$Location = "westeurope",
  [string]$AppName = "talk-to-data",
  [string]$ImageTag = "latest"
)
if (-not $env:LLM_API_KEY) { throw "Set the LLM_API_KEY environment variable first." }
az group create --name $ResourceGroup --location $Location | Out-Null
az acr create --name $AcrName --resource-group $ResourceGroup --sku Basic --admin-enabled true | Out-Null
az acr build --registry $AcrName --image "talk-to-data:$ImageTag" (Join-Path $PSScriptRoot "..\..")
az deployment group create --resource-group $ResourceGroup --template-file (Join-Path $PSScriptRoot "main.bicep") `
  --parameters location=$Location acrName=$AcrName appName=$AppName imageTag=$ImageTag llmApiKey=$env:LLM_API_KEY
