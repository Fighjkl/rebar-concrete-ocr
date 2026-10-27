"""Character-level heat-lot comparisons for review only, never join keys."""
from .matching import heat_valid, norm


def single_heat_difference(certificate_value, nameplate_value):
    """Describe one observed character difference without correcting either value."""
    certificate, nameplate = norm(certificate_value), norm(nameplate_value)
    if (not heat_valid(certificate) or not heat_valid(nameplate) or
            len(certificate) != len(nameplate)):
        return None
    differences = [dict(position=index + 1, certificate_character=left,
                        nameplate_character=right)
                   for index, (left, right) in enumerate(zip(certificate, nameplate)) if left != right]
    if len(differences) != 1:
        return None
    # A single alphabetic OCR substitution (for example E/B) is plausible;
    # a numeric or punctuation difference may identify a different lot or
    # result from a lost separator. Keep this candidate class specifically
    # for letters so near-equal serial numbers do not create review noise.
    left, right = differences[0]['certificate_character'], differences[0]['nameplate_character']
    if not (left.isascii() and left.isalpha() and right.isascii() and right.isalpha()):
        return None
    item = differences[0]
    return dict(certificate_value=str(certificate_value), nameplate_value=str(nameplate_value),
                certificate_normalized=certificate, nameplate_normalized=nameplate,
                differences=differences, description=(f'第{item["position"]}个字符'
                    f'「{item["certificate_character"]}/{item["nameplate_character"]}」不同'),
                review_only=True, write_allowed=False)
