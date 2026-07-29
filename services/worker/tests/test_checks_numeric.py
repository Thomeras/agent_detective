"""Numeric fidelity: the "fluent but wrong" class the judged channel cannot see.

Every case here is taken from the foreign corpus rather than invented. The
boundary adapter of agent_topo_db's topology 22 ran percentages through an
exchange rate and produced a tidy, correctly-shaped table of wrong figures; the
judge scored it 0.9, and blame landed on the joiner that merged the numbers
instead of the node that made them.
"""

from __future__ import annotations

from worker.checks_numeric import (
    _numbers,
    _stated_rates,
    number_not_derivable_signals,
    numeric_content_lost_signals,
)

# The real payloads, verbatim.
ADAPTER_INPUT = """--- partnersky_feed ---
period;active_seats;cancel_rate_bp;satisfaction_0_10;currency;revenue
07/2026;3980;340;7.6;EUR;184300
06/2026;4010;180;8.1;EUR;187900
--- interni_model ---
mesic (YYYY-MM), aktivni_uzivatele (int), churn_pct (float, procenta),
nps (int, -100..100), trzby_czk (int)
Kurz: 1 EUR = 24.6 CZK"""

# What the model actually emitted: 340bp became 3.40/2.46 = 1.38, and revenue
# is 4538580 where 184300 x 24.6 is 4533780.
ADAPTER_OUTPUT_WRONG = """mesic;aktivni_uzivatele;churn_pct;nps;trzby_czk
2026-07;3980;1.38;76;4538580
2026-06;4010;0.73;81;4614340"""

ADAPTER_OUTPUT_CORRECT = """mesic;aktivni_uzivatele;churn_pct;nps;trzby_czk
2026-07;3980;3.40;76;4533780
2026-06;4010;1.80;81;4622340"""


class TestNumberNotDerivable:
    def test_the_real_miscoversion_is_caught(self) -> None:
        (sig,) = number_not_derivable_signals(ADAPTER_INPUT, ADAPTER_OUTPUT_WRONG)
        assert sig["severity"] == "fail"
        assert "1.38" in sig["detail"] and "4538580" in sig["detail"]

    def test_the_correct_conversion_is_silent(self) -> None:
        """The check is worthless if it cannot tell right from wrong: both tables
        have the same shape, the same columns and the same magnitudes."""
        assert number_not_derivable_signals(ADAPTER_INPUT, ADAPTER_OUTPUT_CORRECT) == []

    def test_the_rate_is_read_from_the_input_not_guessed(self) -> None:
        # "Kurz: 1 EUR = 24.6 CZK" — an earlier pattern captured the 1 out of
        # "1 EUR" and every conversion, correct ones included, looked underivable.
        assert _stated_rates(ADAPTER_INPUT) == [__import__("decimal").Decimal("24.6")]

    def test_a_csv_comma_is_a_separator_not_a_decimal_point(self) -> None:
        """`2026-06,4380,1.8,44` parsed as 6.4380 when scanned as one string,
        which corrupted every input figure and made the whole output look
        underivable — a false positive on a node that had done nothing wrong."""
        assert 4380 in _numbers("mesic,aktivni_uzivatele\n2026-06,4380,1.8,44")

    def test_prose_output_is_not_checked(self) -> None:
        """A node that analyses or forecasts legitimately produces figures that
        are no one's copy. Only a delimited table claims every cell traces back."""
        prose = (
            "Aktivni uzivatele klesli mezi cervnem a cervencem, coz odpovida "
            "poklesu o zhruba 2 procenta a zvysenemu churnu okolo 99999 bodu."
        )
        assert number_not_derivable_signals(ADAPTER_INPUT, prose) == []

    def test_a_thin_input_is_not_enough_to_judge(self) -> None:
        assert number_not_derivable_signals("cena: 5", "a;b;c\n1;2;3\n4;5;6") == []


class TestNumericContentLost:
    def test_figures_in_no_figures_out(self) -> None:
        output = (
            "Celkove lze rici, ze vykon v poslednim obdobi mirne kolisal. "
            "Aktivni uzivatele se drzeli na podobne urovni, churn se lehce "
            "zvysil a spokojenost zakazniku zustala v ocekavanem pasmu. "
            "Doporucujeme sledovat vyvoj v dalsim obdobi a zamerit se na "
            "stabilizaci klicovych ukazatelu napric segmenty."
        )
        (sig,) = numeric_content_lost_signals(ADAPTER_INPUT, output)
        assert sig["severity"] == "fail"

    def test_output_that_kept_its_figures_is_silent(self) -> None:
        assert numeric_content_lost_signals(ADAPTER_INPUT, ADAPTER_OUTPUT_CORRECT) == []

    def test_a_qualitative_node_is_not_punished(self) -> None:
        """Input without a table of figures means there was nothing to lose."""
        qualitative = "Zakaznici si stezuji na export a chvali synchronizaci."
        assert numeric_content_lost_signals(qualitative, "Temata: export, sync. " * 20) == []
