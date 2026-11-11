SELECT f.fund_id, f.fund_name, f.region, f.strategy,
       ROUND(n.total_net_assets/1e9,2) AS aum_bn, f.base_ccy
FROM dim_fund f
JOIN fact_nav n ON f.fund_id=n.fund_id AND n.nav_date='2026-09-04'
ORDER BY aum_bn DESC;
