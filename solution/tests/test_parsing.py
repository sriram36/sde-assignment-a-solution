import pytest

from solution.parsing import (mask_phone, normalise_header, normalise_phone, parse_date, parse_decimal,
                              parse_enum, parse_timestamp_us, us_to_ist_iso)


@pytest.mark.parametrize("raw", ["9876543210", "+91 98765 43210", "098765 43210", "91-98765-43210",
                                 "(+91) 9876543210", "+919876543210", " 98765.43210 ", 9876543210])
def test_phone_valid_formats_all_canonicalise(raw):
    assert normalise_phone(raw) == "+919876543210"


@pytest.mark.parametrize("raw", ["", None, "N/A", "987654321", "98765432101", "5876543210",
                                 "022-2345-6789", "+1 415 555 0134", "0919876543210", "919876543210 1",
                                 "+91 5876543210", True, "९८७६५४३२१०"])
def test_phone_invalid(raw):
    assert normalise_phone(raw) is None


def test_phone_ten_digits_starting_91_is_not_stripped():
    assert normalise_phone("9123456789") == "+919123456789"


@pytest.mark.parametrize("d", ["6", "7", "8", "9"])
def test_phone_leading_digits_accepted(d):
    assert normalise_phone(d + "123456789") == "+91" + d + "123456789"


def test_mask():
    assert mask_phone("+919876543210") == "+91******3210"


@pytest.mark.parametrize("raw,iso", [("2026-10-05", "2026-10-05"), ("05/10/2026", "2026-10-05"),
                                     ("05-10-2026", "2026-10-05"), ("05-Oct-2026", "2026-10-05"),
                                     ("05-OCT-2026", "2026-10-05"), ("01-dec-2026", "2026-12-01"),
                                     ("29/02/2028", "2028-02-29")])
def test_date_valid_day_first(raw, iso):
    assert parse_date(raw) == iso


@pytest.mark.parametrize("raw", ["31/02/2026", "29/02/2027", "2026-13-01", "soon", "TBD", "",
                                 "10/13/2026", "05-Foo-2026", "2026/10/05"])
def test_date_invalid(raw):
    assert parse_date(raw) is None


def test_date_is_day_first_not_month_first():
    assert parse_date("03/04/2026") == "2026-04-03"


@pytest.mark.parametrize("raw,out", [("12345", "12345.00"), ("₹12,345", "12345.00"), ("Rs. 4521", "4521.00"),
                                     ("rs 4521.5", "4521.50"), ("Rs.4521", "4521.00"),
                                     ("INR 12345.5", "12345.50"), ("inr12,34,567", "1234567.00"),
                                     ("₹ 1,000.456", "1000.46"), ("0", "0.00")])
def test_decimal_valid(raw, out):
    assert parse_decimal(raw) == out


@pytest.mark.parametrize("raw", ["N/A", "-", "twelve thousand", "-5", "Rs.", "₹", "1.2.3", "USD 5", ""])
def test_decimal_invalid(raw):
    assert parse_decimal(raw) is None


def test_enum():
    vals = ["en", "hi"]
    assert parse_enum("EN", vals) == "en" and parse_enum(" hi ", vals) == "hi"
    assert parse_enum("Hindi", vals) is None


def test_header_normalisation():
    assert normalise_header("  Mobile_No ") == "mobile no"
    assert normalise_header("Policy__No") == "policy no"
    assert normalise_header("CUSTOMER   name") == "customer name"
    assert normalise_header("customer_name") == normalise_header("Customer Name")


def test_timestamps_three_forms_agree():
    a = parse_timestamp_us("2026-10-05T13:20:21Z")
    b = parse_timestamp_us("2026-10-05T18:50:21+05:30")
    c = parse_timestamp_us("2026-10-05 18:50:21")  # naive == IST
    assert a == b == c
    assert us_to_ist_iso(a) == "2026-10-05T18:50:21+05:30"


def test_naive_is_ist_not_utc():
    assert parse_timestamp_us("2026-10-05 00:30:00") == parse_timestamp_us("2026-10-04T19:00:00Z")


@pytest.mark.parametrize("raw", [None, "", "yesterday", 123, "2026-13-40T00:00:00Z"])
def test_timestamp_invalid(raw):
    assert parse_timestamp_us(raw) is None
