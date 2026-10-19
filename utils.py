import re
import random

def parse_spintax(text: str) -> str:
    """Proses spintax secara rekursif. Mendukung nested: {A|{B|C}}."""
    pattern = re.compile(r"\{([^{}]+)\}")
    while pattern.search(text):
        text = pattern.sub(lambda m: random.choice(m.group(1).split("|")), text)
    return text
