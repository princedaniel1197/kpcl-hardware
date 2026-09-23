-- What sample_seq_gap can and cannot see, recorded on the view itself so a
-- reader of the schema does not have to find it in the collector's README.

BEGIN;

COMMENT ON VIEW sample_seq_gap IS
    'Holes between archived rows of one collector run and tag: samples that run '
    'numbered for archiving and the archive does not hold under its key. '
    'held_by_other_runs counts rows another run holds in the same stretch '
    '(redundant collectors: the first write keeps the row); recorded_as_lost '
    'counts rows collector_loss accounts for. A hole needs neighbours: a missing '
    'sample before a run''s first archived row, or after its last, is not visible '
    'here. At a run''s start that is usually harmless -- a value unchanged since an '
    'earlier run archived it is delivered again on subscribe, and the earlier row '
    'is kept.';

COMMIT;
