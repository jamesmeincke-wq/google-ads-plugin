#!/usr/bin/env python3
"""
Google Ads Manager CLI — all commands in one file.
Invoked by the 'google-ads' bash wrapper.
"""

import argparse
import json
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import requests
from google_ads_api import GoogleAdsClient, format_customer_id, BASE_URL, API_VERSION

# Rules persist in the project's working directory (locally-mounted Cowork folder)
RULES_FILE = Path.cwd() / "google_ads_rules.json"

VALID_METRICS = ("cpa", "spend", "ctr", "conversions", "clicks", "impressions")
VALID_OPERATORS = ("less_than", "greater_than")
VALID_ACTIONS = ("increase_budget", "decrease_budget", "pause_campaign", "enable_campaign", "alert_only")

# ─── Rules helpers ────────────────────────────────────────────────────────────

def _load_rules() -> dict:
    if not RULES_FILE.exists():
        return {"rules": []}
    with open(RULES_FILE) as f:
        return json.load(f)


def _save_rules(data: dict):
    with open(RULES_FILE, "w") as f:
        json.dump(data, f, indent=2)


def _query_metric_value(client, customer_id, metric, entity_type, entity_id, lookback_days):
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    field_map = {
        "cpa": "metrics.cost_per_conversion",
        "spend": "metrics.cost_micros",
        "ctr": "metrics.ctr",
        "conversions": "metrics.conversions",
        "clicks": "metrics.clicks",
        "impressions": "metrics.impressions",
    }
    field = field_map[metric]
    where = [f"segments.date BETWEEN '{start_date}' AND '{end_date}'"]
    if entity_type == "campaign" and entity_id:
        where.append(f"campaign.id = {entity_id}")
    query = f"SELECT {field} FROM campaign WHERE {' AND '.join(where)}"
    _, resp = client.search(customer_id, query)
    if resp.status_code != 200:
        return None
    rows = resp.json().get("results", [])
    if not rows:
        return None
    total = 0.0
    count = 0
    for r in rows:
        m = r.get("metrics", {})
        if metric == "cpa":
            val = m.get("costPerConversion")
            if val:
                total += int(val) / 1_000_000
                count += 1
        elif metric == "spend":
            total += int(m.get("costMicros", 0)) / 1_000_000
            count += 1
        elif metric == "ctr":
            total += float(m.get("ctr", 0)) * 100
            count += 1
        else:
            total += float(m.get(metric, 0))
            count += 1
    if metric in ("cpa", "ctr") and count > 0:
        return total / count
    return total


def _get_campaign_budget_info(client, customer_id, campaign_id):
    query = f"""
        SELECT campaign_budget.resource_name, campaign_budget.amount_micros
        FROM campaign
        WHERE campaign.id = {campaign_id}
        LIMIT 1
    """
    _, resp = client.search(customer_id, query)
    if resp.status_code != 200:
        return None
    rows = resp.json().get("results", [])
    if not rows:
        return None
    budget = rows[0].get("campaignBudget", {})
    return {
        "resource_name": budget.get("resourceName"),
        "amount_micros": int(budget.get("amountMicros", 0))
    }


def _execute_rule_action(client, customer_id, rule, dry_run):
    action = rule["action"]
    action_type = action["type"]
    entity_id = rule.get("entity_id")
    fid = format_customer_id(customer_id)

    if action_type == "alert_only":
        return "ALERT: condition met (alert_only rule — no automated action)"

    if action_type in ("pause_campaign", "enable_campaign"):
        new_status = "PAUSED" if action_type == "pause_campaign" else "ENABLED"
        if dry_run:
            return f"[DRY RUN] Would set campaign {entity_id} status to {new_status}"
        _, resp = client.mutate(customer_id, [{
            "campaignOperation": {
                "update": {
                    "resourceName": f"customers/{fid}/campaigns/{entity_id}",
                    "status": new_status
                },
                "updateMask": "status"
            }
        }])
        if resp.status_code != 200:
            return f"Error: {resp.text}"
        return f"Campaign {entity_id} status → {new_status}"

    if action_type in ("increase_budget", "decrease_budget"):
        budget_info = _get_campaign_budget_info(client, customer_id, entity_id)
        if not budget_info or not budget_info.get("resource_name"):
            return f"Error: could not retrieve budget for campaign {entity_id}"
        current_micros = budget_info["amount_micros"]
        percent = action.get("percent_change", 10)
        if action_type == "increase_budget":
            new_micros = int(current_micros * (1 + percent / 100))
            cap = action.get("max_budget_micros")
            if cap and new_micros > cap:
                new_micros = cap
        else:
            new_micros = int(current_micros * (1 - percent / 100))
            floor = action.get("min_budget_micros")
            if floor and new_micros < floor:
                new_micros = floor
        if new_micros <= 0:
            return f"Error: calculated budget ({new_micros}) is zero or negative"
        old_val = current_micros / 1_000_000
        new_val = new_micros / 1_000_000
        direction = "increased" if action_type == "increase_budget" else "decreased"
        if dry_run:
            return f"[DRY RUN] Would {direction} budget {old_val:.2f} → {new_val:.2f} ({percent}%)"
        _, resp = client.mutate(customer_id, [{
            "campaignBudgetOperation": {
                "update": {
                    "resourceName": budget_info["resource_name"],
                    "amountMicros": str(new_micros)
                },
                "updateMask": "amountMicros"
            }
        }])
        if resp.status_code != 200:
            return f"Error updating budget: {resp.text}"
        return f"Budget {direction}: {old_val:.2f} → {new_val:.2f} ({percent}%)"

    return f"Unknown action type: {action_type}"


# ─── Account / Discovery ──────────────────────────────────────────────────────

def cmd_list_accounts(client, args):
    resp = client.list_accessible_customers()
    if resp.status_code != 200:
        print(f"Error: {resp.text}")
        return
    customers = resp.json().get("resourceNames", [])
    if not customers:
        print("No accessible accounts found.")
        return
    print("Accessible Google Ads Accounts:")
    print("-" * 40)
    for rn in customers:
        print(f"  Account ID: {rn.split('/')[-1]}")


def cmd_list_clients(client, args):
    fid = format_customer_id(args.customer_id)
    query = """
        SELECT customer_client.client_customer, customer_client.descriptive_name,
               customer_client.currency_code, customer_client.manager, customer_client.status
        FROM customer_client
        WHERE customer_client.level = 1
    """
    _, resp = client.search(fid, query)
    if resp.status_code != 200:
        print(f"Error: {resp.text}")
        return
    results = resp.json().get("results", [])
    if not results:
        print(f"No client accounts found under {fid}.")
        return
    print(f"Client Accounts under {fid}:")
    print("-" * 60)
    for r in results:
        cc = r.get("customerClient", {})
        cid = cc.get("clientCustomer", "").split("/")[-1]
        name = cc.get("descriptiveName", "")
        currency = cc.get("currencyCode", "")
        is_manager = cc.get("manager", False)
        status = cc.get("status", "")
        print(f"  {cid:>12}  {name:<35}  {currency}  {'MCC' if is_manager else 'Client'}  {status}")


def cmd_account_currency(client, args):
    fid = format_customer_id(args.customer_id)
    query = "SELECT customer.currency_code, customer.descriptive_name FROM customer LIMIT 1"
    _, resp = client.search(fid, query)
    if resp.status_code != 200:
        print(f"Error: {resp.text}")
        return
    results = resp.json().get("results", [])
    if results:
        c = results[0].get("customer", {})
        print(f"Account: {c.get('descriptiveName', fid)}")
        print(f"Currency: {c.get('currencyCode', 'Unknown')}")


# ─── Reporting ────────────────────────────────────────────────────────────────

def cmd_campaign_performance(client, args):
    fid = format_customer_id(args.customer_id)
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d")
    campaign_filter = f"AND campaign.id = {args.campaign_id}" if getattr(args, 'campaign_id', None) else ""
    query = f"""
        SELECT
            campaign.id, campaign.name, campaign.status,
            metrics.impressions, metrics.clicks, metrics.cost_micros,
            metrics.conversions, metrics.average_cpc, metrics.ctr,
            metrics.cost_per_conversion
        FROM campaign
        WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
          AND campaign.status != 'REMOVED'
          {campaign_filter}
        ORDER BY metrics.cost_micros DESC
        LIMIT 50
    """
    _, resp = client.search(fid, query)
    if resp.status_code != 200:
        print(f"Error: {resp.text}")
        return
    results = resp.json().get("results", [])
    if not results:
        print(f"No campaign data for the last {args.days} days.")
        return
    print(f"Campaign Performance — Account {fid} | Last {args.days} days ({start_date} to {end_date})")
    print("=" * 140)
    print(f"{'ID':<12} {'Campaign':<35} {'Status':<10} {'Impr':>9} {'Clicks':>7} {'CTR':>6} {'Spend':>10} {'Conv':>6} {'CPA':>10} {'Avg CPC':>9}")
    print("-" * 140)
    total_spend = total_conv = total_clicks = total_impr = 0.0
    for r in results:
        camp = r.get("campaign", {})
        m = r.get("metrics", {})
        camp_id = camp.get("id", "")
        name = camp.get("name", "")[:34]
        status = camp.get("status", "")[:9]
        impr = int(m.get("impressions", 0))
        clicks = int(m.get("clicks", 0))
        ctr = float(m.get("ctr", 0)) * 100
        spend = int(m.get("costMicros", 0)) / 1_000_000
        conv = float(m.get("conversions", 0))
        cpa_raw = m.get("costPerConversion")
        cpa = int(cpa_raw) / 1_000_000 if cpa_raw else 0.0
        avg_cpc_raw = m.get("averageCpc")
        avg_cpc = int(avg_cpc_raw) / 1_000_000 if avg_cpc_raw else 0.0
        total_impr += impr
        total_clicks += clicks
        total_spend += spend
        total_conv += conv
        print(f"{str(camp_id):<12} {name:<35} {status:<10} {impr:>9,} {clicks:>7,} {ctr:>5.1f}% {spend:>10.2f} {conv:>6.1f} {cpa:>10.2f} {avg_cpc:>9.2f}")
    print("-" * 140)
    total_ctr = (total_clicks / total_impr * 100) if total_impr > 0 else 0.0
    total_cpa = (total_spend / total_conv) if total_conv > 0 else 0.0
    print(f"{'TOTALS':<48} {int(total_impr):>9,} {int(total_clicks):>7,} {total_ctr:>5.1f}% {total_spend:>10.2f} {total_conv:>6.1f} {total_cpa:>10.2f}")
    print(f"\nTotal campaigns: {len(results)}")


