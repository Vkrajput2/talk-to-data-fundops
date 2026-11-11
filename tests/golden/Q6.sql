SELECT p.security_name, p.exception_type, p.stale_days, p.price_source,
       f.fund_name, n.nav_status, n.dod_variance_pct
FROM fact_pricing_exception p
JOIN dim_fund f ON p.fund_id=f.fund_id
JOIN fact_nav n ON n.fund_id=f.fund_id AND n.nav_date='2026-09-04'
WHERE p.status='Open' AND p.stale_days>=2
ORDER BY p.stale_days DESC;
