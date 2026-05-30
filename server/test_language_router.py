#
# Copyright (c) 2024-2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

from language_router import detect_language_cue


def test_hola_selects_spanish_even_after_menu():
    cue = detect_language_cue("Hola", selection_pending=False)

    assert cue is not None
    assert cue.language == "Spanish"
    assert cue.reason == "spanish_word"


def test_spanish_menu_number_selects_spanish_only_while_pending():
    cue = detect_language_cue("2", selection_pending=True)

    assert cue is not None
    assert cue.language == "Spanish"

    assert detect_language_cue("2", selection_pending=False) is None


def test_english_menu_number_selects_english_only_while_pending():
    cue = detect_language_cue("one", selection_pending=True)

    assert cue is not None
    assert cue.language == "English"

    assert detect_language_cue("one", selection_pending=False) is None


def test_explicit_language_names_can_switch_later():
    spanish = detect_language_cue("Can we do Spanish?", selection_pending=False)
    english = detect_language_cue("Switch to English please", selection_pending=False)

    assert spanish is not None
    assert spanish.language == "Spanish"
    assert english is not None
    assert english.language == "English"
