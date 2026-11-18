"""Constants of the quirk installer."""

from datetime import timedelta

DOMAIN = "xquirks"
TITLE = "xquirks"
# The service device's model: the integration page shows it under the device's name.
DESCRIPTION = "Extra quirks for ZHA"

ZHA_DOMAIN = "zha"
CONF_CUSTOM_QUIRKS_PATH = "custom_quirks_path"
# Options: the ids of the devices xquirks leaves alone.
CONF_EXCLUDED = "excluded"
# Options: offer high-risk files that are not verified on a device (held back while off).
CONF_HIGH_RISK = "high_risk"

STORAGE_KEY = f"{DOMAIN}.installed"
STORAGE_VERSION = 1

# Seconds to wait for more device registry changes before checking again.
REFRESH_COOLDOWN = 10

# A Tuya device that talks through the Tuya MCU cluster sends all its datapoints by itself only when
# it joins. A data query (a command without payload) makes it send them again. The quirk ZHA applies
# tells such a device: it puts zhaquirks' Tuya MCU cluster on it, which names this command query_data.
TUYA_CLUSTER = 0xEF00
TUYA_QUERY_DATA = 0x03

# The repository whose content-<n> tags carry the quirk files.
REPOSITORY = "jabla/xquirks"
TAGS_URL = f"https://api.github.com/repos/{REPOSITORY}/git/matching-refs/tags/content-"
# Files are read at the commit a tag points to, never by the tag's name: a branch of the same name
# must not stand in for the protected tag.
RAW_URL = f"https://raw.githubusercontent.com/{REPOSITORY}/{{commit}}/{{path}}"
CHECK_INTERVAL = timedelta(hours=6)
# How often to try again while no content state is in use.
RETRY_INTERVAL = timedelta(minutes=15)
FETCH_TIMEOUT = 30
# A quirk file or manifest larger than this is refused.
MAX_DOWNLOAD = 4 * 1024 * 1024
# GitHub's issue search, asked without a token for the confirmations of a file's key (checks.py). Its own quota: 10
# requests a minute per address.
SEARCH_URL = "https://api.github.com/search/issues"
# Hits per page, oldest first. Every variant of a file has its own issues with the file's key: the next page is asked
# while a variant has no match yet (remote.async_search), SEARCH_PAGES pages at most (SEARCH_PAGES * SEARCH_PAGE stays
# within confirm.MAX_ITEMS).
SEARCH_PAGE = 10
SEARCH_PAGES = 5
# Seconds per page.
SEARCH_TIMEOUT = 10
# Bytes per page. Every hit carries its issue's body: up to 65,536 characters, and anyone can open an issue whose
# characters JSON writes as `\u003c` (6 bytes each). With a title of 256 such characters a hit holds 394,752 bytes of
# them, the rest of a hit (links, account, labels, reactions: about 3 KB) fits in the remaining 129,536 bytes.
MAX_SEARCH = SEARCH_PAGE * 512 * 1024

CONTENT_STORAGE_KEY = f"{DOMAIN}.content"
CONTENT_STORAGE_VERSION = 1
