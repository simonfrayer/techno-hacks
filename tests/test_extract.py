import pytest

from techno_hacks.extract import Details, NotUnderstood, parse_details


@pytest.mark.parametrize(
    "said, expected",
    [
        ("Ibuprofen 400, expires March 2027, 20 tablets", Details("Ibuprofen 400", "2027-03", "20")),
        ("Paracetamol, 03/2027, thirty tablets", Details("Paracetamol", "2027-03", "30")),
        ("Aspirin expiry 2026-11 count 50", Details("Aspirin", "2026-11", "50")),
        ("Amoxicillin expires on the 15th of March 2027, 12 capsules",
         Details("Amoxicillin", "2027-03-15", "12")),
        ("Vitamin D, best before 15.03.2027, 100 tablets", Details("Vitamin D", "2027-03-15", "100")),
        ("Metformin 500 March twenty twenty seven thirty two tablets",
         Details("Metformin 500", "2027-03", "32")),
        ("Name is Cetirizine, expiry date June 2026, 7 pills", Details("Cetirizine", "2026-06", "7")),
        ("Omeprazole, December 26, 28", Details("Omeprazole", "2026-12", "28")),
        ("Loratadine expires 9/27 tablets 14", Details("Loratadine", "2027-09", "14")),
        ("Zinc one hundred tablets expires January 2028", Details("Zinc", "2028-01", "100")),
    ],
)
def test_parses_spoken_details(said, expected):
    assert parse_details(said) == expected


@pytest.mark.parametrize(
    "said, reason",
    [
        ("", "Heard nothing"),
        ("Ibuprofen 20 tablets", "No expiry date"),
        ("Ibuprofen expires March 2027", "No tablet count"),
        ("expires March 2027, 20 tablets", "No product name"),
    ],
)
def test_reports_what_is_missing(said, reason):
    with pytest.raises(NotUnderstood, match=reason):
        parse_details(said)


def test_error_messages_fit_the_screen():
    for said in ["", "x 20 tablets", "x expires March 2027", "expires March 2027, 20 tablets"]:
        with pytest.raises(NotUnderstood) as exc:
            parse_details(said)
        assert len(str(exc.value)) <= 39
