-- Travel Story Book schema.
--
-- One database per trip, so `trip` holds exactly one row and no query needs a trip filter.
-- The table is kept (rather than inlining its fields) so a multi-trip Phase 2 needs no
-- migration.
--
-- Media is identified by content hash everywhere, never by path. Re-importing the same photo
-- from a different folder is a no-op, which is what makes the pipeline idempotent.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS device (
    id                   TEXT PRIMARY KEY,
    make                 TEXT,
    model                TEXT,
    clock_offset_minutes INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS place (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    lat_key  REAL NOT NULL,
    lon_key  REAL NOT NULL,
    poi      TEXT,
    city     TEXT,
    region   TEXT,
    country  TEXT,
    source   TEXT NOT NULL,
    UNIQUE (lat_key, lon_key, source)
);

CREATE TABLE IF NOT EXISTS media (
    hash            TEXT PRIMARY KEY,
    path            TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('image', 'video')),
    bytes           INTEGER NOT NULL,
    mtime           REAL NOT NULL,
    width           INTEGER,
    height          INTEGER,
    duration        REAL,
    device_id       TEXT REFERENCES device (id),
    taken_local     TEXT,
    taken_utc       TEXT,
    tz_name         TEXT,
    tz_offset_minutes INTEGER,
    tz_source       TEXT CHECK (tz_source IN ('exif_offset', 'gps', 'device_neighbor', 'config', 'unknown')),
    -- The raw OffsetTimeOriginal tag, kept separate from the *resolved* tz_offset_minutes.
    -- Timezone resolution reads this and writes the resolved fields; sharing one column made the
    -- stage overwrite its own input, so a second run silently produced a different (worse)
    -- answer than the first.
    exif_offset_minutes INTEGER,
    lat             REAL,
    lon             REAL,
    altitude        REAL,
    gps_source      TEXT CHECK (gps_source IN ('exif', 'interpolated', 'manual', 'none')),
    gps_confidence  REAL,
    place_id        INTEGER REFERENCES place (id),
    is_near_home    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_media_taken_utc ON media (taken_utc);
CREATE INDEX IF NOT EXISTS idx_media_device ON media (device_id, taken_utc);
CREATE INDEX IF NOT EXISTS idx_media_kind ON media (kind);

CREATE TABLE IF NOT EXISTS trip (
    id          INTEGER PRIMARY KEY CHECK (id = 1),  -- one trip per database
    name        TEXT NOT NULL,
    start_local TEXT,
    end_local   TEXT,
    home_lat    REAL,
    home_lon    REAL
);

CREATE TABLE IF NOT EXISTS day (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id    INTEGER NOT NULL REFERENCES trip (id) ON DELETE CASCADE,
    local_date TEXT NOT NULL,
    UNIQUE (trip_id, local_date)
);

CREATE TABLE IF NOT EXISTS event (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    day_id       INTEGER NOT NULL REFERENCES day (id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL,
    start_utc    TEXT,
    end_utc      TEXT,
    centroid_lat REAL,
    centroid_lon REAL,
    place_id     INTEGER REFERENCES place (id),
    label        TEXT,
    UNIQUE (day_id, seq)
);

CREATE TABLE IF NOT EXISTS media_event (
    media_hash TEXT NOT NULL REFERENCES media (hash) ON DELETE CASCADE,
    event_id   INTEGER NOT NULL REFERENCES event (id) ON DELETE CASCADE,
    PRIMARY KEY (media_hash, event_id)
);

CREATE INDEX IF NOT EXISTS idx_media_event_event ON media_event (event_id);

CREATE TABLE IF NOT EXISTS cluster (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    INTEGER NOT NULL REFERENCES event (id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('exact', 'burst', 'similar')),
    keeper_hash TEXT REFERENCES media (hash)
);

CREATE TABLE IF NOT EXISTS media_cluster (
    media_hash TEXT NOT NULL REFERENCES media (hash) ON DELETE CASCADE,
    cluster_id INTEGER NOT NULL REFERENCES cluster (id) ON DELETE CASCADE,
    PRIMARY KEY (media_hash, cluster_id)
);

CREATE INDEX IF NOT EXISTS idx_media_cluster_cluster ON media_cluster (cluster_id);

CREATE TABLE IF NOT EXISTS score (
    media_hash    TEXT PRIMARY KEY REFERENCES media (hash) ON DELETE CASCADE,
    sharpness     REAL,
    exposure      REAL,
    contrast      REAL,
    face_count    INTEGER,
    face_max_frac REAL,
    content_class TEXT,
    overall       REAL
);

CREATE INDEX IF NOT EXISTS idx_score_overall ON score (overall DESC);

CREATE TABLE IF NOT EXISTS embedding (
    media_hash TEXT PRIMARY KEY REFERENCES media (hash) ON DELETE CASCADE,
    model      TEXT NOT NULL,
    dim        INTEGER NOT NULL,
    vector     BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS phash (
    media_hash TEXT PRIMARY KEY REFERENCES media (hash) ON DELETE CASCADE,
    value      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS landmark (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    confidence     REAL,
    description    TEXT,
    source         TEXT NOT NULL,
    prompt_version INTEGER NOT NULL DEFAULT 1,
    UNIQUE (name, source, prompt_version)
);

CREATE TABLE IF NOT EXISTS media_landmark (
    media_hash  TEXT NOT NULL REFERENCES media (hash) ON DELETE CASCADE,
    landmark_id INTEGER NOT NULL REFERENCES landmark (id) ON DELETE CASCADE,
    PRIMARY KEY (media_hash, landmark_id)
);

-- Derived video facts. A first pass kept these in a JSON sidecar under the cache dir, which
-- would have forced the report and package builders to learn an informal file convention.
-- Frame paths are stored relative to the output directory so the export stays portable.
CREATE TABLE IF NOT EXISTS video_meta (
    media_hash    TEXT PRIMARY KEY REFERENCES media (hash) ON DELETE CASCADE,
    fps           REAL,
    poster_path   TEXT,
    keyframe_paths TEXT,   -- JSON array of output-relative paths
    motion_score  REAL,
    mean_volume_db REAL,
    has_speech    INTEGER
);

CREATE TABLE IF NOT EXISTS transcript (
    media_hash TEXT PRIMARY KEY REFERENCES media (hash) ON DELETE CASCADE,
    model      TEXT NOT NULL,
    text       TEXT NOT NULL,
    segments   TEXT
);

CREATE TABLE IF NOT EXISTS selection (
    media_hash TEXT NOT NULL REFERENCES media (hash) ON DELETE CASCADE,
    scope      TEXT NOT NULL CHECK (scope IN ('cluster', 'event', 'day', 'trip')),
    scope_id   INTEGER NOT NULL,
    rank       INTEGER NOT NULL,
    reason     TEXT,
    PRIMARY KEY (media_hash, scope, scope_id)
);

CREATE INDEX IF NOT EXISTS idx_selection_scope ON selection (scope, scope_id, rank);

-- The resumability backbone. One row per (media, stage, stage_version); a stage recomputes
-- only what is missing or stale. Bumping a stage's version constant invalidates exactly
-- that stage.
CREATE TABLE IF NOT EXISTS stage_result (
    media_hash    TEXT NOT NULL,
    stage         TEXT NOT NULL,
    stage_version INTEGER NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('ok', 'failed', 'skipped')),
    error         TEXT,
    computed_at   TEXT NOT NULL,
    PRIMARY KEY (media_hash, stage)
);

CREATE INDEX IF NOT EXISTS idx_stage_result_stage ON stage_result (stage, status);
