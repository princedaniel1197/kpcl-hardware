-- Where a tag lives in the source address space. (Stage 10)
--
-- The collector resolved every tag under a hardcoded "Unit1" object. That is a
-- latent violation of rule 7: adding ANY second unit -- the bench rig, a second
-- simulated unit, a second station -- would have required editing Python to
-- change a string. The parent object is configuration, like everything else
-- about a tag.

BEGIN;

ALTER TABLE tag ADD COLUMN source_path text NOT NULL DEFAULT 'Unit1';

COMMENT ON COLUMN tag.source_path IS
    'Parent object in the source address space, e.g. Unit1 or BenchRig';

COMMIT;
