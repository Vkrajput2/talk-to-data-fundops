SELECT f.fund_name,
       MAX(CASE WHEN fl.month='2026-09' THEN fl.net_flow END) AS net_flow_mtd,
       MAX(CASE WHEN fl.month='2026-08' THEN fl.net_flow END) AS net_flow_prior,
       fl.ccy
FROM fact_flows fl JOIN dim_fund f ON fl.fund_id=f.fund_id
GROUP BY f.fund_name
ORDER BY MAX(CASE WHEN fl.month='2026-09' THEN fl.net_flow END) ASC;
