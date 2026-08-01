package com.acme.export

import org.apache.spark.sql.SparkSession

object GenerateCsv {

  def main(args: Array[String]): Unit = {
    val spark = SparkSession.builder().appName("csv-export").getOrCreate()

    val orders = spark.sql(
      "SELECT order_id, customer_id, amount FROM orders WHERE amount > 0"
    )

    orders.write.mode("overwrite").csv("/data/exports/orders.csv")
  }
}