def cmd_ad_performance(client, args):
    fid = format_customer_id(args.customer_id)
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d")
    campaign_filter = f"AND campaign.id = {args.campaign_id}" if getattr(args, 'campaign_id', None) else ""
    query = f"""
        SELECT
            ad_group_ad.ad.id, ad_group_ad.status, campaign.name, ad_group.name,
            metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions,
            metrics.ctr, metrics.cost_per_conversion
        FROM ad_group_ad
        WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
          AND ad_group_ad.status != 'REMOVED'
          {campaign_filter}
        ORDER BY metrics.impressions DESC
        LIMIT 50
    """
    _, resp = client.search(fid, query)
    if resp.status_code != 200:
        print(f"Error: {resp.text}")
        return
    results = resp.json().get("results", [])
    if not results:
        print(f"No ad data for the last {args.days} days.")
        return
    print(f"Ad Performance — Account {fid} | Last {args.days} days ({start_date} to {end_date})")
    print("=" * 130)
    print(f"{'Ad ID':<12} {'Status':<10} {'Campaign':<28} {'Ad Group':<22} {'Impr':>9} {'Clicks':>7} {'CTR':>6} {'Spend':>10} {'Conv':>6} {'CPA':>10}")
    print("-" * 130)
    for r in results:
        ad = r.get("adGroupAd", {}).get("ad", {})
        m = r.get("metrics", {})
        ad_id = str(ad.get("id", ""))[:11]
        status = r.get("adGroupAd", {}).get("status", "")[:9]
        camp = r.get("campaign", {}).get("name", "")[:27]
        ag = r.get("adGroup", {}).get("name", "")[:21]
        impr = int(m.get("impressions", 0))
        clicks = int(m.get("clicks", 0))
        ctr = float(m.get("ctr", 0)) * 100
        spend = int(m.get("costMicros", 0)) / 1_000_000
        conv = float(m.get("conversions", 0))
        cpa_raw = m.get("costPerConversion")
        cpa = int(cpa_raw) / 1_000_000 if cpa_raw else 0.0
        print(f"{ad_id:<12} {status:<10} {camp:<28} {ag:<22} {impr:>9,} {clicks:>7,} {ctr:>5.1f}% {spend:>10.2f} {conv:>6.1f} {cpa:>10.2f}")
    print(f"\nTotal ads: {len(results)}")


def cmd_keyword_performance(client, args):
    fid = format_customer_id(args.customer_id)
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d")
    campaign_filter = f"AND campaign.id = {args.campaign_id}" if getattr(args, 'campaign_id', None) else ""
    query = f"""
        SELECT
            campaign.name, ad_group.name,
            ad_group_criterion.criterion_id, ad_group_criterion.keyword.text,
            ad_group_criterion.keyword.match_type, ad_group_criterion.cpc_bid_micros,
            ad_group_criterion.status,
            metrics.impressions, metrics.clicks, metrics.ctr,
            metrics.cost_micros, metrics.conversions, metrics.cost_per_conversion
        FROM keyword_view
        WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
          AND ad_group_criterion.status != 'REMOVED'
          {campaign_filter}
        ORDER BY metrics.cost_micros DESC
        LIMIT {args.limit}
    """
    _, resp = client.search(fid, query)
    if resp.status_code != 200:
        print(f"Error: {resp.text}")
        return
    results = resp.json().get("results", [])
    if not results:
        print(f"No keyword data for the last {args.days} days.")
        return
    print(f"Keyword Performance — Account {fid} | Last {args.days} days ({start_date} to {end_date})")
    print("=" * 140)
    print(f"{'Campaign':<28} {'Ad Group':<22} {'Keyword':<30} {'Match':<8} {'Impr':>8} {'Clicks':>7} {'CTR':>6} {'Spend':>10} {'Conv':>6} {'CPA':>10} {'Criterion ID'}")
    print("-" * 140)
    for r in results:
        camp = r.get("campaign", {}).get("name", "")[:27]
        ag = r.get("adGroup", {}).get("name", "")[:21]
        criterion = r.get("adGroupCriterion", {})
        kw = criterion.get("keyword", {})
        kw_text = kw.get("text", "")[:29]
        match_type = kw.get("matchType", "")[:7]
        cid = criterion.get("criterionId", "")
        m = r.get("metrics", {})
        impr = int(m.get("impressions", 0))
        clicks = int(m.get("clicks", 0))
        ctr = float(m.get("ctr", 0)) * 100
        spend = int(m.get("costMicros", 0)) / 1_000_000
        conv = float(m.get("conversions", 0))
        cpa_raw = m.get("costPerConversion")
        cpa = int(cpa_raw) / 1_000_000 if cpa_raw else 0.0
        print(f"{camp:<28} {ag:<22} {kw_text:<30} {match_type:<8} {impr:>8,} {clicks:>7,} {ctr:>5.1f}% {spend:>10.2f} {conv:>6.1f} {cpa:>10.2f} {cid}")
    print(f"\nTotal: {len(results)} keywords")


def cmd_search_terms(client, args):
    fid = format_customer_id(args.customer_id)
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d")
    campaign_filter = f"AND campaign.id = {args.campaign_id}" if getattr(args, 'campaign_id', None) else ""
    query = f"""
        SELECT
            search_term_view.search_term, search_term_view.status,
            campaign.name, ad_group.name,
            metrics.impressions, metrics.clicks, metrics.ctr,
            metrics.cost_micros, metrics.conversions, metrics.cost_per_conversion
        FROM search_term_view
        WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
          AND metrics.impressions >= {args.min_impressions}
          {campaign_filter}
        ORDER BY metrics.cost_micros DESC
        LIMIT {args.limit}
    """
    _, resp = client.search(fid, query)
    if resp.status_code != 200:
        print(f"Error: {resp.text}")
        return
    results = resp.json().get("results", [])
    if not results:
        print(f"No search term data for the last {args.days} days.")
        return
    print(f"Search Terms — Account {fid} | Last {args.days} days ({start_date} to {end_date})")
    print(f"Min impressions filter: {args.min_impressions}")
    print("=" * 140)
    print(f"{'Search Term':<45} {'Status':<10} {'Campaign':<24} {'Ad Group':<19} {'Impr':>8} {'Clicks':>7} {'CTR':>6} {'Spend':>10} {'Conv':>6} {'CPA':>10}")
    print("-" * 140)
    total_impr = total_clicks = total_cost = total_conv = 0.0
    for r in results:
        stv = r.get("searchTermView", {})
        term = stv.get("searchTerm", "")[:44]
        status = stv.get("status", "")[:9]
        camp = r.get("campaign", {}).get("name", "")[:23]
        ag = r.get("adGroup", {}).get("name", "")[:18]
        m = r.get("metrics", {})
        impr = int(m.get("impressions", 0))
        clicks = int(m.get("clicks", 0))
        ctr = float(m.get("ctr", 0)) * 100
        spend = int(m.get("costMicros", 0)) / 1_000_000
        conv = float(m.get("conversions", 0))
        cpa_raw = m.get("costPerConversion")
        cpa = int(cpa_raw) / 1_000_000 if cpa_raw else 0.0
        total_impr += impr
        total_clicks += clicks
        total_cost += spend
        total_conv += conv
        print(f"{term:<45} {status:<10} {camp:<24} {ag:<19} {impr:>8,} {clicks:>7,} {ctr:>5.1f}% {spend:>10.2f} {conv:>6.1f} {cpa:>10.2f}")
    print("-" * 140)
    ttl_ctr = (total_clicks / total_impr * 100) if total_impr > 0 else 0.0
    ttl_cpa = (total_cost / total_conv) if total_conv > 0 else 0.0
    print(f"{'TOTALS':<98} {int(total_impr):>8,} {int(total_clicks):>7,} {ttl_ctr:>5.1f}% {total_cost:>10.2f} {total_conv:>6.1f} {ttl_cpa:>10.2f}")
    print(f"Total rows: {len(results)}")


