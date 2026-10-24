-- install_clickhouse.sh replaces @WAREHOUSE@ and @RAW@ with absolute paths.
CREATE DATABASE IF NOT EXISTS netmon;

-- Hours the converter has loaded: the Iceberg table, at its latest snapshot.
CREATE OR REPLACE VIEW netmon.traffic AS
SELECT * FROM icebergLocal('@WAREHOUSE@/netmon/traffic');

-- The Iceberg table plus rows still only in raw files (the current hour and
-- any the converter has not loaded yet), for dashboards that need recent
-- data. Raw files are read for the last two UTC days only; the date in each
-- path lets ClickHouse skip opening older files.
CREATE OR REPLACE VIEW netmon.traffic_live AS
SELECT ts, host, interval_s, pid, uid, comm, tcp_tx, tcp_rx, udp_tx, udp_rx,
    nfs_read, nfs_write, scale
FROM netmon.traffic
UNION ALL
SELECT toDateTime64(ts, 6, 'UTC') AS ts, host, interval_s, pid, uid, comm,
    tcp_tx, tcp_rx, udp_tx, udp_rx, nfs_read, nfs_write, scale
FROM file(
    '@RAW@/**/*.jsonl', JSONEachRow,
    'ts UInt32, host String, interval_s Int32, pid Int64, uid Int64,
     comm String, tcp_tx Int64, tcp_rx Int64, udp_tx Int64, udp_rx Int64,
     nfs_read Int64, nfs_write Int64, scale Float64'
)
-- LIKE, not a regex: some versions report ** matches with a doubled slash.
WHERE (_path LIKE concat('%/', toString(toDate(now('UTC'))), '/%')
        OR _path LIKE concat('%/', toString(toDate(now('UTC')) - 1), '/%'))
    AND (host, toStartOfHour(toDateTime(ts, 'UTC'))) NOT IN (
        SELECT DISTINCT host, toStartOfHour(ts)
        FROM netmon.traffic
        WHERE ts >= toDate(now('UTC')) - 1
    )
-- A line still being written is skipped, not an error.
SETTINGS input_format_allow_errors_num = 100;
