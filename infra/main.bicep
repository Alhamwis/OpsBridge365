// =============================================================================
// OpsBridge365 - cloud layer, free-tier v2 (ROUTINE deployment)
// =============================================================================
// Deploys the part of the system that changes on every push:
//
//   Log Analytics  <- logs for everything below (5 GB/month free)
//   Container Apps Environment
//   JOB  opsbridge-sync     <- cron, run-and-exit, `python -m app.sync`
//   APP  opsbridge-api      <- HTTPS ingress, scale-to-zero (minReplicas 0)
//
// It does NOT create the Key Vault, the managed identity, the role assignment,
// or any secret value. Those are infra/bootstrap.bicep, run once by a human.
// This template only *references* them, and that split is deliberate:
//
//   * A role assignment needs Role Based Access Control Administrator. Leaving
//     one here meant the GitHub deployment identity carried that privilege on
//     every push forever, to create an assignment that is created once. The
//     routine identity now needs Contributor and nothing more.
//   * Taking the Graph client secret as a parameter meant GitHub had to store
//     it and hand it over on every deployment. The value now lives only in Key
//     Vault; this template names the secret and never sees it.
//
// Consequence, stated plainly: bootstrap.bicep is a PREREQUISITE. Deploying
// this template into a resource group that has not been bootstrapped fails at
// the `existing` lookups below, which is the intended, loud failure.
//
// Cost design: the job only bills while it is running and the API only bills
// while it is awake, so an idle month stays inside the Container Apps free
// grant. See infra/README.md.
//
// Nothing here contains a subscription id, a tenant id, or a secret value -
// every environment-specific value arrives as a parameter.
//
// TWO TENANTS ARE IN PLAY and they are not the same directory:
//   * the Azure tenant owning this subscription - implicit, reached through
//     subscription().tenantId, and what the Key Vault control plane uses;
//   * the Microsoft 365 tenant owning the Graph app and the SharePoint site -
//     supplied explicitly as the `graphTenantId` parameter and handed to the
//     containers as AZURE_TENANT_ID, the value MSAL builds its authority from.
// Passing the Azure tenant id as `graphTenantId` compiles and deploys happily,
// then fails every Graph token request at runtime. Keep them distinct.
// =============================================================================

targetScope = 'resourceGroup'

// ------------------------------------------------------------------ params --

@description('Azure region for every resource. Defaults to the resource group\'s region.')
param location string = resourceGroup().location

@description('Prefix for all resource names. MUST match the namePrefix used by infra/bootstrap.bicep - the Key Vault name is derived from it.')
@minLength(3)
@maxLength(11)
param namePrefix string = 'opsbridge'

@description('Full container image reference, e.g. ghcr.io/OWNER/opsbridge365:latest. The GHCR package must be public - a public image needs no registry credentials.')
param containerImage string

@description('Tenant id of the Microsoft 365 tenant where the Graph app registration lives - the directory the containers build their MSAL authority from. This is NOT the Azure tenant that owns this subscription. Not a secret.')
param graphTenantId string

@description('Application (client) id of the Graph app registration in the Microsoft 365 tenant above. Not a secret.')
param clientId string

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

var graphSecretName = 'graph-client-secret'
var metricsTokenSecretName = 'metrics-api-token'

// Derived identically to infra/bootstrap.bicep. If the two ever disagree, this
// template fails on the `existing` lookup rather than silently building a
// second vault - which is the failure mode worth having.
var keyVaultName = take('${namePrefix}kv${uniqueString(resourceGroup().id)}', 24)
var identityName = '${namePrefix}-id'

var logAnalyticsName = '${namePrefix}-logs'
var environmentName = '${namePrefix}-env'
var jobName = '${namePrefix}-sync'
var apiName = '${namePrefix}-api'

var commonTags = {
  project: 'OpsBridge365'
  layer: 'cloud'
  managedBy: 'bicep'
}

// ------------------------------------------------- bootstrapped references --

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: identityName
}

// Versionless secret URIs. A version-pinned URI would mean every secret
// rotation needs a redeployment; versionless lets Container Apps pick up a new
// version on the next revision restart.
var graphSecretUri = '${keyVault.properties.vaultUri}secrets/${graphSecretName}'
var metricsSecretUri = '${keyVault.properties.vaultUri}secrets/${metricsTokenSecretName}'

// Configuration that is NOT sensitive: ids, not credentials. These are plain
// env vars on purpose - putting public identifiers in Key Vault would add cost
// and noise without adding protection. Only the two secrets are secret.
var configEnv = [
  {
    // Named AZURE_TENANT_ID because that is what app/config.py reads, but the
    // value is the Microsoft 365 (Graph) tenant, not the Azure one.
    name: 'AZURE_TENANT_ID'
    value: graphTenantId
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

var graphSecretEnv = [
  {
    name: 'AZURE_CLIENT_SECRET'
    secretRef: graphSecretName
  }
]

// Only the API needs this. The sync job has no HTTP surface, so giving it the
// metrics token would hand a credential to a workload that cannot use it.
var metricsTokenEnv = [
  {
    name: 'METRICS_API_TOKEN'
    secretRef: metricsTokenSecretName
  }
]

var graphSecretRef = [
  {
    name: graphSecretName
    keyVaultUrl: graphSecretUri
    identity: identity.id
  }
]

var metricsSecretRef = [
  {
    name: metricsTokenSecretName
    keyVaultUrl: metricsSecretUri
    identity: identity.id
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
      secrets: graphSecretRef
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
          env: concat(configEnv, graphSecretEnv)
        }
      ]
    }
  }
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
      secrets: concat(graphSecretRef, metricsSecretRef)
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
          env: concat(configEnv, graphSecretEnv, metricsTokenEnv)
        }
      ]
      scale: {
        // Scale-to-zero. This single line is what makes an idle month cost
        // nothing; the first request after idle pays a cold start instead.
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
}

// ----------------------------------------------------------------- outputs --
// No secret is ever emitted here: a principal id and a vault name are both
// safe to print, and deployment outputs are readable by anyone with RG access.

@description('Public HTTPS hostname of the metrics API.')
output apiFqdn string = apiApp.properties.configuration.ingress.fqdn

@description('Name of the Key Vault holding the runtime secrets (created by bootstrap.bicep).')
output keyVaultName string = keyVault.name

@description('Resource id of the Log Analytics workspace collecting container logs.')
output logAnalyticsWorkspaceId string = logAnalytics.id

@description('Name of the scheduled sync job, for `az containerapp job start`.')
output jobName string = syncJob.name

@description('Principal (object) id of the shared user-assigned managed identity.')
output identityPrincipalId string = identity.properties.principalId
