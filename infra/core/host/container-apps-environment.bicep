param name string
param location string = resourceGroup().location
param tags object = {}

param daprEnabled bool = false
param logAnalyticsWorkspaceName string
param applicationInsightsName string = ''

@description('Storage account name for file share')
param storageAccountName string = ''

@description('Storage account key for file share')
@secure()
param storageAccountKey string = ''

@description('File share name')
param fileShareName string = ''

resource containerAppsEnvironment 'Microsoft.App/managedEnvironments@2022-03-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalyticsWorkspace.properties.customerId
        sharedKey: logAnalyticsWorkspace.listKeys().primarySharedKey
      }
    }
    daprAIInstrumentationKey: daprEnabled && !empty(applicationInsightsName) ? applicationInsights.properties.InstrumentationKey : ''
  }
}

resource storage 'Microsoft.App/managedEnvironments/storages@2022-03-01' = if (!empty(storageAccountName)) {
  parent: containerAppsEnvironment
  name: 'appdata'
  properties: {
    azureFile: {
      accountName: storageAccountName
      accountKey: storageAccountKey
      shareName: fileShareName
      accessMode: 'ReadWrite'
    }
  }
}

resource logAnalyticsWorkspace 'Microsoft.OperationalInsights/workspaces@2022-10-01' existing = {
  name: logAnalyticsWorkspaceName
}

resource applicationInsights 'Microsoft.Insights/components@2020-02-02' existing = if (daprEnabled && !empty(applicationInsightsName)){
  name: applicationInsightsName
}

output defaultDomain string = containerAppsEnvironment.properties.defaultDomain
output name string = containerAppsEnvironment.name
