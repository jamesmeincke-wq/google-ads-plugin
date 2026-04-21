# Google Ads Manager Skill Documentation

Python CLI for full Google Ads account management: reporting, campaign/ad/keyword management, budget automation rules, auction insights, quality scores, and more.

## Setup Requirements

Before any command, check that `.env` exists in the project directory. If missing or incomplete, run `/google-ads-setup`.

Required credentials:
- `GOOGLE_ADS_CLIENT_ID` — OAuth client ID
- `GOOGLE_ADS_CLIENT_SECRET` — OAuth client secret
- `GOOGLE_ADS_REFRESH_TOKEN` — Long-lived refresh token
- `GOOGLE_ADS_DEVELOPER_TOKEN` — From Google Ads API Center
- `GOOGLE_ADS_CUSTOMER_ID` — Default 10-digit account ID (no dashes)

Optional: `GOOGLE_ADS_LOGIN_CUSTOMER_ID` (manager/MCC accounts only)

## Critical Notes

- **Customer ID**: Always 10 digits, no dashes (e.g. `5157364662` not `515-736-4662`)
- **Micros**: All monetary values use micros — multiply display value × 1,000,000 (e.g. €50 budget = 50000000 micros)
- **Dates**: Use explicit date ranges, not literals like LAST_30_DAYS
- **New campaigns/ads**: Always created PAUSED — enable separately after review
- **Keywords**: Text and match type are immutable after creation. To change keyword text, delete and recreate.

## Command Reference

Invoke via: `bash ${CLAUDE_PLUGIN_ROOT}/skills/google-ads-manager/google-ads [command] [args]`

### Account / Discovery
```
list-accounts
list-clients --customer-id ID
account-currency --customer-id ID
```

### Reporting
```
campaign-performance --customer-id ID [--days 30] [--campaign-id ID]
ad-performance       --customer-id ID [--days 30] [--campaign-id ID]
keyword-performance  --customer-id ID [--days 30] [--campaign-id ID] [--limit 100]
search-terms         --customer-id ID [--days 30] [--campaign-id ID] [--min-impressions 10] [--limit 200]
geo-performance      --customer-id ID [--days 30] [--campaign-id ID] [--limit 100]
auction-insights     --customer-id ID [--days 30] [--campaign-id ID]
quality-scores       --customer-id ID [--campaign-id ID] [--limit 200]
budget-pacing        --customer-id ID
change-history       --customer-id ID [--days 7] [--limit 100]
ad-creatives         --customer-id ID [--campaign-id ID]
image-assets         --customer-id ID
query                --customer-id ID --query "SELECT ..." [--format table|json|csv]
```

### Campaign Management
```
create-campaign-budget --customer-id ID --name "Name" --amount-micros 50000000
create-campaign        --customer-id ID --name "Name" --budget-resource "customers/.../campaignBudgets/ID" [--type SEARCH]
update-campaign-status --customer-id ID --campaign-id ID --status ENABLED|PAUSED|REMOVED
update-budget          --customer-id ID --budget-id ID --amount-micros 60000000
```

### Ad Group Management
```
create-ad-group --customer-id ID --campaign-id ID --name "Name" [--cpc-bid-micros 1500000]
update-ad-group --customer-id ID --ad-group-id ID [--name "New Name"] [--status ENABLED|PAUSED] [--cpc-bid-micros 2000000]
```

### Ad Management
```
create-rsa        --customer-id ID --ad-group-id ID --headlines '["H1","H2","H3"]' --descriptions '["D1","D2"]' --final-url "https://..."
update-ad-status  --customer-id ID --ad-group-id ID --ad-id ID --status ENABLED|PAUSED|REMOVED
update-rsa        --customer-id ID --ad-group-id ID --ad-id ID [--headlines '["H1","H2"]'] [--descriptions '["D1","D2"]']
duplicate-ad      --customer-id ID --ad-group-id ID --ad-id ID [--target-ad-group-id ID]
```

### Keyword Management
```
add-keywords           --customer-id ID --ad-group-id ID --keywords '[{"text":"kw","match_type":"EXACT"}]'
add-negative-keywords  --customer-id ID --ad-group-id ID --keywords '["negative term"]'
update-keyword-status  --customer-id ID --ad-group-id ID --criterion-id ID --status ENABLED|PAUSED
update-keyword-bid     --customer-id ID --ad-group-id ID --criterion-id ID --cpc-bid-micros 2000000
bulk-update-bids       --customer-id ID --updates '[{"ad_group_id":"ID","criterion_id":"ID","cpc_bid_micros":2000000}]'
```

### Extensions
```
add-sitelink    --customer-id ID --campaign-id ID --text "Free Trial" --url "https://..." [--desc1 "..."] [--desc2 "..."]
add-callout     --customer-id ID --campaign-id ID --text "No Setup Fee"
list-extensions --customer-id ID --campaign-id ID
```

### Conversions
```
list-conversions  --customer-id ID
create-conversion --customer-id ID --name "Demo Request" --category REQUEST_DEMO|LEAD|PURCHASE|SIGNUP|PAGE_VIEW|DOWNLOAD|OTHER
```

### Ad Scheduling (Dayparting)
```
set-schedule --customer-id ID --campaign-id ID --schedules '[{"day_of_week":"MONDAY","start_hour":8,"end_hour":18}]'
```
Days: MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY, SATURDAY, SUNDAY

### Rules Engine (Automation)
Rules monitor metrics and automatically take action when conditions are met.

```
create-rule --customer-id ID --name "Name" \
  --metric cpa|spend|ctr|conversions|clicks|impressions \
  --operator less_than|greater_than \
  --threshold VALUE \
  [--lookback-days 7] \
  --action-type increase_budget|decrease_budget|pause_campaign|enable_campaign|alert_only \
  [--entity-type account|campaign] [--entity-id CAMPAIGN_ID] \
  [--action-percent 20] [--max-budget 1000] [--min-budget 100] \
  [--cooldown-hours 24]

list-rules [--customer-id ID]
delete-rule --rule-id RULE_ID
evaluate-rules --customer-id ID [--dry-run]
```

**Rules workflow:**
1. Create rule with `create-rule`
2. Preview with `evaluate-rules --dry-run`
3. Run live with `evaluate-rules` (no --dry-run)

**Example rules:**
- CPA < €500 → increase budget 20% (max €1,000/day)
- CPA > €1,500 → decrease budget 15%
- Spend > €5,000 in 7 days → pause campaign
- Conversions < 5 in 14 days → alert only

Rules are stored in `google_ads_rules.json` in the project directory.

## Recommended Workflows

**First time:**
1. `list-accounts` — confirm accessible accounts
2. `account-currency` — confirm currency before interpreting micros
3. `campaign-performance` — get the lay of the land

**Keyword optimization:**
1. `keyword-performance` — find criterion IDs and current bids
2. `quality-scores` — find low-QS keywords to fix
3. `search-terms` — find negatives and new keyword candidates
4. `update-keyword-bid` or `bulk-update-bids` to act

**New campaign:**
1. `create-campaign-budget` → note the budget resource name
2. `create-campaign` with that budget → campaign starts PAUSED
3. `create-ad-group` → add ad groups
4. `create-rsa` → add ads (start PAUSED)
5. `add-keywords` → add keywords
6. `update-campaign-status --status ENABLED` when ready

**Daily check:**
1. `budget-pacing` — catch over/under spend
2. `campaign-performance --days 7` — recent trends
3. `evaluate-rules` — run automation rules
