WITH daily AS (
 SELECT settlement_date d, SUM(status='Settled') settled, COUNT(*) total
 FROM fact_settlement GROUP BY settlement_date)
SELECT ROUND(100.0*(SELECT settled FROM daily WHERE d='2026-09-04')
                  /(SELECT total   FROM daily WHERE d='2026-09-04'),1) AS rate_yesterday_pct,
       ROUND(100.0*SUM(settled)/SUM(total),1) AS rate_4wk_avg_pct
FROM daily;
