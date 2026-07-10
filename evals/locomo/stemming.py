"""Self-contained Porter stemmer for official-compatible LoCoMo scoring.

The official LoCoMo scorer stems tokens with NLTK's ``PorterStemmer`` before
computing token F1. Butly does not depend on nltk, so this module implements
the original Porter (1980) algorithm. NLTK's default mode carries a few
extensions beyond the original paper, so rare words may stem slightly
differently; scores.json therefore labels the F1 as official-compatible
rather than official.
"""

from __future__ import annotations

_VOWELS = "aeiou"

_STEP2_SUFFIXES = (
    ("ational", "ate"),
    ("ization", "ize"),
    ("iveness", "ive"),
    ("fulness", "ful"),
    ("ousness", "ous"),
    ("biliti", "ble"),
    ("tional", "tion"),
    ("ation", "ate"),
    ("alism", "al"),
    ("aliti", "al"),
    ("iviti", "ive"),
    ("ousli", "ous"),
    ("entli", "ent"),
    ("anci", "ance"),
    ("enci", "ence"),
    ("izer", "ize"),
    ("abli", "able"),
    ("alli", "al"),
    ("ator", "ate"),
    ("eli", "e"),
)

_STEP3_SUFFIXES = (
    ("icate", "ic"),
    ("ative", ""),
    ("alize", "al"),
    ("iciti", "ic"),
    ("ical", "ic"),
    ("ness", ""),
    ("ful", ""),
)

_STEP4_SUFFIXES = (
    "ement",
    "ance",
    "ence",
    "able",
    "ible",
    "ment",
    "ant",
    "ent",
    "ion",
    "ism",
    "ate",
    "iti",
    "ous",
    "ive",
    "ize",
    "al",
    "er",
    "ic",
    "ou",
)


def _is_consonant(word: str, index: int) -> bool:
    char = word[index]
    if char in _VOWELS:
        return False
    if char == "y":
        return index == 0 or not _is_consonant(word, index - 1)
    return True


def _measure(stem: str) -> int:
    """Count VC sequences: the ``m`` of the Porter paper."""
    forms = "".join(
        "c" if _is_consonant(stem, index) else "v" for index in range(len(stem))
    )
    return forms.count("vc")


def _contains_vowel(stem: str) -> bool:
    return any(not _is_consonant(stem, index) for index in range(len(stem)))


def _ends_double_consonant(word: str) -> bool:
    return (
        len(word) >= 2
        and word[-1] == word[-2]
        and _is_consonant(word, len(word) - 1)
    )


def _ends_cvc(word: str) -> bool:
    if len(word) < 3:
        return False
    return (
        _is_consonant(word, len(word) - 3)
        and not _is_consonant(word, len(word) - 2)
        and _is_consonant(word, len(word) - 1)
        and word[-1] not in "wxy"
    )


def _step_1a(word: str) -> str:
    if word.endswith("sses"):
        return word[:-2]
    if word.endswith("ies"):
        return word[:-2]
    if word.endswith("ss"):
        return word
    if word.endswith("s"):
        return word[:-1]
    return word


def _step_1b(word: str) -> str:
    if word.endswith("eed"):
        return word[:-1] if _measure(word[:-3]) > 0 else word

    if word.endswith("ed") and _contains_vowel(word[:-2]):
        word = word[:-2]
    elif word.endswith("ing") and _contains_vowel(word[:-3]):
        word = word[:-3]
    else:
        return word

    if word.endswith(("at", "bl", "iz")):
        return word + "e"
    if _ends_double_consonant(word) and word[-1] not in "lsz":
        return word[:-1]
    if _measure(word) == 1 and _ends_cvc(word):
        return word + "e"
    return word


def _step_1c(word: str) -> str:
    if word.endswith("y") and _contains_vowel(word[:-1]):
        return word[:-1] + "i"
    return word


def _apply_suffix_map(word: str, suffixes) -> str:
    for suffix, replacement in suffixes:
        if word.endswith(suffix):
            stem = word[: -len(suffix)]
            if _measure(stem) > 0:
                return stem + replacement
            return word
    return word


def _step_4(word: str) -> str:
    for suffix in _STEP4_SUFFIXES:
        if word.endswith(suffix):
            stem = word[: -len(suffix)]
            if _measure(stem) <= 1:
                return word
            if suffix == "ion" and not stem.endswith(("s", "t")):
                return word
            return stem
    return word


def _step_5(word: str) -> str:
    if word.endswith("e"):
        stem = word[:-1]
        measure = _measure(stem)
        if measure > 1 or (measure == 1 and not _ends_cvc(stem)):
            word = stem
    if _measure(word) > 1 and _ends_double_consonant(word) and word.endswith("l"):
        word = word[:-1]
    return word


def stem(word: str) -> str:
    """Stem one lowercase-insensitive token with the original Porter rules."""
    word = word.lower()
    if len(word) <= 2:
        return word
    word = _step_1a(word)
    word = _step_1b(word)
    word = _step_1c(word)
    word = _apply_suffix_map(word, _STEP2_SUFFIXES)
    word = _apply_suffix_map(word, _STEP3_SUFFIXES)
    word = _step_4(word)
    word = _step_5(word)
    return word
