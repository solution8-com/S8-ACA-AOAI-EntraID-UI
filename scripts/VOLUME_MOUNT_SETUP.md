# Volume Mount Setup for Azure Container Apps

This directory contains scripts to configure persistent volume mounts for Azure Container Apps, specifically for MSAL token caching.

## Files

- **setup-volume-mount.sh** - Interactive Azure Cloud Shell script for setting up volume mounts

## Quick Start

### Using Azure Cloud Shell

1. **Upload the script to Azure Cloud Shell:**
   ```bash
   # In Azure Cloud Shell
   cd ~
   mkdir -p container-app-setup
   cd container-app-setup
   ```

2. **Upload `setup-volume-mount.sh` to Cloud Shell** (use the upload button in Cloud Shell UI)

3. **Make the script executable:**
   ```bash
   chmod +x setup-volume-mount.sh
   ```

4. **Run the script:**
   ```bash
   ./setup-volume-mount.sh
   ```

The script will prompt you for:
- Resource Group name
- Storage Account name (must be lowercase, no hyphens, 3-24 characters)
- Container Apps Environment name
- Container App name

### Using Pre-set Environment Variables

For automated/scripted deployments:

```bash
export RESOURCE_GROUP="your-rg-name"
export STORAGE_ACCOUNT_NAME="yourstorageaccount"
export CONTAINER_APPS_ENVIRONMENT="your-env-name"
export CONTAINER_APP_NAME="your-app-name"
export FILE_SHARE_NAME="app-data"  # Optional, defaults to "app-data"
export STORAGE_VOLUME_NAME="appdata"  # Optional, defaults to "appdata"
export MOUNT_PATH="/home/"  # Optional, defaults to "/home/"

./setup-volume-mount.sh
```

## What the Script Does

1. **Validates Prerequisites:**
   - Checks Azure CLI login status
   - Verifies resource group exists
   - Confirms Container Apps Environment exists

2. **Storage Account Setup:**
   - Creates storage account if it doesn't exist
   - Uses Standard_LRS SKU (cost-effective)
   - Enables HTTPS-only and minimum TLS 1.2

3. **File Share Creation:**
   - Creates Azure Files share named `app-data` (configurable)
   - Sets 5 GiB quota
   - Configures for MSAL token cache storage

4. **Container Apps Environment Configuration:**
   - Registers the file share as a storage volume
   - Names it `appdata` for container reference
   - Sets ReadWrite access mode

5. **Verification:**
   - Provides commands to verify each step
   - Outputs Azure Portal links for manual inspection
   - Displays next steps for completing the setup

## Post-Script Actions Required

After running the script, you must update your Container App deployment to actually mount the volume. Choose one of these methods:

### Method 1: Using Bicep (Recommended for Infrastructure as Code)

This is already configured in the repository's `infra/` directory. Simply run:

```bash
azd up
```

The Bicep templates will:
- Provision all resources including storage
- Register storage in the Container Apps Environment
- Configure volume mounts in the container

### Method 2: Using Azure Portal

1. Navigate to your Container App in Azure Portal
2. Go to **Containers** section
3. Select **Volume mounts** tab
4. Click **Add**
5. Configure:
   - **Volume name**: `appdata`
   - **Volume type**: Azure file
   - **Storage name**: Select the registered storage (`appdata`)
   - **Mount path**: `/home/`
6. Click **Save**
7. The container will restart with the volume mounted

### Method 3: Using Azure CLI

```bash
# Get current configuration
az containerapp show --name "$CONTAINER_APP_NAME" --resource-group "$RESOURCE_GROUP" > current-config.json

# Update with volume mount (requires manual JSON editing)
# Or use the following command structure:
az containerapp update \
  --name "$CONTAINER_APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --yaml volume-mount-config.yaml
```

## Troubleshooting

### Error: "mount error(2): No such file or directory"

**Cause:** The file share name doesn't match what's registered in the environment.

**Solution:**
1. Check the actual file share name:
   ```bash
   az storage share list --account-name "$STORAGE_ACCOUNT_NAME" --query '[].name'
   ```
2. Verify environment storage configuration:
   ```bash
   az containerapp env storage show \
     --name "$STORAGE_VOLUME_NAME" \
     --environment-name "$CONTAINER_APPS_ENVIRONMENT" \
     --resource-group "$RESOURCE_GROUP"
   ```
3. Ensure names match. Update if needed:
   ```bash
   az containerapp env storage set \
     --name "$STORAGE_VOLUME_NAME" \
     --azure-file-share-name "actual-share-name" \
     ...other params...
   ```

### Error: "Storage account name must be between 3 and 24 characters"

**Cause:** Invalid storage account name.

**Solution:** Storage account names must:
- Be 3-24 characters
- Contain only lowercase letters and numbers
- Be globally unique across Azure

### Permission Errors

**Cause:** Insufficient permissions on the subscription or resource group.

**Solution:** Ensure you have:
- `Contributor` or `Owner` role on the resource group
- Permissions to create storage accounts
- Permissions to modify Container Apps Environment

## Verification Commands

After setup, use these commands to verify:

```bash
# Check storage account
az storage account show \
  --name "$STORAGE_ACCOUNT_NAME" \
  --resource-group "$RESOURCE_GROUP"

# List file shares
az storage share list \
  --account-name "$STORAGE_ACCOUNT_NAME" \
  --output table

# Check environment storage
az containerapp env storage list \
  --environment-name "$CONTAINER_APPS_ENVIRONMENT" \
  --resource-group "$RESOURCE_GROUP" \
  --output table

# View container app template
az containerapp show \
  --name "$CONTAINER_APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query 'properties.template' \
  --output json
```

## Cost Considerations

The storage account adds minimal monthly cost:
- **Storage Account**: ~$0.02/GB/month (Standard LRS)
- **5 GiB File Share**: ~$0.10/month
- **Transactions**: Minimal for token cache operations

**Total additional cost: ~$0.10-0.20/month**

## Security Notes

- The storage account key is securely stored in the Container Apps Environment
- Keys are not exposed in container environment variables
- File share access is restricted to the Container Apps Environment
- All connections use HTTPS/TLS 1.2+

## Related Documentation

- [Volume Mount Configuration](../docs/volume-mount.md)
- [Azure Container Apps Storage Mounts](https://learn.microsoft.com/azure/container-apps/storage-mounts)
- [Azure Files Documentation](https://learn.microsoft.com/azure/storage/files/)
- [MSAL Token Cache](https://learn.microsoft.com/entra/msal/python/token-cache-serialization)
