// =============================================================================
// OpsBridge365 - cloud layer, free-tier v2
// =============================================================================
// Deploys the whole cloud half of the system into ONE resource group:
//
//   Log Analytics  <- logs for everything below (5 GB/month free)
//   Container Apps Environment
//   Key Vault (RBAC)        <- holds the Entra app's client secret, nothing else
//   User-assigned identity  <- the only thing allowed to read that secret
//   JOB  opsbridge-sync     <- cron, run-and-exit, `python -m app.sync`
//   APP  opsbridge-api      <- HTTPS ingress, scale-to-zero (minReplicas 0)
//
// Cost design: the job only bills while it is running and the API only bills
// while it is awake, so an idle month stays inside the Container Apps free
// grant. See infra/README.md.
//
// Nothing here contains a subscription id, a tenant id, or a secret value -
// every environment-specific value arrives as a parameter.
// =============================================================================

targetScope = 'resourceGroup'

// ------------------------------------------------------------------ params --

@description('Azure region for every resource. Defaults to the resource group\'s region.')
param location string = resourceGroup().location

@description('Prefix for all resource names. Keep it short - the Key Vault name is derived from it and vault names are capped at 24 characters.')
@minLength(3)
@maxLength(11)
param namePrefix string = 'opsbridge'

@description('Full container image reference, e.g. ghcr.io/OWNER/opsbridge365:latest. The GHCR package must be public - a public image needs no registry credentials.')
param containerImage string

@description('Entra tenant id the sync authenticates against (the trial tenant, not the college's). Not a secret.')
param tenantId string

@description('Application (client) id of the Entra app registration. Not a secret.')
param clientId string

@description('Client secret for the Entra app registration. Stored in Key Vault and read at runtime through the managed identity - it is never inlined into a container definition and never returned as an output.')
@secure()
param clientSecret string

@description('Graph id of the SharePoint site holding the Assets and Tickets lists.')
param sharePointSiteId string

@description('Graph id of the Assets list (written by the sync job).')
param assetsListId string

@description('Graph id of the Tickets list (read by /metrics).')
param ticketsListId string

@description('Cron expression for the sync job, 5 fields, UTC. Default: every 6 hours on the hour.')
param syncCron string = '0 */6 * * *'

// --------------------------------------------------------------- constants --

// Hard ceiling on a single sync execution. The job is killed at this point, so
// a hung Graph call can never bill for longer than 30 minutes.
var replicaTimeoutSeconds = 1800

// Built-in role: Key Vault Secrets User (read secret values, data plane).
var keyVaultSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'

var secretName = 'graph-client-secret'

// Vault names are globally unique and max 24 chars, hence the hash + take().
var keyVaultName = take('${namePrefix}kv${uniqueString(resourceGroup().id)}', 24)

var logAnalyticsName = '${namePrefix}-logs'
var environmentName = '${namePrefix}-env'
var identityName = '${namePrefix}-id'
var jobName = '${namePrefix}-sync'
var apiName = '${namePrefix}-api'

var commonTags = {
  project: 'OpsBridge365'
  layer: 'cloud'
  managedBy: 'bicep'
}

// Configuration that is NOT sensitive: ids, not credentials. These are plain
// env vars on purpose - putting public identifiers in Key Vault would add cost
// and noise without adding protection. Only the client secret is a secret.
var configEnv = [
  {
    name: 'AZURE_TENANT_ID'
    value: tenantId
  }
  {
    name: 'AZURE_CLIENT_ID'
    value: clientId
  }
  {
    name: 'SHAREPOINT_SITE_ID'
    value: sharePointSiteId
  }
  {
    name: 'ASSETS_LIST_ID'
    value: assetsListId
  }
  {
    name: 'TICKETS_LIST_ID'
    value: ticketsListId
  }
]

// The one credential, pulled from Key Vault at container start via secretRef.
var secretEnv = [
  {
    name: 'AZURE_CLIENT_SECRET'
    secretRef: secretName
  }
]

// -------------------------------------------------------------- monitoring --

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsName
  location: location
  tags: commonTags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
    features: {
      searchVersion: 1
    }
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

// ---------------------------------------------------------------- identity --

// One user-assigned identity shared by the job and the app. Shared on purpose:
// both need exactly one permission (read one secret), so two identities would
// be two things to grant, rotate and explain.
resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
  tags: commonTags
}

