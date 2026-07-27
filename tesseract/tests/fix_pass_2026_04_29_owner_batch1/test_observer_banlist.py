"""Owner batch 1 — observer banlist filter (#2).

The observer prompt tells the model never to emit vague placeholder phrases
like "something worth noting", but it leaks them anyway. The server-side
filter in `Observer._run_stream` should drop these phrases as if they were
NONE so the right panel + chat surfaces stay clean.
"""

from __future__ import annotations

from tesseract.brain.observer import _is_banned_observation


def test_exact_banlist_phrase_is_banned():
    assert _is_banned_observation("something worth noting") is True


def test_banlist_phrase_with_trailing_punctuation():
    assert _is_banned_observation("something worth noting.") is True
    assert _is_banned_observation("something worth noting!") is True


def test_banlist_phrase_inside_longer_sentence():
    # The model sometimes wraps the placeholder in flavor text like
    # "There is something worth noting here." — still low-signal.
    assert _is_banned_observation("There is something worth noting here.") is True


def test_specific_observation_is_not_banned():
    text = "Operator restated the kernel-lockdown rule for the third time today."
    assert _is_banned_observation(text) is False


def test_empty_or_whitespace_is_not_banned():
    assert _is_banned_observation("") is False
    assert _is_banned_observation("   ") is False


def test_all_banlist_phrases_are_caught():
    phrases = [
        "Something worth noting",
        "nothing significant",
        "a point of interest",
        "something to consider",
        "noteworthy moment",
        "interesting exchange",
    ]
    for phrase in phrases:
        assert _is_banned_observation(phrase) is True, f"missed: {phrase!r}"
