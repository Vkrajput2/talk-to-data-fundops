// Azure Container Registry + Log Analytics + Container Apps environment + the Talk to Data app.
// The LLM key is a secure parameter, stored as a Container Apps secret and referenced via secretRef (never a literal env value).
targetScope = 'resourceGroup'

param location string = resourceGroup().location
@description('Globally unique ACR name (5-50 alphanumerics)')
param acrName string
param appName string = 'talk-to-data'
param imageTag string = 'latest'
@description('Provider, model and optional base URL are plain settings; change them here without touching the code')
param llmProvider string = 'groq'
param llmModel string = 'openai/gpt-oss-20b'
param llmBaseUrl string = ''
@secure()
param llmApiKey string

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: acrName
  location: location
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: true }
}

resource logs 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: '${appName}-logs'
  location: location
  properties: { sku: { name: 'PerGB2018' }, retentionInDays: 30 }
}

resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${appName}-env'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: appName
  location: location
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      ingress: { external: true, targetPort: 8000, transport: 'auto' }
      registries: [
        { server: acr.properties.loginServer, username: acr.listCredentials().username, passwordSecretRef: 'acr-password' }
      ]
      secrets: [
        { name: 'acr-password', value: acr.listCredentials().passwords[0].value }
        { name: 'llm-api-key', value: llmApiKey }
      ]
    }
    template: {
      containers: [
        {
          name: 'app'
          image: '${acr.properties.loginServer}/talk-to-data:${imageTag}'
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: [
            { name: 'PORT', value: '8000' }
            { name: 'LLM_PROVIDER', value: llmProvider }
            { name: 'LLM_MODEL', value: llmModel }
            { name: 'LLM_BASE_URL', value: llmBaseUrl }
            { name: 'LLM_API_KEY', secretRef: 'llm-api-key' }
          ]
          probes: [
            { type: 'Liveness', httpGet: { path: '/health', port: 8000 }, initialDelaySeconds: 10, periodSeconds: 30 }
          ]
        }
      ]
      // One replica: chat sessions are held in memory. SQLite is rebuilt from the CSVs at start (ephemeral storage).
      scale: { minReplicas: 1, maxReplicas: 1 }
    }
  }
}

output fqdn string = app.properties.configuration.ingress.fqdn
