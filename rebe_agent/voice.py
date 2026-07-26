"""The persona's mechanical rules, shared by both legs.

`docs/wayfinder/persona-spec.md` sets three limits on anything Rebe types that
can be checked without knowing what the message is about: at most one emoji, no
links she wrote herself, and no invented figure. The news leg and the webhook leg
both enforce them, for different reasons - the news leg appends the real link
after the model has answered, the webhook leg has no link to append at all - and
a rule that drifted between the two would be a bot tell in whichever leg lost it.

So the rules live here rather than in either leg. This module knows nothing about
news items, group messages, DeepSeek or Evolution; it is text in, verdict out.
"""

from __future__ import annotations

import re
import unicodedata

MAX_EMOJI = 1
"""The persona spec dials Rebe down to 0-1 per message, often none."""

LINKISH = re.compile(r"https?://|www\.|\.com\b|\.mx\b|\.ai\b|\.org\b", re.IGNORECASE)
"""Anything shaped like an address. She never types one; the news leg appends it."""

DIGITS = re.compile(r"\d+")
"""Every run of digits, for checking a claim against what she was actually given."""

_EMOJI = "So"
"""Unicode's "symbol, other" - the category most emoji live in."""

_MODIFIER = "Sk"
""""Symbol, modifier": the skin tones, which colour the emoji before them."""

_EMOJI_PRESENTATION = "\ufe0f"
"""U+FE0F. Turns an ordinary character into its emoji form: `!!` becomes an emoji."""

_JOINERS = frozenset("\u200d\ufe0f\ufe0e")
"""Zero-width joiner and the variation selectors, which glue two emoji into one."""

_REGIONAL_INDICATORS = frozenset(chr(point) for point in range(0x1F1E6, 0x1F200))
"""The A-Z letters flags are built from. Two of them are one flag, not two emoji."""


def emoji_count(text: str) -> int:
    """How many emoji a reader would see.

    Counted the way a reader counts them, not the way Unicode stores them, because
    the rule the persona spec sets - at most one, usually none - is about pictures
    on a screen. Three cases make that different from counting code points:

    - A joined sequence (a family, a skin-toned wave) is one picture.
    - A flag is *two* regional-indicator letters and one picture, so counting code
      points would refuse a perfectly ordinary "viva mexico \U0001f1f2\U0001f1fd".
    - A character followed by U+FE0F is an emoji whatever its own category says,
      so `‼️` counts even though `‼` alone is punctuation.

    Two emoji side by side with nothing between them are still two.
    """
    count = 0
    joined = False
    half_a_flag = False
    for index, character in enumerate(text):
        if character in _REGIONAL_INDICATORS:
            # The second letter completes the flag the first one opened.
            count += not (joined or half_a_flag)
            half_a_flag = not half_a_flag
            joined = False
            continue

        half_a_flag = False
        category = unicodedata.category(character)
        if category == _EMOJI or text[index + 1 : index + 2] == _EMOJI_PRESENTATION:
            count += not joined
            joined = False
        elif character in _JOINERS:
            joined = True
        elif category != _MODIFIER:
            joined = False
    return count
