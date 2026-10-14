"""Canonicalise raw input so equivalent records produce identical keys."""
import hashlib
import re
import unicodedata


def norm_name(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z\s]", " ", s.lower())
    # token-sorted so "Singh Rahul" == "Rahul Singh"
    return " ".join(sorted(s.split()))


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
