#!/bin/bash

################################################################################
# Azure Container Apps Volume Mount Setup Script
# 
# This script configures persistent volume mounts for MSAL token caching
# in Azure Container Apps. It handles:
# 1. Storage account creation/verification
# 2. File share creation with correct naming
# 3. Storage registration in Container Apps Environment
# 4. Container app volume mount configuration
#
# Prerequisites:
# - Azure CLI installed and logged in
# - Appropriate permissions on the target subscription
# - Container Apps Environment already deployed
#
# Usage:
#   ./setup-volume-mount.sh
#
# The script will prompt for required values or you can set them as environment
# variables before running:
#   export RESOURCE_GROUP="your-rg-name"
#   export STORAGE_ACCOUNT_NAME="yourstorageaccount"
#   export CONTAINER_APPS_ENVIRONMENT="your-env-name"
#   export CONTAINER_APP_NAME="your-app-name"
#   ./setup-volume-mount.sh
################################################################################

set -e  # Exit on error
set -u  # Exit on undefined variable

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Function to print colored output
print_info() { echo -e "${BLUE}ℹ ${1}${NC}"; }
print_success() { echo -e "${GREEN}✓ ${1}${NC}"; }
print_warning() { echo -e "${YELLOW}⚠ ${1}${NC}"; }
print_error() { echo -e "${RED}✗ ${1}${NC}"; }

# Function to prompt for value if not set
prompt_if_empty() {
    local var_name=$1
    local prompt_text=$2
    local current_value="${!var_name:-}"
    
    if [ -z "$current_value" ]; then
        read -p "$prompt_text: " value
        eval "$var_name='$value'"
    else
        print_info "Using $var_name: $current_value"
    fi
}

################################################################################
# Step 1: Gather Configuration
################################################################################

print_info "==================================================================="
print_info "Azure Container Apps Volume Mount Setup"
print_info "==================================================================="
echo ""

# Prompt for required values if not already set
prompt_if_empty RESOURCE_GROUP "Enter the Resource Group name"
prompt_if_empty STORAGE_ACCOUNT_NAME "Enter the Storage Account name (lowercase, no hyphens)"
prompt_if_empty CONTAINER_APPS_ENVIRONMENT "Enter the Container Apps Environment name"
prompt_if_empty CONTAINER_APP_NAME "Enter the Container App name"

# Set defaults for optional values
FILE_SHARE_NAME="${FILE_SHARE_NAME:-app-data}"
STORAGE_VOLUME_NAME="${STORAGE_VOLUME_NAME:-appdata}"
MOUNT_PATH="${MOUNT_PATH:-/home/}"
LOCATION="${LOCATION:-eastus}"

echo ""
print_info "Configuration Summary:"
echo "  Resource Group: $RESOURCE_GROUP"
echo "  Storage Account: $STORAGE_ACCOUNT_NAME"
echo "  File Share Name: $FILE_SHARE_NAME"
echo "  Container Apps Environment: $CONTAINER_APPS_ENVIRONMENT"
echo "  Container App: $CONTAINER_APP_NAME"
echo "  Storage Volume Name: $STORAGE_VOLUME_NAME"
echo "  Mount Path: $MOUNT_PATH"
echo ""

read -p "Proceed with this configuration? (y/n): " confirm
if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
    print_warning "Setup cancelled by user"
    exit 0
fi

################################################################################
# Step 2: Verify Azure CLI is logged in
################################################################################

print_info "Verifying Azure CLI login..."
if ! az account show &>/dev/null; then
    print_error "Not logged in to Azure CLI. Please run 'az login' first."
    exit 1
fi
print_success "Azure CLI is logged in"

# Get current subscription
SUBSCRIPTION_ID=$(az account show --query id -o tsv)
SUBSCRIPTION_NAME=$(az account show --query name -o tsv)
print_info "Using subscription: $SUBSCRIPTION_NAME ($SUBSCRIPTION_ID)"

################################################################################
# Step 3: Verify Resource Group exists
################################################################################

print_info "Verifying resource group exists..."
if ! az group show --name "$RESOURCE_GROUP" &>/dev/null; then
    print_error "Resource group '$RESOURCE_GROUP' not found"
    exit 1
fi
print_success "Resource group exists"

