-- Simple SQL data model on top of lakehouse.nyc_taxi.trips (raw NYC Yellow
-- Taxi trip records). No dbt — just plain DROP + CREATE TABLE AS SELECT
-- statements (DuckDB-Iceberg doesn't support CREATE OR REPLACE TABLE, found
-- live), safe to re-run any time the raw trips table is refreshed.
--
-- Layers:
--   trips (raw)                     -- as loaded, one row per trip
--   dim_vendor / dim_payment_type   -- small lookups for the raw table's
--   / dim_rate_code                    integer codes (NYC TLC data dictionary)
--   fct_trips                       -- cleaned, typed, derived columns, joined
--                                       to the dims; bad/nonsensical rows dropped
--   mart_daily_summary              -- daily rollup by vendor + payment type

-- ---------------------------------------------------------------------------
-- Dimensions (NYC TLC data dictionary codes, restricted to the values that
-- actually occur in this dataset)
-- ---------------------------------------------------------------------------

-- Built via CREATE TABLE AS SELECT (not CREATE TABLE + INSERT INTO) since
-- that's the write path proven to work against this DuckDB/Iceberg version.
DROP TABLE IF EXISTS lakehouse.nyc_taxi.dim_vendor;
CREATE TABLE lakehouse.nyc_taxi.dim_vendor AS
SELECT * FROM (VALUES
    (1, 'Creative Mobile Technologies, LLC'),
    (2, 'Curb Mobility, LLC'),
    (6, 'Myle Technologies Inc')
) AS t(vendor_id, vendor_name);

DROP TABLE IF EXISTS lakehouse.nyc_taxi.dim_payment_type;
CREATE TABLE lakehouse.nyc_taxi.dim_payment_type AS
SELECT * FROM (VALUES
    (0, 'Flex Fare trip'),
    (1, 'Credit card'),
    (2, 'Cash'),
    (3, 'No charge'),
    (4, 'Dispute'),
    (5, 'Unknown'),
    (6, 'Voided trip')
) AS t(payment_type_id, payment_type_name);

DROP TABLE IF EXISTS lakehouse.nyc_taxi.dim_rate_code;
CREATE TABLE lakehouse.nyc_taxi.dim_rate_code AS
SELECT * FROM (VALUES
    (1, 'Standard rate'),
    (2, 'JFK'),
    (3, 'Newark'),
    (4, 'Nassau or Westchester'),
    (5, 'Negotiated fare'),
    (6, 'Group ride'),
    (99, 'Unknown/null in source')
) AS t(rate_code_id, rate_code_name);

-- ---------------------------------------------------------------------------
-- Fact: one row per trip, cleaned + typed + joined to the dims above.
-- Obviously-bad rows are dropped: negative/zero amounts, a dropoff at or
-- before pickup, non-positive distance. RatecodeID nulls in the source map
-- to the 99 "unknown" row instead of being dropped (they're a real, sizeable
-- chunk of the data, not obviously bad — just unlabeled).
-- ---------------------------------------------------------------------------

DROP TABLE IF EXISTS lakehouse.nyc_taxi.fct_trips;
CREATE TABLE lakehouse.nyc_taxi.fct_trips AS
SELECT
    t.VendorID                                          AS vendor_id,
    v.vendor_name,
    t.tpep_pickup_datetime                               AS pickup_datetime,
    t.tpep_dropoff_datetime                              AS dropoff_datetime,
    CAST(t.tpep_pickup_datetime AS DATE)                 AS pickup_date,
    date_part('hour', t.tpep_pickup_datetime)             AS pickup_hour,
    date_diff('minute', t.tpep_pickup_datetime, t.tpep_dropoff_datetime) AS trip_duration_minutes,
    t.passenger_count,
    t.trip_distance                                      AS trip_distance_miles,
    coalesce(t.RatecodeID, 99)                            AS rate_code_id,
    r.rate_code_name,
    t.PULocationID                                        AS pu_location_id,
    t.DOLocationID                                        AS do_location_id,
    t.payment_type                                        AS payment_type_id,
    p.payment_type_name,
    t.fare_amount,
    t.extra                                               AS extra_amount,
    t.mta_tax,
    t.tip_amount,
    t.tolls_amount,
    t.improvement_surcharge,
    t.congestion_surcharge,
    t.Airport_fee                                         AS airport_fee,
    t.total_amount
FROM lakehouse.nyc_taxi.trips t
LEFT JOIN lakehouse.nyc_taxi.dim_vendor v ON v.vendor_id = t.VendorID
LEFT JOIN lakehouse.nyc_taxi.dim_payment_type p ON p.payment_type_id = t.payment_type
LEFT JOIN lakehouse.nyc_taxi.dim_rate_code r ON r.rate_code_id = coalesce(t.RatecodeID, 99)
WHERE t.fare_amount >= 0
  AND t.total_amount >= 0
  AND t.trip_distance > 0
  AND t.tpep_dropoff_datetime > t.tpep_pickup_datetime;

-- ---------------------------------------------------------------------------
-- Mart: daily rollup by vendor + payment type — the kind of question this
-- model exists to answer quickly without re-deriving it from raw trips.
-- ---------------------------------------------------------------------------

DROP TABLE IF EXISTS lakehouse.nyc_taxi.mart_daily_summary;
CREATE TABLE lakehouse.nyc_taxi.mart_daily_summary AS
SELECT
    pickup_date,
    vendor_name,
    payment_type_name,
    count(*)                          AS trip_count,
    sum(passenger_count)              AS total_passengers,
    round(sum(total_amount), 2)       AS total_revenue,
    round(avg(fare_amount), 2)        AS avg_fare,
    round(avg(tip_amount), 2)         AS avg_tip,
    round(avg(trip_distance_miles), 2) AS avg_trip_distance_miles,
    round(avg(trip_duration_minutes), 1) AS avg_duration_minutes
FROM lakehouse.nyc_taxi.fct_trips
GROUP BY pickup_date, vendor_name, payment_type_name
ORDER BY pickup_date, vendor_name, payment_type_name;
