# CI/CD Setup: GitHub Actions + AWS OIDC

**Project:** Firm-AI Backend Lambdas  
**Region:** us-east-2  
**Account:** 006619321854  
**GitHub Repo:** Neuralprenuer-AI/Firm-AI

---

## 1. Create GitHub OIDC Identity Provider (One-time)

Creates the federated identity provider that GitHub Actions will use to assume an AWS role.

```bash
aws iam create-open-id-connect-provider --url https://token.actions.githubusercontent.com --client-id-list sts.amazonaws.com --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1 --region us-east-2
```

**Output:** Returns an ARN like `arn:aws:iam::006619321854:oidc-provider/token.actions.githubusercontent.com`

---

## 2. Create IAM Role: firmos-github-actions-role

Creates the role that GitHub Actions will assume via OIDC. Trust policy restricts to main branch only.

```bash
aws iam create-role --role-name firmos-github-actions-role --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Federated":"arn:aws:iam::006619321854:oidc-provider/token.actions.githubusercontent.com"},"Action":"sts:AssumeRoleWithWebIdentity","Condition":{"StringEquals":{"token.actions.githubusercontent.com:aud":"sts.amazonaws.com"},"StringLike":{"token.actions.githubusercontent.com:sub":"repo:Neuralprenuer-AI/Firm-AI:ref:refs/heads/main"}}}]}' --region us-east-2
```

---

## 3. Create and Attach Lambda Deployment Policy

Grants permissions to update Lambda function code and wait for updates to complete.

```bash
aws iam put-role-policy --role-name firmos-github-actions-role --policy-name FirmosLambdaDeployPolicy --policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["lambda:UpdateFunctionCode","lambda:GetFunction","lambda:GetFunctionConfiguration","lambda:ListFunctions"],"Resource":"arn:aws:lambda:us-east-2:006619321854:function:firmos-*"}]}' --region us-east-2
```

**Required Actions:**
- `lambda:UpdateFunctionCode` — deploy zip files
- `lambda:GetFunction` — verify deployment
- `lambda:GetFunctionConfiguration` — check function details
- `lambda:ListFunctions` — enumerate available Lambdas

---

## 4. Add GitHub Actions Secret

Store the role ARN as a repository secret for the workflow to reference.

1. Navigate to **GitHub Repository → Settings → Secrets and variables → Actions**
2. Click **New repository secret**
3. Name: `AWS_DEPLOY_ROLE_ARN`
4. Value: `arn:aws:iam::006619321854:role/firmos-github-actions-role`
5. Click **Add secret**

---

## 5. Verify Setup

### Test OIDC Authentication

```bash
# Make a commit to main branch
git add .
git commit -m "test: trigger CI/CD workflow"
git push origin main
```

### Monitor Workflow Execution

1. Go to **GitHub Repository → Actions**
2. Click the workflow run for your commit
3. Expand the "Deploy Lambdas to AWS" job
4. Verify all Lambda deployments succeeded

### Troubleshooting

**AssumeRoleWithWebIdentity Error:**
- Check CloudTrail: `aws cloudtrail lookup-events --lookup-attributes AttributeKey=EventName,AttributeValue=AssumeRoleWithWebIdentity --max-results 5 --region us-east-2`
- Verify trust policy includes `"repo:Neuralprenuer-AI/Firm-AI:ref:refs/heads/main"`

**Lambda Update Failed:**
- Check Lambda is in same region: `aws lambda list-functions --region us-east-2`
- Verify role can call `lambda:UpdateFunctionCode`: review IAM policy attachment
- Check CloudWatch Logs for Lambda execution errors

**OIDC Provider Outdated:**
- Update thumbprint if GitHub Actions fails: `aws iam list-open-id-connect-providers --region us-east-2`
- Current valid thumbprint: `6938fd4d98bab03faadb97b34396831e3780aea1`

---

## Workflows Overview

### `deploy.yml` — Production Deployment

**Trigger:** Push to `main` branch

**Process:**
1. Detect if shared files (`shared/*.py`) changed
2. If shared files changed → deploy ALL Lambdas
3. Otherwise → deploy only Lambdas with code changes
4. Zip each Lambda with shared modules and deploy via `lambda:UpdateFunctionCode`
5. Wait for deployment to complete before moving to next Lambda

**Lambda Functions Deployed:**
```
firmos-sms-webhook firmos-sms-router firmos-intake-agent 
firmos-status-bot firmos-escalation firmos-twilio-send 
firmos-crud firmos-whoami firmos-audit-digest 
firmos-clio-sync firmos-crm-push firmos-onboard-firm
```

### `test.yml` — Pre-merge Validation

**Trigger:** 
- Pull requests targeting `main`
- Push to branches matching `feature/**` or `fix/**`

**Process:**
1. Set up Python 3.11 environment
2. Cache pip dependencies
3. Run Ruff linting (GitHub annotations format)
4. Compile all Lambda function syntax
5. Run pytest test suite
6. Print summary of tested Lambdas

**Dependencies Installed:**
- pytest (test framework)
- ruff (linter)
- boto3, requests, twilio (AWS/HTTP/SMS)
- psycopg2-binary (PostgreSQL)

---

## Local Development

### Run Linting Locally

```bash
pip install ruff
ruff check lambdas/ shared/
```

### Run Tests Locally

```bash
pip install pytest boto3 requests twilio psycopg2-binary
pytest tests/ -v
```

### Deploy a Single Lambda Manually

```bash
LAMBDA_NAME="firmos-intake-agent"
TEMP_DIR="/tmp/$LAMBDA_NAME"
mkdir -p "$TEMP_DIR"
cp lambdas/$LAMBDA_NAME/lambda_function.py "$TEMP_DIR/"
cp shared/shared_*.py "$TEMP_DIR/"
cd "$TEMP_DIR"
zip -r "/tmp/$LAMBDA_NAME.zip" . -x "__pycache__/*" "*.pyc"
cd -
aws lambda update-function-code --function-name "$LAMBDA_NAME" --zip-file "fileb:///tmp/$LAMBDA_NAME.zip" --region us-east-2
```