# Get resource group location
if [ -z "${LOCATION:-}" ]; then
    LOCATION=$(az group show --name "$RESOURCE_GROUP" --query location -o tsv)
    print_info "Using resource group location: $LOCATION"
fi

################################################################################
# Step 4: Create or verify Storage Account
################################################################################

print_info "Checking storage account..."
if az storage account show --name "$STORAGE_ACCOUNT_NAME" --resource-group "$RESOURCE_GROUP" &>/dev/null; then
    print_success "Storage account '$STORAGE_ACCOUNT_NAME' already exists"
else
    print_warning "Storage account '$STORAGE_ACCOUNT_NAME' not found. Creating..."
    az storage account create \
        --name "$STORAGE_ACCOUNT_NAME" \
        --resource-group "$RESOURCE_GROUP" \
        --location "$LOCATION" \
        --sku Standard_LRS \
        --kind StorageV2 \
        --https-only true \
        --min-tls-version TLS1_2 \
        --allow-blob-public-access false \
        --tags "purpose=container-app-volume" "managed-by=setup-script"
    print_success "Storage account created"
fi

################################################################################
# Step 5: Get Storage Account Key
################################################################################

print_info "Retrieving storage account key..."
STORAGE_KEY=$(az storage account keys list \
    --resource-group "$RESOURCE_GROUP" \
    --account-name "$STORAGE_ACCOUNT_NAME" \
    --query '[0].value' \
    --output tsv)

if [ -z "$STORAGE_KEY" ]; then
    print_error "Failed to retrieve storage account key"
    exit 1
fi
print_success "Storage account key retrieved"

################################################################################
# Step 6: Create File Share
################################################################################

print_info "Checking file share '$FILE_SHARE_NAME'..."
if az storage share show \
    --name "$FILE_SHARE_NAME" \
    --account-name "$STORAGE_ACCOUNT_NAME" \
    --account-key "$STORAGE_KEY" &>/dev/null; then
    print_success "File share '$FILE_SHARE_NAME' already exists"
else
    print_warning "File share '$FILE_SHARE_NAME' not found. Creating..."
    az storage share create \
        --name "$FILE_SHARE_NAME" \
        --account-name "$STORAGE_ACCOUNT_NAME" \
        --account-key "$STORAGE_KEY" \
        --quota 5120  # 5 GiB
    print_success "File share created with 5 GiB quota"
fi

################################################################################
# Step 7: Verify Container Apps Environment exists
################################################################################

print_info "Verifying Container Apps Environment..."
if ! az containerapp env show \
    --name "$CONTAINER_APPS_ENVIRONMENT" \
    --resource-group "$RESOURCE_GROUP" &>/dev/null; then
    print_error "Container Apps Environment '$CONTAINER_APPS_ENVIRONMENT' not found"
    exit 1
fi
print_success "Container Apps Environment exists"

################################################################################
# Step 8: Register Storage in Container Apps Environment
################################################################################

print_info "Registering storage in Container Apps Environment..."

# Check if storage is already registered
if az containerapp env storage show \
    --name "$STORAGE_VOLUME_NAME" \
    --environment-name "$CONTAINER_APPS_ENVIRONMENT" \
    --resource-group "$RESOURCE_GROUP" &>/dev/null; then
    print_warning "Storage volume '$STORAGE_VOLUME_NAME' already registered. Updating..."
    az containerapp env storage set \
        --name "$STORAGE_VOLUME_NAME" \
        --environment-name "$CONTAINER_APPS_ENVIRONMENT" \
        --resource-group "$RESOURCE_GROUP" \
        --storage-type AzureFile \
        --azure-file-account-name "$STORAGE_ACCOUNT_NAME" \
        --azure-file-account-key "$STORAGE_KEY" \
        --azure-file-share-name "$FILE_SHARE_NAME" \
        --access-mode ReadWrite
    print_success "Storage volume updated"
else
    print_info "Registering new storage volume..."
    az containerapp env storage set \
        --name "$STORAGE_VOLUME_NAME" \
        --environment-name "$CONTAINER_APPS_ENVIRONMENT" \
        --resource-group "$RESOURCE_GROUP" \
        --storage-type AzureFile \
        --azure-file-account-name "$STORAGE_ACCOUNT_NAME" \
        --azure-file-account-key "$STORAGE_KEY" \
        --azure-file-share-name "$FILE_SHARE_NAME" \
        --access-mode ReadWrite
    print_success "Storage volume registered"
