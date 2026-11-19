# Terraform Configurations

## AWS (Primary)

Deploy OpenContext to AWS Lambda. See [Deployment Guide](../docs/DEPLOYMENT.md).

### First-time setup: Bootstrap backend

The AWS configuration uses S3 for state storage. Run the bootstrap **once** to create the backend resources. See [bootstrap/README.md](bootstrap/README.md) for details:

```bash
cd bootstrap
terraform init
terraform apply
```

### Deploy OpenContext

Use the main deploy script (recommended):

```bash
./scripts/deploy.sh
```

Or deploy manually:

```bash
cd aws
terraform init
terraform workspace select anchorage-gis-prod
terraform plan -var-file=prod.tfvars -out=tfplan
terraform apply tfplan
```

Use `./scripts/deploy.sh -e prod`: it repackages the Lambda, selects the workspace and passes `prod.tfvars`. A bare `terraform plan` without `-var-file=prod.tfvars` now stops on the missing `lambda_name` / `stage_name` instead of planning against other names.

**Note:** The deploy script copies `config.yaml` into `terraform/aws/` before running Terraform. For manual runs from `terraform/aws/`, use `-var="config_file=../../config.yaml"` if running from project root.

### Alternative: Per-account backend

Use [scripts/setup-backend.sh](../scripts/setup-backend.sh) to create a per-account S3 bucket and DynamoDB table, then generate `terraform/aws/backend.tf` with custom bucket name. Use this if you need a separate state bucket per AWS account.

## Other Clouds

- **GCP:** [gcp/](gcp/) – Coming soon
- **Azure:** [azure/](azure/) – Coming soon
