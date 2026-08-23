-- 006. PLACEMENTS  (the core outcome table)

BEGIN;

CREATE TYPE placement_status AS ENUM
    ('offered','accepted','declined','active','completed',
     'no_show','terminated','replaced');

CREATE TABLE placements (
    placement_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id         UUID NOT NULL REFERENCES work_requests(request_id),
    candidate_id       UUID NOT NULL REFERENCES candidates(candidate_id),
    status             placement_status NOT NULL DEFAULT 'offered',
    agreed_pay_rwf     INTEGER NOT NULL CHECK (agreed_pay_rwf > 0),
    pay_unit           VARCHAR(12) NOT NULL
                       CHECK (pay_unit IN ('day','hour','month','task')),
    est_transport_rwf  INTEGER NOT NULL DEFAULT 0
                       CHECK (est_transport_rwf >= 0),  -- per working day
    est_commute_min    SMALLINT CHECK (est_commute_min >= 0),
    contract_ref       VARCHAR(60),
    supervisor_name    VARCHAR(120),
    -- Why the matching engine chose this candidate. Written at offer time and
    -- never recomputed: the coordinator must be able to defend the choice to
    -- the employer months later.
    match_reason       TEXT,
    offered_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_on         DATE,
    ended_on           DATE,
    -- Replacement chain. The evidence that the reliability guarantee was
    -- honoured: never overwrite a placement row to record a replacement.
    replaces_placement UUID REFERENCES placements(placement_id),
    UNIQUE (request_id, candidate_id),
    CONSTRAINT chk_placement_dates
        CHECK (ended_on IS NULL OR started_on IS NULL OR ended_on >= started_on),
    CONSTRAINT chk_not_self_replacing
        CHECK (replaces_placement IS NULL OR replaces_placement <> placement_id)
);

CREATE INDEX idx_place_status  ON placements (status);
CREATE INDEX idx_place_cand    ON placements (candidate_id);
CREATE INDEX idx_place_request ON placements (request_id);
-- Guarantee invocations: a placement may only be replaced once.
CREATE UNIQUE INDEX idx_one_replacement_per_placement
    ON placements (replaces_placement) WHERE replaces_placement IS NOT NULL;

-- Net daily earnings after transport. The headline outcome metric: no
-- competitor publishes it, and a role whose transport eats the wage is a
-- placement that dies in week two.
CREATE VIEW v_placement_net_pay AS
SELECT p.placement_id,
       p.candidate_id,
       p.agreed_pay_rwf,
       p.est_transport_rwf,
       p.agreed_pay_rwf - p.est_transport_rwf AS net_daily_rwf,
       ROUND(100.0 * p.est_transport_rwf
             / NULLIF(p.agreed_pay_rwf,0), 1) AS transport_pct
FROM placements p
WHERE p.pay_unit = 'day';

GRANT SELECT, INSERT, UPDATE ON placements TO app_operations;
GRANT SELECT ON v_placement_net_pay TO app_operations;

COMMIT;
