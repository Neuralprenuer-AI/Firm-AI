#!/usr/bin/env bash
# =====================================================================
# setup_cloudwatch_alarms.sh
#
# Creates CloudWatch alarms for every firmos-* Lambda in AI Firm OS.
# All AWS CLI calls are SINGLE-LINE (no backslash line continuations).
# Idempotent: re-running the script will NOT fail.
#
# Prereqs:
#   * AWS CLI v2 configured with credentials for account 006619321854
#   * IAM permissions: sns:*, cloudwatch:PutMetricAlarm, sts:GetCallerIdentity
#
# Alarms created per Lambda:
#   firmos-{name}-errors     AWS/Lambda Errors    >= 5  in 5 min
#   firmos-{name}-throttles  AWS/Lambda Throttles >= 10 in 5 min
#
# Composite / proxy alarms:
#   firmos-budget-warning  (firmos-intake-agent Duration > 55000 ms)
#
# SNS topic: firmos-alerts  (subscribe your email after first run)
# =====================================================================

set -euo pipefail

# ---------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------
AWS_REGION="us-east-2"
AWS_ACCOUNT_ID="006619321854"
SNS_TOPIC_NAME="firmos-alerts"

LAMBDAS=(
    "firmos-sms-webhook"
    "firmos-sms-router"
    "firmos-intake-agent"
    "firmos-status-bot"
    "firmos-escalation"
    "firmos-twilio-send"
    "firmos-crud"
    "firmos-whoami"
    "firmos-audit-digest"
)

# Thresholds (per Phase 1 SLO)
ERROR_THRESHOLD=5        # errors in 5 min window
THROTTLE_THRESHOLD=10    # throttles in 5 min window
PERIOD_SECONDS=300       # 5 minutes
DURATION_BUDGET_MS=55000 # proxy for timeout approach (60s timeout)

# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
log() {
    printf '[%s] %s\n' "$(date +'%Y-%m-%dT%H:%M:%S%z')" "$*"
}

require_cli() {
    if ! command -v aws >/dev/null 2>&1; then
        echo "ERROR: aws CLI not found on PATH" >&2
        exit 1
    fi
}

verify_account() {
    local caller_account
    caller_account="$(aws sts get-caller-identity --query 'Account' --output text)"
    if [[ "${caller_account}" != "${AWS_ACCOUNT_ID}" ]]; then
        echo "ERROR: AWS CLI is pointed at account ${caller_account}, expected ${AWS_ACCOUNT_ID}" >&2
        exit 1
    fi
    log "Verified AWS account ${caller_account} in region ${AWS_REGION}"
}

# ---------------------------------------------------------------------
# 1. Create SNS topic (idempotent — create-topic returns existing ARN)
# ---------------------------------------------------------------------
create_sns_topic() {
    log "Creating/ensuring SNS topic: ${SNS_TOPIC_NAME}"
    SNS_TOPIC_ARN="$(aws sns create-topic --name "${SNS_TOPIC_NAME}" --region "${AWS_REGION}" --query 'TopicArn' --output text)"
    if [[ -z "${SNS_TOPIC_ARN}" || "${SNS_TOPIC_ARN}" == "None" ]]; then
        echo "ERROR: failed to create or fetch SNS topic ARN" >&2
        exit 1
    fi
    log "SNS topic ARN: ${SNS_TOPIC_ARN}"
}

# ---------------------------------------------------------------------
# 2. Per-Lambda alarms (Errors + Throttles)
# ---------------------------------------------------------------------
create_lambda_alarms() {
    local lambda_name="$1"
    local err_alarm="${lambda_name}-errors"
    local thr_alarm="${lambda_name}-throttles"

    # Errors alarm — single-line AWS CLI
    aws cloudwatch put-metric-alarm --region "${AWS_REGION}" --alarm-name "${err_alarm}" --alarm-description "Lambda ${lambda_name} error rate >= ${ERROR_THRESHOLD} in ${PERIOD_SECONDS}s" --namespace "AWS/Lambda" --metric-name "Errors" --statistic "Sum" --period "${PERIOD_SECONDS}" --evaluation-periods 1 --threshold "${ERROR_THRESHOLD}" --comparison-operator "GreaterThanOrEqualToThreshold" --treat-missing-data "notBreaching" --dimensions "Name=FunctionName,Value=${lambda_name}" --alarm-actions "${SNS_TOPIC_ARN}" --ok-actions "${SNS_TOPIC_ARN}"
    log "Created alarm: ${err_alarm}"

    # Throttles alarm — single-line AWS CLI
    aws cloudwatch put-metric-alarm --region "${AWS_REGION}" --alarm-name "${thr_alarm}" --alarm-description "Lambda ${lambda_name} throttles >= ${THROTTLE_THRESHOLD} in ${PERIOD_SECONDS}s" --namespace "AWS/Lambda" --metric-name "Throttles" --statistic "Sum" --period "${PERIOD_SECONDS}" --evaluation-periods 1 --threshold "${THROTTLE_THRESHOLD}" --comparison-operator "GreaterThanOrEqualToThreshold" --treat-missing-data "notBreaching" --dimensions "Name=FunctionName,Value=${lambda_name}" --alarm-actions "${SNS_TOPIC_ARN}" --ok-actions "${SNS_TOPIC_ARN}"
    log "Created alarm: ${thr_alarm}"
}

# ---------------------------------------------------------------------
# 3. Budget-proxy alarm (intake-agent Duration approaches timeout)
# ---------------------------------------------------------------------
create_budget_alarm() {
    local budget_alarm="firmos-budget-warning"
    local target_lambda="firmos-intake-agent"

    # Duration > 55000 ms on firmos-intake-agent — single-line
    aws cloudwatch put-metric-alarm --region "${AWS_REGION}" --alarm-name "${budget_alarm}" --alarm-description "firmos-intake-agent duration approaching 60s timeout (> ${DURATION_BUDGET_MS} ms). Proxy for cost runaway / model stalls." --namespace "AWS/Lambda" --metric-name "Duration" --statistic "Maximum" --period "${PERIOD_SECONDS}" --evaluation-periods 1 --threshold "${DURATION_BUDGET_MS}" --comparison-operator "GreaterThanThreshold" --treat-missing-data "notBreaching" --dimensions "Name=FunctionName,Value=${target_lambda}" --alarm-actions "${SNS_TOPIC_ARN}" --ok-actions "${SNS_TOPIC_ARN}"
    log "Created alarm: ${budget_alarm}"
}

# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------
main() {
    require_cli
    verify_account
    create_sns_topic

    for lambda_name in "${LAMBDAS[@]}"; do
        create_lambda_alarms "${lambda_name}"
    done

    create_budget_alarm

    echo
    echo "====================================================================="
    echo "CloudWatch alarms setup complete."
    echo "SNS topic ARN: ${SNS_TOPIC_ARN}"
    echo
    echo "NEXT STEP — subscribe your email to receive alerts:"
    echo "  aws sns subscribe --region ${AWS_REGION} --topic-arn ${SNS_TOPIC_ARN} --protocol email --notification-endpoint you@example.com"
    echo
    echo "Then confirm the subscription link delivered to that inbox."
    echo "====================================================================="
}

main "$@"
