# Volume Mount Configuration

## Overview

This application requires a persistent volume mount at `/home/` to maintain MSAL (Microsoft Authentication Library) token caching across container restarts in Azure Container Apps.

## Why Volume Mounting is Needed

The application uses Microsoft Entra ID (formerly Azure AD) for authentication via the MSAL library. MSAL caches authentication tokens locally on the filesystem at `~/.cache/msal/` (which resolves to `/home/<user>/.cache/msal/` in the container).

Without persistent storage:
- Authentication tokens are lost when containers restart
- Users experience re-authentication on every deployment or scaling event
- Session continuity is disrupted during container lifecycle events

## Architecture

The volume mount configuration consists of:

1. **Azure Storage Account** (`infra/core/storage/storage-account.bicep`)
   - Provisions a storage account with Azure Files
   - Creates a file share named `app-data` with 5GB quota
   - Uses Standard_LRS SKU for cost-effectiveness

2. **Container Apps Environment Storage** (`infra/core/host/container-apps-environment.bicep`)
   - Registers the Azure File share as a storage volume in the Container Apps Environment
   - Named `appdata` for reference in container configurations
   - Configured with ReadWrite access mode

3. **Container Volume Mount** (`infra/core/host/container-app.bicep`)
   - Mounts the `appdata` volume to `/home/` path in the container
   - Ensures MSAL token cache persists across container instances

## Infrastructure Components

### Storage Account
- **Name**: `<prefix>storage` (hyphens removed)
- **Location**: Same as resource group
- **SKU**: Standard_LRS
- **File Share**: `app-data` (5GB quota)

### Volume Mount Configuration
- **Environment Storage Name**: `appdata`
- **Mount Path**: `/home/`
- **Access Mode**: ReadWrite

## Deployment

### Automatic Deployment (Recommended)

The volume mount is automatically configured when deploying with `azd up`. The infrastructure includes:

```bicep
// Storage account provisioning
module storageAccount 'core/storage/storage-account.bicep' = {
  name: 'storage'
  scope: resourceGroup
  params: {
    name: '${replace(prefix, '-', '')}storage'
    location: location
    tags: tags
    fileShareName: 'app-data'
  }
}

// Volume mount in container app
storageVolumeName: 'appdata'
storageMountPath: '/home/'
```

### Manual Deployment (Azure Cloud Shell)

If you need to manually configure the volume mount on an existing deployment, use the provided Azure Cloud Shell script:

1. Navigate to [Azure Cloud Shell](https://shell.azure.com)
2. Upload the script from `scripts/setup-volume-mount.sh`
3. Follow the instructions in [`scripts/VOLUME_MOUNT_SETUP.md`](../scripts/VOLUME_MOUNT_SETUP.md)

The script will:
- Create or verify the storage account exists
- Create the `app-data` file share
- Register the storage in your Container Apps Environment
- Provide verification steps

**Note:** After running the script, you must still update your Container App deployment to mount the volume (via Bicep, Portal, or CLI).

## Verification

After deployment, verify the volume mount in the Azure Portal:

1. Navigate to your Container App
2. Go to **Containers** section
3. Select **Volume mounts** tab
4. Confirm the file share is mounted at `/home/`

Reference screenshot showing the expected configuration:

![Volume Mount Configuration](../New%20Project.png)

## Security Considerations

- The storage account key is securely passed through Bicep parameters marked as `@secure()`
- The key is stored in the Container Apps Environment configuration, not in container environment variables
- File share access is restricted to the Container Apps Environment

## Troubleshooting

### Volume Mount Not Working
1. Check that the storage account exists and is in the same region
2. Verify the file share `app-data` exists in the storage account
3. Ensure the Container Apps Environment has the correct storage configuration
4. Check container logs for permission errors accessing `/home/`

### Authentication Issues
If users still experience re-authentication after deployment:
1. Verify the volume is actually mounted (check container logs)
2. Ensure the mount path is `/home/` (not `/home` without trailing slash)
3. Check that the application user has write permissions to `/home/.cache/`

## Cost Implications

The Azure Storage Account adds minimal cost:
- **Storage Account**: ~$0.02/GB/month (Standard LRS)
- **File Share**: 5GB = ~$0.10/month
- **Transactions**: Minimal for token cache operations

Total additional cost: **~$0.10-0.20/month**

## References

- [Azure Container Apps Storage Mounts](https://learn.microsoft.com/azure/container-apps/storage-mounts)
- [Azure Files Documentation](https://learn.microsoft.com/azure/storage/files/)
- [MSAL Token Cache](https://learn.microsoft.com/entra/msal/python/token-cache-serialization)