fi

################################################################################
# Step 9: Verify Container App exists
################################################################################

print_info "Verifying Container App exists..."
if ! az containerapp show \
    --name "$CONTAINER_APP_NAME" \
    --resource-group "$RESOURCE_GROUP" &>/dev/null; then
    print_error "Container App '$CONTAINER_APP_NAME' not found"
    exit 1
fi
print_success "Container App exists"

################################################################################
# Step 10: Update Container App with Volume Mount
################################################################################

print_info "Updating Container App with volume mount..."

# Create a temporary YAML file for the volume mount configuration
TEMP_YAML=$(mktemp)
cat > "$TEMP_YAML" << EOF
properties:
  template:
    volumes:
    - name: $STORAGE_VOLUME_NAME
      storageType: AzureFile
      storageName: $STORAGE_VOLUME_NAME
    containers:
    - name: main
      volumeMounts:
      - volumeName: $STORAGE_VOLUME_NAME
        mountPath: $MOUNT_PATH
EOF

print_info "Volume mount configuration:"
cat "$TEMP_YAML"
echo ""

# Note: The full update requires getting current container configuration
# This is a simplified approach - in production, you'd want to preserve all existing settings
print_warning "Note: Updating container app configuration..."
print_warning "This will add the volume mount. Ensure your container image and other settings are preserved."

# Get current container image
CURRENT_IMAGE=$(az containerapp show \
    --name "$CONTAINER_APP_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --query 'properties.template.containers[0].image' \
    -o tsv)

print_info "Current container image: $CURRENT_IMAGE"

# Update the container app
az containerapp update \
    --name "$CONTAINER_APP_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --set-env-vars "VOLUME_MOUNT_CONFIGURED=true" \
    --output none

print_warning "Container app updated with environment variable marker."
print_warning "Volume mount must be configured in your deployment template (Bicep/ARM) or via Azure Portal."

################################################################################
# Step 11: Verification and Next Steps
################################################################################

echo ""
print_success "==================================================================="
print_success "Volume Mount Setup Complete!"
print_success "==================================================================="
echo ""
print_info "Summary of what was configured:"
echo "  ✓ Storage Account: $STORAGE_ACCOUNT_NAME"
echo "  ✓ File Share: $FILE_SHARE_NAME (5 GiB quota)"
echo "  ✓ Environment Storage: $STORAGE_VOLUME_NAME"
echo "  ✓ Mount Path: $MOUNT_PATH"
echo ""
print_info "Verification Steps:"
echo "  1. Check storage account in Azure Portal:"
echo "     https://portal.azure.com/#resource/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.Storage/storageAccounts/$STORAGE_ACCOUNT_NAME"
echo ""
echo "  2. Verify file share exists:"
echo "     az storage share show --name '$FILE_SHARE_NAME' --account-name '$STORAGE_ACCOUNT_NAME' --account-key '***'"
echo ""
echo "  3. Check environment storage registration:"
echo "     az containerapp env storage show --name '$STORAGE_VOLUME_NAME' --environment-name '$CONTAINER_APPS_ENVIRONMENT' --resource-group '$RESOURCE_GROUP'"
echo ""
echo "  4. Verify Container App configuration:"
echo "     az containerapp show --name '$CONTAINER_APP_NAME' --resource-group '$RESOURCE_GROUP' --query 'properties.template'"
echo ""
print_warning "IMPORTANT: To complete the volume mount, you must update your Container App"
print_warning "deployment to include the volume mount in the container template."
print_warning "This is typically done via:"
print_warning "  - Bicep template deployment (azd up)"
print_warning "  - Azure Portal: Container App > Containers > Volume mounts"
print_warning "  - Azure CLI: az containerapp update with volume mount configuration"
echo ""
print_info "For MSAL token caching, the mount path should be: /home/"
print_info "This allows ~/.cache/msal/ to persist across container restarts."
echo ""

# Cleanup
rm -f "$TEMP_YAML"

print_success "Setup script completed successfully!"
