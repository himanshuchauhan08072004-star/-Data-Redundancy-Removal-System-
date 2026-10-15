"""Canonicalise raw input so equivalent records produce identical keys."""
import hashlib
import re
import unicodedata


def norm_name(s: str) -> str:
    """Lowercase letters only, token-sorted. Unicode-aware: non-Latin names
    (e.g. Devanagari) are kept; accents are folded on Latin letters only."""
    out: list[str] = []
    prev = ""
    for ch in unicodedata.normalize("NFKD", s or ""):
        cat = unicodedata.category(ch)
        if cat == "Mn" and prev.isascii() and prev.isalpha():
            continue  # José -> jose
        if cat[0] in ("L", "M"):
            out.append(ch.casefold())
            prev = ch
        else:
            out.append(" ")  # digits, punctuation, whitespace
            prev = ""
    # token-sorted so "Singh Rahul" == "Rahul Singh"
    return " ".join(sorted("".join(out).split()))


def norm_email(s: str) -> str:
    s = (s or "").strip().lower()
    local, _, domain = s.partition("@")
    local = local.split("+")[0]  # drop +tag
    if domain in ("gmail.com", "googlemail.com"):
        local = local.replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"


def norm_phone(s: str) -> str:
    digits = re.sub(r"\D", "", s or "")
    return digits[-10:] if len(digits) >= 10 else digits


def fingerprint(name_n: str, email_n: str, phone_n: str) -> str:
    return hashlib.sha256(f"{name_n}|{email_n}|{phone_n}".encode()).hexdigest()
