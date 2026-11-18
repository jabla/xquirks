# Frozen content

Copies of real quirk files and manifest entries that the tests load, edit or compare. They are
frozen because the content may drop any file once ZHA covers its device itself. Replace a quirk
file only when the guard stops accepting it, for example in the pull request that brings a new
`fixed_code.json`; replace its entry in `manifest.json` with it.

- `manifest.json`: the manifest entries of the quirk files below that the guard tests check them
  against
- `z2m_tuya_ts0105.py.txt`, `z2m_bosch_bsen_w.py.txt`, `z2m_moes_zm6lt1.py.txt`,
  `z2m_aqara_qbcz15lm.py.txt`, `z2m_alias_moes_zc_hm.py.txt`, `z2m_alias_neo_nas_wv03b.py.txt`: the
  guard tests (`test_guard_files.py`, `test_guard.py`); the Tuya file also in the registry check of
  `test_edge_cases.py`, the MOES file also in the data query check of `test_data_query.py`
- `z2m_moes_zm6lt1.py.txt` is no longer built: when the guard started to require the fixed helper
  `_value_error_on_error` around its Tuya converters, they were wrapped by hand as the current files hold
  them (the helper after the file's classes, Revision line and manifest entry renewed)
- `z2m_tuya_ts0105.py.txt` and `z2m_moes_zm6lt1.py.txt`: when the guard started to require the typed Tuya
  cluster (the fixed `_dp_2_attr_update` class, `add_to_registry(replacement_cluster=TypedTuyaCluster)`), it
  was added by hand as published files have it (the class after the file's classes and helpers, Revision
  line and manifest entry renewed)
  and its method renewed by hand when it stopped range-checking plain number types and again when it
  bounded them to what the type holds (Revision line and manifest entry renewed each time)
- `z2m_sonoff_trv_zbt_*.py.txt`: two revisions of one file (change list tests), one of them also in
  the registry check of `test_edge_cases.py`
- `manifest-kinds.json`: real manifest entries, one of each kind the integration reads (a zcl file
  with endpoints, a Tuya file, alias files, a take-over, a refused take-over, a device not built,
  open upstream pull requests, a new file whose picture is not pinned yet), each file with its risk.
  Replace them with current entries of the same kinds when the manifest changes its fields.
- `key` in each entry of `manifest-kinds.json` with a file: the key of that entry's own file version (CONTENT.md).
  The text of the frozen `hysyiot_hs206z` version is gone: that entry has the revision, sha256 and key of a later
  version.
- `verify-bot-opened.json`, `verify-bot-edited.json`: issue events as GitHub sends them (fields trimmed) for
  `test_verify_bot.py`: a submission of the "Device test" form and an edit of its title
