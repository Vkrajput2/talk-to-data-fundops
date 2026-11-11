SELECT s.broker_name, COUNT(*) AS fails, SUM(t.gross_amount) AS fail_value
FROM fact_settlement s JOIN fact_transaction t ON s.txn_id=t.txn_id
WHERE s.settlement_date='2026-09-04' AND s.status='Failed'
GROUP BY s.broker_name ORDER BY fails DESC;
