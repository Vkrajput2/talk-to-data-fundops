SELECT f.fund_name, s.process, s.pct_of_sla, s.status
FROM fact_sla s JOIN dim_fund f ON s.fund_id=f.fund_id
WHERE s.status='Pending' AND s.pct_of_sla>=80
ORDER BY s.pct_of_sla DESC;
