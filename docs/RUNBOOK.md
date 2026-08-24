# Anchorage GIS MCP — Operations Runbook

Prod stack: Lambda `anchorage-gis-mcp-prod`, API Gateway `622f4qcew8`
(stage `prod`), us-west-2, account `420839047325`.
Public URL: `https://anchorage-gis.codeforanchorage.org/mcp`.

## 🔴 Kill switch (runaway traffic / cost)

Instantly stop ALL invocations (both `/mcp` and `/mcp-gcc` — same Lambda):

```bash
aws lambda put-function-concurrency \
  --function-name anchorage-gis-mcp-prod \
  --reserved-concurrent-executions 0
```

Every request now gets throttled at zero Lambda cost. Reverse it:

```bash
# restore the terraform-managed value (see prod.tfvars)
aws lambda put-function-concurrency \
  --function-name anchorage-gis-mcp-prod \
  --reserved-concurrent-executions 25
```

Note: the CLI change drifts from Terraform state; the next
`terraform apply` restores `lambda_reserved_concurrency` from
`prod.tfvars` either way.

Softer clamps (single-value tfvars flips + `terraform apply`, all
in-place):

| Lever | tfvars var | Effect |
|---|---|---|
| Aggregate rps (keyless /mcp) | `api_rate_limit` / `api_burst_limit` | stage-wide throttle |
| Per-IP rate | `waf_rate_limit_per_5min` | WAF rate rule |
| Compute ceiling | `lambda_reserved_concurrency` | max concurrent Lambdas |

## Traffic postures

| Var | Steady state | Raised (e.g. ESRI UC week) |
|---|---|---|
| `api_rate_limit` | 5 | 20 |
| `api_burst_limit` | 10 | 40 |
| `lambda_reserved_concurrency` | 10 | 25 |
| `waf_rate_limit_per_5min` | 300 | 600 |

**Current posture: STEADY STATE**, restored 2026-08-23. The ESRI UC
raise ran 2026-07-13 .. 2026-08-23.

To change posture, edit `terraform/aws/prod.tfvars` and deploy,
answering `yes` at the script's own confirm gate:

```bash
./scripts/deploy.sh -e prod
```

> **⚠ `waf_rate_limit_per_5min` no longer takes effect from this repo.**
> Since `use_shared_waf = true`, the per-IP limit is a Host-scoped rule
> in the fleet web ACL owned by **mcp-stats**. Change it there, in
> `fleet_waf_members` under key `anchorage-gis`, and apply in that repo.
> The value in `prod.tfvars` applies only on a rollback to a dedicated
> ACL.
>
> **Known gap as of 2026-08-23:** the fleet value is still **600** while
> this repo reads 300, so the live per-IP limit remains at the raised
> setting until mcp-stats is updated.

Worst-case cost at full 24/7 saturation: steady ≈ $17/day, UC posture
≈ $50/day. Realistic traffic is far below both (conversational MCP is
bursty, and throttles bite before spend does).

Key nuance: all claude.ai users arrive via ~5 Anthropic egress IPs
(`160.79.106.32/27`), so the per-IP WAF limit is effectively an
aggregate cap on claude.ai traffic. If users report 429s during a
spike, that limit and the stage throttle are the levers.

## Alerting inventory

- **CloudWatch alarms** (errors, throttles, duration>80%, apigw 5xx,
  4xx-probing) notify SNS
  `arn:aws:sns:us-west-2:420839047325:anchorage-gis-mcp-prod-alarms`
  → email. Managed via `alarm_sns_topic_arn` in `prod.tfvars`; the
  topic itself was created by CLI, outside Terraform.
- **AWS Budget** `mcp-fleet-monthly`: $100/mo, filtered on tag
  `Project=mcp-server` (stamped on this stack by provider
  `default_tags`). Alerts at $25 actual, $80 actual, $100 forecast.
  Tag-filtered costs accrue only from when the tag was applied
  (2026-07-13 for Lambda/APIGW/WAF here).
- **Cost Anomaly Detection**: SERVICE-dimension monitor, daily email
  when anomaly impact ≥ $10.
- The usage-plan quota (3,000 req/day) binds ONLY the `/mcp-gcc` API
  key — the keyless public `/mcp` is protected by the
  throttle/concurrency/WAF stack above, not the quota.

## Health / smoke

```bash
PYTHONIOENCODING=utf-8 python scripts/smoke_prod.py       # 13 checks vs prod
PYTHONIOENCODING=utf-8 python scripts/smoke_footprint.py  # footprint_for_parcel acceptance
```

Usage analytics (distinct users = `count_distinct(mcp_session_id)`,
never source IPs): see CloudWatch Insights queries against
`/aws/lambda/anchorage-gis-mcp-prod`.
