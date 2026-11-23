# Frozen content

- `z2m_bosch_bsen_w.py.txt`, `z2m_aqara_qbcz15lm.py.txt`: quirk files of the last content state, the ones their
  definitions in `definitions.jsonl` were lifted from. `test_end_to_end.py` compares the unique ids of each device's
  entities with the definition's. Replace a file when its definition gets a new revision, or pick another definition.
