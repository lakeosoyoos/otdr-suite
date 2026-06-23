"""helixcal — after-the-fact OTDR-fiber-distance → cable-sheath-distance
calibration (Corning AEN142 helix / EFL factor).

This package is a *consumer* of the suite's existing SOR parser
(``splicereport/sor_reader324802a.py``).  It does NOT fork or rewrite any
binary parsing — ``sor_fields`` is a thin adapter that reuses
``_parse_block_directory`` / ``parse_sor_full`` and only adds the two
fields the parser does not already surface (stored IOR, GenParams
locations).

Public surface:
    sor_fields.read_stored_ior(filepath)        -> float | None
    sor_fields.read_genparams(filepath)         -> dict
    sor_fields.read_trace_record(filepath)      -> dict
    anchors.load_anchors(path, ...)             -> list[Anchor]
    cable_db.resolve_cable_type(...)            -> CableTypeResolution
    cable_db.detect_from_genparams(gp)          -> (cable_type|None, token)
    cable_db.register(CableEntry)               -> CableEntry
    calibrate.calibrate(records, anchors, ...)  -> CalibrationResult
    report.write_report(result, output_path)    -> str
"""

__all__ = ["sor_fields", "anchors", "cable_db", "calibrate", "report"]