// --------------------------------------------------------------- key vault --

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  tags: commonTags
  properties: {
    tenantId: subscription().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    // RBAC instead of access policies: one role assignment below is the entire
    // authorisation story, and it shows up in `az role assignment list`.
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    enabledForDeployment: false
    enabledForTemplateDeployment: false
    enabledForDiskEncryption: false
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Allow'
    }
  }
}

resource graphClientSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: secretName
  properties: {
    value: clientSecret
    contentType: 'Entra app registration client secret'
  }
}

// Key Vault Secrets User, scoped to this vault only - not the resource group,
// not the subscription. The identity can read secret values and nothing else.
resource secretsUserAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: keyVault
  name: guid(keyVault.id, identity.id, keyVaultSecretsUserRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleId)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// -------------------------------------------------- container apps runtime --

resource containerAppsEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: environmentName
  location: location
  tags: commonTags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
    zoneRedundant: false
  }
}

// ------------------------------------------------------------- sync job ----

// Schedule-triggered job: Azure starts a replica on the cron, the container
// runs `python -m app.sync` to completion, and the replica is torn down. There
// is no scheduler inside the image and nothing is billed between runs.
resource syncJob 'Microsoft.App/jobs@2024-03-01' = {
  name: jobName
  location: location
  tags: commonTags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
  properties: {
    environmentId: containerAppsEnvironment.id
    configuration: {
      triggerType: 'Schedule'
      replicaTimeout: replicaTimeoutSeconds
      // One attempt at a retry. A sync is idempotent (PATCH by device id), but
      // the next cron tick is only hours away - retrying forever would just
      // burn free-grant seconds on a Graph outage.
      replicaRetryLimit: 1
      scheduleTriggerConfig: {
        cronExpression: syncCron
        parallelism: 1
        replicaCompletionCount: 1
      }
      secrets: [
        {
          name: secretName
          keyVaultUrl: '${keyVault.properties.vaultUri}secrets/${secretName}'
          identity: identity.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'sync'
          image: containerImage
          // Overrides the image's uvicorn CMD - same image, other entrypoint.
          command: [
            'python'
          ]
          args: [
            '-m'
            'app.sync'
          ]
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
          env: concat(configEnv, secretEnv)
        }
      ]
    }
  }
  dependsOn: [
    // The Key Vault reference is resolved when the job is created, so the role
    // assignment and the secret must both exist first.
    secretsUserAssignment
    graphClientSecret
  ]
}

// ---------------------------------------------------------------- api app --

resource apiApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: apiName
  location: location
  tags: commonTags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
  properties: {
    environmentId: containerAppsEnvironment.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        // HTTPS only: plain HTTP is redirected, never served.
        allowInsecure: false
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
      secrets: [
        {
          name: secretName
          keyVaultUrl: '${keyVault.properties.vaultUri}secrets/${secretName}'
          identity: identity.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'api'
          image: containerImage
          // No command override - the image's CMD already starts uvicorn.
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
          env: concat(configEnv, secretEnv)
        }
      ]
      scale: {
        // Scale-to-zero. This single line is what makes an idle month cost
        // nothing; the first request after idle pays a few seconds of cold
        // start instead.
        minReplicas: 0
        maxReplicas: 1
        rules: [
          {
            name: 'http-scale'
            http: {
              metadata: {
                concurrentRequests: '20'
              }
            }
          }
        ]
      }
    }
  }
  dependsOn: [
    secretsUserAssignment
    graphClientSecret
  ]
}

// ----------------------------------------------------------------- outputs --
// No secret is ever emitted here: a principal id and a vault name are both
// safe to print, and deployment outputs are readable by anyone with RG access.

@description('Public HTTPS hostname of the metrics API.')
output apiFqdn string = apiApp.properties.configuration.ingress.fqdn

@description('Name of the Key Vault holding the Graph client secret.')
output keyVaultName string = keyVault.name

@description('Resource id of the Log Analytics workspace collecting container logs.')
output logAnalyticsWorkspaceId string = logAnalytics.id

@description('Name of the scheduled sync job, for `az containerapp job start`.')
output jobName string = syncJob.name

@description('Principal (object) id of the shared user-assigned managed identity.')
output identityPrincipalId string = identity.properties.principalId
