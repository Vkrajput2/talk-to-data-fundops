SELECT f.fund_name, ROUND(c.cash_pct_aum,2) AS cash_pct_aum,
       c.available_balance, c.ccy
FROM fact_cash_balance c JOIN dim_fund f ON c.fund_id=f.fund_id
WHERE c.balance_date='2026-09-04' AND c.cash_pct_aum < 2.0
ORDER BY c.cash_pct_aum ASC;
