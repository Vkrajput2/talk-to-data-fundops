WITH r AS (SELECT month, mgmt_fee_revenue rev, avg_aum, fee_rate_pct
           FROM fact_revenue WHERE fund_id='F001')
SELECT (SELECT rev FROM r WHERE month='2026-08')  AS rev_2026,
       (SELECT rev FROM r WHERE month='2025-08')  AS rev_2025,
       ROUND(100.0*((SELECT rev FROM r WHERE month='2026-08')-(SELECT rev FROM r WHERE month='2025-08'))
             /(SELECT rev FROM r WHERE month='2025-08'),1) AS rev_yoy_pct,
       ROUND(100.0*((SELECT avg_aum FROM r WHERE month='2026-08')-(SELECT avg_aum FROM r WHERE month='2025-08'))
             /(SELECT avg_aum FROM r WHERE month='2025-08'),1) AS avg_aum_yoy_pct,
       (SELECT fee_rate_pct FROM r WHERE month='2025-08') AS fee_rate_2025,
       (SELECT fee_rate_pct FROM r WHERE month='2026-08') AS fee_rate_2026;
