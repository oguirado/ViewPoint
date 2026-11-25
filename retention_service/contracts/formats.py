"""Format assertions for the local Draft 2020-12 contracts."""

from datetime import datetime, timedelta, timezone
from ipaddress import IPv4Address, IPv6Address
import re

from jsonschema import FormatChecker

DATE_TIME = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})[Tt]([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(?:\.[0-9]+)?(?:[Zz]|([+-])([0-9]{2}):([0-9]{2}))"
)
DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")

# RFC 3986, including validation of percent-encoded octets and IP literals.
PCT = r"%[A-Fa-f0-9]{2}"
NAME_CHAR = r"[A-Za-z0-9._~!$&'()*+,;=\-]"
NAME = rf"(?:{NAME_CHAR}|{PCT})"
PCHAR = rf"(?:{NAME}|[:@])"
QUERY_CHAR = rf"(?:{PCHAR}|[/?])"
URI = re.compile(
    rf"[A-Za-z][A-Za-z0-9+.\-]*:"
    rf"(?://(?P<authority>[^/?#]*)(?:/{PCHAR}*)*|"
    rf"/{PCHAR}+(?:/{PCHAR}*)*|/|{PCHAR}+(?:/{PCHAR}*)*|)"
    rf"(?:\?{QUERY_CHAR}*)?(?:\#{QUERY_CHAR}*)?"
)
AUTHORITY = re.compile(
    rf"(?:(?:{NAME}|:)*@)?(?:(?P<literal>\[[^\[\]]+\])|{NAME}*)(?::[0-9]*)?"
)
IP_FUTURE = re.compile(rf"[Vv][A-Fa-f0-9]+\.(?:{NAME_CHAR}|:)+")


def date_time(value):
    match = DATE_TIME.fullmatch(value)
    if match is None:
        return False
    year, month, day, hour, minute, second = map(int, match.groups()[:6])
    sign, offset_hour, offset_minute = match.groups()[6:]
    offset = timedelta()
    if sign is not None:
        hours, minutes = int(offset_hour), int(offset_minute)
        if hours > 23 or minutes > 59:
            return False
        offset = timedelta(hours=hours, minutes=minutes) * (-1 if sign == "-" else 1)
    date = datetime(year, month, day, hour, minute, min(second, 59), tzinfo=timezone(offset))
    if second < 60:
        return True
    if second == 60:
        utc = date.astimezone(timezone.utc)
        return (utc.month, utc.day) in {(6, 30), (12, 31)} and (utc.hour, utc.minute) == (23, 59)
    return False


def ipv4(value):
    IPv4Address(value)
    return True


def ipv6(value):
    if "%" in value:
        return False
    IPv6Address(value)
    return True


def hostname(value):
    name = value.removesuffix(".")
    return len(name) <= 253 and all(DNS_LABEL.fullmatch(label) for label in name.split("."))


def uri(value):
    match = URI.fullmatch(value)
    if match is None:
        return False
    authority = match["authority"]
    if authority is None:
        return True
    match = AUTHORITY.fullmatch(authority)
    if match is None:
        return False
    literal = match["literal"]
    return literal is None or bool(IP_FUTURE.fullmatch(literal[1:-1])) or ipv6(literal[1:-1])


def format_checker():
    checker = FormatChecker(formats=())
    for name, assertion in {
        "date-time": date_time, "ipv4": ipv4, "ipv6": ipv6, "hostname": hostname, "uri": uri,
    }.items():
        def check(value, assertion=assertion):
            return not isinstance(value, str) or assertion(value)

        checker.checks(name, raises=(ValueError, OverflowError))(check)
    return checker
