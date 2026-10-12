"""Unicode surface normalization shared by memory IDs and lexical filters."""
import re
import unicodedata


def normalize(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().replace("_", " ")
    return " ".join(re.findall(r"[^\W_]+", text, flags=re.UNICODE))


def normalize_unit(value: object) -> str:
    # Currency symbols must remain distinct; text normalization would erase them.
    return "".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())
