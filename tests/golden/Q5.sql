SELECT b.break_id, f.fund_name, b.break_type, b.break_amount, b.ccy,
       b.age_days, b.owner, b.custodian
FROM fact_recon_break b JOIN dim_fund f ON b.fund_id=f.fund_id
WHERE b.status='Open' AND b.age_days>5
ORDER BY b.age_days DESC, b.break_amount DESC;
