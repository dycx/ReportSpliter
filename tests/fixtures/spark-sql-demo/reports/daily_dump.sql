-- 日报导出
INSERT OVERWRITE TABLE daily_dump
SELECT order_id, customer_id, amount, order_dt
FROM orders
WHERE order_dt = '2026-07-01'

