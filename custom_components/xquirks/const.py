"""Constants of xquirks."""

DOMAIN = "xquirks"
TITLE = "xquirks"

ZHA_DOMAIN = "zha"
CONF_CUSTOM_QUIRKS_PATH = "custom_quirks_path"
CONF_ENABLE_QUIRKS = "enable_quirks"

# The repository the report links open an issue in.
REPOSITORY = "jabla/xquirks"

# The loader labels each registry entry "xquirks:<id> <rev>" (FORMAT.md). ZHA shows it in a device's quirk_class
# as "<module>:<label>".
LABEL_PREFIX = "xquirks:"

# A Tuya device that talks through the Tuya MCU cluster sends all its datapoints by itself only when it joins. A data
# query (this command without payload) makes it send them again. zhaquirks' Tuya MCU cluster names it query_data.
TUYA_CLUSTER = 0xEF00
TUYA_QUERY_DATA = 0x03

# The buttons on the page of a device on an xquirks definition (button.py), each its translation key: a problem report,
# and the confirmation that the definition works.
REPORT = "report"
WORKS = "works"

# The page of the integration: its Configure opens the options. Home Assistant has no link into the options themselves.
OPTIONS_PAGE = f"/config/integrations/integration/{DOMAIN}"

# The repair issues, each its own translation key.
NO_FOLDER = "missing_quirks_path"
QUIRKS_OFF = "quirks_disabled"
STUB_ISSUE = "stub"
OPTIONS_ISSUE = "options_broken"
CHANGED = "devices_changed"
ISSUES = (NO_FOLDER, QUIRKS_OFF, STUB_ISSUE, OPTIONS_ISSUE, CHANGED)
