WITH fails AS (SELECT fund_id,COUNT(*) c FROM fact_settlement WHERE settlement_date='2026-09-04' AND status='Failed' GROUP BY fund_id),
 aged AS (SELECT fund_id,COUNT(*) c FROM fact_recon_break WHERE status='Open' AND age_days>5 GROUP BY fund_id),
 lowcash AS (SELECT fund_id,1 c FROM fact_cash_balance WHERE balance_date='2026-09-04' AND cash_pct_aum<2.0),
 px AS (SELECT fund_id,COUNT(*) c FROM fact_pricing_exception WHERE status='Open' AND stale_days>=2 GROUP BY fund_id),
 slar AS (SELECT fund_id,1 c FROM fact_sla WHERE status='Pending' AND pct_of_sla>=80),
 navr AS (SELECT fund_id,1 c FROM fact_nav WHERE nav_date='2026-09-04' AND nav_status='Under Review'),
 outfl AS (SELECT fund_id,1 c FROM fact_flows WHERE month='2026-09' AND net_flow<=-100000000)
SELECT f.fund_name,
   (3*COALESCE(fails.c,0)+2*COALESCE(aged.c,0)+2*COALESCE(lowcash.c,0)+2*COALESCE(px.c,0)
     +1*COALESCE(slar.c,0)+2*COALESCE(navr.c,0)+2*COALESCE(outfl.c,0)) AS attention_score
FROM dim_fund f
LEFT JOIN fails ON f.fund_id=fails.fund_id LEFT JOIN aged ON f.fund_id=aged.fund_id
LEFT JOIN lowcash ON f.fund_id=lowcash.fund_id LEFT JOIN px ON f.fund_id=px.fund_id
LEFT JOIN slar ON f.fund_id=slar.fund_id LEFT JOIN navr ON f.fund_id=navr.fund_id
LEFT JOIN outfl ON f.fund_id=outfl.fund_id
ORDER BY attention_score DESC LIMIT 3;
