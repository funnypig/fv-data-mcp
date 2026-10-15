targetScope = 'resourceGroup'

param location string = 'northeurope'
param prefix string = 'fvdata'
param imageDigest string
param auth0Issuer string
param providerAccountId string = 'owner'
@minValue(0)
@maxValue(1)
param minReplicas int = 0
@minValue(1)
@maxValue(2)
param maxReplicas int = 1

var suffix = uniqueString(resourceGroup().id)
resource environment 'Microsoft.App/managedEnvironments@2025-01-01' existing = { name: '${prefix}-environment' }
resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = { name: '${prefix}-runtime' }
resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = { name: '${prefix}${suffix}' }
resource vault 'Microsoft.KeyVault/vaults@2023-07-01' existing = { name: '${prefix}-${suffix}' }
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = { name: '${prefix}${suffix}' }
resource insights 'Microsoft.Insights/components@2020-02-02' existing = { name: '${prefix}-insights' }
var publicUrl = 'https://${prefix}-mcp.${environment.properties.defaultDomain}'

resource app 'Microsoft.App/containerApps@2025-01-01' = {
  name: '${prefix}-mcp'
  location: location
  tags: { application: 'fv-data-mcp', environment: 'production' }
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${identity.id}': {} } }
  properties: {
    managedEnvironmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: { external: true, targetPort: 8000, transport: 'http', allowInsecure: false }
      registries: [{ server: registry.properties.loginServer, identity: identity.id }]
      secrets: [for secretName in ['finviz-api-key', 'redis-url', 'cursor-secret']: {
        name: secretName
        keyVaultUrl: '${vault.properties.vaultUri}secrets/${secretName}'
        identity: identity.id
      }]
    }
    template: {
      containers: [{
        name: 'mcp'
        image: '${registry.properties.loginServer}/fv-data-mcp@${imageDigest}'
        resources: { cpu: json('0.5'), memory: '1Gi' }
        env: [
          { name: 'FV_ENVIRONMENT', value: 'production' }
          { name: 'FV_PUBLIC_URL', value: publicUrl }
          { name: 'FV_AUTH0_AUDIENCE', value: '${publicUrl}/mcp' }
          { name: 'FV_AUTH0_ISSUER', value: auth0Issuer }
          { name: 'FV_ACCESS_BACKEND', value: 'azure' }
          { name: 'FV_TABLE_ENDPOINT', value: storage.properties.primaryEndpoints.table }
          { name: 'FV_PROVIDER_ACCOUNT_ID', value: providerAccountId }
          { name: 'FV_FINVIZ_API_KEY', secretRef: 'finviz-api-key' }
          { name: 'FV_REDIS_URL', secretRef: 'redis-url' }
          { name: 'FV_CURSOR_SECRET', secretRef: 'cursor-secret' }
          { name: 'FV_APPLICATIONINSIGHTS_CONNECTION_STRING', value: insights.properties.ConnectionString }
          { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
        ]
        probes: [
          { type: 'Startup', httpGet: { path: '/health/live', port: 8000, httpHeaders: [{ name: 'Host', value: 'localhost' }] }, periodSeconds: 5, failureThreshold: 30, timeoutSeconds: 2 }
          { type: 'Liveness', httpGet: { path: '/health/live', port: 8000, httpHeaders: [{ name: 'Host', value: 'localhost' }] }, periodSeconds: 30, failureThreshold: 3, timeoutSeconds: 2 }
          { type: 'Readiness', httpGet: { path: '/health/ready', port: 8000, httpHeaders: [{ name: 'Host', value: 'localhost' }] }, periodSeconds: 30, failureThreshold: 3, timeoutSeconds: 5 }
        ]
      }]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
        rules: [{ name: 'http', http: { metadata: { concurrentRequests: '10' } } }]
      }
    }
  }
}
output mcpUrl string = '${publicUrl}/mcp'
output appName string = app.name
