-- 中间层：订单 × 客户聚合
CREATE TABLE sales_agg AS
SELECT o.region,
       SUM(o.amount) AS total_amount,
       COUNT(DISTINCT o.order_id) AS order_count
FROM orders o
JOIN customers c ON o.customer_id = c.customer_id
GROUP BY o.region

