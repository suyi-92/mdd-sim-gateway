"""Number region is independent of the SIM's MCC and its ePDG routing country."""
import re
import phonenumbers


def number_country(value: str) -> str:
    if not re.fullmatch(r"\+[1-9][0-9]{4,14}", str(value or "")):
        return ""
    try:
        number = phonenumbers.parse(value, None)
        if not phonenumbers.is_valid_number(number):
            return ""
        region = phonenumbers.region_code_for_number(number) or ""
        return region.lower() if re.fullmatch(r"[A-Z]{2}", region) else ""
    except phonenumbers.NumberParseException:
        return ""
