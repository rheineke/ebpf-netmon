-- install_clickhouse.sh replaces @RAW@ with an absolute path and makes the
-- Iceberg tables netmon_lake.`netmon.traffic` and netmon_lake.`netmon.interfaces`:
-- the REST catalog's, or IcebergLocal tables over their warehouse folders.
-- The views of the tables are plain (SQL SECURITY INVOKER), so ClickHouse
-- takes a query's time filter down to the table and opens only the days it
-- needs. Through a view that runs as another user, 26.11 and later open
-- every data file. So readers need SELECT on netmon.* and the two tables.
-- Only the views of raw files run as netmon_views (users.d/grafana.xml),
-- which may read files.
CREATE DATABASE IF NOT EXISTS netmon;

-- Hours the converter has loaded: the Iceberg table, at its current
-- snapshot.
CREATE OR REPLACE VIEW netmon.traffic
SQL SECURITY INVOKER AS
SELECT * FROM netmon_lake.`netmon.traffic`;

-- Rows still only in raw files: the current hour and any the converter has
-- not loaded yet. Raw files RAW/[HOST/]DAY/HH.jsonl, named as the converter
-- needs, are read for the last two UTC days only. ClickHouse skips files by
-- path without opening them, so a file is skipped when Iceberg already has
-- its host folder's hour, or with no host folder, any host's. Folders
-- starting with . # @ (Synology's #recycle, @eaDir) are skipped, and
-- DISTINCT counts rows in two copies of a file (say a pi.bak folder) once.
CREATE OR REPLACE VIEW netmon.traffic_raw
DEFINER = netmon_views SQL SECURITY DEFINER AS
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
    tcp_tx, tcp_rx, udp_tx, udp_rx, nfs_read, nfs_write, scale, nfs_fetched
FROM file(
    '@RAW@/**/*.jsonl', JSONEachRow,
    'ts UInt32, host String, interval_s Int32, pid Int64, uid Int64,
     comm String, tcp_tx Int64, tcp_rx Int64, udp_tx Int64, udp_rx Int64,
     nfs_read Int64, nfs_write Int64, scale Float64,
     nfs_fetched Nullable(Int64)'
)
WHERE hour >= toDate(now('UTC')) - 1
    -- HH.interfaces.jsonl: the interfaces' rows, below.
    AND NOT endsWith(_path, '.interfaces.jsonl')
    AND NOT match(substring(_path, length('@RAW@') + 1), '/[.#@]')
    AND (folder, hour) NOT IN (
        SELECT DISTINCT arrayJoin([host, '']), toStartOfHour(ts)
        FROM netmon_lake.`netmon.traffic`
        WHERE ts >= toDate(now('UTC')) - 1
    )
    -- Rows, too: a renamed Pi's file has both names in it.
    AND (host, toStartOfHour(toDateTime(ts, 'UTC'))) NOT IN (
        SELECT DISTINCT host, toStartOfHour(ts)
        FROM netmon_lake.`netmon.traffic`
        WHERE ts >= toDate(now('UTC')) - 1
    )
-- A line still being written is skipped, not an error.
SETTINGS input_format_allow_errors_num = 100;

-- The Iceberg table plus rows still only in raw files, for dashboards that
-- need recent data.
CREATE OR REPLACE VIEW netmon.traffic_live
SQL SECURITY INVOKER AS
SELECT ts, host, interval_s, pid, uid, comm, tcp_tx, tcp_rx, udp_tx, udp_rx,
    nfs_read, nfs_write, scale, nfs_fetched
FROM netmon.traffic
UNION ALL
SELECT * FROM netmon.traffic_raw;

-- What each network interface moved per report, headers and all: hours the
-- converter has loaded, and then with the raw files' recent rows, as above.
CREATE OR REPLACE VIEW netmon.interfaces
SQL SECURITY INVOKER AS
SELECT * FROM netmon_lake.`netmon.interfaces`;

CREATE OR REPLACE VIEW netmon.interfaces_raw
DEFINER = netmon_views SQL SECURITY DEFINER AS
WITH
    arrayFilter(p -> p != '', splitByChar('/', substring(_path, length('@RAW@') + 1)))
        AS parts,
    parts[-3] AS folder,
    parseDateTimeOrNull(
        concat(parts[-2], ' ', parts[-1]), '%Y-%m-%d %H.interfaces.jsonl', 'UTC'
    ) AS hour
SELECT DISTINCT toDateTime64(ts, 6, 'UTC') AS ts, host, interval_s,
    toValidUTF8(interface) AS interface, rx_bytes, rx_packets, tx_bytes,
    tx_packets
FROM file(
    '@RAW@/**/*.interfaces.jsonl', JSONEachRow,
    'ts UInt32, host String, interval_s Int32, interface String,
     rx_bytes Int64, rx_packets Int64, tx_bytes Int64, tx_packets Int64'
)
WHERE hour >= toDate(now('UTC')) - 1
    AND NOT match(substring(_path, length('@RAW@') + 1), '/[.#@]')
    AND (folder, hour) NOT IN (
        SELECT DISTINCT arrayJoin([host, '']), toStartOfHour(ts)
        FROM netmon_lake.`netmon.interfaces`
        WHERE ts >= toDate(now('UTC')) - 1
    )
    AND (host, toStartOfHour(toDateTime(ts, 'UTC'))) NOT IN (
        SELECT DISTINCT host, toStartOfHour(ts)
        FROM netmon_lake.`netmon.interfaces`
        WHERE ts >= toDate(now('UTC')) - 1
    )
SETTINGS input_format_allow_errors_num = 100;

CREATE OR REPLACE VIEW netmon.interfaces_live
SQL SECURITY INVOKER AS
SELECT ts, host, interval_s, interface, rx_bytes, rx_packets, tx_bytes,
    tx_packets
FROM netmon.interfaces
UNION ALL
SELECT * FROM netmon.interfaces_raw;
