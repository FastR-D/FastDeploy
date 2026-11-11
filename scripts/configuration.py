"""Parse workflow inputs without evaluating shell or dotenv interpolation."""
import json
import re

KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def json_object(text):
    value = json.loads(text, object_pairs_hook=object_pairs)
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def validate_values(values):
    for key, value in values.items():
        if not KEY.fullmatch(key):
            raise ValueError("invalid environment variable name")
        if not isinstance(value, str) or "\0" in value:
            raise ValueError(f"{key} must be a string without NUL")
    return values


def parse_env(text):
    result = {}
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not KEY.fullmatch(key):
            raise ValueError(f"invalid runtime-env line {number}")
        if key in result:
            raise ValueError(f"duplicate environment key: {key}")
        # Values are literal: quotes, $ and # are not shell/dotenv syntax.
        result[key] = value
    return validate_values(result)


def merge_env(public, secret):
    public = parse_env(public)
    def sensitive(key, value):
        policy = re.search(r"_ALLOW_LEGACY_[A-Z_]*(?:KEY|KEYS)$", key) and value in {"true", "false"}
        return not policy and re.search(r"(?:SECRET|PASSWORD|PRIVATE_KEY|API_KEYS?|TOKEN)$", key)
    if any(sensitive(key, value) for key, value in public.items()):
        raise ValueError("sensitive environment names must use runtime-secrets")
    secret = validate_values(json_object(secret))
    if public.keys() & secret.keys():
        raise ValueError("runtime-env and runtime-secrets contain duplicate keys")
    if any(key.startswith("VITE_") for key in secret):
        raise ValueError("VITE_ values must not be supplied as runtime secrets")
    if any(not value for value in secret.values()):
        raise ValueError("a supplied runtime secret is empty")
    return public | secret