def cmd_geo_performance(client, args):
    fid = format_customer_id(args.customer_id)
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d")
    campaign_filter = f"AND campaign.id = {args.campaign_id}" if getattr(args, 'campaign_id', None) else ""
    query = f"""
        SELECT
            geographic_view.country_criterion_id, geographic_view.location_type,
            campaign.name,
            metrics.impressions, metrics.clicks, metrics.ctr,
            metrics.cost_micros, metrics.conversions, metrics.cost_per_conversion
        FROM geographic_view
        WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
          {campaign_filter}
        ORDER BY metrics.cost_micros DESC
        LIMIT {args.limit}
    """
    _, resp = client.search(fid, query)
    if resp.status_code != 200:
        print(f"Error: {resp.text}")
        return
    results = resp.json().get("results", [])
    if not results:
        print(f"No geo data for the last {args.days} days.")
        return
    print(f"Geographic Performance — Account {fid} | Last {args.days} days ({start_date} to {end_date})")
    print("=" * 120)
    print(f"{'Country ID':<12} {'Location Type':<16} {'Campaign':<28} {'Impr':>8} {'Clicks':>7} {'CTR':>6} {'Spend':>10} {'Conv':>6} {'CPA':>10}")
    print("-" * 120)
    for r in results:
        gv = r.get("geographicView", {})
        country_id = str(gv.get("countryCriterionId", ""))[:11]
        loc_type = gv.get("locationType", "")[:15]
        camp = r.get("campaign", {}).get("name", "")[:27]
        m = r.get("metrics", {})
        impr = int(m.get("impressions", 0))
        clicks = int(m.get("clicks", 0))
        ctr = float(m.get("ctr", 0)) * 100
        spend = int(m.get("costMicros", 0)) / 1_000_000
        conv = float(m.get("conversions", 0))
        cpa_raw = m.get("costPerConversion")
        cpa = int(cpa_raw) / 1_000_000 if cpa_raw else 0.0
        print(f"{country_id:<12} {loc_type:<16} {camp:<28} {impr:>8,} {clicks:>7,} {ctr:>5.1f}% {spend:>10.2f} {conv:>6.1f} {cpa:>10.2f}")
    print(f"\nTotal rows: {len(results)}")
    print("Note: countryCriterionId — common IDs: 2840=USA, 2826=UK, 2250=France, 2276=Germany, 2724=Spain, 2032=Australia")


def cmd_auction_insights(client, args):
    fid = format_customer_id(args.customer_id)
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d")
    campaign_filter = f"AND campaign.id = {args.campaign_id}" if getattr(args, 'campaign_id', None) else ""

    auction_query = f"""
        SELECT
            auction_insight.domain, metrics.search_impression_share,
            metrics.search_overlap_rate, metrics.search_position_above_rate,
            metrics.search_top_impression_share, metrics.search_absolute_top_impression_share,
            metrics.search_outranking_share
        FROM auction_insight
        WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
          {campaign_filter}
        ORDER BY metrics.search_impression_share DESC
        LIMIT 25
    """
    own_query = f"""
        SELECT
            campaign.name, metrics.search_impression_share,
            metrics.search_budget_lost_impression_share,
            metrics.search_rank_lost_impression_share
        FROM campaign
        WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
          AND campaign.status = 'ENABLED'
          {campaign_filter}
        ORDER BY metrics.search_impression_share DESC
        LIMIT 50
    """
    _, auction_resp = client.search(fid, auction_query)
    _, own_resp = client.search(fid, own_query)

    print(f"Auction Insights — Account {fid} | Last {args.days} days ({start_date} to {end_date})")
    print("=" * 110)
    print("\nCOMPETITOR BREAKDOWN")
    print("-" * 110)
    print(f"{'Domain':<35} {'Impr Share':>11} {'Overlap':>9} {'Above Rate':>11} {'Top IS':>8} {'Abs Top IS':>11} {'Outranking':>11}")
    print("-" * 110)
    if auction_resp.status_code == 200:
        for r in auction_resp.json().get("results", []):
            domain = r.get("auctionInsight", {}).get("domain", "Unknown")[:34]
            m = r.get("metrics", {})
            print(f"{domain:<35} {float(m.get('searchImpressionShare',0) or 0)*100:>10.1f}% "
                  f"{float(m.get('searchOverlapRate',0) or 0)*100:>8.1f}% "
                  f"{float(m.get('searchPositionAboveRate',0) or 0)*100:>10.1f}% "
                  f"{float(m.get('searchTopImpressionShare',0) or 0)*100:>7.1f}% "
                  f"{float(m.get('searchAbsoluteTopImpressionShare',0) or 0)*100:>10.1f}% "
                  f"{float(m.get('searchOutrankingShare',0) or 0)*100:>10.1f}%")
    else:
        print(f"  Error: {auction_resp.text}")

    print("\nYOUR IMPRESSION SHARE BY CAMPAIGN")
    print("-" * 80)
    print(f"{'Campaign':<35} {'Impr Share':>11} {'Budget Lost IS':>15} {'Rank Lost IS':>13}")
    print("-" * 80)
    if own_resp.status_code == 200:
        for r in own_resp.json().get("results", []):
            camp = r.get("campaign", {}).get("name", "")[:34]
            m = r.get("metrics", {})
            print(f"{camp:<35} {float(m.get('searchImpressionShare',0) or 0)*100:>10.1f}% "
                  f"{float(m.get('searchBudgetLostImpressionShare',0) or 0)*100:>14.1f}% "
                  f"{float(m.get('searchRankLostImpressionShare',0) or 0)*100:>12.1f}%")
    else:
        print(f"  Error: {own_resp.text}")


def cmd_quality_scores(client, args):
    fid = format_customer_id(args.customer_id)
    campaign_filter = f"AND campaign.id = {args.campaign_id}" if getattr(args, 'campaign_id', None) else ""
    query = f"""
        SELECT
            campaign.name, ad_group.name,
            ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type,
            ad_group_criterion.quality_info.quality_score,
            ad_group_criterion.quality_info.creative_quality_score,
            ad_group_criterion.quality_info.post_click_quality_score,
            ad_group_criterion.quality_info.search_predicted_ctr,
            metrics.impressions
        FROM ad_group_criterion
        WHERE ad_group_criterion.type = 'KEYWORD'
          AND ad_group_criterion.status != 'REMOVED'
          {campaign_filter}
        ORDER BY ad_group_criterion.quality_info.quality_score ASC
        LIMIT {args.limit}
    """
    _, resp = client.search(fid, query)
    if resp.status_code != 200:
        print(f"Error: {resp.text}")
        return
    results = resp.json().get("results", [])
    if not results:
        print("No quality score data found.")
        return
    print(f"Keyword Quality Scores — Account {fid} (lowest first)")
    print("=" * 130)
    print(f"{'Campaign':<28} {'Ad Group':<22} {'Keyword':<30} {'Match':<8} {'QS':>4} {'Ad Rel':>14} {'LP Exp':>14} {'Pred CTR':>14} {'Impr':>8}")
    print("-" * 130)
    for r in results:
        camp = r.get("campaign", {}).get("name", "")[:27]
        ag = r.get("adGroup", {}).get("name", "")[:21]
        criterion = r.get("adGroupCriterion", {})
        kw = criterion.get("keyword", {})
        qi = criterion.get("qualityInfo", {})
        print(f"{camp:<28} {ag:<22} {kw.get('text','')[:29]:<30} {kw.get('matchType','')[:7]:<8} "
              f"{str(qi.get('qualityScore','N/A')):>4} {str(qi.get('creativeQualityScore','N/A')):>14} "
              f"{str(qi.get('postClickQualityScore','N/A')):>14} {str(qi.get('searchPredictedCtr','N/A')):>14} "
              f"{int(r.get('metrics',{}).get('impressions',0)):>8,}")
    print(f"\nTotal: {len(results)} keywords  |  QS: 1–10 (10=best)  |  Components: ABOVE_AVERAGE | AVERAGE | BELOW_AVERAGE")


def cmd_budget_pacing(client, args):
    fid = format_customer_id(args.customer_id)
    now = datetime.now()
    month_start = now.replace(day=1).strftime("%Y-%m-%d")
    today = now.strftime("%Y-%m-%d")
    days_elapsed = now.day
    if now.month == 12:
        days_in_month = 31
    else:
        days_in_month = (now.replace(month=now.month + 1, day=1) - timedelta(days=1)).day
    query = f"""
        SELECT campaign.id, campaign.name, campaign_budget.amount_micros, metrics.cost_micros
        FROM campaign
        WHERE segments.date BETWEEN '{month_start}' AND '{today}'
          AND campaign.status = 'ENABLED'
        ORDER BY metrics.cost_micros DESC
        LIMIT 100
    """
    _, resp = client.search(fid, query)
    if resp.status_code != 200:
        print(f"Error: {resp.text}")
        return
    results = resp.json().get("results", [])
    if not results:
        print("No campaign budget data found for this month.")
        return
    # Aggregate by campaign (multiple date rows)
    camp_data: dict = {}
    for r in results:
        camp = r.get("campaign", {})
        cid = camp.get("id", "")
        if cid not in camp_data:
            camp_data[cid] = {
                "name": camp.get("name", ""),
                "daily_budget_micros": int(r.get("campaignBudget", {}).get("amountMicros", 0)),
                "cost_micros": 0,
            }
        camp_data[cid]["cost_micros"] += int(r.get("metrics", {}).get("costMicros", 0))
    print(f"Budget Pacing — Account {fid}")
    print(f"Month: {now.strftime('%B %Y')} | Day {days_elapsed} of {days_in_month} | As of {today}")
    print("=" * 120)
    print(f"{'Campaign':<35} {'Daily Budget':>13} {'MTD Spend':>11} {'Expected':>11} {'Projected':>11} {'Pacing %':>9} {'Status':<14}")
    print("-" * 120)
    for cid, data in sorted(camp_data.items(), key=lambda x: x[1]["cost_micros"], reverse=True):
        name = data["name"][:34]
        daily = data["daily_budget_micros"] / 1_000_000
        actual = data["cost_micros"] / 1_000_000
        expected = daily * days_elapsed
        projected = (actual / days_elapsed * days_in_month) if days_elapsed > 0 else 0.0
        pacing = (actual / expected * 100) if expected > 0 else 0.0
        status = "UNDER PACING" if pacing < 85 else "ON TRACK" if pacing <= 115 else "OVER PACING"
        print(f"{name:<35} {daily:>12.2f} {actual:>10.2f} {expected:>10.2f} {projected:>10.2f} {pacing:>8.1f}% {status:<14}")
    print("-" * 120)
    print("UNDER (<85%) | ON TRACK (85-115%) | OVER (>115%) of expected MTD spend")


