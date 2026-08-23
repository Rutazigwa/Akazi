-- 002. CANDIDATES  (operational profile) + availability

BEGIN;

CREATE TYPE candidate_status AS ENUM
    ('registered','assessed','trained','placed','inactive','withdrawn');

CREATE TABLE candidates (
    candidate_id     UUID PRIMARY KEY
                     REFERENCES candidate_identity(candidate_id)
                     ON DELETE CASCADE,
    display_name     VARCHAR(120)     NOT NULL,
    gender           CHAR(1)          CHECK (gender IN ('F','M','X')),
    district         VARCHAR(60)      NOT NULL,
    sector           VARCHAR(60)      NOT NULL,
    cell             VARCHAR(60),
    home_lat         NUMERIC(9,6),
    home_lng         NUMERIC(9,6),
    education_level  VARCHAR(40),
    languages        TEXT[]           NOT NULL DEFAULT '{}',
    has_smartphone   BOOLEAN          NOT NULL DEFAULT FALSE,
    momo_registered  BOOLEAN          NOT NULL DEFAULT FALSE,
    max_commute_rwf  INTEGER          CHECK (max_commute_rwf >= 0), -- daily transport ceiling
    max_commute_min  INTEGER          CHECK (max_commute_min >= 0),
    -- Explicit opt-in required by matching filter 3 before a woman can be
    -- matched to a shift ending after dark without employer-covered transport.
    accepts_after_dark BOOLEAN        NOT NULL DEFAULT FALSE,
    status           candidate_status NOT NULL DEFAULT 'registered',
    registered_by    UUID             REFERENCES staff(staff_id),
    created_at       TIMESTAMPTZ      NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ      NOT NULL DEFAULT now()
);

CREATE INDEX idx_cand_geo    ON candidates (district, sector);
CREATE INDEX idx_cand_status ON candidates (status);

CREATE TABLE availability (
    availability_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    candidate_id    UUID NOT NULL REFERENCES candidates(candidate_id)
                    ON DELETE CASCADE,
    day_of_week     SMALLINT NOT NULL CHECK (day_of_week BETWEEN 0 AND 6),
    start_time      TIME NOT NULL,
    end_time        TIME NOT NULL,
    CONSTRAINT chk_window CHECK (end_time > start_time),
    UNIQUE (candidate_id, day_of_week, start_time)
);

CREATE INDEX idx_avail_cand ON availability (candidate_id);

-- updated_at maintenance
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_candidates_updated_at
    BEFORE UPDATE ON candidates
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

GRANT SELECT, INSERT, UPDATE ON candidates, availability TO app_operations;

COMMIT;
