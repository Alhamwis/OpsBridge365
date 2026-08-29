// =============================================================================
// OpsBridge365 - one-time bootstrap
// =============================================================================
// Everything in this file needs a privilege the routine deployment identity
// deliberately does NOT have, or a secret value that deliberately does NOT live
// in GitHub. It is run by a human operator, from a workstation, on purpose.
//
//   Key Vault                 - the store
//   User-assigned identity    - the only principal allowed to read from it
//   Role assignment           - Key Vault Secrets User, vault scope
//   Secret values             - the Graph client secret and the metrics token
//
// Why this is a separate template
// -------------------------------
// The role assignment is the whole reason. `Microsoft.Authorization/
// roleAssignments` cannot be created by Contributor, so as long as it lived in
// main.bicep the GitHub deployment identity had to hold Role Based Access
// Control Administrator - permanently, on every push, to create one assignment
// that only ever needs creating once. Splitting it out lets the routine
// identity drop to Contributor.
//
// The secret values are the second reason. main.bicep used to take the Graph
// client secret as a parameter, so GitHub had to store it and hand it over on
// every deployment. Here the value is supplied once by the operator; from then
// on main.bicep only references the vault.
//
// Running it
// ----------
//   az deployment group create -g rg-opsbridge365 \
//     --template-file infra/bootstrap.bicep \
//     --parameters graphClientSecret=<value> metricsApiToken=<value>
//
// Both secret parameters are optional and independent: pass only the one you
// are rotating. An omitted parameter leaves the existing secret untouched
// rather than overwriting it with an empty string - see the `if (!empty(...))`
// guards below, which are what makes a partial re-run safe.
//
// This template is idempotent. Re-running it against an existing environment
// re-asserts the same vault, identity and role assignment (the assignment name
// is a deterministic guid()) and changes nothing else.
// =============================================================================

targetScope = 'resourceGroup'

@description('Azure region. Defaults to the resource group\'s region.')
param location string = resourceGroup().location

@description('Prefix for all resource names. MUST match the namePrefix used by main.bicep, or the routine deployment will look for a vault that does not exist.')
@minLength(3)
@maxLength(11)
param namePrefix string = 'opsbridge'

@description('Client secret of the Graph app registration. Leave empty to keep the existing secret - it is only written when a value is supplied.')
@secure()
param graphClientSecret string = ''

@description('Bearer token that authenticates GET /metrics. Leave empty to keep the existing token. Generate with: openssl rand -base64 32')
@secure()
param metricsApiToken string = ''

// ---------------------------------------------------------------- constants --

// Built-in role: Key Vault Secrets User (read secret values, data plane only).
var keyVaultSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'

var graphSecretName = 'graph-client-secret'
var metricsTokenSecretName = 'metrics-api-token'

// Vault names are globally unique and capped at 24 chars. This expression is
// duplicated in main.bicep on purpose: both templates must derive the SAME name
// from the same prefix, and a shared module would make the routine template
// depend on this one at compile time.
var keyVaultName = take('${namePrefix}kv${uniqueString(resourceGroup().id)}', 24)
var identityName = '${namePrefix}-id'

var commonTags = {
  project: 'OpsBridge365'
  layer: 'cloud'
  managedBy: 'bootstrap'
}

// ----------------------------------------------------------------- identity --

// One user-assigned identity shared by the job and the app. Shared on purpose:
// both need exactly one permission (read one secret), so two identities would
// be two things to grant, rotate and explain.
resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
  tags: commonTags
}

// ---------------------------------------------------------------- key vault --

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  tags: commonTags
  properties: {
    // The vault's control-plane tenant is the AZURE tenant that owns this
    // subscription - deliberately NOT the Microsoft 365 tenant. A vault can
    // only trust principals from its own directory, and the identity above is
    // created here.
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

// THE reason this template exists. Contributor cannot create role assignments;
// keeping this out of main.bicep is what lets the deployment identity drop
// Role Based Access Control Administrator.
resource secretsUserAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: keyVault
  name: guid(keyVault.id, identity.id, keyVaultSecretsUserRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      keyVaultSecretsUserRoleId
    )
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// ------------------------------------------------------------------ secrets --

resource graphSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (!empty(graphClientSecret)) {
  parent: keyVault
  name: graphSecretName
  properties: {
    value: graphClientSecret
    contentType: 'Entra app registration client secret'
  }
}

resource metricsSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (!empty(metricsApiToken)) {
  parent: keyVault
  name: metricsTokenSecretName
  properties: {
    value: metricsApiToken
    contentType: 'Bearer token for GET /metrics'
  }
}

// ----------------------------------------------------------------- outputs --
// No secret is ever emitted here. A principal id and a vault name are both safe
// to print; deployment outputs are readable by anyone with resource-group access.

@description('Name of the Key Vault main.bicep must reference.')
output keyVaultName string = keyVault.name

@description('Name of the shared user-assigned managed identity.')
output identityName string = identity.name

@description('Principal (object) id of the shared user-assigned managed identity.')
output identityPrincipalId string = identity.properties.principalId

@description('Names of the secrets this template manages. Values are never output.')
output secretNames array = [graphSecretName, metricsTokenSecretName]