def cmd_change_history(client, args):
    fid = format_customer_id(args.customer_id)
    start_datetime = (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d 00:00:00")
    query = f"""
        SELECT
            change_event.change_date_time, change_event.user_email,
            change_event.resource_type, change_event.resource_change_operation,
            change_event.changed_fields, campaign.name
        FROM change_event
        WHERE change_event.change_date_time >= '{start_datetime}'
        ORDER BY change_event.change_date_time DESC
        LIMIT {args.limit}
    """
    _, resp = client.search(fid, query)
    if resp.status_code != 200:
        print(f"Error: {resp.text}")
        return
    results = resp.json().get("results", [])
    if not results:
        print(f"No change events in the last {args.days} days.")
        return
    print(f"Change History — Account {fid} | Last {args.days} days")
    print("=" * 120)
    print(f"{'Date/Time':<22} {'User':<30} {'Resource Type':<20} {'Operation':<12} {'Campaign':<25} {'Changed Fields'}")
    print("-" * 120)
    for r in results:
        ce = r.get("changeEvent", {})
        print(f"{ce.get('changeDatetime','')[:21]:<22} {ce.get('userEmail','automated')[:29]:<30} "
              f"{ce.get('resourceType','')[:19]:<20} {ce.get('resourceChangeOperation','')[:11]:<12} "
              f"{r.get('campaign',{}).get('name','')[:24]:<25} {ce.get('changedFields','')}")
    print(f"\nTotal: {len(results)} events")


def cmd_ad_creatives(client, args):
    fid = format_customer_id(args.customer_id)
    campaign_filter = f"AND campaign.id = {args.campaign_id}" if getattr(args, 'campaign_id', None) else ""
    query = f"""
        SELECT
            ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status,
            ad_group_ad.ad.final_urls, campaign.name, ad_group.name,
            ad_group_ad.ad.responsive_search_ad.headlines,
            ad_group_ad.ad.responsive_search_ad.descriptions
        FROM ad_group_ad
        WHERE ad_group_ad.status != 'REMOVED'
          {campaign_filter}
        ORDER BY campaign.name
        LIMIT 50
    """
    _, resp = client.search(fid, query)
    if resp.status_code != 200:
        print(f"Error: {resp.text}")
        return
    results = resp.json().get("results", [])
    if not results:
        print("No ads found.")
        return
    print(f"Ad Creatives — Account {fid}")
    print("=" * 90)
    for r in results:
        aga = r.get("adGroupAd", {})
        ad = aga.get("ad", {})
        rsa = ad.get("responsiveSearchAd", {})
        print(f"\nAd ID: {ad.get('id','')}  |  Status: {aga.get('status','')}  |  Campaign: {r.get('campaign',{}).get('name','')}  |  Ad Group: {r.get('adGroup',{}).get('name','')}")
        if ad.get("finalUrls"):
            print(f"  URL: {ad['finalUrls'][0]}")
        if rsa.get("headlines"):
            headlines = [h["text"] for h in rsa["headlines"]]
            print(f"  Headlines ({len(headlines)}): {' | '.join(headlines[:5])}")
        if rsa.get("descriptions"):
            descs = [d["text"] for d in rsa["descriptions"]]
            print(f"  Descriptions ({len(descs)}): {' | '.join(descs[:3])}")
        print("-" * 90)
    print(f"\nTotal ads: {len(results)}")


def cmd_image_assets(client, args):
    fid = format_customer_id(args.customer_id)
    query = """
        SELECT asset.id, asset.name, asset.type, asset.image_asset.full_size.url,
               asset.image_asset.full_size.width_pixels, asset.image_asset.full_size.height_pixels,
               asset.creation_time
        FROM asset
        WHERE asset.type = 'IMAGE'
        ORDER BY asset.creation_time DESC
        LIMIT 50
    """
    _, resp = client.search(fid, query)
    if resp.status_code != 200:
        print(f"Error: {resp.text}")
        return
    results = resp.json().get("results", [])
    if not results:
        print("No image assets found.")
        return
    print(f"Image Assets — Account {fid}")
    print("=" * 100)
    print(f"{'Asset ID':<15} {'Name':<35} {'Dimensions':<18} {'Created':<22}")
    print("-" * 100)
    for r in results:
        asset = r.get("asset", {})
        img = asset.get("imageAsset", {}).get("fullSize", {})
        w = img.get("widthPixels", "")
        h = img.get("heightPixels", "")
        dims = f"{w}x{h}" if w and h else "Unknown"
        print(f"{str(asset.get('id','')):<15} {asset.get('name','')[:34]:<35} {dims:<18} {asset.get('creationTime','')[:21]:<22}")
    print(f"\nTotal: {len(results)}")


def cmd_query(client, args):
    fid = format_customer_id(args.customer_id)
    _, resp = client.search(fid, args.query)
    if resp.status_code != 200:
        print(f"Error: {resp.text}")
        return
    data = resp.json()
    if args.format == "json":
        print(json.dumps(data, indent=2))
        return
    results = data.get("results", [])
    if not results:
        print("No results.")
        return
    if args.format == "csv":
        fields = []
        first = results[0]
        for k, v in first.items():
            if isinstance(v, dict):
                for sk in v:
                    fields.append(f"{k}.{sk}")
            else:
                fields.append(k)
        print(",".join(fields))
        for r in results:
            row = []
            for f in fields:
                if "." in f:
                    parent, child = f.split(".", 1)
                    row.append(str(r.get(parent, {}).get(child, "")).replace(",", ";"))
                else:
                    row.append(str(r.get(f, "")).replace(",", ";"))
            print(",".join(row))
    else:
        fields = []
        first = results[0]
        for k, v in first.items():
            if isinstance(v, dict):
                for sk in v:
                    fields.append(f"{k}.{sk}")
            else:
                fields.append(k)
        print(" | ".join(fields))
        print("-" * 80)
        for r in results:
            row = []
            for f in fields:
                if "." in f:
                    parent, child = f.split(".", 1)
                    row.append(str(r.get(parent, {}).get(child, "")))
                else:
                    row.append(str(r.get(f, "")))
            print(" | ".join(row))
        print(f"\n{len(results)} rows")


# ─── Campaign Management ──────────────────────────────────────────────────────

def cmd_create_campaign_budget(client, args):
    fid = format_customer_id(args.customer_id)
    _, resp = client.mutate(fid, [{
        "campaignBudgetOperation": {
            "create": {
                "name": args.name,
                "amountMicros": str(args.amount_micros),
                "deliveryMethod": "STANDARD"
            }
        }
    }])
    if resp.status_code != 200:
        print(f"Error: {resp.text}")
        return
    resource = resp.json().get("mutateOperationResponses", [{}])[0].get("campaignBudgetResult", {}).get("resourceName", "")
    print(f"Budget created: {resource}")
    print(f"  Name: {args.name}")
    print(f"  Daily amount: {args.amount_micros / 1_000_000:.2f}")


def cmd_create_campaign(client, args):
    fid = format_customer_id(args.customer_id)
    campaign = {
        "name": args.name,
        "status": "PAUSED",
        "advertisingChannelType": args.type.upper(),
        "campaignBudget": args.budget_resource,
        "biddingStrategyType": args.bidding_strategy,
        "networkSettings": {
            "targetGoogleSearch": True,
            "targetSearchNetwork": True,
            "targetContentNetwork": False,
        }
    }
    _, resp = client.mutate(fid, [{"campaignOperation": {"create": campaign}}])
    if resp.status_code != 200:
        print(f"Error: {resp.text}")
        return
    resource = resp.json().get("mutateOperationResponses", [{}])[0].get("campaignResult", {}).get("resourceName", "")
    print(f"Campaign created (PAUSED): {resource}")
    print(f"  Name: {args.name}")
    print(f"  Type: {args.type.upper()}")
    print("Enable with: google-ads update-campaign-status --customer-id ... --campaign-id ... --status ENABLED")


def cmd_update_campaign_status(client, args):
    fid = format_customer_id(args.customer_id)
    status = args.status.upper()
    if status not in ("ENABLED", "PAUSED", "REMOVED"):
        print("Error: status must be ENABLED, PAUSED, or REMOVED")
        return
    _, resp = client.mutate(fid, [{
        "campaignOperation": {
            "update": {
                "resourceName": f"customers/{fid}/campaigns/{args.campaign_id}",
                "status": status
            },
            "updateMask": "status"
        }
    }])
    if resp.status_code != 200:
        print(f"Error: {resp.text}")
        return
    print(f"Campaign {args.campaign_id} status → {status}")


def cmd_update_budget(client, args):
    fid = format_customer_id(args.customer_id)
    _, resp = client.mutate(fid, [{
        "campaignBudgetOperation": {
            "update": {
                "resourceName": f"customers/{fid}/campaignBudgets/{args.budget_id}",
                "amountMicros": str(args.amount_micros)
            },
            "updateMask": "amountMicros"
        }
    }])
    if resp.status_code != 200:
        print(f"Error: {resp.text}")
        return
    print(f"Budget {args.budget_id} → {args.amount_micros / 1_000_000:.2f}")


# ─── Ad Group Management ──────────────────────────────────────────────────────

def cmd_create_ad_group(client, args):
    fid = format_customer_id(args.customer_id)
    ad_group = {
        "name": args.name,
        "campaign": f"customers/{fid}/campaigns/{args.campaign_id}",
        "status": "ENABLED",
        "type": "SEARCH_STANDARD",
    }
    if args.cpc_bid_micros:
        ad_group["cpcBidMicros"] = str(args.cpc_bid_micros)
    _, resp = client.mutate(fid, [{"adGroupOperation": {"create": ad_group}}])
    if resp.status_code != 200:
        print(f"Error: {resp.text}")
        return
    resource = resp.json().get("mutateOperationResponses", [{}])[0].get("adGroupResult", {}).get("resourceName", "")
    print(f"Ad group created: {resource}")
    print(f"  Name: {args.name}  |  Campaign: {args.campaign_id}")


def cmd_update_ad_group(client, args):
    fid = format_customer_id(args.customer_id)
    if not any([args.name, args.status, args.cpc_bid_micros]):
        print("Error: provide at least one of --name, --status, or --cpc-bid-micros")
        return
    update = {"resourceName": f"customers/{fid}/adGroups/{args.ad_group_id}"}
    mask_parts = []
    if args.name:
        update["name"] = args.name
        mask_parts.append("name")
    if args.status:
        update["status"] = args.status.upper()
        mask_parts.append("status")
    if args.cpc_bid_micros:
        update["cpcBidMicros"] = str(args.cpc_bid_micros)
        mask_parts.append("cpcBidMicros")
    _, resp = client.mutate(fid, [{"adGroupOperation": {"update": update, "updateMask": ",".join(mask_parts)}}])
    if resp.status_code != 200:
        print(f"Error: {resp.text}")
        return
    changes = [f"{k}" for k in mask_parts]
    print(f"Ad group {args.ad_group_id} updated: {', '.join(changes)}")


# ─── Ads ──────────────────────────────────────────────────────────────────────

def cmd_create_rsa(client, args):
    fid = format_customer_id(args.customer_id)
    try:
        headlines_list = json.loads(args.headlines)
        descriptions_list = json.loads(args.descriptions)
    except json.JSONDecodeError as e:
        print(f"Error parsing JSON: {e}")
        return
    rsa = {
        "headlines": [{"text": h} for h in headlines_list],
        "descriptions": [{"text": d} for d in descriptions_list],
    }
    if getattr(args, 'path1', None):
        rsa["path1"] = args.path1
    if getattr(args, 'path2', None):
        rsa["path2"] = args.path2
    ad = {
        "adGroup": f"customers/{fid}/adGroups/{args.ad_group_id}",
        "status": "PAUSED",
        "ad": {
            "responsiveSearchAd": rsa,
            "finalUrls": [args.final_url],
        }
    }
    _, resp = client.mutate(fid, [{"adGroupAdOperation": {"create": ad}}])
    if resp.status_code != 200:
        print(f"Error: {resp.text}")
        return
    resource = resp.json().get("mutateOperationResponses", [{}])[0].get("adGroupAdResult", {}).get("resourceName", "")
    print(f"Ad created (PAUSED): {resource}")
    print(f"  Headlines: {len(headlines_list)}  |  Descriptions: {len(descriptions_list)}")
    print(f"  URL: {args.final_url}")
    print("Enable with: google-ads update-ad-status ...")


def cmd_update_ad_status(client, args):
    fid = format_customer_id(args.customer_id)
    status = args.status.upper()
    if status not in ("ENABLED", "PAUSED", "REMOVED"):
        print("Error: status must be ENABLED, PAUSED, or REMOVED")
        return
    resource = f"customers/{fid}/adGroupAds/{args.ad_group_id}~{args.ad_id}"
    _, resp = client.mutate(fid, [{
        "adGroupAdOperation": {
            "update": {"resourceName": resource, "status": status},
            "updateMask": "status"
        }
    }])
    if resp.status_code != 200:
        print(f"Error: {resp.text}")
        return
    print(f"Ad {args.ad_id} status → {status}")


def cmd_update_rsa(client, args):
    fid = format_customer_id(args.customer_id)
    if not args.headlines and not args.descriptions:
        print("Error: provide at least --headlines or --descriptions")
        return
    rsa = {}
    mask_parts = []
    if args.headlines:
        try:
            rsa["headlines"] = [{"text": h} for h in json.loads(args.headlines)]
            mask_parts.append("ad.responsiveSearchAd.headlines")
        except json.JSONDecodeError as e:
            print(f"Error parsing headlines JSON: {e}")
            return
    if args.descriptions:
        try:
            rsa["descriptions"] = [{"text": d} for d in json.loads(args.descriptions)]
            mask_parts.append("ad.responsiveSearchAd.descriptions")
        except json.JSONDecodeError as e:
            print(f"Error parsing descriptions JSON: {e}")
            return
    resource = f"customers/{fid}/adGroupAds/{args.ad_group_id}~{args.ad_id}"
    _, resp = client.mutate(fid, [{
        "adGroupAdOperation": {
            "update": {
                "resourceName": resource,
                "ad": {"responsiveSearchAd": rsa}
            },
            "updateMask": ",".join(mask_parts)
        }
    }])
    if resp.status_code != 200:
        print(f"Error: {resp.text}")
        return
    print(f"Ad {args.ad_id} updated: {', '.join(mask_parts)}")


def cmd_duplicate_ad(client, args):
    fid = format_customer_id(args.customer_id)
    source_ag_id = args.ad_group_id
    dest_ag_id = args.target_ad_group_id or args.ad_group_id

    query = f"""
        SELECT ad_group_ad.ad.id, ad_group_ad.ad.responsive_search_ad.headlines,
               ad_group_ad.ad.responsive_search_ad.descriptions,
               ad_group_ad.ad.responsive_search_ad.path1, ad_group_ad.ad.responsive_search_ad.path2,
               ad_group_ad.ad.final_urls
        FROM ad_group_ad
        WHERE ad_group_ad.ad.id = {args.ad_id}
          AND ad_group.id = {source_ag_id}
        LIMIT 1
    """
    _, search_resp = client.search(fid, query)
    if search_resp.status_code != 200:
        print(f"Error fetching source ad: {search_resp.text}")
        return
    results = search_resp.json().get("results", [])
    if not results:
        print(f"No ad found with ID {args.ad_id} in ad group {source_ag_id}")
        return

    source_ad = results[0]["adGroupAd"]["ad"]
    rsa = source_ad.get("responsiveSearchAd", {})
    if not rsa:
        print(f"Ad {args.ad_id} is not a responsive search ad")
        return

    new_rsa = {
        "headlines": [{"text": h["text"]} for h in rsa.get("headlines", [])],
        "descriptions": [{"text": d["text"]} for d in rsa.get("descriptions", [])],
    }
    if rsa.get("path1"):
        new_rsa["path1"] = rsa["path1"]
    if rsa.get("path2"):
        new_rsa["path2"] = rsa["path2"]

    _, resp = client.mutate(fid, [{
        "adGroupAdOperation": {
            "create": {
                "adGroup": f"customers/{fid}/adGroups/{dest_ag_id}",
                "status": "PAUSED",
                "ad": {
                    "responsiveSearchAd": new_rsa,
                    "finalUrls": source_ad.get("finalUrls", [])
                }
            }
        }
    }])
    if resp.status_code != 200:
        print(f"Error: {resp.text}")
        return
    resource = resp.json().get("mutateOperationResponses", [{}])[0].get("adGroupAdResult", {}).get("resourceName", "")
    location = "same ad group" if dest_ag_id == source_ag_id else f"ad group {dest_ag_id}"
    print(f"Ad {args.ad_id} duplicated into {location} (PAUSED)")
    print(f"New resource: {resource}")


# ─── Keywords ─────────────────────────────────────────────────────────────────

def cmd_add_keywords(client, args):
    fid = format_customer_id(args.customer_id)
    try:
        keywords = json.loads(args.keywords)
    except json.JSONDecodeError as e:
        print(f"Error parsing keywords JSON: {e}")
        return
    operations = []
    for kw in keywords:
        text = kw.get("text") or kw if isinstance(kw, str) else None
        match_type = kw.get("match_type", "BROAD") if isinstance(kw, dict) else "BROAD"
        bid = kw.get("cpc_bid_micros") if isinstance(kw, dict) else None
        if not text:
            continue
        kw_obj = {
            "adGroup": f"customers/{fid}/adGroups/{args.ad_group_id}",
            "status": "ENABLED",
            "keyword": {"text": text, "matchType": match_type.upper()},
        }
        if bid:
            kw_obj["cpcBidMicros"] = str(bid)
        operations.append({"adGroupCriterionOperation": {"create": kw_obj}})
    _, resp = client.mutate(fid, operations)
    if resp.status_code != 200:
        print(f"Error: {resp.text}")
        return
    print(f"Added {len(operations)} keyword(s) to ad group {args.ad_group_id}")


def cmd_add_negative_keywords(client, args):
    fid = format_customer_id(args.customer_id)
    try:
        keywords = json.loads(args.keywords)
    except json.JSONDecodeError as e:
        print(f"Error parsing keywords JSON: {e}")
        return
    operations = []
    for kw in keywords:
        text = kw.get("text") if isinstance(kw, dict) else kw
        match_type = kw.get("match_type", "BROAD") if isinstance(kw, dict) else "BROAD"
        if not text:
            continue
        operations.append({"adGroupCriterionOperation": {"create": {
            "adGroup": f"customers/{fid}/adGroups/{args.ad_group_id}",
            "negative": True,
            "keyword": {"text": text, "matchType": match_type.upper()},
        }}})
    _, resp = client.mutate(fid, operations)
    if resp.status_code != 200:
        print(f"Error: {resp.text}")
        return
    print(f"Added {len(operations)} negative keyword(s) to ad group {args.ad_group_id}")


def cmd_update_keyword_status(client, args):
    fid = format_customer_id(args.customer_id)
    status = args.status.upper()
    resource = f"customers/{fid}/adGroupCriteria/{args.ad_group_id}~{args.criterion_id}"
    _, resp = client.mutate(fid, [{
        "adGroupCriterionOperation": {
            "update": {"resourceName": resource, "status": status},
            "updateMask": "status"
        }
    }])
    if resp.status_code != 200:
        print(f"Error: {resp.text}")
        return
    print(f"Keyword (criterion {args.criterion_id}) status → {status}")


def cmd_update_keyword_bid(client, args):
    fid = format_customer_id(args.customer_id)
    if args.cpc_bid_micros <= 0:
        print("Error: cpc-bid-micros must be > 0")
        return
    resource = f"customers/{fid}/adGroupCriteria/{args.ad_group_id}~{args.criterion_id}"
    _, resp = client.mutate(fid, [{
        "adGroupCriterionOperation": {
            "update": {"resourceName": resource, "cpcBidMicros": str(args.cpc_bid_micros)},
            "updateMask": "cpcBidMicros"
        }
    }])
    if resp.status_code != 200:
        print(f"Error: {resp.text}")
        return
    print(f"Keyword (criterion {args.criterion_id}) bid → {args.cpc_bid_micros / 1_000_000:.2f}")


def cmd_bulk_update_bids(client, args):
    fid = format_customer_id(args.customer_id)
    try:
        updates = json.loads(args.updates)
    except json.JSONDecodeError as e:
        print(f"Error parsing JSON: {e}")
        return
    errors = []
    for i, e in enumerate(updates):
        for field in ("ad_group_id", "criterion_id", "cpc_bid_micros"):
            if field not in e:
                errors.append(f"Entry {i}: missing '{field}'")
    if errors:
        print("Validation errors:\n" + "\n".join(errors))
        return
    operations = [{
        "adGroupCriterionOperation": {
            "update": {
                "resourceName": f"customers/{fid}/adGroupCriteria/{e['ad_group_id']}~{e['criterion_id']}",
                "cpcBidMicros": str(int(e["cpc_bid_micros"]))
            },
            "updateMask": "cpcBidMicros"
        }
    } for e in updates]
    _, resp = client.mutate(fid, operations)
    if resp.status_code != 200:
        print(f"Error: {resp.text}")
        return
    responses = resp.json().get("mutateOperationResponses", [])
    succeeded = [r for r in responses if "adGroupCriterionResult" in r]
    failed = len(updates) - len(succeeded)
    print(f"Bulk bid update: {len(succeeded)}/{len(updates)} succeeded")
    for i, e in enumerate(updates):
        status = "OK" if i < len(succeeded) else "FAILED"
        print(f"  [{status}] AdGroup {e['ad_group_id']} / Criterion {e['criterion_id']} → {int(e['cpc_bid_micros']) / 1_000_000:.2f}")


# ─── Extensions ───────────────────────────────────────────────────────────────

def cmd_add_sitelink(client, args):
    fid = format_customer_id(args.customer_id)
    if len(args.text) > 25:
        print(f"Error: --text exceeds 25 characters ({len(args.text)})")
        return
    sitelink_asset: dict = {"linkText": args.text, "finalUrls": [args.url]}
    if getattr(args, 'desc1', None):
        sitelink_asset["description1"] = args.desc1
    if getattr(args, 'desc2', None):
        sitelink_asset["description2"] = args.desc2

    _, asset_resp = client.mutate(fid, [{"assetOperation": {"create": {"sitelinkAsset": sitelink_asset}}}])
    if asset_resp.status_code != 200:
        print(f"Error creating sitelink asset: {asset_resp.text}")
        return
    asset_resource = asset_resp.json().get("mutateOperationResponses", [{}])[0].get("assetResult", {}).get("resourceName", "")
    if not asset_resource:
        print(f"Asset created but resource name not returned")
        return

    _, link_resp = client.mutate(fid, [{"campaignAssetOperation": {"create": {
        "asset": asset_resource,
        "campaign": f"customers/{fid}/campaigns/{args.campaign_id}",
        "fieldType": "SITELINK"
    }}}])
    if link_resp.status_code != 200:
        print(f"Asset created ({asset_resource}) but link failed: {link_resp.text}")
        return
    print(f"Sitelink added: '{args.text}' → {args.url}")
    print(f"Asset: {asset_resource}")


def cmd_add_callout(client, args):
    fid = format_customer_id(args.customer_id)
    if len(args.text) > 25:
        print(f"Error: --text exceeds 25 characters ({len(args.text)})")
        return
    _, asset_resp = client.mutate(fid, [{"assetOperation": {"create": {"calloutAsset": {"calloutText": args.text}}}}])
    if asset_resp.status_code != 200:
        print(f"Error creating callout asset: {asset_resp.text}")
        return
    asset_resource = asset_resp.json().get("mutateOperationResponses", [{}])[0].get("assetResult", {}).get("resourceName", "")
    if not asset_resource:
        print("Asset created but resource name not returned")
        return
    _, link_resp = client.mutate(fid, [{"campaignAssetOperation": {"create": {
        "asset": asset_resource,
        "campaign": f"customers/{fid}/campaigns/{args.campaign_id}",
        "fieldType": "CALLOUT"
    }}}])
    if link_resp.status_code != 200:
        print(f"Asset created ({asset_resource}) but link failed: {link_resp.text}")
        return
    print(f"Callout added: '{args.text}'")
    print(f"Asset: {asset_resource}")


def cmd_list_extensions(client, args):
    fid = format_customer_id(args.customer_id)
    query = f"""
        SELECT
            campaign_asset.field_type, campaign_asset.status,
            asset.name, asset.sitelink_asset.link_text,
            asset.sitelink_asset.description1, asset.sitelink_asset.description2,
            asset.sitelink_asset.final_urls, asset.callout_asset.callout_text
        FROM campaign_asset
        WHERE campaign.id = {args.campaign_id}
        ORDER BY campaign_asset.field_type
    """
    _, resp = client.search(fid, query)
    if resp.status_code != 200:
        print(f"Error: {resp.text}")
        return
    results = resp.json().get("results", [])
    if not results:
        print(f"No extensions found for campaign {args.campaign_id}")
        return
    by_type: dict = {}
    for r in results:
        ca = r.get("campaignAsset", {})
        by_type.setdefault(ca.get("fieldType", "UNKNOWN"), []).append({"status": ca.get("status", ""), "asset": r.get("asset", {})})
    print(f"Extensions — Campaign {args.campaign_id}")
    print("=" * 80)
    for ext_type, items in sorted(by_type.items()):
        print(f"\n{ext_type} ({len(items)})")
        print("-" * 60)
        for item in items:
            print(f"  Status: {item['status']}")
            asset = item["asset"]
            sl = asset.get("sitelinkAsset", {})
            if sl:
                print(f"  Link: {sl.get('linkText','')} → {sl.get('finalUrls',[''])[0]}")
                if sl.get("description1"):
                    print(f"  Desc: {sl['description1']}")
            co = asset.get("calloutAsset", {})
            if co:
                print(f"  Callout: {co.get('calloutText','')}")
    print(f"\nTotal: {len(results)}")


# ─── Conversions ──────────────────────────────────────────────────────────────

def cmd_list_conversions(client, args):
    fid = format_customer_id(args.customer_id)
    query = """
        SELECT conversion_action.id, conversion_action.name, conversion_action.type,
               conversion_action.category, conversion_action.status,
               conversion_action.click_through_lookback_window_days
        FROM conversion_action
        ORDER BY conversion_action.name
    """
    _, resp = client.search(fid, query)
    if resp.status_code != 200:
        print(f"Error: {resp.text}")
        return
    results = resp.json().get("results", [])
    if not results:
        print("No conversion actions found.")
        return
    print(f"Conversion Actions — Account {fid}")
    print("=" * 100)
    print(f"{'ID':<15} {'Name':<35} {'Type':<15} {'Category':<15} {'Status':<10} {'LB Days':>8}")
    print("-" * 100)
    for r in results:
        ca = r.get("conversionAction", {})
        print(f"{str(ca.get('id','')):<15} {ca.get('name','')[:34]:<35} {ca.get('type','')[:14]:<15} "
              f"{ca.get('category','')[:14]:<15} {ca.get('status','')[:9]:<10} "
              f"{str(ca.get('clickThroughLookbackWindowDays','')):>8}")
    print(f"\nTotal: {len(results)}")


def cmd_create_conversion(client, args):
    fid = format_customer_id(args.customer_id)
    valid_cats = ("PURCHASE", "LEAD", "SIGNUP", "REQUEST_DEMO", "PAGE_VIEW", "DOWNLOAD", "OTHER")
    cat = args.category.upper()
    if cat not in valid_cats:
        print(f"Error: category must be one of {valid_cats}")
        return
    _, resp = client.mutate(fid, [{
        "conversionActionOperation": {
            "create": {
                "name": args.name,
                "category": cat,
                "type": "WEBPAGE",
                "status": "ENABLED",
                "clickThroughLookbackWindowDays": getattr(args, 'click_lookback_days', 30),
                "viewThroughLookbackWindowDays": getattr(args, 'view_lookback_days', 1),
            }
        }
    }])
    if resp.status_code != 200:
        print(f"Error: {resp.text}")
        return
    resource = resp.json().get("mutateOperationResponses", [{}])[0].get("conversionActionResult", {}).get("resourceName", "")
    print(f"Conversion action created: {resource}")
    print(f"  Name: {args.name}  |  Category: {cat}")
    print("Install the conversion tag via Google Tag or GTM using the conversion action ID.")


# ─── Ad Schedule ──────────────────────────────────────────────────────────────

def cmd_set_schedule(client, args):
    fid = format_customer_id(args.customer_id)
    try:
        schedules = json.loads(args.schedules)
    except json.JSONDecodeError as e:
        print(f"Error parsing schedules JSON: {e}")
        return
    operations = []
    for s in schedules:
        operations.append({"campaignCriterionOperation": {"create": {
            "campaign": f"customers/{fid}/campaigns/{args.campaign_id}",
            "adSchedule": {
                "dayOfWeek": s["day_of_week"].upper(),
                "startHour": s["start_hour"],
                "endHour": s["end_hour"],
                "startMinute": "ZERO",
                "endMinute": "ZERO",
            }
        }}})
    _, resp = client.mutate(fid, operations)
    if resp.status_code != 200:
        print(f"Error: {resp.text}")
        return
    print(f"Ad schedule set for campaign {args.campaign_id}: {len(schedules)} time slot(s)")
    for s in schedules:
        print(f"  {s['day_of_week']} {s['start_hour']}:00 – {s['end_hour']}:00")


# ─── Rules Engine ─────────────────────────────────────────────────────────────

def cmd_create_rule(client, args):
    if args.metric not in VALID_METRICS:
        print(f"Error: metric must be one of {VALID_METRICS}")
        return
    if args.operator not in VALID_OPERATORS:
        print(f"Error: operator must be one of {VALID_OPERATORS}")
        return
    if args.action_type not in VALID_ACTIONS:
        print(f"Error: action_type must be one of {VALID_ACTIONS}")
        return
    if args.entity_type == "campaign" and not args.entity_id:
        print("Error: --entity-id (campaign ID) is required when --entity-type is 'campaign'")
        return
    if args.action_type in ("increase_budget", "decrease_budget") and not args.action_percent:
        print("Error: --action-percent is required for budget actions")
        return
    if args.action_type in ("pause_campaign", "enable_campaign") and args.entity_type != "campaign":
        print("Error: pause/enable_campaign requires --entity-type campaign")
        return

    rule = {
        "id": uuid.uuid4().hex[:8],
        "name": args.name,
        "customer_id": format_customer_id(args.customer_id),
        "entity_type": args.entity_type,
        "entity_id": args.entity_id,
        "condition": {
            "metric": args.metric,
            "operator": args.operator,
            "threshold": args.threshold,
            "lookback_days": args.lookback_days,
        },
        "action": {
            "type": args.action_type,
            "percent_change": args.action_percent,
            "max_budget_micros": int(args.max_budget * 1_000_000) if args.max_budget else None,
            "min_budget_micros": int(args.min_budget * 1_000_000) if args.min_budget else None,
        },
        "cooldown_hours": args.cooldown_hours,
        "active": True,
        "created_at": datetime.now().isoformat(),
        "last_evaluated": None,
        "last_triggered": None,
    }
    data = _load_rules()
    data["rules"].append(rule)
    _save_rules(data)
    op = "<" if args.operator == "less_than" else ">"
    print(f"Rule created (ID: {rule['id']})")
    print(f"  Condition: {args.metric} {op} {args.threshold} over last {args.lookback_days} days")
    print(f"  Action:    {args.action_type}" + (f" {args.action_percent}%" if args.action_percent else ""))
    print(f"  Cooldown:  {args.cooldown_hours}h between triggers")
    print(f"\nTest with: google-ads evaluate-rules --customer-id {args.customer_id} --dry-run")


def cmd_list_rules(client, args):
    data = _load_rules()
    rules = data.get("rules", [])
    if getattr(args, 'customer_id', None):
        rules = [r for r in rules if r.get("customer_id") == format_customer_id(args.customer_id)]
    if not rules:
        print("No rules found. Use 'google-ads create-rule' to add your first rule.")
        return
    print(f"Saved Rules ({len(rules)} total)")
    print("=" * 70)
    for r in rules:
        c = r["condition"]
        a = r["action"]
        op = "<" if c["operator"] == "less_than" else ">"
        status = "ACTIVE" if r["active"] else "INACTIVE"
        action_desc = a["type"] + (f" {a['percent_change']}%" if a.get("percent_change") else "")
        print(f"\n[{r['id']}] {r['name']}  ({status})")
        print(f"  Account:   {r['customer_id']}")
        print(f"  Condition: {c['metric']} {op} {c['threshold']}  (last {c['lookback_days']} days)")
        print(f"  Action:    {action_desc}")
        print(f"  Cooldown:  {r.get('cooldown_hours', 24)}h  |  Last evaluated: {r.get('last_evaluated') or 'never'}  |  Last triggered: {r.get('last_triggered') or 'never'}")
        print("-" * 70)


def cmd_delete_rule(client, args):
    data = _load_rules()
    original = len(data["rules"])
    data["rules"] = [r for r in data["rules"] if r["id"] != args.rule_id]
    if len(data["rules"]) == original:
        print(f"No rule found with ID '{args.rule_id}'. Run 'google-ads list-rules' to see all IDs.")
        return
    _save_rules(data)
    print(f"Rule '{args.rule_id}' deleted.")


def cmd_evaluate_rules(client, args):
    fid = format_customer_id(args.customer_id)
    data = _load_rules()
    active_rules = [r for r in data["rules"] if r["active"] and r["customer_id"] == fid]
    if not active_rules:
        print(f"No active rules for account {fid}. Use 'google-ads create-rule' to add rules.")
        return

    dry_run = getattr(args, 'dry_run', False)
    mode_label = " [DRY RUN — no changes will be made]" if dry_run else ""
    print(f"Rule Evaluation — Account {fid}{mode_label}")
    print(f"Evaluated at: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"Rules checked: {len(active_rules)}")
    print("=" * 70)

    now = datetime.now()
    for rule in active_rules:
        c = rule["condition"]
        op_sym = "<" if c["operator"] == "less_than" else ">"
        print(f"\n[{rule['id']}] {rule['name']}")
        print(f"  Condition: {c['metric']} {op_sym} {c['threshold']} (last {c['lookback_days']} days)")

        last_triggered = rule.get("last_triggered")
        if last_triggered and not dry_run:
            elapsed = (now - datetime.fromisoformat(last_triggered)).total_seconds() / 3600
            cooldown = rule.get("cooldown_hours", 24)
            if elapsed < cooldown:
                print(f"  Status:    SKIPPED — cooldown active ({cooldown - elapsed:.1f}h remaining)")
                print("-" * 70)
                continue

        try:
            value = _query_metric_value(client, fid, c["metric"], rule["entity_type"], rule.get("entity_id"), c["lookback_days"])
        except Exception as e:
            print(f"  Status:    ERROR querying metric — {e}")
            print("-" * 70)
            continue

        if value is None:
            print(f"  Status:    No data returned for {c['metric']}")
            print("-" * 70)
            continue

        print(f"  Current:   {c['metric']} = {value:.2f}")
        condition_met = value < c["threshold"] if c["operator"] == "less_than" else value > c["threshold"]

        if not condition_met:
            print("  Status:    Condition NOT met — no action")
        else:
            print("  Status:    Condition MET ✓")
            result = _execute_rule_action(client, fid, rule, dry_run)
            print(f"  Action:    {result}")
            if not dry_run:
                rule["last_triggered"] = now.isoformat()

        rule["last_evaluated"] = now.isoformat()
        print("-" * 70)

    if not dry_run:
        _save_rules(data)
    print("\nDone.")


# ─── Argument Parser ──────────────────────────────────────────────────────────

def build_parser():
    parser = argparse.ArgumentParser(
        prog='google-ads',
        description='Google Ads Manager — full account management CLI'
    )
    sub = parser.add_subparsers(dest='command', required=True)

    def cid(p):
        p.add_argument('--customer-id', required=True, help='10-digit Google Ads customer ID')

    def days_arg(p, default=30):
        p.add_argument('--days', type=int, default=default, help=f'Days to look back (default: {default})')

    def campaign_filter(p):
        p.add_argument('--campaign-id', default=None, help='Optional: filter to one campaign')

    def limit_arg(p, default=100):
        p.add_argument('--limit', type=int, default=default, help=f'Max rows (default: {default})')

    # Account
    sub.add_parser('list-accounts', help='List all accessible Google Ads accounts')
    p = sub.add_parser('list-clients', help='List client accounts under a manager account')
    cid(p)
    p = sub.add_parser('account-currency', help='Show account name and currency')
    cid(p)

    # Reporting
    p = sub.add_parser('campaign-performance', help='Campaign metrics (spend, clicks, CPA, etc.)')
    cid(p); days_arg(p); campaign_filter(p)

    p = sub.add_parser('ad-performance', help='Ad-level performance metrics')
    cid(p); days_arg(p); campaign_filter(p)

    p = sub.add_parser('keyword-performance', help='Keyword metrics with criterion IDs for bid updates')
    cid(p); days_arg(p); campaign_filter(p); limit_arg(p)

    p = sub.add_parser('search-terms', help='Actual search queries that triggered your ads')
    cid(p); days_arg(p); campaign_filter(p); limit_arg(p, 200)
    p.add_argument('--min-impressions', type=int, default=10, help='Min impressions filter (default: 10)')

    p = sub.add_parser('geo-performance', help='Performance by country/region')
    cid(p); days_arg(p); campaign_filter(p); limit_arg(p)

    p = sub.add_parser('auction-insights', help='Competitor impression share and overlap metrics')
    cid(p); days_arg(p); campaign_filter(p)

    p = sub.add_parser('quality-scores', help='Keyword quality scores (lowest first)')
    cid(p); campaign_filter(p)
    p.add_argument('--limit', type=int, default=200)

    p = sub.add_parser('budget-pacing', help='MTD spend vs. expected budget pacing')
    cid(p)

    p = sub.add_parser('change-history', help='Log of recent account changes')
    cid(p); days_arg(p, 7); limit_arg(p)

    p = sub.add_parser('ad-creatives', help='Ad headlines, descriptions, and URLs')
    cid(p); campaign_filter(p)

    p = sub.add_parser('image-assets', help='List all image assets in the account')
    cid(p)

    p = sub.add_parser('query', help='Run any custom GAQL query')
    cid(p)
    p.add_argument('--query', required=True, help='GAQL query string')
    p.add_argument('--format', default='table', choices=['table', 'json', 'csv'])

    # Campaign management
    p = sub.add_parser('create-campaign-budget', help='Create a campaign budget')
    cid(p)
    p.add_argument('--name', required=True)
    p.add_argument('--amount-micros', type=int, required=True, help='Daily budget in micros (1000000 = 1 currency unit)')

    p = sub.add_parser('create-campaign', help='Create a new campaign (starts PAUSED)')
    cid(p)
    p.add_argument('--name', required=True)
    p.add_argument('--budget-resource', required=True, help='Budget resource name from create-campaign-budget')
    p.add_argument('--type', default='SEARCH', help='SEARCH, DISPLAY, SHOPPING, VIDEO (default: SEARCH)')
    p.add_argument('--bidding-strategy', default='MANUAL_CPC')

    p = sub.add_parser('update-campaign-status', help='Enable, pause, or remove a campaign')
    cid(p)
    p.add_argument('--campaign-id', required=True)
    p.add_argument('--status', required=True, help='ENABLED, PAUSED, or REMOVED')

    p = sub.add_parser('update-budget', help='Update a campaign budget amount')
    cid(p)
    p.add_argument('--budget-id', required=True)
    p.add_argument('--amount-micros', type=int, required=True)

    # Ad group
    p = sub.add_parser('create-ad-group', help='Create a new ad group')
    cid(p)
    p.add_argument('--campaign-id', required=True)
    p.add_argument('--name', required=True)
    p.add_argument('--cpc-bid-micros', type=int, default=None)

    p = sub.add_parser('update-ad-group', help='Update ad group name, status, or default bid')
    cid(p)
    p.add_argument('--ad-group-id', required=True)
    p.add_argument('--name', default=None)
    p.add_argument('--status', default=None, help='ENABLED or PAUSED')
    p.add_argument('--cpc-bid-micros', type=int, default=None)

    # Ads
    p = sub.add_parser('create-rsa', help='Create a responsive search ad (starts PAUSED)')
    cid(p)
    p.add_argument('--ad-group-id', required=True)
    p.add_argument('--headlines', required=True, help='JSON array of headline strings (3-15 items)')
    p.add_argument('--descriptions', required=True, help='JSON array of description strings (2-4 items)')
    p.add_argument('--final-url', required=True)
    p.add_argument('--path1', default=None)
    p.add_argument('--path2', default=None)

    p = sub.add_parser('update-ad-status', help='Enable, pause, or remove a specific ad')
    cid(p)
    p.add_argument('--ad-group-id', required=True)
    p.add_argument('--ad-id', required=True)
    p.add_argument('--status', required=True, help='ENABLED, PAUSED, or REMOVED')

    p = sub.add_parser('update-rsa', help='Update headlines and/or descriptions on a responsive search ad')
    cid(p)
    p.add_argument('--ad-group-id', required=True)
    p.add_argument('--ad-id', required=True)
    p.add_argument('--headlines', default=None, help='JSON array of new headlines')
    p.add_argument('--descriptions', default=None, help='JSON array of new descriptions')

    p = sub.add_parser('duplicate-ad', help='Duplicate a responsive search ad (copy goes to PAUSED)')
    cid(p)
    p.add_argument('--ad-group-id', required=True, help='Source ad group ID')
    p.add_argument('--ad-id', required=True)
    p.add_argument('--target-ad-group-id', default=None, help='Destination ad group (default: same as source)')

    # Keywords
    p = sub.add_parser('add-keywords', help='Add keywords to an ad group')
    cid(p)
    p.add_argument('--ad-group-id', required=True)
    p.add_argument('--keywords', required=True, help='JSON array: [{"text":"...", "match_type":"BROAD"}] or ["word1","word2"]')

    p = sub.add_parser('add-negative-keywords', help='Add negative keywords to an ad group')
    cid(p)
    p.add_argument('--ad-group-id', required=True)
    p.add_argument('--keywords', required=True, help='JSON array of negative keywords')

    p = sub.add_parser('update-keyword-status', help='Enable or pause a keyword')
    cid(p)
    p.add_argument('--ad-group-id', required=True)
    p.add_argument('--criterion-id', required=True)
    p.add_argument('--status', required=True, help='ENABLED or PAUSED')

    p = sub.add_parser('update-keyword-bid', help='Update max CPC bid for a keyword')
    cid(p)
    p.add_argument('--ad-group-id', required=True)
    p.add_argument('--criterion-id', required=True)
    p.add_argument('--cpc-bid-micros', type=int, required=True, help='New bid in micros (1000000 = 1 currency unit)')

    p = sub.add_parser('bulk-update-bids', help='Update multiple keyword bids in one API call')
    cid(p)
    p.add_argument('--updates', required=True, help='JSON array: [{"ad_group_id":"...","criterion_id":"...","cpc_bid_micros":1500000}]')

    # Extensions
    p = sub.add_parser('add-sitelink', help='Add a sitelink extension to a campaign')
    cid(p)
    p.add_argument('--campaign-id', required=True)
    p.add_argument('--text', required=True, help='Display text (max 25 chars)')
    p.add_argument('--url', required=True, help='Landing page URL')
    p.add_argument('--desc1', default=None, help='Description line 1 (max 35 chars)')
    p.add_argument('--desc2', default=None, help='Description line 2 (max 35 chars)')

    p = sub.add_parser('add-callout', help='Add a callout extension to a campaign')
    cid(p)
    p.add_argument('--campaign-id', required=True)
    p.add_argument('--text', required=True, help='Callout text (max 25 chars)')

    p = sub.add_parser('list-extensions', help='List all extensions on a campaign')
    cid(p)
    p.add_argument('--campaign-id', required=True)

    # Conversions
    p = sub.add_parser('list-conversions', help='List all conversion actions in the account')
    cid(p)

    p = sub.add_parser('create-conversion', help='Create a new website conversion action')
    cid(p)
    p.add_argument('--name', required=True)
    p.add_argument('--category', required=True, help='PURCHASE, LEAD, SIGNUP, REQUEST_DEMO, PAGE_VIEW, DOWNLOAD, or OTHER')
    p.add_argument('--click-lookback-days', type=int, default=30)
    p.add_argument('--view-lookback-days', type=int, default=1)

    # Ad Schedule
    p = sub.add_parser('set-schedule', help='Set ad scheduling (dayparting) for a campaign')
    cid(p)
    p.add_argument('--campaign-id', required=True)
    p.add_argument('--schedules', required=True, help='JSON array: [{"day_of_week":"MONDAY","start_hour":8,"end_hour":18}]')

    # Rules engine
    p = sub.add_parser('create-rule', help='Create an automation rule that monitors a metric and takes action')
    cid(p)
    p.add_argument('--name', required=True)
    p.add_argument('--metric', required=True, help='cpa, spend, ctr, conversions, clicks, or impressions')
    p.add_argument('--operator', required=True, help='less_than or greater_than')
    p.add_argument('--threshold', type=float, required=True)
    p.add_argument('--lookback-days', type=int, default=7)
    p.add_argument('--action-type', required=True, help='increase_budget, decrease_budget, pause_campaign, enable_campaign, or alert_only')
    p.add_argument('--entity-type', default='account', help='account or campaign (default: account)')
    p.add_argument('--entity-id', default=None, help='Campaign ID (required when --entity-type is campaign)')
    p.add_argument('--action-percent', type=float, default=None, help='Budget change % (required for budget actions)')
    p.add_argument('--max-budget', type=float, default=None, help='Budget cap in account currency')
    p.add_argument('--min-budget', type=float, default=None, help='Budget floor in account currency')
    p.add_argument('--cooldown-hours', type=int, default=24)

    p = sub.add_parser('list-rules', help='List all saved automation rules')
    p.add_argument('--customer-id', default=None, help='Filter by account (optional)')

    p = sub.add_parser('delete-rule', help='Delete a rule by ID')
    p.add_argument('--rule-id', required=True)

    p = sub.add_parser('evaluate-rules', help='Evaluate all active rules and execute triggered actions')
    cid(p)
    p.add_argument('--dry-run', action='store_true', help='Preview actions without making any changes')

    return parser


COMMAND_MAP = {
    'list-accounts': cmd_list_accounts,
    'list-clients': cmd_list_clients,
    'account-currency': cmd_account_currency,
    'campaign-performance': cmd_campaign_performance,
    'ad-performance': cmd_ad_performance,
    'keyword-performance': cmd_keyword_performance,
    'search-terms': cmd_search_terms,
    'geo-performance': cmd_geo_performance,
    'auction-insights': cmd_auction_insights,
    'quality-scores': cmd_quality_scores,
    'budget-pacing': cmd_budget_pacing,
    'change-history': cmd_change_history,
    'ad-creatives': cmd_ad_creatives,
    'image-assets': cmd_image_assets,
    'query': cmd_query,
    'create-campaign-budget': cmd_create_campaign_budget,
    'create-campaign': cmd_create_campaign,
    'update-campaign-status': cmd_update_campaign_status,
    'update-budget': cmd_update_budget,
    'create-ad-group': cmd_create_ad_group,
    'update-ad-group': cmd_update_ad_group,
    'create-rsa': cmd_create_rsa,
    'update-ad-status': cmd_update_ad_status,
    'update-rsa': cmd_update_rsa,
    'duplicate-ad': cmd_duplicate_ad,
    'add-keywords': cmd_add_keywords,
    'add-negative-keywords': cmd_add_negative_keywords,
    'update-keyword-status': cmd_update_keyword_status,
    'update-keyword-bid': cmd_update_keyword_bid,
    'bulk-update-bids': cmd_bulk_update_bids,
    'add-sitelink': cmd_add_sitelink,
    'add-callout': cmd_add_callout,
    'list-extensions': cmd_list_extensions,
    'list-conversions': cmd_list_conversions,
    'create-conversion': cmd_create_conversion,
    'set-schedule': cmd_set_schedule,
    'create-rule': cmd_create_rule,
    'list-rules': cmd_list_rules,
    'delete-rule': cmd_delete_rule,
    'evaluate-rules': cmd_evaluate_rules,
}


def main():
    parser = build_parser()
    args = parser.parse_args()

    # Commands that don't need credentials for setup check
    no_auth_commands = {'list-rules', 'delete-rule'}

    try:
        client = GoogleAdsClient()
    except ValueError as e:
        if args.command not in no_auth_commands:
            print(f"Error: {e}")
            sys.exit(1)
        client = None

    try:
        COMMAND_MAP[args.command](client, args)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
