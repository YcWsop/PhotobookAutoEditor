"""Interface for an AI (or any other) reviewer of the pairs the rules could not decide.

auto.py hands `review()` a list of dicts, one per undecided neighbouring pair:
    kind        'uncertain' (the seam rule almost passed) or 'flat' (plain-colour seam, nearly equal colours on both sides)
    left, right page names in the order the rules guessed (left | right)
    left_img, right_img   paths of the two normalised page images (JPEG files in the book's .work folder)
    stats       seam numbers (seam, ratio, jump, std)
It returns the pairs to MERGE as [(left_name, right_name), ...]. Everything not returned stays separate and stays on the
contact sheet for a human look. The default does nothing, so nothing leaves the machine.

PRIVACY: this repository must never contain API keys, tokens, account names or personal paths. A real reviewer reads its
credentials from ENVIRONMENT VARIABLES only (set outside the repo); never write them into a file here."""


def review(queue):
    return []
