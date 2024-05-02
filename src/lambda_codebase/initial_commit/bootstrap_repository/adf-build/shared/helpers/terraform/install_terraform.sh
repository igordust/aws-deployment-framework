#!/usr/bin/env bash
set -e

echo "Installing terraform version ${TERRAFORM_VERSION}"
TF_STAGE=install bash adf-build/helpers/terraform/adf_terraform.sh