-- install_clickhouse.sh replaces @RAW@ with an absolute path, and @TRAFFIC@
-- with the Iceberg table: icebergLocal() over the warehouse folder, or the
-- REST catalog's netmon_lake.`netmon.traffic`.
-- The views read files as netmon_views (users.d/grafana.xml), so their
-- readers need SELECT on netmon.* only.
CREATE DATABASE IF NOT EXISTS netmon;

-- Hours the converter has loaded: the Iceberg table, at its current
-- snapshot.
CREATE OR REPLACE VIEW netmon.traffic
DEFINER = netmon_views SQL SECURITY DEFINER AS
SELECT * FROM @TRAFFIC@;

-- The Iceberg table plus rows still only in raw files (the current hour and
-- any the converter has not loaded yet), for dashboards that need recent
-- data. Raw files RAW/[HOST/]DAY/HH.jsonl, named as the converter needs, are
-- read for the last two UTC days only. ClickHouse skips files by path
-- without opening them, so a file is skipped when Iceberg already has its
-- host folder's hour, or with no host folder, any host's. Folders starting
-- with . # @ (Synology's #recycle, @eaDir) are skipped, and DISTINCT counts
-- rows in two copies of a file (say a pi.bak folder) once.
CREATE OR REPLACE VIEW netmon.traffic_live
DEFINER = netmon_views SQL SECURITY DEFINER AS
SELECT ts, host, interval_s, pid, uid, comm, tcp_tx, tcp_rx, udp_tx, udp_rx,
    nfs_read, nfs_write, scale
FROM netmon.traffic
UNION ALL
WITH
    -- Empty parts dropped: some versions report ** matches with doubled
    -- slashes (DAY//HH.jsonl).
    arrayFilter(p -> p != '', splitByChar('/', substring(_path, length('@RAW@') + 1)))
        AS parts,
    parts[-3] AS folder,  -- '' when RAW has no host folders
    parseDateTimeOrNull(
        concat(parts[-2], ' ', parts[-1]), '%Y-%m-%d %H.jsonl', 'UTC'
    ) AS hour
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
WHERE hour >= toDate(now('UTC')) - 1
    AND NOT match(substring(_path, length('@RAW@') + 1), '/[.#@]')
    AND (folder, hour) NOT IN (
        SELECT DISTINCT arrayJoin([host, '']), toStartOfHour(ts)
        FROM netmon.traffic
        WHERE ts >= toDate(now('UTC')) - 1
    )
    -- Rows, too: a renamed Pi's file has both names in it.
    AND (host, toStartOfHour(toDateTime(ts, 'UTC'))) NOT IN (
        SELECT DISTINCT host, toStartOfHour(ts)
        FROM netmon.traffic
        WHERE ts >= toDate(now('UTC')) - 1
    )
-- A line still being written is skipped, not an error.
SETTINGS input_format_allow_errors_num = 100;
