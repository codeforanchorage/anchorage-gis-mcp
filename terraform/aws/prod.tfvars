lambda_name = "anchorage-gis-mcp-prod"
stage_name  = "prod"
aws_region  = "us-west-2"
config_file = "config.yaml"
# 1024 MB: aggregate_by_polygon holds up to AGG_SOURCE_LIMIT source features in
# memory plus a bounded 32-entry polygon cache. Also buys more Lambda
# CPU, which accelerates the pure-Python point-in-polygon work.
lambda_memory   = 1024
lambda_timeout  = 120
api_quota_limit = 3000
# Rate/burst feed BOTH the stage-wide throttle (the aggregate cap on the
# keyless public /mcp) and the usage-plan throttle (API-key traffic).
# ESRI UC WEEK POSTURE (raised 2026-07-13 for conference traffic; see
# docs/RUNBOOK.md): 20 rps / 40 burst. Steady-state values: 5 / 10.
api_rate_limit  = 20
api_burst_limit = 40
custom_domain   = "anchorage-gis.codeforanchorage.org"

# Cap concurrent Lambda executions. Cost and blast-radius protection if
# WAF is bypassed via distributed sources. Conversational MCP traffic does
# not need horizontal scale; raise if legitimate users start getting throttled.
# ESRI UC WEEK POSTURE: 25 (steady-state: 10). Bounds worst-case Lambda
# spend to ~25 GB-s/s ~= $36/day even at full saturation.
lambda_reserved_concurrency = 25

# WAF per-IP rate limit (rolling 5-minute window). The MCP tools are
# conversational, so 1 rps sustained per IP (~300/5min) is plenty for
# real users and tight enough to slow scrapers and denial-of-wallet probes.
# NOTE: ALL claude.ai users share ~5 Anthropic egress IPs (160.79.106.32/27),
# so this per-IP limit is effectively an aggregate cap on claude.ai traffic.
# ESRI UC WEEK POSTURE: 600 (steady-state: 300).
waf_rate_limit_per_5min = 600

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
# keeps this MCP's 600/5min limit as its own counter, aggregated on
# (IP, Host) so it stays independent of the other MCPs sharing that limit.
#
# The effective limit now lives in mcp-stats' `fleet_waf_members` under the key
# `anchorage-gis` — change it there, not here. The rate-limit value above is retained
# so that rolling back (use_shared_waf = false) restores the original limit.
# See mcp-stats/docs/waf-consolidation.md.
use_shared_waf = true
