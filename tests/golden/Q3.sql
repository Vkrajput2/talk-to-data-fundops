SELECT f.fund_name, t.security_name, t.asset_class, s.broker_name,
       s.fail_reason_code, s.fail_reason_text,
       t.gross_amount AS value, t.ccy
FROM fact_settlement s
JOIN fact_transaction t ON s.txn_id=t.txn_id
JOIN dim_fund f ON s.fund_id=f.fund_id
WHERE s.settlement_date='2026-09-04' AND s.status='Failed'
ORDER BY t.gross_amount DESC;
