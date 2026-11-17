lambda_name = "anchorage-gis-mcp-prod"
stage_name  = "prod"
aws_region  = "us-west-2"
config_file = "config.yaml"
# 1024 MB: aggregate_by_polygon holds up to AGG_SOURCE_LIMIT source features in
# memory plus a bounded 32-entry polygon cache. Also buys more Lambda
# CPU, which accelerates the pure-Python point-in-polygon work.
lambda_memory = 1024
# Kept in sync with config.yaml, which WINS for this variable (main.tf reads
# `local.config.aws.lambda_timeout` first and only falls back to this var).
# 28s sits just under API Gateway's hard, non-adjustable 29s integration
# timeout so the Lambda self-terminates before the gateway gives up.
lambda_timeout  = 28
api_quota_limit = 3000
# Rate/burst feed BOTH the stage-wide throttle (the aggregate cap on the
# keyless public /mcp) and the usage-plan throttle (API-key traffic).
# Raised 5 / 10 -> 15 / 30 on 2026-11-17 for ~11 more attorneys onboarding
# via Claude.ai (same sizing as eCode's c6ca0e2). Earlier postures: 5 / 10
# steady state, 20 / 40 for the ESRI UC week; see docs/RUNBOOK.md.
api_rate_limit  = 15
api_burst_limit = 30
custom_domain   = "anchorage-gis.codeforanchorage.org"

# Cap concurrent Lambda executions. Cost and blast-radius protection if
# WAF is bypassed via distributed sources. Raised 10 -> 30 on 2026-11-17 for
# ~11 more attorneys onboarding via Claude.ai; the cap of 10 was hit on
# 2026-11-10 (8 throttles, 5xx alarm). Earlier: 10 steady state, 25 for the
# ESRI UC week. Bounds worst-case Lambda spend to ~30 GB-s/s ~= $43/day at
# full saturation. The Lambda Throttles alarm fires on any throttle here.
lambda_reserved_concurrency = 30

# WAF per-IP rate limit (rolling 5-minute window). The MCP tools are
# conversational, so 1 rps sustained per IP (~300/5min) is plenty for
# real users and tight enough to slow scrapers and denial-of-wallet probes.
# NOTE: ALL claude.ai users share ~5 Anthropic egress IPs (160.79.106.32/27),
# so this per-IP limit is effectively an aggregate cap on claude.ai traffic.
# Raised 300 -> 2000 on 2026-11-17 for that reason (earlier: 300 steady
# state, 600 for the ESRI UC week).
# NOTE: INERT while use_shared_waf = true -- it only takes effect on a
# rollback to a dedicated ACL. The LIVE per-IP limit lives in mcp-stats'
# `fleet_waf_members` under key `anchorage-gis` (2000 since mcp-stats
# 26f2caa); change it THERE and keep this value in agreement.
waf_rate_limit_per_5min = 2000

# CloudWatch alarms (errors, throttles, 5xx, probing, duration) notify this
# topic; email subscription on it. Empty string = alarms are dashboard-only.
alarm_sns_topic_arn = "arn:aws:sns:us-west-2:420839047325:anchorage-gis-mcp-prod-alarms"

# Hardened, API-key-gated /mcp-gcc route for an M365 GCC Copilot consumer.
# Kept enabled. The Copilot Studio connector isn't wired up yet, but the route
# + API key are live in prod and retained for when it is. (The buffering tools
# + instructions also ship on the public /mcp route, same Lambda.) Retrieve the
# key with: terraform output -raw gcc_api_key_value
enable_gcc_route = true

# Use the fleet-wide WAF instead of a dedicated ACL for this MCP. A dedicated
# ACL costs ~$8/mo in fixed AWS charges regardless of traffic; the shared ACL
# keeps this MCP's 2000/5min limit as its own counter, aggregated on
# (IP, Host) so it stays independent of the other MCPs sharing that limit.
#
# The effective limit now lives in mcp-stats' `fleet_waf_members` under the key
# `anchorage-gis` — change it there, not here. The rate-limit value above is retained
# so that rolling back (use_shared_waf = false) restores the original limit.
# See mcp-stats/docs/waf-consolidation.md.
use_shared_waf = true
