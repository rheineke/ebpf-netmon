-- install_clickhouse.sh replaces @WAREHOUSE@ and @RAW@ with absolute paths.
-- The views read files as netmon_views (users.d/grafana.xml), so their
-- readers need SELECT on netmon.* only.
CREATE DATABASE IF NOT EXISTS netmon;

-- Hours the converter has loaded: the Iceberg table, at its latest snapshot.
CREATE OR REPLACE VIEW netmon.traffic
DEFINER = netmon_views SQL SECURITY DEFINER AS
SELECT * FROM icebergLocal('@WAREHOUSE@/netmon/traffic');

-- The Iceberg table plus rows still only in raw files (the current hour and
-- any the converter has not loaded yet), for dashboards that need recent
-- data. Raw files are read for the last two UTC days only; the date in each
-- path lets ClickHouse skip opening older files. Folders starting with . # @
-- (Synology's #recycle, @eaDir) are skipped, and DISTINCT counts rows in
-- two copies of a file (say a pi.bak folder) once.
CREATE OR REPLACE VIEW netmon.traffic_live
DEFINER = netmon_views SQL SECURITY DEFINER AS
SELECT ts, host, interval_s, pid, uid, comm, tcp_tx, tcp_rx, udp_tx, udp_rx,
    nfs_read, nfs_write, scale
FROM netmon.traffic
UNION ALL
SELECT DISTINCT toDateTime64(ts, 6, 'UTC') AS ts, host, interval_s, pid, uid,
    -- As the converter does: the kernel cuts names mid-character.
    toValidUTF8(comm) AS comm,
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
    AND NOT match(substring(_path, length('@RAW@') + 1), '/[.#@]')
    AND (host, toStartOfHour(toDateTime(ts, 'UTC'))) NOT IN (
        SELECT DISTINCT host, toStartOfHour(ts)
        FROM netmon.traffic
        WHERE ts >= toDate(now('UTC')) - 1
    )
-- A line still being written is skipped, not an error.
SETTINGS input_format_allow_errors_num = 100;
