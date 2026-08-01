-- 月度销售报表
INSERT OVERWRITE TABLE monthly_sales
SELECT region,
       SUM(total_amount) AS monthly_amount,
       MAX(order_count) AS max_order_count
FROM sales_agg
GROUP BY region

